"""
Call recording for SIP calls handled by the gateway.

Each answered patient call is recorded as one stereo 8 kHz WAV and uploaded
to S3 when the call ends:
  - left channel:  the patient (RTP received from Chime)
  - right channel: what the patient heard (RTP sent to Chime: the disclosure,
    Cara after any barge-in purge, then the human agent once bridged)

Frames are taken from the RTP socket, so the recording covers the whole call,
including the agent conversation after a transfer, when the patient's audio
no longer goes through Nova Sonic.

Hot path cost is one bytearray append per 20 ms frame. Frames are written to
local files about once a second on a worker thread, and the WAV build and S3
upload run on a small thread pool after hangup, never on the audio path.

Outbound RTP only flows while someone is talking, so each frame is placed by
its time since pickup and gaps are filled with silence; that keeps the two
channels aligned.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import io
import logging
import os
import time
import wave
from typing import Any, Callable, Dict, Optional, Tuple

import boto3
from botocore.exceptions import ProfileNotFound

logger = logging.getLogger("careharmony.gateway.call_recorder")

RECORDING_ENABLED = os.getenv("CALL_RECORDING_ENABLED", "false").lower() == "true"
RECORDING_BUCKET = os.getenv("CALL_RECORDING_BUCKET", "")
RECORDING_DIR = os.getenv("CALL_RECORDING_DIR", "/tmp/recordings")
RECORDING_PREFIX = "recordings"

SAMPLE_RATE = 8000
ULAW_SILENCE = 0xFF
FLUSH_INTERVAL_SECONDS = 1.0
# Arrival jitter below this is not treated as a gap (60 ms at 8 kHz)
MAX_JITTER_SAMPLES = 480
UPLOAD_ATTEMPTS = 3

# Uploads from many calls ending at once share these few threads, so they
# can't take CPU from calls still in progress.
_UPLOAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="rec-upload")
_s3_client: Any = None


def recording_enabled() -> bool:
    return RECORDING_ENABLED and bool(RECORDING_BUCKET)


def _get_s3_client() -> Any:
    global _s3_client
    if _s3_client is None:
        region = os.getenv("AWS_REGION", "us-east-1")
        try:
            session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "careharmony"), region_name=region)
            session.get_credentials()
        except (ProfileNotFound, Exception):
            session = boto3.Session(region_name=region)
        _s3_client = session.client("s3")
    return _s3_client


def recording_key(call_id: str, started_at: dt.datetime) -> str:
    """S3 key for a call: date and call ID only, no patient details."""
    return f"{RECORDING_PREFIX}/{started_at:%Y/%m/%d}/{call_id}.wav"


def _append(path: str, data: bytes):
    with open(path, "ab") as f:
        f.write(data)


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


def build_stereo_wav(inbound_ulaw: bytes, outbound_ulaw: bytes) -> Tuple[bytes, float]:
    """Interleave two u-law channels into a 16-bit stereo WAV. Returns (wav, seconds)."""
    import numpy as np

    from gateway.sip_transport import ulaw_decode

    n = max(len(inbound_ulaw), len(outbound_ulaw))
    pad = bytes([ULAW_SILENCE])
    left = np.frombuffer(ulaw_decode(inbound_ulaw + pad * (n - len(inbound_ulaw))), dtype=np.int16)
    right = np.frombuffer(ulaw_decode(outbound_ulaw + pad * (n - len(outbound_ulaw))), dtype=np.int16)
    stereo = np.empty(2 * n, dtype=np.int16)
    stereo[0::2] = left
    stereo[1::2] = right

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(stereo.tobytes())
    return buf.getvalue(), n / SAMPLE_RATE


def upload_recording(
    bucket: str,
    call_id: str,
    inbound_path: str,
    outbound_path: str,
    started_at: dt.datetime,
    metadata: Dict[str, Any],
    s3_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Tuple[Optional[str], float]:
    """Build the WAV from the channel files and put it in S3 (runs on a thread).

    Local files are deleted after a successful upload and kept otherwise.
    Returns (s3_uri or None, duration_seconds).
    """
    wav, duration = build_stereo_wav(_read(inbound_path), _read(outbound_path))
    key = recording_key(call_id, started_at)
    meta = {k: str(v) for k, v in metadata.items() if v is not None}
    meta["duration_sec"] = f"{duration:.1f}"
    s3 = s3_client or _get_s3_client()

    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=wav,
                ContentType="audio/wav",
                ServerSideEncryption="AES256",
                Metadata=meta,
            )
            for path in (inbound_path, outbound_path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            return f"s3://{bucket}/{key}", duration
        except Exception as e:
            logger.warning(
                f"[RECORDING_ERROR] Upload attempt {attempt}/{UPLOAD_ATTEMPTS} failed for {call_id}: {e}"
            )
            if attempt < UPLOAD_ATTEMPTS:
                sleep(2 ** (attempt - 1))
    logger.error(
        f"[RECORDING_ERROR] Giving up on {call_id}; audio kept at {inbound_path} / {outbound_path}"
    )
    return None, duration


class _Channel:
    """One side of the call: frames waiting to be written, and samples so far."""

    def __init__(self, path: str):
        self.path = path
        self.pending = bytearray()
        self.samples = 0

    def add(self, ulaw: bytes, target_sample: int):
        gap = target_sample - self.samples
        if gap > MAX_JITTER_SAMPLES:
            self.pending += bytes([ULAW_SILENCE]) * gap
            self.samples += gap
        self.pending += ulaw
        self.samples += len(ulaw)

    def take(self) -> bytes:
        data = bytes(self.pending)
        self.pending.clear()
        return data


class CallRecorder:
    """Records one call's two RTP directions and uploads them as a stereo WAV."""

    def __init__(
        self,
        call_id: str,
        bucket: str,
        directory: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        s3_client: Any = None,
    ):
        self.call_id = call_id
        self.bucket = bucket
        self.metadata: Dict[str, Any] = {"call_id": call_id}
        self.started_at = dt.datetime.now(dt.timezone.utc)
        self._clock = clock
        self._t0 = clock()
        self._s3_client = s3_client
        directory = directory or RECORDING_DIR
        os.makedirs(directory, exist_ok=True)
        base = os.path.join(directory, call_id)
        self.inbound = _Channel(base + ".in.ulaw")
        self.outbound = _Channel(base + ".out.ulaw")
        self._closed = False
        self._stop = asyncio.Event()
        self._flush_task: Optional[asyncio.Task] = None

    def start(self):
        """Begin flushing frames to disk about once a second."""
        self._flush_task = asyncio.create_task(self._flush_loop())

    def _now_sample(self) -> int:
        return int((self._clock() - self._t0) * SAMPLE_RATE)

    def add_inbound(self, ulaw: bytes):
        if not self._closed:
            self.inbound.add(ulaw, self._now_sample())

    def add_outbound(self, ulaw: bytes):
        if not self._closed:
            self.outbound.add(ulaw, self._now_sample())

    async def _flush(self):
        for channel in (self.inbound, self.outbound):
            data = channel.take()
            if data:
                await asyncio.to_thread(_append, channel.path, data)

    async def _flush_loop(self):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), FLUSH_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            try:
                await self._flush()
            except Exception as e:
                logger.warning(f"[RECORDING_ERROR] Could not write audio for {self.call_id}: {e}")

    async def finalize(self) -> Tuple[Optional[str], float]:
        """Stop recording, write what's left and upload. Returns (s3_uri, seconds)."""
        if self._closed:
            return None, 0.0
        self._closed = True
        self._stop.set()
        if self._flush_task:
            await self._flush_task
        await self._flush()  # frames added since the loop's last pass
        self.metadata.setdefault("start_utc", self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.metadata["end_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _UPLOAD_POOL,
            lambda: upload_recording(
                self.bucket,
                self.call_id,
                self.inbound.path,
                self.outbound.path,
                self.started_at,
                dict(self.metadata),
                s3_client=self._s3_client,
            ),
        )
