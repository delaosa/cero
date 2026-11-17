"""Collapsible right panel: 'Now playing' and 'Play queue'."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gtk, Pango
from music_assistant_models.enums import MediaType
from music_assistant_models.queue_item import QueueItem

from ..artwork import ArtworkLoader
from ..icons import make_icon_image, set_button_svg
from ..ma_service import MAService, NowPlayingState, format_artists
from .player_bar import open_quality_details_popover
from .widgets import (
    _make_menu_row_btn,
    format_duration,
    forward_right_click,
    make_cover_frame,
    open_track_credits_window,
    update_favorite_button_visual,
)


class QueuePanel(Gtk.Box):
    """Right side panel with a featured cover for the current track and queue management."""

    def __init__(
        self,
        ma_service: MAService,
        artwork: ArtworkLoader,
        on_close: Callable[[], None],
        on_open_item: Callable[[Any], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.ma_service = ma_service
        self.artwork = artwork
        self._on_open_item = on_open_item
        self._current_state = NowPlayingState()
        self.add_css_class("cero-island")
        self.set_size_request(300, -1)
        self.set_vexpand(True)

        # Panel header
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr.set_margin_top(12)
        hdr.set_margin_start(14)
        hdr.set_margin_end(10)

        title = Gtk.Label(label="Play Queue", xalign=0.0)
        title.add_css_class("track-title")
        title.set_hexpand(True)
        hdr.append(title)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.add_css_class("flat")
        clear_btn.add_css_class("muted-label")
        clear_btn.set_tooltip_text("Clear play queue")
        clear_btn.connect(
            "clicked", lambda _b: self.ma_service.runner.submit(self.ma_service.clear_queue())
        )
        hdr.append(clear_btn)

        close_btn = Gtk.Button()
        close_btn.add_css_class("icon-flat-btn")
        set_button_svg(close_btn, "close", size=18, color="#B3B3B3")
        close_btn.set_tooltip_text("Hide queue")
        close_btn.connect("clicked", lambda _b: on_close())
        hdr.append(close_btn)
        self.append(hdr)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_start(14)
        body.set_margin_end(14)
        body.set_margin_bottom(14)

        # "Now Playing" card (header includes discreet audio quality indicator)
        now_hdr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        now_hdr = Gtk.Label(label="Now playing", xalign=0.0)
        now_hdr.add_css_class("muted-label")
        now_hdr.set_hexpand(True)
        now_hdr_row.append(now_hdr)

        self._quality_btn = Gtk.Button()
        self._quality_btn.add_css_class("quality-badge-icon")
        self._quality_btn.set_valign(Gtk.Align.CENTER)
        self._quality_btn.set_visible(False)
        self._quality_btn.connect("clicked", lambda _b: self._open_quality_details())
        q_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        q_box.set_valign(Gtk.Align.CENTER)
        self._quality_dot = Gtk.Box()
        self._quality_dot.add_css_class("quality-dot")
        self._quality_dot.set_valign(Gtk.Align.CENTER)
        self._quality_tier_lbl = Gtk.Label(label="HQ")
        self._quality_tier_lbl.add_css_class("quality-tier-text")
        self._quality_tier_lbl.set_valign(Gtk.Align.CENTER)
        q_box.append(self._quality_dot)
        q_box.append(self._quality_tier_lbl)
        self._quality_btn.set_child(q_box)
        now_hdr_row.append(self._quality_btn)
        body.append(now_hdr_row)

        self._now_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._now_card.add_css_class("media-card")
        cover_frame, self._now_pic = make_cover_frame(184)
        cover_frame.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        cover_frame.set_tooltip_text("Open album / playlist")
        cover_click = Gtk.GestureClick()
        cover_click.set_button(1)
        cover_click.connect("released", lambda *_a: self._open_current_context())
        cover_frame.add_controller(cover_click)
        self._now_card.append(cover_frame)

        meta_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        meta_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta_col.set_hexpand(True)

        self._now_title = Gtk.Label(label="Nothing playing", xalign=0.0)
        self._now_title.add_css_class("track-title")
        self._now_title.set_ellipsize(Pango.EllipsizeMode.END)
        meta_col.append(self._now_title)

        self._now_artist = Gtk.Label(label="Select a track or playlist", xalign=0.0)
        self._now_artist.add_css_class("subtitle-dim")
        self._now_artist.add_css_class("player-sub-links")
        self._now_artist.set_ellipsize(Pango.EllipsizeMode.END)
        self._now_artist.connect("activate-link", self._on_sub_link)
        forward_right_click(self._now_artist)
        meta_col.append(self._now_artist)
        meta_row.append(meta_col)

        self._now_fav_btn = Gtk.Button()
        self._now_fav_btn.add_css_class("icon-flat-btn")
        self._now_fav_btn.set_valign(Gtk.Align.CENTER)
        update_favorite_button_visual(self._now_fav_btn, False, size=18)
        self._now_fav_btn.set_visible(False)
        self._now_fav_btn.connect("clicked", self._on_now_fav_clicked)
        meta_row.append(self._now_fav_btn)
        self._now_card.append(meta_row)

        body.append(self._now_card)

        # "Next Up" section with total duration
        self._next_hdr = Gtk.Label(label="Next up", xalign=0.0)
        self._next_hdr.add_css_class("muted-label")
        body.append(self._next_hdr)

        self._queue_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.append(self._queue_list)

        scroll.set_child(body)
        self.append(scroll)

        self.artwork.load_into_picture(self._now_pic, "", title="CERO", size=184, kind="cero")
        self.ma_service.on_state_changed(self._on_state)
        self.ma_service.on_queue_changed(self._on_queue)
        self.ma_service.on_favorite_changed(self._sync_fav_from_service)

    def _open_quality_details(self) -> Gtk.Popover | None:
        return open_quality_details_popover(self._quality_btn, self._current_state)

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
            alb = getattr(st.media_item, "album", None) if st.media_item is not None else None
            if getattr(alb, "media_type", None) == MediaType.ALBUM:
                self._on_open_item((alb, st.media_item))
                return
            target = None
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

    def _on_sub_link(self, _label: Gtk.Label, uri: str) -> bool:
        if self._on_open_item is None:
            return True
        item = self._current_state.media_item
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
        self._now_artist.set_markup(" • ".join(parts) if parts else "Select a track or playlist")
        self._now_artist.set_tooltip_text(" • ".join(tip) if tip else None)

    def _sync_fav_from_service(self) -> None:
        item = self._current_state.media_item
        if item is not None and self._now_fav_btn.get_visible():
            update_favorite_button_visual(
                self._now_fav_btn, bool(getattr(item, "favorite", False)), size=18
            )

    def _on_now_fav_clicked(self, _btn: Gtk.Button) -> None:
        item = self._current_state.media_item
        if item is None:
            return
        new_fav = not bool(getattr(item, "favorite", self._current_state.favorite))
        update_favorite_button_visual(self._now_fav_btn, new_fav, size=18)
        self.ma_service.runner.submit(
            self.ma_service.toggle_favorite_item(item),
            on_success=lambda is_fav: update_favorite_button_visual(self._now_fav_btn, is_fav, size=18),
            on_error=lambda _e: update_favorite_button_visual(
                self._now_fav_btn, bool(getattr(item, "favorite", False)), size=18
            ),
        )

    def _on_state(self, st: NowPlayingState) -> None:
        self._current_state = st
        self._now_title.set_label(st.title or "Nothing playing")
        self._set_sub_line(st)
        self.artwork.load_into_picture(
            self._now_pic, st.image_url, title=st.title or "CERO", size=184, kind="cero"
        )
        has_track = st.media_item is not None and bool(st.title)
        self._now_fav_btn.set_visible(has_track)
        if has_track:
            update_favorite_button_visual(
                self._now_fav_btn, bool(getattr(st.media_item, "favorite", st.favorite)), size=18
            )
        for cls in ("quality-tier-hr", "quality-tier-hq", "quality-tier-sq", "quality-tier-lq"):
            self._quality_dot.remove_css_class(cls)
        if st.quality and st.quality.badge_text:
            tier = st.quality.tier or "SQ"
            self._quality_dot.add_css_class(f"quality-tier-{tier.lower()}")
            self._quality_tier_lbl.set_label(tier)
            self._quality_btn.set_tooltip_text(f"{st.quality.badge_text}\nClick for Audio Pipeline")
            self._quality_btn.set_visible(True)
        else:
            self._quality_btn.set_visible(False)

    def _on_queue(self, items: list[QueueItem], current_item_id: str) -> None:
        while (child := self._queue_list.get_first_child()) is not None:
            self._queue_list.remove(child)

        if not items:
            self._next_hdr.set_label("Next up")
            empty = Gtk.Label(label="Queue is empty", xalign=0.0)
            empty.add_css_class("subtitle-dim")
            self._queue_list.append(empty)
            return

        start_idx = 0
        if current_item_id:
            for idx, qi in enumerate(items):
                if qi.queue_item_id == current_item_id:
                    start_idx = idx + 1
                    break

        remaining = items[start_idx:]
        total_secs = sum(
            float(qi.duration or getattr(qi.media_item, "duration", 0) or 0)
            for qi in remaining
        )
        dur_str = format_duration(total_secs)
        if remaining and dur_str:
            self._next_hdr.set_label(f"Next up • {len(remaining)} tracks • {dur_str}")
        elif remaining:
            self._next_hdr.set_label(f"Next up • {len(remaining)} tracks")
        else:
            self._next_hdr.set_label("Next up")

        upcoming = remaining[:60]
        if not upcoming:
            done_lbl = Gtk.Label(label="No more tracks after the current one", xalign=0.0)
            done_lbl.add_css_class("subtitle-dim")
            self._queue_list.append(done_lbl)
            return

        for offset, qi in enumerate(upcoming):
            real_index = start_idx + offset
            self._queue_list.append(self._make_queue_row(qi, real_index, is_next=(offset == 0)))

    @staticmethod
    def _frame_drag_icon(frame: Gtk.Widget) -> Gdk.Texture | None:
        """Snapshot of the cover as shown in the row (cropped, rounded) to use as drag icon.

        The raw cover texture would be dragged at its full resolution.
        """
        w, h = frame.get_width(), frame.get_height()
        native = frame.get_native()
        if w <= 0 or h <= 0 or native is None:
            return None
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(frame).snapshot(snap, w, h)
        node = snap.to_node()
        if node is None:
            return None
        return native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))

    def _make_queue_row(
        self, qi: QueueItem, queue_index: int, *, is_next: bool = False
    ) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add_css_class("track-row")
        row.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        grip = make_icon_image("grip", size=14, color="#6E7B8F")
        grip.add_css_class("row-drag-grip")
        grip.set_valign(Gtk.Align.CENTER)
        grip.set_tooltip_text("Drag to reorder queue")
        row.append(grip)

        media = qi.media_item
        title = getattr(media, "name", "") or qi.name or "Track"
        artist = format_artists(media)

        frame, pic = make_cover_frame(52)
        url = self.ma_service.resolve_image_url(qi, size=120)
        self.artwork.load_into_picture(pic, url, title=title, size=52)
        row.append(frame)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        vbox.set_hexpand(True)
        vbox.set_valign(Gtk.Align.CENTER)

        t_lbl = Gtk.Label(label=title, xalign=0.0)
        t_lbl.add_css_class("track-title")
        t_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        vbox.append(t_lbl)

        if artist:
            a_lbl = Gtk.Label(label=artist, xalign=0.0)
            a_lbl.add_css_class("subtitle-dim")
            a_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            vbox.append(a_lbl)

        row.append(vbox)

        # Drag & Drop to reorder items within the queue
        dragging = False
        drag_src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)

        def _prepare(_src: Gtk.DragSource, _x: float, _y: float) -> Gdk.ContentProvider:
            val = GObject.Value(GObject.TYPE_STRING, f"qitem:{qi.queue_item_id}:{queue_index}")
            return Gdk.ContentProvider.new_for_value(val)

        def _begin(src: Gtk.DragSource, _drag: Gdk.Drag) -> None:
            nonlocal dragging
            dragging = True
            # Take the icon before dimming the row, so the icon is not dimmed too
            if icon := self._frame_drag_icon(frame):
                src.set_icon(icon, icon.get_width() // 2, icon.get_height() // 2)
            row.add_css_class("dragging-row")

        def _end(_src: Gtk.DragSource, _drag: Gdk.Drag, _del: bool) -> None:
            nonlocal dragging
            dragging = False
            row.remove_css_class("dragging-row")

        drag_src.connect("prepare", _prepare)
        drag_src.connect("drag-begin", _begin)
        drag_src.connect("drag-end", _end)
        row.add_controller(drag_src)

        drop_tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)

        def _clear_drop() -> None:
            row.remove_css_class("drop-above")
            row.remove_css_class("drop-below")

        def _motion(_tgt: Gtk.DropTarget, _x: float, y: float) -> Gdk.DragAction:
            _clear_drop()
            if y < max(1, row.get_height()) / 2.0:
                row.add_css_class("drop-above")
            else:
                row.add_css_class("drop-below")
            return Gdk.DragAction.MOVE

        def _drop(_tgt: Gtk.DropTarget, value: object, _x: float, y: float) -> bool:
            _clear_drop()
            if not isinstance(value, str) or not value.startswith("qitem:"):
                return False
            parts = value.split(":", 2)
            if len(parts) != 3:
                return False
            src_id, src_idx_str = parts[1], parts[2]
            if not src_id or src_id == qi.queue_item_id:
                return False
            try:
                src_idx = int(src_idx_str)
            except ValueError:
                return False
            insert_after = y >= (max(1, row.get_height()) / 2.0)
            target_idx = queue_index + (1 if insert_after else 0)
            if src_idx < target_idx:
                shift = target_idx - src_idx - 1
            else:
                shift = target_idx - src_idx
            if shift != 0:
                self.ma_service.runner.submit(self.ma_service.move_queue_item(src_id, shift))
            return True

        drop_tgt.connect("motion", _motion)
        drop_tgt.connect("leave", lambda _t: _clear_drop())
        drop_tgt.connect("drop", _drop)
        row.add_controller(drop_tgt)

        click = Gtk.GestureClick()
        click.set_button(1)

        def _on_click(_g: Gtk.GestureClick, _n: int, _x: float, _y: float) -> None:
            if dragging:
                return
            self.ma_service.runner.submit(self.ma_service.play_queue_item(queue_index))

        click.connect("released", _on_click)
        row.add_controller(click)
        self._attach_queue_item_menu(row, qi, queue_index, is_next=is_next)
        return row

    def _attach_queue_item_menu(
        self, row: Gtk.Box, qi: QueueItem, queue_index: int, *, is_next: bool
    ) -> None:
        rclick = Gtk.GestureClick()
        rclick.set_button(3)

        def _on_pressed(gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            pop = Gtk.Popover()
            pop.set_has_arrow(False)
            pop.set_autohide(True)
            pop.set_cursor(Gdk.Cursor.new_from_name("default"))
            pop.set_parent(row)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            pop.set_pointing_to(rect)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_size_request(220, -1)

            def _act(fn: Callable[[], None]) -> None:
                pop.popdown()
                fn()

            box.append(
                _make_menu_row_btn(
                    "Play from Here",
                    "play",
                    lambda: _act(
                        lambda: self.ma_service.runner.submit(
                            self.ma_service.play_queue_item(queue_index)
                        )
                    ),
                    icon_color="#94A3B8",
                )
            )
            if not is_next:
                box.append(
                    _make_menu_row_btn(
                        "Play Next",
                        "forward",
                        lambda: _act(
                            lambda: self.ma_service.runner.submit(
                                self.ma_service.move_queue_item_next(qi.queue_item_id)
                            )
                        ),
                    )
                )

            sep1 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep1.set_margin_top(4)
            sep1.set_margin_bottom(4)
            box.append(sep1)

            box.append(
                _make_menu_row_btn(
                    "Remove from Queue",
                    "close",
                    lambda: _act(
                        lambda: self.ma_service.runner.submit(
                            self.ma_service.remove_queue_item(qi.queue_item_id)
                        )
                    ),
                )
            )
            box.append(
                _make_menu_row_btn(
                    "Remove from Here Down",
                    "trash",
                    lambda: _act(
                        lambda: self.ma_service.runner.submit(
                            self.ma_service.remove_queue_items_from(queue_index)
                        )
                    ),
                )
            )

            media = qi.media_item
            nav_rows: list[Gtk.Widget] = []
            if media is not None:
                album_obj = getattr(media, "album", None)
                if album_obj is not None and self._on_open_item is not None:
                    nav_rows.append(
                        _make_menu_row_btn(
                            "Go to Album",
                            "album",
                            lambda: _act(lambda: self._on_open_item((album_obj, media))),
                        )
                    )
                artists = getattr(media, "artists", None) or []
                if artists and self._on_open_item is not None:
                    nav_rows.append(
                        _make_menu_row_btn(
                            "Go to Artist",
                            "artist",
                            lambda: _act(lambda: self._on_open_item(artists[0])),
                        )
                    )
                nav_rows.append(
                    _make_menu_row_btn(
                        "Track Credits",
                        "info",
                        lambda: _act(
                            lambda: open_track_credits_window(row, self.ma_service, media)
                        ),
                    )
                )
            if nav_rows:
                sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                sep2.set_margin_top(4)
                sep2.set_margin_bottom(4)
                box.append(sep2)
                for w in nav_rows:
                    box.append(w)

            pop.set_child(box)
            pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
            pop.popup()

        rclick.connect("pressed", _on_pressed)
        row.add_controller(rclick)
