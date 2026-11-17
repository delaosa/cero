"""Session persistence and library sorting (~/.config/cero/)."""

from __future__ import annotations

import json
import os
import re
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

VALID_SORT_MODES = ("year", "alpha", "recent", "creator")
VALID_SORT_DIRECTIONS = ("asc", "desc")

_YEAR_REGEX = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _parse_date_key(val: Any) -> int:
    """Converts a datetime/date/ISO string into a comparable YYYYMMDD integer (or 0 if invalid)."""
    if val is None:
        return 0
    if hasattr(val, "year"):
        y = int(val.year)
        m = int(getattr(val, "month", 0) or 0)
        d = int(getattr(val, "day", 0) or 0)
        return y * 10000 + m * 100 + d if y > 0 else 0
    if isinstance(val, (int, float)) and val > 0:
        try:
            dt = datetime.fromtimestamp(float(val))
            return dt.year * 10000 + dt.month * 100 + dt.day
        except Exception:
            return 0
    if isinstance(val, str):
        s = val.strip()
        if len(s) >= 10 and s[:4].isdigit() and s[5:7].isdigit() and s[8:10].isdigit():
            return int(s[:4]) * 10000 + int(s[5:7]) * 100 + int(s[8:10])
        if len(s) >= 7 and s[:4].isdigit() and s[5:7].isdigit():
            return int(s[:4]) * 10000 + int(s[5:7]) * 100
        if len(s) >= 4 and s[:4].isdigit():
            return int(s[:4]) * 10000
    return 0


def extract_item_release_date(item: Any) -> int:
    """Extracts the release date (YYYYMMDD), falling back to year * 10000."""
    if item is None:
        return 0
    if isinstance(item, dict):
        if rd := _parse_date_key(item.get("release_date")):
            return rd
        if y := item.get("year"):
            return int(y) * 10000
        return 0

    meta = getattr(item, "metadata", None)
    if meta is not None:
        if rd := _parse_date_key(getattr(meta, "release_date", None)):
            return rd

    if (yr := getattr(item, "year", None)) and isinstance(yr, int) and yr > 0:
        return yr * 10000

    # For albums, never mistake library `last_refresh` / `date_added` for a release date
    mt_val = getattr(getattr(item, "media_type", None), "value", getattr(item, "media_type", None))
    if mt_val != "album":
        if meta is not None:
            if rd := _parse_date_key(getattr(meta, "last_refresh", None)):
                return rd

        for attr in ("date_added", "last_played", "timestamp_modified", "timestamp_added"):
            if rd := _parse_date_key(getattr(item, attr, None)):
                return rd

    for text_attr in ("name", "version"):
        txt = getattr(item, text_attr, None)
        if isinstance(txt, str) and txt:
            m = _YEAR_REGEX.search(txt)
            if m:
                return int(m.group(1)) * 10000

    return 0


def extract_item_year(item: Any) -> int:
    """Extracts the release year (or the last update/date for playlists)."""
    rd = extract_item_release_date(item)
    if rd >= 10000:
        return rd // 10000
    if (mod_rank := getattr(item, "_ma_modified_rank", None)) is not None:
        return int(mod_rank)
    return 0


def get_config_dir() -> Path:
    """Returns the application's XDG config directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    cfg = base / "cero"
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg


def get_cache_dir() -> Path:
    """Returns the XDG cache directory for cover art."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    root = base / "cero"
    cache = root / "artwork"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def get_library_cache_path() -> Path:
    """Returns the on-disk cache path used to open the library instantly."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    d = base / "cero"
    d.mkdir(parents=True, exist_ok=True)
    return d / "library_cache.json"


def default_player_name() -> str:
    """Friendly name used to identify this PC in Music Assistant."""
    host = socket.gethostname().split(".")[0] or "Linux"
    return f"CERO ({host})"


@dataclass
class SessionData:
    """Music Assistant session credentials and sorting preferences."""

    server_url: str = ""
    token: str = ""
    username: str = ""
    selected_player_id: str = ""
    local_volume: int = 85
    local_muted: bool = False
    output_delay_ms: float = 0.0
    sendspin_private_key_b64u: str = ""
    local_player_name: str = field(default_factory=default_player_name)

    # Panel, filter and sorting preferences
    library_width: int = 285
    library_collapsed: bool = False
    queue_width: int = 300
    queue_visible: bool = False
    library_active_tab: str = "albums"
    explorer_active_section: str = "discover"
    library_sort_modes: dict[str, str] = field(
        default_factory=lambda: {
            "playlists": "recent",
            "albums": "year",
            "artists": "alpha",
            "radios": "alpha",
        }
    )
    library_sort_directions: dict[str, str] = field(
        default_factory=lambda: {
            "playlists": "desc",
            "albums": "desc",
            "artists": "asc",
            "radios": "asc",
        }
    )
    pinned_library_items: dict[str, list[str]] = field(default_factory=dict)

    @property
    def has_credentials(self) -> bool:
        return bool(self.server_url.strip())


class SessionStore:
    """Atomic read/write of ~/.config/cero/session.json."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (get_config_dir() / "session.json")
        self.data = self._load()
        self._sanitize()

    def _load(self) -> SessionData:
        env_url = os.environ.get("MA_SERVER_URL", "").strip()
        env_token = os.environ.get("MA_TOKEN", "").strip()
        if not self.path.exists():
            return SessionData(server_url=env_url, token=env_token)
        try:
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            known = {k: v for k, v in raw.items() if k in SessionData.__dataclass_fields__}
            session = SessionData(**known)
            if env_url:
                session.server_url = env_url
            if env_token:
                session.token = env_token
            return session
        except Exception:
            return SessionData(server_url=env_url, token=env_token)

    def _sanitize(self) -> None:
        self.data.library_width = max(210, min(520, int(self.data.library_width)))
        self.data.library_collapsed = bool(self.data.library_collapsed)
        self.data.queue_width = max(240, min(520, int(self.data.queue_width)))
        self.data.queue_visible = bool(self.data.queue_visible)
        if not isinstance(self.data.library_sort_modes, dict):
            self.data.library_sort_modes = {}
        if not isinstance(self.data.library_sort_directions, dict):
            self.data.library_sort_directions = {}
        if not isinstance(self.data.pinned_library_items, dict):
            self.data.pinned_library_items = {}
        else:
            for cat, keys in list(self.data.pinned_library_items.items()):
                if not isinstance(keys, list):
                    self.data.pinned_library_items[cat] = []
                else:
                    self.data.pinned_library_items[cat] = [str(k) for k in keys if k]
        for cat, mode in list(self.data.library_sort_modes.items()):
            if mode not in VALID_SORT_MODES:
                self.data.library_sort_modes[cat] = "year" if cat == "albums" else "alpha"

    def save(self) -> None:
        self._sanitize()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(self.data), fh, indent=2)
        tmp.replace(self.path)

    def clear_credentials(self) -> None:
        self.data.token = ""
        self.save()

    # --- Sorting and direction (Ascending / Descending) ---

    def get_library_sort_mode(self, category: str) -> str:
        default_mode = "year" if category == "albums" else ("recent" if category == "playlists" else "alpha")
        raw = self.data.library_sort_modes.get(category, default_mode)
        if category == "playlists" and raw == "year":
            return "recent"
        return raw if raw in VALID_SORT_MODES else default_mode

    def get_library_sort_direction(self, category: str) -> str:
        default_dir = "desc" if self.get_library_sort_mode(category) in ("recent", "year") else "asc"
        d = self.data.library_sort_directions.get(category, default_dir)
        return d if d in VALID_SORT_DIRECTIONS else default_dir

    def set_library_sort_mode(
        self, category: str, mode: str, direction: str | None = None
    ) -> None:
        if mode not in VALID_SORT_MODES:
            return
        prev_mode = self.get_library_sort_mode(category)
        self.data.library_sort_modes[category] = mode
        if direction in VALID_SORT_DIRECTIONS:
            self.data.library_sort_directions[category] = direction
        elif prev_mode != mode and category not in self.data.library_sort_directions:
            self.data.library_sort_directions[category] = (
                "desc" if mode in ("recent", "year") else "asc"
            )
        self.save()

    def set_library_sort_direction(self, category: str, direction: str) -> None:
        if direction in VALID_SORT_DIRECTIONS:
            self.data.library_sort_directions[category] = direction
            self.save()

    def toggle_library_sort_direction(self, category: str) -> str:
        cur = self.get_library_sort_direction(category)
        nxt = "desc" if cur == "asc" else "asc"
        self.data.library_sort_directions[category] = nxt
        self.save()
        return nxt

    def get_pinned_keys(self, category: str) -> list[str]:
        return list(self.data.pinned_library_items.get(category, ()))

    def is_pinned(self, category: str, key: str) -> bool:
        return bool(key) and key in self.data.pinned_library_items.get(category, ())

    def toggle_pin(self, category: str, key: str) -> bool:
        """Pins or unpins an item in `category` and persists the change. Returns new state."""
        if not key:
            return False
        pinned = list(self.data.pinned_library_items.get(category, ()))
        if key in pinned:
            pinned.remove(key)
            now_pinned = False
        else:
            pinned.insert(0, key)
            now_pinned = True
        self.data.pinned_library_items[category] = pinned
        self.save()
        return now_pinned

    def sort_library_entries(
        self,
        category: str,
        entries: list[Any],
        *,
        key_fn: Callable[[Any], str] | None = None,
        name_fn: Callable[[Any], str],
        creator_fn: Callable[[Any], str],
        timestamp_fn: Callable[[Any], float],
        year_fn: Callable[[Any], int] | None = None,
    ) -> list[Any]:
        """Sorts entries by criterion (year, alpha, recent, creator) and direction (asc/desc)."""
        mode = self.get_library_sort_mode(category)
        direction = self.get_library_sort_direction(category)
        reverse = direction == "desc"

        if mode == "creator":
            ordered = sorted(
                entries,
                key=lambda x: (creator_fn(x).casefold(), name_fn(x).casefold()),
                reverse=reverse,
            )
        elif mode == "recent":
            ordered = sorted(entries, key=timestamp_fn, reverse=reverse)
        elif mode == "year":
            y_fn = year_fn or extract_item_release_date
            with_year = [e for e in entries if y_fn(e) > 0]
            without_year = [e for e in entries if y_fn(e) <= 0]
            with_year.sort(
                key=lambda x: (y_fn(x), name_fn(x).casefold()),
                reverse=reverse,
            )
            without_year.sort(key=lambda x: name_fn(x).casefold())
            ordered = with_year + without_year
        else:
            ordered = sorted(entries, key=lambda x: name_fn(x).casefold(), reverse=reverse)

        pinned_keys = self.data.pinned_library_items.get(category)
        if key_fn is not None and pinned_keys:
            rank = {k: i for i, k in enumerate(pinned_keys)}
            pinned = [e for e in ordered if key_fn(e) in rank]
            if pinned:
                pinned.sort(key=lambda e: rank[key_fn(e)])
                rest = [e for e in ordered if key_fn(e) not in rank]
                return pinned + rest
        return ordered
