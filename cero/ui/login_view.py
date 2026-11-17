"""Minimal, elegant connection view for Music Assistant (no administration settings)."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ..ma_service import DiscoveredServer, MAService


class LoginView(Gtk.Box):
    """Clean form to sign in to Music Assistant just once."""

    def __init__(self, ma_service: MAService, on_connected: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ma_service = ma_service
        self._on_connected = on_connected
        self._use_token_mode = False

        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        card.add_css_class("login-card")
        card.set_size_request(430, -1)
        self.append(card)

        # Brand header
        brand_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        brand_box.set_halign(Gtk.Align.CENTER)
        title = Gtk.Label(label="CERO")
        title.add_css_class("hero-title")
        subtitle = Gtk.Label(label="CERO Electron, Reproduce Only — Music Assistant")
        subtitle.add_css_class("subtitle-dim")
        brand_box.append(title)
        brand_box.append(subtitle)
        card.append(brand_box)

        # Servers automatically discovered via mDNS on the local network
        self._discovered_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._discovered_box.set_visible(False)
        disc_lbl = Gtk.Label(label="Servers discovered on your network:", xalign=0.0)
        disc_lbl.add_css_class("muted-label")
        self._discovered_box.append(disc_lbl)
        self._discovered_chips = Gtk.FlowBox()
        self._discovered_chips.set_selection_mode(Gtk.SelectionMode.NONE)
        self._discovered_chips.set_max_children_per_line(2)
        self._discovered_chips.set_column_spacing(6)
        self._discovered_chips.set_row_spacing(6)
        self._discovered_box.append(self._discovered_chips)
        card.append(self._discovered_box)

        # Server URL field
        url_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        url_lbl = Gtk.Label(label="Server URL", xalign=0.0)
        url_lbl.add_css_class("muted-label")
        self._url_entry = Gtk.Entry()
        self._url_entry.add_css_class("login-entry")
        self._url_entry.set_placeholder_text("http://192.168.1.50:8095")
        saved_url = ma_service.session_store.data.server_url
        if saved_url:
            self._url_entry.set_text(saved_url)
        self._url_entry.connect("activate", lambda _e: self._submit())
        url_box.append(url_lbl)
        url_box.append(self._url_entry)
        card.append(url_box)

        # Mode selector: Username/Password vs Token
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._mode_creds_btn = Gtk.Button(label="Username & Password")
        self._mode_creds_btn.add_css_class("filter-chip")
        self._mode_creds_btn.add_css_class("active")
        self._mode_token_btn = Gtk.Button(label="Access Token")
        self._mode_token_btn.add_css_class("filter-chip")
        self._mode_creds_btn.connect("clicked", lambda _b: self._set_mode(False))
        self._mode_token_btn.connect("clicked", lambda _b: self._set_mode(True))
        mode_row.append(self._mode_creds_btn)
        mode_row.append(self._mode_token_btn)
        card.append(mode_row)

        # Username + Password container
        self._creds_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._user_entry = Gtk.Entry()
        self._user_entry.add_css_class("login-entry")
        self._user_entry.set_placeholder_text("Username")
        if ma_service.session_store.data.username:
            self._user_entry.set_text(ma_service.session_store.data.username)
        self._user_entry.connect("activate", lambda _e: self._pass_entry.grab_focus())

        self._pass_entry = Gtk.PasswordEntry()
        self._pass_entry.add_css_class("login-entry")
        self._pass_entry.set_show_peek_icon(True)
        self._pass_entry.props.placeholder_text = "Password"
        self._pass_entry.connect("activate", lambda _e: self._submit())

        self._creds_box.append(self._user_entry)
        self._creds_box.append(self._pass_entry)
        card.append(self._creds_box)

        # Long-Lived Token container
        self._token_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._token_box.set_visible(False)
        self._token_entry = Gtk.PasswordEntry()
        self._token_entry.add_css_class("login-entry")
        self._token_entry.set_show_peek_icon(True)
        self._token_entry.props.placeholder_text = "Long-Lived Access Token (eyJ...)"
        self._token_entry.connect("activate", lambda _e: self._submit())
        self._token_box.append(self._token_entry)
        card.append(self._token_box)

        # Error banner
        self._error_lbl = Gtk.Label(label="", xalign=0.0)
        self._error_lbl.add_css_class("error-banner")
        self._error_lbl.set_wrap(True)
        self._error_lbl.set_max_width_chars(44)
        self._error_lbl.set_visible(False)
        card.append(self._error_lbl)

        # Connect button + Spinner
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_row.set_halign(Gtk.Align.FILL)
        self._connect_btn = Gtk.Button(label="Start Listening")
        self._connect_btn.add_css_class("cero-accent-btn")
        self._connect_btn.set_hexpand(True)
        self._connect_btn.connect("clicked", lambda _b: self._submit())

        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)

        btn_row.append(self._connect_btn)
        btn_row.append(self._spinner)
        card.append(btn_row)

        self.start_discovery()

    def _set_mode(self, token_mode: bool) -> None:
        self._use_token_mode = token_mode
        self._creds_box.set_visible(not token_mode)
        self._token_box.set_visible(token_mode)
        if token_mode:
            self._mode_token_btn.add_css_class("active")
            self._mode_creds_btn.remove_css_class("active")
        else:
            self._mode_creds_btn.add_css_class("active")
            self._mode_token_btn.remove_css_class("active")

    def start_discovery(self) -> None:
        self.ma_service.runner.submit(
            self.ma_service.discover_servers(timeout=1.8),
            on_success=self._populate_discovered,
        )

    def _populate_discovered(self, servers: list[DiscoveredServer]) -> None:
        while (child := self._discovered_chips.get_first_child()) is not None:
            self._discovered_chips.remove(child)
        if not servers:
            self._discovered_box.set_visible(False)
            return
        self._discovered_box.set_visible(True)
        for srv in servers:
            btn = Gtk.Button(label=f"{srv.name}")
            btn.add_css_class("filter-chip")
            btn.set_tooltip_text(srv.url)
            btn.connect("clicked", lambda _b, u=srv.url: self._url_entry.set_text(u))
            self._discovered_chips.append(btn)
        if not self._url_entry.get_text().strip():
            self._url_entry.set_text(servers[0].url)

    def show_error(self, msg: str) -> None:
        self._error_lbl.set_label(msg)
        self._error_lbl.set_visible(bool(msg))
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._connect_btn.set_sensitive(not busy)
        self._spinner.set_visible(busy)
        self._spinner.set_spinning(busy)

    def _submit(self) -> None:
        url = self._url_entry.get_text().strip()
        if not url:
            self.show_error("Enter your Music Assistant server address.")
            return

        self._error_lbl.set_visible(False)
        self._set_busy(True)

        if self._use_token_mode:
            coro = self.ma_service.authenticate_and_connect(
                url, token=self._token_entry.get_text().strip()
            )
        else:
            coro = self.ma_service.authenticate_and_connect(
                url,
                username=self._user_entry.get_text().strip(),
                password=self._pass_entry.get_text(),
            )

        def _ok(_res: None) -> None:
            self._set_busy(False)
            self._on_connected()

        def _err(exc: BaseException) -> None:
            self.show_error(f"Could not connect: {exc}")

        self.ma_service.runner.submit(coro, on_success=_ok, on_error=_err)
