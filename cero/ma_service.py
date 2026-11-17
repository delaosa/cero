"""Connection and playback service for Music Assistant Server (no configuration views)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from music_assistant_client import MusicAssistantClient, get_server_info, login_with_token
from music_assistant_models.enums import (
    EventType,
    ImageType,
    MediaType,
    PlaybackState,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    LoginFailed,
    MusicAssistantError,
)
from music_assistant_models.event import MassEvent
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemType,
    Playlist,
    Radio,
    RecommendationFolder,
    SearchResults,
    Track,
    media_from_dict,
)
from music_assistant_models.player import Player
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from .async_runner import AsyncRunner
from .session import SessionStore, get_library_cache_path

logger = logging.getLogger(__name__)


def normalize_server_url(raw: str) -> str:
    """Normalizes a MA server URL typed by the user (e.g. 192.168.1.50 -> http://192.168.1.50:8095)."""
    url = raw.strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.netloc
    if ":" not in netloc.rsplit("]", 1)[-1] and parsed.scheme == "http":
        netloc = f"{netloc}:8095"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def extract_host_from_url(server_url: str) -> str:
    parsed = urllib.parse.urlsplit(normalize_server_url(server_url))
    return parsed.hostname or "localhost"


def format_artists(item: Any) -> str:
    """Extracts a clean artist string for any MA media item."""
    if item is None:
        return ""
    if artist_str := getattr(item, "artist_str", None):
        return str(artist_str)
    artists = getattr(item, "artists", None)
    if artists:
        names = [getattr(a, "name", "") for a in artists if getattr(a, "name", "")]
        if names:
            return ", ".join(names)
    if owner := getattr(item, "owner", None):
        return str(owner)
    if artist := getattr(item, "artist", None):
        return str(artist)
    return ""


@dataclass
class DiscoveredServer:
    name: str
    url: str
    version: str = ""


@dataclass
class AudioQualityInfo:
    """Audio quality summary and Audio Pipeline, Music Assistant style."""

    tier: str = "SQ"  # HR, HQ, SQ, LQ
    badge_text: str = ""
    is_hires: bool = False
    is_lossless: bool = False
    source_provider: str = ""
    source_codec: str = ""
    source_sample_rate: int = 0
    source_bit_depth: int = 0
    source_channels: int = 0
    source_bit_rate: int = 0
    output_tier: str = "HQ"
    output_codec: str = ""
    output_sample_rate: int = 0
    output_bit_depth: int = 0
    output_channels: int = 0
    stream_type: str = ""
    protocol: str = ""
    loudness: float | None = None
    loudness_album: float | None = None
    normalization_mode: str = ""
    target_loudness: float | None = None
    audio_processing: str = ""


@dataclass
class NowPlayingState:
    """Unified snapshot of the current playback state for the UI and MPRIS2."""

    connected: bool = False
    player_id: str = ""
    player_name: str = "No player"
    is_local_player: bool = False
    playback_state: PlaybackState = PlaybackState.IDLE
    title: str = ""
    artist: str = ""
    album: str = ""
    uri: str = ""
    queue_item_id: str = ""
    image_url: str = ""
    duration: float = 0.0
    position: float = 0.0
    volume: int = 85
    muted: bool = False
    shuffle: bool = False
    repeat: RepeatMode = RepeatMode.OFF
    favorite: bool = False
    can_play: bool = True
    can_pause: bool = True
    can_next: bool = True
    can_previous: bool = True
    can_seek: bool = False
    media_item: Any = None
    source_context: Any = None
    quality: AudioQualityInfo | None = None
    pending: bool = False  # a play / pause command is waiting for the server


@dataclass
class HomeContent:
    recently_played: list[Any] = field(default_factory=list)
    recommendations: list[RecommendationFolder] = field(default_factory=list)
    playlists: list[Playlist] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    artists: list[Artist] = field(default_factory=list)
    radios: list[Radio] = field(default_factory=list)


@dataclass
class ArtistDetail:
    artist: Artist
    top_tracks: list[Track] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)


class MAService:
    """Orchestrates the Music Assistant connection, player selection and state events."""

    def __init__(self, runner: AsyncRunner, session_store: SessionStore) -> None:
        self.runner = runner
        self.session_store = session_store
        self.client: MusicAssistantClient | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._local_sendspin_peer_id: str = ""
        self._local_output_format_cb: Callable[[], tuple[str, int, int, int] | None] | None = None

        self._state_listeners: list[Callable[[NowPlayingState], None]] = []
        self._players_listeners: list[Callable[[list[Player], str], None]] = []
        self._queue_listeners: list[Callable[[list[QueueItem], str], None]] = []
        self._connection_listeners: list[Callable[[bool, str], None]] = []
        self._library_listeners: list[Callable[[], None]] = []
        self._favorite_listeners: list[Callable[[], None]] = []
        self._favorite_keys: set[tuple[str, str]] = set()
        # (target state, deadline, previous item key, wait for audio progress) while waiting for MA
        self._pending_playback: tuple[PlaybackState | None, float, str, bool] | None = None
        # Immediate preview of the track/item being started until the server reports it
        self._optimistic_track: dict[str, Any] | None = None
        self._favorite_keys_at: float = 0.0
        self._force_next_playlist_refresh: bool = False
        self._refresh_listeners: list[Callable[[Any], None]] = []
        self._media_updated_listeners: list[Callable[[str], None]] = []

        self._cached_queue_items: list[QueueItem] = []
        self._last_fetched_queue_key: tuple[str, int | None, str | None] = ("", None, None)
        self._playback_context_by_queue: dict[str, Any] = {}
        self._startup_suppress_until: float = 0.0
        self._manual_player_selection: bool = False

        self._lib_id_by_uri: dict[str, tuple[MediaType, str]] = {}
        self._lib_id_by_mapping: dict[tuple[str, str, str], tuple[MediaType, str]] = {}
        self._removed_lib_keys: set[tuple[str, str]] = set()
        # URIs of items whose "add to library" request is still running on the server
        self._pending_lib_adds: set[str] = set()

    def set_local_sendspin_peer_id(self, peer_id: str) -> None:
        self._local_sendspin_peer_id = peer_id

    def set_local_output_format_provider(
        self, cb: Callable[[], tuple[str, int, int, int] | None]
    ) -> None:
        self._local_output_format_cb = cb

    # --- Subscriptions from UI / MPRIS ---

    def on_state_changed(self, cb: Callable[[NowPlayingState], None]) -> None:
        self._state_listeners.append(cb)

    def on_players_changed(self, cb: Callable[[list[Player], str], None]) -> None:
        self._players_listeners.append(cb)

    def on_queue_changed(self, cb: Callable[[list[QueueItem], str], None]) -> None:
        self._queue_listeners.append(cb)

    def on_connection_changed(self, cb: Callable[[bool, str], None]) -> None:
        self._connection_listeners.append(cb)

    def on_library_changed(self, cb: Callable[[], None]) -> None:
        self._library_listeners.append(cb)

    def off_library_changed(self, cb: Callable[[], None]) -> None:
        try:
            self._library_listeners.remove(cb)
        except ValueError:
            pass

    def on_item_refreshed(self, cb: Callable[[Any], None]) -> None:
        self._refresh_listeners.append(cb)

    def on_media_item_updated(self, cb: Callable[[str], None]) -> None:
        """Notifies (with the URI) when MA reports changes to an item, e.g. from another client."""
        self._media_updated_listeners.append(cb)

    def _emit_item_refreshed(self, item: Any) -> None:
        for cb in list(self._refresh_listeners):
            self.runner.call_ui(cb, item)

    def on_favorite_changed(self, cb: Callable[[], None]) -> None:
        self._favorite_listeners.append(cb)

    def off_favorite_changed(self, cb: Callable[[], None]) -> None:
        try:
            self._favorite_listeners.remove(cb)
        except ValueError:
            pass

    def _emit_favorite_changed(self) -> None:
        for cb in list(self._favorite_listeners):
            self.runner.call_ui(cb)

    def _emit_library_changed(self) -> None:
        for cb in list(self._library_listeners):
            self.runner.call_ui(cb)

    def _emit_state(self) -> None:
        snap = self.get_now_playing()
        for cb in list(self._state_listeners):
            self.runner.call_ui(cb, snap)

    def _emit_players(self) -> None:
        players = self.get_available_players()
        active_id = self.active_player_id
        for cb in list(self._players_listeners):
            self.runner.call_ui(cb, players, active_id)

    def _emit_connection(self, connected: bool, message: str = "") -> None:
        for cb in list(self._connection_listeners):
            self.runner.call_ui(cb, connected, message)

    # --- mDNS discovery of MA servers on the network ---

    async def discover_servers(self, timeout: float = 2.0) -> list[DiscoveredServer]:
        def _scan() -> list[DiscoveredServer]:
            found: dict[str, DiscoveredServer] = {}

            class _Listener(ServiceListener):
                def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                    info = zc.get_service_info(type_, name)
                    if not info:
                        return
                    addrs = info.parsed_scoped_addresses()
                    if not addrs:
                        return
                    host = addrs[0]
                    port = info.port or 8095
                    props = {
                        (k.decode() if isinstance(k, bytes) else str(k)): (
                            v.decode(errors="ignore") if isinstance(v, bytes) else str(v)
                        )
                        for k, v in info.properties.items()
                    }
                    base_url = props.get("base_url") or f"http://{host}:{port}"
                    friendly = props.get("server_id") or name.split(".")[0]
                    version = props.get("server_version", "")
                    found[base_url] = DiscoveredServer(
                        name=f"Music Assistant ({host})",
                        url=normalize_server_url(base_url),
                        version=version,
                    )

                def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                    self.add_service(zc, type_, name)

                def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                    pass

            zc = Zeroconf()
            try:
                ServiceBrowser(zc, "_mass._tcp.local.", _Listener())
                time.sleep(timeout)
            finally:
                zc.close()
            return list(found.values())

        return await asyncio.to_thread(_scan)

    # --- Authentication and connection lifecycle ---

    def _build_ssl_context(self, server_url: str) -> ssl.SSLContext | None:
        if server_url.startswith("https://"):
            ctx = ssl.create_default_context()
            return ctx
        return None

    async def authenticate_and_connect(
        self,
        server_url: str,
        *,
        username: str = "",
        password: str = "",
        token: str = "",
    ) -> None:
        """Authenticates (with username/password or token) and starts the persistent session."""
        clean_url = normalize_server_url(server_url)
        if not clean_url:
            raise ValueError("Enter the Music Assistant server URL")

        ssl_ctx = self._build_ssl_context(clean_url)
        info = await get_server_info(clean_url, ssl_context=ssl_ctx)

        resolved_token = token.strip()
        if not resolved_token and username.strip():
            user, resolved_token = await login_with_token(
                clean_url,
                username.strip(),
                password,
                token_name="CERO Linux Player",
                ssl_context=ssl_ctx,
            )
            self.session_store.data.username = user.username
        elif info.schema_version >= 28 and not resolved_token:
            raise AuthenticationRequired("This server requires a username/password or a token")

        self.session_store.data.server_url = clean_url
        self.session_store.data.token = resolved_token
        self.session_store.save()

        await self.connect_saved_session()

    async def connect_saved_session(self) -> None:
        """Connects using the data saved in SessionStore."""
        self._stopping = False
        await self._teardown_client()

        url = normalize_server_url(self.session_store.data.server_url)
        token = self.session_store.data.token.strip() or None
        if not url:
            raise ValueError("No server configured")

        ssl_ctx = self._build_ssl_context(url)
        connector = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx else aiohttp.TCPConnector()
        self._http_session = aiohttp.ClientSession(connector=connector)

        client = MusicAssistantClient(url, self._http_session, token=token, ssl_context=ssl_ctx)
        client.subscribe(self._on_mass_event)

        init_ready = asyncio.Event()
        listen_task = asyncio.create_task(client.start_listening(init_ready=init_ready))
        self.client = client
        self._listen_task = listen_task

        done, _ = await asyncio.wait(
            {listen_task, asyncio.create_task(init_ready.wait())},
            timeout=15.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if listen_task in done:
            exc = listen_task.exception()
            await self._teardown_client()
            if exc:
                raise exc
            raise RuntimeError("The connection to Music Assistant closed prematurely")
        if not init_ready.is_set():
            await self._teardown_client()
            raise TimeoutError("Timed out connecting to Music Assistant")

        listen_task.add_done_callback(self._on_listen_task_ended)
        self._startup_suppress_until = time.monotonic() + 8.0
        self._manual_player_selection = False
        self._auto_select_player()
        self._emit_connection(True, url)
        self._emit_players()
        self._emit_state()
        asyncio.create_task(self.refresh_queue_items())
        asyncio.create_task(self._suppress_startup_autoplay())

    def _on_listen_task_ended(self, task: asyncio.Task[None]) -> None:
        if self._stopping:
            return
        exc = None
        if not task.cancelled():
            exc = task.exception()
        if isinstance(exc, (AuthenticationFailed, AuthenticationRequired, LoginFailed)):
            self._emit_connection(False, "Session expired. Please sign in again.")
            return
        self._emit_connection(False, "Reconnecting to Music Assistant…")
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = 2.0
        while not self._stopping and self.session_store.data.has_credentials:
            await asyncio.sleep(delay)
            if self._stopping:
                return
            try:
                await self.connect_saved_session()
                return
            except (AuthenticationFailed, AuthenticationRequired, LoginFailed):
                self._emit_connection(False, "Authentication required")
                return
            except Exception as err:
                logger.debug("Connection retry failed: %s", err)
                delay = min(delay * 1.6, 15.0)

    async def disconnect(self) -> None:
        self._stopping = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        try:
            snap = self.get_now_playing()
            if snap.is_local_player and snap.playback_state == PlaybackState.PLAYING:
                await self.pause()
        except Exception:
            pass
        await self._teardown_client()
        self._emit_connection(False, "Disconnected")

    async def _suppress_startup_autoplay(self) -> None:
        if time.monotonic() >= self._startup_suppress_until or not self.client:
            return
        pid = self.active_player_id
        player = self.client.players.get(pid) if pid else None
        if player and self.is_local_player(player):
            snap = self.get_now_playing()
            if snap.playback_state == PlaybackState.PLAYING:
                self._startup_suppress_until = 0.0
                try:
                    await self.pause()
                except Exception:
                    pass

    async def _teardown_client(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        if self._listen_task is not None and not self._listen_task.done():
            self._listen_task.cancel()
        self._listen_task = None
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None

    # --- Player selection (Local PC vs remote devices) ---

    def is_local_player(self, player: Player) -> bool:
        peer = self._local_sendspin_peer_id
        if peer:
            if player.player_id == peer or peer in player.player_id:
                return True
            for op in getattr(player, "output_protocols", None) or ():
                op_id = getattr(op, "output_protocol_id", "") or ""
                if op_id == peer or peer in op_id:
                    return True
        local_name = self.session_store.data.local_player_name.lower()
        if local_name and player.name.lower() == local_name:
            return True
        return False

    def get_available_players(self) -> list[Player]:
        if not self.client:
            return []
        players = [
            p
            for p in self.client.players.players
            if p.enabled and not p.hide_in_ui and (p.available or self.is_local_player(p))
        ]
        # Sort: "This PC" (local Sendspin) first, then playing, then alphabetical
        players.sort(
            key=lambda p: (
                0 if self.is_local_player(p) else 1,
                0 if p.playback_state == PlaybackState.PLAYING else 1,
                p.name.lower(),
            )
        )
        return players

    @property
    def active_player_id(self) -> str:
        if not self.client:
            return self.session_store.data.selected_player_id
        selected = self.session_store.data.selected_player_id
        if selected and (p := self.client.players.get(selected)) and p.available:
            return selected
        self._ensure_selected_player()
        return self.session_store.data.selected_player_id

    @property
    def active_queue_id(self) -> str:
        pid = self.active_player_id
        if not self.client or not pid:
            return pid
        player = self.client.players.get(pid)
        if player:
            if player.synced_to:
                return player.synced_to
            if player.active_group:
                return player.active_group
            if player.active_source and self.client.player_queues.get(player.active_source):
                return player.active_source
        return pid

    def _queue_for_player(self, player: Player) -> Any:
        if not self.client:
            return None
        qid = (
            player.synced_to
            or player.active_group
            or (
                player.active_source
                if player.active_source and self.client.player_queues.get(player.active_source)
                else None
            )
            or player.player_id
        )
        return self.client.player_queues.get(qid)

    def _player_has_queue_content(self, player: Player) -> bool:
        q = self._queue_for_player(player)
        if q and ((q.items or 0) > 0 or q.current_item is not None):
            return True
        return player.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED)

    def get_player_queue_summary(self, player: Player) -> str:
        """Short description of what is playing or queued on `player`, or '' if its queue is empty."""
        q = self._queue_for_player(player)
        title = ""
        has_items = False
        if q is not None:
            has_items = int(q.items or 0) > 0
            if qi := q.current_item:
                title = getattr(qi.media_item, "name", "") or qi.name or ""
        if not title and (cm := getattr(player, "current_media", None)):
            title = getattr(cm, "title", "") or ""
        if not title and not has_items:
            return ""
        is_playing = getattr(player, "playback_state", None) == PlaybackState.PLAYING or (
            q is not None and q.state == PlaybackState.PLAYING
        )
        prefix = "Playing" if is_playing else "Queued"
        return f"{prefix}: {title}" if title else prefix

    def _pick_auto_player(self, players: list[Player]) -> Player | None:
        """Selects the player with items in its queue; if more than one (or none), prefers the local PC."""
        avail = [p for p in players if p.available]
        local = next((p for p in players if self.is_local_player(p)), None)
        if not avail:
            return local or (players[0] if players else None)
        with_queue = [p for p in avail if self._player_has_queue_content(p)]
        if len(with_queue) == 1:
            return with_queue[0]
        if len(with_queue) > 1:
            for p in with_queue:
                if self.is_local_player(p):
                    return p
            for p in with_queue:
                if p.playback_state == PlaybackState.PLAYING:
                    return p
            return with_queue[0]
        # No player has queue items: pick the local player right away (even while Sendspin
        # is still finishing its connection handshake) instead of flashing another device.
        if local is not None:
            return local
        return avail[0]

    def _auto_select_player(self) -> bool:
        """Applies automatic player selection unless the user explicitly chose one in this session."""
        if self._manual_player_selection:
            return False
        picked = self._pick_auto_player(self.get_available_players())
        if picked and picked.player_id != self.session_store.data.selected_player_id:
            self.session_store.data.selected_player_id = picked.player_id
            self.session_store.save()
            return True
        return False

    def _ensure_selected_player(self) -> None:
        players = self.get_available_players()
        if not players:
            return
        current = self.session_store.data.selected_player_id
        if current and any(p.player_id == current and p.available for p in players):
            return
        picked = self._pick_auto_player(players)
        if picked:
            self.session_store.data.selected_player_id = picked.player_id
            self.session_store.save()

    async def select_player(self, player_id: str, transfer_playback: bool = False) -> None:
        prev_queue = self.active_queue_id
        self._manual_player_selection = True
        self.session_store.data.selected_player_id = player_id
        self.session_store.save()
        if transfer_playback and self.client and prev_queue and prev_queue != player_id:
            prev_q_obj = self.client.player_queues.get(prev_queue)
            if prev_q_obj and prev_q_obj.current_item:
                try:
                    await self.client.player_queues.transfer(
                        prev_queue,
                        player_id,
                        auto_play=(prev_q_obj.state == PlaybackState.PLAYING),
                    )
                except MusicAssistantError as err:
                    logger.warning("Could not transfer the queue: %s", err)
        self._emit_players()
        self._emit_state()
        await self.refresh_queue_items(force=True)

    # --- Incoming events from Music Assistant ---

    def _on_mass_event(self, event: MassEvent) -> None:
        ev = event.event
        if ev in (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED, EventType.PLAYER_UPDATED):
            changed = self._auto_select_player()
            self._emit_players()
            self._emit_state()
            if changed:
                asyncio.create_task(self.refresh_queue_items(force=True))
            if self._startup_suppress_until > time.monotonic():
                asyncio.create_task(self._suppress_startup_autoplay())
        elif ev in (EventType.QUEUE_ADDED, EventType.QUEUE_UPDATED):
            if self._auto_select_player():
                self._emit_players()
                asyncio.create_task(self.refresh_queue_items(force=True))
            self._emit_state()
            if self._startup_suppress_until > time.monotonic():
                asyncio.create_task(self._suppress_startup_autoplay())
            if event.object_id == self.active_queue_id and self.client:
                q = self.client.player_queues.get(event.object_id)
                cur_item_id = q.current_item.queue_item_id if q and q.current_item else None
                key = (event.object_id, q.items if q else None, cur_item_id)
                if key != self._last_fetched_queue_key:
                    asyncio.create_task(self.refresh_queue_items(force=True))
        elif ev == EventType.QUEUE_ITEMS_UPDATED:
            if event.object_id == self.active_queue_id:
                asyncio.create_task(self.refresh_queue_items(force=True))
        elif ev == EventType.QUEUE_TIME_UPDATED:
            if event.object_id == self.active_queue_id:
                self._emit_state()
        elif ev in (EventType.MEDIA_ITEM_UPDATED, EventType.MEDIA_ITEM_ADDED):
            self._emit_state()
            uri = event.object_id or ""
            if not uri and isinstance(event.data, dict):
                uri = str(event.data.get("uri") or "")
            if uri:
                for cb in list(self._media_updated_listeners):
                    self.runner.call_ui(cb, uri)

    # --- Current state snapshot (NowPlayingState) ---

    def resolve_image_url(self, item: Any, size: int = 300) -> str:
        if not item or not self.client or not self.client.server_info:
            return ""
        try:
            url = self.client.get_media_item_image_url(item, ImageType.THUMB, size=size)
            if url:
                return url
        except Exception:
            pass
        if img_url := getattr(item, "image_url", None):
            return str(img_url)
        return ""

    @staticmethod
    def is_service_playlist(playlist: Any) -> bool:
        """False for the dynamic lists Music Assistant generates itself (Random tracks, Recently played…).

        Playlists the user created in MA's own storage are editable and are kept.
        """
        mappings = getattr(playlist, "provider_mappings", None) or ()
        only_builtin = bool(mappings) and all(
            getattr(mp, "provider_domain", None) == "builtin" for mp in mappings
        )
        return not (only_builtin and not getattr(playlist, "is_editable", False))

    _FAVORITES_NAME = re.compile(r"favorit|favourite|liked|me gusta|coup de c", re.IGNORECASE)

    @classmethod
    def favorites_playlist_domain(cls, playlist: Any) -> str | None:
        """Provider domain when `playlist` is a service's own favorites list ("Liked Songs"), else None."""
        if getattr(playlist, "is_editable", True):
            return None
        for mp in getattr(playlist, "provider_mappings", None) or ():
            domain = getattr(mp, "provider_domain", None)
            if not domain or domain in ("library", "builtin"):
                continue
            item_id = str(getattr(mp, "item_id", "") or "")
            if item_id.startswith("liked_songs") or cls._FAVORITES_NAME.search(
                getattr(playlist, "name", "") or ""
            ):
                return str(domain)
        return None

    _SERVICE_NAMES = {
        "spotify": "Spotify",
        "apple_music": "Apple Music",
        "tidal": "Tidal",
        "qobuz": "Qobuz",
        "deezer": "Deezer",
        "ytmusic": "YouTube Music",
        "soundcloud": "SoundCloud",
        "plex": "Plex",
        "jellyfin": "Jellyfin",
        "subsonic": "Subsonic",
    }

    @classmethod
    def display_name(cls, item: Any) -> str:
        """Name to show for an item; a service's favorites list gets the service in parentheses."""
        name = getattr(item, "name", "") or ""
        domain = cls.favorites_playlist_domain(item) if getattr(item, "media_type", None) == MediaType.PLAYLIST else None
        if not domain:
            return name
        service = cls._SERVICE_NAMES.get(domain) or domain.replace("_", " ").title()
        return name if f"({service})" in name else f"{name} ({service})"

    def current_service_domain(self) -> str | None:
        """Provider domain of the track that is playing now (e.g. "spotify"), if known."""
        try:
            item = self.get_now_playing().media_item
        except Exception:
            return None
        for mp in getattr(item, "provider_mappings", None) or ():
            domain = getattr(mp, "provider_domain", None)
            if domain and domain not in ("library", "builtin"):
                return str(domain)
        return None

    def favorites_first(self, items: list[Any], playlist_of: Callable[[Any], Any] = lambda x: x) -> list[Any]:
        """Moves the favorites playlist of the current service to the top (all of them if none plays)."""
        current = self.current_service_domain()
        pinned: list[Any] = []
        for it in items:
            domain = self.favorites_playlist_domain(playlist_of(it))
            if domain and (current is None or domain == current):
                pinned.append(it)
        if not pinned:
            return items
        return pinned + [it for it in items if all(it is not x for x in pinned)]

    def get_now_playing(self) -> NowPlayingState:
        if not self.client or not self.client.connection.connected:
            return NowPlayingState(connected=False)

        pid = self.active_player_id
        player = self.client.players.get(pid) if pid else None
        qid = self.active_queue_id
        queue = self.client.player_queues.get(qid) if qid else None

        if not player:
            return NowPlayingState(connected=True)

        state = player.playback_state
        if queue and queue.active:
            if player.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                state = player.playback_state
            else:
                state = queue.state
        now = time.time()

        title = ""
        artist = ""
        album = ""
        uri = ""
        queue_item_id = ""
        image_url = ""
        duration = 0.0
        position = 0.0
        favorite = False
        media_item: Any = None

        base_elapsed = 0.0
        if queue and queue.current_item:
            qi = queue.current_item
            queue_item_id = qi.queue_item_id
            media_item = qi.media_item
            title = getattr(media_item, "name", "") or qi.name or ""
            artist = format_artists(media_item)
            album_obj = getattr(media_item, "album", None)
            album = getattr(album_obj, "name", "") if album_obj else ""
            uri = getattr(media_item, "uri", "") or ""
            favorite = bool(getattr(media_item, "favorite", False))
            duration = float(qi.duration or getattr(media_item, "duration", 0) or 0)
            image_url = self.resolve_image_url(qi, size=300)
            base_elapsed = float(queue.elapsed_time or 0.0)
            updated_at = float(queue.elapsed_time_last_updated or now)
            if state == PlaybackState.PLAYING:
                position = base_elapsed + max(0.0, now - updated_at)
            else:
                position = base_elapsed
        elif player.current_media:
            pm = player.current_media
            title = pm.title or ""
            artist = pm.artist or ""
            album = pm.album or ""
            uri = pm.uri or ""
            queue_item_id = pm.queue_item_id or ""
            image_url = pm.image_url or ""
            duration = float(pm.duration or 0.0)
            base_elapsed = float(pm.elapsed_time or player.elapsed_time or 0.0)
            updated_at = float(
                pm.elapsed_time_last_updated or player.elapsed_time_last_updated or now
            )
            if state == PlaybackState.PLAYING:
                position = base_elapsed + max(0.0, now - updated_at)
            else:
                position = base_elapsed

        pending = self._pending_playback
        if pending is not None:
            target, until, from_key, wait_audio = pending
            if from_key:
                cur_key = queue_item_id or uri
                track_changed = bool(cur_key and cur_key != from_key)
                if track_changed and self._optimistic_track is not None:
                    # Server has switched to the new item: drop optimistic preview, keep pending lock
                    self._optimistic_track = None
                if target is None:
                    reached = track_changed
                else:
                    audio_started = (not wait_audio) or (base_elapsed > 0.2) or (duration <= 0)
                    reached = track_changed and state == target and audio_started
            else:
                reached = state == target or (
                    target == PlaybackState.PAUSED
                    and state in (PlaybackState.IDLE, PlaybackState.PAUSED)
                )
            if reached or time.monotonic() > until:
                self._pending_playback = pending = None
                self._optimistic_track = None

        if pending is not None and self._optimistic_track is not None:
            opt = self._optimistic_track
            title = opt["title"]
            artist = opt["artist"]
            album = opt["album"]
            uri = opt["uri"]
            queue_item_id = opt["queue_item_id"]
            image_url = opt["image_url"]
            duration = opt["duration"]
            position = 0.0
            favorite = opt["favorite"]
            media_item = opt["media_item"]

        if duration > 0 and position > duration:
            position = duration

        # Players without a native pause (CERO's own Sendspin player) are stopped by MA on pause,
        # keeping the track and the position. That is a pause for the user (and for MPRIS clients).
        if (
            state == PlaybackState.IDLE
            and queue is not None
            and queue.current_item is not None
            and position > 0
            and (duration <= 0 or position < duration - 1)
        ):
            state = PlaybackState.PAUSED

        vol = int(player.volume_level if player.volume_level is not None else 100)
        muted = bool(player.volume_muted)
        shuffle = bool(queue.shuffle_enabled) if queue else False
        repeat = queue.repeat_mode if queue else RepeatMode.OFF
        is_local = self.is_local_player(player)
        quality = self._build_quality_info(
            queue.current_item if queue else None,
            media_item,
            player,
            is_local=is_local,
        )

        source_context = self._playback_context_by_queue.get(qid) if qid else None
        return NowPlayingState(
            connected=True,
            player_id=player.player_id,
            player_name=player.name,
            is_local_player=is_local,
            playback_state=state,
            pending=pending is not None,
            title=title,
            artist=artist,
            album=album,
            uri=uri,
            queue_item_id=queue_item_id,
            image_url=image_url,
            duration=duration,
            position=position,
            volume=vol,
            muted=muted,
            shuffle=shuffle,
            repeat=repeat,
            favorite=favorite,
            can_seek=duration > 0,
            media_item=media_item,
            source_context=source_context,
            quality=quality,
        )

    def _build_quality_info(
        self,
        qi: QueueItem | None,
        media_item: Any,
        player: Player,
        *,
        is_local: bool,
    ) -> AudioQualityInfo | None:
        sd = getattr(qi, "streamdetails", None) if qi is not None else None
        af = getattr(sd, "audio_format", None) if sd is not None else None
        if af is None and media_item is not None:
            af = getattr(media_item, "audio_metadata", None)

        if sd is None and af is None and not is_local:
            return None

        def _enum_str(val: Any) -> str:
            if val is None:
                return ""
            return str(getattr(val, "value", val))

        src_codec = _enum_str(getattr(af, "content_type", None) or getattr(af, "codec_type", None)).upper()
        if src_codec in ("UNKNOWN", "?"):
            src_codec = ""
        src_sr = int(getattr(af, "sample_rate", 0) or 0)
        src_bd = int(getattr(af, "bit_depth", 0) or 0)
        src_ch = int(getattr(af, "channels", 0) or 0)
        src_br = int(getattr(af, "bit_rate", 0) or 0)

        out_codec, out_sr, out_bd, out_ch = ("", 0, 0, 0)
        if is_local and self._local_output_format_cb is not None:
            if loc := self._local_output_format_cb():
                out_codec, out_sr, out_ch, out_bd = (
                    str(loc[0]).upper(),
                    int(loc[1]),
                    int(loc[3]),
                    int(loc[2]),
                )
        if not out_codec and sd is not None:
            daf = getattr(sd, "decoded_audio_format", None)
            if daf is not None:
                out_codec = _enum_str(
                    getattr(daf, "content_type", None) or getattr(daf, "codec_type", None)
                ).upper()
                out_sr = int(getattr(daf, "sample_rate", 0) or 0)
                out_bd = int(getattr(daf, "bit_depth", 0) or 0)
                out_ch = int(getattr(daf, "channels", 0) or 0)

        codec_for_badge = src_codec or out_codec
        sr_for_badge = src_sr or out_sr
        bd_for_badge = src_bd or out_bd

        is_lossless = codec_for_badge in ("FLAC", "ALAC", "WAV", "PCM", "AIFF", "DSD", "DSF")
        is_hires = is_lossless and (sr_for_badge > 48000 or bd_for_badge > 16)

        if is_hires:
            tier = "HR"
        elif is_lossless:
            tier = "HQ"
        elif src_br > 0 and src_br < 256:
            tier = "LQ"
        else:
            tier = "SQ"

        out_is_lossless = out_codec in ("FLAC", "ALAC", "WAV", "PCM", "AIFF", "DSD", "DSF")
        out_is_hires = out_is_lossless and (out_sr > 48000 or out_bd > 16)
        if out_is_hires:
            out_tier = "HR"
        elif out_is_lossless:
            out_tier = "HQ"
        else:
            out_tier = tier

        parts: list[str] = []
        if codec_for_badge:
            parts.append(codec_for_badge)
        if bd_for_badge > 0 and sr_for_badge > 0:
            sr_khz = f"{sr_for_badge / 1000:g}"
            parts.append(f"{sr_khz} kHz / {bd_for_badge} bit")
        elif sr_for_badge > 0:
            parts.append(f"{sr_for_badge / 1000:g} kHz")
        elif src_br > 0:
            parts.append(f"{src_br} kbps")

        badge_text = " • ".join(parts) if len(parts) > 1 else "".join(parts)
        if not badge_text:
            return None

        prov_raw = str(getattr(sd, "provider", "") or getattr(media_item, "provider", "") or "")
        prov_clean = prov_raw.split("--")[0].replace("_", " ").title() if prov_raw else ""

        protocol = (
            "Sendspin"
            if is_local
            else str(getattr(player, "provider", "") or "").replace("_", " ").title()
        )
        proc = getattr(sd, "audio_processing", None)
        proc_str = ", ".join(str(x) for x in proc) if isinstance(proc, (list, tuple)) else str(proc or "")

        norm_mode = _enum_str(getattr(sd, "volume_normalization_mode", None))

        return AudioQualityInfo(
            tier=tier,
            badge_text=badge_text,
            is_hires=is_hires,
            is_lossless=is_lossless,
            source_provider=prov_clean,
            source_codec=src_codec,
            source_sample_rate=src_sr,
            source_bit_depth=src_bd,
            source_channels=src_ch,
            source_bit_rate=src_br,
            output_tier=out_tier,
            output_codec=out_codec,
            output_sample_rate=out_sr,
            output_bit_depth=out_bd,
            output_channels=out_ch,
            stream_type=_enum_str(getattr(sd, "stream_type", None)),
            protocol=protocol,
            loudness=getattr(sd, "loudness", None),
            loudness_album=getattr(sd, "loudness_album", None),
            normalization_mode=norm_mode,
            target_loudness=getattr(sd, "target_loudness", None),
            audio_processing=proc_str,
        )

    # --- Playback and queue controls ---

    async def _clear_other_queues(self, active_queue_id: str) -> None:
        """Stops and clears the queues of all other devices before playing."""
        if not self.client or not active_queue_id:
            return
        tasks = []
        for q in list(self.client.player_queues.player_queues):
            if q.queue_id == active_queue_id:
                continue
            has_content = (
                (q.items or 0) > 0
                or q.current_item is not None
                or q.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
            )
            if has_content:
                tasks.append(self.client.player_queues.clear(q.queue_id))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def play_pause(self) -> None:
        if not self.client:
            return
        self._startup_suppress_until = 0.0
        snap = self.get_now_playing()
        if snap.pending:
            return  # the previous command has not been confirmed yet
        if snap.playback_state == PlaybackState.PLAYING:
            await self.pause()
        else:
            await self.play()

    def _set_pending(
        self,
        target: PlaybackState | None,
        *,
        from_item_key: str = "",
        timeout_s: float = 10.0,
        wait_audio: bool = False,
        preview: Any = None,
    ) -> None:
        """Marks a transport command as waiting for the server, so the UI shows it is busy."""
        self._optimistic_track = self._build_optimistic_track(preview) if preview is not None else None
        self._pending_playback = (target, time.monotonic() + timeout_s, from_item_key, wait_audio)
        self._emit_state()
        # If the server never answers, the busy indicator must not stay forever
        asyncio.get_running_loop().call_later(timeout_s + 0.5, self._expire_pending)

    def _build_optimistic_track(self, item: Any) -> dict[str, Any] | None:
        if item is None or isinstance(item, str):
            return None
        media = getattr(item, "media_item", None) or item
        if isinstance(media, str):
            return None
        alb_obj = getattr(media, "album", None)
        return {
            "title": getattr(media, "name", "") or getattr(item, "name", "") or "",
            "artist": format_artists(media),
            "album": getattr(alb_obj, "name", "") if alb_obj else "",
            "uri": getattr(media, "uri", "") or "",
            "queue_item_id": getattr(item, "queue_item_id", "") or "",
            "image_url": self.resolve_image_url(item, size=300)
            or self.resolve_image_url(media, size=300),
            "duration": float(
                getattr(item, "duration", 0) or getattr(media, "duration", 0) or 0
            ),
            "favorite": bool(getattr(media, "favorite", False)),
            "media_item": media,
        }

    def _shorten_pending(self, remaining_s: float = 1.2) -> None:
        """Caps an active pending wait after the server call returns (e.g. end of queue)."""
        if self._pending_playback is None:
            return
        target, _until, from_key, wait_audio = self._pending_playback
        self._pending_playback = (target, time.monotonic() + remaining_s, from_key, wait_audio)
        asyncio.get_running_loop().call_later(remaining_s + 0.1, self._expire_pending)

    def _expire_pending(self) -> None:
        if self._pending_playback is not None and time.monotonic() > self._pending_playback[1]:
            self._clear_pending()

    def _clear_pending(self) -> None:
        if self._pending_playback is not None or self._optimistic_track is not None:
            self._pending_playback = None
            self._optimistic_track = None
            self._emit_state()

    async def play(self) -> None:
        if not self.client:
            return
        self._startup_suppress_until = 0.0
        self._set_pending(PlaybackState.PLAYING)
        try:
            await self._play_now()
        except BaseException:
            self._clear_pending()
            raise

    async def _play_now(self) -> None:
        qid = self.active_queue_id
        pid = self.active_player_id
        if qid:
            await self._clear_other_queues(qid)
        if pid:
            # Same command the MA web frontend sends (`players/cmd/play`): unpauses an active
            # protocol session in ~0.3 s without restarting the stream, and automatically
            # falls back to `player_queues/resume` on the server when the player is IDLE.
            await self.client.players.play(pid)
        elif qid:
            await self.client.player_queues.play(qid)

    async def pause(self) -> None:
        if not self.client:
            return
        self._set_pending(PlaybackState.PAUSED)
        try:
            await self._pause_now()
        except BaseException:
            self._clear_pending()
            raise

    async def _pause_now(self) -> None:
        qid = self.active_queue_id
        pid = self.active_player_id
        if pid:
            await self.client.players.pause(pid)
        elif qid and self.client.player_queues.get(qid):
            await self.client.player_queues.pause(qid)

    async def stop_playback(self) -> None:
        if not self.client:
            return
        qid = self.active_queue_id
        pid = self.active_player_id
        if qid and self.client.player_queues.get(qid):
            await self.client.player_queues.stop(qid)
        elif pid:
            await self.client.players.stop(pid)

    async def next_track(self) -> None:
        if not self.client or self._pending_playback is not None:
            return
        snap = self.get_now_playing()
        prev_key = snap.queue_item_id or snap.uri
        target = PlaybackState.PLAYING if snap.playback_state == PlaybackState.PLAYING else None
        self._set_pending(
            target if prev_key else PlaybackState.PLAYING,
            from_item_key=prev_key,
            wait_audio=(target == PlaybackState.PLAYING),
        )
        try:
            qid = self.active_queue_id
            pid = self.active_player_id
            if qid and self.client.player_queues.get(qid):
                await self.client.player_queues.next(qid)
            elif pid:
                await self.client.players.next_track(pid)
        except BaseException:
            self._clear_pending()
            raise
        # Only shorten quickly if the track did not change at all (e.g. end of queue)
        after = self.get_now_playing()
        if (after.queue_item_id or after.uri) == prev_key:
            self._shorten_pending(1.2)

    async def previous_track(self) -> None:
        if not self.client or self._pending_playback is not None:
            return
        snap = self.get_now_playing()
        if snap.position > 5.0 and snap.can_seek:
            self._set_pending(snap.playback_state, from_item_key="__seek__", timeout_s=4.0)
            try:
                qid = self.active_queue_id
                pid = self.active_player_id
                if qid and self.client.player_queues.get(qid):
                    await self.client.player_queues.seek(qid, 0)
                elif pid:
                    await self.client.players.seek(pid, 0)
            finally:
                self._clear_pending()
            return
        prev_key = snap.queue_item_id or snap.uri
        target = PlaybackState.PLAYING if snap.playback_state == PlaybackState.PLAYING else None
        self._set_pending(
            target if prev_key else PlaybackState.PLAYING,
            from_item_key=prev_key,
            wait_audio=(target == PlaybackState.PLAYING),
        )
        try:
            qid = self.active_queue_id
            pid = self.active_player_id
            if qid and self.client.player_queues.get(qid):
                await self.client.player_queues.previous(qid)
            elif pid:
                await self.client.players.previous_track(pid)
        except BaseException:
            self._clear_pending()
            raise
        after = self.get_now_playing()
        if (after.queue_item_id or after.uri) == prev_key:
            self._shorten_pending(1.2)

    async def seek_seconds(self, position_seconds: float) -> None:
        if not self.client or self._pending_playback is not None:
            return
        pos = max(0, int(round(position_seconds)))
        qid = self.active_queue_id
        pid = self.active_player_id
        if qid and self.client.player_queues.get(qid):
            await self.client.player_queues.seek(qid, pos)
        elif pid:
            await self.client.players.seek(pid, pos)

    async def set_volume(self, volume: int) -> None:
        if not self.client:
            return
        pid = self.active_player_id
        if not pid:
            return
        clamped = max(0, min(100, int(round(volume))))
        await self.client.players.volume_set(pid, clamped)

    async def set_mute(self, muted: bool) -> None:
        if not self.client:
            return
        pid = self.active_player_id
        if not pid:
            return
        await self.client.players.volume_mute(pid, muted)

    async def set_shuffle(self, enabled: bool) -> None:
        if not self.client or self._pending_playback is not None:
            return
        qid = self.active_queue_id
        if qid and self.client.player_queues.get(qid):
            await self.client.player_queues.shuffle(qid, enabled)

    async def toggle_shuffle(self) -> None:
        if self._pending_playback is not None:
            return
        snap = self.get_now_playing()
        await self.set_shuffle(not snap.shuffle)

    async def set_repeat(self, mode: RepeatMode) -> None:
        if not self.client or self._pending_playback is not None:
            return
        qid = self.active_queue_id
        if qid and self.client.player_queues.get(qid):
            await self.client.player_queues.repeat(qid, mode)

    async def cycle_repeat(self) -> None:
        if self._pending_playback is not None:
            return
        snap = self.get_now_playing()
        order = [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE]
        try:
            nxt = order[(order.index(snap.repeat) + 1) % len(order)]
        except ValueError:
            nxt = RepeatMode.ALL
        await self.set_repeat(nxt)

    async def toggle_favorite_item(self, item: Any) -> bool:
        """Toggles an item's favorite state and returns the new value."""
        if not self.client or item is None:
            return False
        current = bool(getattr(item, "favorite", False))
        new_val = not current
        # Optimistic: show the new state right away and revert it if the server refuses
        self._set_favorite_flag(item, new_val)
        self._favorite_keys_at = 0.0  # the next list reloads the favorites
        try:
            if new_val:
                await self.client.music.add_item_to_favorites(item)
            else:
                media_type = getattr(item, "media_type", None)
                item_id = getattr(item, "item_id", None)
                provider = getattr(item, "provider", "")
                lib_ref = (
                    (media_type, item_id)
                    if provider == "library" and media_type and item_id is not None
                    else self.find_library_id(item)
                )
                if lib_ref is not None:
                    await self.client.music.remove_item_from_favorites(lib_ref[0], lib_ref[1])
                else:
                    await self.client.music.set_item_favorite(item, False)
        except BaseException:
            self._set_favorite_flag(item, current)
            raise
        if new_val and not self.is_in_library(item):
            # A favorite is also in the library; the add can be slow, so it runs on its own
            asyncio.ensure_future(self._add_favorite_to_library(item))
        return new_val

    def _set_favorite_flag(self, item: Any, value: bool) -> None:
        try:
            item.favorite = value
        except Exception:
            pass
        self._emit_state()
        self._emit_favorite_changed()

    async def _add_favorite_to_library(self, item: Any) -> None:
        try:
            await self.toggle_library_item(item)
        except Exception as err:
            logger.warning("Could not add %s to the library: %s", getattr(item, "uri", item), err)

    # --- Library management (add / remove / query state) ---

    def _index_single_library_item(self, item: Any) -> None:
        mt = getattr(item, "media_type", None)
        iid = getattr(item, "item_id", None)
        prov = getattr(item, "provider", "")
        if not mt or iid is None:
            return
        if prov != "library":
            return
        ref = (mt, str(iid))
        self._removed_lib_keys.discard((mt.value, str(iid)))
        if uri := getattr(item, "uri", None):
            self._lib_id_by_uri[str(uri)] = ref
        for mp in getattr(item, "provider_mappings", None) or ():
            mp_iid = getattr(mp, "item_id", None)
            if not mp_iid:
                continue
            for p_key in (
                getattr(mp, "provider_instance", None),
                getattr(mp, "provider_domain", None),
            ):
                if p_key:
                    self._lib_id_by_mapping[(mt.value, str(p_key), str(mp_iid))] = ref

    def index_library_items(self, items: list[Any]) -> None:
        for it in items:
            self._index_single_library_item(it)

    def rebuild_library_index(self, home: HomeContent) -> None:
        self._lib_id_by_uri.clear()
        self._lib_id_by_mapping.clear()
        for lst in (home.albums, home.playlists, home.artists, home.radios):
            for it in lst:
                self._index_single_library_item(it)

    def find_library_id(self, item: Any) -> tuple[MediaType, str] | None:
        if item is None:
            return None
        mt = getattr(item, "media_type", None)
        iid = getattr(item, "item_id", None)
        prov = getattr(item, "provider", "")
        if prov == "library" and mt is not None and iid is not None:
            if (mt.value, str(iid)) not in self._removed_lib_keys:
                return (mt, str(iid))
        if uri := getattr(item, "uri", None):
            if ref := self._lib_id_by_uri.get(str(uri)):
                return ref
        if mt is not None and iid is not None and prov:
            if ref := self._lib_id_by_mapping.get((mt.value, str(prov), str(iid))):
                return ref
        if mt is not None:
            for mp in getattr(item, "provider_mappings", None) or ():
                mp_iid = getattr(mp, "item_id", None)
                if not mp_iid:
                    continue
                for p_key in (
                    getattr(mp, "provider_instance", None),
                    getattr(mp, "provider_domain", None),
                ):
                    if p_key and (
                        ref := self._lib_id_by_mapping.get((mt.value, str(p_key), str(mp_iid)))
                    ):
                        return ref
        return None

    def is_in_library(self, item: Any) -> bool:
        if self.find_library_id(item) is not None:
            return True
        # Optimistic: an item being added counts as in the library (the server can take ~30 s)
        return bool(self._pending_lib_adds and self._library_keys(item) & self._pending_lib_adds)

    def _library_keys(self, item: Any) -> set[str]:
        """URIs that identify an item: its own URI and the URIs of its provider versions."""
        keys = set(self._provider_uris(item))
        if uri := getattr(item, "uri", None):
            keys.add(str(uri))
        return keys

    async def add_track_to_playlist(self, playlist_item_id: str | int, track_uri: str) -> None:
        if not self.client or not track_uri:
            return
        await self.client.music.add_playlist_tracks(playlist_item_id, [track_uri])

    async def remove_playlist_track(self, playlist: Any, track: Any) -> None:
        """Removes a track (by its position) from an editable playlist."""
        position = getattr(track, "position", None)
        if not self.client or playlist is None or not position:
            raise RuntimeError("The track has no position in the playlist")
        await self.client.music.remove_playlist_tracks(playlist.item_id, (int(position),))

    @staticmethod
    def _provider_uris(item: Any) -> list[str]:
        """URIs of the original provider versions of an item (available ones first)."""
        mt = getattr(item, "media_type", None)
        if mt is None:
            return []
        mappings = sorted(
            getattr(item, "provider_mappings", None) or (),
            key=lambda m: not getattr(m, "available", True),
        )
        uris: list[str] = []
        for mp in mappings:
            domain = getattr(mp, "provider_domain", None)
            iid = getattr(mp, "item_id", None)
            if not domain or not iid or domain == "library":
                continue
            uri = f"{domain}://{mt.value}/{iid}"
            if uri not in uris:
                uris.append(uri)
        return uris

    async def _add_to_library(self, item: Any) -> Any:
        """Adds an item to the library.

        A library object that was removed no longer exists on the server: adding it again
        fails with MediaNotFoundError. In that case it is re-added from the URI of its
        original provider (e.g. apple_music://album/123).
        """
        assert self.client is not None
        if getattr(item, "provider", "") == "library":
            last_err: Exception | None = None
            for uri in self._provider_uris(item):
                try:
                    return await self.client.music.add_item_to_library(uri)
                except MusicAssistantError as err:
                    last_err = err
            if last_err is not None:
                raise last_err
        return await self.client.music.add_item_to_library(item)

    async def toggle_library_item(self, item: Any) -> bool:
        """Adds or removes an item (album, playlist, artist, radio) from the MA library."""
        if not self.client or item is None:
            return False
        lib_ref = self.find_library_id(item)
        if lib_ref is not None:
            if self.favorites_playlist_domain(item):
                return True  # service favorites playlists are protected from removal
            mt, lib_id = lib_ref
            # Optimistic: it leaves the library (and loses its favorite flag) right away;
            # both are restored if the server refuses
            by_uri = {k: r for k, r in self._lib_id_by_uri.items() if r == lib_ref}
            by_mapping = {k: r for k, r in self._lib_id_by_mapping.items() if r == lib_ref}
            was_favorite = bool(getattr(item, "favorite", False))
            was_removed = (mt.value, lib_id) in self._removed_lib_keys
            self._removed_lib_keys.add((mt.value, lib_id))
            for k in by_uri:
                self._lib_id_by_uri.pop(k, None)
            for k in by_mapping:
                self._lib_id_by_mapping.pop(k, None)
            try:
                item.favorite = False
            except Exception:
                pass
            self._emit_library_changed()
            self._emit_favorite_changed()
            try:
                await self.client.music.remove_item_from_library(mt, lib_id, recursive=True)
            except BaseException:
                self._lib_id_by_uri.update(by_uri)
                self._lib_id_by_mapping.update(by_mapping)
                if not was_removed:
                    self._removed_lib_keys.discard((mt.value, lib_id))
                try:
                    item.favorite = was_favorite
                except Exception:
                    pass
                self._emit_library_changed()
                self._emit_favorite_changed()
                raise
            self._emit_state()
            return False

        keys = self._library_keys(item)
        if keys & self._pending_lib_adds:
            return True  # already being added: a second click must not send another request
        # Show it as added right away; the server request below can take up to ~30 s
        self._pending_lib_adds |= keys
        self._emit_library_changed()
        try:
            added = await self._add_to_library(item)
        except BaseException:
            self._pending_lib_adds -= keys
            self._emit_library_changed()  # revert the optimistic icon
            raise
        self._pending_lib_adds -= keys
        if isinstance(added, dict):
            added_id = added.get("item_id")
            raw_mt = added.get("media_type")
            try:
                added_mt = MediaType(raw_mt) if raw_mt else getattr(item, "media_type", None)
            except Exception:
                added_mt = getattr(item, "media_type", None)
            added_uri = added.get("uri")
            added_mappings = added.get("provider_mappings") or ()
        else:
            self._index_single_library_item(added)
            added_id = getattr(added, "item_id", None)
            added_mt = getattr(added, "media_type", None) or getattr(item, "media_type", None)
            added_uri = getattr(added, "uri", None)
            added_mappings = getattr(added, "provider_mappings", None) or ()

        if added_mt is None:
            added_mt = getattr(item, "media_type", None)
        if added_id is None and added_mt is not None:
            added_id = getattr(item, "item_id", None) or "pending"

        if added_id is not None and added_mt is not None:
            ref = (added_mt, str(added_id))
            # Only the NEW id comes back. The id of a removed library object must stay in
            # _removed_lib_keys: the URI index below redirects that stale object to the new id.
            self._removed_lib_keys.discard((added_mt.value, str(added_id)))
            if added_uri:
                self._lib_id_by_uri[str(added_uri)] = ref
            if uri := getattr(item, "uri", None):
                self._lib_id_by_uri[str(uri)] = ref
            if prov := getattr(item, "provider", None):
                if iid := getattr(item, "item_id", None):
                    self._lib_id_by_mapping[(added_mt.value, str(prov), str(iid))] = ref
            for mp in (*(added_mappings or ()), *(getattr(item, "provider_mappings", None) or ())):
                mp_iid = mp.get("item_id") if isinstance(mp, dict) else getattr(mp, "item_id", None)
                if not mp_iid:
                    continue
                p_inst = mp.get("provider_instance") if isinstance(mp, dict) else getattr(mp, "provider_instance", None)
                p_dom = mp.get("provider_domain") if isinstance(mp, dict) else getattr(mp, "provider_domain", None)
                for p_key in (p_inst, p_dom):
                    if p_key:
                        self._lib_id_by_mapping[(added_mt.value, str(p_key), str(mp_iid))] = ref
        self._emit_library_changed()
        return True

    async def play_media(
        self,
        media: MediaItemType | list[MediaItemType] | str | list[str],
        option: QueueOption = QueueOption.PLAY,
        start_item: str | None = None,
        radio_mode: bool = False,
        shuffle: bool | None = None,
        preview_item: Any = None,
    ) -> None:
        if not self.client:
            return
        self._startup_suppress_until = 0.0
        qid = self.active_queue_id
        if not qid:
            self._ensure_selected_player()
            qid = self.active_queue_id
        if not qid:
            raise RuntimeError("No player is available in Music Assistant")
        if option in (QueueOption.REPLACE, QueueOption.PLAY):
            prev_snap = self.get_now_playing()
            prev_key = prev_snap.queue_item_id or prev_snap.uri
            if not isinstance(media, list) and getattr(media, "media_type", None) in (
                MediaType.PLAYLIST,
                MediaType.ALBUM,
                MediaType.ARTIST,
            ):
                self._playback_context_by_queue[qid] = media
            else:
                self._playback_context_by_queue.pop(qid, None)
            cand = preview_item
            if cand is None:
                cand = media[0] if isinstance(media, list) and media else media
            self._set_pending(
                PlaybackState.PLAYING,
                from_item_key=prev_key,
                wait_audio=True,
                preview=cand,
            )
        if option != QueueOption.ADD:
            await self._clear_other_queues(qid)
        try:
            await self.client.player_queues.play_media(
                queue_id=qid,
                media=media,
                option=option,
                radio_mode=radio_mode,
                start_item=start_item,
                shuffle=shuffle,
            )
        except BaseException:
            if option in (QueueOption.REPLACE, QueueOption.PLAY):
                self._clear_pending()
            raise

    async def refresh_queue_items(self, force: bool = False) -> list[QueueItem]:
        if not self.client:
            return []
        qid = self.active_queue_id
        if not qid:
            return []
        q = self.client.player_queues.get(qid)
        cur_id = q.current_item.queue_item_id if q and q.current_item else None
        key = (qid, q.items if q else None, cur_id)
        if not force and key == self._last_fetched_queue_key:
            return self._cached_queue_items
        try:
            items = await self.client.player_queues.get_queue_items(qid, limit=250)
        except Exception as err:
            logger.debug("Could not load the queue items: %s", err)
            items = []
        self._cached_queue_items = items
        self._last_fetched_queue_key = key
        for cb in list(self._queue_listeners):
            self.runner.call_ui(cb, items, cur_id or "")
        return items

    async def play_queue_item(self, index_or_id: int | str) -> None:
        if not self.client or not self.active_queue_id:
            return
        qid = self.active_queue_id
        target: QueueItem | None = None
        if isinstance(index_or_id, int) and 0 <= index_or_id < len(self._cached_queue_items):
            target = self._cached_queue_items[index_or_id]
        else:
            for qi in self._cached_queue_items:
                if qi.queue_item_id == str(index_or_id):
                    target = qi
                    break

        prev_snap = self.get_now_playing()
        prev_key = prev_snap.queue_item_id or prev_snap.uri
        self._set_pending(
            PlaybackState.PLAYING,
            from_item_key=prev_key,
            wait_audio=True,
            preview=target,
        )
        if target is not None:
            # Advance the upcoming queue list immediately before server confirmation
            for cb in list(self._queue_listeners):
                self.runner.call_ui(cb, list(self._cached_queue_items), target.queue_item_id)
        try:
            await self._clear_other_queues(qid)
            await self.client.player_queues.play_index(qid, index_or_id)
        except BaseException:
            self._clear_pending()
            await self.refresh_queue_items(force=True)
            raise

    async def move_queue_item(self, queue_item_id: str, pos_shift: int) -> None:
        if not self.client or not self.active_queue_id or pos_shift == 0:
            return
        await self.client.player_queues.move_item(
            self.active_queue_id, queue_item_id, pos_shift=pos_shift
        )
        await self.refresh_queue_items(force=True)

    async def move_queue_item_next(self, queue_item_id: str) -> None:
        """Moves a queued item to play right after the current track."""
        if not self.client or not self.active_queue_id or not queue_item_id:
            return
        await self.client.player_queues.move_next(self.active_queue_id, queue_item_id)
        await self.refresh_queue_items(force=True)

    async def remove_queue_item(self, item_id_or_index: int | str) -> None:
        if not self.client or not self.active_queue_id:
            return
        await self.client.player_queues.delete_item(self.active_queue_id, item_id_or_index)
        await self.refresh_queue_items(force=True)

    async def remove_queue_items_from(self, start_index: int) -> None:
        """Removes the item at `start_index` and every item after it in the active queue."""
        if not self.client or not self.active_queue_id or start_index < 0:
            return
        qid = self.active_queue_id
        items = list(self._cached_queue_items)
        if not items or start_index >= len(items):
            items = await self.client.player_queues.get_queue_items(qid, limit=250)
        targets = items[start_index:]
        for qi in reversed(targets):
            try:
                await self.client.player_queues.delete_item(qid, qi.queue_item_id)
            except Exception as err:
                logger.debug("Could not delete queue item %s: %s", qi.queue_item_id, err)
        await self.refresh_queue_items(force=True)

    async def clear_queue(self) -> None:
        if not self.client or not self.active_queue_id:
            return
        qid = self.active_queue_id
        q = self.client.player_queues.get(qid)
        if q and getattr(q, "autoplay_enabled", False):
            try:
                await self.client.player_queues.dont_stop_the_music(qid, False)
            except Exception:
                pass
        if q and q.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            try:
                await self.client.player_queues.stop(qid)
            except Exception:
                pass
        await self.client.player_queues.clear(qid)
        self._playback_context_by_queue.pop(qid, None)
        self._cached_queue_items = []
        self._last_fetched_queue_key = (qid, 0, None)
        for cb in list(self._queue_listeners):
            self.runner.call_ui(cb, [], "")
        await self.refresh_queue_items(force=True)

    # --- Local disk cache + library queries ---

    def load_cached_home_content(self) -> HomeContent | None:
        """Quickly loads the last library snapshot from disk (synchronously)."""
        path = get_library_cache_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("server_url") != normalize_server_url(self.session_store.data.server_url):
                return None

            albums = [Album.from_dict(x) for x in raw.get("albums", [])]
            playlists = [Playlist.from_dict(x) for x in raw.get("playlists", [])]
            playlists = [p for p in playlists if self.is_service_playlist(p)]
            artists = [Artist.from_dict(x) for x in raw.get("artists", [])]
            radios = [Radio.from_dict(x) for x in raw.get("radios", [])]

            self._stamp_ranks(albums, is_playlist=False)
            self._stamp_ranks(playlists, is_playlist=True)
            self._stamp_ranks(artists, is_playlist=False)
            self._stamp_ranks(radios, is_playlist=False)

            hc = HomeContent(
                albums=albums,
                playlists=playlists,
                artists=artists,
                radios=radios,
            )
            self.rebuild_library_index(hc)
            return hc
        except Exception as err:
            logger.debug("Could not read the library cache: %s", err)
            return None

    def _save_cached_home_content(self, home: HomeContent) -> None:
        path = get_library_cache_path()
        try:
            payload = {
                "server_url": normalize_server_url(self.session_store.data.server_url),
                "updated_at": time.time(),
                "albums": [a.to_dict() for a in home.albums],
                "playlists": [p.to_dict() for p in home.playlists],
                "artists": [ar.to_dict() for ar in home.artists],
                "radios": [r.to_dict() for r in home.radios],
            }
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            tmp.replace(path)
        except Exception as err:
            logger.debug("Could not save the library cache: %s", err)

    @staticmethod
    def _stamp_ranks(items: list[Any], *, is_playlist: bool = False) -> None:
        total = len(items)
        for idx, item in enumerate(items):
            try:
                setattr(item, "_ma_recent_rank", total - idx)
                if is_playlist:
                    setattr(item, "_ma_modified_rank", total - idx)
            except Exception:
                pass

    async def _fetch_all_pages(
        self,
        fetch_fn: Callable[..., Any],
        *,
        page_size: int = 500,
        limit: int | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        items: list[Any] = []
        offset = 0
        while True:
            req_limit = page_size
            if limit is not None:
                rem = limit - len(items)
                if rem <= 0:
                    break
                req_limit = min(page_size, rem)
            batch = await fetch_fn(limit=req_limit, offset=offset, **kwargs)
            items.extend(batch)
            if len(batch) < req_limit:
                break
            offset += len(batch)
        return items

    async def get_discover_folders(self) -> list[RecommendationFolder]:
        """Gets all Music Assistant Discover rows with their items."""
        if not self.client:
            return []
        try:
            folders = await self.client.music.recommendations()
        except Exception as err:
            logger.debug("Could not load Discover recommendations: %s", err)
            return []

        async def _populate(folder: RecommendationFolder) -> RecommendationFolder:
            fid = (getattr(folder, "item_id", "") or "").casefold()
            fname = (getattr(folder, "name", "") or "").casefold()
            if (
                fid in ("recently_added_tracks", "recent_tracks")
                or "recently added tracks" in fname
                or "recent tracks" in fname
            ):
                folder.items = []
                return folder
            if getattr(folder, "items", None):
                return folder
            try:
                raw_items = await self.client.send_command(  # type: ignore[union-attr]
                    "music/recommendations/items",
                    provider=folder.provider,
                    item_id=folder.item_id,
                )
                parsed = [
                    media_from_dict(obj)
                    for obj in (raw_items or [])
                    if isinstance(obj, dict) and obj.get("media_type") != MediaType.FOLDER.value
                ]
                fname = (getattr(folder, "name", "") or "").casefold()
                fid = (getattr(folder, "item_id", "") or "").casefold()
                if "recent" in fname or "reciente" in fname or "recent" in fid or "played" in fid:
                    parsed = [
                        it for it in parsed if getattr(it, "media_type", None) != MediaType.PLAYLIST
                    ]
                folder.items = parsed
            except Exception as err:
                logger.debug("Error loading Discover '%s': %s", folder.name, err)
            return folder

        populated = await asyncio.gather(*(_populate(f) for f in folders))
        return [f for f in populated if getattr(f, "items", None)]

    async def get_home_content(self) -> HomeContent:
        if not self.client:
            return HomeContent()
        tasks = await asyncio.gather(
            self.client.music.recently_played(
                limit=12,
                media_types=[
                    MediaType.ALBUM,
                    MediaType.TRACK,
                    MediaType.RADIO,
                ],
                fully_played_only=False,
            ),
            self.get_discover_folders(),
            self.get_playlists(limit=None),
            self.get_albums(limit=None),
            self.get_artists(limit=None),
            self.get_radios(limit=None),
            return_exceptions=True,
        )

        def _ok(val: Any) -> list[Any]:
            return list(val) if isinstance(val, list) else []

        content = HomeContent(
            recently_played=_ok(tasks[0]),
            recommendations=_ok(tasks[1]),
            playlists=_ok(tasks[2]),
            albums=_ok(tasks[3]),
            artists=_ok(tasks[4]),
            radios=_ok(tasks[5]),
        )
        self.rebuild_library_index(content)
        await asyncio.to_thread(self._save_cached_home_content, content)
        return content

    async def search_all(self, query: str, limit: int = 25) -> SearchResults:
        if not self.client:
            return SearchResults()
        return await self.client.music.search(
            query,
            media_types=[
                MediaType.ALBUM,
                MediaType.TRACK,
                MediaType.ARTIST,
                MediaType.PLAYLIST,
                MediaType.RADIO,
            ],
            limit=limit,
        )

    async def get_playlists(
        self, search: str | None = None, limit: int | None = None
    ) -> list[Playlist]:
        if not self.client:
            return []
        try:
            items: list[Playlist] = await self._fetch_all_pages(
                self.client.music.get_library_playlists,
                limit=limit,
                search=search,
                order_by="timestamp_modified_desc",
            )
        except Exception:
            items = await self._fetch_all_pages(
                self.client.music.get_library_playlists,
                limit=limit,
                search=search,
            )
        items = [p for p in items if self.is_service_playlist(p)]
        self._stamp_ranks(items, is_playlist=True)
        self.index_library_items(items)
        return items

    async def get_albums(
        self,
        search: str | None = None,
        favorite: bool | None = None,
        limit: int | None = None,
    ) -> list[Album]:
        if not self.client:
            return []
        try:
            items: list[Album] = await self._fetch_all_pages(
                self.client.music.get_library_albums,
                limit=limit,
                search=search,
                favorite=favorite,
                order_by="timestamp_added_desc",
            )
        except Exception:
            items = await self._fetch_all_pages(
                self.client.music.get_library_albums,
                limit=limit,
                search=search,
                favorite=favorite,
            )
        self._stamp_ranks(items, is_playlist=False)
        self.index_library_items(items)
        return items

    async def get_artists(
        self,
        search: str | None = None,
        limit: int | None = None,
        album_artists_only: bool = False,
    ) -> list[Artist]:
        if not self.client:
            return []
        try:
            items: list[Artist] = await self._fetch_all_pages(
                self.client.music.get_library_artists,
                limit=limit,
                search=search,
                album_artists_only=album_artists_only,
                order_by="timestamp_added_desc",
            )
        except Exception:
            items = await self._fetch_all_pages(
                self.client.music.get_library_artists,
                limit=limit,
                search=search,
                album_artists_only=album_artists_only,
            )
        self._stamp_ranks(items, is_playlist=False)
        self.index_library_items(items)
        return items

    async def get_radios(
        self, search: str | None = None, limit: int | None = None
    ) -> list[Radio]:
        if not self.client:
            return []
        items: list[Radio] = await self._fetch_all_pages(
            self.client.music.get_library_radios,
            limit=limit,
            search=search,
        )
        self._stamp_ranks(items, is_playlist=False)
        self.index_library_items(items)
        return items

    async def get_tracks(
        self,
        search: str | None = None,
        favorite: bool | None = None,
        limit: int | None = 500,
    ) -> list[Track]:
        if not self.client:
            return []
        return await self._fetch_all_pages(
            self.client.music.get_library_tracks,
            limit=limit,
            search=search,
            favorite=favorite,
        )

    @staticmethod
    def _track_keys(track: Any) -> set[tuple[str, str]]:
        """(provider, item_id) pairs that identify a track: itself and each provider version."""
        keys: set[tuple[str, str]] = set()
        if (iid := getattr(track, "item_id", None)) is not None and getattr(track, "provider", None):
            keys.add((str(track.provider), str(iid)))
        for mp in getattr(track, "provider_mappings", None) or ():
            if (mid := getattr(mp, "item_id", None)) is None:
                continue
            for prov in (getattr(mp, "provider_domain", None), getattr(mp, "provider_instance", None)):
                if prov:
                    keys.add((str(prov), str(mid)))
        return keys

    async def mark_favorite_tracks(self, tracks: list[Any]) -> None:
        """Sets `favorite` on tracks that are favorites on the server.

        Tracks listed straight from a service (an album or playlist of Apple Music, Spotify…)
        come without the flag: it only exists on the library copy of the track.
        """
        if not self.client or not tracks:
            return
        if time.monotonic() - self._favorite_keys_at > 120:
            try:
                favs = await self._fetch_all_pages(
                    self.client.music.get_library_tracks, limit=None, search=None, favorite=True
                )
            except Exception as err:
                logger.debug("Could not load favorite tracks: %s", err)
                return
            self._favorite_keys = {k for f in favs for k in self._track_keys(f)}
            self._favorite_keys_at = time.monotonic()
        for tr in tracks:
            if not getattr(tr, "favorite", False) and self._track_keys(tr) & self._favorite_keys:
                try:
                    tr.favorite = True
                except Exception:
                    pass

    async def resolve_full_item(self, item: Any) -> Any:
        """Converts an ItemMapping into the full object if needed."""
        if not self.client or not isinstance(item, ItemMapping):
            return item
        try:
            return await self.client.music.get_item(item.media_type, item.item_id, item.provider)
        except Exception:
            return item

    async def fetch_fresh_item(self, item: Any) -> Any:
        """Always asks MA for the item's current version (favorite, metadata, etc.)."""
        if not self.client or item is None:
            return item
        mt = getattr(item, "media_type", None)
        iid = getattr(item, "item_id", None)
        prov = getattr(item, "provider", None)
        if mt is None or iid is None or not prov:
            return item
        try:
            return await self.client.music.get_item(mt, iid, prov)
        except Exception:
            return await self.resolve_full_item(item)

    async def refresh_item(self, item: Any) -> Any:
        """Forces Music Assistant to re-fetch the item from its provider and reloads its tracks."""
        if not self.client or item is None:
            return None
        self._favorite_keys_at = 0.0
        mt = getattr(item, "media_type", None)
        if mt == MediaType.PLAYLIST:
            self._force_next_playlist_refresh = True
        fresh: Any = None
        try:
            fresh = await self.client.music.refresh_item(item)
        except Exception as err:
            logger.debug("music/refresh_item failed for %s: %s", getattr(item, "uri", item), err)
        if fresh is None:
            fresh = item
            iid = getattr(item, "item_id", None)
            prov = getattr(item, "provider", None)
            if mt is not None and iid is not None and prov:
                try:
                    fresh = await self.client.music.get_item(mt, iid, prov)
                except Exception as err:
                    logger.debug("get_item failed for %s: %s", getattr(item, "uri", item), err)
        if fresh is not item:
            self._index_single_library_item(fresh)
        self._emit_item_refreshed(fresh)
        return fresh

    async def get_album_tracks(
        self, album: Album | ItemMapping, source_track: Any = None
    ) -> tuple[Any, list[Track]]:
        if not self.client:
            return album, []
        full = await self.fetch_fresh_item(album)
        tracks: list[Track] = []
        try:
            tracks = list(await self.client.music.get_album_tracks(full.item_id, full.provider))
        except Exception:
            tracks = []

        # Playlist tracks from providers like Apple Music carry a stub ItemMapping whose
        # `item_id` is the album title instead of a real ID, so get_album_tracks returns [].
        # Resolve the real album via the full track object first, then by library/provider search.
        if not tracks and source_track is not None:
            tr_mt = getattr(source_track, "media_type", None)
            tr_id = getattr(source_track, "item_id", None)
            tr_prov = getattr(source_track, "provider", None)
            if tr_mt is not None and tr_id is not None and tr_prov:
                try:
                    full_tr = await self.client.music.get_item(tr_mt, tr_id, tr_prov)
                    real_alb = getattr(full_tr, "album", None)
                    if real_alb is not None and getattr(real_alb, "item_id", None):
                        cand = await self.fetch_fresh_item(real_alb)
                        cand_tracks = list(
                            await self.client.music.get_album_tracks(cand.item_id, cand.provider)
                        )
                        if cand_tracks:
                            full, tracks = cand, cand_tracks
                except Exception:
                    pass

        if not tracks:
            alb_name = (getattr(full, "name", "") or "").strip()
            if alb_name:
                artist_hint = ""
                if source_track is not None:
                    artist_hint = format_artists(source_track).casefold()
                if not artist_hint:
                    artist_hint = format_artists(full).casefold()
                try:
                    candidates: list[Any] = list(
                        await self.client.music.get_library_albums(search=alb_name)
                    )
                    if not candidates:
                        s_res = await self.client.music.search(
                            alb_name, media_types=[MediaType.ALBUM]
                        )
                        candidates = list(getattr(s_res, "albums", None) or [])
                    exact = [
                        c
                        for c in candidates
                        if (getattr(c, "name", "") or "").casefold() == alb_name.casefold()
                    ] or candidates
                    if artist_hint:
                        by_artist = [
                            c for c in exact if artist_hint in format_artists(c).casefold()
                        ]
                        if by_artist:
                            exact = by_artist
                    if exact:
                        cand = await self.fetch_fresh_item(exact[0])
                        cand_tracks = list(
                            await self.client.music.get_album_tracks(cand.item_id, cand.provider)
                        )
                        if cand_tracks:
                            full, tracks = cand, cand_tracks
                except Exception:
                    pass

        await self.mark_favorite_tracks(tracks)
        return full, tracks

    async def get_playlist_tracks(self, playlist: Playlist | ItemMapping) -> tuple[Any, list[Any]]:
        if not self.client:
            return playlist, []
        force = self._force_next_playlist_refresh
        self._force_next_playlist_refresh = False
        full = await self.fetch_fresh_item(playlist)
        tracks = await self.client.music.get_playlist_tracks(
            full.item_id, full.provider, force_refresh=force
        )
        tracks = list(tracks)
        if self.favorites_playlist_domain(full):
            # Everything in a service's own favorites list is a favorite (display only)
            for tr in tracks:
                try:
                    tr.favorite = True
                except Exception:
                    pass
        else:
            await self.mark_favorite_tracks(tracks)
        return full, tracks

    async def get_artist_detail(self, artist: Artist | ItemMapping) -> ArtistDetail:
        if not self.client:
            raise RuntimeError("Not connected")
        full = await self.fetch_fresh_item(artist)
        if isinstance(full, ItemMapping) or not getattr(full, "provider_mappings", None):
            art_name = (getattr(full, "name", "") or "").strip()
            if art_name:
                try:
                    lib_hits = await self.client.music.get_library_artists(search=art_name)
                    exact = next(
                        (x for x in lib_hits if (getattr(x, "name", "") or "").casefold() == art_name.casefold()),
                        None,
                    )
                    if exact is not None:
                        full = exact
                    else:
                        s_res = await self.client.music.search(art_name, media_types=[MediaType.ARTIST])
                        s_hits = list(getattr(s_res, "artists", None) or [])
                        exact_s = next(
                            (x for x in s_hits if (getattr(x, "name", "") or "").casefold() == art_name.casefold()),
                            s_hits[0] if s_hits else None,
                        )
                        if exact_s is not None:
                            full = await self.resolve_full_item(exact_s)
                except Exception:
                    pass

        album_calls = [
            self.client.music.get_artist_albums(
                full.item_id, full.provider, in_library_only=False
            )
        ]
        track_calls = [
            self.client.music.get_artist_tracks(
                full.item_id, full.provider, in_library_only=False
            )
        ]
        for pm in getattr(full, "provider_mappings", None) or ():
            p_inst = getattr(pm, "provider_instance", None) or getattr(pm, "provider_domain", None)
            p_id = getattr(pm, "item_id", None)
            if p_inst and p_id and getattr(pm, "provider_domain", "") != "library":
                album_calls.append(
                    self.client.music.get_artist_albums(p_id, p_inst, in_library_only=False)
                )
                track_calls.append(
                    self.client.music.get_artist_tracks(p_id, p_inst, in_library_only=False)
                )

        all_res = await asyncio.gather(*album_calls, *track_calls, return_exceptions=True)
        n_alb = len(album_calls)
        album_lists = [list(r) for r in all_res[:n_alb] if isinstance(r, list)]
        track_lists = [list(r) for r in all_res[n_alb:] if isinstance(r, list)]

        # Merge albums/singles, preferring the library version and avoiding duplicates
        merged_albums: list[Album] = []
        seen_mappings: set[tuple[str, str]] = set()
        seen_names: set[tuple[str, Any]] = set()

        # Library items first (indexing them so the UI can highlight them)
        first_batch = album_lists[0] if album_lists else []
        lib_batch = [a for a in first_batch if getattr(a, "provider", "") == "library" or self.is_in_library(a)]
        if lib_batch:
            self.index_library_items(lib_batch)

        for batch in album_lists:
            for alb in batch:
                is_lib = getattr(alb, "provider", "") == "library" or self.is_in_library(alb)
                pms = {
                    (str(getattr(pm, "provider_domain", "")), str(getattr(pm, "item_id", "")))
                    for pm in (getattr(alb, "provider_mappings", None) or ())
                    if getattr(pm, "item_id", None)
                }
                if getattr(alb, "provider", None) and getattr(alb, "item_id", None):
                    pms.add((str(alb.provider), str(alb.item_id)))
                name_key = (
                    (getattr(alb, "name", "") or "").casefold().strip(),
                    getattr(alb, "album_type", None),
                )
                if not is_lib and (pms & seen_mappings or name_key in seen_names):
                    continue
                if name_key in seen_names and is_lib:
                    # Already added as a library item
                    continue
                merged_albums.append(alb)
                seen_mappings.update(pms)
                seen_names.add(name_key)

        merged_tracks: list[Track] = []
        seen_track_names: set[str] = set()
        for batch in track_lists:
            for tr in batch:
                tk = (getattr(tr, "name", "") or "").casefold().strip()
                if tk and tk in seen_track_names:
                    continue
                if tk:
                    seen_track_names.add(tk)
                merged_tracks.append(tr)

        await self.mark_favorite_tracks(merged_tracks)
        return ArtistDetail(artist=full, top_tracks=merged_tracks, albums=merged_albums)
