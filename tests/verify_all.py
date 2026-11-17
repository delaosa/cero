"""End-to-end verification suite: GTK4 CSS, GStreamer engine, Sendspin and real MPRIS2 with playerctl."""

from __future__ import annotations

import math
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from aiosendspin.client import AudioFormat, PCMFormat
from aiosendspin.models.types import AudioCodec
from gi.repository import GLib, Gst, Gtk
from music_assistant_models.enums import MediaType, PlaybackState, RepeatMode

from cero.artwork import make_placeholder_texture
from cero.async_runner import AsyncRunner
from cero.icons import get_icon_texture
from cero.ma_service import MAService, NowPlayingState, normalize_server_url
from cero.mpris import MprisServer
from cero.sendspin_player import GstAudioEngine, LocalSendspinService
from cero.session import SessionStore


def test_url_normalization() -> None:
    assert normalize_server_url("192.168.1.25") == "http://192.168.1.25:8095"
    assert normalize_server_url("http://mass.local:8095/") == "http://mass.local:8095"
    assert normalize_server_url("https://music.example.com") == "https://music.example.com"
    print("[OK] URL normalization")


def test_session_store() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "session.json"
        store = SessionStore(p)
        store.data.server_url = "http://192.168.1.10:8095"
        store.data.token = "secret-token"
        store.data.local_volume = 72
        store.save()

        reloaded = SessionStore(p)
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600, f"Insecure permissions: {oct(mode)}"
        assert reloaded.data.server_url == "http://192.168.1.10:8095"
        assert reloaded.data.token == "secret-token"
        assert reloaded.data.local_volume == 72

        sample_items = [
            {"k": "pl:1", "n": "Zeta", "c": "B", "t": 10.0, "year": 2018},
            {"k": "pl:2", "n": "Alfa", "c": "C", "t": 30.0, "year": 2024},
            {"k": "pl:3", "n": "Beta", "c": "A", "t": 20.0, "year": 2021},
        ]
        reloaded.set_library_sort_mode("playlists", "alpha", "asc")
        az = reloaded.sort_library_entries(
            "playlists",
            sample_items,
            key_fn=lambda x: x["k"],
            name_fn=lambda x: x["n"],
            creator_fn=lambda x: x["c"],
            timestamp_fn=lambda x: x["t"],
            year_fn=lambda x: x["year"],
        )
        assert [x["n"] for x in az] == ["Alfa", "Beta", "Zeta"]

        reloaded.set_library_sort_mode("albums", "year", "desc")
        yr_desc = reloaded.sort_library_entries(
            "albums",
            sample_items,
            key_fn=lambda x: x["k"],
            name_fn=lambda x: x["n"],
            creator_fn=lambda x: x["c"],
            timestamp_fn=lambda x: x["t"],
            year_fn=lambda x: x["year"],
        )
        assert [x["year"] for x in yr_desc] == [2024, 2021, 2018]

        reloaded.toggle_library_sort_direction("albums")
        yr_asc = reloaded.sort_library_entries(
            "albums",
            sample_items,
            key_fn=lambda x: x["k"],
            name_fn=lambda x: x["n"],
            creator_fn=lambda x: x["c"],
            timestamp_fn=lambda x: x["t"],
            year_fn=lambda x: x["year"],
        )
        assert [x["year"] for x in yr_asc] == [2018, 2021, 2024]
    print("[OK] SessionStore, 0600 permissions and sort filters (Asc/Desc + Year)")


def test_css_and_vector_assets() -> None:
    Gtk.init()
    css = Gtk.CssProvider()
    errors: list[str] = []

    def _on_parsing_error(_prov: Gtk.CssProvider, section: object, error: GLib.Error) -> None:
        errors.append(f"{section}: {error.message}")

    css.connect("parsing-error", _on_parsing_error)
    css_path = Path(__file__).resolve().parent.parent / "cero" / "style.css"
    css.load_from_path(str(css_path))
    assert not errors, f"GTK4 CSS errors: {errors}"

    for icon in ("play", "pause", "next", "prev", "shuffle", "repeat", "repeat_one", "devices", "queue"):
        tex = get_icon_texture(icon, size=24, color="#3B82F6")
        assert tex.get_width() == 24

    for kind in ("album", "favorites", "artist", "radio"):
        ptex = make_placeholder_texture("Discovery", size=160, kind=kind)
        assert ptex.get_width() == 160
    print("[OK] GTK4 stylesheet and vector icons/covers")


def _encode_flac_sample_bytes() -> tuple[bytes, bytes]:
    """Generates a tiny real FLAC stream using GStreamer flacenc to test FLAC decoding."""
    pipeline = Gst.parse_launch(
        "audiotestsrc num-buffers=8 samplesperbuffer=1024 wave=sine "
        "! audio/x-raw,format=S16LE,rate=48000,channels=2 "
        "! flacenc ! appsink name=sink emit-signals=false"
    )
    sink = pipeline.get_by_name("sink")
    pipeline.set_state(Gst.State.PLAYING)
    chunks: list[bytes] = []
    while True:
        sample = sink.emit("try-pull-sample", 500 * Gst.MSECOND)
        if sample is None:
            break
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if ok:
            chunks.append(bytes(mapinfo.data))
            buf.unmap(mapinfo)
    pipeline.set_state(Gst.State.NULL)
    assert len(chunks) >= 2
    header = b"".join(chunks[:2])
    payload = b"".join(chunks[2:])
    return header, payload


def test_gstreamer_pcm_and_flac_engine() -> None:
    engine = GstAudioEngine(initial_volume=80, initial_muted=False)
    try:
        # 1. PCM 48kHz 16-bit stereo test
        pcm_fmt = AudioFormat(
            codec=AudioCodec.PCM,
            pcm_format=PCMFormat(sample_rate=48000, channels=2, bit_depth=16),
        )
        samples = bytearray()
        for i in range(2400):
            v = int(8000 * math.sin(2 * math.pi * 440 * i / 48000))
            samples.extend(struct.pack("<hh", v, v))
        engine.push_chunk(bytes(samples), pcm_fmt, sink_name="fakesink")
        engine.set_volume(55)
        engine.set_muted(True)
        engine.set_muted(False)
        engine.clear_buffers()

        # 2. FLAC 48kHz 16-bit stereo test and repeated Play -> Pause -> Play cycles
        hdr, flac_frames = _encode_flac_sample_bytes()
        flac_fmt = AudioFormat(
            codec=AudioCodec.FLAC,
            pcm_format=PCMFormat(sample_rate=48000, channels=2, bit_depth=16),
            codec_header=hdr[:42],
        )
        for cycle in range(3):
            gen_before = engine.generation
            engine.push_chunk(
                flac_frames,
                flac_fmt,
                sink_name="appsink name=outsink sync=false",
                generation=gen_before,
            )
            assert engine._pipeline is not None
            outsink = engine._pipeline.get_by_name("outsink")
            sample = outsink.emit("try-pull-sample", 400 * Gst.MSECOND)
            assert sample is not None, f"Cycle {cycle}: no samples produced after Play"

            new_gen = engine.clear_buffers()
            assert new_gen > gen_before
            engine.push_chunk(
                flac_frames,
                flac_fmt,
                sink_name="appsink name=outsink sync=false",
                generation=gen_before,
            )
            assert engine._pipeline is None, "A pre-pause chunk must not recreate the pipeline"
    finally:
        engine.stop()
    print("[OK] GStreamer audio engine (PCM + FLAC + Play/Pause/Play cycles without blocking)")


def test_sendspin_client_setup() -> None:
    runner = AsyncRunner()
    runner.start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "session.json")
            svc = LocalSendspinService(store)
            assert len(svc.peer_id) > 20
            svc2 = LocalSendspinService(store)
            assert svc2.peer_id == svc.peer_id
    finally:
        runner.stop()
    print("[OK] Sendspin 1.0 identity and configuration")


def test_mpris_with_playerctl() -> None:
    """Starts a MprisServer in a GLib.MainLoop and runs real `playerctl` commands."""
    runner = AsyncRunner()
    runner.start()

    calls: list[tuple[str, object]] = []

    class MockMAService(MAService):
        def get_now_playing(self) -> NowPlayingState:
            return self._mock_state

        async def play_pause(self) -> None:
            calls.append(("play_pause", None))
            self._mock_state.playback_state = (
                PlaybackState.PAUSED
                if self._mock_state.playback_state == PlaybackState.PLAYING
                else PlaybackState.PLAYING
            )

        async def next_track(self) -> None:
            calls.append(("next", None))

        async def previous_track(self) -> None:
            calls.append(("previous", None))

        async def seek_seconds(self, position_seconds: float) -> None:
            calls.append(("seek", round(position_seconds, 1)))
            self._mock_state.position = position_seconds

        async def set_volume(self, volume: int) -> None:
            calls.append(("volume", volume))
            self._mock_state.volume = volume

        async def set_shuffle(self, enabled: bool) -> None:
            calls.append(("shuffle", enabled))
            self._mock_state.shuffle = enabled

        async def set_repeat(self, mode: RepeatMode) -> None:
            calls.append(("repeat", mode.value))
            self._mock_state.repeat = mode

    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "session.json")
        svc = MockMAService(runner, store)
        svc._mock_state = NowPlayingState(
            connected=True,
            player_id="local-pc",
            player_name="Este PC",
            is_local_player=True,
            playback_state=PlaybackState.PLAYING,
            title="Veridis Quo",
            artist="Daft Punk",
            album="Discovery",
            uri="library://track/42",
            queue_item_id="qitem42",
            duration=345.0,
            position=30.0,
            volume=80,
            shuffle=False,
            repeat=RepeatMode.OFF,
            can_seek=True,
        )

        bus_suffix = "cero_verify"
        mpris = MprisServer(svc, bus_name=f"org.mpris.MediaPlayer2.{bus_suffix}")
        glib_loop = GLib.MainLoop()

        def _start_mpris() -> bool:
            mpris.start()
            mpris.update_state(svc._mock_state, force_emit=True)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_start_mpris)
        loop_thread = threading.Thread(target=glib_loop.run, daemon=True)
        loop_thread.start()

        try:
            deadline = time.time() + 4.0
            listed = ""
            while time.time() < deadline:
                proc = subprocess.run(
                    ["playerctl", "-l"], capture_output=True, text=True, check=False
                )
                listed = proc.stdout
                if bus_suffix in listed:
                    break
                time.sleep(0.1)
            assert bus_suffix in listed, f"playerctl did not find {bus_suffix} in: {listed}"

            # MPRIS DesktopEntry must match the .desktop file id so shells can show the app icon
            de = subprocess.run(
                ["gdbus", "call", "--session", "--dest", f"org.mpris.MediaPlayer2.{bus_suffix}",
                 "--object-path", "/org/mpris/MediaPlayer2", "--method",
                 "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2", "DesktopEntry"],
                capture_output=True, text=True, check=True,
            ).stdout
            assert "xyz.delaosa.CERO" in de, f"Unexpected MPRIS DesktopEntry: {de}"

            def _pctl(*args: str) -> str:
                res = subprocess.run(
                    ["playerctl", "-p", bus_suffix, *args],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return res.stdout.strip()

            assert _pctl("status") == "Playing"
            assert _pctl("metadata", "xesam:title") == "Veridis Quo"
            assert _pctl("metadata", "xesam:artist") == "Daft Punk"
            assert _pctl("metadata", "xesam:album") == "Discovery"

            _pctl("play-pause")
            time.sleep(0.1)
            assert _pctl("status") == "Paused"

            _pctl("next")
            _pctl("previous")
            _pctl("volume", "0.65")
            _pctl("shuffle", "On")
            _pctl("loop", "Track")
            _pctl("position", "120")
            time.sleep(0.15)

            names = [c[0] for c in calls]
            for expected in ("play_pause", "next", "previous", "volume", "shuffle", "repeat", "seek"):
                assert expected in names, f"Missing call {expected} in {calls}"
            assert _pctl("shuffle") == "On"
            assert _pctl("loop") == "Track"
        finally:
            GLib.idle_add(lambda: (mpris.stop(), glib_loop.quit(), False)[2])
            loop_thread.join(timeout=2.0)
            runner.stop()

    print("[OK] Real D-Bus MPRIS2 integration with playerctl (status, metadata, controls, seek, volume, shuffle, loop)")


def test_gtk4_ui_smoke() -> None:
    """Instantiates the main window and renders all views without warnings."""
    from gi.repository import Adw
    from music_assistant_models.media_items import Album, Artist, Playlist, Radio, Track

    from cero.artwork import ArtworkLoader
    from cero.ma_service import ArtistDetail, HomeContent
    from cero.ui.main_window import MainWindow

    Adw.init()
    runner = AsyncRunner()
    runner.start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "session.json")
            ma = MAService(runner, store)
            ss = LocalSendspinService(store)
            art = ArtworkLoader(runner, store)
            app = Adw.Application(application_id="xyz.delaosa.CERO.SmokeTest")

            def _on_activate(a: Adw.Application) -> None:
                win = MainWindow(a, ma, ss, art)
                artist = Artist(
                    item_id="1",
                    provider="library",
                    name="Daft Punk",
                    uri="library://artist/1",
                    provider_mappings=set(),
                )
                album = Album(
                    item_id="2",
                    provider="library",
                    name="Discovery",
                    uri="library://album/2",
                    year=2001,
                    artists=[artist],
                    provider_mappings=set(),
                )
                track = Track(
                    item_id="3",
                    provider="library",
                    name="One More Time",
                    uri="library://track/3",
                    duration=320,
                    artists=[artist],
                    album=album,
                    provider_mappings=set(),
                )
                playlist = Playlist(
                    item_id="4",
                    provider="library",
                    name="Synthwave Mix",
                    uri="library://playlist/4",
                    owner="User",
                    provider_mappings=set(),
                )
                radio = Radio(
                    item_id="5",
                    provider="library",
                    name="SomaFM Groove Salad",
                    uri="library://radio/5",
                    provider_mappings=set(),
                )

                # Test the on-disk library cache for fast startup
                import os

                os.environ["XDG_CACHE_HOME"] = tmp
                ma.session_store.data.server_url = "http://192.168.1.22:8095"
                hc = HomeContent(
                    recently_played=[track, album],
                    playlists=[playlist],
                    albums=[album],
                    artists=[artist],
                    radios=[radio],
                )
                ma._save_cached_home_content(hc)
                loaded_hc = ma.load_cached_home_content()
                assert loaded_hc is not None and len(loaded_hc.albums) == 1

                # Verify that the queue revealer is hidden when the queue is closed
                assert win._queue_revealer.get_visible() is False

                win._render_library_sidebar([playlist, album, artist, radio])

                # The library tree shows no year; cards/detail keep it, and filtering by year still works
                from cero.ui.widgets import media_subtitle
                assert "2001" in media_subtitle(album)
                lib_entries = {e.title: e for e in win._build_library_entries()}
                assert "2001" not in lib_entries["Discovery"].subtitle, lib_entries["Discovery"].subtitle
                win._lib_filter_query = "2001"
                win._render_library_sidebar([playlist, album, artist, radio])
                assert [e.title for e in win._lib_pending_entries] == ["Discovery"], win._lib_pending_entries
                win._lib_filter_query = ""
                win._render_library_sidebar([playlist, album, artist, radio])
                assert len(win._lib_pending_entries) >= 4
                win._render_home_content(hc)
                win._render_collection_view(
                    badge="ALBUM",
                    title="Discovery",
                    subtitle="Daft Punk • 1 tracks",
                    cover_url="",
                    cover_kind="album",
                    parent_playable=album,
                    tracks=[track],
                    show_cover=True,
                    show_album=True,
                )
                win._render_artist_detail(
                    ArtistDetail(artist=artist, top_tracks=[track], albums=[album])
                )
                win._player_bar._apply_state(
                    NowPlayingState(
                        connected=True,
                        player_id="local",
                        player_name="CERO PC",
                        is_local_player=True,
                        playback_state=PlaybackState.PLAYING,
                        title="One More Time",
                        artist="Daft Punk",
                        album="Discovery",
                        uri="library://track/3",
                        duration=320.0,
                        position=45.0,
                        volume=80,
                        shuffle=True,
                        repeat=RepeatMode.ALL,
                        can_seek=True,
                        media_item=track,
                    )
                )
                # Verify that a huge cover (1200x1200) does NOT enlarge the bottom bar
                from gi.repository import Gdk

                huge_svg = (
                    b'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1200">'
                    b'<rect width="1200" height="1200" fill="#3B82F6"/></svg>'
                )
                huge_tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(huge_svg))

                # Queue drag icon must use the cover as shown in the row, not the full-size texture
                from cero.ui.queue_panel import QueuePanel
                from cero.ui.widgets import make_cover_frame

                drag_frame, drag_pic = make_cover_frame(38)
                drag_pic.set_paintable(huge_tex)
                drag_win = Gtk.Window()
                drag_win.set_child(drag_frame)
                drag_win.present()
                ctx = GLib.MainContext.default()
                deadline = time.time() + 3.0
                while drag_frame.get_width() == 0 and time.time() < deadline:
                    ctx.iteration(False)
                drag_icon = QueuePanel._frame_drag_icon(drag_frame)
                max_px = 38 * drag_frame.get_scale_factor()
                assert drag_icon is not None, "Drag icon could not be rendered"
                assert drag_icon.get_width() <= max_px and drag_icon.get_height() <= max_px, (
                    f"Queue drag icon is {drag_icon.get_width()}x{drag_icon.get_height()}, expected <= {max_px}"
                )
                drag_win.destroy()
                win._player_bar._cover_pic.set_paintable(huge_tex)
                min_h, nat_h = win._player_bar.measure(Gtk.Orientation.VERTICAL, -1)[:2]
                assert nat_h <= 104, f"The bottom bar grew too much with a large cover: {nat_h}px"

                for sec in ("discover", "albums", "playlists", "artists", "radios"):
                    win._select_explorer_section(sec)
                    if sec != "discover":
                        win._toggle_sort_direction(sec)

                # Test opening the cover at the maximum available size, copying with right-click and closing with Esc
                win._open_artwork_lightbox(
                    win._player_bar._current_state.image_url, "One More Time", "Daft Punk • Discovery"
                )
                assert win._art_lightbox.get_visible() is True
                assert win._lightbox_title.get_label() == "One More Time"
                assert win._copy_lightbox_image_to_clipboard() is True
                fmts = win.get_clipboard().get_formats().to_string()
                assert "image/png" in fmts, f"Missing image/png in clipboard: {fmts}"
                handled = win._on_key_pressed(None, Gdk.KEY_Escape, 0, Gdk.ModifierType(0))
                assert handled is True
                assert win._art_lightbox.get_visible() is False

                def _walk(widget):
                    yield widget
                    child = widget.get_first_child()
                    while child is not None:
                        yield from _walk(child)
                        child = child.get_next_sibling()

                def _click(widget) -> None:
                    """Fires the primary-button click handler of a widget, as a real click would."""
                    gestures = [
                        c for c in widget.observe_controllers()
                        if isinstance(c, Gtk.GestureClick) and c.get_button() == 1
                    ]
                    assert gestures, f"No primary click handler on {widget}"
                    gestures[0].emit("released", 1, 1.0, 1.0)

                # Clicking the cover in the detail panel maximizes it (and only there)
                cover_url = "http://127.0.0.1:1/cover.jpg"
                real_fetch, real_load = win.artwork.fetch_full_resolution_bytes, win.artwork.load_into_picture

                async def _no_fetch(_url: str) -> None:
                    return None

                win.artwork.fetch_full_resolution_bytes = _no_fetch  # no real download in the test
                win.artwork.load_into_picture = lambda *_a, **_k: None
                try:
                    win._render_collection_view(
                        badge="ALBUM", title="Discovery", subtitle="Daft Punk • 1 tracks",
                        cover_url=cover_url, cover_kind="album", parent_playable=album,
                        tracks=[track], show_cover=True, show_album=True,
                    )
                    expandable = [w for w in _walk(win._center_body) if w.get_tooltip_text() == "Expand cover art"]
                    assert len(expandable) == 1, f"Expected one expandable cover in the detail, got {len(expandable)}"
                    _click(expandable[0])
                    assert win._art_lightbox.get_visible() is True
                    assert win._lightbox_url == cover_url and win._lightbox_title.get_label() == "Discovery"
                    win._close_artwork_lightbox()
                finally:
                    win.artwork.fetch_full_resolution_bytes, win.artwork.load_into_picture = real_fetch, real_load
                # Without a real image (e.g. Liked Songs placeholder) there is nothing to enlarge
                win._render_collection_view(
                    badge="PLAYLIST", title="Liked Songs", subtitle="1 liked tracks",
                    cover_url="", cover_kind="favorites", parent_playable=[track],
                    tracks=[track], show_cover=True, show_album=True,
                )
                assert not [w for w in _walk(win._center_body) if w.get_tooltip_text() == "Expand cover art"]

                # Playlist sorting: the sort button reorders the rows without touching the data
                from types import SimpleNamespace as _NS
                trs = [_NS(name=n, uri=f"library://track/{n}", duration=d, favorite=False, artists=[], album=None,
                           media_type=MediaType.TRACK, item_id=n, provider="library", provider_mappings=[])
                       for n, d in (("b", 30), ("c", 10), ("a", 20))]
                win._render_collection_view(
                    badge="PLAYLIST", title="Sortable", subtitle="3 tracks", cover_url="", cover_kind="album",
                    parent_playable=[], tracks=trs, show_cover=False, show_album=False,
                )
                sort_btns = [w for w in _walk(win._center_body) if w.get_tooltip_text() == "Sort tracks"]
                assert len(sort_btns) == 1, "A playlist view must have a sort button"
                shown = lambda: [r.uri.rsplit("/", 1)[1] for r in win._active_track_rows]
                assert shown() == ["b", "c", "a"], shown()
                from cero.ui.main_window import _sort_tracks
                assert [t.name for t in _sort_tracks(trs, "title", False)] == ["a", "b", "c"]
                assert [t.name for t in _sort_tracks(trs, "duration", True)] == ["b", "a", "c"]
                assert [t.name for t in _sort_tracks(trs, "custom", True)] == ["a", "c", "b"]
                win._render_collection_view(
                    badge="ALBUM", title="Album", subtitle="3 tracks", cover_url="", cover_kind="album",
                    parent_playable=[], tracks=trs, show_cover=False, show_album=False,
                )
                assert not [w for w in _walk(win._center_body) if w.get_tooltip_text() == "Sort tracks"]

                # Filter boxes have a clear button, visible only while they hold text
                for entry in (win._lib_filter_entry, win._exp_filter_entry):
                    clear = entry.get_next_sibling()
                    assert clear.get_tooltip_text() == "Clear", "Filter box without clear button"
                    entry.set_text("")
                    assert not clear.get_visible()
                    entry.set_text("abc")
                    assert clear.get_visible()
                    clear.emit("clicked")
                    assert entry.get_text() == "" and not clear.get_visible()

                # Removing a track from a playlist goes by its position
                removed_calls: list = []

                class _FakeMusic:
                    async def remove_playlist_tracks(self, pid, positions):
                        removed_calls.append((pid, positions))

                real_client = win.ma_service.client
                win.ma_service.client = _NS(music=_FakeMusic())  # type: ignore[assignment]
                try:
                    __import__("asyncio").run(win.ma_service.remove_playlist_track(_NS(item_id="77"), _NS(position=3)))
                    assert removed_calls == [("77", (3,))], removed_calls
                    try:
                        __import__("asyncio").run(win.ma_service.remove_playlist_track(_NS(item_id="77"), _NS(position=None)))
                        raise AssertionError("A track without position cannot be removed")
                    except RuntimeError:
                        pass
                finally:
                    win.ma_service.client = real_client

                # Artist names in rows and cards are links to the artist page
                from cero.ui.widgets import MediaCard, TrackRow, make_artist_links_label
                artist_a, artist_b = _NS(name="Ana"), _NS(name="Beto")
                opened_artists: list = []
                lbl = make_artist_links_label(_NS(artists=[artist_a, artist_b]), opened_artists.append)
                assert "cero:artist:1" in lbl.get_label() and "Ana" in lbl.get_label()
                lbl.emit("activate-link", "cero:artist:1")
                assert opened_artists == [artist_b], opened_artists
                assert make_artist_links_label(_NS(artists=[]), opened_artists.append) is None
                album_item = _NS(name="Alb", media_type=MediaType.ALBUM, artists=[artist_a], uri="library://album/9",
                                 item_id="9", provider="library", provider_mappings=[], year=2020)
                card = MediaCard(album_item, win.ma_service, win.artwork, lambda _i: None, lambda _i: None,
                                 on_open_artist=opened_artists.append)
                links = [w for w in _walk(card) if isinstance(w, Gtk.Label) and "cero:artist:0" in (w.get_label() or "")]
                assert len(links) == 1, "An album card must show its artist as a link"
                links[0].emit("activate-link", "cero:artist:0")
                assert opened_artists[-1] is artist_a

                # Clicking the cover in the player bar goes to the same detail as clicking the title
                opened: list = []
                win._player_bar._on_open_item = opened.append
                bar_cover = [w for w in _walk(win._player_bar) if w.get_tooltip_text() == "Open album / playlist"]
                assert len(bar_cover) == 1, "The player bar cover must open the album / playlist"
                _click(bar_cover[0])
                win._player_bar._on_meta_clicked()
                assert len(opened) == 2 and opened[0] is opened[1] and opened[0] is not None, opened
                assert win._art_lightbox.get_visible() is False, "The bar cover must no longer maximize"

                # Test local device detection via output_protocols and the highlighted list
                from types import SimpleNamespace
                ma.set_local_sendspin_peer_id("PEER_XYZ_123")
                local_p = SimpleNamespace(
                    player_id="uped1745dd",
                    name="CERO",
                    output_protocols=[SimpleNamespace(output_protocol_id="PEER_XYZ_123")],
                )
                remote_p = SimpleNamespace(
                    player_id="upd8aa59821008",
                    name="Soundbar",
                    output_protocols=[],
                )
                assert ma.is_local_player(local_p) is True
                assert ma.is_local_player(remote_p) is False
                win._player_bar._populate_devices([local_p, remote_p], "uped1745dd")

                # Test the quality indicator and the technical details window
                from cero.ma_service import AudioQualityInfo

                sample_state = NowPlayingState(
                        connected=True,
                        player_id="local",
                        player_name="CERO PC",
                        is_local_player=True,
                        playback_state=PlaybackState.PLAYING,
                        title="One More Time",
                        artist="Daft Punk",
                        album="Discovery",
                        uri="library://track/3",
                        duration=320.0,
                        position=45.0,
                        volume=80,
                        shuffle=True,
                        repeat=RepeatMode.ALL,
                        can_seek=True,
                        media_item=track,
                        quality=AudioQualityInfo(
                            tier="HR",
                            badge_text="FLAC • 96 kHz / 24 bit",
                            is_hires=True,
                            is_lossless=True,
                            source_provider="Apple Music",
                            source_codec="FLAC",
                            source_sample_rate=96000,
                            source_bit_depth=24,
                            source_channels=2,
                            output_tier="HR",
                            output_codec="FLAC",
                            output_sample_rate=48000,
                            output_bit_depth=24,
                            output_channels=2,
                            protocol="Sendspin",
                            loudness=-8.4,
                        ),
                    )
                win._player_bar._apply_state(sample_state)
                win._queue_panel._on_state(sample_state)
                assert win._queue_panel._now_fav_btn.get_visible() is True
                assert win._queue_panel._quality_btn.get_visible() is True
                assert win._queue_panel._quality_tier_lbl.get_label() == "HR"
                assert "FLAC • 96 kHz / 24 bit" in (win._queue_panel._quality_btn.get_tooltip_text() or "")
                assert "Daft Punk" in win._player_bar._artist_lbl.get_text()
                assert "Discovery" in win._player_bar._artist_lbl.get_text()
                details = win._queue_panel._open_quality_details()
                assert isinstance(details, Gtk.Popover), "Audio quality must open as a menu like Devices"
                assert details.get_parent() is win._queue_panel._quality_btn
                assert details.get_has_arrow() is False

                credits_win = win._player_bar._open_track_credits()
                assert credits_win is not None
                credits_win.close()

                from music_assistant_models.enums import AlbumType

                test_art = Artist(item_id="a1", provider="library", name="Test Artist", uri="library://artist/a1", provider_mappings=set())
                alb_y = Album(item_id="al1", provider="library", name="Album 2024", uri="library://album/al1", year=2024, album_type=AlbumType.ALBUM, provider_mappings=set())
                alb_n = Album(item_id="al2", provider="library", name="Album NoYear", uri="library://album/al2", album_type=AlbumType.ALBUM, provider_mappings=set())
                sng_y = Album(item_id="sg1", provider="library", name="Single 2025", uri="library://album/sg1", year=2025, album_type=AlbumType.SINGLE, provider_mappings=set())
                win._render_artist_detail(ArtistDetail(artist=test_art, top_tracks=[], albums=[alb_n, alb_y, sng_y]))

                from cero.ui.widgets import MediaCard
                ext_alb = Album(item_id="ext1", provider="apple_music", name="External", uri="apple_music://album/ext1", provider_mappings=set())
                card = MediaCard(ext_alb, ma, art, on_open=lambda _i: None, on_play=lambda _i: None, highlight_library=True)
                assert not card.has_css_class("media-card-in-library")
                assert card._lib_badge.get_visible() is False
                ma._lib_id_by_uri["apple_music://album/ext1"] = (MediaType.ALBUM, "999")
                card._refresh_lib_highlight()
                assert not card.has_css_class("media-card-in-library"), "Library items must not be tinted"
                assert card._lib_badge.get_visible() is True
                ma._lib_id_by_uri.clear()
                card._refresh_lib_highlight()
                assert not card.has_css_class("media-card-in-library")
                assert card._lib_badge.get_visible() is False

                # Logout: first shows the current values; it does not log out until confirmed
                ma.session_store.data.server_url = "http://ma.local:8095"
                ma.session_store.data.username = "tester"
                rows = dict(win._session_summary_rows())
                assert rows["Server"] == "http://ma.local:8095"
                assert rows["Authentication"] == "User: tester"
                dlg = win._show_logout_dialog()
                assert ma.session_store.data.server_url == "http://ma.local:8095", "Must not log out without confirmation"
                dlg.force_close()

                # Auto-refresh on MA events: only if it affects what is shown
                rerenders: list[int] = []
                win._rerender_current_view = lambda: (rerenders.append(1), False)[1]
                win._render_artist_detail(ArtistDetail(artist=test_art, top_tracks=[], albums=[]))
                assert "library://artist/a1" in win._view_uris()
                win._on_media_item_updated("library://album/unrelated")
                assert win._item_refresh_timer == 0, "A change unrelated to the view must not re-render"
                win._on_media_item_updated("library://artist/a1")
                assert win._item_refresh_timer, "A change to the displayed item must schedule a re-render"
                GLib.source_remove(win._item_refresh_timer)
                win._item_refresh_timer = 0

                # Panel widths: only the divider the user is dragging is saved
                sess = ma.session_store.data
                sess.library_width, sess.queue_width = 300, 350
                win._applying_panes = False
                win._dragging_divider = None
                win._outer_paned.set_position(260)  # layout move (no drag): nothing saved
                assert (sess.library_width, sess.queue_width) == (300, 350), (sess.library_width, sess.queue_width)
                win._dragging_divider = "library"
                win._outer_paned.set_position(280)  # user drags the library divider
                assert sess.library_width == 280 and sess.queue_width == 350, (sess.library_width, sess.queue_width)
                win._dragging_divider = None

                win.close()
                a.quit()

            app.connect("activate", _on_activate)
            app.run([])
    finally:
        runner.stop()
    print("[OK] Full GTK4/Libadwaita UI rendering")


def test_clear_other_queues() -> None:
    """Verifies that non-empty queues on other devices are cleared before playing."""
    import asyncio
    from types import SimpleNamespace

    runner = AsyncRunner()
    runner.start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "session.json")
            svc = MAService(runner, store)
            cleared: list[str] = []

            class FakeQueues:
                player_queues = [
                    SimpleNamespace(queue_id="local_pc", items=3, current_item="t1", state=PlaybackState.IDLE),
                    SimpleNamespace(queue_id="speaker_1", items=5, current_item="t2", state=PlaybackState.PLAYING),
                    SimpleNamespace(queue_id="speaker_2", items=0, current_item=None, state=PlaybackState.PAUSED),
                    SimpleNamespace(queue_id="speaker_empty", items=0, current_item=None, state=PlaybackState.IDLE),
                ]

                async def clear(self, qid: str) -> None:
                    cleared.append(qid)

            svc.client = SimpleNamespace(player_queues=FakeQueues())  # type: ignore[assignment]
            asyncio.run(svc._clear_other_queues("local_pc"))
            assert cleared == ["speaker_1", "speaker_2"], f"Unexpected queues cleared: {cleared}"
    finally:
        runner.stop()
    print("[OK] Clearing other devices' queues before playing")


def test_library_toggle_and_index() -> None:
    """Verifies library detection (including via provider_mappings) and add/remove."""
    import asyncio
    from types import SimpleNamespace
    from music_assistant_models.enums import MediaType
    from music_assistant_models.errors import MediaNotFoundError
    from music_assistant_models.media_items import Album, ProviderMapping
    from cero.ma_service import HomeContent

    runner = AsyncRunner()
    runner.start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "session.json")
            svc = MAService(runner, store)
            ops: list[tuple[str, object]] = []

            lib_album = Album(
                item_id="55",
                provider="library",
                name="Discovery",
                uri="library://album/55",
                provider_mappings={
                    ProviderMapping(
                        item_id="697194953",
                        provider_domain="apple_music",
                        provider_instance="apple_music--1",
                    )
                },
            )
            ext_album = Album(
                item_id="697194953",
                provider="apple_music",
                name="Discovery",
                uri="apple_music://album/697194953",
                provider_mappings={
                    ProviderMapping(
                        item_id="697194953",
                        provider_domain="apple_music",
                        provider_instance="apple_music--1",
                    )
                },
            )

            class FakeMusic:
                """Behaves like the real server: a removed library object no longer exists."""

                def __init__(self) -> None:
                    self.server_ids = {"55"}
                    self.next_id = 56

                async def remove_item_from_library(self, mt: MediaType, iid: str, recursive: bool = True) -> None:
                    ops.append(("remove", (mt, iid)))
                    self.server_ids.discard(iid)

                async def add_item_to_library(self, item: object) -> dict:
                    uri = item if isinstance(item, str) else getattr(item, "uri", "")
                    ops.append(("add", uri))
                    if uri.startswith("library://") and uri.rsplit("/", 1)[1] not in self.server_ids:
                        raise MediaNotFoundError("The requested media item could not be found.")
                    new_id = str(self.next_id)
                    self.next_id += 1
                    self.server_ids.add(new_id)
                    return {
                        "item_id": new_id,
                        "media_type": "album",
                        "provider": "library",
                        "uri": f"library://album/{new_id}",
                        "provider_mappings": [
                            {
                                "item_id": "697194953",
                                "provider_domain": "apple_music",
                                "provider_instance": "apple_music--1",
                            }
                        ],
                    }

            svc.client = SimpleNamespace(music=FakeMusic(), connection=SimpleNamespace(connected=False))  # type: ignore[assignment]
            svc.rebuild_library_index(HomeContent(albums=[lib_album]))

            assert svc.is_in_library(lib_album) is True
            assert svc.is_in_library(ext_album) is True

            res_rem = asyncio.run(svc.toggle_library_item(ext_album))
            assert res_rem is False
            assert ops[-1] == ("remove", (MediaType.ALBUM, "55"))
            assert svc.is_in_library(ext_album) is False
            assert svc.is_in_library(lib_album) is False

            res_add = asyncio.run(svc.toggle_library_item(ext_album))
            assert res_add is True
            assert ops[-1] == ("add", "apple_music://album/697194953")
            assert svc.is_in_library(ext_album) is True
            assert svc.is_in_library(lib_album) is True

            # Remove and re-add from the SAME stale library object (what an artist page card holds):
            # the removed library item no longer exists, so it must be re-added from its provider URI
            asyncio.run(svc.toggle_library_item(lib_album))
            assert ops[-1] == ("remove", (MediaType.ALBUM, "56")), ops[-1]
            assert svc.is_in_library(lib_album) is False
            assert svc.is_in_library(ext_album) is False
            # Removing from the library clears the favorite flag locally, without a server call
            assert not any(o[0] == "unfavorite" for o in ops)
            assert getattr(lib_album, "favorite", False) is False

            res_readd = asyncio.run(svc.toggle_library_item(lib_album))
            assert res_readd is True, "Re-adding a removed library album must succeed"
            assert ops[-1] == ("add", "apple_music://album/697194953"), ops[-1]
            assert svc.is_in_library(lib_album) is True
            assert svc.is_in_library(ext_album) is True
            # ... and the next removal must delete the NEW library item, not the stale id
            assert svc.find_library_id(lib_album) == (MediaType.ALBUM, "57"), svc.find_library_id(lib_album)
            asyncio.run(svc.toggle_library_item(lib_album))
            assert ops[-1] == ("remove", (MediaType.ALBUM, "57")), ops[-1]
            assert svc.is_in_library(lib_album) is False

            # Optimistic add: the icon shows "in library" while the (slow) server is still working,
            # a second click sends no second request, and a failure reverts the icon
            import threading as _threading

            class SlowMusic:
                def __init__(self, fail: bool) -> None:
                    self.fail, self.calls = fail, 0
                    self.release = _threading.Event()

                async def add_item_to_library(self, item: object) -> dict:
                    self.calls += 1
                    await asyncio.get_running_loop().run_in_executor(None, self.release.wait, 10)
                    if self.fail:
                        raise MediaNotFoundError("server error")
                    return {"item_id": "90", "media_type": "album", "provider": "library",
                            "uri": "library://album/90", "provider_mappings": []}

            for fail in (False, True):
                svc2 = MAService(runner, store)
                slow = SlowMusic(fail)
                svc2.client = SimpleNamespace(music=slow, connection=SimpleNamespace(connected=False))  # type: ignore[assignment]
                changes: list[bool] = []
                svc2._emit_library_changed = lambda s=svc2: changes.append(s.is_in_library(ext_album))  # type: ignore[method-assign]
                fut = runner.submit(svc2.toggle_library_item(ext_album))
                deadline = time.time() + 3
                while slow.calls == 0 and time.time() < deadline:
                    time.sleep(0.01)
                assert slow.calls == 1 and not fut.done(), "the server request must still be running"
                assert svc2.is_in_library(ext_album) is True, "icon must show 'in library' before the server answers"
                assert changes == [True], f"UI must be notified right away: {changes}"
                second = runner.submit(svc2.toggle_library_item(ext_album)).result(timeout=3)
                assert second is True and slow.calls == 1, "a second click must not send another request"
                slow.release.set()
                if fail:
                    try:
                        fut.result(timeout=5)
                        raise AssertionError("the failure must be reported")
                    except MediaNotFoundError:
                        pass
                    assert svc2.is_in_library(ext_album) is False, "a failed add must revert the icon"
                    assert changes[-1] is False
                else:
                    assert fut.result(timeout=5) is True
                    assert svc2.is_in_library(ext_album) is True and svc2._pending_lib_adds == set()
    finally:
        runner.stop()
    print("[OK] Library indicator and add/remove from the library")


def test_pane_fit() -> None:
    """Saved panel widths are applied only as far as the available width allows."""
    from cero.ui.main_window import CENTER_MIN_W, fit_pane_sizes

    # Plenty of space: preferences applied as saved
    assert fit_pane_sizes(1600, 8, 400, 400, True) == (400, 400)
    # Queue closed: queue width is 0 and only the library counts
    assert fit_pane_sizes(1600, 8, 400, 400, False) == (400, 0)
    # Not enough space: the queue shrinks first (down to its minimum), center keeps its minimum
    lib, queue = fit_pane_sizes(1000, 8, 400, 400, True, lib_min=285, queue_min=296)
    assert queue == 296 and lib + queue + CENTER_MIN_W + 16 <= 1000 and lib >= 285, (lib, queue)
    # Even less space: then the library shrinks, never below the content minimum
    lib, queue = fit_pane_sizes(800, 8, 400, 400, True, lib_min=285, queue_min=296)
    assert (lib, queue) == (285, 296), (lib, queue)
    # Out-of-range preferences are clamped to the allowed limits
    assert fit_pane_sizes(3000, 8, 9999, 10, True) == (520, 240)
    print("[OK] Panel widths: saved preferences fitted to the available space")


if __name__ == "__main__":
    test_url_normalization()
    test_session_store()
    test_pane_fit()
    test_css_and_vector_assets()
    test_gstreamer_pcm_and_flac_engine()
    test_sendspin_client_setup()
    test_mpris_with_playerctl()
    test_clear_other_queues()
    test_library_toggle_and_index()
    test_gtk4_ui_smoke()
    print("\n=== ALL TESTS PASSED ===")
