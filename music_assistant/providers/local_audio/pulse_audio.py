"""PulseAudio Simple API fallback for environments where PortAudio cannot enumerate devices.

Used inside the Home Assistant OS Supervisor addon container where /proc/asound
is masked, preventing PortAudio/ALSA from discovering sound cards. The PulseAudio
socket at /run/audio/pulse.sock remains accessible.
"""

from __future__ import annotations

import ctypes
import subprocess
from typing import Any

PULSE_SOCKET = "unix:/run/audio/pulse.sock"

PA_STREAM_PLAYBACK = 1
PA_SAMPLE_S16LE = 3


class pa_sample_spec(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


def _load_libpulse_simple() -> ctypes.CDLL | None:
    """Load libpulse-simple, return None if unavailable."""
    try:
        lib = ctypes.CDLL("libpulse-simple.so.0")
    except OSError:
        return None
    lib.pa_simple_new.restype = ctypes.c_void_p
    lib.pa_simple_new.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.POINTER(pa_sample_spec),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.pa_simple_free.argtypes = [ctypes.c_void_p]
    lib.pa_simple_free.restype = None
    lib.pa_simple_write.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.pa_simple_write.restype = ctypes.c_int
    lib.pa_simple_drain.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    lib.pa_simple_drain.restype = ctypes.c_int
    return lib


_libpulse = _load_libpulse_simple()


def is_available() -> bool:
    """Return True if PulseAudio Simple API is usable."""
    return _libpulse is not None


def enumerate_pulse_sinks() -> list[dict[str, Any]]:
    """Enumerate PulseAudio output sinks via pactl.

    Returns a list of dicts compatible with the device format expected by
    LocalAudioBridgeManager.discover_and_register().
    """
    devices: list[dict[str, Any]] = []
    try:
        result = subprocess.run(
            ["pactl", "list", "sinks", "short"],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PULSE_SERVER": PULSE_SOCKET},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return devices

    if result.returncode != 0:
        return devices

    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        sink_index = int(parts[0])
        sink_name = parts[1]
        # Derive a friendly display name from the PulseAudio sink name
        display_name = sink_name.replace("alsa_output.", "").replace(".", " ").replace("-", " ")
        # Trim common suffixes
        for suffix in (" analog stereo", " analog mono"):
            if display_name.lower().endswith(suffix):
                display_name = display_name[: -len(suffix)]
        devices.append(
            {
                "index": sink_index,
                "name": display_name.strip(),
                "pulse_sink": sink_name,
                "hostapi": 0,
                "max_output_channels": 2,
            }
        )
    return devices


class PulseOutputStream:
    """A write-based output stream using PulseAudio Simple API, matching the
    interface used by SendspinLocalAudioBridge._audio_writer().
    """

    def __init__(
        self,
        sink_name: str | None = None,
        samplerate: int = 48000,
        channels: int = 2,
    ) -> None:
        if _libpulse is None:
            msg = "libpulse-simple.so.0 not available"
            raise RuntimeError(msg)

        spec = pa_sample_spec(PA_SAMPLE_S16LE, samplerate, channels)
        error = ctypes.c_int(0)
        sink_bytes = sink_name.encode() if sink_name else None

        self._handle = _libpulse.pa_simple_new(
            PULSE_SOCKET.encode(),
            b"music_assistant",
            PA_STREAM_PLAYBACK,
            sink_bytes,
            b"local_audio_out",
            ctypes.byref(spec),
            None,
            None,
            ctypes.byref(error),
        )
        if not self._handle:
            msg = f"Failed to open PulseAudio stream (error {error.value})"
            raise RuntimeError(msg)

    def write(self, data: bytes) -> None:
        """Write raw PCM data to the stream (blocking)."""
        error = ctypes.c_int(0)
        ret = _libpulse.pa_simple_write(
            self._handle,
            data,
            len(data),
            ctypes.byref(error),
        )
        if ret < 0:
            msg = f"PulseAudio write error {error.value}"
            raise RuntimeError(msg)

    def start(self) -> None:
        """No-op for API compatibility with sd.RawOutputStream."""

    def stop(self) -> None:
        """Drain remaining audio."""
        if self._handle:
            error = ctypes.c_int(0)
            _libpulse.pa_simple_drain(self._handle, ctypes.byref(error))

    def close(self) -> None:
        """Free the PulseAudio connection."""
        if self._handle:
            _libpulse.pa_simple_free(self._handle)
            self._handle = None
