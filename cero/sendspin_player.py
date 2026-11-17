"""Local Sendspin 1.0 audio receiver with native GStreamer decoding and output."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from collections.abc import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from aiosendspin.client import (
    AudioFormat,
    ClientListener,
    PairingSupport,
    SendspinClient,
)
from aiosendspin.models.core import DeviceInfo, ServerCommandPayload, StreamStartMessage
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, GoodbyeReason, PlayerCommand, Roles
from aiosendspin.noise.keys import Identity, b64url_decode
from aiosendspin.noise.trust_store import FileClientPairingStore
from gi.repository import Gst, GstApp

from . import __version__
from .ma_service import extract_host_from_url
from .session import SessionStore, get_config_dir

logger = logging.getLogger(__name__)

Gst.init(None)


def _volume_to_linear_gain(volume_pct: int) -> float:
    """Converts perceptual volume (0..100) to linear gain per the Sendspin 1.0 specification."""
    clamped = max(0.0, min(100.0, float(volume_pct)))
    return (clamped / 100.0) ** 1.5


class GstAudioEngine:
    """Self-recovering low-latency GStreamer pipeline for Sendspin FLAC and PCM streams."""

    def __init__(self, initial_volume: int = 85, initial_muted: bool = False) -> None:
        self._lock = threading.Lock()
        self._pipeline: Gst.Pipeline | None = None
        self._appsrc: GstApp.AppSrc | None = None
        self._vol_elem: Gst.Element | None = None
        self._current_sig: tuple[str, int, int, int, bytes | None] | None = None
        self._header_sent = False
        self._volume = initial_volume
        self._muted = initial_muted
        self._generation: int = 0

    @property
    def active_format(self) -> tuple[str, int, int, int] | None:
        """Returns (codec, sample_rate, channels, bit_depth) if a pipeline is active."""
        with self._lock:
            if self._current_sig is None:
                return None
            codec, sr, ch, bd, _hdr = self._current_sig
            return (codec, sr, ch, bd)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def muted(self) -> bool:
        return self._muted

    def set_volume(self, volume: int) -> None:
        with self._lock:
            self._volume = max(0, min(100, int(volume)))
            if self._vol_elem is not None:
                self._vol_elem.set_property("volume", _volume_to_linear_gain(self._volume))

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            self._muted = bool(muted)
            if self._vol_elem is not None:
                self._vol_elem.set_property("mute", self._muted)

    def bump_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def _build_pipeline_unlocked(self, fmt: AudioFormat, sink_name: str = "autoaudiosink") -> None:
        self._teardown_unlocked()

        codec = fmt.codec
        sr = fmt.pcm_format.sample_rate
        ch = fmt.pcm_format.channels
        bd = fmt.pcm_format.bit_depth

        if codec == AudioCodec.FLAC:
            desc = (
                "appsrc name=src is-live=true format=time block=false max-bytes=1048576 "
                f"! flacparse ! flacdec ! audioconvert ! audioresample ! volume name=vol ! {sink_name} sync=false"
            )
            caps = Gst.Caps.from_string("audio/x-flac")
        elif codec == AudioCodec.PCM:
            pcm_fmt_map = {16: "S16LE", 24: "S24LE", 32: "S32LE"}
            gst_pcm = pcm_fmt_map.get(bd, "S16LE")
            desc = (
                "appsrc name=src is-live=true format=time block=false max-bytes=1048576 "
                f"! audioconvert ! audioresample ! volume name=vol ! {sink_name} sync=false"
            )
            caps = Gst.Caps.from_string(
                f"audio/x-raw,format={gst_pcm},layout=interleaved,rate={sr},channels={ch}"
            )
        else:
            raise ValueError(f"Unsupported codec in local engine: {codec}")

        pipeline = Gst.parse_launch(desc)
        assert isinstance(pipeline, Gst.Pipeline)
        appsrc = pipeline.get_by_name("src")
        vol = pipeline.get_by_name("vol")
        assert isinstance(appsrc, GstApp.AppSrc)
        assert vol is not None

        appsrc.set_property("caps", caps)
        vol.set_property("volume", _volume_to_linear_gain(self._volume))
        vol.set_property("mute", self._muted)

        pipeline.set_state(Gst.State.PLAYING)

        self._pipeline = pipeline
        self._appsrc = appsrc
        self._vol_elem = vol
        self._current_sig = (codec.value, sr, ch, bd, fmt.codec_header)
        self._header_sent = False

    def _has_bus_error_unlocked(self) -> bool:
        if self._pipeline is None:
            return False
        bus = self._pipeline.get_bus()
        if bus is None:
            return False
        msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        return msg is not None

    def _push_once_unlocked(self, payload: bytes, fmt: AudioFormat) -> Gst.FlowReturn:
        assert self._appsrc is not None
        if fmt.codec == AudioCodec.FLAC and not self._header_sent:
            if fmt.codec_header:
                hdr = fmt.codec_header
                if not hdr.startswith(b"fLaC"):
                    hdr = b"fLaC" + hdr
                self._appsrc.push_buffer(Gst.Buffer.new_wrapped(hdr))
            self._header_sent = True

        return self._appsrc.push_buffer(Gst.Buffer.new_wrapped(payload))

    def push_chunk(
        self,
        payload: bytes,
        fmt: AudioFormat,
        sink_name: str = "autoaudiosink",
        generation: int | None = None,
    ) -> None:
        if not payload:
            return
        sig = (
            fmt.codec.value,
            fmt.pcm_format.sample_rate,
            fmt.pcm_format.channels,
            fmt.pcm_format.bit_depth,
            fmt.codec_header,
        )
        with self._lock:
            # Atomically discard in-flight chunks from before a pause or seek
            if generation is not None and generation != self._generation:
                return

            if (
                self._pipeline is None
                or self._current_sig != sig
                or self._has_bus_error_unlocked()
            ):
                self._build_pipeline_unlocked(fmt, sink_name=sink_name)

            ret = self._push_once_unlocked(payload, fmt)
            if ret != Gst.FlowReturn.OK:
                logger.debug("Reconstruyendo pipeline GStreamer tras FlowReturn=%s", ret)
                self._build_pipeline_unlocked(fmt, sink_name=sink_name)
                self._push_once_unlocked(payload, fmt)

    def clear_buffers(self) -> int:
        """Invalidates in-flight chunks and restarts the pipeline so the next Play starts clean."""
        with self._lock:
            self._generation += 1
            self._teardown_unlocked()
            return self._generation

    def stop(self) -> int:
        with self._lock:
            self._generation += 1
            self._teardown_unlocked()
            return self._generation

    def _teardown_unlocked(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._appsrc = None
        self._vol_elem = None
        self._current_sig = None
        self._header_sent = False


class LocalSendspinService:
    """Registers this Linux PC as a native Sendspin player in Music Assistant."""

    def __init__(
        self,
        session_store: SessionStore,
        on_pairing_code: Callable[[str | None], None] | None = None,
    ) -> None:
        self.session_store = session_store
        self._on_pairing_code = on_pairing_code
        self.audio = GstAudioEngine(
            initial_volume=session_store.data.local_volume,
            initial_muted=session_store.data.local_muted,
        )
        self._identity = self._load_or_create_identity()
        self._client: SendspinClient | None = None
        self._listener: ClientListener | None = None
        self._connect_task: asyncio.Task[None] | None = None
        self._feeder_task: asyncio.Task[None] | None = None
        self._chunk_queue: asyncio.Queue[tuple[int, int, bytes, AudioFormat]] = asyncio.Queue(
            maxsize=2048
        )
        self._wake_event = asyncio.Event()
        self._running = False
        self._server_url = ""

    @property
    def peer_id(self) -> str:
        return self._identity.peer_id

    def get_active_format(self) -> tuple[str, int, int, int] | None:
        return self.audio.active_format

    def _load_or_create_identity(self) -> Identity:
        saved = self.session_store.data.sendspin_private_key_b64u.strip()
        if saved:
            try:
                return Identity.from_private_bytes(b64url_decode(saved))
            except Exception:
                pass
        ident = Identity.generate()
        self.session_store.data.sendspin_private_key_b64u = ident.private_b64u
        self.session_store.save()
        return ident

    @staticmethod
    def _find_available_port(preferred: int = 8928) -> int:
        for port in range(preferred, preferred + 15):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("0.0.0.0", port))
                    return port
                except OSError:
                    continue
        return 0

    async def start(self, server_url: str = "") -> None:
        """Starts the local Sendspin client (mDNS + direct fallback connection to the MA server)."""
        if self._running:
            if server_url:
                self.update_server_url(server_url)
            return

        self._running = True
        self._server_url = server_url
        self._wake_event = asyncio.Event()

        store_path = get_config_dir() / "sendspin_pairing.json"
        pairing_store = await FileClientPairingStore.open(store_path)

        supported_formats = [
            SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=48000, bit_depth=24),
            SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=44100, bit_depth=24),
            SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=96000, bit_depth=24),
            SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=48000, bit_depth=16),
            SupportedAudioFormat(codec=AudioCodec.FLAC, channels=2, sample_rate=44100, bit_depth=16),
            SupportedAudioFormat(codec=AudioCodec.PCM, channels=2, sample_rate=48000, bit_depth=16),
            SupportedAudioFormat(codec=AudioCodec.PCM, channels=2, sample_rate=44100, bit_depth=16),
            SupportedAudioFormat(codec=AudioCodec.PCM, channels=2, sample_rate=48000, bit_depth=24),
            SupportedAudioFormat(codec=AudioCodec.PCM, channels=1, sample_rate=48000, bit_depth=16),
        ]

        async def _show_code(code: str | None, *, grouped: str | None) -> None:
            if self._on_pairing_code:
                self._on_pairing_code(grouped or code)

        async def _gesture_prompt(waiting: bool) -> None:
            if waiting and self._client is not None:
                self._client.open_pairing_window()

        pairing_support = PairingSupport(
            gesture_prompt=_gesture_prompt,
            pairing_code_display=_show_code,
            pair_pending_message="CERO player ready to pair",
        )

        client = SendspinClient(
            identity=self._identity,
            client_name=self.session_store.data.local_player_name,
            roles=[Roles.PLAYER, Roles.METADATA, Roles.CONTROLLER],
            pairing_store=pairing_store,
            device_info=DeviceInfo(
                product_name="CERO Linux Player",
                manufacturer="CERO",
                software_version=__version__,
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=supported_formats,
                # 256 KB (~8 s of FLAC) keeps memory low and avoids overflows
                buffer_capacity=256 * 1024,
            ),
            output_delay_ms=self.session_store.data.output_delay_ms,
            initial_volume=self.session_store.data.local_volume,
            initial_muted=self.session_store.data.local_muted,
            state_supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            pairing_support=pairing_support,
        )
        await client.set_unpaired_access(enabled=True)
        client.open_pairing_window()

        client.add_stream_start_listener(self._on_stream_start)
        client.add_stream_clear_listener(self._on_stream_clear)
        client.add_stream_end_listener(self._on_stream_end)
        client.add_audio_chunk_listener(self._on_audio_chunk)
        client.add_server_command_listener(self._on_server_command)
        client.add_output_delay_listener(self._on_output_delay_changed)

        self._client = client
        self._feeder_task = asyncio.create_task(self._audio_feeder_loop())

        port = self._find_available_port(8928)
        if port > 0:
            try:
                listener = ClientListener(
                    client_id=f"cero-{self._identity.peer_id[:10]}",
                    on_connection=client.attach_websocket,
                    port=port,
                    advertise_mdns=True,
                    client_name=self.session_store.data.local_player_name,
                )
                await listener.start()
                self._listener = listener
            except Exception as err:
                logger.warning("Could not start the Sendspin mDNS ClientListener: %s", err)

        self._connect_task = asyncio.create_task(self._direct_connect_loop())

    def update_server_url(self, server_url: str) -> None:
        self._server_url = server_url
        if self._running and (self._connect_task is None or self._connect_task.done()):
            self._connect_task = asyncio.create_task(self._direct_connect_loop())

    async def _direct_connect_loop(self) -> None:
        """Connects to ws://<ma_host>:8927/sendspin only if mDNS has not established a session."""
        # Give the incoming mDNS connection an initial grace period to avoid opening duplicate sockets
        await asyncio.sleep(3.0)
        while self._running:
            if self._client and not self._client.connected and self._server_url:
                host = extract_host_from_url(self._server_url)
                ws_url = f"ws://{host}:8927/sendspin"
                try:
                    await self._client.connect(ws_url)
                    logger.info("Sendspin connected directly to %s", ws_url)
                except Exception as err:
                    logger.debug("Direct Sendspin connection pending (%s): %s", ws_url, err)
            await asyncio.sleep(5.0)

    def _drain_chunk_queue(self) -> None:
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _on_stream_start(self, _msg: StreamStartMessage) -> None:
        self.audio.clear_buffers()
        self._drain_chunk_queue()
        self._wake_event.set()

    def _on_stream_clear(self, roles: list[str] | None) -> None:
        if roles is None or any(r.startswith("player") for r in roles):
            self.audio.clear_buffers()
            self._drain_chunk_queue()
            self._wake_event.set()

    def _on_stream_end(self, roles: list[str] | None) -> None:
        if roles is None or any(r.startswith("player") for r in roles):
            self.audio.stop()
            self._drain_chunk_queue()
            self._wake_event.set()

    def _on_audio_chunk(
        self, timestamp_us: int, payload: bytes, audio_format: AudioFormat, _send_ahead: int
    ) -> None:
        if not self._client:
            return
        play_time_us = self._client.compute_play_time(timestamp_us)
        gen = self.audio.generation
        try:
            self._chunk_queue.put_nowait((gen, play_time_us, payload, audio_format))
        except asyncio.QueueFull:
            # If it ever fills up, restart cleanly instead of dropping a FLAC frame mid-stream
            new_gen = self.audio.clear_buffers()
            self._drain_chunk_queue()
            self._chunk_queue.put_nowait((new_gen, play_time_us, payload, audio_format))

    async def _audio_feeder_loop(self) -> None:
        """Dispatches audio chunks paced to the Sendspin clock (~180 ms buffer)."""
        while self._running:
            try:
                gen, play_time_us, payload, fmt = await self._chunk_queue.get()
            except asyncio.CancelledError:
                return

            if not self._client or gen != self.audio.generation:
                continue

            now_us = self._client.now_us()
            # Drop stale chunks (>500 ms late)
            if play_time_us < now_us - 500_000:
                continue

            # Keep only ~180 ms pre-queued in GStreamer so Pause is instantaneous
            lead_s = (play_time_us - now_us) / 1_000_000.0
            if lead_s > 0.22:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(), timeout=min(lead_s - 0.16, 1.5)
                    )
                except TimeoutError:
                    pass
                if gen != self.audio.generation:
                    continue

            await asyncio.to_thread(
                self.audio.push_chunk, payload, fmt, "autoaudiosink", gen
            )

    def _on_server_command(self, payload: ServerCommandPayload) -> None:
        p_cmd = payload.player
        if p_cmd is None:
            return
        changed = False
        if p_cmd.command == PlayerCommand.VOLUME and p_cmd.volume is not None:
            self.audio.set_volume(p_cmd.volume)
            self.session_store.data.local_volume = self.audio.volume
            changed = True
        elif p_cmd.command == PlayerCommand.MUTE and p_cmd.mute is not None:
            self.audio.set_muted(p_cmd.mute)
            self.session_store.data.local_muted = self.audio.muted
            changed = True

        if changed:
            self.session_store.save()
            if self._client and self._client.connected:
                asyncio.create_task(
                    self._client.send_player_state(
                        available=True,
                        volume=self.audio.volume,
                        muted=self.audio.muted,
                    )
                )

    def _on_output_delay_changed(self, delay_ms: float) -> None:
        self.session_store.data.output_delay_ms = delay_ms
        self.session_store.save()

    async def stop(self) -> None:
        self._running = False
        self._wake_event.set()
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._feeder_task and not self._feeder_task.done():
            self._feeder_task.cancel()
        if self._listener is not None:
            try:
                await self._listener.stop()
            except Exception:
                pass
            self._listener = None
        if self._client is not None:
            try:
                await self._client.disconnect(GoodbyeReason.SHUTDOWN)
            except Exception:
                pass
            self._client = None
        self.audio.stop()
