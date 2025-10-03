"""
mumble_bridge.py — Slim Mumble wrapper for remote_trx

Features:
- Connect to Mumble, optionally join a channel
- Keepalive ping thread (logs each ping; default 5s interval)
- Safe TX path (radio → Mumble): queue + worker, frames to encoder size
- Simple API: start(), stop(), set_tx(), send_pcm(), client, connected

Assumptions:
- 48 kHz, mono int16 pipeline (Mumble's native rate). If you feed some
  other rate, resample before calling send_pcm().
"""

from __future__ import annotations
import logging
import threading
import queue
import time
from typing import Optional

import numpy as np
from pymumble_py3 import Mumble, errors
from pymumble_py3.callbacks import PYMUMBLE_CLBK_SOUNDRECEIVED as PCS

# Mumble's native sample rate
_PYMUMBLE_SR = 48000


class MumbleBridge:
    def __init__(
        self,
        server: str,
        user: str,
        *,
        password: Optional[str] = None,
        port: int = 64738,
        channel: Optional[str] = None,
        sample_rate: int = 48000,
        channels: int = 1,
        ping_interval: float = 5.0,
        log_pings: bool = True,
        reconnect: bool = True,
    ) -> None:
        if not server:
            raise ValueError("server is required")
        if not user:
            raise ValueError("user is required")
        if sample_rate != _PYMUMBLE_SR:
            logging.warning(
                f"[MumbleBridge] Sample rate {sample_rate} ≠ {_PYMUMBLE_SR} — "
                "you should resample to 48 kHz before send_pcm()."
            )
        if channels != 1:
            logging.warning("[MumbleBridge] Non‑mono audio is not supported; forcing mono in worker.")

        self._server = server
        self._user = user
        self._password = password
        self._port = port
        self._channel = channel
        self._sr = int(sample_rate)
        self._ch = int(channels)
        self._ping_interval = float(ping_interval)
        self._log_pings = bool(log_pings)

        # Underlying Mumble client
        self._m = Mumble(
            server,
            user,
            port=port,
            password=password,
            reconnect=reconnect
        )

        # We want to be able to read remote audio via .sound_input
        self._m.set_receive_sound(True)

        # State
        self._connected = False
        self._last_ping_ts: Optional[float] = None

        # Safe shutdown
        self._shutdown = threading.Event()

        # Queues & threads
        self._tx_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)  # radio → Mumble
        self._rx_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self._tx_thread: Optional[threading.Thread] = None
        self._ping_thread: Optional[threading.Thread] = None

        # Framing buffer for encoder (bytes)
        self._frame_bytes: Optional[int] = None
        self._accum = bytearray()

        # Callbacks
        self._m.callbacks.set_callback("connected", self._on_connected)
        try:
            self._m.callbacks.set_callback("disconnected", self._on_disconnected)
        except Exception:
            # Older pymumble_py3 may not expose this; best‑effort
            pass

    # ---------- Public API ----------
    @property
    def client(self) -> Mumble:
        """Access the underlying pymumble client (e.g., for .sound_input.get_sound())."""
        return self._m

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_ping_at(self) -> Optional[float]:
        return self._last_ping_ts

    def start(self) -> None:
        """Connect, wait ready, join channel (if set), start ping + TX worker."""
        logging.info(f"[MumbleBridge] Connecting to {self._server}:{self._port} as {self._user}")
        self._m.start()
        try:
            self._m.is_ready()  # blocks until connected/initialized
        except errors.ConnectionRejectedError as e:
            logging.error(f"[MumbleBridge] Connection rejected: {e}")
            raise

        # Join channel if requested
        if self._channel:
            chan = self._m.channels.find_by_name(self._channel)
            if chan is None:
                logging.warning(f"[MumbleBridge] Channel '{self._channel}' not found; staying in root")
            else:
                chan.move_in()
                logging.info(f"[MumbleBridge] Joined channel '{self._channel}'")

        # Start threads
        self._shutdown.clear()
        self._tx_thread = threading.Thread(target=self._tx_worker, name="MumbleTX", daemon=True)
        self._tx_thread.start()

        self._ping_thread = threading.Thread(target=self._ping_worker, name="MumblePing", daemon=True)
        self._ping_thread.start()

        logging.info("[MumbleBridge] Started (ping + TX worker running)")

    def stop(self) -> None:
        """Graceful shutdown of workers and Mumble."""
        logging.info("[MumbleBridge] Stopping…")
        self._shutdown.set()
        try:
            if self._tx_thread and self._tx_thread.is_alive():
                self._tx_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            if self._ping_thread and self._ping_thread.is_alive():
                self._ping_thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            self._m.stop()
        except Exception:
            pass
        logging.info("[MumbleBridge] Stopped")

    def send_pcm(self, pcm: np.ndarray | bytes) -> None:
        """
        Queue PCM to send to Mumble (radio → Mumble).
        Accepts:
          - numpy int16 mono array (N,) or (N,1)
          - raw bytes of int16 mono
        Framing to encoder size is handled in the TX worker thread.
        """
        try:
            if isinstance(pcm, np.ndarray):
                if pcm.dtype != np.int16:
                    pcm = pcm.astype(np.int16, copy=False)
                pcm = pcm.reshape(-1)
                buf = pcm.tobytes()
            elif isinstance(pcm, bytes):
                buf = pcm
            else:
                return
            self._tx_q.put_nowait(buf)
        except queue.Full:
            logging.debug("TX queue full, dropping frame")

        except Exception as e:
            logging.warning(f"[MumbleBridge] send_pcm failed: {e}")

    def _on_sound_received(self, user, soundchunk):
        """
        Called by pymumble when audio is received.
        soundchunk.pcm is a bytes buffer (int16 mono).
        """
        pcm_bytes = soundchunk.pcm
        try:
            self._rx_q.put_nowait(pcm_bytes)
        except queue.Full:
            logging.debug("[MB] RX queue full, dropping frame")

    def get_received(self, timeout: float = 0.1) -> Optional[bytes]:
        """Return the next received PCM frame bytes, or None if none available."""
        try:
            return self._rx_q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain_tx(self):
        """
        Internal: call in your main TX loop to dequeue and call add_sound
        """
        while True:
            try:
                pcm = self._tx_q.get_nowait()
            except queue.Empty:
                break
            try:
                self.client.sound_output.add_sound(pcm)
            except Exception as e:
                logging.warning(f"[MB] add_sound failed: {e}")



    # ---------- Internal ----------
    def _on_connected(self, *args, **kwargs) -> None:
        self._connected = True
        logging.info("[MumbleBridge] Connected")

    def _on_disconnected(self, *args, **kwargs) -> None:
        self._connected = False
        logging.warning("[MumbleBridge] Disconnected")

    def _compute_frame_bytes_if_ready(self) -> Optional[int]:
        """Calculate encoder frame size in BYTES once encoder is initialized."""
        if self._frame_bytes:
            return self._frame_bytes
        try:
            encoder_framesize = getattr(self._m.sound_output, "encoder_framesize", None)
            if encoder_framesize:
                # bytes per frame = seconds_per_frame * SR * bytes_per_sample(2) * channels
                self._frame_bytes = int(encoder_framesize * _PYMUMBLE_SR * 2 * 1)
                logging.info(f"[MumbleBridge] Encoder frame: {self._frame_bytes} bytes "
                             f"({int(self._frame_bytes/2)} samples @ 48 kHz)")
        except Exception:
            pass
        return self._frame_bytes

    def _tx_worker(self) -> None:
        """Drain TX queue, frame to encoder size, send via sound_output.add_sound()."""
        logging.info("[MumbleBridge] TX worker started")
        while not self._shutdown.is_set():
            try:
                chunk = self._tx_q.get(timeout=0.1)
            except queue.Empty:
                continue

            # Append to buffer
            try:
                self._accum.extend(chunk.tobytes())
            except Exception:
                # In case chunk is already bytes-like
                self._accum.extend(bytes(chunk))

            # Wait until encoder is initialized to compute frame size
            frame_bytes = self._compute_frame_bytes_if_ready()
            if not frame_bytes:
                # Encoder not ready yet; keep accumulating
                continue
        while len(self._accum) >= frame_bytes:
            frame = self._accum[:frame_bytes]
            del self._accum[:frame_bytes]
            try:
                self._m.sound_output.add_sound(frame)
            except Exception as e:
                logging.warning(f"[MumbleBridge] add_sound failed: {e}")

        logging.info("[MumbleBridge] TX worker exiting")

    def _ping_worker(self) -> None:
        """Send Mumble ping on interval and log each one."""
        logging.info("[MumbleBridge] Ping worker started")
        while not self._shutdown.is_set():
            try:
                self._m.ping()
                self._last_ping_ts = time.time()
                if self._log_pings:
                    logging.info(f"📡 Mumble ping @ {time.strftime('%H:%M:%S', time.localtime(self._last_ping_ts))}")
            except Exception as e:
                logging.warning(f"[MumbleBridge] ping failed: {e}")
            self._shutdown.wait(self._ping_interval)
        logging.info("[MumbleBridge] Ping worker exiting")
