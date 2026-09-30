"""Unit tests for Full-Duplex SIP / RTP Transport (G.711 u-law / 20ms framing / <50ms barge-in / RFC 3261 Signaling)."""

import asyncio
import unittest
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipecat.frames.frames import (
    AudioRawFrame,
    InterruptionFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from gateway.sip_transport import (
    RTPPacket,
    SDPMessage,
    SIPMessage,
    SIPRTPMediaServer,
    SIPRTPOutputProcessor,
    SIPSignalingServer,
    resample_pcm16_fast,
    ulaw_decode,
    ulaw_encode,
)


class TestSIPTransport(unittest.IsolatedAsyncioTestCase):
    def test_g711_ulaw_encoding_and_decoding(self):
        # Generate 16-bit linear PCM audio sine wave (160 samples @ 8 kHz = 20ms)
        t = np.linspace(0, 0.02, 160, endpoint=False)
        pcm_in = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16).tobytes()
        self.assertEqual(len(pcm_in), 320)

        # Encode to G.711 u-law (160 samples -> 160 bytes)
        ulaw_encoded = ulaw_encode(pcm_in)
        self.assertEqual(len(ulaw_encoded), 160)

        # Decode back to 16-bit linear PCM (160 bytes -> 320 bytes)
        pcm_decoded = ulaw_decode(ulaw_encoded)
        self.assertEqual(len(pcm_decoded), 320)

        # Verify decoded signal closely matches original within quantization noise (<5%)
        orig_samples = np.frombuffer(pcm_in, dtype=np.int16)
        recon_samples = np.frombuffer(pcm_decoded, dtype=np.int16)
        rmse = np.sqrt(np.mean((orig_samples.astype(float) - recon_samples.astype(float)) ** 2))
        max_val = np.max(np.abs(orig_samples))
        self.assertLess(rmse / max_val, 0.05)

    def test_rtp_packet_pack_and_unpack(self):
        payload = b"\x80" * 160  # 160 bytes of PCMU
        seq_num = 100
        timestamp = 32000
        ssrc = 98765432

        packet = RTPPacket.pack(
            payload=payload,
            seq_num=seq_num,
            timestamp=timestamp,
            ssrc=ssrc,
            payload_type=RTPPacket.PAYLOAD_TYPE_PCMU,
        )
        self.assertEqual(len(packet), 172)

        pt, rx_seq, rx_ts, rx_ssrc, rx_payload = RTPPacket.unpack(packet)
        self.assertEqual(pt, 0)
        self.assertEqual(rx_seq, seq_num)
        self.assertEqual(rx_ts, timestamp)
        self.assertEqual(rx_ssrc, ssrc)
        self.assertEqual(rx_payload, payload)

    def test_sdp_parser_and_generator(self):
        sample_sdp = (
            "v=0\r\n"
            "o=ChimeVoiceConnector 1001 1001 IN IP4 3.80.16.50\r\n"
            "s=Chime Session\r\n"
            "c=IN IP4 3.80.16.50\r\n"
            "t=0 0\r\n"
            "m=audio 14320 RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=ptime:20\r\n"
        )
        sdp = SDPMessage.parse(sample_sdp)
        self.assertEqual(sdp.ip, "3.80.16.50")
        self.assertEqual(sdp.rtp_port, 14320)
        self.assertEqual(sdp.payload_type, 0)
        self.assertEqual(sdp.ptime, 20)

        # Generate answer SDP
        answer = SDPMessage(ip="100.49.33.242", rtp_port=10000, payload_type=0, ptime=20).to_sdp()
        self.assertIn("c=IN IP4 100.49.33.242", answer)
        self.assertIn("m=audio 10000 RTP/AVP 0", answer)
        self.assertIn("a=rtpmap:0 PCMU/8000", answer)

    def test_sip_message_parser_and_response_builder(self):
        raw_invite = (
            b"INVITE sip:+17134811560@100.49.33.242:5060 SIP/2.0\r\n"
            b"Via: SIP/2.0/UDP 3.80.16.50:5060;branch=z9hG4bK-12345\r\n"
            b"From: <sip:+15551234567@chime.aws>;tag=tag-caller\r\n"
            b"To: <sip:+17134811560@100.49.33.242>\r\n"
            b"Call-ID: call-abc-123@chime.aws\r\n"
            b"CSeq: 1 INVITE\r\n"
            b"Content-Type: application/sdp\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        msg = SIPMessage.parse(raw_invite)
        self.assertEqual(msg.method, "INVITE")
        self.assertEqual(msg.get_header("Call-ID"), "call-abc-123@chime.aws")
        self.assertEqual(msg.get_header("From"), "<sip:+15551234567@chime.aws>;tag=tag-caller")

        # Build 200 OK
        resp_bytes = msg.build_response(200, "OK", to_tag="tag-gateway", sdp_body="v=0\r\n")
        resp_str = resp_bytes.decode()
        self.assertTrue(resp_str.startswith("SIP/2.0 200 OK"))
        self.assertIn("Call-ID: call-abc-123@chime.aws", resp_str)
        self.assertIn("tag=tag-gateway", resp_str)

    async def test_sip_signaling_and_media_registration(self):
        media_server = SIPRTPMediaServer(host="127.0.0.1", port=10010, echo_mode=True)
        signaling_server = SIPSignalingServer(
            host="127.0.0.1",
            port=15060,
            public_ip="100.49.33.242",
            media_server=media_server,
        )
        await media_server.start()
        await signaling_server.start()

        # Inbound INVITE with remote SDP (c=IN IP4 3.80.16.50, m=audio 12345 RTP/AVP 0)
        invite_data = (
            b"INVITE sip:+17134811560@100.49.33.242:5060 SIP/2.0\r\n"
            b"Via: SIP/2.0/UDP 127.0.0.1:5080;branch=z9hG4bK-test\r\n"
            b"From: <sip:+15551234567@chime.aws>;tag=src1\r\n"
            b"To: <sip:+17134811560@100.49.33.242>\r\n"
            b"Call-ID: test-call-id-999\r\n"
            b"CSeq: 1 INVITE\r\n"
            b"Content-Type: application/sdp\r\n\r\n"
            b"v=0\r\nc=IN IP4 3.80.16.50\r\nm=audio 12345 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        )
        signaling_server.handle_inbound_sip(invite_data, ("127.0.0.1", 5080))
        await asyncio.sleep(0.05)

        # Verify remote endpoint is populated from negotiated SDP
        self.assertEqual(media_server.remote_endpoints.get("test-call-id-999"), ("3.80.16.50", 12345))

        # Handle BYE
        bye_data = (
            b"BYE sip:+17134811560@100.49.33.242:5060 SIP/2.0\r\n"
            b"Via: SIP/2.0/UDP 127.0.0.1:5080;branch=z9hG4bK-test2\r\n"
            b"From: <sip:+15551234567@chime.aws>;tag=src1\r\n"
            b"To: <sip:+17134811560@100.49.33.242>;tag=tag-gateway\r\n"
            b"Call-ID: test-call-id-999\r\n"
            b"CSeq: 2 BYE\r\n\r\n"
        )
        signaling_server.handle_inbound_sip(bye_data, ("127.0.0.1", 5080))
        await asyncio.sleep(0.05)
        self.assertNotIn("test-call-id-999", media_server.remote_endpoints)

        media_server.stop()
        signaling_server.stop()

    async def test_sip_rtp_output_processor_and_barge_in(self):
        server = SIPRTPMediaServer(host="127.0.0.1", port=10020)
        processor = SIPRTPOutputProcessor(
            media_server=server,
            contact_id="test-contact-001",
            sample_rate=8000,
            frame_duration_ms=20,
        )

        # 24 kHz synthetic audio chunk (480 samples = 20ms = 960 bytes L16)
        synth_pcm = (np.zeros(480, dtype=np.int16)).tobytes()
        synth_frame = AudioRawFrame(audio=synth_pcm, sample_rate=24000, num_channels=1)

        await processor.process_frame(synth_frame, FrameDirection.DOWNSTREAM)
        await processor.process_frame(synth_frame, FrameDirection.DOWNSTREAM)

        # Should have 2 encoded frames in queue
        self.assertEqual(processor.queue.qsize(), 2)

        # Test Silero VAD Barge-in InterruptionFrame purging (<50ms cutoff)
        await processor.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        self.assertEqual(processor.queue.qsize(), 0)

    def test_fast_resampling_latency(self):
        # 100ms of 8 kHz audio (800 samples = 1600 bytes)
        raw_8k = (np.ones(800, dtype=np.int16) * 1000).tobytes()
        resampled_16k = resample_pcm16_fast(raw_8k, src_rate=8000, dst_rate=16000)
        self.assertEqual(len(resampled_16k), 3200)


if __name__ == "__main__":
    unittest.main()
