"""Bridge between a dedicated background asyncio loop and the main GTK/GLib thread."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

from gi.repository import GLib

logger = logging.getLogger(__name__)
T = TypeVar("T")


class AsyncRunner:
    """Runs asyncio coroutines in a daemon thread and dispatches UI callbacks via GLib.idle_add."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="cero-asyncio", daemon=True
        )
        self._started = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(
        self,
        coro: Coroutine[Any, Any, T],
        on_success: Callable[[T], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> Future[T]:
        """Submits a coroutine to the asyncio loop and delivers the result on the GTK thread."""
        if not self._started:
            self.start()
        fut: Future[T] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        if on_success is not None or on_error is not None:

            def _done(done_fut: Future[T]) -> None:
                try:
                    result = done_fut.result()
                except asyncio.CancelledError:
                    return
                except BaseException as exc:
                    if on_error is not None:
                        GLib.idle_add(self._invoke_ui, on_error, exc)
                    else:
                        logger.warning("Async task failed: %s", exc)
                    return
                if on_success is not None:
                    GLib.idle_add(self._invoke_ui, on_success, result)

            fut.add_done_callback(_done)
        return fut

    @staticmethod
    def _invoke_ui(cb: Callable[[Any], None], arg: Any) -> bool:
        cb(arg)
        return GLib.SOURCE_REMOVE

    def call_ui(self, cb: Callable[..., None], *args: Any) -> None:
        """Safely runs a function on the main GTK thread."""

        def _wrap() -> bool:
            cb(*args)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_wrap)

    def stop(self) -> None:
        if not self._started or not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)
