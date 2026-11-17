"""Native MPRIS2 D-Bus server (org.mpris.MediaPlayer2.cero) compatible with playerctl."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib
from music_assistant_models.enums import PlaybackState, RepeatMode

from . import __app_id__
from .ma_service import MAService, NowPlayingState

logger = logging.getLogger(__name__)

MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"
MPRIS_BUS_NAME_DEFAULT = "org.mpris.MediaPlayer2.cero"
MPRIS_ROOT_IFACE = "org.mpris.MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

MPRIS_INTROSPECTION_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg direction="in" name="Offset" type="x"/>
    </method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri">
      <arg direction="in" name="Uri" type="s"/>
    </method>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


def _make_track_object_path(raw_id: str) -> str:
    if not raw_id:
        return "/org/mpris/MediaPlayer2/TrackList/NoTrack"
    digest = hashlib.sha1(raw_id.encode("utf-8", errors="ignore")).hexdigest()[:20]
    return f"/xyz/delaosa/CERO/Track/{digest}"


class MprisServer:
    """Publishes and syncs the player state on the D-Bus session bus."""

    def __init__(
        self,
        ma_service: MAService,
        *,
        on_raise: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        bus_name: str = MPRIS_BUS_NAME_DEFAULT,
    ) -> None:
        self.ma_service = ma_service
        self._on_raise = on_raise
        self._on_quit = on_quit
        self._bus_name = bus_name

        self._conn: Gio.DBusConnection | None = None
        self._owner_id: int = 0
        self._reg_ids: list[int] = []
        self._state = NowPlayingState()
        self._last_emitted_sig: tuple[Any, ...] | None = None
        self._local_art_override: dict[str, str] = {}

    def set_local_artwork_file(self, image_url: str, file_uri: str) -> None:
        """Allows exposing file:// in mpris:artUrl for desktop applets that do not download over HTTP."""
        if not image_url or not file_uri:
            return
        self._local_art_override[image_url] = file_uri
        if self._state.image_url == image_url:
            self.update_state(self.ma_service.get_now_playing(), force_emit=True)

    def start(self) -> None:
        if self._owner_id:
            return
        node_info = Gio.DBusNodeInfo.new_for_xml(MPRIS_INTROSPECTION_XML)

        def _on_bus_acquired(conn: Gio.DBusConnection, _name: str) -> None:
            self._conn = conn
            for iface in node_info.interfaces:
                reg_id = conn.register_object(
                    MPRIS_OBJECT_PATH,
                    iface,
                    self._handle_method_call,
                    self._handle_get_property,
                    self._handle_set_property,
                )
                if reg_id > 0:
                    self._reg_ids.append(reg_id)
            logger.info("MPRIS2 service registered on D-Bus as %s", self._bus_name)

        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            self._bus_name,
            Gio.BusNameOwnerFlags.NONE,
            _on_bus_acquired,
            None,
            None,
        )
        self.ma_service.on_state_changed(self.update_state)

    def stop(self) -> None:
        if self._conn is not None:
            for reg_id in self._reg_ids:
                try:
                    self._conn.unregister_object(reg_id)
                except Exception:
                    pass
            self._reg_ids.clear()
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0
        self._conn = None

    # --- MA state -> MPRIS2 properties conversion ---

    @staticmethod
    def _playback_status_str(state: PlaybackState) -> str:
        if state == PlaybackState.PLAYING:
            return "Playing"
        if state == PlaybackState.PAUSED:
            return "Paused"
        return "Stopped"

    @staticmethod
    def _loop_status_str(mode: RepeatMode) -> str:
        if mode == RepeatMode.ONE:
            return "Track"
        if mode == RepeatMode.ALL:
            return "Playlist"
        return "None"

    @staticmethod
    def _parse_loop_status(val: str) -> RepeatMode:
        if val == "Track":
            return RepeatMode.ONE
        if val == "Playlist":
            return RepeatMode.ALL
        return RepeatMode.OFF

    def _build_metadata_dict(self, st: NowPlayingState) -> dict[str, GLib.Variant]:
        raw_id = st.queue_item_id or st.uri or st.title
        track_path = _make_track_object_path(raw_id)
        meta: dict[str, GLib.Variant] = {
            "mpris:trackid": GLib.Variant("o", track_path),
        }
        if st.duration > 0:
            meta["mpris:length"] = GLib.Variant("x", int(st.duration * 1_000_000))
        if st.title:
            meta["xesam:title"] = GLib.Variant("s", st.title)
        artists = [a.strip() for a in st.artist.split(",") if a.strip()] if st.artist else []
        meta["xesam:artist"] = GLib.Variant("as", artists or ([""] if st.title else []))
        if st.album:
            meta["xesam:album"] = GLib.Variant("s", st.album)
        if st.uri:
            meta["xesam:url"] = GLib.Variant("s", st.uri)
        if st.image_url:
            art_uri = self._local_art_override.get(st.image_url, st.image_url)
            meta["mpris:artUrl"] = GLib.Variant("s", art_uri)
        return meta

    def update_state(self, new_state: NowPlayingState, force_emit: bool = False) -> None:
        prev_pos = self._state.position
        prev_track = self._state.queue_item_id or self._state.uri
        self._state = new_state

        if self._conn is None:
            return

        pb_status = self._playback_status_str(new_state.playback_state)
        loop_status = self._loop_status_str(new_state.repeat)
        art_uri = self._local_art_override.get(new_state.image_url, new_state.image_url)
        sig = (
            pb_status,
            loop_status,
            new_state.shuffle,
            new_state.volume,
            new_state.title,
            new_state.artist,
            new_state.album,
            new_state.duration,
            new_state.queue_item_id,
            new_state.uri,
            art_uri,
            new_state.can_seek,
        )

        # Detect abrupt time jumps within the same track -> Seeked signal
        cur_track = new_state.queue_item_id or new_state.uri
        if cur_track and cur_track == prev_track and abs(new_state.position - prev_pos) > 3.5:
            self.emit_seeked(int(new_state.position * 1_000_000))

        if not force_emit and sig == self._last_emitted_sig:
            return
        self._last_emitted_sig = sig

        changed: dict[str, GLib.Variant] = {
            "PlaybackStatus": GLib.Variant("s", pb_status),
            "LoopStatus": GLib.Variant("s", loop_status),
            "Shuffle": GLib.Variant("b", bool(new_state.shuffle)),
            "Volume": GLib.Variant("d", max(0.0, min(1.0, new_state.volume / 100.0))),
            "Metadata": GLib.Variant("a{sv}", self._build_metadata_dict(new_state)),
            "CanGoNext": GLib.Variant("b", bool(new_state.can_next)),
            "CanGoPrevious": GLib.Variant("b", bool(new_state.can_previous)),
            "CanPlay": GLib.Variant("b", bool(new_state.can_play)),
            "CanPause": GLib.Variant("b", bool(new_state.can_pause)),
            "CanSeek": GLib.Variant("b", bool(new_state.can_seek)),
        }
        self._emit_properties_changed(MPRIS_PLAYER_IFACE, changed)

    def emit_seeked(self, position_us: int) -> None:
        if self._conn is None:
            return
        try:
            self._conn.emit_signal(
                None,
                MPRIS_OBJECT_PATH,
                MPRIS_PLAYER_IFACE,
                "Seeked",
                GLib.Variant("(x)", (max(0, int(position_us)),)),
            )
        except Exception as err:
            logger.debug("Error emitting MPRIS Seeked signal: %s", err)

    def _emit_properties_changed(
        self, interface_name: str, changed: dict[str, GLib.Variant]
    ) -> None:
        if self._conn is None or not changed:
            return
        try:
            self._conn.emit_signal(
                None,
                MPRIS_OBJECT_PATH,
                DBUS_PROPS_IFACE,
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (interface_name, changed, [])),
            )
        except Exception as err:
            logger.debug("Error emitting MPRIS PropertiesChanged: %s", err)

    # --- GDBus handlers ---

    def _handle_get_property(
        self,
        _conn: Gio.DBusConnection,
        _sender: str,
        _object_path: str,
        interface_name: str,
        property_name: str,
    ) -> GLib.Variant | None:
        st = self.ma_service.get_now_playing() if self.ma_service.client else self._state

        if interface_name == MPRIS_ROOT_IFACE:
            root_props: dict[str, GLib.Variant] = {
                "CanQuit": GLib.Variant("b", True),
                "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", "CERO"),
                "DesktopEntry": GLib.Variant("s", __app_id__),
                "SupportedUriSchemes": GLib.Variant("as", ["library", "spotify", "http", "https"]),
                "SupportedMimeTypes": GLib.Variant(
                    "as", ["audio/flac", "audio/mpeg", "audio/ogg", "audio/wav"]
                ),
            }
            return root_props.get(property_name)

        if interface_name == MPRIS_PLAYER_IFACE:
            if property_name == "PlaybackStatus":
                return GLib.Variant("s", self._playback_status_str(st.playback_state))
            if property_name == "LoopStatus":
                return GLib.Variant("s", self._loop_status_str(st.repeat))
            if property_name == "Rate":
                return GLib.Variant("d", 1.0)
            if property_name == "Shuffle":
                return GLib.Variant("b", bool(st.shuffle))
            if property_name == "Metadata":
                return GLib.Variant("a{sv}", self._build_metadata_dict(st))
            if property_name == "Volume":
                return GLib.Variant("d", max(0.0, min(1.0, st.volume / 100.0)))
            if property_name == "Position":
                return GLib.Variant("x", max(0, int(st.position * 1_000_000)))
            if property_name in ("MinimumRate", "MaximumRate"):
                return GLib.Variant("d", 1.0)
            if property_name == "CanGoNext":
                return GLib.Variant("b", bool(st.can_next))
            if property_name == "CanGoPrevious":
                return GLib.Variant("b", bool(st.can_previous))
            if property_name == "CanPlay":
                return GLib.Variant("b", bool(st.can_play))
            if property_name == "CanPause":
                return GLib.Variant("b", bool(st.can_pause))
            if property_name == "CanSeek":
                return GLib.Variant("b", bool(st.can_seek))
            if property_name == "CanControl":
                return GLib.Variant("b", True)

        return None

    def _handle_set_property(
        self,
        _conn: Gio.DBusConnection,
        _sender: str,
        _object_path: str,
        interface_name: str,
        property_name: str,
        value: GLib.Variant,
    ) -> bool:
        if interface_name != MPRIS_PLAYER_IFACE:
            return False

        if property_name == "Volume":
            vol_pct = max(0, min(100, int(round(value.get_double() * 100.0))))
            self._state.volume = vol_pct
            self.ma_service.runner.submit(self.ma_service.set_volume(vol_pct))
            self._emit_properties_changed(
                MPRIS_PLAYER_IFACE, {"Volume": GLib.Variant("d", vol_pct / 100.0)}
            )
            return True

        if property_name == "Shuffle":
            enabled = bool(value.get_boolean())
            self._state.shuffle = enabled
            self.ma_service.runner.submit(self.ma_service.set_shuffle(enabled))
            self._emit_properties_changed(
                MPRIS_PLAYER_IFACE, {"Shuffle": GLib.Variant("b", enabled)}
            )
            return True

        if property_name == "LoopStatus":
            mode_str = value.get_string()
            mode = self._parse_loop_status(mode_str)
            self._state.repeat = mode
            self.ma_service.runner.submit(self.ma_service.set_repeat(mode))
            self._emit_properties_changed(
                MPRIS_PLAYER_IFACE,
                {"LoopStatus": GLib.Variant("s", self._loop_status_str(mode))},
            )
            return True

        if property_name == "Rate":
            return True

        return False

    def _handle_method_call(
        self,
        _conn: Gio.DBusConnection,
        _sender: str,
        _object_path: str,
        interface_name: str,
        method_name: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        runner = self.ma_service.runner

        if interface_name == MPRIS_ROOT_IFACE:
            if method_name == "Raise" and self._on_raise:
                self._on_raise()
            elif method_name == "Quit" and self._on_quit:
                self._on_quit()
            invocation.return_value(None)
            return

        if interface_name == MPRIS_PLAYER_IFACE:
            if method_name == "PlayPause":
                runner.submit(self.ma_service.play_pause())
            elif method_name == "Play":
                runner.submit(self.ma_service.play())
            elif method_name == "Pause":
                runner.submit(self.ma_service.pause())
            elif method_name == "Stop":
                runner.submit(self.ma_service.stop_playback())
            elif method_name == "Next":
                runner.submit(self.ma_service.next_track())
            elif method_name == "Previous":
                runner.submit(self.ma_service.previous_track())
            elif method_name == "Seek":
                (offset_us,) = parameters.unpack()
                st = self.ma_service.get_now_playing() if self.ma_service.client else self._state
                target_s = max(0.0, st.position + (offset_us / 1_000_000.0))
                if st.duration > 0:
                    target_s = min(st.duration, target_s)
                self._state.position = target_s
                runner.submit(self.ma_service.seek_seconds(target_s))
                self.emit_seeked(int(target_s * 1_000_000))
            elif method_name == "SetPosition":
                _track_id, pos_us = parameters.unpack()
                target_s = max(0.0, pos_us / 1_000_000.0)
                st = self.ma_service.get_now_playing() if self.ma_service.client else self._state
                if st.duration > 0:
                    target_s = min(st.duration, target_s)
                self._state.position = target_s
                runner.submit(self.ma_service.seek_seconds(target_s))
                self.emit_seeked(int(target_s * 1_000_000))
            elif method_name == "OpenUri":
                (uri,) = parameters.unpack()
                if uri:
                    runner.submit(self.ma_service.play_media(uri))

            invocation.return_value(None)
            return

        invocation.return_dbus_error(
            "org.freedesktop.DBus.Error.UnknownMethod",
            f"Unknown method {interface_name}.{method_name}",
        )
