# CERO

**CERO Electron, Reproduce Only** — a native Linux desktop player for
[Music Assistant](https://www.music-assistant.io/).

CERO is a pure playback client: browse your Music Assistant library, play it on this
computer or on any of your Music Assistant speakers, and control it from the keyboard,
the desktop or `playerctl`. It does **not** configure the server — use the Music
Assistant web UI for that.

It is written in Python with GTK 4 and Libadwaita: no Electron, no web view, low memory
use, and it looks at home on GNOME, KDE, tiling window managers (i3, Sway, Hyprland…),
Wayland and X11.

---

## Features

### Playback
- **Plays on this computer** — CERO registers itself in Music Assistant as a native
  [Sendspin](https://www.music-assistant.io/) player and decodes the stream locally with
  GStreamer (lossless FLAC or PCM), so the PC shows up as one more speaker.
- **Controls any Music Assistant player** — switch between this device and your remote
  speakers from the devices menu, optionally transferring the current queue.
- **Single active queue** — starting playback clears the queues of the other devices,
  so only one thing is playing at a time.
- **Audio quality indicator** — HR / HQ / SQ / LQ in the *Now playing* queue panel. Click it
  to open the *Audio quality* menu with the signal path: source, processing (normalization,
  DSP) and output formats.

### Library and discovery
- **Discover** home page with Music Assistant's recommendation rows.
- Full, paginated **library** of albums, playlists and artists, sortable by release date,
  name, recently added or creator, ascending or descending, with a quick filter.
- Fast **global search** across all your providers.
- **Album, playlist and artist pages** — artist pages list every album and single
  available from your providers and highlight the ones already in your library.
- **Track credits** (composer, lyricist, performers, label, copyright…) and album
  copyright.
- Add/remove items from your **library** and **favorites**, add tracks to playlists,
  start a radio, play now / next / add to queue — all from the right-click menu.
- Views update automatically when the same item changes in another Music Assistant
  client (e.g. un-favoriting an album in the web UI).

### Desktop integration
- **MPRIS2** (`org.mpris.MediaPlayer2.cero`): media keys, desktop widgets, status bars
  and `playerctl` all work out of the box.
- Queue panel with **drag-and-drop** reordering, remaining track count and total time.
- Full-size **cover art viewer**: click the cover on an album, playlist or artist page
  (right-click inside the viewer copies the image to the clipboard).
- Clicking the cover in the player bar opens the album or playlist that is playing, just
  like clicking the title.
- Remembers your session, panel sizes and whether the queue panel was open.
- Does **not** resume playback by itself on startup.

### Keyboard and mouse

| Action | Shortcut |
|---|---|
| Play / Pause | <kbd>Space</kbd> |
| Next / previous track | <kbd>Ctrl</kbd>+<kbd>→</kbd> / <kbd>Ctrl</kbd>+<kbd>←</kbd> |
| Search | <kbd>Ctrl</kbd>+<kbd>K</kbd> or <kbd>Ctrl</kbd>+<kbd>F</kbd> |
| Back / forward | <kbd>Alt</kbd>+<kbd>←</kbd> / <kbd>Alt</kbd>+<kbd>→</kbd>, or the mouse back/forward side buttons |
| Refresh the open album / artist / playlist from the server | <kbd>F5</kbd> |
| Move through the grid / open the focused card | <kbd>↑</kbd> <kbd>↓</kbd> <kbd>←</kbd> <kbd>→</kbd> / <kbd>Enter</kbd> |
| Quit | <kbd>Ctrl</kbd>+<kbd>Q</kbd> |
| Close cover viewer / dialogs | <kbd>Esc</kbd> |
| Play a sidebar item or a track, replacing the queue | Double-click |
| Open an album / playlist / artist (sidebar or cards) | Click |
| Track credits | Right-click the track title in the player bar |

---

## Requirements

### Music Assistant
- A running **Music Assistant Server 2.x** reachable over the network.
  Tested with **Music Assistant 2.10.12** (API schema 65).
- An account on the server (username and password) **or** a long-lived access token.
- To play audio **on this computer**, the server must be able to reach it (Sendspin uses
  mDNS on the local network and falls back to a direct connection to the server).

### System
- Linux with a Wayland or X11 session.
- PipeWire (with `pipewire-pulse`) or PulseAudio for sound output.
- Python **3.13** or newer — the **same Python your distribution's PyGObject is built
  for** (on Arch Linux, `/usr/bin/python`).

| Component | Required | Tested with | Arch Linux package |
|---|---|---|---|
| Python | ≥ 3.13 | 3.14 | `python` |
| PyGObject | — | 3.58 | `python-gobject` |
| GTK | ≥ 4.14 | 4.24 | `gtk4` |
| Libadwaita | ≥ 1.5 | 1.10 | `libadwaita` |
| GStreamer + base plugins | — | 1.28 | `gstreamer`, `gst-plugins-base` |
| GStreamer good plugins (FLAC decoder) | — | 1.28 | `gst-plugins-good` |
| aiohttp | ≥ 3.9.2 | 3.14 | `python-aiohttp` |
| zeroconf | ≥ 0.147 | 0.154 | `python-zeroconf` |
| orjson | ≥ 3.10 | 3.13 | `python-orjson` |
| cryptography | ≥ 42 | 50 | `python-cryptography` |
| playerctl *(optional)* | — | 2.4 | `playerctl` |

Libadwaita 1.5 is the hard minimum (CERO uses `Adw.AlertDialog`), which in turn needs
GTK 4.14. Python 3.13 is required by `music-assistant-client`; the other Python minimums
come from CERO's dependencies.

These Python libraries are **not** packaged in the Arch repositories and are installed
from PyPI (the AUR package bundles them):

| Library | Version |
|---|---|
| `music-assistant-client` | ≥ 1.8.0 |
| `music-assistant-models` | exactly the version required by `music-assistant-client` |
| `aiosendspin` | ≥ 10.3.0 |
| `mashumaro`, `cpace`, `noiseprotocol` | dependencies of `aiosendspin` |

---

## Installation

### Arch Linux (AUR)

```sh
yay -S cero        # or: paru -S cero
```

The package installs the `cero` command, a desktop launcher and the application icon.

### From source

```sh
# 1. System dependencies (Arch Linux; use your distribution's equivalents elsewhere)
sudo pacman -S --needed python python-gobject gtk4 libadwaita \
    gstreamer gst-plugins-base gst-plugins-good \
    python-aiohttp python-zeroconf python-orjson python-cryptography playerctl

# 2. Get the code
git clone https://github.com/delaosa/cero.git
cd cero

# 3. Virtual environment that can see the system PyGObject/GTK.
#    Use the system Python explicitly: if `python` points to pyenv/asdf/conda,
#    PyGObject will not be found and the dependencies may not install.
/usr/bin/python -m venv --system-site-packages .venv
.venv/bin/pip install -e .

# 4. Run it
./run.sh

# 5. Optional: add CERO to your application menu (per user)
scripts/install-launcher.sh
```

`scripts/install-launcher.sh --uninstall` removes the launcher and icon again.
If you move the project folder, run `scripts/install-launcher.sh` once more.

---

## First run

1. Enter your Music Assistant server address. A bare IP or host name is fine
   (`192.168.1.50` becomes `http://192.168.1.50:8095`); servers found on your network
   are listed automatically.
2. Sign in with your Music Assistant **username and password**, or switch to
   **Access Token** and paste a long-lived access token, then press **Start Listening**.
3. This computer appears in Music Assistant as **`CERO (<hostname>)`** and is selected as
   the output. If Music Assistant asks to pair the Sendspin player, CERO shows the
   pairing code in the top bar.

You only sign in once; the session is stored locally (see below). Use the log-out button
in the top bar to switch servers — it shows your current session details before asking
for confirmation.

---

## Controlling CERO from the command line

```sh
playerctl -p cero play-pause
playerctl -p cero next
playerctl -p cero metadata --format '{{ artist }} — {{ title }}'
playerctl -p cero volume 0.5
```

On i3 / Sway the window class (and Wayland app id) is `xyz.delaosa.CERO`, e.g.:

```
for_window [class="xyz.delaosa.CERO"] move to workspace 9
```

---

## Files and data

| Path | Contents |
|---|---|
| `~/.config/cero/session.json` | Server URL, credentials/token and UI preferences (permissions `0600`) |
| `~/.config/cero/sendspin_pairing.json` | Identity of this computer as a Sendspin player |
| `~/.cache/cero/` | Cover art and a library snapshot for instant start-up |

Paths follow `XDG_CONFIG_HOME` / `XDG_CACHE_HOME`. Deleting `~/.cache/cero` is always
safe. Deleting `~/.config/cero` signs you out and makes Music Assistant see this
computer as a new player.

Set `CERO_DEBUG=1` to get debug logging on the terminal:

```sh
CERO_DEBUG=1 cero
```

---

## Troubleshooting

- **No sound on this computer** — check that `gst-plugins-good` is installed
  (`gst-inspect-1.0 flacdec` must succeed) and that PipeWire/PulseAudio is running.
  Make sure *this device* is selected in the devices menu of the player bar.
- **This computer does not appear in Music Assistant** — mDNS must work between the PC
  and the server (same network, no firewall blocking UDP 5353). CERO also tries a direct
  connection to the server on port 8927.
- **`playerctl` does not see CERO** — CERO must be running; check with `playerctl -l`.
- **Some commands fail on older servers** — CERO is tested against API schema 65 and
  avoids newer-only commands where it can; update Music Assistant if something
  misbehaves.

---

## Development

```sh
.venv/bin/python tests/verify_all.py
```

The test suite needs a graphical session (it renders the real GTK windows), GStreamer
and `playerctl`. It does not need a Music Assistant server.

```
cero/
├── __main__.py         entry point
├── ma_service.py       Music Assistant connection, library, queue and playback
├── sendspin_player.py  local Sendspin receiver + GStreamer audio engine
├── mpris.py            MPRIS2 D-Bus server
├── session.py          session storage, XDG paths, sorting
├── artwork.py          cover art loading and caching
├── style.css           application stylesheet
└── ui/                 GTK 4 / Libadwaita interface
tests/verify_all.py     end-to-end verification suite
scripts/                  install-launcher.sh, local-install.sh, publish-aur.sh
```

### Packaging notes

- Application id: `xyz.delaosa.CERO` (desktop file `xyz.delaosa.CERO.desktop`, icon
  `xyz.delaosa.CERO`).
- MPRIS bus name: `org.mpris.MediaPlayer2.cero`.
- `cero.svg` is the scalable application icon; `cero.desktop` is a launcher template.
- The Python package is built with setuptools (`pyproject.toml`) and ships `style.css`
  as package data.

---

## License

[MIT](LICENSE) © delaosa

CERO is an independent project and is not affiliated with or endorsed by Music
Assistant.
