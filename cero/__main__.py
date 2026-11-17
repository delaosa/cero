"""Executable entry point for the CERO player."""

from __future__ import annotations

import logging
import os
import sys

from gi.repository import GLib

from . import __app_id__, __app_name__
from .ui.app import CeroApp


def main() -> int:
    log_level = logging.DEBUG if os.environ.get("CERO_DEBUG") else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Without this, X11 reports WM_CLASS "python" and the window is not matched to the launcher
    GLib.set_prgname(__app_id__)
    GLib.set_application_name(__app_name__)
    app = CeroApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
