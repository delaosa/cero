"""Asynchronous cover art loading with a shared HTTP session, disk cache and vector placeholders."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import aiohttp
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from .async_runner import AsyncRunner
from .session import SessionStore, get_cache_dir

logger = logging.getLogger(__name__)

_GRADIENTS: list[tuple[str, str]] = [
    ("#450AF5", "#8E8EE5"),
    ("#475569", "#1E3A8A"),
    ("#E8115B", "#501931"),
    ("#0284C7", "#0F172A"),
    ("#E13300", "#731A00"),
    ("#8D67AB", "#3B2252"),
    ("#0D9488", "#134E4A"),
    ("#BC5900", "#4A2400"),
]


@lru_cache(maxsize=256)
def make_placeholder_texture(title: str = "", size: int = 160, kind: str = "album") -> Gdk.Texture:
    """Generates an elegant vector cover with a deterministic gradient and a music icon."""
    if kind == "cero":
        # Monochrome CERO emblem (mirrors cero.svg) for empty queue / idle poster
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 180 180">
          <defs>
            <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#262B36"/>
              <stop offset="100%" stop-color="#101319"/>
            </linearGradient>
            <linearGradient id="ring" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stop-color="#E5E9F0"/>
              <stop offset="100%" stop-color="#7B879B"/>
            </linearGradient>
          </defs>
          <rect width="180" height="180" fill="url(#bg)"/>
          <circle cx="90" cy="90" r="46" fill="none" stroke="url(#ring)" stroke-width="14"/>
          <polygon points="81,71 81,109 113,90" fill="#FFFFFF" stroke="#FFFFFF" stroke-width="4" stroke-linejoin="round"/>
        </svg>"""
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(svg.encode("utf-8")))

    seed = int(hashlib.md5((title or kind).encode("utf-8")).hexdigest()[:8], 16)
    if kind == "favorites":
        c1, c2 = ("#94A3B8", "#A5B4FC")
    elif kind == "radio":
        c1, c2 = ("#0284C7", "#38BDF8")
    else:
        c1, c2 = _GRADIENTS[seed % len(_GRADIENTS)]

    if kind == "favorites":
        glyph = (
            '<path fill="#FFFFFF" opacity="0.95" transform="translate(52,52) scale(3.2)" '
            'd="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 '
            '3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 '
            '6.86-8.55 11.54L12 21.35z"/>'
        )
    elif kind == "artist":
        initial = html.escape((title.strip()[:1] or "♪").upper())
        glyph = (
            f'<text x="90" y="112" text-anchor="middle" fill="#FFFFFF" opacity="0.9" '
            f'font-family="sans-serif" font-weight="700" font-size="72">{initial}</text>'
        )
    else:
        glyph = (
            '<path fill="#FFFFFF" opacity="0.78" transform="translate(54,50) scale(3.2)" '
            'd="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 '
            '4-4V7h4V3h-6z"/>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 180 180">
      <defs>
        <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="{c1}"/>
          <stop offset="100%" stop-color="{c2}"/>
        </linearGradient>
      </defs>
      <rect width="180" height="180" fill="url(#g)"/>
      {glyph}
    </svg>"""
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(svg.encode("utf-8")))


class ArtworkLoader:
    """Downloads and caches Music Assistant cover art."""

    def __init__(
        self,
        runner: AsyncRunner,
        session_store: SessionStore,
        on_cached_file: Callable[[str, str], None] | None = None,
    ) -> None:
        self.runner = runner
        self.session_store = session_store
        self._on_cached_file = on_cached_file
        self._cache_dir = get_cache_dir()
        self._mem_cache: OrderedDict[str, Gdk.Texture] = OrderedDict()
        self._mem_limit = 320
        self._inflight: dict[str, list[Callable[[Gdk.Texture], None]]] = {}
        self._http_session: aiohttp.ClientSession | None = None
        self._sem: asyncio.Semaphore | None = None

    def _disk_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return self._cache_dir / f"{digest}.img"

    def _put_mem(self, url: str, tex: Gdk.Texture) -> None:
        self._mem_cache[url] = tex
        self._mem_cache.move_to_end(url)
        while len(self._mem_cache) > self._mem_limit:
            self._mem_cache.popitem(last=False)

    def invalidate(self, url: str) -> None:
        """Drops the in-memory and on-disk image to force a fresh download."""
        if not url:
            return
        self._mem_cache.pop(url, None)
        try:
            self._disk_path(url).unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _set_widget_paintable(widget: Gtk.Widget, paintable: Gdk.Paintable) -> None:
        if isinstance(widget, Gtk.Image):
            widget.set_from_paintable(paintable)
        elif isinstance(widget, Gtk.Picture):
            widget.set_paintable(paintable)

    def load_into_picture(
        self,
        widget: Gtk.Widget,
        url: str,
        *,
        title: str = "",
        size: int = 160,
        kind: str = "album",
    ) -> None:
        """Immediately sets the placeholder (or RAM cache) and downloads in the background."""
        setattr(widget, "_target_art_url", url)
        if not url:
            self._set_widget_paintable(widget, make_placeholder_texture(title, size=size, kind=kind))
            return

        if cached := self._mem_cache.get(url):
            self._mem_cache.move_to_end(url)
            self._set_widget_paintable(widget, cached)
            disk_file = self._disk_path(url)
            if self._on_cached_file and disk_file.exists():
                self._on_cached_file(url, disk_file.as_uri())
            return

        self._set_widget_paintable(widget, make_placeholder_texture(title, size=size, kind=kind))

        def _apply(tex: Gdk.Texture) -> None:
            if getattr(widget, "_target_art_url", None) == url:
                self._set_widget_paintable(widget, tex)

        if url in self._inflight:
            self._inflight[url].append(_apply)
            return

        self._inflight[url] = [_apply]
        self.runner.submit(
            # Twice the display size leaves room for HiDPI; servers often ignore the requested size
            self._fetch_and_prepare(url, min(max(size * 2, 128), 1000)),
            on_success=lambda data: self._on_bytes_ready(url, data),
            on_error=lambda _e: self._inflight.pop(url, None),
        )

    @staticmethod
    def _downscale(data: bytes, max_dim: int) -> bytes:
        """Shrinks the image (in a background thread) when it is larger than needed.

        A decoded texture takes width x height x 4 bytes, so a 1200 px cover shown in a
        140 px card would waste ~5 MB of RAM.
        """
        if len(data) < 4_000:
            return data
        try:
            resized = False
            loader = GdkPixbuf.PixbufLoader()

            def _on_size(ldr: GdkPixbuf.PixbufLoader, w: int, h: int) -> None:
                nonlocal resized
                if w > max_dim or h > max_dim:
                    scale = max_dim / max(w, h)
                    ldr.set_size(max(1, int(w * scale)), max(1, int(h * scale)))
                    resized = True

            loader.connect("size-prepared", _on_size)
            loader.write(data)
            loader.close()
            if not resized:
                return data
            pixbuf = loader.get_pixbuf()
            if pixbuf is not None:
                if pixbuf.get_has_alpha():
                    ok, out = pixbuf.save_to_bufferv("png", [], [])
                else:
                    ok, out = pixbuf.save_to_bufferv("jpeg", ["quality"], ["88"])
                if ok and out:
                    return out
        except Exception:
            pass
        return data

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            connector = aiohttp.TCPConnector(limit=12, ttl_dns_cache=300)
            self._http_session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=10),
            )
        if self._sem is None:
            self._sem = asyncio.Semaphore(6)
        return self._http_session

    async def _fetch_and_prepare(self, url: str, max_dim: int = 400) -> bytes | None:
        disk = self._disk_path(url)
        if disk.exists():
            try:
                return await asyncio.to_thread(disk.read_bytes)
            except Exception:
                pass

        session = await self._get_session()
        assert self._sem is not None

        headers: dict[str, str] = {}
        srv = self.session_store.data.server_url.rstrip("/")
        tok = self.session_store.data.token.strip()
        if srv and tok and url.startswith(srv):
            headers["Authorization"] = f"Bearer {tok}"

        async with self._sem:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
                if not raw:
                    return None
                scaled = await asyncio.to_thread(self._downscale, raw, max_dim)
                try:
                    await asyncio.to_thread(disk.write_bytes, scaled)
                except Exception:
                    pass
                return scaled

    async def fetch_full_resolution_bytes(self, url: str) -> bytes | None:
        """Downloads the cover art at original resolution (size=0 when using imageproxy) without rescaling."""
        if not url:
            return None
        full_url = url
        if "imageproxy" in full_url and "size=" in full_url:
            import re

            full_url = re.sub(r"([?&])size=\d+", r"\g<1>size=0", full_url)

        session = await self._get_session()
        headers: dict[str, str] = {}
        srv = self.session_store.data.server_url.rstrip("/")
        tok = self.session_store.data.token.strip()
        if srv and tok and full_url.startswith(srv):
            headers["Authorization"] = f"Bearer {tok}"

        async with session.get(full_url, headers=headers) as resp:
            if resp.status != 200:
                return None
            return await resp.read()

    def _on_bytes_ready(self, url: str, data: bytes | None) -> None:
        waiters = self._inflight.pop(url, [])
        if not data:
            return
        try:
            tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except Exception as err:
            logger.debug("Could not decode image (%s): %s", url, err)
            return
        self._put_mem(url, tex)
        disk = self._disk_path(url)
        if self._on_cached_file and disk.exists():
            self._on_cached_file(url, disk.as_uri())
        for cb in waiters:
            cb(tex)
