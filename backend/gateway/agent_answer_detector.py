"""Decide whether a person or a machine answered the human-agent leg.

Carriers often report "answered" the moment they start playing their own
ringing or voicemail, so a 200 OK does not mean the agent picked up. This
listens to the agent leg's audio and classifies it:

- ringing tone (400-500 Hz dominant): the phone is still ringing; keep waiting
- a short burst of speech ("Hello?") followed by quiet: HUMAN
- speech that keeps going (a greeting) or a voicemail beep: MACHINE
- nothing heard for the listening window: SILENCE

Feed 20 ms frames of 8 kHz PCM16 (320 bytes). Thresholds come from
AGENT_AMD_* environment variables so they can be tuned without a code change.
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np

from gateway.beep_detector import BeepDetector

FRAME_MS = 20
SAMPLE_RATE = 8000
FRAME_BYTES = 320

HUMAN = "HUMAN"
MACHINE = "MACHINE"
SILENCE = "SILENCE"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _frames(seconds: float) -> int:
    return max(1, int(round(seconds * 1000 / FRAME_MS)))


class AgentAnswerDetector:
    def __init__(self):
        self.speech_rms = _env_float("AGENT_AMD_SPEECH_RMS", 300.0)
        self.min_speech_frames = _frames(_env_float("AGENT_AMD_MIN_SPEECH_S", 0.16))
        self.max_hello_frames = _frames(_env_float("AGENT_AMD_MAX_HELLO_S", 1.8))
        self.hello_silence_frames = _frames(_env_float("AGENT_AMD_HELLO_SILENCE_S", 0.8))
        self.machine_frames = _frames(_env_float("AGENT_AMD_MACHINE_S", 2.5))
        self.listen_frames = _frames(_env_float("AGENT_AMD_LISTEN_S", 5.0))
        self.tone_ratio = _env_float("AGENT_AMD_TONE_RATIO", 0.7)

        self._beep = BeepDetector()
        self._listened = 0        # non-ringing frames since the last ring
        self._speech = 0          # frames in the current speech burst
        self._gap = 0             # quiet frames since the burst went quiet
        self._in_burst = False
        # Summary for logs
        self.frames_seen = 0
        self.ringing_frames = 0
        self.decided_by = ""

    def _is_ringing(self, samples: np.ndarray) -> bool:
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples)))) ** 2
        total = float(np.sum(spectrum))
        if total < 1e-6:
            return False
        freqs = np.fft.rfftfreq(len(samples), d=1.0 / SAMPLE_RATE)
        band = float(np.sum(spectrum[(freqs >= 380) & (freqs <= 500)]))
        return band / total > self.tone_ratio

    def feed_frame(self, pcm16_8k: bytes) -> Optional[str]:
        """Returns HUMAN, MACHINE or SILENCE once decided, otherwise None."""
        if len(pcm16_8k) != FRAME_BYTES:
            return None
        self.frames_seen += 1
        samples = np.frombuffer(pcm16_8k, dtype=np.int16).astype(np.float64)
        rms = float(np.sqrt(np.mean(samples ** 2)))
        loud = rms >= self.speech_rms

        if self._beep.feed_frame(pcm16_8k):
            self.decided_by = "beep"
            return MACHINE

        if loud and self._is_ringing(samples):
            # Carrier ringback: nobody has picked up yet. Start listening afresh.
            self.ringing_frames += 1
            self._listened = 0
            self._speech = self._gap = 0
            self._in_burst = False
            return None

        self._listened += 1
        if loud:
            self._speech += 1
            self._gap = 0
            if self._speech >= self.min_speech_frames:
                self._in_burst = True
            if self._speech >= self.machine_frames:
                self.decided_by = f"speech ran {self._speech * FRAME_MS} ms"
                return MACHINE
        elif self._in_burst:
            self._gap += 1
            if self._gap >= self.hello_silence_frames:
                if self._speech <= self.max_hello_frames:
                    self.decided_by = f"hello {self._speech * FRAME_MS} ms then quiet"
                    return HUMAN
                # Long-ish speech with a pause: a greeting between sentences
                self.decided_by = f"speech {self._speech * FRAME_MS} ms with a pause"
                return MACHINE
        else:
            # Quiet with no burst yet; a blip too short to be speech is noise
            self._speech = 0

        if not self._in_burst and self._listened >= self.listen_frames:
            self.decided_by = f"nothing heard for {self._listened * FRAME_MS} ms"
            return SILENCE
        return None
