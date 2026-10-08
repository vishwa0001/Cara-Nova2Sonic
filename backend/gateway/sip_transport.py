"""Full-Duplex SIP / RTP Audio Transport & Signaling Gateway for Amazon Chime Voice Connector.

Provides RFC 3261 SIP Signaling and RFC 3550 RTP Bidirectional Audio Streaming:
- Signaling: RFC 3261 SIP User Agent (UDP 5060) handling INVITE, 100 Trying, 180 Ringing, 200 OK, ACK, BYE, OPTIONS.
- SDP Negotiation: RFC 4566 SDP parser/generator negotiating G.711 u-law (PCMU, Payload Type 0, 8000 Hz mono, 20ms ptime).
- Dynamic RTP Port Allocation: Unique, dynamically assigned RTP port per call from pool 10000-20000, released on hangup.
- Strict Call Media Isolation: Dedicated UDP sockets per call, eliminating cross-talk. Unmatched packets are dropped.
- Symmetrical RTP: Latches to actual remote RTP source on initial packet arrival per call socket.
- Echo Mode: Dedicated isolated RTP audio loopback test mode to verify telephony media path.
- Outbound Processor: High-precision 20ms G.711 u-law frame packetizer with instant (<50ms) barge-in queue purging.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional, Set, Tuple

import numpy as np
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from gateway.agent_answer_detector import HUMAN, SILENCE, AgentAnswerDetector

logger = logging.getLogger("sip_transport")

# Caller audio held between the 200 OK and the pipeline being attached
# (20 ms frames; 150 = 3 s), and the backlog limit once it is attached.
EARLY_AUDIO_MAX_FRAMES = int(float(os.getenv("CARA_EARLY_AUDIO_MAX_S", "3.0")) * 50)
INBOUND_QUEUE_MAX_FRAMES = 500

# When to build Cara for an outbound call: "invite" (as soon as we dial),
# "ringing" (on 180/183) or "answer" (on 200 OK, the old behaviour).
PREPARE_PIPELINE_AT = os.getenv("CARA_PREPARE_PIPELINE_AT", "invite").lower()


# ==============================================================================
# G.711 u-law (PCMU) Fast Vectorized Lookup Tables (Pure Numpy / Zero-Dependency)
# ==============================================================================

def _build_ulaw_tables() -> Tuple[np.ndarray, np.ndarray]:
    """Construct static precomputed lookup tables for G.711 mu-law encoding & decoding."""
    ulaw_to_pcm = np.zeros(256, dtype=np.int16)
    for i in range(256):
        u_val = ~i & 0xFF
        sign = u_val & 0x80
        exponent = (u_val >> 4) & 0x07
        mantissa = u_val & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        ulaw_to_pcm[i] = -sample if sign else sample

    pcm_to_ulaw = np.zeros(65536, dtype=np.uint8)
    bias = 0x84
    clip = 32635

    for pcm_val in range(-32768, 32768):
        sign = 0x80 if pcm_val < 0 else 0
        mag = abs(pcm_val)
        if mag > clip:
            mag = clip
        mag += bias
        exponent = 7
        for exp in range(7, -1, -1):
            if mag >= (1 << (exp + 7)):
                exponent = exp
                break
        mantissa = (mag >> (exponent + 3)) & 0x0F
        u_byte = ~(sign | (exponent << 4) | mantissa) & 0xFF
        idx = (pcm_val + 32768) & 0xFFFF
        pcm_to_ulaw[idx] = u_byte

    return ulaw_to_pcm, pcm_to_ulaw


_ULAW_TO_PCM_TABLE, _PCM_TO_ULAW_TABLE = _build_ulaw_tables()


def ulaw_decode(ulaw_bytes: bytes) -> bytes:
    """Fast vectorized G.711 u-law (8-bit) to 16-bit linear PCM decoding."""
    if not ulaw_bytes:
        return b""
    u_arr = np.frombuffer(ulaw_bytes, dtype=np.uint8)
    pcm_arr = _ULAW_TO_PCM_TABLE[u_arr]
    return pcm_arr.tobytes()


def ulaw_encode(pcm_bytes: bytes) -> bytes:
    """Fast vectorized 16-bit linear PCM to G.711 u-law (8-bit) encoding."""
    if not pcm_bytes:
        return b""
    pcm_arr = np.frombuffer(pcm_bytes, dtype=np.int16)
    indices = (pcm_arr.astype(np.int32) + 32768).astype(np.uint16)
    ulaw_arr = _PCM_TO_ULAW_TABLE[indices]
    return ulaw_arr.tobytes()


def resample_pcm16_fast(audio_bytes: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Ultra-low-latency (<1ms) 16-bit linear PCM resampling using vectorized interpolation."""
    if src_rate == dst_rate or not audio_bytes:
        return audio_bytes

    samples = np.frombuffer(audio_bytes, dtype=np.int16)
    if len(samples) == 0:
        return b""

    num_dst_samples = int(round(len(samples) * dst_rate / src_rate))
    if num_dst_samples == 0:
        return b""

    src_indices = np.linspace(0, len(samples) - 1, len(samples))
    dst_indices = np.linspace(0, len(samples) - 1, num_dst_samples)
    resampled = np.interp(dst_indices, src_indices, samples).astype(np.int16)
    return resampled.tobytes()


# ==============================================================================
# RFC 4566 SDP (SESSION DESCRIPTION PROTOCOL) PARSER & GENERATOR
# ==============================================================================

class SDPMessage:
    """Parses and generates RFC 4566 SDP for G.711 PCMU 8kHz mono audio."""

    def __init__(
        self,
        ip: str = "0.0.0.0",
        rtp_port: int = 10000,
        payload_type: int = 0,
        sample_rate: int = 8000,
        ptime: int = 20,
    ):
        self.ip = ip
        self.rtp_port = rtp_port
        self.payload_type = payload_type
        self.sample_rate = sample_rate
        self.ptime = ptime

    @classmethod
    def parse(cls, sdp_text: str) -> "SDPMessage":
        """Parse SDP string and extract remote connection IP, audio port, and codec."""
        msg = cls()
        for line in sdp_text.splitlines():
            line = line.strip()
            if line.startswith("c="):
                parts = line.split()
                if len(parts) >= 3:
                    msg.ip = parts[2]
            elif line.startswith("m=audio"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        msg.rtp_port = int(parts[1])
                    except ValueError:
                        pass
                if len(parts) >= 4:
                    try:
                        msg.payload_type = int(parts[3])
                    except ValueError:
                        pass
            elif line.startswith("a=ptime:"):
                try:
                    msg.ptime = int(line.split(":")[1])
                except ValueError:
                    pass
        return msg

    def to_sdp(self, session_id: str = "123456") -> str:
        """Generate SDP string offering/answering PCMU 8000Hz mono."""
        lines = [
            "v=0",
            f"o=CareHarmony {session_id} {session_id} IN IP4 {self.ip}",
            "s=CareHarmony Voice Session",
            f"c=IN IP4 {self.ip}",
            "t=0 0",
            f"m=audio {self.rtp_port} RTP/AVP 0",
            "a=rtpmap:0 PCMU/8000",
            "a=sendrecv",
            f"a=ptime:{self.ptime}",
        ]
        return "\r\n".join(lines) + "\r\n"


# ==============================================================================
# RFC 3261 SIP MESSAGE PARSER & SERIALIZER
# ==============================================================================

class SIPMessage:
    """RFC 3261 SIP Message Parser and Serializer."""

    def __init__(
        self,
        method: Optional[str] = None,
        uri: Optional[str] = None,
        status_code: Optional[int] = None,
        reason_phrase: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        body: str = "",
    ):
        self.method = method
        self.uri = uri
        self.status_code = status_code
        self.reason_phrase = reason_phrase
        self.headers = headers or {}
        self.body = body

    @classmethod
    def parse(cls, raw_data: bytes) -> "SIPMessage":
        """Parse raw SIP UDP datagram into SIPMessage."""
        text = raw_data.decode("utf-8", errors="replace")
        parts = text.split("\r\n\r\n", 1)
        header_section = parts[0]
        body = parts[1] if len(parts) > 1 else ""

        lines = header_section.split("\r\n")
        first_line = lines[0].strip()

        method, uri, status_code, reason = None, None, None, None
        if first_line.startswith("SIP/2.0"):
            status_parts = first_line.split(" ", 2)
            status_code = int(status_parts[1]) if len(status_parts) > 1 else 200
            reason = status_parts[2] if len(status_parts) > 2 else "OK"
        else:
            req_parts = first_line.split(" ", 2)
            method = req_parts[0]
            uri = req_parts[1] if len(req_parts) > 1 else ""

        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        return cls(
            method=method,
            uri=uri,
            status_code=status_code,
            reason_phrase=reason,
            headers=headers,
            body=body,
        )

    def get_header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def build_response(
        self,
        status_code: int,
        reason_phrase: str,
        to_tag: Optional[str] = None,
        sdp_body: str = "",
        local_contact: str = "sip:gateway@0.0.0.0:5060",
    ) -> bytes:
        """Construct SIP response matching this request."""
        to_val = self.get_header("To")
        if to_tag and "tag=" not in to_val:
            to_val = f"{to_val};tag={to_tag}"

        resp_headers = [
            f"SIP/2.0 {status_code} {reason_phrase}",
            f"Via: {self.get_header('Via')}",
            f"From: {self.get_header('From')}",
            f"To: {to_val}",
            f"Call-ID: {self.get_header('Call-ID')}",
            f"CSeq: {self.get_header('CSeq')}",
            f"Contact: <{local_contact}>",
            "User-Agent: CareHarmony-Voice-Gateway/2.0",
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
        ]

        if sdp_body:
            resp_headers.append("Content-Type: application/sdp")
            resp_headers.append(f"Content-Length: {len(sdp_body)}")
        else:
            resp_headers.append("Content-Length: 0")

        msg_str = "\r\n".join(resp_headers) + "\r\n\r\n" + sdp_body
        return msg_str.encode("utf-8")


# ==============================================================================
# RFC 3550 RTP PACKETIZER & DEPACKETIZER
# ==============================================================================

class RTPPacket:
    """RFC 3550 Real-time Transport Protocol (RTP) Packetizer / Depacketizer."""

    PAYLOAD_TYPE_PCMU = 0  # Standard G.711 u-law 8000 Hz

    @staticmethod
    def pack(
        payload: bytes,
        seq_num: int,
        timestamp: int,
        ssrc: int = 12345678,
        payload_type: int = 0,
        marker: int = 0,
    ) -> bytes:
        """Construct standard 12-byte RTP header followed by audio payload."""
        byte0 = (2 << 6)  # Version=2
        byte1 = ((marker & 1) << 7) | (payload_type & 0x7F)
        header = struct.pack("!BBHII", byte0, byte1, seq_num & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
        return header + payload

    @staticmethod
    def unpack(data: bytes) -> Tuple[int, int, int, int, bytes]:
        """Parse RTP header and return (payload_type, seq_num, timestamp, ssrc, payload)."""
        if len(data) < 12:
            return 0, 0, 0, 0, data
        byte0, byte1, seq_num, timestamp, ssrc = struct.unpack("!BBHII", data[:12])
        payload_type = byte1 & 0x7F
        csrc_count = byte0 & 0x0F
        has_extension = (byte0 >> 4) & 1
        offset = 12 + (csrc_count * 4)
        if has_extension and len(data) >= offset + 4:
            ext_len = struct.unpack("!H", data[offset + 2 : offset + 4])[0] * 4
            offset += 4 + ext_len
        payload = data[offset:] if len(data) >= offset else b""
        return payload_type, seq_num, timestamp, ssrc, payload


# ==============================================================================
# DYNAMIC RTP PORT ALLOCATOR (POOL 10000 - 20000)
# ==============================================================================

class RTPPortPool:
    """Thread-safe dynamic RTP port allocator (Range: 10000 - 20000)."""

    def __init__(self, min_port: int = 10000, max_port: int = 20000):
        self.min_port = min_port
        self.max_port = max_port
        self._available_ports: Set[int] = set(range(min_port, max_port + 1))
        self._allocated_ports: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def allocate_port(self, contact_id: str) -> int:
        """Allocate a unique RTP port for a call session."""
        async with self._lock:
            if contact_id in self._allocated_ports:
                return self._allocated_ports[contact_id]
            if not self._available_ports:
                raise RuntimeError("RTP Port Pool Exhausted (10000-20000 all in use)")
            port = self._available_ports.pop()
            self._allocated_ports[contact_id] = port
            logger.info(f"[PORT_POOL] Allocated dynamic RTP port {port} for Contact {contact_id} ({len(self._available_ports)} remaining)")
            return port

    async def release_port(self, contact_id: str) -> Optional[int]:
        """Release an allocated port back to the pool."""
        async with self._lock:
            port = self._allocated_ports.pop(contact_id, None)
            if port is not None:
                self._available_ports.add(port)
                logger.info(f"[PORT_POOL] Released RTP port {port} for Contact {contact_id}")
            return port


# ==============================================================================
# PER-CALL DEDICATED RTP MEDIA SESSION & SOCKET
# ==============================================================================

# SSRC for audio relayed between bridged legs, distinct from Cara's stream
# so the far end treats it as a new source rather than out-of-order packets.
BRIDGE_SSRC = 0x0B1D6E55


class CallRTPMediaProtocol(asyncio.DatagramProtocol):
    """AsyncIO Datagram Protocol for an individual call's RTP socket."""

    def __init__(self, session: "CallRTPMediaSession"):
        self.session = session
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport
        self.session.transport = transport
        logger.info(f"[RTP_SOCKET] Dedicated RTP socket active on {transport.get_extra_info('sockname')} for Contact {self.session.contact_id}")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        self.session.handle_inbound_rtp(data, addr)

    def error_received(self, exc: Exception):
        logger.warning(f"[RTP_SOCKET] Socket error for Contact {self.session.contact_id}: {exc}")

    def connection_lost(self, exc: Optional[Exception]):
        logger.info(f"[RTP_SOCKET] Socket closed for Contact {self.session.contact_id} (exc: {exc})")


class CallRTPMediaSession:
    """Dedicated RTP audio socket & transport for a single active call."""

    def __init__(
        self,
        contact_id: str,
        local_port: int,
        remote_addr: Tuple[str, int],
        host: str = "0.0.0.0",
        echo_mode: bool = False,
        pipeline_session: Optional[Any] = None,
    ):
        self.contact_id = contact_id
        self.local_port = local_port
        self.remote_addr = remote_addr
        self.host = host
        self.echo_mode = echo_mode
        self.pipeline_session = pipeline_session
        self.transport: Optional[asyncio.DatagramTransport] = None
        self._is_active = False
        self._echo_seq = 0
        self._echo_ts = 0
        # Set while this leg is bridged to another call (agent transfer):
        # inbound audio is relayed to the peer instead of Nova Sonic.
        self.bridge_peer: Optional["CallRTPMediaSession"] = None
        self._bridge_seq = 0
        self._bridge_ts = 0
        # Set while we listen to an agent leg to tell a person from voicemail
        self.answer_detector: Optional[AgentAnswerDetector] = None
        self.answer_verdict: Optional[asyncio.Future] = None
        # Caller audio only flows to Cara after the 200 OK: before it, the
        # line carries ringback / carrier early media, never the patient.
        self.answered = False
        # Caller frames in arrival order. They wait here (capped) until the
        # pipeline is attached, so a "hello" right on pickup isn't lost.
        self._inbound: Deque[bytes] = deque()
        self._inbound_ready = asyncio.Event()
        self._pump_task: Optional[asyncio.Task] = None
        self.early_frames_dropped = 0

    async def start(self):
        """Bind dedicated UDP socket for this call."""
        self._is_active = True
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: CallRTPMediaProtocol(self),
            local_addr=(self.host, self.local_port),
        )
        self.transport = transport

    def stop(self):
        """Close dedicated UDP socket."""
        self._is_active = False
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
        self._inbound.clear()
        if self.transport and not self.transport.is_closing():
            self.transport.close()

    def attach_pipeline(self, pipeline_session: Any):
        """Route caller audio to the pipeline, starting with anything buffered."""
        self.pipeline_session = pipeline_session
        if self._inbound:
            logger.info(
                f"[EARLY_AUDIO] {len(self._inbound)} caller frames "
                f"({len(self._inbound) * 20} ms) held since pickup handed to the pipeline "
                f"for Contact {self.contact_id}"
            )
        self._ensure_pump()

    def _ensure_pump(self):
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump_inbound())
        if self._inbound:
            self._inbound_ready.set()

    def _queue_caller_audio(self, pcm_8k: bytes):
        # Before the pipeline is attached keep only the newest few seconds;
        # once it is attached the cap only guards against a stalled loop.
        cap = INBOUND_QUEUE_MAX_FRAMES if self.pipeline_session else EARLY_AUDIO_MAX_FRAMES
        while len(self._inbound) >= cap:
            self._inbound.popleft()
            self.early_frames_dropped += 1
        self._inbound.append(pcm_8k)
        if self.pipeline_session:
            self._inbound_ready.set()

    async def _pump_inbound(self):
        """Deliver caller frames to the pipeline one at a time, in order."""
        while self._is_active:
            if not self._inbound or not self.pipeline_session:
                self._inbound_ready.clear()
                await self._inbound_ready.wait()
                continue
            pcm_8k = self._inbound.popleft()
            try:
                await self.pipeline_session.push_audio(pcm_8k)
            except Exception as e:
                logger.debug(f"Caller audio delivery note for {self.contact_id}: {e}")

    def handle_inbound_rtp(self, data: bytes, addr: Tuple[str, int]):
        """Process incoming RTP packet for this specific call."""
        if not self._is_active:
            return

        # Symmetrical RTP latching: on initial arrival, latch remote endpoint if port shifted
        if self.remote_addr != addr:
            logger.info(f"[RTP] Latched symmetrical RTP from {self.remote_addr} to {addr} for Contact {self.contact_id}")
            self.remote_addr = addr

        payload_type, seq_num, timestamp, ssrc, payload = RTPPacket.unpack(data)
        if not payload:
            return

        if self.answer_detector is not None:
            pcm_8k = ulaw_decode(payload)
            for i in range(0, len(pcm_8k), 320):
                verdict = self.answer_detector.feed_frame(pcm_8k[i:i + 320])
                if verdict:
                    self.answer_detector = None
                    if self.answer_verdict and not self.answer_verdict.done():
                        self.answer_verdict.set_result(verdict)
                    break
            return

        if self.bridge_peer:
            self.bridge_peer.send_bridged_audio(payload)
            return

        # 1. ISOLATED ECHO MODE TEST (PHASE 2 GATE)
        if self.echo_mode:
            self.send_rtp_packet(
                payload=payload,
                seq_num=self._echo_seq,
                timestamp=self._echo_ts,
            )
            self._echo_seq = (self._echo_seq + 1) & 0xFFFF
            self._echo_ts = (self._echo_ts + len(payload)) & 0xFFFFFFFF
            if self._echo_seq % 100 == 0:
                logger.info(f"[RTP_ECHO] Echoed 100 packets back to {addr} for Contact {self.contact_id}")
            return

        # 2. FULL-DUPLEX NOVA CONVERSATIONAL PIPELINE MODE
        if self.answered:
            self._queue_caller_audio(ulaw_decode(payload))

    def send_bridged_audio(self, payload: bytes):
        """Relay audio from the bridged peer leg, as its own RTP stream (new SSRC)."""
        self.send_rtp_packet(payload, self._bridge_seq, self._bridge_ts, ssrc=BRIDGE_SSRC)
        self._bridge_seq = (self._bridge_seq + 1) & 0xFFFF
        self._bridge_ts = (self._bridge_ts + len(payload)) & 0xFFFFFFFF

    def send_rtp_packet(self, payload: bytes, seq_num: int, timestamp: int, ssrc: int = 12345678):
        """Transmit 20ms PCMU RTP packet to remote telephony destination."""
        if not self.transport or self.transport.is_closing():
            return

        packet = RTPPacket.pack(
            payload=payload,
            seq_num=seq_num,
            timestamp=timestamp,
            ssrc=ssrc,
            payload_type=RTPPacket.PAYLOAD_TYPE_PCMU,
        )
        self.transport.sendto(packet, self.remote_addr)
        if seq_num % 100 == 0 or seq_num < 5:
            logger.info(
                f"[RTP_OUT] Sent 20ms PCMU packet (seq={seq_num}, ts={timestamp}, size={len(payload)}B) to {self.remote_addr} for Contact {self.contact_id}"
            )


# ==============================================================================
# GLOBAL SIP & RTP MEDIA SERVER
# ==============================================================================

class SIPRTPMediaServer:
    """Manages dynamic per-call RTP sockets and port allocation for full call isolation."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 10000,
        sample_rate: int = 8000,
        echo_mode: bool = False,
    ):
        self.host = host
        self.port = port  # base/default port
        self.sample_rate = sample_rate
        self.echo_mode = echo_mode
        self.port_pool = RTPPortPool(min_port=10000, max_port=20000)
        self.call_sessions: Dict[str, CallRTPMediaSession] = {}
        self.remote_endpoints: Dict[str, Tuple[str, int]] = {}
        self.active_sessions: Dict[str, Any] = {}
        self._is_running = False

    async def start(self):
        self._is_running = True
        logger.info(f"SIP/RTP Media Manager active (Dynamic Ports: 10000-20000, EchoMode={self.echo_mode})")

    def stop(self):
        self._is_running = False
        for cid, session in list(self.call_sessions.items()):
            session.stop()
        self.call_sessions.clear()
        self.remote_endpoints.clear()
        self.active_sessions.clear()
        logger.info("SIP/RTP Media Manager stopped.")

    async def create_call_session(
        self,
        contact_id: str,
        remote_addr: Tuple[str, int],
        pipeline_session: Optional[Any] = None,
    ) -> Tuple[int, CallRTPMediaSession]:
        """Allocate a unique RTP port and open an isolated UDP socket for the call."""
        local_port = await self.port_pool.allocate_port(contact_id)
        session = CallRTPMediaSession(
            contact_id=contact_id,
            local_port=local_port,
            remote_addr=remote_addr,
            host=self.host,
            echo_mode=self.echo_mode,
            pipeline_session=pipeline_session,
        )
        await session.start()
        self.call_sessions[contact_id] = session
        self.remote_endpoints[contact_id] = remote_addr
        return local_port, session

    async def close_call_session(self, contact_id: str):
        """Terminate call media socket and free its dynamic port."""
        session = self.call_sessions.pop(contact_id, None)
        if session:
            session.stop()
        await self.port_pool.release_port(contact_id)
        self.remote_endpoints.pop(contact_id, None)
        self.active_sessions.pop(contact_id, None)

    def register_remote_endpoint(self, contact_id: str, remote_addr: Tuple[str, int]):
        self.remote_endpoints[contact_id] = remote_addr
        if contact_id in self.call_sessions:
            self.call_sessions[contact_id].remote_addr = remote_addr

    def register_session(self, contact_id: str, pipeline_session: Any, remote_addr: Optional[Tuple[str, int]] = None):
        self.active_sessions[contact_id] = pipeline_session
        if contact_id in self.call_sessions:
            self.call_sessions[contact_id].attach_pipeline(pipeline_session)

    def mark_answered(self, contact_id: str):
        """The far end answered: from now on caller audio is for Cara."""
        session = self.call_sessions.get(contact_id)
        if session:
            session.answered = True

    def unregister_session(self, contact_id: str):
        asyncio.create_task(self.close_call_session(contact_id))

    def bridge_calls(self, call_a: str, call_b: str) -> bool:
        """Relay audio both ways between two call legs; Cara's audio is muted."""
        a = self.call_sessions.get(call_a)
        b = self.call_sessions.get(call_b)
        if not a or not b:
            return False
        a.bridge_peer = b
        b.bridge_peer = a
        a.pipeline_session = None
        b.pipeline_session = None
        logger.info(f"[BRIDGE] Joined audio of {call_a} <-> {call_b}")
        return True

    def send_rtp_packet(self, payload: bytes, contact_id: str, seq_num: int, timestamp: int):
        """Send outbound audio packet through the call's dedicated RTP socket."""
        session = self.call_sessions.get(contact_id)
        if session and session.bridge_peer:
            return  # leg is bridged to a human agent; Cara stays silent
        if session:
            session.send_rtp_packet(payload, seq_num, timestamp)
        else:
            logger.debug(f"Dropped outbound RTP packet: no active socket for Contact {contact_id}")


# ==============================================================================
# ASYNCIO UDP SIP SIGNALING PROTOCOL & SERVER (PORT 5060)
# ==============================================================================

class SIPSignalingProtocol(asyncio.DatagramProtocol):
    """AsyncIO Datagram Protocol for receiving and replying to SIP messages."""

    def __init__(self, server: "SIPSignalingServer"):
        self.server = server
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport
        self.server.transport = transport
        logger.info(f"SIP Signaling Socket active on {transport.get_extra_info('sockname')}")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        self.server.handle_inbound_sip(data, addr)

    def error_received(self, exc: Exception):
        logger.warning(f"SIP Socket error received: {exc}")

    def connection_lost(self, exc: Optional[Exception]):
        logger.info(f"SIP Socket closed (exc: {exc})")


RING_TIMEOUT_SECONDS = 20
AGENT_RING_TIMEOUT_SECONDS = 25

SIP_FAILURE_REASONS = {
    486: "BUSY",
    480: "UNAVAILABLE",       # phone off / unreachable
    408: "NO_ANSWER",         # request timeout = no answer
    603: "REJECTED",          # explicit decline
    487: "CANCELLED",         # we cancelled it (ring timeout path)
    404: "FAILED",            # invalid number
    503: "FAILED",            # carrier/service error
}


class SIPSignalingServer:
    """RFC 3261 SIP User Agent Server managing call setup, SDP negotiation, and teardown."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 5060,
        public_ip: str = "100.49.33.242",
        media_server: Optional[SIPRTPMediaServer] = None,
        on_call_started: Optional[Callable[[str, SDPMessage, Tuple[str, int]], Any]] = None,
        on_call_ended: Optional[Callable[[str], Any]] = None,
        on_call_failed: Optional[Callable[..., Any]] = None,
        on_call_preparing: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ):
        self.host = host
        self.port = port
        self.public_ip = public_ip
        self.media_server = media_server
        self.on_call_started = on_call_started
        self.on_call_ended = on_call_ended
        self.on_call_failed = on_call_failed
        # Builds Cara for an outbound call before it is answered (see PREPARE_PIPELINE_AT)
        self.on_call_preparing = on_call_preparing
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.active_dialogs: Dict[str, Dict[str, Any]] = {}
        self._is_running = False
        # agent leg Call-ID -> patient leg Call-ID, for live transfers
        self.agent_legs: Dict[str, str] = {}

    async def start(self):
        self._is_running = True
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: SIPSignalingProtocol(self),
                local_addr=(self.host, self.port),
            )
            self.transport = transport
            logger.info(f"SIP Signaling Server listening on UDP {self.host}:{self.port} (Public IP: {self.public_ip})")
        except Exception as e:
            logger.error(f"Could not bind SIP Signaling Server on {self.host}:{self.port}: {e}")

    def stop(self):
        self._is_running = False
        if self.transport and not self.transport.is_closing():
            self.transport.close()
        logger.info("SIP Signaling Server stopped.")

    @staticmethod
    def parse_invite_headers(sip_message: str) -> dict:
        """
        Extract all SIP headers from INVITE into a dict.
        Preserves X-* custom headers for AMD and future use.
        """
        headers = {}
        for line in sip_message.split('\r\n'):
            if ':' in line:
                key, _, value = line.partition(':')
                headers[key.strip()] = value.strip()
        return headers

    def handle_inbound_sip(self, data: bytes, addr: Tuple[str, int]):
        """Parse incoming SIP message and drive state machine."""
        if not self._is_running:
            return

        logger.info(f"[SIP_IN] Received {len(data)} bytes from {addr}: {data[:100]!r}")
        msg = SIPMessage.parse(data)
        call_id = msg.get_header("Call-ID")
        raw_text = data.decode("utf-8", errors="replace")
        parsed_headers = self.parse_invite_headers(raw_text)

        # 1. Handle SIP Status Responses (UAC Outbound Mode: 100 Trying, 180 Ringing, 200 OK, 4xx/5xx/6xx)
        if msg.status_code is not None:
            if msg.status_code >= 300:
                self._ack_final_error(msg, addr)
            dialog = self.active_dialogs.get(call_id)
            if not dialog:
                logger.warning(f"[SIP_RESP] Received {msg.status_code} {msg.reason_phrase} for unknown Call-ID {call_id}")
                return

            if msg.status_code == 100:
                logger.info(f"[SIP_RESP] 100 Trying for Call-ID {call_id}")
            elif msg.status_code in (180, 183):
                dialog["state"] = "RINGING"
                logger.info(f"[SIP_RESP] {msg.status_code} {msg.reason_phrase} (Ringing) for Call-ID {call_id}")
                if PREPARE_PIPELINE_AT == "ringing":
                    self._prepare_pipeline(call_id, dialog)
            elif msg.status_code == 200:
                logger.info(f"[SIP_RESP] 200 OK (Call Answered) for Call-ID {call_id}")
                if dialog.get("ring_timeout_task"):
                    dialog["ring_timeout_task"].cancel()
                dialog["state"] = "CONNECTED"
                dialog["remote_sip_ip"] = addr[0]
                dialog["remote_sip_port"] = addr[1]
                to_hdr = msg.get_header("To")
                to_tag = ""
                if "tag=" in to_hdr:
                    to_tag = to_hdr.split("tag=")[1].split(";")[0]
                dialog["to_tag"] = to_tag

                if msg.body:
                    remote_sdp = SDPMessage.parse(msg.body)
                    remote_rtp_ip = remote_sdp.ip if remote_sdp.ip != "0.0.0.0" else addr[0]
                    remote_rtp_port = remote_sdp.rtp_port
                    dialog["remote_rtp_addr"] = (remote_rtp_ip, remote_rtp_port)
                    dialog["remote_sdp"] = remote_sdp
                    if self.media_server:
                        self.media_server.register_remote_endpoint(call_id, (remote_rtp_ip, remote_rtp_port))

                # Send SIP ACK per RFC 3261 §13.2.2.4
                contact_hdr = msg.get_header("Contact")
                if "<" in contact_hdr and ">" in contact_hdr:
                    ack_request_uri = contact_hdr.split("<")[1].split(">")[0].strip()
                elif contact_hdr:
                    ack_request_uri = contact_hdr.split(";")[0].strip()
                else:
                    ack_request_uri = dialog.get("to_uri") or f"sip:{dialog.get('destination', '')}"

                dialog["to_hdr"] = to_hdr
                dialog["contact_uri"] = ack_request_uri

                cseq_num = msg.get_header("CSeq").split()[0] if msg.get_header("CSeq") else "1"

                ack_headers = [
                    f"ACK {ack_request_uri} SIP/2.0",
                    f"Via: SIP/2.0/UDP {self.public_ip}:{self.port};branch=z9hG4bK-{uuid.uuid4().hex[:8]}",
                    f"From: {dialog.get('from_hdr', '')}",
                    f"To: {to_hdr}",
                    f"Call-ID: {call_id}",
                    f"CSeq: {cseq_num} ACK",
                    f"Contact: <sip:cara@{self.public_ip}:{self.port}>",
                    "Max-Forwards: 70",
                    "User-Agent: CareHarmony-Voice-Gateway/2.0",
                ]

                record_route = msg.get_header("Record-Route")
                if record_route:
                    ack_headers.append(f"Route: {record_route}")
                    dialog["record_route"] = record_route

                ack_headers.append("Content-Length: 0")
                ack_msg = "\r\n".join(ack_headers) + "\r\n\r\n"
                if self.transport:
                    self.transport.sendto(ack_msg.encode("utf-8"), addr)
                    logger.info(f"[SIP_ACK] Sent ACK (URI: {ack_request_uri}) to {addr} for Call-ID {call_id}")

                answer_future = dialog.get("answer_future")
                if answer_future is not None:
                    # Agent leg of a transfer: bridge it, never start a Cara pipeline
                    if not answer_future.done():
                        answer_future.set_result(True)
                elif self.on_call_started and "remote_sdp" in dialog and not dialog.get("pipeline_started"):
                    dialog["pipeline_started"] = True
                    if self.media_server:
                        self.media_server.mark_answered(call_id)
                    asyncio.create_task(self.on_call_started(
                        call_id,
                        dialog["remote_sdp"],
                        dialog["remote_rtp_addr"],
                        dialog.get("patient_meta", {})
                    ))

            elif msg.status_code >= 400:
                reason = SIP_FAILURE_REASONS.get(msg.status_code, "FAILED")
                logger.info(
                    f"[SIP_ERROR] Outbound call failed: {msg.status_code} "
                    f"{msg.reason_phrase} ({reason}) for Call-ID {call_id}"
                )
                if dialog:
                    if dialog.get("ring_timeout_task"):
                        dialog["ring_timeout_task"].cancel()
                    asyncio.create_task(self._finalize_call_failure(
                        call_id, dialog, reason=reason, sip_code=msg.status_code
                    ))
                else:
                    if call_id in self.active_dialogs:
                        del self.active_dialogs[call_id]
                    if self.media_server:
                        self.media_server.unregister_session(call_id)
                    if self.on_call_ended:
                        asyncio.create_task(self.on_call_ended(call_id))
            return

        # 2. Handle Inbound SIP Requests (OPTIONS, INVITE, ACK, BYE)
        if msg.method == "OPTIONS":
            resp = msg.build_response(200, "OK", local_contact=f"sip:{self.public_ip}:{self.port}")
            if self.transport:
                self.transport.sendto(resp, addr)
            logger.debug(f"[SIP] Answered OPTIONS ping from {addr}")

        elif msg.method == "INVITE":
            logger.info(f"[SIP] Inbound INVITE from {addr} (Call-ID: {call_id})")
            remote_sdp = SDPMessage.parse(msg.body)
            remote_rtp_ip = remote_sdp.ip if remote_sdp.ip != "0.0.0.0" else addr[0]
            remote_rtp_port = remote_sdp.rtp_port
            logger.info(
                f"[SIP/SDP] Negotiated remote SDP for Call-ID {call_id}: "
                f"Remote RTP = {remote_rtp_ip}:{remote_rtp_port}, Codec = PCMU/8000"
            )

            to_tag = uuid.uuid4().hex[:8]
            self.active_dialogs[call_id] = {
                "remote_sip_addr": addr,
                "remote_rtp_addr": (remote_rtp_ip, remote_rtp_port),
                "to_tag": to_tag,
                "remote_sdp": remote_sdp,
                "state": "RINGING",
            }

            # 1. Allocate dynamic unique RTP port for this call
            asyncio.create_task(self._process_invite(msg, call_id, to_tag, addr, remote_sdp, (remote_rtp_ip, remote_rtp_port)))

        elif msg.method == "ACK":
            logger.info(f"[SIP] Received ACK for Call-ID {call_id} - Call is ACTIVE and Full-Duplex")
            if call_id in self.active_dialogs:
                self.active_dialogs[call_id]["state"] = "CONNECTED"

        elif msg.method == "BYE":
            logger.info(f"[SIP] Received BYE for Call-ID {call_id} - Terminating Call")
            resp = msg.build_response(200, "OK", local_contact=f"sip:{self.public_ip}:{self.port}")
            if self.transport:
                self.transport.sendto(resp, addr)

            dialog = self.active_dialogs.pop(call_id, None)

            if self.media_server:
                self.media_server.unregister_session(call_id)

            other_leg = self._bridged_leg(call_id, dialog)
            if other_leg:
                logger.info(f"[BRIDGE] {call_id} hung up, ending bridged leg {other_leg}")
                asyncio.create_task(self.send_bye(other_leg))

            if self.on_call_ended:
                asyncio.create_task(self.on_call_ended(call_id))

    def _bridged_leg(self, call_id: str, dialog: Optional[Dict[str, Any]]) -> Optional[str]:
        """The other leg of a transfer bridge, if this call is part of one."""
        if call_id in self.agent_legs:
            return self.agent_legs.pop(call_id)
        agent_leg = (dialog or {}).get("agent_leg")
        if agent_leg:
            self.agent_legs.pop(agent_leg, None)
        return agent_leg

    async def dial_agent_and_bridge(
        self,
        patient_call_id: str,
        agent_phone: str,
        ring_timeout: float = AGENT_RING_TIMEOUT_SECONDS,
    ) -> bool:
        """Call the human agent and, once they answer, join them to the patient.

        Returns False if the agent doesn't answer, rejects the call, or the
        patient hung up meanwhile; the patient's call is left untouched.
        """
        patient_dialog = self.active_dialogs.get(patient_call_id)
        if not patient_dialog:
            logger.warning(f"[TRANSFER] Patient call {patient_call_id} is gone; not dialing agent")
            return False

        loop = asyncio.get_running_loop()
        answer_future: asyncio.Future = loop.create_future()
        ring_deadline = loop.time() + ring_timeout
        agent_call_id = await self.originate_call(
            destination_phone=agent_phone,
            source_phone=patient_dialog.get("source") or "+12819568434",
            patient_meta={"agent_leg_for": patient_call_id},
            answer_future=answer_future,
            ring_timeout=ring_timeout,
        )
        self.agent_legs[agent_call_id] = patient_call_id
        logger.info(f"[TRANSFER] Dialing agent {agent_phone} (Call-ID {agent_call_id}) for {patient_call_id}")

        try:
            answered = await asyncio.wait_for(asyncio.shield(answer_future), timeout=ring_timeout + 5)
        except asyncio.TimeoutError:
            answered = False

        if answered and patient_call_id in self.active_dialogs:
            verdict = await self._screen_agent_answer(agent_call_id, ring_deadline)
            # A silent pickup is a person waiting for the caller to speak; machines
            # give a greeting or beep (call 10316d32 hung up on a silent agent)
            answered = verdict in (HUMAN, SILENCE)

        if answered and patient_call_id in self.active_dialogs and self.media_server:
            if self.media_server.bridge_calls(patient_call_id, agent_call_id):
                self.active_dialogs[patient_call_id]["agent_leg"] = agent_call_id
                logger.info(f"[TRANSFER] Agent answered; {patient_call_id} is now talking to {agent_phone}")
                return True

        logger.info(f"[TRANSFER] Agent leg {agent_call_id} not connected (answered={answered})")
        self.agent_legs.pop(agent_call_id, None)
        agent_dialog = self.active_dialogs.get(agent_call_id)
        if agent_dialog:
            if agent_dialog.get("state") == "CONNECTED":
                await self.send_bye(agent_call_id)
            else:
                await self._send_cancel(agent_call_id, agent_dialog)
                await self._finalize_call_failure(agent_call_id, agent_dialog, reason="CANCELLED")
        return False

    async def _screen_agent_answer(self, agent_call_id: str, ring_deadline: float) -> str:
        """Listen to the answered agent leg and decide HUMAN / MACHINE / SILENCE.

        Carriers report "answered" while still playing their own ringing or
        voicemail, so the patient is joined only when a person says hello.
        """
        session = self.media_server.call_sessions.get(agent_call_id) if self.media_server else None
        if not session:
            return "NO_MEDIA"
        loop = asyncio.get_running_loop()
        detector = AgentAnswerDetector()
        verdict_future: asyncio.Future = loop.create_future()
        session.answer_verdict = verdict_future
        session.answer_detector = detector
        # Keep listening through carrier ringing up to the ring deadline,
        # but always allow a full listening window after the answer.
        listen_s = detector.listen_frames * 0.02
        wait_s = max(ring_deadline - loop.time(), listen_s) + 1.0
        started = loop.time()
        try:
            verdict = await asyncio.wait_for(verdict_future, timeout=wait_s)
        except asyncio.TimeoutError:
            verdict = "NO_AUDIO" if detector.frames_seen == 0 else "RINGING_TIMEOUT"
        finally:
            session.answer_detector = None
            session.answer_verdict = None
        logger.info(
            f"[AGENT_AMD] {agent_call_id} verdict={verdict} ({detector.decided_by or 'no decision'}) "
            f"after {loop.time() - started:.1f}s, frames={detector.frames_seen}, "
            f"ringing_frames={detector.ringing_frames}"
        )
        return verdict

    def _ack_final_error(self, msg: "SIPMessage", addr: Tuple[str, int]):
        """ACK a 3xx-6xx response to our INVITE (RFC 3261 17.1.1.3) so the far
        end stops retransmitting it."""
        if not self.transport or "INVITE" not in msg.get_header("CSeq"):
            return
        to_hdr = msg.get_header("To")
        request_uri = to_hdr.split("<")[1].split(">")[0] if "<" in to_hdr else to_hdr.split(";")[0]
        cseq_num = msg.get_header("CSeq").split()[0]
        ack = "\r\n".join([
            f"ACK {request_uri} SIP/2.0",
            f"Via: {msg.get_header('Via')}",
            f"From: {msg.get_header('From')}",
            f"To: {to_hdr}",
            f"Call-ID: {msg.get_header('Call-ID')}",
            f"CSeq: {cseq_num} ACK",
            "Max-Forwards: 70",
            "Content-Length: 0",
        ]) + "\r\n\r\n"
        self.transport.sendto(ack.encode("utf-8"), addr)

    async def originate_call(
        self,
        destination_phone: str,
        source_phone: str = "+12819568434",
        vc_host: str = "cafawc0zxzbcpxusmsonrd.voiceconnector.chime.aws",
        vc_port: int = 5060,
        patient_meta: Optional[Dict[str, Any]] = None,
        answer_future: Optional[asyncio.Future] = None,
        ring_timeout: float = RING_TIMEOUT_SECONDS,
    ) -> str:
        """Originate an outbound SIP INVITE call via Amazon Chime Voice Connector."""
        call_id = str(uuid.uuid4())
        from_tag = uuid.uuid4().hex[:8]
        branch = f"z9hG4bK-{uuid.uuid4().hex[:10]}"

        # Allocate dynamic unique RTP port from pool 10000-20000
        local_rtp_port = 10000
        if self.media_server:
            local_rtp_port, _ = await self.media_server.create_call_session(
                contact_id=call_id,
                remote_addr=(vc_host, vc_port),
            )

        sdp_offer = SDPMessage(
            ip=self.public_ip,
            rtp_port=local_rtp_port,
            payload_type=0,
            sample_rate=8000,
            ptime=20,
        ).to_sdp(session_id=call_id[:10])

        clean_source = "+" + "".join(c for c in source_phone if c.isdigit())
        clean_dest = "+" + "".join(c for c in destination_phone if c.isdigit())

        to_uri = f"sip:{clean_dest}@{vc_host}"
        from_hdr = f"<sip:{clean_source}@{vc_host}>;tag={from_tag}"
        to_hdr = f"<sip:{clean_dest}@{vc_host}>"

        invite_lines = [
            f"INVITE {to_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.public_ip}:{self.port};branch={branch}",
            f"From: {from_hdr}",
            f"To: {to_hdr}",
            f"Call-ID: {call_id}",
            "CSeq: 1 INVITE",
            f"Contact: <sip:{clean_source}@{self.public_ip}:{self.port}>",
            f"P-Asserted-Identity: <sip:{clean_source}@{vc_host}>",
            f"Remote-Party-ID: <sip:{clean_source}@{vc_host}>;party=calling;screen=yes;privacy=off",
            "Max-Forwards: 70",
            "User-Agent: CareHarmony-Voice-Gateway/2.0",
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
            "Content-Type: application/sdp",
            f"Content-Length: {len(sdp_offer)}",
        ]
        invite_msg = "\r\n".join(invite_lines) + "\r\n\r\n" + sdp_offer

        loop = asyncio.get_running_loop()
        addr_info = await loop.getaddrinfo(vc_host, vc_port)
        remote_ip = addr_info[0][4][0]

        patient_id = (patient_meta or {}).get("patient_id") or (patient_meta or {}).get("empi") or ""

        dialog = {
            "call_id": call_id,
            "patient_id": patient_id,
            "remote_sip_addr": (remote_ip, vc_port),
            "remote_sip_host": vc_host,
            "remote_sip_ip": remote_ip,
            "remote_sip_port": vc_port,
            "local_rtp_port": local_rtp_port,
            "destination": clean_dest,
            "source": clean_source,
            "from_hdr": from_hdr,
            "to_hdr_original": to_hdr,
            "to_hdr": to_hdr,
            "to_uri": to_uri,
            "request_uri": to_uri,
            "invite_branch": branch,
            "local_cseq": 1,
            "patient_meta": patient_meta or {},
            "state": "TRYING",
            "ring_timeout": ring_timeout,
        }
        if answer_future is not None:
            dialog["answer_future"] = answer_future
        self.active_dialogs[call_id] = dialog

        if self.transport:
            self.transport.sendto(invite_msg.encode("utf-8"), (remote_ip, vc_port))
            logger.info(f"[SIP_OUT] Sent INVITE for {destination_phone} to {remote_ip}:{vc_port} (Call-ID: {call_id}, RTP Port: {local_rtp_port})")

        dialog["ring_timeout_task"] = asyncio.create_task(
            self._ring_timeout_watchdog(call_id)
        )
        if PREPARE_PIPELINE_AT == "invite":
            self._prepare_pipeline(call_id, dialog)

        return call_id

    def _prepare_pipeline(self, call_id: str, dialog: Dict[str, Any]):
        """Build Cara and open Nova while the phone rings, so both are ready at
        pickup. Patient legs only; an unanswered call tears it down again."""
        if (
            not self.on_call_preparing
            or "answer_future" in dialog
            or dialog.get("pipeline_preparing")
        ):
            return
        dialog["pipeline_preparing"] = True
        asyncio.create_task(self.on_call_preparing(call_id, dialog.get("patient_meta", {})))

    async def _ring_timeout_watchdog(self, call_id: str):
        """
        If still ringing after RING_TIMEOUT_SECONDS with no 200 OK
        or final failure response, CANCEL and record as NO_ANSWER.
        """
        try:
            timeout = (self.active_dialogs.get(call_id) or {}).get("ring_timeout", RING_TIMEOUT_SECONDS)
            await asyncio.sleep(timeout)
            dialog = self.active_dialogs.get(call_id)
            if not dialog or dialog.get("state") in ("CONNECTED", "TERMINATED"):
                return
            logger.info(
                f"[SIP_RING_TIMEOUT] No answer after "
                f"{timeout}s for {call_id} — sending CANCEL"
            )
            await self._send_cancel(call_id, dialog)
            await self._finalize_call_failure(
                call_id, dialog, reason="NO_ANSWER", sip_code=None
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(f"[SIP_RING_TIMEOUT_ERROR] for {call_id}")

    async def _send_cancel(self, call_id: str, dialog: dict):
        if not self.transport:
            return
        remote_addr = dialog.get("remote_sip_addr") or (
            dialog.get("remote_sip_ip"), dialog.get("remote_sip_port", 5060)
        )
        req_uri = dialog.get("request_uri") or dialog.get("to_uri", "")
        branch = dialog.get("invite_branch", "")
        from_hdr = dialog.get("from_hdr", "")
        to_hdr = dialog.get("to_hdr_original", dialog.get("to_hdr", ""))
        cseq = dialog.get("local_cseq", 1)

        cancel_lines = [
            f"CANCEL {req_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.public_ip}:{self.port};branch={branch}",
            f"From: {from_hdr}",
            f"To: {to_hdr}",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} CANCEL",
            "Max-Forwards: 70",
            "Content-Length: 0",
        ]
        cancel_msg = "\r\n".join(cancel_lines) + "\r\n\r\n"
        self.transport.sendto(cancel_msg.encode("utf-8"), remote_addr)
        logger.info(f"[SIP_CANCEL] Sent CANCEL for {call_id}")

    async def _finalize_call_failure(
        self, call_id: str, dialog: dict, reason: str, sip_code: Optional[int] = None
    ):
        answer_future = dialog.get("answer_future")
        if answer_future is not None:
            # Agent leg of a transfer failed: report back to the waiting
            # transfer, and leave the patient's record alone.
            logger.info(f"[TRANSFER] Agent leg {call_id} failed: {reason} (sip_code={sip_code})")
            if not answer_future.done():
                answer_future.set_result(False)
            self.active_dialogs.pop(call_id, None)
            self.agent_legs.pop(call_id, None)
            if self.media_server:
                self.media_server.unregister_session(call_id)
            return

        patient_id = dialog.get("patient_id") or dialog.get("patient_meta", {}).get("patient_id")
        logger.info(
            f"[CALL_FAILED] call_id={call_id} patient_id={patient_id} "
            f"reason={reason} sip_code={sip_code}"
        )
        if self.on_call_failed:
            try:
                res = self.on_call_failed(
                    call_id=call_id, patient_id=patient_id,
                    reason=reason, sip_code=sip_code,
                )
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                logger.exception(
                    f"[CALL_FAILED_CALLBACK_ERROR] for {call_id}"
                )
        self.active_dialogs.pop(call_id, None)
        if self.media_server:
            self.media_server.unregister_session(call_id)
        if self.on_call_ended:
            try:
                res = self.on_call_ended(call_id)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

    async def send_bye(self, call_id: str):
        """Send SIP BYE to terminate an active call."""
        dialog = self.active_dialogs.get(call_id)
        if not dialog:
            logger.debug(
                f"[SIP_BYE] No active dialog for {call_id} — "
                f"already terminated or never registered, skipping"
            )
            return  # idempotent — safe to call multiple times
        if not self.transport:
            logger.warning(
                f"[SIP_BYE] No transport available for {call_id} — "
                f"cannot send BYE"
            )
            return

        remote_sip_ip = dialog.get("remote_sip_ip")
        remote_sip_port = dialog.get("remote_sip_port", 5060)

        if not remote_sip_ip:
            import socket
            host = dialog.get(
                "remote_sip_host",
                "cafawc0zxzbcpxusmsonrd.voiceconnector.chime.aws",
            )
            port = dialog.get("remote_sip_port", 5060)
            try:
                resolved = socket.getaddrinfo(
                    host, port, socket.AF_INET, socket.SOCK_DGRAM
                )[0][4][0]
                remote_sip_addr = (resolved, port)
                logger.info(f"[SIP_BYE] Resolved {host} -> {resolved} for {call_id}")
            except socket.gaierror as e:
                logger.error(
                    f"[SIP_BYE] DNS resolution failed for {host}: {e} — "
                    f"cannot send BYE for {call_id}"
                )
                return
        else:
            remote_sip_addr = (remote_sip_ip, remote_sip_port)

        # ── Request-URI ─────────────────────────────────────────────
        # Per RFC 3261 §12.2.1.1: use the Contact URI from the 200 OK
        # NOT a reconstructed URI with numeric IP.
        request_uri = dialog.get("contact_uri")
        if not request_uri:
            # Fallback: construct from remote_sip_addr
            request_uri = (
                f"sip:{dialog.get('destination', '')}@"
                f"{remote_sip_addr[0]}:{remote_sip_addr[1]}"
            )

        # ── CSeq ────────────────────────────────────────────────────
        # Increment from last CSeq used in INVITE (was 1 ACK, so BYE=2)
        last_cseq = dialog.get("local_cseq", 1)
        bye_cseq = last_cseq + 1

        # ── To: header ──────────────────────────────────────────────
        # Per RFC 3261 §12.2.1.1: MUST use the To: from the 200 OK
        # verbatim (includes the to_tag Chime assigned).
        # NEVER reconstruct with numeric IP — Chime won't match the dialog.
        to_hdr = dialog.get("to_hdr")
        if not to_hdr:
            # Fallback only if 200 OK parsing failed
            dest = dialog.get("destination", "")
            vc_host = dialog.get(
                "remote_sip_host",
                "cafawc0zxzbcpxusmsonrd.voiceconnector.chime.aws",
            )
            to_tag = dialog.get("to_tag", "")
            to_hdr = f"<sip:{dest}@{vc_host}>"
            if to_tag:
                to_hdr += f";tag={to_tag}"

        # ── From: header ─────────────────────────────────────────────
        from_hdr = dialog.get("from_hdr", "")

        # ── Route headers ────────────────────────────────────────────
        # Per RFC 3261 §12.2.1.1: if 200 OK had Record-Route, BYE MUST
        # include Route headers (in reverse order of Record-Route).
        record_route = dialog.get("record_route")

        # ── Build BYE ────────────────────────────────────────────────
        bye_lines = [
            f"BYE {request_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.public_ip}:{self.port};branch=z9hG4bK-{uuid.uuid4().hex[:8]}",
            f"From: {from_hdr}",
            f"To: {to_hdr}",
            f"Call-ID: {call_id}",
            f"CSeq: {bye_cseq} BYE",
            f"Contact: <sip:cara@{self.public_ip}:{self.port}>",
            "Max-Forwards: 70",
            "User-Agent: CareHarmony-Voice-Gateway/2.0",
        ]

        # Add Route only if Record-Route was present in 200 OK
        if record_route:
            # RFC 3261: reverse Record-Route order for Route in requests
            routes = [r.strip() for r in record_route.split(",")]
            routes.reverse()
            bye_lines.append(f"Route: {', '.join(routes)}")

        bye_lines.append("Content-Length: 0")

        bye_msg = "\r\n".join(bye_lines) + "\r\n\r\n"
        self.transport.sendto(bye_msg.encode("utf-8"), remote_sip_addr)
        logger.info(
            f"[SIP_BYE] Sent BYE to {remote_sip_addr} "
            f"Request-URI={request_uri} "
            f"To={to_hdr} "
            f"CSeq={bye_cseq} "
            f"Route={'yes' if record_route else 'none'} "
            f"for Call-ID {call_id}"
        )

        del self.active_dialogs[call_id]
        if self.media_server:
            self.media_server.unregister_session(call_id)
        other_leg = self._bridged_leg(call_id, dialog)
        if other_leg:
            await self.send_bye(other_leg)
        if self.on_call_ended:
            await self.on_call_ended(call_id)

    async def _process_invite(
        self,
        msg: SIPMessage,
        call_id: str,
        to_tag: str,
        addr: Tuple[str, int],
        remote_sdp: SDPMessage,
        remote_rtp_addr: Tuple[str, int],
    ):
        """Asynchronously allocate dynamic RTP port, bind media socket, and answer 200 OK."""
        local_rtp_port = 10000
        if self.media_server:
            local_rtp_port, _ = await self.media_server.create_call_session(
                contact_id=call_id,
                remote_addr=remote_rtp_addr,
            )

        local_sdp_obj = SDPMessage(
            ip=self.public_ip,
            rtp_port=local_rtp_port,
            payload_type=0,
            sample_rate=8000,
            ptime=20,
        )
        local_sdp_str = local_sdp_obj.to_sdp(session_id=call_id[:10])

        trying_resp = msg.build_response(100, "Trying", local_contact=f"sip:{self.public_ip}:{self.port}")
        ringing_resp = msg.build_response(180, "Ringing", to_tag=to_tag, local_contact=f"sip:{self.public_ip}:{self.port}")
        ok_resp = msg.build_response(
            200,
            "OK",
            to_tag=to_tag,
            sdp_body=local_sdp_str,
            local_contact=f"sip:{self.public_ip}:{self.port}",
        )

        if self.transport:
            self.transport.sendto(trying_resp, addr)
            self.transport.sendto(ringing_resp, addr)
            self.transport.sendto(ok_resp, addr)
            logger.info(f"[SIP] Sent 200 OK with dynamic local RTP port {local_rtp_port} to {addr} for Call-ID {call_id}")

        if self.media_server:
            self.media_server.mark_answered(call_id)
        if self.on_call_started:
            await self.on_call_started(call_id, remote_sdp, remote_rtp_addr)


# ==============================================================================
# PIPECAT FRAME PROCESSOR: SIP RTP OUTPUT PROCESSOR
# ==============================================================================

class SIPRTPOutputProcessor(FrameProcessor):
    """Processes outbound synthetic audio, downsamples, encodes to G.711 u-law, and streams 20ms RTP packets."""

    def __init__(
        self,
        media_server: SIPRTPMediaServer,
        contact_id: str,
        sample_rate: int = 8000,
        frame_duration_ms: int = 20,
        on_queue_drained: Optional[Callable[[], Any]] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.media_server = media_server
        self.contact_id = contact_id
        self.sample_rate = sample_rate
        self.frame_duration_ms = frame_duration_ms
        self.samples_per_frame = int(sample_rate * frame_duration_ms / 1000)
        self.frame_size_bytes = self.samples_per_frame
        self._seq_num = 0
        self._timestamp = 0
        self._outbound_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._stream_task: Optional[asyncio.Task] = None
        self._interrupted = asyncio.Event()
        self._is_active = True
        self._had_audio_queued = False
        self.on_queue_drained = on_queue_drained

    @property
    def queue(self) -> asyncio.Queue[bytes]:
        return self._outbound_queue

    def purge_queue(self):
        """Immediately purge pending audio buffer for instant <50ms barge-in cutoff."""
        self._interrupted.set()
        self._had_audio_queued = False
        count = 0
        while not self._outbound_queue.empty():
            try:
                self._outbound_queue.get_nowait()
                count += 1
            except asyncio.QueueEmpty:
                break
        if count > 0:
            logger.info(f"Purged {count} outbound RTP frames on user interruption (<50ms cutoff) for Contact {self.contact_id}")

    def handle_interruption(self):
        self.purge_queue()

    async def wait_until_queue_empty(self):
        """Block until all queued frames are transmitted or purged."""
        while not self._outbound_queue.empty() and self._is_active:
            await asyncio.sleep(0.02)

    async def _rtp_streaming_loop(self):
        """Stream 20ms G.711 u-law RTP audio chunks with precise clock timing."""
        while self._is_active:
            try:
                frame_chunk = await self._outbound_queue.get()
                if not frame_chunk:
                    continue
                if self._interrupted.is_set():
                    self._interrupted.clear()
                    continue  # frame was queued before barge-in, drop it

                if len(frame_chunk) < self.frame_size_bytes:
                    frame_chunk = frame_chunk + b"\xFF" * (self.frame_size_bytes - len(frame_chunk))

                self.media_server.send_rtp_packet(
                    payload=frame_chunk,
                    contact_id=self.contact_id,
                    seq_num=self._seq_num,
                    timestamp=self._timestamp,
                )
                self._seq_num = (self._seq_num + 1) & 0xFFFF
                self._timestamp = (self._timestamp + self.samples_per_frame) & 0xFFFFFFFF

                # Check if queue has drained all pending frames
                if self._had_audio_queued and self._outbound_queue.empty():
                    self._had_audio_queued = False
                    if self.on_queue_drained:
                        try:
                            if asyncio.iscoroutinefunction(self.on_queue_drained):
                                await self.on_queue_drained()
                            else:
                                self.on_queue_drained()
                        except Exception as cb_err:
                            logger.debug(f"on_queue_drained callback note: {cb_err}")

                await asyncio.sleep(self.frame_duration_ms / 1000.0 * 0.95)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"RTP streaming loop iteration error: {e}")
                await asyncio.sleep(0.02)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        try:
            await super().process_frame(frame, direction)
        except Exception:
            pass

        if isinstance(frame, StartFrame):
            self._is_active = True
            if not self._stream_task or self._stream_task.done():
                self._stream_task = asyncio.create_task(self._rtp_streaming_loop())
            await self.push_frame(frame, direction)
        elif isinstance(frame, InterruptionFrame):
            self.handle_interruption()
            await self.push_frame(frame, direction)
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._is_active = False
            if self._stream_task and not self._stream_task.done():
                self._stream_task.cancel()
            self.purge_queue()
            await self.push_frame(frame, direction)
        elif isinstance(frame, AudioRawFrame):
            src_rate = frame.sample_rate or 24000
            pcm_8k = resample_pcm16_fast(frame.audio, src_rate=src_rate, dst_rate=self.sample_rate)
            ulaw_bytes = ulaw_encode(pcm_8k)
            # FIX 1: Slice into 160-byte (20ms at 8kHz PCMU) frames so purge_queue drains instantly on barge-in
            FRAME_SIZE = self.frame_size_bytes or 160
            self._had_audio_queued = True
            for i in range(0, len(ulaw_bytes), FRAME_SIZE):
                chunk = ulaw_bytes[i : i + FRAME_SIZE]
                await self._outbound_queue.put(chunk)
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)
