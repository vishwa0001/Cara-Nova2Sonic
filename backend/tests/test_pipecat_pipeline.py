"""Unit and simulation tests for the Pipecat + Nova 2 Sonic + Connect KVS voice gateway."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    InterruptionFrame,
    StartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from domain_rules import CallMode, PatientContext
from gateway.kvs_transport import (
    KVSInputProcessor,
    KVSOutputProcessor,
    resample_pcm16,
)
from gateway.nova_sonic_service import NovaSonicLLMService
from gateway.tools import OutreachToolExecutor


class TestPipecatPipeline(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patient = PatientContext(
            patient_id="PT-PIPE-001",
            first_name="David",
            last_name="Miller",
            practice_name="Highland Health",
            phone_number="+18145550177",
            direct_agent=False,
            call_mode=CallMode.MODE_2_NORMAL_COACHING,
        )

    def test_audio_resampling(self):
        # Generate 100ms of 8 kHz sine wave audio (800 samples)
        t = np.linspace(0, 0.1, 800, endpoint=False)
        sin_samples = (np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
        raw_8k = sin_samples.tobytes()
        self.assertEqual(len(raw_8k), 1600)  # 800 samples * 2 bytes

        # Resample to 16 kHz -> expect ~1600 samples (3200 bytes)
        resampled_16k = resample_pcm16(raw_8k, src_rate=8000, dst_rate=16000)
        self.assertEqual(len(resampled_16k), 3200)

        # Downsample 24 kHz synthetic audio (2400 samples / 4800 bytes) to 8 kHz (800 samples / 1600 bytes)
        t24 = np.linspace(0, 0.1, 2400, endpoint=False)
        sin24 = (np.sin(2 * np.pi * 440 * t24) * 32767).astype(np.int16).tobytes()
        downsampled_8k = resample_pcm16(sin24, src_rate=24000, dst_rate=8000)
        self.assertEqual(len(downsampled_8k), 1600)

    async def test_kvs_input_processor(self):
        processor = KVSInputProcessor(target_sample_rate=16000)
        # 20ms of 8kHz audio = 160 samples = 320 bytes
        in_audio = b"\x00\x01" * 160
        frame_8k = AudioRawFrame(audio=in_audio, sample_rate=8000, num_channels=1)

        pushed_frames = []

        async def capture_frame(frame, direction):
            pushed_frames.append(frame)

        processor.push_frame = capture_frame

        await processor.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
        await processor.process_frame(frame_8k, FrameDirection.DOWNSTREAM)

        self.assertEqual(len(pushed_frames), 2)
        audio_out = pushed_frames[1]
        self.assertIsInstance(audio_out, AudioRawFrame)
        self.assertEqual(audio_out.sample_rate, 16000)
        self.assertEqual(len(audio_out.audio), 640)  # 320 samples = 640 bytes

    async def test_kvs_output_processor_and_barge_in_purging(self):
        output_processor = KVSOutputProcessor(target_sample_rate=8000)
        # Push 3 synthetic 24kHz audio frames
        synth_frame = AudioRawFrame(audio=b"\x00\x02" * 480, sample_rate=24000, num_channels=1)

        await output_processor.process_frame(synth_frame, FrameDirection.DOWNSTREAM)
        await output_processor.process_frame(synth_frame, FrameDirection.DOWNSTREAM)
        await output_processor.process_frame(synth_frame, FrameDirection.DOWNSTREAM)

        self.assertEqual(output_processor.queue.qsize(), 3)

        # Trigger VAD barge-in interruption frame
        await output_processor.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

        # Output queue should be instantly cleared (cutoff < 50ms)
        self.assertEqual(output_processor.queue.qsize(), 0)

    def test_nova_sonic_service_session_config(self):
        service = NovaSonicLLMService(patient_context=self.patient)
        config = service.get_session_config()

        self.assertEqual(config["modelId"], "amazon.nova-2-sonic-v1:0")
        self.assertEqual(config["sessionSettings"]["endpointingSensitivity"], "HIGH")
        self.assertEqual(config["sessionSettings"]["voice"], "ruth")
        self.assertIn("David Miller", config["systemPrompt"])
        self.assertIn("Highland Health", config["systemPrompt"])

    async def test_tool_executor_evaluate_and_transfer_available(self):
        executor = OutreachToolExecutor(api_base_url="http://127.0.0.1:8000")

        # Mock availability and webhook responses
        with patch("httpx.AsyncClient.get") as mock_get, patch("httpx.AsyncClient.post") as mock_post:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "available": True,
                    "agentPhone": "+15822671755",
                    "agentId": "agent-001",
                    "agentName": "Sarah Jenkins",
                    "checkedAt": "2026-09-22T15:00:00Z",
                },
            )
            mock_post.return_value = MagicMock(status_code=200, json=lambda: {"status": "success"})

            result = await executor.evaluate_and_transfer_pes(
                call_id="call-12345",
                patient_id="PT-PIPE-001",
                campaign_id="camp-test",
                call_mode=CallMode.MODE_2_NORMAL_COACHING.value,
                patient_name="David Miller",
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["action"], "TRANSFER_PES")
            self.assertEqual(result["agent"]["agentPhone"], "+15822671755")

            # Check that DynamoDB linked the assigned agent
            rec = executor.storage.get_record("PT-PIPE-001")
            self.assertIsNotNone(rec)
            self.assertEqual(rec["assigned_agent"]["agentId"], "agent-001")

    async def test_tool_executor_evaluate_and_transfer_unavailable_mode1(self):
        executor = OutreachToolExecutor(api_base_url="http://127.0.0.1:8000")

        with patch("httpx.AsyncClient.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"available": False},
            )

            result = await executor.evaluate_and_transfer_pes(
                call_id="call-12345",
                patient_id="PT-PIPE-001",
                campaign_id="camp-test",
                call_mode=CallMode.MODE_1_DIRECT_HANDOFF.value,
            )

            self.assertFalse(result["success"])
            self.assertEqual(result["action"], "FALLBACK_COACHING")

    async def test_tool_executor_evaluate_and_transfer_unavailable_mode2(self):
        executor = OutreachToolExecutor(api_base_url="http://127.0.0.1:8000")

        with patch("httpx.AsyncClient.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"available": False},
            )

            result = await executor.evaluate_and_transfer_pes(
                call_id="call-12345",
                patient_id="PT-PIPE-001",
                campaign_id="camp-test",
                call_mode=CallMode.MODE_2_NORMAL_COACHING.value,
            )

            self.assertFalse(result["success"])
            self.assertEqual(result["action"], "SCHEDULE_CALLBACK")

    async def test_tool_executor_schedule_callback(self):
        import uuid
        test_patient = f"PT-CB-{uuid.uuid4().hex[:8]}"
        executor = OutreachToolExecutor()
        res = await executor.schedule_callback(
            patient_id=test_patient,
            requested_time="2026-09-23T14:00:00Z",
            call_id="call-99",
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["reschedule_count"], 1)
        self.assertEqual(res["scheduled_time"], "2026-09-23T14:00:00Z")

    async def test_tool_executor_emergency_disconnect(self):
        executor = OutreachToolExecutor()
        res_med = await executor.emergency_disconnect(
            call_id="call-99",
            patient_id="PT-PIPE-MED",
            protocol_type="Section 6a Medical Emergency",
        )
        self.assertTrue(res_med["success"])
        self.assertEqual(res_med["disposition"], "SAFETY_MEDICAL")

        res_beh = await executor.emergency_disconnect(
            call_id="call-99",
            patient_id="PT-PIPE-BEH",
            protocol_type="Section 6b Behavioral Crisis",
        )
        self.assertTrue(res_beh["success"])
        self.assertEqual(res_beh["disposition"], "SAFETY_BEHAVIORAL")

    def test_rtp_packet_pack_unpack(self):
        from gateway.sip_transport import RTPPacket
        sample_payload = b"\x01\x02\x03\x04" * 40  # 160 bytes
        seq = 101
        ts = 32000
        ssrc = 98765432
        
        packed = RTPPacket.pack(
            payload=sample_payload,
            seq_num=seq,
            timestamp=ts,
            ssrc=ssrc,
            payload_type=RTPPacket.PAYLOAD_TYPE_PCMU,
        )
        self.assertEqual(len(packed), 12 + len(sample_payload))
        
        pt, r_seq, r_ts, r_ssrc, r_payload = RTPPacket.unpack(packed)
        self.assertEqual(pt, RTPPacket.PAYLOAD_TYPE_PCMU)
        self.assertEqual(r_seq, seq)
        self.assertEqual(r_ts, ts)
        self.assertEqual(r_ssrc, ssrc)
        self.assertEqual(r_payload, sample_payload)

    async def test_sip_rtp_output_processor_and_barge_in(self):
        from gateway.sip_transport import SIPRTPMediaServer, SIPRTPOutputProcessor
        media_server = SIPRTPMediaServer(port=10005)
        processor = SIPRTPOutputProcessor(media_server=media_server, contact_id="test-contact-1")
        
        synth_frame = AudioRawFrame(audio=b"\x00\x02" * 480, sample_rate=24000, num_channels=1)
        await processor.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
        await processor.process_frame(synth_frame, FrameDirection.DOWNSTREAM)
        
        self.assertGreaterEqual(processor.queue.qsize(), 1)
        
        # Interruption frame immediately clears queue
        await processor.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        self.assertEqual(processor.queue.qsize(), 0)
        
        await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)


if __name__ == "__main__":
    unittest.main()

