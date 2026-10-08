"""Tell whether the patient is talking, from the inbound phone audio.

After the call-recording disclosure Cara waits for the patient to speak. If
the line stays quiet, the pipeline opens the conversation itself instead of
waiting for a "hello" that may never come (see CaraVoicePipeline). This
module only decides *whether someone is speaking*; it never hangs up a call.

Speech is detected with Silero VAD (the model ships with Pipecat) and gated
by the line's own noise floor, so steady background noise does not count.
If the model can't be loaded, a loudness check is used instead.

Feed 8 kHz PCM16 frames of any length. Thresholds come from CARA_VAD_*
environment variables so they can be tuned without a code change.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger("caller_speech")

SAMPLE_RATE = 8000
WINDOW_SAMPLES = 256  # Silero's window at 8 kHz (32 ms)
WINDOW_S = WINDOW_SAMPLES / SAMPLE_RATE
_CONTEXT_SAMPLES = 32


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# One ONNX session per process, shared by every call; each call keeps its own
# recurrent state, so loading the model never happens on a live call.
_silero_session = None
_silero_lock = threading.Lock()
_silero_failed = False


def load_vad_model():
    """Load the Silero model once. Returns None when it isn't available."""
    global _silero_session, _silero_failed
    if os.environ.get("CARA_VAD_ENGINE", "silero").lower() != "silero":
        return None
    if _silero_session is not None or _silero_failed:
        return _silero_session
    with _silero_lock:
        if _silero_session is not None or _silero_failed:
            return _silero_session
        try:
            from importlib.resources import files

            import onnxruntime

            path = str(files("pipecat.audio.vad.data").joinpath("silero_vad.onnx"))
            opts = onnxruntime.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            _silero_session = onnxruntime.InferenceSession(
                path, providers=["CPUExecutionProvider"], sess_options=opts
            )
            logger.info("[CALLER_VAD] Silero VAD model loaded")
        except Exception as e:
            _silero_failed = True
            logger.warning(f"[CALLER_VAD] Silero VAD unavailable ({e}); using the loudness check")
    return _silero_session


class CallerSpeechDetector:
    """Tracks whether the caller is speaking, one call at a time."""

    def __init__(self, vad_session=None):
        self.min_speech_s = _env_float("CARA_VAD_MIN_SPEECH_S", 0.22)
        self.confidence = _env_float("CARA_VAD_CONFIDENCE", 0.6)
        # Speech must be this much louder than the line's own background
        self.noise_margin = 10 ** (_env_float("CARA_VAD_NOISE_MARGIN_DB", 10.0) / 20)
        self.min_rms = _env_float("CARA_VAD_MIN_RMS", 120.0)
        # Loudness-only fallback threshold (same scale as AGENT_AMD_SPEECH_RMS)
        self.fallback_rms = _env_float("CARA_VAD_FALLBACK_RMS", 300.0)
        # A burst survives this much quiet inside it (pauses between syllables)
        self.hangover_s = _env_float("CARA_VAD_HANGOVER_S", 0.1)

        self._vad = vad_session if vad_session is not None else load_vad_model()
        self._state = np.zeros((2, 1, 128), dtype="float32")
        self._context = np.zeros((1, _CONTEXT_SAMPLES), dtype="float32")
        self._pending = np.zeros(0, dtype=np.int16)
        self._noise_rms: Optional[float] = None
        self._burst_s = 0.0
        self._gap_s = 0.0
        self._muted = False

        self.heard_speech = False
        self.speaking = False
        self.last_speech_at: Optional[float] = None

    @property
    def engine(self) -> str:
        return "silero" if self._vad is not None else "rms"

    def set_muted(self, muted: bool):
        """Ignore audio while our own disclosure plays, so its echo isn't speech."""
        self._muted = muted
        if muted:
            self._burst_s = self._gap_s = 0.0
            self.speaking = False

    def feed(self, pcm16_8k: bytes, now: float):
        samples = np.frombuffer(pcm16_8k, dtype=np.int16)
        self._pending = np.concatenate((self._pending, samples))
        while len(self._pending) >= WINDOW_SAMPLES:
            window = self._pending[:WINDOW_SAMPLES]
            self._pending = self._pending[WINDOW_SAMPLES:]
            self._feed_window(window, now)

    def _feed_window(self, window: np.ndarray, now: float):
        audio = window.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(window.astype(np.float64) ** 2)))
        is_speech = self._window_is_speech(audio, rms)

        if not is_speech:
            # Track the line's background level from the windows that aren't speech
            if self._noise_rms is None:
                self._noise_rms = rms
            else:
                self._noise_rms = 0.95 * self._noise_rms + 0.05 * rms

        if self._muted:
            return

        if is_speech:
            self._burst_s += WINDOW_S
            self._gap_s = 0.0
            if self._burst_s >= self.min_speech_s:
                self.heard_speech = True
                self.speaking = True
                self.last_speech_at = now
        else:
            self._gap_s += WINDOW_S
            if self._gap_s > self.hangover_s:
                self._burst_s = 0.0
                self.speaking = False

    def _window_is_speech(self, audio: np.ndarray, rms: float) -> bool:
        floor = max(self.min_rms, (self._noise_rms or 0.0) * self.noise_margin)
        if self._vad is None:
            return rms >= max(self.fallback_rms, floor)
        x = np.concatenate((self._context, audio.reshape(1, -1)), axis=1)
        try:
            out, self._state = self._vad.run(
                None,
                {"input": x, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype="int64")},
            )
        except Exception as e:
            logger.warning(f"[CALLER_VAD] Silero failed ({e}); using the loudness check for this call")
            self._vad = None
            return rms >= max(self.fallback_rms, floor)
        self._context = x[..., -_CONTEXT_SAMPLES:]
        return float(out[0][0]) >= self.confidence and rms >= floor

    def quiet_for(self, now: float, since: float) -> float:
        """Seconds without caller speech, counting from `since` at the earliest."""
        start = since if self.last_speech_at is None else max(since, self.last_speech_at)
        return max(0.0, now - start)
