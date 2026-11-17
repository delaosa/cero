"""Native Adw.Application that coordinates the UI, Music Assistant, Sendspin and MPRIS2."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, Gtk

from .. import __app_id__
from ..artwork import ArtworkLoader
from ..async_runner import AsyncRunner
from ..ma_service import MAService
from ..mpris import MprisServer
from ..sendspin_player import LocalSendspinService
from ..session import SessionStore
from .main_window import MainWindow

# Without a compositor (e.g. i3 on X11) the margin GTK reserves around a menu for its shadow
# cannot be transparent and is painted in Adwaita's grey, leaving a rounded card inside a
# grey box. There, menus become a single flat rectangle with one border instead.
_NO_COMPOSITOR_CSS = """
popover.background { background-color: #1c222e; box-shadow: none; padding: 0; margin: 0; }
popover.background > contents {
  box-shadow: none; border: 1px solid #2e384b; border-radius: 0; margin: 0;
}
"""


class CeroApp(Adw.Application):
    """Main GTK4 / Libadwaita application instance."""

    def __init__(self) -> None:
        super().__init__(
            application_id=__app_id__,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.runner = AsyncRunner()
        self.session_store = SessionStore()
        self.ma_service = MAService(self.runner, self.session_store)
        self.sendspin_service = LocalSendspinService(
            self.session_store,
            on_pairing_code=self._on_pairing_code,
        )
        self.ma_service.set_local_sendspin_peer_id(self.sendspin_service.peer_id)
        self.ma_service.set_local_output_format_provider(self.sendspin_service.get_active_format)

        self.mpris = MprisServer(
            self.ma_service,
            on_raise=self._raise_window,
            on_quit=self.quit,
        )
        self.artwork = ArtworkLoader(
            self.runner,
            self.session_store,
            on_cached_file=self.mpris.set_local_artwork_file,
        )
        self._window: MainWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        style_mgr = Adw.StyleManager.get_default()
        style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        css = Gtk.CssProvider()
        css_path = Path(__file__).resolve().parent.parent / "style.css"
        css.load_from_path(str(css_path))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            if not display.is_composited():
                flat = Gtk.CssProvider()
                flat.load_from_string(_NO_COMPOSITOR_CSS)
                Gtk.StyleContext.add_provider_for_display(
                    display, flat, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1
                )
        # Window icon by name: scripts/install-launcher.sh installs it into the user's icon theme
        Gtk.Window.set_default_icon_name(__app_id__)

        self.runner.start()
        self.mpris.start()

    def do_activate(self) -> None:
        if self._window is None:
            self._window = MainWindow(
                self,
                self.ma_service,
                self.sendspin_service,
                self.artwork,
            )
        self._window.present()

    def do_shutdown(self) -> None:
        self.mpris.stop()
        try:
            fut1 = self.runner.submit(self.sendspin_service.stop())
            fut2 = self.runner.submit(self.ma_service.disconnect())
            fut1.result(timeout=2.0)
            fut2.result(timeout=2.0)
        except Exception:
            pass
        self.runner.stop()
        Adw.Application.do_shutdown(self)

    def _raise_window(self) -> None:
        if self._window is not None:
            self.runner.call_ui(self._window.present)

    def _on_pairing_code(self, code: str | None) -> None:
        if self._window is not None:
            self.runner.call_ui(self._window.show_pairing_code, code)
