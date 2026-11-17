"""Bottom player bar + device selector."""

from __future__ import annotations

from collections.abc import Callable
import math
import time
from typing import Any

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango
from music_assistant_models.enums import MediaType, PlaybackState, RepeatMode
from music_assistant_models.player import Player

from ..artwork import ArtworkLoader
from ..icons import make_icon_image, set_button_svg
from ..ma_service import MAService, NowPlayingState
from .widgets import (
    forward_right_click,
    format_duration,
    make_cover_frame,
    open_track_credits_window,
)

_TIER_NAMES = {
    "HR": "Hi-Res lossless",
    "HQ": "Lossless",
    "SQ": "High quality",
    "LQ": "Low quality",
}


def _fmt_audio_spec(codec: str, sr: int, bd: int, ch: int, br: int = 0) -> str:
    parts: list[str] = []
    if codec:
        parts.append(codec)
    if sr:
        parts.append(f"{sr / 1000:g} kHz")
    if bd:
        parts.append(f"{bd} bit")
    if ch:
        parts.append("Stereo" if ch == 2 else ("Mono" if ch == 1 else f"{ch} ch"))
    if br:
        parts.append(f"{br} kbps")
    return " • ".join(parts)


def _quality_stage_row(
    icon_name: str,
    title: str,
    badge: str,
    badge_class: str,
    lines: list[str],
    *,
    highlight: bool = False,
) -> Gtk.Box:
    """One stage of the signal path, laid out like a row of the devices menu."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    row.add_css_class("device-row")
    if highlight:
        row.add_css_class("local-device")
        row.add_css_class("active-device")
    icon = make_icon_image(icon_name, size=20, color="#94A3B8" if highlight else "#FFFFFF")
    icon.set_valign(Gtk.Align.START)
    row.append(icon)

    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    vbox.set_hexpand(True)
    title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    t_lbl = Gtk.Label(label=title, xalign=0.0)
    t_lbl.add_css_class("device-active-label" if highlight else "track-title")
    t_lbl.set_ellipsize(Pango.EllipsizeMode.END)
    title_row.append(t_lbl)
    if badge:
        b_lbl = Gtk.Label(label=badge)
        for css_class in badge_class.split():
            b_lbl.add_css_class(css_class)
        b_lbl.set_valign(Gtk.Align.CENTER)
        title_row.append(b_lbl)
    vbox.append(title_row)
    for line in lines:
        if not line:
            continue
        l_lbl = Gtk.Label(label=line, xalign=0.0)
        l_lbl.add_css_class("muted-label")
        l_lbl.set_wrap(True)
        l_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        l_lbl.set_max_width_chars(34)
        vbox.append(l_lbl)
    row.append(vbox)
    return row


def open_quality_details_popover(parent: Gtk.Widget, st: NowPlayingState) -> Gtk.Popover | None:
    """Audio quality menu, styled like the devices menu: input -> processing -> output."""
    q = st.quality
    if q is None:
        return None

    pop = Gtk.Popover()
    pop.set_has_arrow(False)
    pop.set_parent(parent)
    pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_size_request(320, -1)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    box.set_margin_start(6)
    box.set_margin_end(6)

    tier = (q.tier or "SQ").upper()
    hdr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    hdr = Gtk.Label(label="Audio quality", xalign=0.0)
    hdr.add_css_class("track-title")
    hdr.set_hexpand(True)
    hdr_row.append(hdr)
    tier_pill = Gtk.Label(label=f"{tier} · {_TIER_NAMES.get(tier, '')}".rstrip(" ·"))
    tier_pill.add_css_class("quality-tier-pill")
    tier_pill.add_css_class(f"quality-tier-{tier.lower()}")
    tier_pill.set_valign(Gtk.Align.CENTER)
    hdr_row.append(tier_pill)
    box.append(hdr_row)

    track = " • ".join(p for p in (st.title, st.artist) if p)
    if track:
        t_lbl = Gtk.Label(label=track, xalign=0.0)
        t_lbl.add_css_class("subtitle-dim")
        t_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        t_lbl.set_max_width_chars(40)
        box.append(t_lbl)

    sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    sep.set_margin_top(2)
    box.append(sep)
    path_lbl = Gtk.Label(label="Signal path", xalign=0.0)
    path_lbl.add_css_class("muted-label")
    path_lbl.set_margin_start(4)
    box.append(path_lbl)

    stages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

    in_spec = _fmt_audio_spec(
        q.source_codec,
        q.source_sample_rate,
        q.source_bit_depth,
        q.source_channels,
        q.source_bit_rate,
    )
    stream = q.stream_type.replace("_", " ").capitalize() if q.stream_type else ""
    stages.append(
        _quality_stage_row(
            "library",
            q.source_provider or "Music Assistant",
            tier,
            f"quality-tier-pill quality-tier-{tier.lower()}",
            [in_spec or q.badge_text, f"Stream: {stream}" if stream else ""],
        )
    )

    proc_sr = q.source_sample_rate or q.output_sample_rate
    internal = f"PCM 32-bit float • {proc_sr / 1000:g} kHz" if proc_sr else "PCM 32-bit float"
    norm = q.normalization_mode.replace("_", " ").capitalize() if q.normalization_mode else "Off"
    if q.loudness is not None:
        norm = f"{norm} ({q.loudness:.1f} LUFS)"
    elif q.target_loudness is not None:
        norm = f"{norm} (target {q.target_loudness:.1f} LUFS)"
    stages.append(
        _quality_stage_row(
            "sort",
            "Processing",
            "",
            "",
            [internal, f"Volume normalization: {norm}", f"DSP: {q.audio_processing or 'None'}"],
        )
    )

    out_tier = (q.output_tier or tier).upper()
    out_spec = _fmt_audio_spec(
        q.output_codec or q.source_codec,
        q.output_sample_rate or q.source_sample_rate,
        q.output_bit_depth or q.source_bit_depth,
        q.output_channels or q.source_channels,
    )
    out_row = _quality_stage_row(
        "computer" if st.is_local_player else "speaker",
        st.player_name or "Output",
        "THIS DEVICE" if st.is_local_player else out_tier,
        "local-device-badge"
        if st.is_local_player
        else f"quality-tier-pill quality-tier-{out_tier.lower()}",
        [" • ".join(p for p in (q.protocol or "Sendspin", out_spec) if p)],
        highlight=True,
    )
    stages.append(out_row)
    box.append(stages)

    pop.set_child(box)
    pop.popup()
    return pop


class PlayerBar(Gtk.Box):
    """Fixed 3-column bottom bar (Track info | Transport + Progress | Queue + Devices + Volume)."""

    def __init__(
        self,
        ma_service: MAService,
        artwork: ArtworkLoader,
        on_toggle_queue: Callable[[], bool],
        on_open_item: Callable[[Any], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.ma_service = ma_service
        self.artwork = artwork
        self._on_toggle_queue = on_toggle_queue
        self._on_open_item = on_open_item

        self._seeking = False
        self._updating_sliders = False
        self._current_state = NowPlayingState()
        self._transfer_on_switch = True
        self._play_btn_state: tuple[bool, bool] | None = None
        self._spin_angle = 0.0
        self._spin_tick_id = 0
        self._spin_last_us = 0
        self._spin_area: Gtk.DrawingArea | None = None

        # Main 3-column row using a CenterBox to keep the transport centered
        bar = Gtk.CenterBox()
        bar.add_css_class("player-bar")

        # --- 1. Left column: Cover art + Title/Artist ---
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        left.set_valign(Gtk.Align.CENTER)
        left.set_size_request(150, -1)

        cover_frame, self._cover_pic = make_cover_frame(68)
        # Same destination as clicking the title: the album / playlist that is playing
        cover_frame.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        cover_frame.set_tooltip_text("Open album / playlist")
        cover_click = Gtk.GestureClick()
        cover_click.set_button(1)
        cover_click.connect("released", lambda *_a: self._open_current_context())
        cover_frame.add_controller(cover_click)
        left.append(cover_frame)
        self.artwork.load_into_picture(self._cover_pic, "", title="CERO", size=68, kind="cero")

        meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        meta_box.set_valign(Gtk.Align.CENTER)
        meta_box.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        meta_box.set_tooltip_text("Click to open album / playlist • Right-click for track credits")

        meta_lclick = Gtk.GestureClick()
        meta_lclick.set_button(1)
        meta_lclick.connect("released", lambda *_a: self._on_meta_clicked())
        meta_box.add_controller(meta_lclick)

        meta_rclick = Gtk.GestureClick()
        meta_rclick.set_button(3)
        meta_rclick.connect("pressed", lambda *_a: self._open_track_credits())
        meta_box.add_controller(meta_rclick)

        self._title_lbl = Gtk.Label(label="Ready to play", xalign=0.0)
        self._title_lbl.add_css_class("track-title")
        self._title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._title_lbl.set_width_chars(1)
        self._title_lbl.set_max_width_chars(34)
        meta_box.append(self._title_lbl)

        # A single "Artist • Album" line with links: it is ellipsized as one block and does not overlap
        self._artist_lbl = Gtk.Label(label="Music Assistant", xalign=0.0)
        self._artist_lbl.add_css_class("subtitle-dim")
        self._artist_lbl.add_css_class("player-sub-links")
        self._artist_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._artist_lbl.set_width_chars(1)
        self._artist_lbl.set_max_width_chars(38)
        self._artist_lbl.connect("activate-link", self._on_sub_link)
        forward_right_click(self._artist_lbl)
        self._link_activated_at = 0.0
        meta_box.append(self._artist_lbl)

        left.append(meta_box)

        bar.set_start_widget(left)

        # --- 2. Center column: Controls + adaptive Progress bar ---
        center_clamp = Adw.Clamp()
        center_clamp.set_maximum_size(860)
        center_clamp.set_tightening_threshold(560)
        center_clamp.set_hexpand(True)
        center_clamp.set_margin_start(14)
        center_clamp.set_margin_end(14)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        center.set_valign(Gtk.Align.CENTER)
        center.set_hexpand(True)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        controls.set_halign(Gtk.Align.CENTER)

        self._shuffle_btn = Gtk.Button()
        self._shuffle_btn.add_css_class("icon-flat-btn")
        self._shuffle_btn.set_valign(Gtk.Align.CENTER)
        set_button_svg(self._shuffle_btn, "shuffle", size=19, color="#B3B3B3")
        self._shuffle_btn.set_tooltip_text("Shuffle")
        self._shuffle_btn.connect(
            "clicked", lambda _b: self.ma_service.runner.submit(self.ma_service.toggle_shuffle())
        )
        controls.append(self._shuffle_btn)

        self._prev_btn = Gtk.Button()
        self._prev_btn.add_css_class("icon-flat-btn")
        self._prev_btn.set_valign(Gtk.Align.CENTER)
        set_button_svg(self._prev_btn, "prev", size=22, color="#FFFFFF")
        self._prev_btn.set_tooltip_text("Previous")
        self._prev_btn.connect(
            "clicked", lambda _b: self.ma_service.runner.submit(self.ma_service.previous_track())
        )
        controls.append(self._prev_btn)

        self._play_btn = Gtk.Button()
        self._play_btn.add_css_class("play-circle-btn")
        self._play_btn.set_valign(Gtk.Align.CENTER)
        set_button_svg(self._play_btn, "play", size=21, color="#000000")
        self._play_btn.set_tooltip_text("Play / Pause (Space)")
        self._play_btn.connect(
            "clicked", lambda _b: self.ma_service.runner.submit(self.ma_service.play_pause())
        )
        controls.append(self._play_btn)

        self._next_btn = Gtk.Button()
        self._next_btn.add_css_class("icon-flat-btn")
        self._next_btn.set_valign(Gtk.Align.CENTER)
        set_button_svg(self._next_btn, "next", size=22, color="#FFFFFF")
        self._next_btn.set_tooltip_text("Next")
        self._next_btn.connect(
            "clicked", lambda _b: self.ma_service.runner.submit(self.ma_service.next_track())
        )
        controls.append(self._next_btn)

        self._repeat_btn = Gtk.Button()
        self._repeat_btn.add_css_class("icon-flat-btn")
        self._repeat_btn.set_valign(Gtk.Align.CENTER)
        set_button_svg(self._repeat_btn, "repeat", size=19, color="#B3B3B3")
        self._repeat_btn.set_tooltip_text("Repeat")
        self._repeat_btn.connect(
            "clicked", lambda _b: self.ma_service.runner.submit(self.ma_service.cycle_repeat())
        )
        controls.append(self._repeat_btn)

        center.append(controls)

        # Progress bar with times on both sides (stretches across available center space)
        prog_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        prog_row.set_valign(Gtk.Align.CENTER)
        prog_row.set_hexpand(True)

        self._pos_lbl = Gtk.Label(label="0:00", xalign=1.0)
        self._pos_lbl.add_css_class("muted-label")
        self._pos_lbl.set_size_request(36, -1)
        prog_row.append(self._pos_lbl)

        self._seek_adj = Gtk.Adjustment(value=0, lower=0, upper=100, step_increment=1, page_increment=10)
        self._seek_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self._seek_adj)
        self._seek_scale.add_css_class("cero-scale")
        self._seek_scale.set_draw_value(False)
        self._seek_scale.set_hexpand(True)
        self._seek_scale.set_size_request(110, -1)
        self._seek_scale.set_sensitive(False)

        seek_drag = Gtk.GestureClick()
        seek_drag.connect("pressed", lambda *_a: self._set_seeking(True))
        seek_drag.connect("released", self._on_seek_released)
        self._seek_scale.add_controller(seek_drag)
        self._seek_scale.connect("value-changed", self._on_seek_preview)
        prog_row.append(self._seek_scale)

        self._dur_lbl = Gtk.Label(label="0:00", xalign=0.0)
        self._dur_lbl.add_css_class("muted-label")
        self._dur_lbl.set_size_request(36, -1)
        prog_row.append(self._dur_lbl)

        center.append(prog_row)
        center_clamp.set_child(center)
        bar.set_center_widget(center_clamp)

        # --- 3. Right column: Queue + Devices + Volume ---
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        right.set_valign(Gtk.Align.CENTER)
        right.set_halign(Gtk.Align.END)

        self._queue_btn = Gtk.Button()
        self._queue_btn.add_css_class("icon-flat-btn")
        self._queue_btn.set_valign(Gtk.Align.CENTER)
        set_button_svg(self._queue_btn, "queue", size=22, color="#B3B3B3")
        self._queue_btn.set_tooltip_text("Play queue")
        self._queue_btn.connect("clicked", self._handle_queue_toggle)
        right.append(self._queue_btn)

        # Devices button and popover (with discreet, width-capped selected-player label)
        self._devices_btn = Gtk.MenuButton()
        self._devices_btn.add_css_class("device-selector-btn")
        self._devices_btn.set_valign(Gtk.Align.CENTER)
        dev_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        dev_box.set_valign(Gtk.Align.CENTER)
        self._devices_icon_holder = Gtk.Box()
        self._devices_icon_holder.set_valign(Gtk.Align.CENTER)
        self._devices_icon_holder.append(make_icon_image("devices", size=19, color="#B3B3B3"))
        dev_box.append(self._devices_icon_holder)
        self._devices_name_lbl = Gtk.Label(label="", xalign=0.0)
        self._devices_name_lbl.add_css_class("device-selector-label")
        self._devices_name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._devices_name_lbl.set_width_chars(1)
        self._devices_name_lbl.set_max_width_chars(13)
        self._devices_name_lbl.set_visible(False)
        dev_box.append(self._devices_name_lbl)
        self._devices_btn.set_child(dev_box)
        self._devices_btn.set_tooltip_text("Connect to a device")
        self._devices_popover = self._build_devices_popover()
        self._devices_btn.set_popover(self._devices_popover)
        right.append(self._devices_btn)

        # Mute + Volume
        self._mute_btn = Gtk.Button()
        self._mute_btn.add_css_class("icon-flat-btn")
        self._mute_btn.set_valign(Gtk.Align.CENTER)
        set_button_svg(self._mute_btn, "volume_high", size=22, color="#B3B3B3")
        self._mute_btn.set_tooltip_text("Mute / Unmute")
        self._mute_btn.connect("clicked", self._on_mute_clicked)
        right.append(self._mute_btn)

        self._vol_adj = Gtk.Adjustment(value=85, lower=0, upper=100, step_increment=2, page_increment=10)
        self._vol_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self._vol_adj)
        self._vol_scale.add_css_class("cero-scale")
        self._vol_scale.set_valign(Gtk.Align.CENTER)
        self._vol_scale.set_draw_value(False)
        self._vol_scale.set_size_request(80, -1)
        self._vol_scale.connect("value-changed", self._on_volume_changed)
        right.append(self._vol_scale)

        bar.set_end_widget(right)
        self.append(bar)

        self.ma_service.on_state_changed(self._apply_state)
        self.ma_service.on_players_changed(self._populate_devices)

        GLib.timeout_add(500, self._tick_progress)

    def _build_devices_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_has_arrow(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_size_request(290, -1)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        hdr = Gtk.Label(label="Connect to a device", xalign=0.0)
        hdr.add_css_class("track-title")
        box.append(hdr)

        transfer_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        transfer_lbl = Gtk.Label(label="Transfer queue on switch", xalign=0.0)
        transfer_lbl.add_css_class("subtitle-dim")
        transfer_lbl.set_hexpand(True)
        transfer_sw = Gtk.Switch()
        transfer_sw.set_active(True)
        transfer_sw.connect(
            "notify::active", lambda sw, _p: setattr(self, "_transfer_on_switch", sw.get_active())
        )
        transfer_row.append(transfer_lbl)
        transfer_row.append(transfer_sw)
        box.append(transfer_row)

        self._devices_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self._devices_list)

        pop.connect(
            "show",
            lambda _p: self._populate_devices(
                self.ma_service.get_available_players(),
                self.ma_service.active_player_id,
            ),
        )
        pop.set_child(box)
        return pop

    def _populate_devices(self, players: list[Player], active_id: str) -> None:
        while (child := self._devices_list.get_first_child()) is not None:
            self._devices_list.remove(child)

        if not players:
            lbl = Gtk.Label(label="Waiting for Music Assistant players…", xalign=0.0)
            lbl.add_css_class("subtitle-dim")
            self._devices_list.append(lbl)
            return

        has_local = any(self.ma_service.is_local_player(p) for p in players)
        added_remote_header = False

        for p in players:
            is_active = p.player_id == active_id
            is_local = self.ma_service.is_local_player(p)

            if has_local and not is_local and not added_remote_header:
                added_remote_header = True
                sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                sep.set_margin_top(4)
                sep.set_margin_bottom(2)
                self._devices_list.append(sep)
                rem_lbl = Gtk.Label(label="Other devices", xalign=0.0)
                rem_lbl.add_css_class("muted-label")
                rem_lbl.set_margin_start(4)
                self._devices_list.append(rem_lbl)

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.add_css_class("device-row")
            if is_local and is_active:  # the local device is only highlighted while it is the selected one
                row.add_css_class("local-device")
            if is_active:
                row.add_css_class("active-device")
            row.set_cursor(Gdk.Cursor.new_from_name("pointer"))

            icon_name = "computer" if is_local else "speaker"
            icon_color = "#94A3B8" if is_active else "#FFFFFF"
            row.append(make_icon_image(icon_name, size=20, color=icon_color))

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            vbox.set_hexpand(True)

            title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            n_lbl = Gtk.Label(label=p.name, xalign=0.0)
            n_lbl.add_css_class("device-active-label" if is_active else "track-title")
            n_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            title_row.append(n_lbl)
            if is_local:
                badge = Gtk.Label(label="THIS DEVICE")
                badge.add_css_class("local-device-badge")
                title_row.append(badge)
            vbox.append(title_row)

            queue_info = self.ma_service.get_player_queue_summary(p)
            if queue_info:
                sub = queue_info
            elif is_active:
                sub = "Selected"
            elif is_local:
                sub = "Local audio on this PC"
            else:
                sub = "Music Assistant speaker"
            s_lbl = Gtk.Label(label=sub, xalign=0.0)
            s_lbl.add_css_class("muted-label")
            s_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            s_lbl.set_max_width_chars(30)
            if queue_info:
                s_lbl.set_tooltip_text(queue_info)
            vbox.append(s_lbl)
            row.append(vbox)

            click = Gtk.GestureClick()
            click.set_button(1)

            def _pick(_g: Gtk.GestureClick, _n: int, _x: float, _y: float, pid: str = p.player_id) -> None:
                self._devices_popover.popdown()
                self.ma_service.runner.submit(
                    self.ma_service.select_player(pid, transfer_playback=self._transfer_on_switch)
                )

            click.connect("released", _pick)
            row.add_controller(click)
            self._devices_list.append(row)

    def set_queue_active_visual(self, open_state: bool) -> None:
        set_button_svg(
            self._queue_btn, "queue", size=22, color="#94A3B8" if open_state else "#B3B3B3"
        )

    def _handle_queue_toggle(self, _btn: Gtk.Button) -> None:
        is_open = self._on_toggle_queue()
        self.set_queue_active_visual(is_open)

    def _open_track_credits(self) -> Gtk.Window | None:
        st = self._current_state
        return open_track_credits_window(
            self,
            self.ma_service,
            st.media_item,
            title=st.title,
            artist=st.artist,
            album=st.album,
            duration=st.duration,
            uri=st.uri,
            source_provider=st.quality.source_provider if st.quality else "",
        )

    def _open_current_context(self) -> None:
        if self._on_open_item is None:
            return
        st = self._current_state
        target = st.source_context
        if getattr(target, "media_type", None) not in (
            MediaType.ALBUM,
            MediaType.PLAYLIST,
            MediaType.ARTIST,
        ):
            target = getattr(st.media_item, "album", None) if st.media_item is not None else None
        if getattr(target, "media_type", None) not in (
            MediaType.ALBUM,
            MediaType.PLAYLIST,
            MediaType.ARTIST,
        ):
            artists = getattr(st.media_item, "artists", None) or [] if st.media_item is not None else []
            target = artists[0] if artists else None
        if getattr(target, "media_type", None) in (
            MediaType.ALBUM,
            MediaType.PLAYLIST,
            MediaType.ARTIST,
        ):
            self._on_open_item(target)

    def _on_meta_clicked(self) -> None:
        # If the click was on a link (artist / album), _on_sub_link already handled it
        if time.monotonic() - self._link_activated_at < 0.5:
            return
        self._open_current_context()

    def _on_sub_link(self, _label: Gtk.Label, uri: str) -> bool:
        self._link_activated_at = time.monotonic()
        if self._on_open_item is None:
            return True
        st = self._current_state
        item = st.media_item
        if uri == "cero:artist":
            artists = (getattr(item, "artists", None) or []) if item is not None else []
            if artists:
                self._on_open_item(artists[0])
        elif uri == "cero:album":
            album_obj = getattr(item, "album", None) if item is not None else None
            if getattr(album_obj, "media_type", None) == MediaType.ALBUM:
                self._on_open_item((album_obj, item))
            else:
                self._open_current_context()
        return True

    def _set_sub_line(self, st: NowPlayingState) -> None:
        """Renders "Artist • Album" with links only where there is a navigable target."""
        item = st.media_item
        has_artist_link = bool(item is not None and (getattr(item, "artists", None) or []))
        has_album_link = bool(item is not None and getattr(item, "album", None) is not None)
        parts: list[str] = []
        tip: list[str] = []
        artist = st.artist or st.player_name
        if artist:
            esc = GLib.markup_escape_text(artist)
            parts.append(f'<a href="cero:artist">{esc}</a>' if has_artist_link else esc)
            tip.append(artist)
        if st.album:
            esc = GLib.markup_escape_text(st.album)
            parts.append(f'<a href="cero:album">{esc}</a>' if has_album_link else esc)
            tip.append(st.album)
        self._artist_lbl.set_markup(" • ".join(parts))
        self._artist_lbl.set_tooltip_text(" • ".join(tip) if tip else None)

    def _set_seeking(self, val: bool) -> None:
        self._seeking = val

    def _on_seek_preview(self, scale: Gtk.Scale) -> None:
        if self._seeking and not self._updating_sliders:
            self._pos_lbl.set_label(format_duration(scale.get_value()) or "0:00")

    def _on_seek_released(self, *_args: object) -> None:
        if not self._seeking:
            return
        self._seeking = False
        target = self._seek_scale.get_value()
        self.ma_service.runner.submit(self.ma_service.seek_seconds(target))

    def _on_mute_clicked(self, _btn: Gtk.Button) -> None:
        self.ma_service.runner.submit(self.ma_service.set_mute(not self._current_state.muted))

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        if self._updating_sliders:
            return
        vol = int(round(scale.get_value()))
        self.ma_service.runner.submit(self.ma_service.set_volume(vol))

    def _apply_state(self, st: NowPlayingState) -> None:
        self._current_state = st

        self._title_lbl.set_label(st.title or "Ready to play")
        self._title_lbl.set_tooltip_text(st.title or None)
        self._set_sub_line(st)
        self.artwork.load_into_picture(
            self._cover_pic, st.image_url, title=st.title or "CERO", size=68, kind="cero"
        )

        # Play / Pause + transport lock while waiting for the server
        is_playing = st.playback_state == PlaybackState.PLAYING
        btn_state = (st.pending, is_playing)
        if btn_state != self._play_btn_state:
            self._play_btn_state = btn_state
            self._update_play_button_visual(st.pending, is_playing)

        controls_ready = not st.pending
        for ctrl_btn in (
            self._shuffle_btn,
            self._prev_btn,
            self._play_btn,
            self._next_btn,
            self._repeat_btn,
        ):
            ctrl_btn.set_sensitive(controls_ready)

        # Shuffle & Repeat
        set_button_svg(
            self._shuffle_btn, "shuffle", size=19, color="#94A3B8" if st.shuffle else "#B3B3B3"
        )
        if st.repeat == RepeatMode.ONE:
            set_button_svg(self._repeat_btn, "repeat_one", size=19, color="#94A3B8")
        elif st.repeat == RepeatMode.ALL:
            set_button_svg(self._repeat_btn, "repeat", size=19, color="#94A3B8")
        else:
            set_button_svg(self._repeat_btn, "repeat", size=19, color="#B3B3B3")

        # Devices and Quality
        remote_active = bool(st.player_id and not st.is_local_player)
        while (child := self._devices_icon_holder.get_first_child()) is not None:
            self._devices_icon_holder.remove(child)
        dev_icon = "computer" if (st.player_id and st.is_local_player) else "devices"
        self._devices_icon_holder.append(
            make_icon_image(dev_icon, size=19, color="#94A3B8" if remote_active else "#B3B3B3")
        )
        has_player = bool(st.player_id and st.player_name)
        self._devices_name_lbl.set_label(st.player_name if has_player else "")
        self._devices_name_lbl.set_visible(has_player)
        if remote_active:
            self._devices_name_lbl.add_css_class("remote")
        else:
            self._devices_name_lbl.remove_css_class("remote")
        self._devices_btn.set_tooltip_text(
            f"Connect to a device ({st.player_name})" if st.player_name else "Connect to a device"
        )

        # Volume and progress
        self._updating_sliders = True
        try:
            self._vol_scale.set_value(st.volume)
            if st.muted or st.volume == 0:
                set_button_svg(self._mute_btn, "volume_mute", size=22, color="#B3B3B3")
            elif st.volume < 50:
                set_button_svg(self._mute_btn, "volume_low", size=22, color="#B3B3B3")
            else:
                set_button_svg(self._mute_btn, "volume_high", size=22, color="#B3B3B3")

            if not self._seeking:
                dur = max(0.0, st.duration)
                self._seek_scale.set_sensitive(not st.pending and st.can_seek and dur > 0)
                self._seek_adj.set_upper(dur if dur > 0 else 100.0)
                self._seek_scale.set_value(min(st.position, dur) if dur > 0 else 0.0)
                self._pos_lbl.set_label(format_duration(st.position) or "0:00")
                self._dur_lbl.set_label(format_duration(dur) or ("LIVE" if st.title and dur == 0 else "0:00"))
        finally:
            self._updating_sliders = False

    def _update_play_button_visual(self, pending: bool, is_playing: bool) -> None:
        if self._spin_tick_id:
            self._play_btn.remove_tick_callback(self._spin_tick_id)
            self._spin_tick_id = 0
        self._spin_area = None
        if not pending:
            self._play_btn.remove_css_class("is-pending")
            set_button_svg(self._play_btn, "pause" if is_playing else "play", size=21, color="#000000")
            return

        self._play_btn.add_css_class("is-pending")
        area = Gtk.DrawingArea()
        area.set_content_width(22)
        area.set_content_height(22)
        area.set_halign(Gtk.Align.CENTER)
        area.set_valign(Gtk.Align.CENTER)
        area.set_draw_func(self._draw_pending_arc)
        self._spin_area = area
        self._spin_last_us = 0
        self._play_btn.set_child(area)
        self._spin_tick_id = self._play_btn.add_tick_callback(self._on_spin_tick)

    def _on_spin_tick(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        if self._spin_area is None:
            self._spin_tick_id = 0
            return GLib.SOURCE_REMOVE
        now_us = clock.get_frame_time()
        if self._spin_last_us:
            dt = max(0.0, min(0.1, (now_us - self._spin_last_us) / 1_000_000.0))
            self._spin_angle = (self._spin_angle + dt * math.tau * 1.15) % math.tau
        self._spin_last_us = now_us
        self._spin_area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _draw_pending_arc(
        self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int
    ) -> None:
        cx = width / 2.0
        cy = height / 2.0
        radius = min(width, height) / 2.0 - 2.4
        cr.set_line_width(2.4)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        # Subtle full ring track
        cr.set_source_rgba(0.05, 0.07, 0.09, 0.20)
        cr.arc(cx, cy, radius, 0.0, math.tau)
        cr.stroke()
        # Smooth rotating dark arc
        cr.set_source_rgba(0.05, 0.07, 0.09, 0.92)
        cr.arc(cx, cy, radius, self._spin_angle, self._spin_angle + math.pi * 1.35)
        cr.stroke()

    def _tick_progress(self) -> bool:
        if (
            not self._seeking
            and self.ma_service.client
            and self._current_state.playback_state == PlaybackState.PLAYING
        ):
            snap = self.ma_service.get_now_playing()
            self._current_state = snap
            if snap.duration > 0:
                self._updating_sliders = True
                try:
                    self._seek_adj.set_upper(snap.duration)
                    self._seek_scale.set_value(min(snap.position, snap.duration))
                    self._pos_lbl.set_label(format_duration(snap.position) or "0:00")
                finally:
                    self._updating_sliders = False
        return GLib.SOURCE_CONTINUE
