"""Call recording on the gateway: one stereo WAV per answered call in S3,
patient on the left, what the patient heard (Cara, then the bridged agent)
on the right. Before this, SIP calls weren't recorded anywhere (call 366e8e57)."""

import asyncio
import io
import os
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import call_recorder
from gateway.call_recorder import CallRecorder, build_stereo_wav, recording_key, upload_recording
from gateway.sip_transport import CallRTPMediaSession, RTPPacket, SIPRTPMediaServer, ulaw_encode

FRAME = 160  # 20 ms of u-law at 8 kHz


def _tone(value: int) -> bytes:
    """One 20 ms u-law frame of constant PCM value, easy to spot per channel."""
    return ulaw_encode(np.full(FRAME, value, dtype=np.int16).tobytes())


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _channels(wav_bytes: bytes):
    with wave.open(io.BytesIO(wav_bytes)) as w:
        assert w.getnchannels() == 2 and w.getsampwidth() == 2 and w.getframerate() == 8000
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data[0::2], data[1::2]


class RecorderTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s3 = MagicMock()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _put(self):
        self.assertEqual(self.s3.put_object.call_count, 1)
        return self.s3.put_object.call_args.kwargs


class AlignmentTests(RecorderTestBase):
    def test_gap_in_cara_audio_is_filled_with_silence(self):
        clock = FakeClock()
        rec = CallRecorder("c1", "bucket", directory=self.dir, clock=clock, s3_client=self.s3)
        # Patient audio arrives every 20 ms for 2 s; Cara only talks from 1.0 s to 1.2 s
        for i in range(100):
            clock.t = 100.0 + i * 0.02
            rec.add_inbound(_tone(1000))
            if 50 <= i < 60:
                rec.add_outbound(_tone(-2000))
        self.assertEqual(rec.inbound.samples, 100 * FRAME)
        # Cara's first frame lands at 1.0 s, not at 0 s
        self.assertEqual(rec.outbound.samples, 8000 + 10 * FRAME)
        out = bytes(rec.outbound.pending)
        self.assertEqual(set(out[:8000]), {call_recorder.ULAW_SILENCE})

    def test_small_jitter_is_not_padded(self):
        clock = FakeClock()
        rec = CallRecorder("c2", "bucket", directory=self.dir, clock=clock, s3_client=self.s3)
        rec.add_inbound(_tone(1000))
        clock.t += 0.05  # 30 ms late, inside the jitter allowance
        rec.add_inbound(_tone(1000))
        self.assertEqual(rec.inbound.samples, 2 * FRAME)

    def test_stereo_wav_puts_patient_left_and_heard_audio_right(self):
        wav, seconds = build_stereo_wav(_tone(1000) * 2, _tone(-2000))
        left, right = _channels(wav)
        self.assertAlmostEqual(seconds, 0.04)
        self.assertEqual(len(left), 2 * FRAME)
        self.assertTrue(np.all(np.abs(left - 1000) < 50))
        self.assertTrue(np.all(np.abs(right[:FRAME] + 2000) < 80))
        self.assertTrue(np.all(right[FRAME:] == 0))  # shorter side padded with silence


class UploadTests(RecorderTestBase):
    async def test_finalize_uploads_encrypted_wav_and_removes_local_files(self):
        rec = CallRecorder("call-abc", "rec-bucket", directory=self.dir, s3_client=self.s3)
        rec.metadata.update(patient_id="p-1", campaign_id="CAMP-1")
        rec.start()
        rec.add_inbound(_tone(1000))
        rec.add_outbound(_tone(-2000))
        uri, seconds = await rec.finalize()

        put = self._put()
        key = recording_key("call-abc", rec.started_at)
        self.assertEqual(uri, f"s3://rec-bucket/{key}")
        self.assertTrue(key.startswith("recordings/") and key.endswith("/call-abc.wav"))
        self.assertEqual(put["Bucket"], "rec-bucket")
        self.assertEqual(put["ServerSideEncryption"], "AES256")
        self.assertEqual(put["ContentType"], "audio/wav")
        self.assertEqual(put["Metadata"]["patient_id"], "p-1")
        self.assertEqual(put["Metadata"]["campaign_id"], "CAMP-1")
        self.assertIn("end_utc", put["Metadata"])
        left, right = _channels(put["Body"])
        self.assertEqual(len(left), FRAME)
        self.assertAlmostEqual(seconds, 0.02)
        self.assertEqual(os.listdir(self.dir), [])

    async def test_finalize_twice_uploads_once(self):
        rec = CallRecorder("c3", "b", directory=self.dir, s3_client=self.s3)
        rec.add_inbound(_tone(1))
        await rec.finalize()
        self.assertEqual(await rec.finalize(), (None, 0.0))
        self.assertEqual(self.s3.put_object.call_count, 1)

    def test_upload_retries_then_succeeds(self):
        rec_in = os.path.join(self.dir, "x.in.ulaw")
        Path(rec_in).write_bytes(_tone(1))
        self.s3.put_object.side_effect = [RuntimeError("throttled"), RuntimeError("throttled"), {}]
        sleeps = []
        uri, _ = upload_recording("b", "x", rec_in, rec_in + ".none", call_recorder.dt.datetime(2026, 10, 14),
                                  {}, s3_client=self.s3, sleep=sleeps.append)
        self.assertEqual(uri, "s3://b/recordings/2026/10/14/x.wav")
        self.assertEqual(sleeps, [1, 2])

    def test_upload_gives_up_and_keeps_audio(self):
        rec_in = os.path.join(self.dir, "y.in.ulaw")
        Path(rec_in).write_bytes(_tone(1))
        self.s3.put_object.side_effect = RuntimeError("no access")
        uri, _ = upload_recording("b", "y", rec_in, rec_in + ".none", call_recorder.dt.datetime(2026, 10, 14),
                                  {}, s3_client=self.s3, sleep=lambda s: None)
        self.assertIsNone(uri)
        self.assertEqual(self.s3.put_object.call_count, call_recorder.UPLOAD_ATTEMPTS)
        self.assertTrue(os.path.exists(rec_in))


def _media_session(call_id, remote):
    s = CallRTPMediaSession(contact_id=call_id, local_port=0, remote_addr=remote)
    s._is_active = True
    s.transport = MagicMock()
    s.transport.is_closing.return_value = False
    return s


def _rtp(payload: bytes) -> bytes:
    return RTPPacket.pack(payload=payload, seq_num=1, timestamp=0)


class GatewayWiringTests(RecorderTestBase):
    def setUp(self):
        super().setUp()
        patcher = patch.multiple(call_recorder, RECORDING_DIR=self.dir, _s3_client=self.s3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _media(self, bucket="rec-bucket"):
        media = SIPRTPMediaServer()
        media.recording_bucket = bucket
        media.call_sessions["patient"] = _media_session("patient", ("1.1.1.1", 4000))
        return media

    async def test_disabled_records_nothing(self):
        media = self._media(bucket="")
        media.mark_answered("patient")
        self.assertIsNone(media.call_sessions["patient"].recorder)

    async def test_whole_call_including_agent_is_recorded_and_saved(self):
        media = self._media()
        saved = []
        media.on_recording_saved = lambda *args: saved.append(args)
        media.set_recording_metadata("patient", patient_id="p-9", campaign_id="CAMP-9")
        media.mark_answered("patient")
        patient = media.call_sessions["patient"]
        self.assertIsNotNone(patient.recorder)

        patient.handle_inbound_rtp(_rtp(_tone(1000)), ("1.1.1.1", 4000))  # patient speaks
        media.send_rtp_packet(_tone(-2000), "patient", 1, 0)  # Cara speaks

        # Transfer: the agent's audio is relayed to the patient and recorded too
        media.call_sessions["agent"] = _media_session("agent", ("2.2.2.2", 5000))
        self.assertTrue(media.bridge_calls("patient", "agent"))
        media.call_sessions["agent"].handle_inbound_rtp(_rtp(_tone(3000)), ("2.2.2.2", 5000))
        patient.handle_inbound_rtp(_rtp(_tone(1000)), ("1.1.1.1", 4000))
        self.assertIsNone(media.call_sessions["agent"].recorder)

        await media.close_call_session("patient")
        await asyncio.gather(*media._recording_tasks)

        put = self._put()
        self.assertEqual(put["Metadata"]["transferred"], "true")
        self.assertEqual(put["Metadata"]["agent_leg_call_id"], "agent")
        left, right = _channels(put["Body"])
        self.assertEqual(len(left), 2 * FRAME)
        self.assertTrue(np.all(np.abs(right[:FRAME] + 2000) < 80))  # Cara
        self.assertTrue(np.all(np.abs(right[FRAME:] - 3000) < 120))  # agent
        call_id, uri, seconds, meta = saved[0]
        self.assertEqual(call_id, "patient")
        self.assertTrue(uri.startswith("s3://rec-bucket/recordings/"))
        self.assertEqual(meta["patient_id"], "p-9")

    async def test_shutdown_uploads_calls_still_open(self):
        media = self._media()
        media.mark_answered("patient")
        media.call_sessions["patient"].handle_inbound_rtp(_rtp(_tone(1000)), ("1.1.1.1", 4000))
        await media.finish_all_recordings(timeout=5)
        self._put()


if __name__ == "__main__":
    unittest.main()
