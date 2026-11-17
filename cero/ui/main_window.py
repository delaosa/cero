"""Main window (Header + Library + Central Explorer + Queue + Bottom Bar)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango
from music_assistant_models.enums import AlbumType, MediaType, PlaybackState, QueueOption
from music_assistant_models.media_items import SearchResults

from ..artwork import ArtworkLoader
from ..icons import make_icon_image, set_button_svg
from ..ma_service import ArtistDetail, HomeContent, MAService, NowPlayingState, format_artists
from ..sendspin_player import LocalSendspinService
from ..session import extract_item_release_date
from .login_view import LoginView
from .player_bar import PlayerBar
from .queue_panel import QueuePanel
from .widgets import (
    LibraryRow,
    MediaCard,
    QuickPickCard,
    TrackRow,
    attach_media_context_menu,
    is_video_album,
    make_cover_frame,
    make_library_toggle_button,
    make_section_header,
    make_track_table_header,
    media_kind,
    media_subtitle,
    set_current_view_provider,
)

EXPLORER_SECTIONS: tuple[tuple[str, str], ...] = (
    ("discover", "Discover"),
    ("albums", "Albums"),
    ("playlists", "Playlists"),
    ("artists", "Artists"),
)

# Panel width limits (px). Saved widths are preferences, applied only as far as space allows.
LIB_MIN_W, LIB_MAX_W = 210, 520
LIB_EXPANDED_MIN_W = 260
LIB_COLLAPSED_W = 72
LIB_SNAP_W = 160
QUEUE_MIN_W, QUEUE_MAX_W = 240, 520
CENTER_MIN_W = 340
CENTER_TWO_PANE_MIN_W = 620
CENTER_THREE_PANE_MIN_W = 420


def fit_pane_sizes(
    total: int,
    handle: int,
    lib_pref: int,
    queue_pref: int,
    queue_open: bool,
    lib_min: int = LIB_MIN_W,
    queue_min: int = QUEUE_MIN_W,
) -> tuple[int, int]:
    """Library and queue widths that fit in `total` px keeping the center at its minimum.

    If the preferred widths do not fit, the queue shrinks first, then the library,
    each down to its own minimum (the real minimum of its content may exceed the
    default). Returns (library_width, queue_width); queue is 0 if closed.
    """
    lib_min = max(LIB_MIN_W, lib_min)
    queue_min = max(QUEUE_MIN_W, queue_min)
    lib = max(lib_min, min(LIB_MAX_W, lib_pref))
    queue = max(queue_min, min(QUEUE_MAX_W, queue_pref)) if queue_open else 0
    handles = handle * (2 if queue_open else 1)
    excess = lib + queue + CENTER_MIN_W + handles - total
    if excess > 0 and queue_open:
        cut = min(excess, queue - queue_min)
        queue -= cut
        excess -= cut
    if excess > 0:
        lib -= min(excess, lib - lib_min)
    return lib, queue


_TRACK_SORT_MODES = (
    ("custom", "Custom order"),
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("duration", "Duration"),
)


def _sort_tracks(tracks: list[Any], mode: str, descending: bool) -> list[Any]:
    """Tracks in the chosen order; "custom" keeps the playlist's own order (reversed if descending)."""

    def _key(tr: Any) -> Any:
        if mode == "title":
            return (getattr(tr, "name", "") or "").casefold()
        if mode == "artist":
            return (format_artists(tr) or "").casefold()
        if mode == "album":
            return (getattr(getattr(tr, "album", None), "name", "") or "").casefold()
        return getattr(tr, "duration", 0) or 0

    ordered = list(tracks) if mode == "custom" else sorted(tracks, key=_key)
    return ordered[::-1] if descending else ordered


def _sort_label_for_category(mode: str, category: str) -> str:
    if mode == "year":
        return "Release date"
    labels = {
        "alpha": "Name",
        "recent": "Recent",
        "creator": "Artist" if category in ("albums", "artists") else "Creator",
    }
    return labels.get(mode, "Sort")


def _sort_options_for_category(category: str) -> list[tuple[str, str]]:
    creator_title = (
        "Artist" if category in ("albums", "artists") else "Creator / Author"
    )
    options: list[tuple[str, str]] = []
    if category != "playlists":
        options.append(("year", "Release date"))
    options.extend(
        [
            ("alpha", "Name (A–Z / Z–A)"),
            ("recent", "Recently added / updated"),
            ("creator", creator_title),
        ]
    )
    return options


@dataclass
class _LibEntry:
    key: str
    title: str
    subtitle: str
    creator: str
    timestamp: float
    year: int
    image_url: str
    kind: str
    on_click: Callable[[], None]
    on_play: Callable[[], None] | None = None
    raw_item: Any = None


def _greeting_by_hour() -> str:
    h = datetime.now().hour
    if 5 <= h < 13:
        return "Good morning"
    if 13 <= h < 20:
        return "Good afternoon"
    return "Good evening"


def _item_unique_key(item: Any) -> str:
    uri = getattr(item, "uri", None)
    if uri:
        return str(uri)
    prov = getattr(item, "provider", "library")
    iid = getattr(item, "item_id", "")
    return f"{prov}:{iid}"


def _item_timestamp(item: Any) -> float:
    dt = getattr(item, "date_added", None) or getattr(item, "last_played", None)
    if dt is not None:
        if isinstance(dt, (int, float)):
            return float(dt)
        if hasattr(dt, "timestamp"):
            try:
                return float(dt.timestamp())
            except Exception:
                pass
    if (rank := getattr(item, "_ma_recent_rank", None)) is not None:
        return float(rank)
    return 0.0


logger = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow):
    """Native GTK4/Libadwaita main window."""

    def __init__(
        self,
        app: Adw.Application,
        ma_service: MAService,
        sendspin_service: LocalSendspinService,
        artwork: ArtworkLoader,
    ) -> None:
        super().__init__(application=app, title="CERO")
        self.ma_service = ma_service
        self.sendspin_service = sendspin_service
        self.artwork = artwork

        self.set_default_size(1280, 800)
        self.set_size_request(560, 520)

        sess = self.ma_service.session_store.data
        self._history_idx: int = -1
        self._history: list[Callable[[], None]] = []
        self._scroll_by_route: dict[int, float] = {}
        self._scroll_restore_token: int = 0
        self._navigating_history = False
        self._search_timer_id: int = 0
        self._active_track_rows: list[TrackRow] = []
        self._current_playing_uri: str = ""
        self._current_is_playing: bool = False
        self._lib_category: str = (
            sess.library_active_tab
            if sess.library_active_tab in ("playlists", "albums", "artists")
            else "albums"
        )
        self._explorer_section: str = (
            sess.explorer_active_section
            if sess.explorer_active_section
            in ("discover", "albums", "playlists", "artists")
            else "discover"
        )
        self._explorer_filter_query: str = ""

        self._home_cache: HomeContent = HomeContent()
        self._raw_library_items: list[Any] = []
        self._lib_filter_query: str = ""
        self._lib_render_gen: int = 0
        self._lib_pending_entries: list[_LibEntry] = []
        self._lib_rendered_count: int = 0
        self._lib_collapsed: bool = False
        self._lib_expanded_min_w: int = LIB_MIN_W
        self._force_lib_expand: bool = False
        self._last_paned_total: int = 0
        self._explorer_render_gen: int = 0
        self._exp_pending_items: list[Any] = []
        self._exp_rendered_count: int = 0
        self._exp_flow: Gtk.FlowBox | None = None
        self._home_rendered_sig: tuple[int, int, int, int] | None = None

        self._window_overlay = Gtk.Overlay()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("cero-root")
        self._window_overlay.set_child(root)

        self._art_lightbox = self._build_artwork_lightbox()
        self._window_overlay.add_overlay(self._art_lightbox)
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._window_overlay)
        self.set_content(self._toast_overlay)

        # Unified top bar (draggable + native window controls)
        root.append(self._build_topbar())

        # Root stack: Login vs full Player
        self._root_stack = Gtk.Stack()
        self._root_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._root_stack.set_transition_duration(180)
        self._root_stack.set_vexpand(True)
        self._root_stack.set_hexpand(True)

        self._login_view = LoginView(self.ma_service, on_connected=self._on_authenticated)
        self._root_stack.add_named(self._login_view, "login")

        self._workspace_box = self._build_workspace()
        self._root_stack.add_named(self._workspace_box, "player")

        root.append(self._root_stack)

        # Global keyboard shortcuts (mouse side buttons Back=8 / Forward=9 are handled in _on_window_event)
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

        self.ma_service.on_connection_changed(self._on_connection_changed)
        self.ma_service.on_state_changed(self._on_now_playing_changed)
        self.ma_service.on_library_changed(self._on_library_changed_refresh)
        self.ma_service.on_item_refreshed(self._on_item_refreshed)
        set_current_view_provider(lambda: self._current_view_item)
        self.ma_service.on_media_item_updated(self._on_media_item_updated)
        self._current_view_item: Any = None
        self._item_refresh_timer: int = 0

        # Start the saved session automatically or show the login screen
        if sess.has_credentials:
            self._status_lbl.set_label("Connecting…")
            self._root_stack.set_visible_child_name("player")
            # Show the library from the disk cache immediately while connecting
            if cached_home := self.ma_service.load_cached_home_content():
                self._home_cache = cached_home
                cat_items = getattr(cached_home, self._lib_category, [])
                if cat_items:
                    self._render_library_sidebar(cat_items)
                self._render_home_content(cached_home)
            self.ma_service.runner.submit(
                self.ma_service.connect_saved_session(),
                on_success=lambda _r: self._on_authenticated(),
                on_error=self._on_initial_connect_failed,
            )
        else:
            self._set_topbar_controls_visible(False)
            self._root_stack.set_visible_child_name("login")

    # ------------------------------------------------------------------
    # Building the Header and the 3-panel Workspace
    # ------------------------------------------------------------------

    def _build_topbar(self) -> Gtk.WindowHandle:
        handle = Gtk.WindowHandle()
        bar = Gtk.CenterBox()
        bar.add_css_class("cero-topbar")

        # Left: Back / Forward
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        left.set_valign(Gtk.Align.CENTER)

        self._back_btn = Gtk.Button()
        self._back_btn.add_css_class("nav-circle-btn")
        set_button_svg(self._back_btn, "back", size=17, color="#FFFFFF")
        self._back_btn.set_tooltip_text("Back (Alt+Left)")
        self._back_btn.set_sensitive(False)
        self._back_btn.connect("clicked", lambda _b: self._go_back())
        left.append(self._back_btn)

        self._fwd_btn = Gtk.Button()
        self._fwd_btn.add_css_class("nav-circle-btn")
        set_button_svg(self._fwd_btn, "forward", size=17, color="#FFFFFF")
        self._fwd_btn.set_tooltip_text("Forward (Alt+Right)")
        self._fwd_btn.set_sensitive(False)
        self._fwd_btn.connect("clicked", lambda _b: self._go_forward())
        left.append(self._fwd_btn)

        bar.set_start_widget(left)

        # Center: Home button + rounded Search bar (clamped so it grows on wide windows, shrinks on narrow)
        center = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        center.set_valign(Gtk.Align.CENTER)
        center.set_hexpand(True)
        center.set_margin_start(12)
        center.set_margin_end(12)

        self._home_btn = Gtk.Button()
        self._home_btn.add_css_class("nav-circle-btn")
        self._home_btn.set_size_request(48, 48)
        self._home_btn.set_valign(Gtk.Align.CENTER)
        self._home_btn.set_halign(Gtk.Align.CENTER)
        set_button_svg(self._home_btn, "home", size=22, color="#FFFFFF")
        self._home_btn.set_tooltip_text("Home / Library")
        self._home_btn.connect("clicked", lambda _b: self.navigate_home())
        center.append(self._home_btn)

        search_pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        search_pill.add_css_class("search-pill")
        search_pill.set_size_request(180, 40)
        search_pill.set_hexpand(True)
        search_pill.append(make_icon_image("search", size=18, color="#B3B3B3"))

        self._search_entry = Gtk.Entry()
        self._search_entry.set_hexpand(True)
        self._search_entry.set_width_chars(1)
        self._search_entry.connect("changed", self._on_search_changed)
        self._search_entry.connect("activate", self._on_search_activate)
        search_pill.append(self._search_entry)

        self._clear_search_btn = Gtk.Button()
        self._clear_search_btn.add_css_class("icon-flat-btn")
        set_button_svg(self._clear_search_btn, "close", size=15, color="#B3B3B3")
        self._clear_search_btn.set_visible(False)
        self._clear_search_btn.connect("clicked", lambda _b: self._search_entry.set_text(""))
        search_pill.append(self._clear_search_btn)

        center.append(search_pill)
        top_clamp = Adw.Clamp()
        top_clamp.set_maximum_size(420)
        top_clamp.set_hexpand(True)
        top_clamp.set_child(center)
        self._topbar_center = top_clamp
        bar.set_center_widget(top_clamp)

        # Right: Status + Log out + native Linux window controls
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        right.set_valign(Gtk.Align.CENTER)

        self._pairing_lbl = Gtk.Label(label="")
        self._pairing_lbl.add_css_class("connect-banner")
        self._pairing_lbl.set_visible(False)
        right.append(self._pairing_lbl)

        self._status_pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._status_pill.add_css_class("status-pill")
        self._status_pill.set_valign(Gtk.Align.CENTER)
        self._status_pill.set_margin_start(16)
        self._status_dot = Gtk.Box()
        self._status_dot.add_css_class("status-dot")
        self._status_dot.set_valign(Gtk.Align.CENTER)
        self._status_pill.append(self._status_dot)

        self._status_lbl = Gtk.Label(label="")
        self._status_lbl.add_css_class("status-pill-label")
        self._status_lbl.set_valign(Gtk.Align.CENTER)
        self._status_pill.append(self._status_lbl)
        right.append(self._status_pill)

        self._logout_btn = Gtk.Button()
        self._logout_btn.add_css_class("nav-circle-btn")
        self._logout_btn.set_valign(Gtk.Align.CENTER)
        set_button_svg(self._logout_btn, "logout", size=16, color="#B3B3B3")
        self._logout_btn.set_tooltip_text("Switch server / Log out")
        self._logout_btn.connect("clicked", self._on_logout_clicked)
        right.append(self._logout_btn)

        win_controls = Gtk.WindowControls(side=Gtk.PackType.END)
        win_controls.set_valign(Gtk.Align.CENTER)
        right.append(win_controls)

        bar.set_end_widget(right)
        handle.set_child(bar)
        return handle

    def _build_workspace(self) -> Gtk.Box:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_vexpand(True)
        outer.set_hexpand(True)

        sess = self.ma_service.session_store.data
        self._pane_save_timer_id: int = 0
        self._pane_apply_id: int = 0
        self._applying_panes: bool = False
        self._pending_nav: Callable[[], None] | None = None  # side-button navigation, run on release
        self._dragging_divider: str | None = None  # "library" / "queue" while the user drags it

        # Adaptive horizontal dividers: at least the Left (Library) and Central (Explorer) panels are always visible
        self._outer_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._outer_paned.add_css_class("panels-paned")
        self._outer_paned.set_wide_handle(True)
        self._outer_paned.set_margin_start(8)
        self._outer_paned.set_margin_end(8)
        self._outer_paned.set_margin_top(4)
        self._outer_paned.set_margin_bottom(8)
        self._outer_paned.set_vexpand(True)
        self._outer_paned.set_hexpand(True)
        self._outer_paned.set_resize_start_child(False)
        self._outer_paned.set_resize_end_child(True)
        self._outer_paned.set_shrink_start_child(True)
        self._outer_paned.set_shrink_end_child(False)

        # 1. Left panel: Library (collapsible to covers-only, adjustable when expanded)
        lib_sidebar = self._build_library_sidebar()
        lib_sidebar.set_size_request(LIB_COLLAPSED_W, -1)
        lib_sidebar.set_hexpand(False)
        self._lib_sidebar = lib_sidebar
        self._outer_paned.set_start_child(lib_sidebar)
        self._outer_paned.set_position(
            LIB_COLLAPSED_W if sess.library_collapsed else sess.library_width
        )

        # Right container (Center + collapsible Queue)
        self._right_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._right_paned.add_css_class("panels-paned")
        self._right_paned.set_wide_handle(True)
        self._right_paned.set_hexpand(True)
        self._right_paned.set_vexpand(True)
        self._right_paned.set_resize_start_child(True)
        self._right_paned.set_resize_end_child(False)
        self._right_paned.set_shrink_start_child(False)
        self._right_paned.set_shrink_end_child(False)

        # 2. Center panel: Explorer (340px minimum, fills all free space)
        center_island = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        center_island.add_css_class("cero-center-island")
        center_island.set_size_request(CENTER_MIN_W, -1)
        center_island.set_hexpand(True)
        center_island.set_vexpand(True)
        center_island.set_halign(Gtk.Align.FILL)
        center_island.set_overflow(Gtk.Overflow.HIDDEN)

        self._center_scroll = Gtk.ScrolledWindow()
        self._center_scroll.set_vexpand(True)
        self._center_scroll.set_hexpand(True)
        self._center_scroll.set_halign(Gtk.Align.FILL)
        self._center_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._center_scroll.set_propagate_natural_width(False)
        if vadj := self._center_scroll.get_vadjustment():
            vadj.connect("value-changed", self._on_center_scroll_changed)

        self._center_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._center_body.set_hexpand(True)
        self._center_body.set_halign(Gtk.Align.FILL)
        self._center_body.set_margin_top(20)
        self._center_body.set_margin_bottom(28)
        self._center_body.set_margin_start(24)
        self._center_body.set_margin_end(24)
        self._center_scroll.set_child(self._center_body)
        center_island.append(self._center_scroll)
        self._right_paned.set_start_child(center_island)

        # 3. Right panel: Play queue (collapsible; remembers its last state)
        q_vis = bool(self.ma_service.session_store.data.queue_visible)
        self._queue_revealer = Gtk.Revealer()
        self._queue_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self._queue_revealer.set_transition_duration(180)
        self._queue_revealer.set_reveal_child(q_vis)
        self._queue_revealer.set_hexpand(False)
        self._queue_revealer.set_visible(q_vis)
        self._queue_revealer.connect("notify::child-revealed", self._on_queue_child_revealed)

        self._queue_panel = QueuePanel(
            self.ma_service,
            self.artwork,
            on_close=self._close_queue_panel,
            on_open_item=self.open_media_item,
        )
        self._queue_panel.set_size_request(QUEUE_MIN_W, -1)
        self._queue_revealer.set_child(self._queue_panel)
        self._right_paned.set_end_child(self._queue_revealer)

        self._outer_paned.set_end_child(self._right_paned)
        self._outer_paned.connect("notify::position", self._on_outer_paned_moved)
        self._right_paned.connect("notify::position", self._on_right_paned_moved)
        # max-position changes whenever the available width changes (start-up, resize, maximize)
        self._outer_paned.connect("notify::max-position", self._schedule_apply_panes)
        self._watch_divider_drags(self._outer_paned, "library")
        self._watch_divider_drags(self._right_paned, "queue")
        release_watch = Gtk.EventControllerLegacy()
        release_watch.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        release_watch.connect("event", self._on_window_event)
        self.add_controller(release_watch)

        outer.append(self._outer_paned)

        # Compact bottom player bar
        self._player_bar = PlayerBar(
            self.ma_service,
            self.artwork,
            on_toggle_queue=self._toggle_queue_panel,
            on_open_item=self.open_media_item,
        )
        self._player_bar.set_queue_active_visual(q_vis)
        outer.append(self._player_bar)
        return outer

    def _build_artwork_lightbox(self) -> Gtk.Box:
        self._lightbox_url = ""

        backdrop = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        backdrop.add_css_class("artwork-lightbox")
        backdrop.set_hexpand(True)
        backdrop.set_vexpand(True)
        backdrop.set_halign(Gtk.Align.FILL)
        backdrop.set_valign(Gtk.Align.FILL)
        backdrop.set_visible(False)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("artwork-lightbox-card")
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.CENTER)
        card.set_hexpand(True)
        card.set_vexpand(True)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top_row.append(spacer)
        close_btn = Gtk.Button()
        close_btn.add_css_class("icon-flat-btn")
        set_button_svg(close_btn, "close", size=18, color="#B3B3B3")
        close_btn.set_tooltip_text("Close (Esc)")
        close_btn.connect("clicked", lambda _b: self._close_artwork_lightbox())
        top_row.append(close_btn)
        card.append(top_row)

        self._lightbox_frame = Gtk.ScrolledWindow()
        self._lightbox_frame.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.EXTERNAL)
        self._lightbox_frame.set_propagate_natural_width(False)
        self._lightbox_frame.set_propagate_natural_height(False)
        self._lightbox_frame.set_halign(Gtk.Align.CENTER)
        self._lightbox_frame.set_valign(Gtk.Align.CENTER)
        self._lightbox_frame.set_overflow(Gtk.Overflow.HIDDEN)
        self._lightbox_frame.add_css_class("card-cover")
        self._lightbox_frame.set_tooltip_text("Right-click to copy image")

        self._lightbox_pic = Gtk.Picture()
        self._lightbox_pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._lightbox_pic.set_can_shrink(True)
        self._lightbox_frame.set_child(self._lightbox_pic)
        self._set_lightbox_box_size(420, 420)

        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        rclick.connect("pressed", lambda g, *_a: (g.set_state(Gtk.EventSequenceState.CLAIMED), self._copy_lightbox_image_to_clipboard()))
        self._lightbox_frame.add_controller(rclick)

        card.append(self._lightbox_frame)

        self._lightbox_title = Gtk.Label(label="", xalign=0.5)
        self._lightbox_title.add_css_class("section-heading")
        self._lightbox_title.set_ellipsize(Pango.EllipsizeMode.END)
        self._lightbox_title.set_max_width_chars(36)
        card.append(self._lightbox_title)

        self._lightbox_sub = Gtk.Label(label="", xalign=0.5)
        self._lightbox_sub.add_css_class("subtitle-dim")
        self._lightbox_sub.set_ellipsize(Pango.EllipsizeMode.END)
        self._lightbox_sub.set_max_width_chars(40)
        card.append(self._lightbox_sub)

        backdrop.append(card)

        # Close when clicking the dark backdrop outside the card
        bg_click = Gtk.GestureClick()
        bg_click.set_button(1)

        def _on_bg_released(_g: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
            al = card.get_allocation()
            if not (al.width > 0 and al.x <= x <= al.x + al.width and al.y <= y <= al.y + al.height):
                self._close_artwork_lightbox()

        bg_click.connect("released", _on_bg_released)
        backdrop.add_controller(bg_click)
        return backdrop

    def _set_lightbox_box_size(self, width: int, height: int) -> None:
        self._lightbox_frame.set_min_content_width(-1)
        self._lightbox_frame.set_min_content_height(-1)
        self._lightbox_frame.set_max_content_width(width)
        self._lightbox_frame.set_max_content_height(height)
        self._lightbox_frame.set_min_content_width(width)
        self._lightbox_frame.set_min_content_height(height)
        self._lightbox_frame.set_size_request(width, height)
        self._lightbox_pic.set_size_request(width, height)

    def _apply_full_lightbox_texture(self, tex: Gdk.Texture) -> None:
        orig_w = max(1, tex.get_width())
        orig_h = max(1, tex.get_height())
        win_w = self.get_width() or 1280
        win_h = self.get_height() or 800
        max_w = max(420, win_w - 110)
        max_h = max(420, win_h - 175)
        scale = min(1.0, max_w / orig_w, max_h / orig_h)
        disp_w = max(1, int(round(orig_w * scale)))
        disp_h = max(1, int(round(orig_h * scale)))
        self._set_lightbox_box_size(disp_w, disp_h)
        self._lightbox_pic.set_paintable(tex)

    def _open_artwork_lightbox(self, url: str, title: str, subtitle: str = "") -> None:
        """Shows the cover art full size (the thumbnail first, then the original resolution)."""
        title = title or "CERO"
        self._lightbox_url = url
        self._lightbox_title.set_label(title)
        self._lightbox_sub.set_label(subtitle)
        self._lightbox_sub_text = subtitle

        # Immediately show the fitted thumbnail or placeholder and load the original resolution
        win_w = self.get_width() or 1280
        win_h = self.get_height() or 800
        init_sz = min(560, max(420, min(win_w - 110, win_h - 175)))
        self._set_lightbox_box_size(init_sz, init_sz)
        self.artwork.load_into_picture(self._lightbox_pic, url, title=title, size=init_sz)
        self._art_lightbox.set_visible(True)

        if url:
            def _on_raw(data: bytes | None) -> None:
                if not data or url != self._lightbox_url or not self._art_lightbox.get_visible():
                    return
                try:
                    tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                except Exception:
                    return
                self._apply_full_lightbox_texture(tex)

            self.ma_service.runner.submit(
                self.artwork.fetch_full_resolution_bytes(url),
                on_success=_on_raw,
            )

    def _make_cover_expandable(
        self, cover_frame: Gtk.Widget, url: str, title: str, subtitle: str = ""
    ) -> None:
        """Left-click on a detail view cover shows it full size (right-click keeps its menu)."""
        if not url:
            return  # no real image (e.g. Liked Songs placeholder): nothing to enlarge
        cover_frame.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        cover_frame.set_tooltip_text("Expand cover art")
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect(
            "released", lambda _g, _n, _x, _y: self._open_artwork_lightbox(url, title, subtitle)
        )
        cover_frame.add_controller(click)

    def _copy_lightbox_image_to_clipboard(self) -> bool:
        paintable = self._lightbox_pic.get_paintable()
        if not isinstance(paintable, Gdk.Texture):
            return False
        png_bytes = paintable.save_to_png_bytes()
        provider = Gdk.ContentProvider.new_for_bytes("image/png", png_bytes)
        clipboard = self.get_clipboard()
        clipboard.set_content(provider)
        saved_sub = getattr(self, "_lightbox_sub_text", "")
        self._lightbox_sub.set_label("Copied image to clipboard")
        GLib.timeout_add(
            1800,
            lambda: (
                self._lightbox_sub.set_label(saved_sub)
                if self._lightbox_sub.get_label() == "Copied image to clipboard"
                else None,
                GLib.SOURCE_REMOVE,
            )[1],
        )
        return True

    def _close_artwork_lightbox(self) -> None:
        self._art_lightbox.set_visible(False)

    def _pane_handle_width(self) -> int:
        """Width of a divider handle, measured from the current allocation."""
        h = self._outer_paned.get_width() - self._lib_sidebar.get_width() - self._right_paned.get_width()
        return h if 0 < h < 40 else 8

    def _queue_is_open(self) -> bool:
        return self._queue_revealer.get_reveal_child()

    def _schedule_apply_panes(self, *_args: object) -> None:
        if not self._pane_apply_id:
            # On the next frame, once GTK has laid out the new size. A frame callback runs
            # promptly even while start-up work keeps the main loop busy (an idle callback
            # could wait seconds)
            self._pane_apply_id = self.add_tick_callback(self._apply_pane_sizes)

    def _fit_current_panes(self) -> tuple[int, int, int, bool]:
        """(library, queue, handle, collapse_library) that fit the current window."""
        sess = self.ma_service.session_store.data
        handle = self._pane_handle_width()
        total = self._outer_paned.get_width()
        if self._last_paned_total and total < self._last_paned_total - 8:
            self._force_lib_expand = False
        if total > 0:
            self._last_paned_total = total
        queue_open = self._queue_is_open()
        if not self._lib_collapsed:
            measured = self._lib_sidebar.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            if measured > LIB_COLLAPSED_W:
                self._lib_expanded_min_w = max(LIB_EXPANDED_MIN_W, measured)
        lib_min = self._lib_expanded_min_w
        queue_min = max(QUEUE_MIN_W, self._queue_panel.measure(Gtk.Orientation.HORIZONTAL, -1)[0])
        auto_collapse = total > 0 and (
            (
                queue_open
                and total < lib_min + queue_min + CENTER_THREE_PANE_MIN_W + handle * 2
            )
            or (
                not queue_open
                and total < lib_min + CENTER_TWO_PANE_MIN_W + handle
            )
        )
        if (sess.library_collapsed or auto_collapse) and not self._force_lib_expand:
            rem = max(0, total - LIB_COLLAPSED_W - CENTER_MIN_W - handle * (2 if queue_open else 1))
            queue = max(queue_min, min(QUEUE_MAX_W, min(sess.queue_width, rem))) if queue_open else 0
            return LIB_COLLAPSED_W, queue, handle, True
        if not auto_collapse:
            self._force_lib_expand = False
        lib, queue = fit_pane_sizes(
            total,
            handle,
            sess.library_width,
            sess.queue_width,
            queue_open,
            lib_min,
            queue_min,
        )
        return lib, queue, handle, False

    def _toggle_library_collapsed(self) -> None:
        sess = self.ma_service.session_store.data
        if self._lib_collapsed:
            sess.library_collapsed = False
            if sess.library_width < LIB_MIN_W:
                sess.library_width = 285
            self.ma_service.session_store.save()
            # If 3 panels still do not fit, closing the queue makes room to expand
            handle = self._pane_handle_width()
            total = self._outer_paned.get_width()
            queue_min = max(QUEUE_MIN_W, self._queue_panel.measure(Gtk.Orientation.HORIZONTAL, -1)[0])
            if (
                self._queue_is_open()
                and total > 0
                and total < self._lib_expanded_min_w + queue_min + CENTER_THREE_PANE_MIN_W + handle * 2
            ):
                self._force_lib_expand = True
                self._close_queue_panel()
                return
            self._force_lib_expand = True
        else:
            self._force_lib_expand = False
            sess.library_collapsed = True
            self.ma_service.session_store.save()
        self._schedule_apply_panes()

    def _set_library_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._lib_collapsed:
            return
        self._lib_collapsed = collapsed
        self._lib_toggle_lbl.set_visible(not collapsed)
        self._lib_hdr_spacer.set_visible(not collapsed)
        self._lib_toggle_btn.set_tooltip_text("Expand Library" if collapsed else "Collapse Library")
        self._sort_btn.set_visible(not collapsed)
        self._sort_dir_btn.set_visible(not collapsed)
        self._lib_chips_row.set_visible(not collapsed)
        self._lib_filter_box.set_visible(not collapsed)
        self._lib_hdr.set_halign(Gtk.Align.CENTER if collapsed else Gtk.Align.FILL)
        self._lib_hdr.set_margin_top(10 if collapsed else 12)
        self._lib_hdr.set_margin_start(0 if collapsed else 10)
        self._lib_hdr.set_margin_end(0 if collapsed else 12)
        self._lib_list_box.set_margin_start(4 if collapsed else 8)
        self._lib_list_box.set_margin_end(4 if collapsed else 8)
        if collapsed:
            self._lib_sidebar.add_css_class("collapsed")
        else:
            self._lib_sidebar.remove_css_class("collapsed")
        row = self._lib_list_box.get_first_child()
        while row is not None:
            if isinstance(row, LibraryRow):
                row.set_collapsed(collapsed)
            row = row.get_next_sibling()

    def _apply_pane_sizes(self, *_args: object) -> bool:
        """Apply the saved widths as far as the available space allows, without saving.

        Two steps: first the library divider; the queue divider is set once GTK has
        re-allocated the right container, otherwise GTK takes the difference from the
        queue instead of the center.
        """
        self._pane_apply_id = 0
        total = self._outer_paned.get_width()
        if total <= 0:
            return GLib.SOURCE_REMOVE
        lib, _queue, _handle, collapse_lib = self._fit_current_panes()
        self._set_library_collapsed(collapse_lib)
        self._applying_panes = True
        if self._outer_paned.get_position() == lib:
            # Library divider already in place (e.g. at start-up): the right container has its
            # final width, so the queue can be set in this same frame
            return self._apply_queue_size()
        self._outer_paned.set_position(lib)
        self.add_tick_callback(self._apply_queue_size)
        return GLib.SOURCE_REMOVE

    def _apply_queue_size(self, *_args: object) -> bool:
        if self._queue_is_open():
            _lib, queue, handle, _collapsed = self._fit_current_panes()
            right_w = self._right_paned.get_width()
            self._right_paned.set_position(max(CENTER_MIN_W, right_w - handle - queue))
        self.add_tick_callback(self._end_applying_panes)
        return GLib.SOURCE_REMOVE

    def _end_applying_panes(self, *_args: object) -> bool:
        self._applying_panes = False
        return GLib.SOURCE_REMOVE

    def _watch_divider_drags(self, paned: Gtk.Paned, name: str) -> None:
        """Records which divider the user starts dragging (observes only; GTK still does the drag).

        GTK's own divider gesture takes over the drag and cancels this one, so the mark is
        cleared on mouse button release instead (see _on_window_event).
        """
        watcher = Gtk.GestureDrag()
        watcher.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def _begin(_g: Gtk.GestureDrag, x: float, y: float) -> None:
            if self._divider_at(paned, x, y):
                self._dragging_divider = name

        watcher.connect("drag-begin", _begin)
        paned.add_controller(watcher)

    def _on_window_event(self, ctrl: Gtk.EventControllerLegacy, _event: object) -> bool:
        # PyGObject passes the event argument as None: read it from the controller instead
        event = ctrl.get_current_event()
        if event is None:
            return False
        etype = event.get_event_type()
        if etype == Gdk.EventType.BUTTON_PRESS:
            if self._root_stack.get_visible_child_name() == "player":
                btn = Gdk.ButtonEvent.get_button(event)
                if btn == 8:
                    self._pending_nav = self._go_back
                    return True
                if btn == 9:
                    self._pending_nav = self._go_forward
                    return True
        elif etype == Gdk.EventType.BUTTON_RELEASE:
            if self._pending_nav is not None:
                btn = Gdk.ButtonEvent.get_button(event)
                if btn in (8, 9):
                    action, self._pending_nav = self._pending_nav, None

                    def _run_nav() -> bool:
                        action()
                        # GTK4 on X11 can leave pointer_focus / implicit_grab stuck after
                        # side-button (8/9) events; toggling sensitivity clears the grab
                        # without disturbing layout or keyboard focus.
                        focus = self.get_focus()
                        self.set_sensitive(False)
                        self.set_sensitive(True)
                        if focus is not None and focus.get_mapped():
                            focus.grab_focus()
                        return GLib.SOURCE_REMOVE

                    GLib.idle_add(_run_nav)
                    return True
                self._pending_nav = None
            if self._dragging_divider:
                # Let GTK apply the last divider move first, then end the drag
                GLib.idle_add(lambda: (setattr(self, "_dragging_divider", None), GLib.SOURCE_REMOVE)[1])
        return False  # observe only

    @staticmethod
    def _divider_at(paned: Gtk.Paned, x: float, y: float) -> bool:
        """Whether (x, y), in paned coordinates, is on the paned's own divider handle."""
        child = paned.get_first_child()
        while child is not None:
            if child.get_css_name() == "separator":
                ok, b = child.compute_bounds(paned)
                return bool(ok) and b.get_x() - 2 <= x <= b.get_x() + b.get_width() + 2
            child = child.get_next_sibling()
        return False

    def _on_outer_paned_moved(self, paned: Gtk.Paned, _pspec: object) -> None:
        # Saved only while the user drags THIS divider; any other move is forced by the layout
        if self._applying_panes or self._dragging_divider != "library":
            return
        pos = paned.get_position()
        sess = self.ma_service.session_store.data
        if pos < LIB_SNAP_W:
            sess.library_collapsed = True
            self._set_library_collapsed(True)
            self._applying_panes = True
            paned.set_position(LIB_COLLAPSED_W)
            self.add_tick_callback(self._end_applying_panes)
            self._schedule_save_panes()
            return
        if self._lib_collapsed or sess.library_collapsed:
            sess.library_collapsed = False
            self._set_library_collapsed(False)
        clamped = max(LIB_MIN_W, min(LIB_MAX_W, pos))
        if pos < LIB_MIN_W:
            self._applying_panes = True
            paned.set_position(clamped)
            self.add_tick_callback(self._end_applying_panes)
        sess.library_width = clamped
        self._schedule_save_panes()

    def _on_right_paned_moved(self, paned: Gtk.Paned, _pspec: object) -> None:
        if self._applying_panes or self._dragging_divider != "queue" or not self._queue_is_open():
            return
        queue_w = paned.get_width() - paned.get_position() - self._pane_handle_width()
        self.ma_service.session_store.data.queue_width = max(QUEUE_MIN_W, min(QUEUE_MAX_W, queue_w))
        self._schedule_save_panes()

    def _schedule_save_panes(self) -> None:
        if self._pane_save_timer_id:
            GLib.source_remove(self._pane_save_timer_id)
        self._pane_save_timer_id = GLib.timeout_add(350, self._commit_save_panes)

    def _commit_save_panes(self) -> bool:
        self._pane_save_timer_id = 0
        self.ma_service.session_store.save()
        return GLib.SOURCE_REMOVE

    def _build_library_sidebar(self) -> Gtk.Box:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar.add_css_class("cero-island")
        sidebar.set_vexpand(True)

        # Header: clickable "Library" toggle (collapses/expands the rail) + sort controls
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hdr.set_margin_top(12)
        hdr.set_margin_start(10)
        hdr.set_margin_end(12)

        toggle_btn = Gtk.Button()
        toggle_btn.add_css_class("lib-collapse-btn")
        toggle_btn.set_tooltip_text("Collapse Library")
        toggle_btn.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        toggle_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toggle_inner.set_valign(Gtk.Align.CENTER)
        toggle_inner.append(make_icon_image("library", size=20, color="#B3B3B3"))
        lbl = Gtk.Label(label="Library", xalign=0.0)
        lbl.add_css_class("track-title")
        toggle_inner.append(lbl)
        toggle_btn.set_child(toggle_inner)
        toggle_btn.connect("clicked", lambda _b: self._toggle_library_collapsed())
        hdr.append(toggle_btn)

        hdr_spacer = Gtk.Box()
        hdr_spacer.set_hexpand(True)
        hdr.append(hdr_spacer)
        self._lib_hdr_spacer = hdr_spacer
        self._lib_hdr = hdr
        self._lib_toggle_btn = toggle_btn
        self._lib_toggle_lbl = lbl

        self._sort_btn = Gtk.MenuButton()
        self._sort_btn.add_css_class("sort-pill-btn")
        self._sort_popover = self._build_sort_popover(
            get_category=lambda: self._lib_category,
            on_changed=self._on_sort_settings_changed,
        )
        self._sort_btn.set_popover(self._sort_popover)
        self._sort_btn.set_tooltip_text("Sort items")
        hdr.append(self._sort_btn)

        self._sort_dir_btn = Gtk.Button()
        self._sort_dir_btn.add_css_class("sort-dir-btn")
        self._sort_dir_btn.connect(
            "clicked", lambda _b: self._toggle_sort_direction(self._lib_category)
        )
        hdr.append(self._sort_dir_btn)

        self._update_sort_btn_content()
        sidebar.append(hdr)

        # Compact category chips without horizontal scrolling (they all fit together)
        chips_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        chips_row.set_margin_start(10)
        chips_row.set_margin_end(10)
        chips_row.set_homogeneous(True)
        self._lib_chips_row = chips_row

        self._lib_chip_btns: dict[str, Gtk.Button] = {}
        for key, title in (
            ("albums", "Albums"),
            ("playlists", "Playlists"),
            ("artists", "Artists"),
        ):
            btn = Gtk.Button(label=title)
            btn.add_css_class("filter-chip-compact")
            if key == self._lib_category:
                btn.add_css_class("active")
            btn.connect("clicked", lambda _b, k=key: self._select_library_tab(k))
            chips_row.append(btn)
            self._lib_chip_btns[key] = btn

        sidebar.append(chips_row)

        # Quick filter in the Library panel + counter
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        filter_box.add_css_class("lib-filter-box")
        filter_box.set_margin_start(12)
        filter_box.set_margin_end(12)
        self._lib_filter_box = filter_box
        filter_box.append(make_icon_image("search", size=14, color="#B3B3B3"))
        self._lib_filter_entry = Gtk.Entry()
        self._lib_filter_entry.set_hexpand(True)
        self._lib_filter_entry.set_placeholder_text("Filter by title, artist or year…")
        self._lib_filter_entry.connect("changed", self._on_lib_filter_changed)
        filter_box.append(self._lib_filter_entry)
        filter_box.append(self._make_clear_button(self._lib_filter_entry))

        self._lib_count_lbl = Gtk.Label(label="")
        self._lib_count_lbl.add_css_class("muted-label")
        filter_box.append(self._lib_count_lbl)
        sidebar.append(filter_box)

        # Scrollable Library list
        self._lib_scroll = Gtk.ScrolledWindow()
        self._lib_scroll.set_vexpand(True)
        self._lib_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        if lvadj := self._lib_scroll.get_vadjustment():
            lvadj.connect("value-changed", self._on_lib_scroll_changed)

        self._lib_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._lib_list_box.set_margin_start(8)
        self._lib_list_box.set_margin_end(8)
        self._lib_list_box.set_margin_bottom(10)
        self._lib_scroll.set_child(self._lib_list_box)
        sidebar.append(self._lib_scroll)

        return sidebar

    # ------------------------------------------------------------------
    # Sort popover (Criterion + Ascending/Descending + Year)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_clear_button(entry: Gtk.Entry) -> Gtk.Button:
        """Small "x" next to a filter box that empties it; only visible while it has text."""
        btn = Gtk.Button()
        btn.add_css_class("icon-flat-btn")
        set_button_svg(btn, "close", size=14, color="#B3B3B3")
        btn.set_tooltip_text("Clear")
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_visible(bool(entry.get_text()))
        btn.connect("clicked", lambda _b: (entry.set_text(""), entry.grab_focus()))
        entry.connect("changed", lambda e: btn.set_visible(bool(e.get_text())))
        return btn

    def _build_sort_popover(
        self,
        *,
        get_category: Callable[[], str],
        on_changed: Callable[[str], None],
    ) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_size_request(230, -1)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        hdr = Gtk.Label(label="Sort by", xalign=0.0)
        hdr.add_css_class("muted-label")
        hdr.set_margin_start(6)
        box.append(hdr)

        options_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(options_box)

        def _populate(_p: Gtk.Popover) -> None:
            while (c := options_box.get_first_child()) is not None:
                options_box.remove(c)

            cat = get_category()
            cur_mode = self.ma_service.session_store.get_library_sort_mode(cat)

            for mode_key, label in _sort_options_for_category(cat):
                row_btn = Gtk.Button()
                row_btn.add_css_class("flat")
                row_btn.add_css_class("device-row")
                inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                lbl = Gtk.Label(label=label, xalign=0.0)
                lbl.set_hexpand(True)
                lbl.add_css_class("device-active-label" if mode_key == cur_mode else "track-title")
                inner.append(lbl)
                if mode_key == cur_mode:
                    inner.append(make_icon_image("check", size=15, color="#94A3B8"))
                row_btn.set_child(inner)

                def _choose(_b: Gtk.Button, mk: str = mode_key, c: str = cat) -> None:
                    pop.popdown()
                    self.ma_service.session_store.set_library_sort_mode(c, mk)
                    on_changed(c)

                row_btn.connect("clicked", _choose)
                options_box.append(row_btn)

        pop.connect("show", _populate)
        pop.set_child(box)
        return pop

    def _toggle_sort_direction(self, category: str) -> None:
        self.ma_service.session_store.toggle_library_sort_direction(category)
        self._on_sort_settings_changed(category)

    def _on_sort_settings_changed(self, category: str) -> None:
        self._update_sort_btn_content()
        if category == self._lib_category:
            self._refresh_library_list_ui()
        if self._explorer_section == category and hasattr(self, "_explorer_grid_host"):
            self._refresh_explorer_catalog_ui()

    def _update_sort_btn_content(self) -> None:
        store = self.ma_service.session_store
        cur_mode = store.get_library_sort_mode(self._lib_category)
        cur_dir = store.get_library_sort_direction(self._lib_category)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.append(make_icon_image("sort", size=13, color="#B3B3B3"))
        lbl = Gtk.Label(label=_sort_label_for_category(cur_mode, self._lib_category))
        lbl.add_css_class("muted-label")
        box.append(lbl)
        self._sort_btn.set_child(box)

        dir_icon = "arrow_up" if cur_dir == "asc" else "arrow_down"
        dir_text = (
            "Ascending (click to reverse)"
            if cur_dir == "asc"
            else "Descending (click to reverse)"
        )
        set_button_svg(self._sort_dir_btn, dir_icon, size=13, color="#94A3B8")
        self._sort_dir_btn.set_tooltip_text(dir_text)

    # ------------------------------------------------------------------
    # Session and State management
    # ------------------------------------------------------------------

    def show_pairing_code(self, code: str | None) -> None:
        if code:
            self._pairing_lbl.set_label(f"Sendspin code: {code}")
            self._pairing_lbl.set_visible(True)
        else:
            self._pairing_lbl.set_visible(False)

    def _set_topbar_controls_visible(self, visible: bool) -> None:
        self._topbar_center.set_visible(visible)
        self._back_btn.set_visible(visible)
        self._fwd_btn.set_visible(visible)
        self._logout_btn.set_visible(visible)

    def _on_authenticated(self) -> None:
        self._set_topbar_controls_visible(True)
        self._root_stack.set_visible_child_name("player")
        srv_url = self.ma_service.session_store.data.server_url
        self.ma_service.runner.submit(self.sendspin_service.start(srv_url))
        self.navigate_home()

    def _on_initial_connect_failed(self, exc: BaseException) -> None:
        self._set_topbar_controls_visible(False)
        self._root_stack.set_visible_child_name("login")
        self._login_view.show_error(f"Could not reconnect automatically: {exc}")

    def _on_connection_changed(self, connected: bool, message: str) -> None:
        self._status_pill.remove_css_class("online")
        self._status_pill.remove_css_class("offline")
        if connected:
            self._status_lbl.set_label("Connected")
            self._status_pill.add_css_class("online")
            self._status_pill.set_tooltip_text(message or "Connected to Music Assistant")
        else:
            self._status_lbl.set_label(message or "Disconnected")
            self._status_pill.add_css_class("offline")
            self._status_pill.set_tooltip_text(message or "Disconnected from Music Assistant")

    def _on_logout_clicked(self, _btn: Gtk.Button) -> None:
        self._show_logout_dialog()

    def _session_summary_rows(self) -> list[tuple[str, str]]:
        """Current session values, shown before logging out."""
        sess = self.ma_service.session_store.data
        client = self.ma_service.client
        info = getattr(client, "server_info", None) if client else None
        if sess.username:
            auth = f"User: {sess.username}"
        elif sess.token:
            auth = f"Token: …{sess.token[-6:]}" if len(sess.token) > 6 else "Token"
        else:
            auth = "None"
        active_name = ""
        if client and sess.selected_player_id:
            if p := client.players.get(sess.selected_player_id):
                active_name = p.name
        server_ver = ""
        if info is not None:
            server_ver = f"{info.server_version} (API schema {info.schema_version})"
        return [
            ("Server", sess.server_url or "—"),
            ("Server name", str(getattr(info, "name", "") or "") if info else ""),
            ("Version", server_ver),
            ("Status", "Connected" if client else "Disconnected"),
            ("Authentication", auth),
            ("This device", sess.local_player_name),
            ("Active player", active_name),
        ]

    def _show_logout_dialog(self) -> Adw.AlertDialog:
        dialog = Adw.AlertDialog(
            heading="Log out of Music Assistant?",
            body="You will need to sign in again to use CERO.",
        )
        grid = Gtk.Grid(column_spacing=16, row_spacing=6)
        grid.set_margin_top(4)
        row = 0
        for key, val in self._session_summary_rows():
            if not val:
                continue
            k_lbl = Gtk.Label(label=key, xalign=0.0)
            k_lbl.add_css_class("muted-label")
            k_lbl.set_valign(Gtk.Align.START)
            v_lbl = Gtk.Label(label=val, xalign=0.0)
            v_lbl.add_css_class("subtitle-dim")
            v_lbl.set_selectable(True)
            v_lbl.set_wrap(True)
            v_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            v_lbl.set_max_width_chars(36)
            grid.attach(k_lbl, 0, row, 1, 1)
            grid.attach(v_lbl, 1, row, 1, 1)
            row += 1
        dialog.set_extra_child(grid)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("logout", "Log Out")
        dialog.set_response_appearance("logout", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response", lambda _d, resp: self._do_logout() if resp == "logout" else None
        )
        dialog.present(self)
        return dialog

    def _do_logout(self) -> None:
        self.ma_service.session_store.clear_credentials()
        self.ma_service.runner.submit(self.ma_service.disconnect())
        self._set_topbar_controls_visible(False)
        self._root_stack.set_visible_child_name("login")

    def _toggle_queue_panel(self) -> bool:
        new_state = not self._queue_revealer.get_reveal_child()
        if new_state:
            self._queue_revealer.set_visible(True)
        self._queue_revealer.set_reveal_child(new_state)
        if new_state:
            self._schedule_apply_panes()
        self.ma_service.session_store.data.queue_visible = new_state
        self.ma_service.session_store.save()
        if new_state:
            self.ma_service.runner.submit(self.ma_service.refresh_queue_items(force=True))
        return new_state

    def _close_queue_panel(self) -> None:
        self._queue_revealer.set_reveal_child(False)
        self._player_bar.set_queue_active_visual(False)
        self.ma_service.session_store.data.queue_visible = False
        self.ma_service.session_store.save()
        self._schedule_apply_panes()

    def _on_queue_child_revealed(self, rev: Gtk.Revealer, _pspec: object) -> None:
        if not rev.get_reveal_child() and not rev.get_child_revealed():
            rev.set_visible(False)
            self._schedule_apply_panes()

    def _on_now_playing_changed(self, st: NowPlayingState) -> None:
        is_playing = st.playback_state == PlaybackState.PLAYING
        active_uri = (
            st.uri
            if st.uri
            and (st.playback_state in (PlaybackState.PLAYING, PlaybackState.PAUSED) or st.pending)
            else ""
        )
        if active_uri == self._current_playing_uri and is_playing == self._current_is_playing:
            return
        self._current_playing_uri = active_uri
        self._current_is_playing = is_playing
        for row in self._active_track_rows:
            row.set_playing(bool(active_uri and row.uri == active_uri), animated=is_playing)

    def _on_library_changed_refresh(self) -> None:
        """Syncs the sidebar, explorer grid and cache when an item is added/removed from the library."""
        if not self.ma_service.client:
            return
        # 1. Immediately remove deleted items from the local cache, sidebar and open explorer grid
        for attr in ("albums", "playlists", "artists", "radios"):
            lst = getattr(self._home_cache, attr, None)
            if lst:
                setattr(
                    self._home_cache,
                    attr,
                    [it for it in lst if self.ma_service.is_in_library(it)],
                )
        if self._raw_library_items:
            self._raw_library_items = [
                it for it in self._raw_library_items if self.ma_service.is_in_library(it)
            ]
            self._refresh_library_list_ui()

        self._update_explorer_section_counts()
        if self._exp_flow is not None and self._explorer_section in (
            "albums",
            "playlists",
            "artists",
            "radios",
        ):
            removed_rendered = 0
            child = self._exp_flow.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                card = child.get_first_child() if isinstance(child, Gtk.FlowBoxChild) else child
                if isinstance(card, MediaCard) and not self.ma_service.is_in_library(card.item):
                    self._exp_flow.remove(child)
                    removed_rendered += 1
                child = nxt
            if removed_rendered:
                self._exp_pending_items = [
                    it for it in self._exp_pending_items if self.ma_service.is_in_library(it)
                ]
                self._exp_rendered_count = max(0, self._exp_rendered_count - removed_rendered)
                if getattr(self, "_exp_count_lbl", None) is not None:
                    self._exp_count_lbl.set_label(f"{len(self._exp_pending_items)} items")

        # 2. Debounced server sync so newly added items appear once the server finishes
        if getattr(self, "_lib_sync_timer", 0):
            GLib.source_remove(self._lib_sync_timer)
        self._lib_sync_timer = GLib.timeout_add(600, self._sync_library_from_server)

    def _update_explorer_section_counts(self) -> None:
        if not hasattr(self, "_exp_sec_btns"):
            return
        counts = {
            "albums": len(self._home_cache.albums),
            "playlists": len(self._home_cache.playlists),
            "artists": len(self._home_cache.artists),
            "radios": len(self._home_cache.radios),
        }
        for sec_key, sec_title in EXPLORER_SECTIONS:
            if btn := self._exp_sec_btns.get(sec_key):
                cnt = counts.get(sec_key)
                btn.set_label(f"{sec_title} ({cnt})" if cnt else sec_title)

    def _sync_library_from_server(self) -> bool:
        self._lib_sync_timer = 0
        if not self.ma_service.client:
            return GLib.SOURCE_REMOVE

        def _on_synced(home: HomeContent) -> None:
            for attr in ("albums", "playlists", "artists", "radios"):
                lst = getattr(home, attr, None)
                if lst:
                    setattr(home, attr, [it for it in lst if self.ma_service.is_in_library(it)])
            self._home_cache = home
            cat_items = getattr(home, self._lib_category, None)
            if cat_items is not None:
                self._raw_library_items = list(cat_items)
                self._refresh_library_list_ui()
            self._update_explorer_section_counts()
            if (
                self._exp_flow is not None
                and self._explorer_section in ("albums", "playlists", "artists", "radios")
                and len(getattr(home, self._explorer_section, [])) != len(self._exp_pending_items)
                and not self._explorer_filter_query
            ):
                self._refresh_explorer_catalog_ui()

        self.ma_service.runner.submit(self.ma_service.get_home_content(), on_success=_on_synced)
        return GLib.SOURCE_REMOVE

    def _view_uris(self) -> set[str]:
        """URIs that identify what is currently shown (the open item + its tracks)."""
        uris: set[str] = set()
        items = [self._current_view_item] if self._current_view_item is not None else []
        for it in items:
            if uri := getattr(it, "uri", None):
                uris.add(str(uri))
            if ref := self.ma_service.find_library_id(it):
                uris.add(f"library://{ref[0].value}/{ref[1]}")
        for row in self._active_track_rows:
            if row.uri:
                uris.add(str(row.uri))
        return uris

    def _on_media_item_updated(self, uri: str) -> None:
        if self._current_view_item is None or uri not in self._view_uris():
            return
        # Same batched re-render as Refresh, without discarding cached cover art
        if getattr(self, "_item_refresh_timer", 0):
            GLib.source_remove(self._item_refresh_timer)
        self._item_refresh_timer = GLib.timeout_add(300, self._rerender_current_view)

    def _on_item_refreshed(self, item: Any) -> None:
        if item is not None and not isinstance(item, dict):
            for sz in (120, 300):
                self.artwork.invalidate(self.ma_service.resolve_image_url(item, size=sz))
        # Batch bursts of MA events into a single re-render, preserving the scroll position
        if getattr(self, "_item_refresh_timer", 0):
            GLib.source_remove(self._item_refresh_timer)
        self._item_refresh_timer = GLib.timeout_add(50, self._rerender_current_view)

    def _rerender_current_view(self) -> bool:
        self._item_refresh_timer = 0
        if not (0 <= self._history_idx < len(self._history)):
            return GLib.SOURCE_REMOVE
        vadj = self._center_scroll.get_vadjustment()
        saved_pos = vadj.get_value() if vadj else 0.0
        self._navigating_history = True
        try:
            self._history[self._history_idx]()
        finally:
            self._navigating_history = False
        if vadj and saved_pos > 0:
            GLib.timeout_add(
                700, lambda: (vadj.set_value(saved_pos), GLib.SOURCE_REMOVE)[1]
            )
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Navigation history (Back / Forward)
    # ------------------------------------------------------------------

    def _remember_scroll(self) -> None:
        """Keeps the scroll offset of the view being left, to restore it when coming back."""
        if 0 <= self._history_idx < len(self._history):
            if vadj := self._center_scroll.get_vadjustment():
                self._scroll_by_route[self._history_idx] = vadj.get_value()

    def _focus_anchor(self) -> None:
        """Leaves the keyboard focus on a button after navigating, so the arrow keys work.

        Rebuilding a view drops the focus (it was on the button or card that was clicked)
        and without one the arrow keys do nothing. The active section button is the same
        starting point as choosing the section by hand; if the sections are not shown,
        the Back button takes the focus.
        """
        btn = self._exp_sec_btns.get(self._explorer_section)
        if btn is None or not btn.get_mapped():
            btn = self._back_btn
        btn.grab_focus()

    def _restore_scroll(self, offset: float) -> None:
        """Scrolls back to `offset` and restores the keyboard focus once the view is laid out."""
        self._scroll_restore_token += 1
        token = self._scroll_restore_token
        frames_left = [30]  # frames to wait for the content to be laid out

        def _tick(_widget: Gtk.Widget, _clock: Gdk.FrameClock) -> bool:
            if token != self._scroll_restore_token:
                return GLib.SOURCE_REMOVE  # another navigation took over
            vadj = self._center_scroll.get_vadjustment()
            if vadj is None:
                return GLib.SOURCE_REMOVE
            # The grid renders in batches: draw enough cards in one go to reach the offset
            short = offset > 0 and vadj.get_upper() - vadj.get_page_size() < offset
            if short and self._exp_flow is not None and self._exp_rendered_count < len(self._exp_pending_items):
                per_card = max(vadj.get_upper(), 1.0) / max(self._exp_rendered_count, 1)
                wanted = int(offset / per_card * 1.15) + 24
                self._append_explorer_batch(max(wanted - self._exp_rendered_count, 36))
                frames_left[0] = 30  # drawing the cards can take a while
                return GLib.SOURCE_CONTINUE
            offset_done = offset <= 0
            if offset > 0 and vadj.get_upper() - vadj.get_page_size() >= offset:
                vadj.set_value(offset)
                offset_done = True
            if offset_done:
                return GLib.SOURCE_REMOVE
            frames_left[0] -= 1
            return GLib.SOURCE_CONTINUE if frames_left[0] > 0 else GLib.SOURCE_REMOVE

        self._center_scroll.add_tick_callback(_tick)

    def _push_route(self, render_fn: Callable[[], None]) -> None:
        if not self._navigating_history:
            self._remember_scroll()
            if self._history_idx < len(self._history) - 1:
                self._history = self._history[: self._history_idx + 1]
                self._scroll_by_route = {
                    i: v for i, v in self._scroll_by_route.items() if i <= self._history_idx
                }
            self._history.append(render_fn)
            self._history_idx = len(self._history) - 1
            self._scroll_restore_token += 1
        self._update_nav_buttons()
        render_fn()

    def _go_back(self) -> None:
        if self._history_idx <= 0:
            return
        self._go_to_history(self._history_idx - 1)

    def _go_forward(self) -> None:
        if self._history_idx >= len(self._history) - 1:
            return
        self._go_to_history(self._history_idx + 1)

    def _go_to_history(self, idx: int) -> None:
        self._remember_scroll()
        self._history_idx = idx
        self._navigating_history = True
        try:
            self._history[idx]()
        finally:
            self._navigating_history = False
        self._restore_scroll(self._scroll_by_route.get(idx, 0.0))
        self._focus_anchor()
        self._update_nav_buttons()

    def _update_nav_buttons(self) -> None:
        self._back_btn.set_sensitive(self._history_idx > 0)
        self._fwd_btn.set_sensitive(self._history_idx < len(self._history) - 1)

    def _clear_center(self) -> None:
        self._current_view_item = None
        self._explorer_render_gen += 1
        self._exp_flow = None
        self._exp_pending_items = []
        self._exp_rendered_count = 0
        self._active_track_rows.clear()
        while (child := self._center_body.get_first_child()) is not None:
            self._center_body.remove(child)
        vadj = self._center_scroll.get_vadjustment()
        if vadj:
            vadj.set_value(0)

    def _show_loading(self, text: str = "Loading music…") -> None:
        self._clear_center()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_top(60)
        sp = Gtk.Spinner()
        sp.set_spinning(True)
        lbl = Gtk.Label(label=text)
        lbl.add_css_class("subtitle-dim")
        box.append(sp)
        box.append(lbl)
        self._center_body.append(box)

    # ------------------------------------------------------------------
    # Library (Left panel with the whole library and sorting)
    # ------------------------------------------------------------------

    def _on_lib_filter_changed(self, entry: Gtk.Entry) -> None:
        self._lib_filter_query = entry.get_text().strip().casefold()
        self._refresh_library_list_ui()

    def _select_library_tab(self, category: str) -> None:
        self._lib_category = category
        self.ma_service.session_store.data.library_active_tab = category
        self.ma_service.session_store.save()
        for k, btn in self._lib_chip_btns.items():
            if k == category:
                btn.add_css_class("active")
            else:
                btn.remove_css_class("active")
        self._update_sort_btn_content()
        # If it is already in the in-memory cache, show it instantly without an extra server request
        cached_list = getattr(self._home_cache, category, None)
        if cached_list:
            self._raw_library_items = list(cached_list)
            self._refresh_library_list_ui()
            return
        self._load_library_sidebar()

    def _load_library_sidebar(self) -> None:
        cat = self._lib_category
        if cat == "playlists":
            coro = self.ma_service.get_playlists(limit=None)
        elif cat == "albums":
            coro = self.ma_service.get_albums(limit=None)
        elif cat == "artists":
            coro = self.ma_service.get_artists(limit=None)
        else:
            coro = self.ma_service.get_radios(limit=None)

        self.ma_service.runner.submit(coro, on_success=self._render_library_sidebar)

    def _render_library_sidebar(self, items: list[Any]) -> None:
        self._raw_library_items = list(items)
        if self._lib_category == "albums":
            self._home_cache.albums = list(items)
        elif self._lib_category == "playlists":
            self._home_cache.playlists = list(items)
        elif self._lib_category == "artists":
            self._home_cache.artists = list(items)
        elif self._lib_category == "radios":
            self._home_cache.radios = list(items)
        self._refresh_library_list_ui()

    def _sort_media_items_for_category(self, category: str, items: list[Any]) -> list[Any]:
        sorted_items = self._sort_media_items_for_category_unpinned(category, items)
        if category == "playlists":
            return self.ma_service.favorites_first(sorted_items)
        return sorted_items

    def _sort_media_items_for_category_unpinned(self, category: str, items: list[Any]) -> list[Any]:
        return self.ma_service.session_store.sort_library_entries(
            category,
            items,
            key_fn=_item_unique_key,
            name_fn=lambda it: getattr(it, "name", "") or "",
            creator_fn=lambda it: format_artists(it) or getattr(it, "name", "") or "",
            timestamp_fn=_item_timestamp,
            year_fn=extract_item_release_date,
        )

    def _build_library_entries(self) -> list[_LibEntry]:
        entries: list[_LibEntry] = []
        for item in self._raw_library_items:
            name = self.ma_service.display_name(item) or "Untitled"
            entries.append(
                _LibEntry(
                    key=_item_unique_key(item),
                    title=name,
                    subtitle=media_subtitle(item, include_year=False),
                    creator=format_artists(item) or name,
                    timestamp=_item_timestamp(item),
                    year=extract_item_release_date(item),
                    image_url=self.ma_service.resolve_image_url(item, size=120),
                    kind=media_kind(item),
                    on_click=lambda it=item: self.open_media_item(it),
                    on_play=lambda it=item: self.play_item_now(it),
                    raw_item=item,
                )
            )
        sorted_entries = self.ma_service.session_store.sort_library_entries(
            self._lib_category,
            entries,
            key_fn=lambda e: e.key,
            name_fn=lambda e: e.title,
            creator_fn=lambda e: e.creator,
            timestamp_fn=lambda e: e.timestamp,
            year_fn=lambda e: e.year,
        )
        if self._lib_category == "playlists":
            return self.ma_service.favorites_first(sorted_entries, lambda e: e.raw_item)
        return sorted_entries

    def _refresh_library_list_ui(self) -> None:
        self._lib_render_gen += 1
        while (child := self._lib_list_box.get_first_child()) is not None:
            self._lib_list_box.remove(child)

        sorted_entries = self._build_library_entries()
        q = self._lib_filter_query

        def _entry_year_text(e: _LibEntry) -> str:
            # e.year holds the date as YYYYMMDD (or a bare year): the filter compares the year only
            return str(e.year // 10000 if e.year > 9999 else e.year) if e.year else ""

        filtered = [
            e
            for e in sorted_entries
            if not q
            or q in e.title.casefold()
            or q in e.subtitle.casefold()
            or q in _entry_year_text(e)
        ]
        self._lib_count_lbl.set_label(str(len(self._raw_library_items)))

        self._lib_pending_entries = filtered
        self._lib_rendered_count = 0
        self._append_lib_batch(40)

    def _append_lib_batch(self, count: int = 40) -> None:
        start = self._lib_rendered_count
        end = min(len(self._lib_pending_entries), start + count)
        if start >= end:
            return
        for entry in self._lib_pending_entries[start:end]:
            self._lib_list_box.append(self._make_library_row_widget(entry))
        self._lib_rendered_count = end

    def _on_lib_scroll_changed(self, adj: Gtk.Adjustment) -> None:
        if self._lib_rendered_count >= len(self._lib_pending_entries):
            return
        if adj.get_value() + adj.get_page_size() >= adj.get_upper() - 420:
            self._append_lib_batch(40)

    def _toggle_library_pin(self, category: str, key: str) -> None:
        self.ma_service.session_store.toggle_pin(category, key)
        if category == self._lib_category:
            self._refresh_library_list_ui()
        if self._explorer_section == category and hasattr(self, "_explorer_grid_host"):
            self._refresh_explorer_catalog_ui()

    def _make_library_row_widget(self, entry: _LibEntry) -> LibraryRow:
        cat = self._lib_category
        can_pin = cat in ("albums", "playlists")
        pinned = can_pin and self.ma_service.session_store.is_pinned(cat, entry.key)
        return LibraryRow(
            title=entry.title,
            subtitle=entry.subtitle,
            artwork=self.artwork,
            image_url=entry.image_url,
            kind=entry.kind,
            on_click=entry.on_click,
            on_play=entry.on_play,
            item=entry.raw_item,
            ma_service=self.ma_service,
            on_open=self.open_media_item,
            collapsed=self._lib_collapsed,
            pinned=pinned,
            on_toggle_pin=(
                (lambda c=cat, k=entry.key: self._toggle_library_pin(c, k)) if can_pin else None
            ),
        )

    # ------------------------------------------------------------------
    # Central Explorer: Prioritizes the Full Library (especially Albums)
    # ------------------------------------------------------------------

    def navigate_home(self) -> None:
        def _render() -> None:
            has_cache = bool(
                self._home_cache.albums
                or self._home_cache.playlists
                or self._home_cache.artists
                or self._home_cache.radios
            )
            if has_cache:
                self._render_home_content(self._home_cache)
            else:
                self._show_loading("Loading your library…")
            self.ma_service.runner.submit(
                self.ma_service.get_home_content(),
                on_success=self._on_remote_home_ready,
            )

        self._push_route(_render)

    def _on_remote_home_ready(self, home: HomeContent) -> None:
        new_sig = (len(home.albums), len(home.playlists), len(home.artists), len(home.radios))
        prev_recs_len = len(self._home_cache.recommendations)
        self._home_cache = home
        # If we already show exactly this library and Discover was already loaded, avoid a heavy re-render
        if (
            self._home_rendered_sig == new_sig
            and hasattr(self, "_explorer_grid_host")
            and (self._explorer_section not in ("discover", "overview") or prev_recs_len == len(home.recommendations))
        ):
            return
        self._render_home_content(home)

    def _render_home_content(self, home: HomeContent) -> None:
        self._home_cache = home
        self._home_rendered_sig = (
            len(home.albums),
            len(home.playlists),
            len(home.artists),
            len(home.radios),
        )
        cat_items = getattr(home, self._lib_category, None)
        if cat_items is not None:
            self._raw_library_items = list(cat_items)
            self._refresh_library_list_ui()
        self._clear_center()

        # Central Explorer filter bar (wraps cleanly when the center column gets narrow)
        toolbar = Adw.WrapBox(child_spacing=8, line_spacing=8)
        toolbar.set_hexpand(True)

        self._exp_sec_btns: dict[str, Gtk.Button] = {}
        counts = {
            "albums": len(home.albums),
            "playlists": len(home.playlists),
            "artists": len(home.artists),
            "radios": len(home.radios),
        }
        for sec_key, sec_title in EXPLORER_SECTIONS:
            cnt = counts.get(sec_key)
            btn_lbl = f"{sec_title} ({cnt})" if cnt else sec_title
            btn = Gtk.Button(label=btn_lbl)
            btn.add_css_class("filter-chip")
            if sec_key == self._explorer_section:
                btn.add_css_class("active")
            btn.connect("clicked", lambda _b, k=sec_key: self._select_explorer_section(k))
            toolbar.append(btn)
            self._exp_sec_btns[sec_key] = btn

        self._exp_filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._exp_filter_box.add_css_class("lib-filter-box")
        self._exp_filter_box.set_size_request(170, -1)
        self._exp_filter_box.set_hexpand(True)
        self._exp_filter_box.append(make_icon_image("search", size=14, color="#B3B3B3"))
        self._exp_filter_entry = Gtk.Entry()
        self._exp_filter_entry.set_hexpand(True)
        self._exp_filter_entry.set_width_chars(1)
        self._exp_filter_entry.set_placeholder_text("Filter by name, artist or year…")
        self._exp_filter_entry.set_text(self._explorer_filter_query)
        self._exp_filter_entry.connect("changed", self._on_explorer_filter_changed)
        self._exp_filter_box.append(self._exp_filter_entry)
        self._exp_filter_box.append(self._make_clear_button(self._exp_filter_entry))
        toolbar.append(self._exp_filter_box)

        self._exp_sort_btn = Gtk.MenuButton()
        self._exp_sort_btn.add_css_class("sort-pill-btn")
        self._exp_sort_popover = self._build_sort_popover(
            get_category=lambda: (
                self._explorer_section if self._explorer_section != "overview" else "albums"
            ),
            on_changed=self._on_sort_settings_changed,
        )
        self._exp_sort_btn.set_popover(self._exp_sort_popover)
        toolbar.append(self._exp_sort_btn)

        self._exp_sort_dir_btn = Gtk.Button()
        self._exp_sort_dir_btn.add_css_class("sort-dir-btn")
        self._exp_sort_dir_btn.connect(
            "clicked",
            lambda _b: self._toggle_sort_direction(
                self._explorer_section if self._explorer_section != "overview" else "albums"
            ),
        )
        toolbar.append(self._exp_sort_dir_btn)

        self._center_body.append(toolbar)

        self._explorer_grid_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self._explorer_grid_host.set_hexpand(True)
        self._explorer_grid_host.set_halign(Gtk.Align.FILL)
        self._center_body.append(self._explorer_grid_host)

        self._refresh_explorer_catalog_ui()

    def _select_explorer_section(self, section: str) -> None:
        self._explorer_section = section
        self.ma_service.session_store.data.explorer_active_section = section
        self.ma_service.session_store.save()
        for k, btn in self._exp_sec_btns.items():
            if k == section:
                btn.add_css_class("active")
            else:
                btn.remove_css_class("active")
        self._refresh_explorer_catalog_ui()

    def _on_explorer_filter_changed(self, entry: Gtk.Entry) -> None:
        self._explorer_filter_query = entry.get_text().strip().casefold()
        self._refresh_explorer_catalog_ui()

    def _update_explorer_sort_controls(self) -> None:
        if not hasattr(self, "_exp_sort_btn"):
            return
        show_sort = self._explorer_section != "discover"
        self._exp_sort_btn.set_visible(show_sort)
        self._exp_sort_dir_btn.set_visible(show_sort)
        if not show_sort:
            return
        cat = self._explorer_section
        store = self.ma_service.session_store
        cur_mode = store.get_library_sort_mode(cat)
        cur_dir = store.get_library_sort_direction(cat)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.append(make_icon_image("sort", size=13, color="#B3B3B3"))
        lbl = Gtk.Label(label=_sort_label_for_category(cur_mode, cat))
        lbl.add_css_class("muted-label")
        box.append(lbl)
        self._exp_sort_btn.set_child(box)

        dir_icon = "arrow_up" if cur_dir == "asc" else "arrow_down"
        set_button_svg(self._exp_sort_dir_btn, dir_icon, size=13, color="#94A3B8")
        self._exp_sort_dir_btn.set_tooltip_text(
            "Ascending (click to reverse)"
            if cur_dir == "asc"
            else "Descending (click to reverse)"
        )

    def _refresh_explorer_catalog_ui(self) -> None:
        if not hasattr(self, "_explorer_grid_host"):
            return
        self._explorer_render_gen += 1
        self._exp_flow = None
        self._exp_pending_items = []
        self._exp_rendered_count = 0

        while (c := self._explorer_grid_host.get_first_child()) is not None:
            self._explorer_grid_host.remove(c)

        self._update_explorer_sort_controls()
        home = self._home_cache
        sec = self._explorer_section

        if sec == "discover":
            self._render_discover_section(home)
            return

        if sec == "albums":
            raw_items = home.albums
            heading = "Albums in your library"
        elif sec == "playlists":
            raw_items = home.playlists
            heading = "Playlists in your library"
        elif sec == "artists":
            raw_items = home.artists
            heading = "Artists in your library"
        else:
            raw_items = home.radios
            heading = "Radio stations"

        sorted_items = self._sort_media_items_for_category(sec, list(raw_items))
        q = self._explorer_filter_query
        if q:
            sorted_items = [
                it
                for it in sorted_items
                if q in (getattr(it, "name", "") or "").casefold()
                or q in media_subtitle(it).casefold()
            ]

        hdr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        h_lbl = Gtk.Label(label=heading, xalign=0.0)
        h_lbl.add_css_class("section-heading")
        hdr_row.append(h_lbl)

        cnt_lbl = Gtk.Label(label=f"{len(sorted_items)} items", xalign=0.0)
        cnt_lbl.add_css_class("subtitle-dim")
        cnt_lbl.set_valign(Gtk.Align.END)
        self._exp_count_lbl = cnt_lbl
        hdr_row.append(cnt_lbl)
        self._explorer_grid_host.append(hdr_row)

        if not sorted_items:
            empty = Gtk.Label(
                label="No items match your current filter.",
                xalign=0.0,
            )
            empty.add_css_class("subtitle-dim")
            self._explorer_grid_host.append(empty)
            return

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_hexpand(True)
        flow.set_halign(Gtk.Align.FILL)
        flow.set_homogeneous(True)
        flow.set_max_children_per_line(12)
        flow.set_min_children_per_line(1)
        flow.set_column_spacing(14)
        flow.set_row_spacing(14)
        self._explorer_grid_host.append(flow)

        self._exp_flow = flow
        self._exp_pending_items = sorted_items
        self._exp_rendered_count = 0
        self._append_explorer_batch(40)

    def _append_explorer_batch(self, count: int = 36) -> None:
        if self._exp_flow is None:
            return
        start = self._exp_rendered_count
        end = min(len(self._exp_pending_items), start + count)
        if start >= end:
            return
        for item in self._exp_pending_items[start:end]:
            self._exp_flow.append(
                MediaCard(
                    item,
                    self.ma_service,
                    self.artwork,
                    on_open=self.open_media_item,
                    on_play=self.play_item_now,
                )
            )
        self._exp_rendered_count = end

    def _on_center_scroll_changed(self, adj: Gtk.Adjustment) -> None:
        if self._exp_flow is None or self._exp_rendered_count >= len(self._exp_pending_items):
            return
        if adj.get_value() + adj.get_page_size() >= adj.get_upper() - 600:
            self._append_explorer_batch(36)

    def _render_discover_section(self, home: HomeContent) -> None:
        folders = list(home.recommendations or [])
        if not folders:
            if self.ma_service.client:
                loading_lbl = Gtk.Label(
                    label="Loading Discover recommendations…",
                    xalign=0.0,
                )
                loading_lbl.add_css_class("subtitle-dim")
                self._explorer_grid_host.append(loading_lbl)
                self.ma_service.runner.submit(
                    self.ma_service.get_discover_folders(),
                    on_success=self._on_discover_folders_loaded,
                )
            else:
                empty = Gtk.Label(
                    label="Connect to Music Assistant to browse Discover.",
                    xalign=0.0,
                )
                empty.add_css_class("subtitle-dim")
                self._explorer_grid_host.append(empty)
            return

        q = self._explorer_filter_query
        shown_any = False

        for folder in folders:
            raw_items = [
                it for it in getattr(folder, "items", []) or [] if getattr(it, "is_playable", True)
            ]
            if q:
                raw_items = [
                    it
                    for it in raw_items
                    if q in (getattr(it, "name", "") or "").casefold()
                    or q in media_subtitle(it).casefold()
                    or q in (getattr(folder, "name", "") or "").casefold()
                ]
            if not raw_items:
                continue
            shown_any = True
            self._explorer_grid_host.append(
                make_section_header(getattr(folder, "name", "Discover"))
            )

            # Individual tracks are shown in compact rows; albums/playlists/artists/radios in cards
            all_tracks = all(getattr(it, "media_type", None) == MediaType.TRACK for it in raw_items)
            if all_tracks:
                t_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                for idx, tr in enumerate(raw_items[:12], start=1):
                    row = TrackRow(
                        idx,
                        tr,
                        self.ma_service,
                        self.artwork,
                        on_activate=self.play_item_now,
                        show_cover=True,
                        show_album=True,
                        active_uri=self._current_playing_uri,
                        is_active_playing=self._current_is_playing,
                        on_open_album=self.open_media_item,
                        on_open_artist=self.open_media_item,
                    )
                    self._active_track_rows.append(row)
                    t_box.append(row)
                self._explorer_grid_host.append(t_box)
            else:
                flow = Gtk.FlowBox()
                flow.set_selection_mode(Gtk.SelectionMode.NONE)
                flow.set_hexpand(True)
                flow.set_halign(Gtk.Align.FILL)
                flow.set_homogeneous(True)
                flow.set_max_children_per_line(12)
                flow.set_min_children_per_line(2)
                flow.set_column_spacing(14)
                flow.set_row_spacing(14)
                for item in raw_items[:16]:
                    flow.append(
                        MediaCard(
                            item,
                            self.ma_service,
                            self.artwork,
                            on_open=self.open_media_item,
                            on_play=self.play_item_now,
                        )
                    )
                self._explorer_grid_host.append(flow)

        if not shown_any:
            empty = Gtk.Label(
                label="No Discover items match your current filter.",
                xalign=0.0,
            )
            empty.add_css_class("subtitle-dim")
            self._explorer_grid_host.append(empty)

    def _on_discover_folders_loaded(self, folders: list[Any]) -> None:
        self._home_cache.recommendations = folders
        if self._explorer_section == "discover" and hasattr(self, "_explorer_grid_host"):
            self._refresh_explorer_catalog_ui()

    def _append_card_shelf(
        self, title: str, items: list[Any], *, highlight_library: bool = False
    ) -> None:
        self._center_body.append(make_section_header(title))
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_hexpand(True)
        flow.set_halign(Gtk.Align.FILL)
        flow.set_homogeneous(True)
        flow.set_max_children_per_line(12)
        flow.set_min_children_per_line(1)
        flow.set_column_spacing(14)
        flow.set_row_spacing(14)
        for item in items:
            flow.append(
                MediaCard(
                    item,
                    self.ma_service,
                    self.artwork,
                    on_open=self.open_media_item,
                    on_play=self.play_item_now,
                    highlight_library=highlight_library,
                )
            )
        self._center_body.append(flow)

    # --- Instant search ---

    def _on_search_changed(self, entry: Gtk.Entry) -> None:
        text = entry.get_text().strip()
        self._clear_search_btn.set_visible(bool(text))
        if self._search_timer_id:
            GLib.source_remove(self._search_timer_id)
            self._search_timer_id = 0
        if not text:
            return
        self._search_timer_id = GLib.timeout_add(280, self._trigger_search, text)

    def _on_search_activate(self, entry: Gtk.Entry) -> None:
        text = entry.get_text().strip()
        if not text:
            return
        if self._search_timer_id:
            GLib.source_remove(self._search_timer_id)
            self._search_timer_id = 0
        self._trigger_search(text)

    def _trigger_search(self, query: str) -> bool:
        self._search_timer_id = 0

        def _render() -> None:
            self._show_loading(f"Searching “{query}”…")
            self.ma_service.runner.submit(
                self.ma_service.search_all(query, limit=25),
                on_success=lambda res: self._render_search_results(query, res),
            )

        self._push_route(_render)
        return GLib.SOURCE_REMOVE

    def _render_search_results(self, query: str, res: SearchResults) -> None:
        self._clear_center()

        hdr = Gtk.Label(label=f"Results for “{query}”", xalign=0.0)
        hdr.add_css_class("hero-title")
        hdr.set_ellipsize(Pango.EllipsizeMode.END)
        self._center_body.append(hdr)

        if res.albums:
            self._append_card_shelf("Albums", list(res.albums), highlight_library=True)

        tracks = list(res.tracks or [])
        if tracks:
            self._center_body.append(make_section_header("Tracks"))
            tracks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for idx, tr in enumerate(tracks[:10], start=1):
                row = TrackRow(
                    idx,
                    tr,
                    self.ma_service,
                    self.artwork,
                    on_activate=self.play_item_now,
                    show_cover=True,
                    show_album=True,
                    active_uri=self._current_playing_uri,
                    is_active_playing=self._current_is_playing,
                    on_open_album=self.open_media_item,
                    on_open_artist=self.open_media_item,
                )
                self._active_track_rows.append(row)
                tracks_box.append(row)
            self._center_body.append(tracks_box)

        if res.artists:
            self._append_card_shelf("Artists", list(res.artists), highlight_library=True)
        if res.playlists:
            self._append_card_shelf("Playlists", list(res.playlists), highlight_library=True)
        if res.radio:
            self._append_card_shelf("Radio stations", list(res.radio), highlight_library=True)

        if not (tracks or res.artists or res.albums or res.playlists or res.radio):
            none_lbl = Gtk.Label(label="No results found.", xalign=0.0)
            none_lbl.add_css_class("subtitle-dim")
            self._center_body.append(none_lbl)

    # --- Navigation to Collections and Items ---

    def show_toast(self, text: str) -> None:
        """Shows a short message at the bottom of the window."""
        toast = Adw.Toast.new(text)
        toast.set_timeout(3)
        self._toast_overlay.add_toast(toast)

    def play_item_now(self, item: Any) -> None:
        self.ma_service.runner.submit(self.ma_service.play_media(item, option=QueueOption.REPLACE))

    def open_media_item(self, item: Any) -> None:
        if isinstance(item, tuple) and len(item) == 2:
            album_cand, source_track = item
            if getattr(album_cand, "media_type", None) == MediaType.ALBUM:
                self.navigate_album(album_cand, source_track=source_track)
                return
            item = album_cand
        mt = getattr(item, "media_type", None)
        if mt == MediaType.ALBUM:
            self.navigate_album(item)
        elif mt == MediaType.PLAYLIST:
            self.navigate_playlist(item)
        elif mt == MediaType.ARTIST:
            self.navigate_artist(item)
        else:
            self.play_item_now(item)

    def navigate_album(self, album: Any, source_track: Any = None) -> None:
        def _render() -> None:
            self._show_loading("Loading album…")
            self.ma_service.runner.submit(
                self.ma_service.get_album_tracks(album, source_track=source_track),
                on_success=lambda pair: self._render_collection_view(
                    badge="ALBUM",
                    title=getattr(pair[0], "name", "Album"),
                    subtitle=f"{media_subtitle(pair[0])} • {len(pair[1])} tracks",
                    cover_url=self.ma_service.resolve_image_url(pair[0], size=300),
                    cover_kind="album",
                    parent_playable=pair[0],
                    tracks=pair[1],
                    show_cover=False,
                    show_album=False,
                ),
            )

        self._push_route(_render)

    def navigate_playlist(self, playlist: Any) -> None:
        def _render() -> None:
            self._show_loading("Loading playlist…")
            self.ma_service.runner.submit(
                self.ma_service.get_playlist_tracks(playlist),
                on_success=lambda pair: self._render_collection_view(
                    badge="PLAYLIST",
                    title=self.ma_service.display_name(pair[0]) or "Playlist",
                    subtitle=f"{media_subtitle(pair[0])} • {len(pair[1])} tracks",
                    cover_url=self.ma_service.resolve_image_url(pair[0], size=300),
                    cover_kind="album",
                    parent_playable=pair[0],
                    tracks=pair[1],
                    show_cover=True,
                    show_album=True,
                ),
            )

        self._push_route(_render)

    def navigate_artist(self, artist: Any) -> None:
        def _render() -> None:
            self._show_loading("Loading artist…")
            self.ma_service.runner.submit(
                self.ma_service.get_artist_detail(artist),
                on_success=self._render_artist_detail,
            )

        self._push_route(_render)

    def _setup_track_sort_button(
        self,
        btn: Gtk.MenuButton,
        state: dict[str, Any],
        on_changed: Callable[[], None],
    ) -> None:
        """Sort pill + menu for the tracks of an open playlist (not saved: each view starts in playlist order)."""
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_size_request(230, -1)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(6)
        hdr = Gtk.Label(label="Sort by", xalign=0.0)
        hdr.add_css_class("muted-label")
        hdr.set_margin_start(6)
        box.append(hdr)
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(rows)

        def _update_pill() -> None:
            pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            pill.append(make_icon_image("sort", size=13, color="#B3B3B3"))
            lbl = Gtk.Label(label=dict(_TRACK_SORT_MODES)[state["mode"]])
            lbl.add_css_class("muted-label")
            pill.append(lbl)
            pill.append(
                make_icon_image("arrow_down" if state["desc"] else "arrow_up", size=13, color="#B3B3B3")
            )
            btn.set_child(pill)

        def _row(label: str, active: bool, on_click: Callable[[], None]) -> Gtk.Button:
            row_btn = Gtk.Button()
            row_btn.add_css_class("flat")
            row_btn.add_css_class("device-row")
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=label, xalign=0.0)
            lbl.set_hexpand(True)
            lbl.add_css_class("device-active-label" if active else "track-title")
            inner.append(lbl)
            if active:
                inner.append(make_icon_image("check", size=15, color="#94A3B8"))
            row_btn.set_child(inner)
            row_btn.connect("clicked", lambda _b: on_click())
            return row_btn

        def _populate(*_a: object) -> None:
            while (c := rows.get_first_child()) is not None:
                rows.remove(c)

            def _set(key: str, value: Any) -> None:
                pop.popdown()
                state[key] = value
                _update_pill()
                on_changed()

            for mode, label in _TRACK_SORT_MODES:
                rows.append(_row(label, state["mode"] == mode, lambda m=mode: _set("mode", m)))
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_top(4)
            sep.set_margin_bottom(4)
            rows.append(sep)
            rows.append(_row("Ascending", not state["desc"], lambda: _set("desc", False)))
            rows.append(_row("Descending", state["desc"], lambda: _set("desc", True)))

        pop.connect("show", _populate)
        pop.set_child(box)
        btn.set_popover(pop)
        _update_pill()

    def _render_collection_view(
        self,
        *,
        badge: str,
        title: str,
        subtitle: str,
        cover_url: str,
        cover_kind: str,
        parent_playable: Any,
        tracks: list[Any],
        show_cover: bool,
        show_album: bool,
    ) -> None:
        self._clear_center()
        if not isinstance(parent_playable, list):
            self._current_view_item = parent_playable

        # Hero header
        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        cover_frame, cover_pic = make_cover_frame(184)
        self.artwork.load_into_picture(cover_pic, cover_url, title=title, size=184, kind=cover_kind)
        self._make_cover_expandable(cover_frame, cover_url, title, subtitle)
        if not isinstance(parent_playable, list):
            attach_media_context_menu(
                cover_frame,
                parent_playable,
                self.ma_service,
                on_open_artist=self.open_media_item,
            )
        hero.append(cover_frame)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        info_box.set_valign(Gtk.Align.END)
        info_box.set_hexpand(True)

        is_service_fav_pl = (
            badge == "PLAYLIST"
            and not isinstance(parent_playable, list)
            and bool(self.ma_service.favorites_playlist_domain(parent_playable))
        )

        is_vid_album = (
            badge == "ALBUM"
            and not isinstance(parent_playable, list)
            and is_video_album(parent_playable)
        )

        b_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        b_row.set_valign(Gtk.Align.CENTER)
        b_lbl = Gtk.Label(label=badge, xalign=0.0)
        b_lbl.add_css_class("hero-type")
        b_row.append(b_lbl)
        if is_service_fav_pl:
            ro_pill = Gtk.Label(label="READ-ONLY")
            ro_pill.add_css_class("local-device-badge")
            ro_pill.set_valign(Gtk.Align.CENTER)
            ro_pill.set_tooltip_text("Managed by the music provider; tracks cannot be edited here")
            b_row.append(ro_pill)
        elif is_vid_album:
            vid_pill = Gtk.Label(label="VIDEO • NOT PLAYABLE")
            vid_pill.add_css_class("local-device-badge")
            vid_pill.set_valign(Gtk.Align.CENTER)
            vid_pill.set_tooltip_text("Video albums cannot be played")
            b_row.append(vid_pill)
        info_box.append(b_row)

        t_lbl = Gtk.Label(label=title, xalign=0.0)
        t_lbl.add_css_class("hero-title")
        t_lbl.set_wrap(True)
        t_lbl.set_lines(2)
        t_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        info_box.append(t_lbl)

        album_artists = (
            getattr(parent_playable, "artists", None) or []
            if not isinstance(parent_playable, list)
            and getattr(parent_playable, "media_type", None) == MediaType.ALBUM
            else []
        )
        if album_artists:
            sub_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            sub_row.set_valign(Gtk.Align.CENTER)
            for idx_a, art_obj in enumerate(album_artists):
                art_name = getattr(art_obj, "name", "")
                if not art_name:
                    continue
                if idx_a > 0:
                    comma = Gtk.Label(label=", ")
                    comma.add_css_class("subtitle-dim")
                    sub_row.append(comma)
                a_btn = Gtk.Button(label=art_name)
                a_btn.add_css_class("artist-link-btn")
                a_btn.set_cursor(Gdk.Cursor.new_from_name("pointer"))
                a_btn.set_tooltip_text(f"Go to {art_name}")
                a_btn.connect("clicked", lambda _b, a=art_obj: self.open_media_item(a))
                sub_row.append(a_btn)
            yr = getattr(parent_playable, "year", None)
            tail_parts = [str(yr)] if yr else []
            tail_parts.append(f"{len(tracks)} tracks")
            tail_lbl = Gtk.Label(label=" • " + " • ".join(tail_parts), xalign=0.0)
            tail_lbl.add_css_class("subtitle-dim")
            sub_row.append(tail_lbl)
            info_box.append(sub_row)
        else:
            s_lbl = Gtk.Label(label=subtitle, xalign=0.0)
            s_lbl.add_css_class("subtitle-dim")
            info_box.append(s_lbl)

        hero.append(info_box)
        self._center_body.append(hero)

        # Action bar (large circular Play button + Shuffle)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        actions.set_margin_top(6)
        actions.set_margin_bottom(4)

        play_all_btn = Gtk.Button()
        play_all_btn.add_css_class("cero-accent-fab")
        set_button_svg(play_all_btn, "play", size=24, color="#FFFFFF")
        play_all_btn.set_tooltip_text("Play")
        play_all_btn.set_sensitive(bool(tracks))
        # Playlist sorting: "custom" is the playlist's own order
        sort_state = {"mode": "custom", "desc": False}

        def _ordered_tracks() -> list[Any]:
            return _sort_tracks(tracks, sort_state["mode"], sort_state["desc"])

        def _play_target() -> Any:
            # With a sort active the queue must follow the order on screen
            if sort_state["mode"] == "custom" and not sort_state["desc"]:
                return parent_playable
            return _ordered_tracks()

        play_all_btn.connect(
            "clicked",
            lambda _b: self.ma_service.runner.submit(
                self.ma_service.play_media(
                    _play_target(),
                    option=QueueOption.REPLACE,
                    shuffle=False,
                    preview_item=(_ordered_tracks()[0] if tracks else None),
                )
            ),
        )
        actions.append(play_all_btn)

        shuffle_btn = Gtk.Button()
        shuffle_btn.add_css_class("icon-flat-btn")
        set_button_svg(shuffle_btn, "shuffle", size=24, color="#94A3B8")
        shuffle_btn.set_tooltip_text("Shuffle play")
        shuffle_btn.set_sensitive(bool(tracks))
        shuffle_btn.connect(
            "clicked",
            lambda _b: self.ma_service.runner.submit(
                self.ma_service.play_media(_play_target(), option=QueueOption.REPLACE, shuffle=True)
            ),
        )
        actions.append(shuffle_btn)

        if not isinstance(parent_playable, list) and not is_service_fav_pl:
            lib_btn = make_library_toggle_button(
                parent_playable,
                self.ma_service,
                size=26,
                css_class="icon-flat-btn",
            )
            actions.append(lib_btn)

        sort_btn: Gtk.MenuButton | None = None
        if badge == "PLAYLIST" and tracks:
            sort_btn = Gtk.MenuButton()
            sort_btn.add_css_class("sort-pill-btn")
            sort_btn.set_tooltip_text("Sort tracks")
            sort_btn.set_valign(Gtk.Align.CENTER)
            actions.append(sort_btn)

        self._center_body.append(actions)

        # Track table
        if tracks:
            self._center_body.append(
                make_track_table_header(show_cover=show_cover, show_album=show_album)
            )
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        def _play_from_track(tr: Any) -> None:
            custom_order = sort_state["mode"] == "custom" and not sort_state["desc"]
            if isinstance(parent_playable, list) or not custom_order:
                shown = tracks if isinstance(parent_playable, list) else _ordered_tracks()
                try:
                    idx = shown.index(tr)
                    slice_tracks = shown[idx:]
                except ValueError:
                    slice_tracks = [tr]
                self.ma_service.runner.submit(
                    self.ma_service.play_media(slice_tracks, option=QueueOption.REPLACE)
                )
            else:
                start_id = getattr(tr, "item_id", None) or getattr(tr, "uri", None)
                self.ma_service.runner.submit(
                    self.ma_service.play_media(
                        parent_playable,
                        option=QueueOption.REPLACE,
                        start_item=str(start_id) if start_id else None,
                        preview_item=tr,
                    )
                )

        shown_rows: list[TrackRow] = []

        removable = (
            badge == "PLAYLIST"
            and not isinstance(parent_playable, list)
            and bool(getattr(parent_playable, "is_editable", False))
            and not self.ma_service.favorites_playlist_domain(parent_playable)
        )

        def _remove_from_playlist(tr: Any) -> None:
            name = getattr(tr, "name", "") or "Track"
            gone = getattr(tr, "position", None)

            def _done(_r: Any) -> None:
                if tr in tracks:
                    tracks.remove(tr)
                for other in tracks:  # the positions after the removed one shift down
                    pos = getattr(other, "position", None)
                    if gone and pos and pos > gone:
                        other.position = pos - 1
                _build_rows()
                self.show_toast(f"Removed “{name}” from the playlist")

            def _failed(err: BaseException) -> None:
                logger.warning("Removing from playlist failed: %s", err)
                self.show_toast(f"Could not remove “{name}”")

            self.ma_service.runner.submit(
                self.ma_service.remove_playlist_track(parent_playable, tr),
                on_success=_done,
                on_error=_failed,
            )

        def _build_rows() -> None:
            for old in shown_rows:
                list_box.remove(old)
                if old in self._active_track_rows:
                    self._active_track_rows.remove(old)
            shown_rows.clear()
            for idx, tr in enumerate(_ordered_tracks(), start=1):
                row = TrackRow(
                    idx,
                    tr,
                    self.ma_service,
                    self.artwork,
                    on_activate=_play_from_track,
                    show_cover=show_cover,
                    show_album=show_album,
                    active_uri=self._current_playing_uri,
                    is_active_playing=self._current_is_playing,
                    on_open_album=self.open_media_item,
                    on_open_artist=self.open_media_item,
                    on_remove_from_playlist=_remove_from_playlist if removable else None,
                    favorite_editable=not is_service_fav_pl,
                )
                self._active_track_rows.append(row)
                shown_rows.append(row)
                list_box.append(row)

        _build_rows()

        if sort_btn is not None:
            self._setup_track_sort_button(sort_btn, sort_state, _build_rows)

        if not tracks:
            empty = Gtk.Label(
                label=(
                    "Video albums cannot be played in CERO."
                    if is_vid_album
                    else "No tracks in this collection."
                ),
                xalign=0.0,
            )
            empty.add_css_class("subtitle-dim")
            list_box.append(empty)

        self._center_body.append(list_box)

        if (
            not isinstance(parent_playable, list)
            and getattr(parent_playable, "media_type", None) == MediaType.ALBUM
        ):
            meta = getattr(parent_playable, "metadata", None)
            copyright_str = str(getattr(meta, "copyright", "") or "").strip()
            label_str = str(getattr(meta, "label", "") or "").strip()
            footer_lines: list[str] = []
            if label_str and label_str.casefold() not in copyright_str.casefold():
                footer_lines.append(label_str)
            if copyright_str:
                footer_lines.append(copyright_str)
            if footer_lines:
                footer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                footer_box.set_margin_top(12)
                footer_box.set_margin_bottom(8)
                for line in footer_lines:
                    c_lbl = Gtk.Label(label=line, xalign=0.0)
                    c_lbl.add_css_class("muted-label")
                    c_lbl.set_wrap(True)
                    c_lbl.set_selectable(True)
                    footer_box.append(c_lbl)
                self._center_body.append(footer_box)

    def _render_artist_detail(self, detail: ArtistDetail) -> None:
        self._clear_center()
        art = detail.artist
        self._current_view_item = art
        name = getattr(art, "name", "Artist")

        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        cover_frame, cover_pic = make_cover_frame(168, circular=True)
        artist_img_url = self.ma_service.resolve_image_url(art, size=300)
        self.artwork.load_into_picture(
            cover_pic,
            artist_img_url,
            title=name,
            size=168,
            kind="artist",
        )
        self._make_cover_expandable(cover_frame, artist_img_url, name, "Artist")
        attach_media_context_menu(cover_frame, art, self.ma_service)
        hero.append(cover_frame)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_valign(Gtk.Align.END)
        b_lbl = Gtk.Label(label="ARTIST", xalign=0.0)
        b_lbl.add_css_class("hero-type")
        t_lbl = Gtk.Label(label=name, xalign=0.0)
        t_lbl.add_css_class("hero-title")
        vbox.append(b_lbl)
        vbox.append(t_lbl)
        hero.append(vbox)
        self._center_body.append(hero)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        play_btn = Gtk.Button()
        play_btn.add_css_class("cero-accent-fab")
        set_button_svg(play_btn, "play", size=22, color="#FFFFFF")
        play_btn.connect("clicked", lambda _b: self.play_item_now(art))
        actions.append(play_btn)

        lib_btn = make_library_toggle_button(
            art,
            self.ma_service,
            size=26,
            css_class="icon-flat-btn",
        )
        actions.append(lib_btn)

        self._center_body.append(actions)

        if detail.albums:
            albums: list[Any] = []
            eps: list[Any] = []
            singles: list[Any] = []
            for a in detail.albums:
                at = getattr(a, "album_type", None)
                nm = (getattr(a, "name", "") or "").strip()
                if at == AlbumType.EP or (at != AlbumType.SINGLE and nm.endswith(" - EP")):
                    eps.append(a)
                elif at == AlbumType.SINGLE or nm.endswith(" - Single"):
                    singles.append(a)
                else:
                    albums.append(a)

            sort_key = lambda a: (
                -extract_item_release_date(a),
                (getattr(a, "name", "") or "").casefold(),
            )
            albums.sort(key=sort_key)
            eps.sort(key=sort_key)
            singles.sort(key=sort_key)

            if albums:
                self._append_card_shelf(
                    f"Albums ({len(albums)})", albums, highlight_library=True
                )
            if eps:
                self._append_card_shelf(
                    f"EPs ({len(eps)})", eps, highlight_library=True
                )
            if singles:
                self._append_card_shelf(
                    f"Singles ({len(singles)})", singles, highlight_library=True
                )

        if detail.top_tracks:
            self._center_body.append(make_section_header("Popular"))
            t_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for idx, tr in enumerate(detail.top_tracks[:15], start=1):
                row = TrackRow(
                    idx,
                    tr,
                    self.ma_service,
                    self.artwork,
                    on_activate=lambda track, all_t=detail.top_tracks: self.ma_service.runner.submit(
                        self.ma_service.play_media(
                            all_t[all_t.index(track) :], option=QueueOption.REPLACE
                        )
                    ),
                    show_cover=True,
                    show_album=True,
                    active_uri=self._current_playing_uri,
                    is_active_playing=self._current_is_playing,
                    on_open_album=self.open_media_item,
                    on_open_artist=self.open_media_item,
                )
                self._active_track_rows.append(row)
                t_box.append(row)
            self._center_body.append(t_box)

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _on_key_pressed(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape and self._art_lightbox.get_visible():
            self._close_artwork_lightbox()
            return True

        if keyval == Gdk.KEY_F5:
            if self._root_stack.get_visible_child_name() == "player":
                if self._current_view_item is not None:
                    self.show_toast("Refreshing from server…")
                    self.ma_service.runner.submit(
                        self.ma_service.refresh_item(self._current_view_item),
                        on_error=lambda err: logger.warning("Refresh failed: %s", err),
                    )
                else:
                    self.show_toast("Refreshing library…")
                    self._load_library_sidebar()
                    self.ma_service.runner.submit(
                        self.ma_service.get_home_content(),
                        on_success=self._render_home_content,
                        on_error=lambda err: logger.warning("Library refresh failed: %s", err),
                    )
            return True

        focus = self.get_focus()
        typing = isinstance(focus, (Gtk.Text, Gtk.Entry, Gtk.PasswordEntry, Gtk.SearchEntry))
        ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt_held = bool(state & Gdk.ModifierType.ALT_MASK)

        # Enter opens the card that has the keyboard focus (same as clicking it)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not typing and isinstance(focus, Gtk.FlowBoxChild):
            card = focus.get_first_child()
            if isinstance(card, MediaCard):
                card.on_open(card.item)
                return True

        if ctrl_held and keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            if app := self.get_application():
                app.quit()
            else:
                self.close()
            return True

        if ctrl_held and keyval in (Gdk.KEY_k, Gdk.KEY_K, Gdk.KEY_f, Gdk.KEY_F):
            if self._topbar_center.get_visible():
                self._search_entry.grab_focus()
                return True

        if alt_held and keyval == Gdk.KEY_Left:
            self._go_back()
            return True
        if alt_held and keyval == Gdk.KEY_Right:
            self._go_forward()
            return True

        if not typing and self._root_stack.get_visible_child_name() == "player":
            if keyval == Gdk.KEY_space:
                self.ma_service.runner.submit(self.ma_service.play_pause())
                return True
            if ctrl_held and keyval == Gdk.KEY_Right:
                self.ma_service.runner.submit(self.ma_service.next_track())
                return True
            if ctrl_held and keyval == Gdk.KEY_Left:
                self.ma_service.runner.submit(self.ma_service.previous_track())
                return True

        return False
