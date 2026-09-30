"""Amazon Connect KVS Bidirectional Audio Transport for Pipecat.

Handles L16 PCM audio streaming:
- Inbound: 8 kHz telephony L16 PCM -> Resample to 16 kHz -> Emit AudioRawFrame
- Outbound: 24 kHz / 16 kHz synthetic audio -> Downsample to 8 kHz L16 PCM -> Write to KVS
- Interruption: Instantly purges outbound audio queue upon InterruptionFrame (VAD cutoff < 50ms)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

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

logger = logging.getLogger("kvs_transport")


def resample_pcm16(audio_bytes: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit mono PCM audio bytes between arbitrary sample rates."""
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
    resampled_samples = np.interp(dst_indices, src_indices, samples).astype(np.int16)

    return resampled_samples.tobytes()


class MatroskaKVSStreamDemuxer:
    """Demuxes Amazon Connect Matroska (MKV) KVS stream and extracts Track 1 (AUDIO_FROM_CUSTOMER) L16 PCM."""

    def __init__(self, target_track: int = 1):
        self.target_track = target_track
        self._buffer = bytearray()

    @staticmethod
    def parse_ebml_vint(buffer: bytes | bytearray, offset: int) -> tuple[Optional[int], int]:
        if offset >= len(buffer):
            return None, 0
        first_byte = buffer[offset]
        if first_byte == 0:
            return None, 0
        mask = 0x80
        length = 1
        while mask > 0 and (first_byte & mask) == 0:
            length += 1
            mask >>= 1
        if offset + length > len(buffer):
            return None, 0
        value = first_byte & (mask - 1)
        for i in range(1, length):
            value = (value << 8) | buffer[offset + i]
        return value, length

    def feed_bytes(self, chunk: bytes) -> list[bytes]:
        """Feed raw EBML/MKV bytes and extract pure PCM frames for target track (AUDIO_FROM_CUSTOMER)."""
        self._buffer.extend(chunk)
        extracted_pcm: list[bytes] = []
        idx = 0
        buf_len = len(self._buffer)

        while idx < buf_len - 4:
            if self._buffer[idx] == 0xA3:
                size, vint_len = self.parse_ebml_vint(self._buffer, idx + 1)
                if size is not None and size > 4:
                    block_start = idx + 1 + vint_len
                    block_end = block_start + size
                    if block_end <= buf_len:
                        track_num, track_vint_len = self.parse_ebml_vint(self._buffer, block_start)
                        if track_num is not None:
                            header_size = track_vint_len + 3  # Track(VINT) + Timecode(2) + Flags(1)
                            if header_size < size:
                                frame_payload = bytes(self._buffer[block_start + header_size : block_end])
                                if track_num == self.target_track:
                                    extracted_pcm.append(frame_payload)
                        idx = block_end
                        continue
                    else:
                        break
            idx += 1

        if idx > 0:
            del self._buffer[:idx]

        if len(self._buffer) > 1024 * 1024:
            self._buffer = self._buffer[-65536:]

        return extracted_pcm


class KVSInputProcessor(FrameProcessor):
    """Pulls 8 kHz telephony L16 PCM from Amazon Connect KVS and emits 16 kHz AudioRawFrames."""

    def __init__(self, target_sample_rate: int = 16000, **kwargs):
        super().__init__(**kwargs)
        self.source_sample_rate = 8000
        self.target_sample_rate = target_sample_rate
        self._is_active = True
        self.demuxer = MatroskaKVSStreamDemuxer(target_track=1)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        try:
            await super().process_frame(frame, direction)
        except Exception:
            pass

        if isinstance(frame, StartFrame):
            self._is_active = True
            await self.push_frame(frame, direction)
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._is_active = False
            await self.push_frame(frame, direction)
        elif isinstance(frame, AudioRawFrame) and self._is_active:
            # Check if input is raw MKV/EBML container bytes (starts with 0x1A45DFA3 or contains 0xA3 blocks)
            raw_audio = frame.audio
            if len(raw_audio) > 4 and (raw_audio[:4] == b"\x1a\x45\xdf\xa3" or b"\xa3" in raw_audio):
                pcm_chunks = self.demuxer.feed_bytes(raw_audio)
                for pcm in pcm_chunks:
                    resampled = resample_pcm16(
                        pcm,
                        src_rate=self.source_sample_rate,
                        dst_rate=self.target_sample_rate,
                    )
                    out_frame = AudioRawFrame(
                        audio=resampled,
                        sample_rate=self.target_sample_rate,
                        num_channels=1,
                    )
                    await self.push_frame(out_frame, direction)
            else:
                # Direct PCM frame
                resampled = resample_pcm16(
                    raw_audio,
                    src_rate=frame.sample_rate or self.source_sample_rate,
                    dst_rate=self.target_sample_rate,
                )
                out_frame = AudioRawFrame(
                    audio=resampled,
                    sample_rate=self.target_sample_rate,
                    num_channels=1,
                )
                await self.push_frame(out_frame, direction)
        else:
            await self.push_frame(frame, direction)


class KVSOutputProcessor(FrameProcessor):
    """Receives Nova 2 Sonic synthesized audio, downsamples to 8 kHz PCM, and outputs to active telephony channel."""

    def __init__(self, target_sample_rate: int = 8000, contact_id: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.target_sample_rate = target_sample_rate
        self.contact_id = contact_id or "UNKNOWN"
        self._outbound_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._is_interrupted = False

    @property
    def queue(self) -> asyncio.Queue[bytes]:
        return self._outbound_queue

    def purge_queue(self):
        """Immediately purge pending audio buffer for instant barge-in / interruption cutoff."""
        count = 0
        while not self._outbound_queue.empty():
            try:
                self._outbound_queue.get_nowait()
                count += 1
            except asyncio.QueueEmpty:
                break
        if count > 0:
            logger.info(f"Purged {count} outbound audio frames on user interruption (cutoff < 50ms) for Contact {self.contact_id}")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        try:
            await super().process_frame(frame, direction)
        except Exception:
            pass

        if isinstance(frame, InterruptionFrame):
            self.purge_queue()
            self._is_interrupted = True
            await self.push_frame(frame, direction)
        elif isinstance(frame, AudioRawFrame):
            self._is_interrupted = False
            src_rate = frame.sample_rate or 24000
            downsampled = resample_pcm16(frame.audio, src_rate=src_rate, dst_rate=self.target_sample_rate)
            await self._outbound_queue.put(downsampled)
            import datetime
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            logger.info(
                f"Transmitted {len(downsampled)} bytes ({self.target_sample_rate} Hz L16 PCM) "
                f"to active telephony session return channel for Contact {self.contact_id} at {now_iso}"
            )
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    async def get_next_audio_chunk(self) -> Optional[bytes]:
        """Fetch next 8 kHz PCM audio chunk to send back to telephony channel."""
        try:
            return await asyncio.wait_for(self._outbound_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None
