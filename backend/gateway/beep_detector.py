import numpy as np

BEEP_FREQ_MIN = 800
BEEP_FREQ_MAX = 1200
BEEP_MIN_DURATION_MS = 250
SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)  # 160 samples

class BeepDetector:
    """
    Stateful beep detector. Feed 20ms PCM16 8kHz frames (320 bytes,
    160 int16 samples) continuously. Detects a beep as: a narrow
    spectral peak in 800-1200Hz that is dominant (>60% of total
    energy in that band vs rest of spectrum) sustained for at
    least BEEP_MIN_DURATION_MS.
    """

    def __init__(self):
        self._consecutive_tone_frames = 0
        self._frames_needed = max(
            1, int(BEEP_MIN_DURATION_MS / FRAME_MS)
        )

    def feed_frame(self, pcm16_8k: bytes) -> bool:
        """
        Returns True the instant a beep is confirmed. Call for
        every inbound 20ms frame at the SIP RTP hook point.
        """
        if len(pcm16_8k) != FRAME_SAMPLES * 2:
            return False  # skip malformed/partial frames

        samples = np.frombuffer(pcm16_8k, dtype=np.int16).astype(np.float64)
        if np.max(np.abs(samples)) < 500:
            # too quiet to be a beep — reset streak
            self._consecutive_tone_frames = 0
            return False

        windowed = samples * np.hanning(len(samples))
        spectrum = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(windowed), d=1.0 / SAMPLE_RATE)

        total_energy = np.sum(spectrum ** 2)
        if total_energy < 1e-6:
            self._consecutive_tone_frames = 0
            return False

        band_mask = (freqs >= BEEP_FREQ_MIN) & (freqs <= BEEP_FREQ_MAX)
        band_energy = np.sum(spectrum[band_mask] ** 2)
        band_ratio = band_energy / total_energy

        if band_ratio > 0.6:
            self._consecutive_tone_frames += 1
        else:
            self._consecutive_tone_frames = 0

        return self._consecutive_tone_frames >= self._frames_needed

    def reset(self):
        self._consecutive_tone_frames = 0
