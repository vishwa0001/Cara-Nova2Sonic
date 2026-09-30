"""Telling a person from voicemail on the human-agent leg, using generated
audio: carrier ringback, a short "hello", a long greeting and a beep."""

import unittest

import numpy as np

from backend.gateway.agent_answer_detector import HUMAN, MACHINE, SILENCE, AgentAnswerDetector

SR = 8000
FRAME = 160


def _tone(freqs, seconds, amp=6000):
    t = np.arange(int(SR * seconds)) / SR
    return sum(np.sin(2 * np.pi * f * t) for f in freqs) * amp / len(freqs)


def _speech(seconds, amp=5000):
    """Voice-like: 140 Hz fundamental with harmonics to 3 kHz, syllable envelope."""
    t = np.arange(int(SR * seconds)) / SR
    voice = sum(np.sin(2 * np.pi * 140 * k * t) / k ** 0.5 for k in range(1, 21))
    envelope = 0.6 + 0.4 * np.abs(np.sin(2 * np.pi * 3 * t))
    return voice / np.max(np.abs(voice)) * envelope * amp


def _quiet(seconds):
    return np.random.default_rng(0).normal(0, 20, int(SR * seconds))


def _run(*parts):
    audio = np.concatenate(parts).astype(np.int16).tobytes()
    det = AgentAnswerDetector()
    for i in range(0, len(audio) - FRAME * 2 + 1, FRAME * 2):
        verdict = det.feed_frame(audio[i:i + FRAME * 2])
        if verdict:
            return verdict, det
    return None, det


class AgentAnswerDetectorTests(unittest.TestCase):
    def test_person_says_hello(self):
        verdict, det = _run(_quiet(0.5), _speech(0.7), _quiet(2.0))
        self.assertEqual(verdict, HUMAN, det.decided_by)

    def test_person_answers_after_carrier_ringing(self):
        ring = _tone([440, 480], 2.0)
        verdict, det = _run(ring, _quiet(4.0), ring, _quiet(0.6), _speech(0.8), _quiet(1.5))
        self.assertEqual(verdict, HUMAN, det.decided_by)
        self.assertGreater(det.ringing_frames, 100)

    def test_voicemail_greeting(self):
        verdict, det = _run(_quiet(0.3), _speech(5.0))
        self.assertEqual(verdict, MACHINE, det.decided_by)

    def test_voicemail_after_ringing(self):
        ring = _tone([440, 480], 2.0)
        verdict, det = _run(ring, _quiet(4.0), ring, _quiet(0.5), _speech(6.0))
        self.assertEqual(verdict, MACHINE, det.decided_by)

    def test_beep(self):
        verdict, det = _run(_quiet(1.0), _tone([1000], 0.6), _quiet(1.0))
        self.assertEqual(verdict, MACHINE, det.decided_by)

    def test_nothing_heard(self):
        verdict, det = _run(_quiet(6.0))
        self.assertEqual(verdict, SILENCE, det.decided_by)

    def test_ringing_alone_is_not_a_decision(self):
        ring = _tone([440, 480], 2.0)
        verdict, det = _run(ring, _quiet(4.0), ring, _quiet(4.0), ring)
        self.assertIsNone(verdict)


if __name__ == "__main__":
    unittest.main()
