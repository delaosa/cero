"""Reusable widgets (MediaCard, QuickPickCard, TrackRow, LibraryRow)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango
from music_assistant_models.enums import AlbumType, MediaType, QueueOption

from ..artwork import ArtworkLoader
from ..icons import make_icon_image, set_button_svg
from ..ma_service import MAService, format_artists
from ..session import extract_item_year

logger = logging.getLogger(__name__)


def format_duration(seconds: float | int | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    total = int(round(seconds))
    hrs, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def media_kind(item: Any) -> str:
    mt = getattr(item, "media_type", None)
    if mt == MediaType.ARTIST:
        return "artist"
    if mt == MediaType.RADIO:
        return "radio"
    return "album"


def media_subtitle(item: Any, include_year: bool = True) -> str:
    mt = getattr(item, "media_type", None)
    if mt == MediaType.ARTIST:
        return "Artist"
    if mt == MediaType.RADIO:
        return "Radio station"
    yr = extract_item_year(item)
    year_str = str(yr) if include_year and 1900 <= yr <= 2100 else ""
    if mt == MediaType.PLAYLIST:
        owner = getattr(item, "owner", "")
        return f"Playlist • {owner}" if owner else "Playlist"
    artists = format_artists(item)
    if mt == MediaType.ALBUM:
        parts = [p for p in (year_str, artists) if p]
        return " • ".join(parts) or "Album"
    return artists or "Album"


def make_cover_frame(size: int, circular: bool = False) -> tuple[Gtk.Widget, Gtk.Picture]:
    """Creates a clipped container of strictly constant size (min=nat=size) with COVER cropping."""
    frame = Gtk.ScrolledWindow()
    frame.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.EXTERNAL)
    frame.set_size_request(size, size)
    frame.set_min_content_width(size)
    frame.set_max_content_width(size)
    frame.set_min_content_height(size)
    frame.set_max_content_height(size)
    frame.set_propagate_natural_width(False)
    frame.set_propagate_natural_height(False)
    frame.set_hexpand(False)
    frame.set_vexpand(False)
    frame.set_valign(Gtk.Align.CENTER)
    frame.set_halign(Gtk.Align.CENTER)
    frame.set_overflow(Gtk.Overflow.HIDDEN)
    frame.add_css_class("card-cover-circle" if circular else "card-cover")

    pic = Gtk.Picture()
    pic.set_size_request(size, size)
    pic.set_content_fit(Gtk.ContentFit.COVER)
    pic.set_can_shrink(True)
    frame.set_child(pic)
    return frame, pic


def update_library_button_visual(btn: Gtk.Button, in_library: bool, size: int = 18) -> None:
    if in_library:
        set_button_svg(btn, "lib_added", size=size, color="#94A3B8")
        btn.set_tooltip_text("In your library (click to remove)")
    else:
        set_button_svg(btn, "lib_add", size=size, color="#E2E8F0")
        btn.set_tooltip_text("Add to library")


def make_library_toggle_button(
    item: Any,
    ma_service: MAService,
    *,
    size: int = 18,
    css_class: str = "card-lib-btn",
) -> Gtk.Button:
    btn = Gtk.Button()
    btn.add_css_class(css_class)
    update_library_button_visual(btn, ma_service.is_in_library(item), size=size)

    def _sync_visual() -> None:
        update_library_button_visual(btn, ma_service.is_in_library(item), size=size)

    ma_service.on_library_changed(_sync_visual)
    btn.connect("unrealize", lambda _w: ma_service.off_library_changed(_sync_visual))

    def _on_clicked(_b: Gtk.Button) -> None:
        btn.set_sensitive(False)
        # Show the new state right away; the real result corrects it afterwards
        update_library_button_visual(btn, not ma_service.is_in_library(item), size=size)

        def _done(now_in_lib: bool) -> None:
            btn.set_sensitive(True)
            update_library_button_visual(btn, now_in_lib, size=size)

        def _err(err: BaseException) -> None:
            logger.warning("Library toggle failed: %s", err)
            btn.set_sensitive(True)
            _sync_visual()

        ma_service.runner.submit(
            ma_service.toggle_library_item(item),
            on_success=_done,
            on_error=_err,
        )

    btn.connect("clicked", _on_clicked)
    return btn


def update_favorite_button_visual(btn: Gtk.Button, favorite: bool, size: int = 18) -> None:
    if favorite:
        set_button_svg(btn, "heart_filled", size=size, color="#94A3B8")
        btn.set_tooltip_text("Remove from Favorites")
    else:
        set_button_svg(btn, "heart", size=size, color="#E2E8F0")
        btn.set_tooltip_text("Add to Favorites")


def make_favorite_toggle_button(
    item: Any,
    ma_service: MAService,
    *,
    size: int = 24,
    css_class: str = "icon-flat-btn",
) -> Gtk.Button:
    btn = Gtk.Button()
    btn.add_css_class(css_class)
    update_favorite_button_visual(btn, bool(getattr(item, "favorite", False)), size=size)

    def _on_clicked(_b: Gtk.Button) -> None:
        btn.set_sensitive(False)
        # Show the new state right away; the real result corrects it afterwards
        update_favorite_button_visual(btn, not bool(getattr(item, "favorite", False)), size=size)

        def _done(now_fav: bool) -> None:
            btn.set_sensitive(True)
            update_favorite_button_visual(btn, now_fav, size=size)

        def _err(err: BaseException) -> None:
            logger.warning("Favorite toggle failed: %s", err)
            btn.set_sensitive(True)
            update_favorite_button_visual(btn, bool(getattr(item, "favorite", False)), size=size)

        ma_service.runner.submit(
            ma_service.toggle_favorite_item(item),
            on_success=_done,
            on_error=_err,
        )

    def _sync_favorite() -> None:
        # Re-read the flag after any favorite change (also when removing from the library clears it)
        update_favorite_button_visual(btn, bool(getattr(item, "favorite", False)), size=size)

    ma_service.on_favorite_changed(_sync_favorite)
    btn.connect("unrealize", lambda _w: ma_service.off_favorite_changed(_sync_favorite))

    btn.connect("clicked", _on_clicked)
    return btn


def _make_menu_row_btn(
    label: str,
    icon_name: str,
    on_click: Callable[[], None],
    *,
    icon_color: str = "#E2E8F0",
) -> Gtk.Button:
    btn = Gtk.Button()
    btn.add_css_class("flat")
    btn.add_css_class("ctx-menu-btn")
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    row.append(make_icon_image(icon_name, size=15, color=icon_color))
    lbl = Gtk.Label(label=label, xalign=0.0)
    lbl.add_css_class("track-title")
    lbl.set_hexpand(True)
    row.append(lbl)
    btn.set_child(row)
    btn.connect("clicked", lambda _b: on_click())
    return btn


def _extract_credits_roles(meta: Any) -> tuple[str, str, str]:
    """Extracts Composer, Lyricist and other Performers from metadata (when available)."""
    composers: list[str] = []
    lyricists: list[str] = []
    others: list[str] = []

    for attr in ("composer", "composers"):
        val = getattr(meta, attr, None)
        if isinstance(val, str) and val.strip():
            composers.append(val.strip())
        elif isinstance(val, (list, tuple)):
            composers.extend(str(x).strip() for x in val if str(x).strip())

    for attr in ("lyricist", "lyricists"):
        val = getattr(meta, attr, None)
        if isinstance(val, str) and val.strip():
            lyricists.append(val.strip())
        elif isinstance(val, (list, tuple)):
            lyricists.extend(str(x).strip() for x in val if str(x).strip())

    for p in getattr(meta, "performers", None) or []:
        s = str(p).strip()
        if not s:
            continue
        low = s.casefold()
        if "composer" in low or "(music)" in low:
            composers.append(s)
        elif "lyricist" in low or "(lyrics)" in low:
            lyricists.append(s)
        else:
            others.append(s)

    def _dedup(items: list[str]) -> str:
        seen: set[str] = set()
        out: list[str] = []
        for x in items:
            k = x.casefold()
            if k not in seen:
                seen.add(k)
                out.append(x)
        return ", ".join(out)

    return _dedup(composers), _dedup(lyricists), _dedup(others)


def open_track_credits_window(
    parent_widget: Gtk.Widget,
    ma_service: MAService,
    item: Any,
    *,
    title: str = "",
    artist: str = "",
    album: str = "",
    duration: float = 0.0,
    uri: str = "",
    source_provider: str = "",
) -> Gtk.Window | None:
    """Opens the modal track credits dialog (Composer, Lyricist, Performers, Copyright, etc.)."""
    if not title and item is None:
        return None

    track_title = title or getattr(item, "name", "") or "Unknown Track"
    track_artist = artist or format_artists(item)
    album_obj0 = getattr(item, "album", None) if item is not None else None
    track_album = album or (getattr(album_obj0, "name", "") if album_obj0 else "")
    track_dur = duration or float(getattr(item, "duration", 0) or 0)
    track_uri = uri or str(getattr(item, "uri", "") or "")

    root_win = parent_widget.get_root()
    win = Gtk.Window(
        title="Track Credits",
        transient_for=root_win if isinstance(root_win, Gtk.Window) else None,
        modal=True,
        resizable=False,
    )
    win.set_default_size(440, -1)

    key_ctrl = Gtk.EventControllerKey()
    key_ctrl.connect(
        "key-pressed",
        lambda _c, kv, _kc, _st: (win.close(), True)[1] if kv == Gdk.KEY_Escape else False,
    )
    win.add_controller(key_ctrl)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    outer.set_margin_top(16)
    outer.set_margin_bottom(16)
    outer.set_margin_start(18)
    outer.set_margin_end(18)

    hdr_title = Gtk.Label(label=track_title, xalign=0.0)
    hdr_title.add_css_class("section-heading")
    hdr_title.set_ellipsize(Pango.EllipsizeMode.END)
    outer.append(hdr_title)

    sub = " • ".join(p for p in (track_artist, track_album) if p)
    if sub:
        hdr_sub = Gtk.Label(label=sub, xalign=0.0)
        hdr_sub.add_css_class("subtitle-dim")
        hdr_sub.set_ellipsize(Pango.EllipsizeMode.END)
        outer.append(hdr_sub)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_max_content_height(520)
    scroll.set_propagate_natural_height(True)

    sections_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    scroll.set_child(sections_box)
    outer.append(scroll)

    def _render_item_credits(resolved: Any) -> None:
        while (c := sections_box.get_first_child()) is not None:
            sections_box.remove(c)

        def _add_card(sec_title: str, rows: list[tuple[str, str]]) -> None:
            filtered = [(k, v) for k, v in rows if v]
            if not filtered:
                return
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.add_css_class("tech-section-card")
            s_lbl = Gtk.Label(label=sec_title, xalign=0.0)
            s_lbl.add_css_class("hero-type")
            card.append(s_lbl)
            for k, v in filtered:
                r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                k_lbl = Gtk.Label(label=k, xalign=0.0)
                k_lbl.add_css_class("subtitle-dim")
                k_lbl.set_valign(Gtk.Align.START)
                k_lbl.set_hexpand(True)
                v_lbl = Gtk.Label(label=v, xalign=1.0)
                v_lbl.add_css_class("track-title")
                v_lbl.set_wrap(True)
                v_lbl.set_max_width_chars(30)
                v_lbl.set_selectable(True)
                v_lbl.set_focusable(False)  # selectable with the mouse, but never auto-selected on open
                r.append(k_lbl)
                r.append(v_lbl)
                card.append(r)
            sections_box.append(card)

        meta = getattr(resolved, "metadata", None) if resolved is not None else None
        artists = getattr(resolved, "artists", None) or []
        artist_names = ", ".join(
            getattr(a, "name", "") for a in artists if getattr(a, "name", "")
        ) or track_artist
        album_obj = getattr(resolved, "album", None)
        if album_obj is not None:
            album_name, album_type = split_album_name_and_type(album_obj)
        else:
            album_name = track_album
            album_type = ""
        album_meta = getattr(album_obj, "metadata", None) if album_obj is not None else None

        composers, lyricists, performers = (
            _extract_credits_roles(meta) if meta is not None else ("", "", "")
        )

        genres_raw = getattr(meta, "genres", None) or []
        genres = ", ".join(str(g) for g in genres_raw if g)

        rel_date = getattr(meta, "release_date", None)
        if rel_date is not None and hasattr(rel_date, "strftime"):
            rel_str = rel_date.strftime("%Y-%m-%d")
        elif rel_date is not None:
            rel_str = str(rel_date)[:10]
        else:
            yr = getattr(album_obj, "year", None) if album_obj else None
            rel_str = str(yr) if yr else ""

        track_no = getattr(resolved, "track_number", None)
        disc_no = getattr(resolved, "disc_number", None)
        pos_str = ""
        if track_no:
            pos_str = f"Disc {disc_no}, Track {track_no}" if disc_no else f"Track {track_no}"

        copyright_str = str(
            getattr(meta, "copyright", "")
            or getattr(album_meta, "copyright", "")
            or ""
        )
        label_str = str(
            getattr(meta, "label", "")
            or getattr(album_meta, "label", "")
            or ""
        )

        _add_card(
            "CREDITS",
            [
                ("Artist(s)", artist_names),
                ("Composer", composers),
                ("Lyricist", lyricists),
                ("Performers", performers),
                ("Album", album_name),
                ("Album Type", album_type),
                ("Track #", pos_str),
                ("Duration", format_duration(track_dur)),
            ],
        )

        _add_card(
            "RELEASE & RIGHTS",
            [
                ("Release Date", rel_str),
                ("Label", label_str),
                ("Copyright", copyright_str),
                ("Genres", genres),
            ],
        )

        prov = getattr(resolved, "provider", "") or source_provider
        _add_card(
            "SOURCE",
            [
                ("Provider", str(prov).split("--")[0].replace("_", " ").title() if prov else ""),
                ("URI", track_uri),
            ],
        )

    _render_item_credits(item)

    if item is not None and ma_service.client:
        ma_service.runner.submit(
            ma_service.resolve_full_item(item),
            on_success=lambda full: _render_item_credits(full) if win.get_visible() else None,
        )

    close_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    close_row.set_halign(Gtk.Align.END)
    close_btn = Gtk.Button(label="Close")
    close_btn.add_css_class("filter-chip")
    close_btn.connect("clicked", lambda _b: win.close())
    close_row.append(close_btn)
    outer.append(close_row)

    win.set_child(outer)
    win.present()
    win.set_focus(None)
    return win


_current_view_provider: Callable[[], Any] | None = None


def set_current_view_provider(provider: Callable[[], Any] | None) -> None:
    """Registers how to get the item whose page is open, so menus skip "go to" that same item."""
    global _current_view_provider
    _current_view_provider = provider


def _is_current_view(target: Any) -> bool:
    """Whether `target` is the item whose page is currently open."""
    current = _current_view_provider() if _current_view_provider else None
    if current is None or target is None:
        return False
    uri, cur_uri = getattr(target, "uri", None), getattr(current, "uri", None)
    if uri and cur_uri:
        return str(uri) == str(cur_uri)
    return (
        getattr(target, "media_type", None) == getattr(current, "media_type", None)
        and getattr(target, "item_id", None) == getattr(current, "item_id", None)
        and getattr(target, "provider", None) == getattr(current, "provider", None)
    )


def _service_domains(item: Any) -> set[str]:
    """Provider domains (spotify, apple_music…) a media item comes from, ignoring MA's own."""
    return {
        str(d)
        for mp in getattr(item, "provider_mappings", None) or ()
        if (d := getattr(mp, "provider_domain", None)) and d not in ("library", "builtin")
    }


def _open_playlist_picker(anchor: Gtk.Widget, menu: Gtk.Popover, track: Any, ma_service: MAService) -> None:
    """Side menu with a search box listing the editable playlists of the track's own service."""
    pop = Gtk.Popover()
    pop.set_has_arrow(False)
    pop.set_autohide(True)
    pop.set_position(Gtk.PositionType.RIGHT)
    pop.set_cursor(Gdk.Cursor.new_from_name("default"))
    pop.set_parent(anchor)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_size_request(260, -1)
    search = Gtk.SearchEntry()
    search.set_placeholder_text("Find a playlist")
    box.append(search)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_min_content_height(40)
    scroll.set_max_content_height(260)
    scroll.set_propagate_natural_height(True)
    list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    scroll.set_child(list_box)
    box.append(scroll)
    pop.set_child(box)

    playlists: list[Any] = []

    def _message(text: str) -> None:
        while (c := list_box.get_first_child()) is not None:
            list_box.remove(c)
        lbl = Gtk.Label(label=text, xalign=0.0)
        lbl.add_css_class("muted-label")
        lbl.set_margin_start(8)
        lbl.set_margin_top(4)
        lbl.set_margin_bottom(4)
        list_box.append(lbl)

    def _add(pl: Any) -> None:
        pl_name = getattr(pl, "name", "") or "Playlist"
        track_name = getattr(track, "name", "") or "Track"
        root = anchor.get_root()
        pop.popdown()
        menu.popdown()

        def _toast_on(text: str) -> None:
            if root is not None and (show := getattr(root, "show_toast", None)):
                show(text)

        def _failed(err: BaseException) -> None:
            logger.warning("Adding to playlist failed: %s", err)
            _toast_on(f"Could not add to “{pl_name}”")

        # The server processes the addition in the background and can take a while
        ma_service.runner.submit(
            ma_service.add_track_to_playlist(pl.item_id, track.uri),
            on_success=lambda _r: _toast_on(f"Added “{track_name}” to “{pl_name}”"),
            on_error=_failed,
        )

    def _render(*_a: object) -> None:
        q = search.get_text().strip().casefold()
        shown = [p for p in playlists if q in (getattr(p, "name", "") or "").casefold()]
        if not shown:
            _message("No playlists found" if playlists else "No editable playlists in this service")
            return
        while (c := list_box.get_first_child()) is not None:
            list_box.remove(c)
        for pl in shown[:100]:
            list_box.append(
                _make_menu_row_btn(getattr(pl, "name", "") or "Playlist", "lib_add", lambda p=pl: _add(p))
            )

    def _on_playlists(all_playlists: list[Any]) -> None:
        domains = _service_domains(track)
        playlists.clear()
        playlists.extend(
            p
            for p in all_playlists
            if getattr(p, "is_editable", True) and (not domains or domains & _service_domains(p))
        )
        _render()

    search.connect("search-changed", _render)
    pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
    _message("Loading playlists…")
    pop.popup()
    search.grab_focus()
    ma_service.runner.submit(ma_service.get_playlists(limit=None), on_success=_on_playlists)


def forward_right_click(label: Gtk.Label) -> None:
    """Replaces the label's own link menu ("Copy link") by the menu of the row or card it is in."""
    gesture = Gtk.GestureClick()
    gesture.set_button(3)
    gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def _on_pressed(g: Gtk.GestureClick, n: int, x: float, y: float) -> None:
        g.set_state(Gtk.EventSequenceState.CLAIMED)  # the label never shows its own menu
        parent = label.get_parent()
        while parent is not None:
            for ctrl in parent.observe_controllers():
                if isinstance(ctrl, Gtk.GestureClick) and ctrl.get_button() == 3:
                    point = label.translate_coordinates(parent, x, y)
                    px, py = point if point else (x, y)
                    ctrl.emit("pressed", n, px, py)
                    return
            parent = parent.get_parent()

    gesture.connect("pressed", _on_pressed)
    label.add_controller(gesture)


def make_artist_links_label(
    item: Any,
    on_open_artist: Callable[[Any], None] | None,
    *,
    max_lines: int = 1,
    css: str = "subtitle-dim",
) -> Gtk.Label | None:
    """Label with the item's artists, each one a link to its page. None if it has no artists."""
    artists = [a for a in (getattr(item, "artists", None) or []) if getattr(a, "name", "")]
    if not artists:
        return None
    if on_open_artist is None:
        label = Gtk.Label(label=", ".join(a.name for a in artists), xalign=0.0)
    else:
        markup = ", ".join(
            f'<a href="cero:artist:{i}">{GLib.markup_escape_text(a.name)}</a>'
            for i, a in enumerate(artists)
        )
        label = Gtk.Label(xalign=0.0)
        label.set_markup(markup)
        label.add_css_class("player-sub-links")

        def _on_link(_lbl: Gtk.Label, uri: str) -> bool:
            if uri.startswith("cero:artist:"):
                on_open_artist(artists[int(uri.rsplit(":", 1)[1])])
                return True
            return False

        label.connect("activate-link", _on_link)
        forward_right_click(label)
    label.add_css_class(css)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    if max_lines > 1:
        label.set_lines(max_lines)
    return label


def attach_media_context_menu(
    widget: Gtk.Widget,
    item: Any,
    ma_service: MAService,
    *,
    on_open: Callable[[Any], None] | None = None,
    on_open_album: Callable[[Any], None] | None = None,
    on_open_artist: Callable[[Any], None] | None = None,
    on_remove_from_playlist: Callable[[Any], None] | None = None,
    is_pinned: Callable[[], bool] | None = None,
    on_toggle_pin: Callable[[], None] | None = None,
) -> None:
    """Adds a right-click context menu (same actions as the Music Assistant UI)."""
    if item is None:
        return

    rclick = Gtk.GestureClick()
    rclick.set_button(3)

    def _on_pressed(gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        pop = Gtk.Popover()
        pop.set_has_arrow(False)
        pop.set_autohide(True)
        pop.set_cursor(Gdk.Cursor.new_from_name("default"))
        pop.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_size_request(220, -1)

        def _act(fn: Callable[[], None]) -> None:
            pop.popdown()
            fn()

        mt = getattr(item, "media_type", None)
        playable = getattr(item, "is_playable", True) and not is_video_album(item)

        if playable:
            box.append(
                _make_menu_row_btn(
                    "Play Now",
                    "play",
                    lambda: _act(
                        lambda: ma_service.runner.submit(
                            ma_service.play_media(item, option=QueueOption.REPLACE)
                        )
                    ),
                    icon_color="#94A3B8",
                )
            )
            box.append(
                _make_menu_row_btn(
                    "Play Now (Keep Queue)",
                    "play",
                    lambda: _act(
                        lambda: ma_service.runner.submit(
                            ma_service.play_media(item, option=QueueOption.PLAY)
                        )
                    ),
                )
            )
            box.append(
                _make_menu_row_btn(
                    "Play Next",
                    "forward",
                    lambda: _act(
                        lambda: ma_service.runner.submit(
                            ma_service.play_media(item, option=QueueOption.REPLACE_NEXT)
                        )
                    ),
                )
            )
            box.append(
                _make_menu_row_btn(
                    "Play Next (Keep Queue)",
                    "forward",
                    lambda: _act(
                        lambda: ma_service.runner.submit(
                            ma_service.play_media(item, option=QueueOption.NEXT)
                        )
                    ),
                )
            )
            box.append(
                _make_menu_row_btn(
                    "Add to Queue",
                    "queue",
                    lambda: _act(
                        lambda: ma_service.runner.submit(
                            ma_service.play_media(item, option=QueueOption.ADD)
                        )
                    ),
                )
            )
        else:
            unplayable_lbl = Gtk.Label(
                label="Video albums cannot be played", xalign=0.0
            )
            unplayable_lbl.add_css_class("muted-label")
            unplayable_lbl.set_margin_start(10)
            unplayable_lbl.set_margin_end(10)
            unplayable_lbl.set_margin_top(4)
            unplayable_lbl.set_margin_bottom(4)
            box.append(unplayable_lbl)
        mid_widgets: list[Gtk.Widget] = []
        if (
            on_toggle_pin is not None
            and mt in (MediaType.ALBUM, MediaType.PLAYLIST)
            and not (mt == MediaType.PLAYLIST and ma_service.favorites_playlist_domain(item))
        ):
            pinned_now = bool(is_pinned()) if is_pinned is not None else False
            mid_widgets.append(
                _make_menu_row_btn(
                    "Unpin" if pinned_now else "Pin to Library",
                    "pin",
                    lambda: _act(on_toggle_pin),
                    icon_color="#94A3B8" if pinned_now else "#E2E8F0",
                )
            )
        if mt not in (MediaType.PLAYLIST, MediaType.ALBUM):
            is_fav = bool(getattr(item, "favorite", False))
            mid_widgets.append(
                _make_menu_row_btn(
                    "Remove from Favorites" if is_fav else "Add to Favorites",
                    "heart_filled" if is_fav else "heart",
                    lambda: _act(lambda: ma_service.runner.submit(ma_service.toggle_favorite_item(item))),
                    icon_color="#94A3B8" if is_fav else "#E2E8F0",
                )
            )

        # A favorite track is already in the library, so tracks only get the Favorites entry;
        # a service's own favorites playlist cannot be removed from the library either.
        if mt != MediaType.TRACK and not (
            mt == MediaType.PLAYLIST and ma_service.favorites_playlist_domain(item)
        ):
            in_lib = ma_service.is_in_library(item)
            mid_widgets.append(
                _make_menu_row_btn(
                    "Remove from Library" if in_lib else "Add to Library",
                    "lib_remove" if in_lib else "lib_add",
                    lambda: _act(
                        lambda: ma_service.runner.submit(
                            ma_service.toggle_library_item(item),
                            on_error=lambda err: logger.warning("Library toggle failed: %s", err),
                        )
                    ),
                )
            )

        if mid_widgets:
            sep1 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep1.set_margin_top(4)
            sep1.set_margin_bottom(4)
            box.append(sep1)
            for w in mid_widgets:
                box.append(w)

        # Navigation actions and add track to playlist
        extra_widgets: list[Gtk.Widget] = []
        if (
            on_open is not None
            and mt in (MediaType.ALBUM, MediaType.PLAYLIST, MediaType.ARTIST)
            and not _is_current_view(item)
        ):
            label_map = {
                MediaType.ALBUM: ("Go to Album", "album"),
                MediaType.PLAYLIST: ("Open Playlist", "queue"),
                MediaType.ARTIST: ("Go to Artist", "artist"),
            }
            lbl_text, ic_name = label_map[mt]
            extra_widgets.append(
                _make_menu_row_btn(
                    lbl_text,
                    ic_name,
                    lambda: _act(lambda: on_open(item)),
                )
            )

        album_obj = getattr(item, "album", None)
        if (
            mt == MediaType.TRACK
            and album_obj is not None
            and on_open_album is not None
            and not _is_current_view(album_obj)
        ):
            extra_widgets.append(
                _make_menu_row_btn(
                    "Go to Album",
                    "album",
                    lambda: _act(lambda: on_open_album((album_obj, item))),
                )
            )

        artists_list = getattr(item, "artists", None) or []
        if (
            artists_list
            and mt in (MediaType.TRACK, MediaType.ALBUM)
            and on_open_artist is not None
            and not _is_current_view(artists_list[0])
        ):
            first_artist = artists_list[0]
            extra_widgets.append(
                _make_menu_row_btn(
                    "Go to Artist",
                    "artist",
                    lambda: _act(lambda: on_open_artist(first_artist)),
                )
            )

        if mt == MediaType.TRACK:
            extra_widgets.append(
                _make_menu_row_btn(
                    "Track Credits",
                    "info",
                    lambda: _act(lambda: open_track_credits_window(widget, ma_service, item)),
                )
            )

        if mt == MediaType.TRACK and getattr(item, "uri", None):
            pl_btn = _make_menu_row_btn("Add to Playlist…", "lib_add", lambda: None)
            pl_btn.connect(
                "clicked", lambda b: _open_playlist_picker(b, pop, item, ma_service)
            )
            extra_widgets.append(pl_btn)
            if on_remove_from_playlist is not None:
                extra_widgets.append(
                    _make_menu_row_btn(
                        "Remove from This Playlist",
                        "trash",
                        lambda: _act(lambda: on_remove_from_playlist(item)),
                    )
                )

        if extra_widgets:
            sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep2.set_margin_top(4)
            sep2.set_margin_bottom(4)
            box.append(sep2)
            for w in extra_widgets:
                box.append(w)

        pop.set_child(box)
        pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
        pop.popup()

    rclick.connect("pressed", _on_pressed)
    widget.add_controller(rclick)


class MediaCard(Gtk.Box):
    """Vertical card for an album, playlist, artist or radio with a floating Play button."""

    def __init__(
        self,
        item: Any,
        ma_service: MAService,
        artwork: ArtworkLoader,
        on_open: Callable[[Any], None],
        on_play: Callable[[Any], None],
        size: int = 176,
        highlight_library: bool = False,
        on_open_artist: Callable[[Any], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.item = item
        self.on_open = on_open
        self.add_css_class("media-card")
        self.set_size_request(size + 20, -1)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        kind = media_kind(item)
        is_artist = kind == "artist"

        overlay = Gtk.Overlay()
        frame, pic = make_cover_frame(size, circular=is_artist)
        overlay.set_child(frame)

        url = ma_service.resolve_image_url(item, size=300)
        name = ma_service.display_name(item) or "Untitled"
        artwork.load_into_picture(pic, url, title=name, size=size, kind=kind)

        if highlight_library:
            lib_badge = Gtk.Box()
            lib_badge.add_css_class("card-lib-badge")
            lib_badge.set_halign(Gtk.Align.END)
            lib_badge.set_valign(Gtk.Align.START)
            lib_badge.set_margin_top(6)
            lib_badge.set_margin_end(6)
            lib_badge.set_tooltip_text("In your library")
            lib_badge.append(make_icon_image("lib_added", size=14, color="#FFFFFF"))
            overlay.add_overlay(lib_badge)

            def _refresh_lib_highlight() -> None:
                # Only the badge tells that an item is in the library; the card itself is not tinted
                lib_badge.set_visible(ma_service.is_in_library(item))

            _refresh_lib_highlight()
            self._lib_badge = lib_badge
            self._refresh_lib_highlight = _refresh_lib_highlight
            ma_service.on_library_changed(_refresh_lib_highlight)
            self.connect("unrealize", lambda _w: ma_service.off_library_changed(_refresh_lib_highlight))

        play_btn: Gtk.Button | None = None
        if not is_video_album(item) and getattr(item, "is_playable", True):
            play_btn = Gtk.Button()
            play_btn.add_css_class("card-play-fab")
            set_button_svg(play_btn, "play", size=20, color="#FFFFFF")
            play_btn.set_halign(Gtk.Align.END)
            play_btn.set_valign(Gtk.Align.END)
            play_btn.set_margin_end(8)
            play_btn.set_margin_bottom(8)
            play_btn.set_tooltip_text(f"Play {name}")
            play_btn.connect("clicked", lambda _b: on_play(item))
            overlay.add_overlay(play_btn)
        else:
            vid_badge = Gtk.Label(label="VIDEO")
            vid_badge.add_css_class("local-device-badge")
            vid_badge.set_halign(Gtk.Align.START)
            vid_badge.set_valign(Gtk.Align.END)
            vid_badge.set_margin_start(8)
            vid_badge.set_margin_bottom(8)
            vid_badge.set_tooltip_text("Video albums cannot be played")
            overlay.add_overlay(vid_badge)

        self.append(overlay)

        title_lbl = Gtk.Label(label=name, xalign=0.0)
        title_lbl.add_css_class("track-title")
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.set_max_width_chars(22)
        self.append(title_lbl)

        sub_lbl = None
        if getattr(item, "media_type", None) == MediaType.ALBUM:
            sub_lbl = make_artist_links_label(item, on_open_artist or on_open, max_lines=2)
        if sub_lbl is None:
            sub_lbl = Gtk.Label(label=media_subtitle(item, include_year=False), xalign=0.0)
            sub_lbl.add_css_class("subtitle-dim")
            sub_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            sub_lbl.set_lines(2)
        sub_lbl.set_max_width_chars(22)
        self.append(sub_lbl)

        click = Gtk.GestureClick()
        click.set_button(1)

        def _on_released(_gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
            if play_btn is not None:
                alloc = play_btn.get_allocation()
                if (
                    alloc.width > 0
                    and alloc.x <= x <= alloc.x + alloc.width
                    and alloc.y <= y <= alloc.y + alloc.height
                ):
                    return
            on_open(item)

        click.connect("released", _on_released)
        self.add_controller(click)
        attach_media_context_menu(self, item, ma_service, on_open=on_open)


class QuickPickCard(Gtk.Box):
    """Compact horizontal card for the Home top grid."""

    def __init__(
        self,
        item: Any,
        ma_service: MAService,
        artwork: ArtworkLoader,
        on_open: Callable[[Any], None],
        on_play: Callable[[Any], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add_css_class("quick-pick-card")
        self.set_hexpand(True)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        name = getattr(item, "name", "") or "Untitled"
        kind = media_kind(item)
        frame, pic = make_cover_frame(56, circular=False)
        url = ma_service.resolve_image_url(item, size=120)
        artwork.load_into_picture(pic, url, title=name, size=56, kind=kind)
        self.append(frame)

        lbl = Gtk.Label(label=name, xalign=0.0)
        lbl.add_css_class("track-title")
        lbl.set_hexpand(True)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.append(lbl)

        play_btn = Gtk.Button()
        play_btn.add_css_class("icon-flat-btn")
        set_button_svg(play_btn, "play", size=18, color="#94A3B8")
        play_btn.set_valign(Gtk.Align.CENTER)
        play_btn.set_tooltip_text(f"Play {name}")
        play_btn.connect("clicked", lambda _b: on_play(item))
        self.append(play_btn)

        click = Gtk.GestureClick()
        click.set_button(1)

        def _on_released(_g: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
            alloc = play_btn.get_allocation()
            if (
                alloc.width > 0
                and alloc.x <= x <= alloc.x + alloc.width
                and alloc.y <= y <= alloc.y + alloc.height
            ):
                return
            on_open(item)

        click.connect("released", _on_released)
        self.add_controller(click)
        attach_media_context_menu(self, item, ma_service, on_open=on_open)


_ALBUM_TYPE_LABELS: dict[AlbumType, str] = {
    AlbumType.ALBUM: "Album",
    AlbumType.SINGLE: "Single",
    AlbumType.EP: "EP",
    AlbumType.LIVE: "Live",
    AlbumType.SOUNDTRACK: "Soundtrack",
    AlbumType.COMPILATION: "Compilation",
}

_ALBUM_TYPE_SUFFIXES: tuple[tuple[str, str], ...] = (
    (" - Video Album", "Video Album"),
    (" (Video Album)", "Video Album"),
    (" - Single", "Single"),
    (" (Single)", "Single"),
    (" - EP", "EP"),
    (" (EP)", "EP"),
)


def is_video_album(item: Any) -> bool:
    """Whether `item` is an unplayable Video Album (e.g. Apple Music Video Albums)."""
    if item is None or getattr(item, "media_type", None) != MediaType.ALBUM:
        return False
    name = (getattr(item, "name", "") or "").strip()
    return name.endswith((" - Video Album", " (Video Album)"))


def split_album_name_and_type(album: Any) -> tuple[str, str]:
    """Splits an album's display name from its type (e.g. 'Culito - EP' -> ('Culito', 'EP'))."""
    if album is None:
        return "", ""
    name = (getattr(album, "name", "") or "").strip()
    if not name:
        return "", ""
    detected = ""
    for suffix, label in _ALBUM_TYPE_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)].rstrip()
            detected = label
            break
    if not detected:
        at = getattr(album, "album_type", None)
        detected = _ALBUM_TYPE_LABELS.get(at, "") if at is not None else ""
    return name, detected or "Album"


def make_track_table_header(*, show_cover: bool = True, show_album: bool = True) -> Gtk.Box:
    """Column header row aligned with TrackRow (# | Title | [Album | Type] | Duration)."""
    hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    hdr.add_css_class("track-table-header")

    num_lbl = Gtk.Label(label="#", xalign=1.0)
    num_lbl.add_css_class("muted-label")
    num_lbl.set_size_request(26, -1)
    hdr.append(num_lbl)

    if show_cover:
        cover_spacer = Gtk.Box()
        cover_spacer.set_size_request(40, -1)
        hdr.append(cover_spacer)

    title_lbl = Gtk.Label(label="TITLE", xalign=0.0)
    title_lbl.add_css_class("muted-label")
    title_lbl.set_hexpand(True)

    if show_album:
        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        cols.set_hexpand(True)
        cols.set_homogeneous(True)
        cols.append(title_lbl)

        alb_lbl = Gtk.Label(label="ALBUM", xalign=0.0)
        alb_lbl.add_css_class("muted-label")
        alb_lbl.set_hexpand(True)
        cols.append(alb_lbl)
        hdr.append(cols)

        type_lbl = Gtk.Label(label="ALBUM TYPE", xalign=0.0)
        type_lbl.add_css_class("muted-label")
        type_lbl.set_size_request(90, -1)
        type_lbl.set_margin_end(16)
        hdr.append(type_lbl)
    else:
        hdr.append(title_lbl)

    for _ in range(2):  # matches queue_btn and fav_btn (28px min-width + 3px*2 padding = 34px)
        btn_spacer = Gtk.Box()
        btn_spacer.set_size_request(34, -1)
        hdr.append(btn_spacer)

    dur_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    dur_box.set_size_request(52, -1)
    dur_box.set_hexpand(False)
    dur_icon = make_icon_image("clock", size=14, color="#8b9bb4")
    dur_icon.set_margin_start(38)  # right-aligned inside the 52px duration slot
    dur_icon.set_tooltip_text("Duration")
    dur_box.append(dur_icon)
    hdr.append(dur_box)
    return hdr


class TrackRow(Gtk.Box):
    """Numbered track row with an active indicator, artist/album, favorite and duration."""

    def __init__(
        self,
        index: int,
        track: Any,
        ma_service: MAService,
        artwork: ArtworkLoader,
        on_activate: Callable[[Any], None],
        *,
        show_cover: bool = True,
        show_album: bool = True,
        active_uri: str = "",
        is_active_playing: bool = True,
        on_open_album: Callable[[Any], None] | None = None,
        on_open_artist: Callable[[Any], None] | None = None,
        on_remove_from_playlist: Callable[[Any], None] | None = None,
        favorite_editable: bool = True,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.track = track
        self.uri = str(getattr(track, "uri", "") or "")
        self.add_css_class("track-row")
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        is_playing = bool(active_uri and self.uri and active_uri == self.uri)
        if is_playing:
            self.add_css_class("playing")

        name = getattr(track, "name", "") or "Track"

        num_slot = Gtk.Overlay()
        num_slot.set_size_request(26, -1)
        num_slot.set_valign(Gtk.Align.CENTER)

        self._num_stack = Gtk.Stack()
        self._num_stack.add_css_class("track-num-lbl")
        self._num_stack.set_size_request(26, -1)
        self._num_stack.set_valign(Gtk.Align.CENTER)

        self._num_lbl = Gtk.Label(label=str(index), xalign=1.0)
        self._num_lbl.set_size_request(26, -1)
        self._num_lbl.add_css_class("muted-label")
        self._index_str = str(index)
        self._num_stack.add_named(self._num_lbl, "num")

        playing_icon = make_icon_image("waveform", size=18, color="#FFFFFF")
        playing_icon.set_halign(Gtk.Align.CENTER)
        playing_icon.set_valign(Gtk.Align.CENTER)
        self._num_stack.add_named(playing_icon, "playing")

        paused_icon = make_icon_image("waveform_paused", size=18, color="#94A3B8")
        paused_icon.set_halign(Gtk.Align.CENTER)
        paused_icon.set_valign(Gtk.Align.CENTER)
        self._num_stack.add_named(paused_icon, "paused")

        if is_playing:
            self._num_stack.set_visible_child_name("playing" if is_active_playing else "paused")
        else:
            self._num_stack.set_visible_child_name("num")
        num_slot.set_child(self._num_stack)

        play_btn = Gtk.Button()
        play_btn.add_css_class("track-row-play-btn")
        play_btn.set_halign(Gtk.Align.CENTER)
        play_btn.set_valign(Gtk.Align.CENTER)
        set_button_svg(play_btn, "play", size=14, color="#FFFFFF")
        play_btn.set_tooltip_text(f"Play {name}")
        play_btn.connect("clicked", lambda _b: on_activate(track))
        num_slot.add_overlay(play_btn)
        self.append(num_slot)

        if show_cover:
            frame, pic = make_cover_frame(40)
            url = ma_service.resolve_image_url(track, size=120)
            artwork.load_into_picture(pic, url, title=name, size=40, kind=media_kind(track))
            self.append(frame)

        meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta_box.set_hexpand(True)
        meta_box.set_valign(Gtk.Align.CENTER)

        title_lbl = Gtk.Label(label=name, xalign=0.0)
        title_lbl.add_css_class("track-title")
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.set_width_chars(1)
        meta_box.append(title_lbl)

        artist_lbl = make_artist_links_label(track, on_open_artist)
        if artist_lbl is None and (artist_text := format_artists(track)):
            artist_lbl = Gtk.Label(label=artist_text, xalign=0.0)
            artist_lbl.add_css_class("subtitle-dim")
            artist_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        if artist_lbl is not None:
            artist_lbl.set_width_chars(1)
            meta_box.append(artist_lbl)

        album_obj = getattr(track, "album", None)
        if show_album:
            cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            cols.set_hexpand(True)
            cols.set_homogeneous(True)
            cols.append(meta_box)

            alb_name, alb_type = split_album_name_and_type(album_obj)
            alb_btn = Gtk.Button()
            alb_btn.add_css_class("flat")
            alb_btn.add_css_class("subtitle-dim")
            alb_btn.add_css_class("track-album-btn")
            alb_btn.set_hexpand(True)
            alb_btn.set_halign(Gtk.Align.FILL)
            alb_btn.set_valign(Gtk.Align.CENTER)
            alb_lbl = Gtk.Label(label=alb_name, xalign=0.0)
            alb_lbl.set_halign(Gtk.Align.FILL)
            alb_lbl.set_hexpand(True)
            alb_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            alb_lbl.set_width_chars(1)
            alb_btn.set_child(alb_lbl)
            if album_obj and on_open_album:
                alb_btn.connect("clicked", lambda _b: on_open_album((album_obj, track)))
            else:
                alb_btn.set_sensitive(False)
            cols.append(alb_btn)
            self.append(cols)

            type_lbl = Gtk.Label(label=alb_type, xalign=0.0)
            type_lbl.add_css_class("muted-label")
            type_lbl.set_size_request(90, -1)
            type_lbl.set_margin_end(16)
            type_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            type_lbl.set_valign(Gtk.Align.CENTER)
            self.append(type_lbl)
        else:
            self.append(meta_box)

        queue_btn = Gtk.Button()
        queue_btn.add_css_class("icon-flat-btn")
        set_button_svg(queue_btn, "queue", size=16, color="#B3B3B3")
        queue_btn.set_tooltip_text("Add to queue")
        queue_btn.set_valign(Gtk.Align.CENTER)
        queue_btn.connect(
            "clicked",
            lambda _b: ma_service.runner.submit(
                ma_service.play_media(track, option=QueueOption.ADD)
            ),
        )
        self.append(queue_btn)

        fav_btn = Gtk.Button()
        fav_btn.add_css_class("icon-flat-btn")
        fav_btn.set_valign(Gtk.Align.CENTER)
        self._update_fav_btn(fav_btn, bool(getattr(track, "favorite", False)))

        if favorite_editable:
            def _toggle_fav(_b: Gtk.Button) -> None:
                self._update_fav_btn(fav_btn, not bool(getattr(track, "favorite", False)))
                ma_service.runner.submit(
                    ma_service.toggle_favorite_item(track),
                    on_success=lambda is_fav: self._update_fav_btn(fav_btn, is_fav),
                    on_error=lambda _e: self._update_fav_btn(
                        fav_btn, bool(getattr(track, "favorite", False))
                    ),
                )

            fav_btn.connect("clicked", _toggle_fav)

            def _sync_fav() -> None:
                self._update_fav_btn(fav_btn, bool(getattr(track, "favorite", False)))

            ma_service.on_favorite_changed(_sync_fav)
            fav_btn.connect("unrealize", lambda _w: ma_service.off_favorite_changed(_sync_fav))
        else:
            fav_btn.set_sensitive(False)
            fav_btn.set_tooltip_text("Managed by the music provider")
        self.append(fav_btn)

        dur = format_duration(getattr(track, "duration", 0))
        if not dur and getattr(track, "media_type", None) == MediaType.RADIO:
            dur = "LIVE"
        dur_lbl = Gtk.Label(label=dur, xalign=1.0)
        dur_lbl.add_css_class("muted-label")
        dur_lbl.set_size_request(52, -1)
        self.append(dur_lbl)

        click = Gtk.GestureClick()
        click.set_button(1)

        def _on_click(_g: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
            for btn in (num_slot, queue_btn, fav_btn):
                al = btn.get_allocation()
                if al.width > 0 and al.x <= x <= al.x + al.width and al.y <= y <= al.y + al.height:
                    return
            if n_press == 2:  # double-click plays now, replacing the queue
                on_activate(track)

        click.connect("released", _on_click)
        self.add_controller(click)
        attach_media_context_menu(
            self,
            track,
            ma_service,
            on_open_album=on_open_album,
            on_open_artist=on_open_artist,
            on_remove_from_playlist=on_remove_from_playlist,
        )

    @staticmethod
    def _update_fav_btn(btn: Gtk.Button, is_fav: bool) -> None:
        if is_fav:
            set_button_svg(btn, "heart_filled", size=16, color="#94A3B8")
            btn.set_tooltip_text("Remove from Favorites")
        else:
            set_button_svg(btn, "heart", size=16, color="#B3B3B3")
            btn.set_tooltip_text("Save to Favorites")

    def set_playing(self, playing: bool, *, animated: bool = True) -> None:
        if playing:
            self.add_css_class("playing")
            self._num_stack.set_visible_child_name("playing" if animated else "paused")
        else:
            self.remove_css_class("playing")
            self._num_stack.set_visible_child_name("num")


class LibraryRow(Gtk.Box):
    """Compact Library panel row: click opens the detail; double-click or the cover plays (replacing the queue)."""

    def __init__(
        self,
        title: str,
        subtitle: str,
        artwork: ArtworkLoader,
        image_url: str,
        kind: str,
        on_click: Callable[[], None],
        on_play: Callable[[], None] | None = None,
        *,
        item: Any = None,
        ma_service: MAService | None = None,
        on_open: Callable[[Any], None] | None = None,
        collapsed: bool = False,
        pinned: bool = False,
        on_toggle_pin: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.add_css_class("library-row")
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self._full_tooltip = f"{title}\n{subtitle}" if subtitle else title

        frame, pic = make_cover_frame(52, circular=(kind == "artist"))
        artwork.load_into_picture(pic, image_url, title=title, size=52, kind=kind)

        if on_play is not None:
            cover_overlay = Gtk.Overlay()
            cover_overlay.set_child(frame)
            play_badge = Gtk.Button()
            play_badge.add_css_class("lib-cover-play-overlay")
            set_button_svg(play_badge, "play", size=18, color="#FFFFFF")
            play_badge.set_tooltip_text(f"Play {title}")
            play_badge.connect("clicked", lambda _b: on_play())
            cover_overlay.add_overlay(play_badge)
            self.append(cover_overlay)
            poster_widget: Gtk.Widget = cover_overlay
        else:
            self.append(frame)
            poster_widget = frame
        self._poster_widget = poster_widget

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_valign(Gtk.Align.CENTER)
        vbox.set_hexpand(True)

        t_lbl = Gtk.Label(label=title, xalign=0.0)
        t_lbl.add_css_class("track-title")
        t_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        vbox.append(t_lbl)

        sub_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        sub_row.set_valign(Gtk.Align.CENTER)
        if pinned:
            pin_icon = make_icon_image("pin", size=12, color="#94A3B8")
            pin_icon.set_valign(Gtk.Align.CENTER)
            pin_icon.set_tooltip_text("Pinned")
            sub_row.append(pin_icon)
        s_lbl = Gtk.Label(label=subtitle, xalign=0.0)
        s_lbl.add_css_class("subtitle-dim")
        s_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        s_lbl.set_hexpand(True)
        sub_row.append(s_lbl)
        vbox.append(sub_row)

        self._text_box = vbox
        self.append(vbox)
        if collapsed:
            self.set_collapsed(True)

        click = Gtk.GestureClick()
        click.set_button(1)

        def _on_released(_g: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
            if on_play is not None and self._text_box.get_visible():
                al = poster_widget.get_allocation()
                if al.width > 0 and al.x <= x <= al.x + al.width and al.y <= y <= al.y + al.height:
                    return
            if n_press == 2 and on_play is not None:
                # Double-click: play now, replacing the queue (the first click already opened the detail)
                on_play()
                return
            if n_press == 1:
                on_click()

        click.connect("released", _on_released)
        self.add_controller(click)
        if item is not None and ma_service is not None:
            attach_media_context_menu(
                self,
                item,
                ma_service,
                on_open=on_open,
                is_pinned=(lambda: pinned) if on_toggle_pin is not None else None,
                on_toggle_pin=on_toggle_pin,
            )

    def set_collapsed(self, collapsed: bool) -> None:
        self._text_box.set_visible(not collapsed)
        self._poster_widget.set_hexpand(collapsed)
        self._poster_widget.set_halign(Gtk.Align.CENTER if collapsed else Gtk.Align.START)
        if collapsed:
            self.add_css_class("collapsed")
        else:
            self.remove_css_class("collapsed")
        self.set_tooltip_text(self._full_tooltip if collapsed else None)


def make_section_header(title: str, action_label: str = "", on_action: Callable[[], None] | None = None) -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.set_margin_top(8)
    row.set_margin_bottom(4)
    lbl = Gtk.Label(label=title, xalign=0.0)
    lbl.add_css_class("section-heading")
    lbl.set_hexpand(True)
    row.append(lbl)
    if action_label and on_action:
        btn = Gtk.Button(label=action_label)
        btn.add_css_class("flat")
        btn.add_css_class("subtitle-dim")
        btn.connect("clicked", lambda _b: on_action())
        row.append(btn)
    return row
