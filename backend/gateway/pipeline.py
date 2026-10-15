"""Pipecat Voice Pipeline Assembly and Full-Duplex SIP/RTP Gateway for CareHarmony Outreach IVR.

Assembles the end-to-end streaming pipeline:
Telephony Inbound (SIP/RTP or Connect KVS 8kHz L16 PCM) -> KVSInputProcessor (16kHz) ->
Bedrock Nova 2 Sonic (<550ms turnaround, <50ms barge-in cutoff) ->
OutreachToolExecutor -> SIPRTPOutputProcessor / KVSOutputProcessor -> Telephony Return (8kHz L16 PCM)

Provides an active SIP/RTP UDP Media Server, KVS Stream Consumer Worker, and FastAPI lifecycle server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Set, Tuple

import boto3
from botocore.exceptions import ClientError, ProfileNotFound
from fastapi import FastAPI, Response
import uvicorn

from pipecat.frames.frames import AudioRawFrame, StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask

from domain_rules import CALL_CONNECTED_CUE, SCRIPT_DEFINITIONS, CallMode, PatientContext, resolve_voice
from gateway.caller_speech import CallerSpeechDetector, load_vad_model
from gateway.kvs_transport import KVSInputProcessor, KVSOutputProcessor
from gateway.nova_sonic_service import NovaSonicLLMService
from gateway.sip_transport import (
    EARLY_AUDIO_MAX_FRAMES,
    SDPMessage,
    SIPRTPMediaServer,
    SIPRTPOutputProcessor,
    SIPSignalingServer,
)
from gateway.tools import OutreachToolExecutor
from storage.outreach_records import (
    RETRYABLE_CALL_OUTCOMES,
    OutreachRecordService,
    format_call_summary,
)

logger = logging.getLogger("gateway_pipeline")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

DEFAULT_STREAM_NAME = os.getenv("KVS_STREAM_NAME", "careharmony-outreach-media-stream")
DEFAULT_REGION = os.getenv("AWS_REGION", "us-east-1")
DEFAULT_PROFILE = os.getenv("AWS_PROFILE", "careharmony")
DEFAULT_RTP_PORT = int(os.getenv("RTP_PORT", "10000"))
DEFAULT_SIP_PORT = int(os.getenv("SIP_PORT", "5060"))
DEFAULT_CONNECT_INSTANCE = os.getenv("CONNECT_INSTANCE_ID", "8e7c7423-5e98-44ab-a748-b3ca0bd7a896")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Cara opens the call herself when the caller stays quiet after the disclosure.
# Long speech is never treated as voicemail here; only the beep or voicemail
# phrases in the transcript end a call (see NovaSonicSession).
OPENING_ENABLED = os.getenv("CARA_OPENING_ENABLED", "true").lower() == "true"
# Off: Cara's opening line is played through Polly. On: Nova is sent a text
# cue instead (a mid-stream text block has hung Nova before, so test first).
OPENING_CUE_ENABLED = os.getenv("CARA_OPENING_CUE_ENABLED", "false").lower() == "true"
OPENING_SILENCE_S = _env_float("CARA_OPENING_SILENCE_S", 1.5)
OPENING_AFTER_SPEECH_S = _env_float("CARA_OPENING_AFTER_SPEECH_S", 2.5)
OPENING_CAP_S = _env_float("CARA_OPENING_CAP_S", 3.0)
OPENING_CAP_PAUSE_S = _env_float("CARA_OPENING_CAP_PAUSE_S", 0.3)
OPENING_POLL_S = 0.05
# Ringback tone for the patient while a transfer dials the human agent
TRANSFER_RINGBACK_ENABLED = os.getenv("CARA_TRANSFER_RINGBACK_ENABLED", "true").lower() == "true"
# Silence fed to Nova while the phone rings (100 ms of 8 kHz PCM16)
RINGING_SILENCE_FRAME = b"\x00" * 1600

_polly_client = None


def _get_polly_client():
    global _polly_client
    if _polly_client is None:
        profile = os.getenv("AWS_PROFILE", DEFAULT_PROFILE)
        region = os.getenv("AWS_REGION", DEFAULT_REGION)
        try:
            session = boto3.Session(profile_name=profile, region_name=region)
            session.get_credentials()
            _polly_client = session.client("polly")
        except Exception:
            _polly_client = boto3.client("polly", region_name=region)
    return _polly_client


async def synthesize_forced_closing(text: str, voice: str = "") -> bytes:
    """
    Synthesizes a short closing line via Polly as 8kHz PCMU,
    used ONLY for forced call-ending scenarios where Nova Sonic's
    bidirectional stream cannot be relied upon to respond
    (e.g. after an injected mid-stream text block would deadlock
    it). Returns raw PCMU bytes ready for direct RTP playback.
    """
    loop = asyncio.get_running_loop()
    polly = _get_polly_client()
    response = await loop.run_in_executor(
        None,
        lambda: polly.synthesize_speech(
            Text=text,
            OutputFormat="pcm",
            SampleRate="8000",
            VoiceId=resolve_voice(voice)[1],
            Engine="neural",
        ),
    )
    pcm_16bit = response["AudioStream"].read()
    try:
        from gateway.sip_transport import ulaw_encode
    except ImportError:
        from backend.gateway.sip_transport import ulaw_encode
    return ulaw_encode(pcm_16bit)


def resolve_public_ip() -> str:
    """Resolve public IP via env, checkip.amazonaws.com, or fallback."""
    env_ip = os.getenv("PUBLIC_IP")
    if env_ip and env_ip.lower() not in ("auto", ""):
        return env_ip
    try:
        import urllib.request
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=3) as resp:
            ip = resp.read().decode("utf-8").strip()
            if ip:
                return ip
    except Exception:
        pass
    return "100.49.33.242"


DEFAULT_PUBLIC_IP = resolve_public_ip()
DEFAULT_ECHO_MODE = os.getenv("RTP_ECHO_MODE", "false").lower() == "true"


def get_aws_session(profile: Optional[str] = None, region: Optional[str] = None) -> boto3.Session:
    """Create a boto3 Session with graceful fallback for credentials."""
    prof = profile or DEFAULT_PROFILE
    reg = region or DEFAULT_REGION
    try:
        session = boto3.Session(profile_name=prof, region_name=reg)
        session.get_credentials()
        return session
    except (ProfileNotFound, Exception):
        return boto3.Session(region_name=reg)


class AMDHandler:
    """
    Answering Machine Detection handler.

    TRAFFIC_TYPE=CAMPAIGNS (future):
        Amazon Connect / Chime will pass AMD result in the SIP
        INVITE as a custom header X-AMD-Result: HUMAN|MACHINE|UNKNOWN
        or via a pre-call webhook payload. Parse and route accordingly.

    TRAFFIC_TYPE=GENERAL (current):
        AMD is not available. Assume HUMAN on every call.
        Voicemail detection falls back to Nova Sonic detecting
        silence + one-way audio pattern (no USER transcript within
        first 8 seconds after greeting = likely voicemail → invoke
        terminate_call(disposition="VOICEMAIL")).
    """

    TRAFFIC_TYPE = os.getenv("TRAFFIC_TYPE", "GENERAL")

    @classmethod
    def get_amd_result(cls, sip_invite_headers: Optional[dict] = None) -> str:
        if cls.TRAFFIC_TYPE == "CAMPAIGNS":
            # Parse X-AMD-Result from SIP INVITE custom header
            # Set by Amazon Connect outbound campaign dialer
            headers = sip_invite_headers or {}
            for k, v in headers.items():
                if k.lower() == "x-amd-result":
                    return v.upper()
            return "UNKNOWN"
        else:
            # GENERAL traffic: AMD not available, assume HUMAN
            # Nova Sonic handles voicemail detection via silence pattern
            return "HUMAN"

    @classmethod
    def should_leave_voicemail(cls, amd_result: str) -> bool:
        return amd_result in ("MACHINE", "VOICEMAIL")

    @classmethod
    def should_abandon(cls, amd_result: str) -> bool:
        # Policy: abandon on UNKNOWN in campaign mode to avoid
        # wasting minutes on undetected machines
        return cls.TRAFFIC_TYPE == "CAMPAIGNS" and amd_result == "UNKNOWN"


class CaraVoicePipeline:
    """Assembles and runs the real-time Pipecat pipeline for a single outreach call session."""

    def __init__(
        self,
        patient_context: PatientContext,
        call_id: str,
        campaign_id: str = "CAMPAIGN-001",
        api_base_url: Optional[str] = None,
        connect_instance_id: Optional[str] = None,
        media_server: Optional[SIPRTPMediaServer] = None,
        sip_signaling_server: Optional[SIPSignalingServer] = None,
        remote_rtp_addr: Optional[Tuple[str, int]] = None,
        invite_headers: Optional[Dict[str, str]] = None,
        answered: bool = True,
    ):
        self.call_id = call_id
        self.campaign_id = campaign_id
        self.media_server = media_server
        self._is_running = False
        self._stopped = False
        # False while the phone is still ringing (pipeline built before pickup)
        self._answered = answered
        self._answered_event = asyncio.Event()
        if answered:
            self._answered_event.set()
        # Caller frames that arrive before run() starts delivering them
        self._early_audio: Deque[bytes] = deque(maxlen=EARLY_AUDIO_MAX_FRAMES)
        self.caller_speech = CallerSpeechDetector()
        self._listening_for_opening = True
        self._opening_audio_task: Optional[asyncio.Task] = None
        self.sip_signaling_server = sip_signaling_server or globals().get("sip_signaling_server")
        self.invite_headers = invite_headers or {}

        # AMD Resolution
        self.amd_result = AMDHandler.get_amd_result(
            sip_invite_headers=self.invite_headers
        )
        logger.info(f"[AMD] Traffic Type: {AMDHandler.TRAFFIC_TYPE}, AMD Result: {self.amd_result} for {call_id}")

        # Pass to PatientContext so Nova Sonic system prompt sees it
        if patient_context:
            self.patient = PatientContext(
                patient_id=patient_context.patient_id,
                first_name=patient_context.first_name,
                last_name=patient_context.last_name,
                practice_name=patient_context.practice_name,
                phone_number=patient_context.phone_number,
                callback_number=patient_context.callback_number,
                provider_name=patient_context.provider_name,
                direct_agent=patient_context.direct_agent,
                call_mode=patient_context.call_mode,
                reschedule_count=patient_context.reschedule_count,
                human_agent_phone=patient_context.human_agent_phone,
                timezone=patient_context.timezone,
                voice=patient_context.voice,
                amd_result=self.amd_result,
            )
        else:
            self.patient = patient_context

        if AMDHandler.should_abandon(self.amd_result):
            logger.info(f"[AMD] Abandoning call {call_id} — AMD result UNKNOWN")
            self._abandon = True
        else:
            self._abandon = False

        if AMDHandler.should_leave_voicemail(self.amd_result):
            logger.info(f"[AMD] Voicemail detected for {call_id} — queuing voicemail path")

        async def _on_cara_hangup():
            """
            Called by NovaSonicSession after farewell audio drains.
            Sends SIP BYE to terminate the call from our side.
            """
            logger.info(
                f"[PIPELINE_HANGUP] on_hangup fired for {self.call_id} — "
                f"sending SIP BYE"
            )
            try:
                sig_server = self.sip_signaling_server or globals().get("sip_signaling_server")
                if sig_server:
                    await sig_server.send_bye(self.call_id)
            except Exception:
                logger.exception(
                    f"[PIPELINE_HANGUP_ERROR] send_bye failed for {self.call_id}"
                )

        async def _on_cara_transfer(agent_phone: str) -> bool:
            """Dial the human agent and bridge them to this call."""
            sig_server = self.sip_signaling_server or globals().get("sip_signaling_server")
            if not sig_server:
                logger.warning(f"[PIPELINE_TRANSFER] No SIP server for {self.call_id}")
                return False
            output = self.sip_output_processor
            if output and TRANSFER_RINGBACK_ENABLED:
                output.start_ringback()
            try:
                return await sig_server.dial_agent_and_bridge(self.call_id, agent_phone)
            finally:
                if output:
                    await output.stop_ringback()

        # 1. Processors
        self.storage = OutreachRecordService()
        self.tool_executor = OutreachToolExecutor(
            api_base_url=api_base_url,
            connect_instance_id=connect_instance_id or DEFAULT_CONNECT_INSTANCE,
            patient_context=self.patient,
            contact_id=self.call_id,
            storage_service=self.storage,
            is_sip_call=bool(media_server),
        )

        async def _on_forced_audio(pcmu_bytes: bytes):
            """
            Plays synthesized audio directly through the RTP output,
            bypassing Nova Sonic's normal audioOutput->queue path. Used
            for forced closings where Nova Sonic's stream cannot be
            relied upon (identity-unconfirmed, voicemail).
            """
            if self.sip_output_processor and getattr(self.sip_output_processor, "_outbound_queue", None):
                FRAME_SIZE = 160  # 20ms at 8kHz PCMU
                for i in range(0, len(pcmu_bytes), FRAME_SIZE):
                    await self.sip_output_processor._outbound_queue.put(
                        pcmu_bytes[i:i + FRAME_SIZE]
                    )
                logger.info(
                    f"[FORCED_AUDIO_QUEUED] {len(pcmu_bytes)} bytes queued "
                    f"for {self.call_id}"
                )
            else:
                logger.warning(f"[FORCED_AUDIO_DROPPED] No sip_output_processor queue for {self.call_id}")

        self._on_forced_audio = _on_forced_audio

        def _purge_output():
            """Drop audio queued for the caller (voicemail message start / cancel)."""
            output = getattr(self, "sip_output_processor", None)
            if output:
                output.purge_queue()
                output._interrupted.clear()  # nothing to drop: keep the next frame

        self.input_processor = KVSInputProcessor(target_sample_rate=16000)
        self.nova_sonic = NovaSonicLLMService(
            patient_context=self.patient,
            voice=resolve_voice(self.patient.voice if self.patient else "")[0],
            contact_id=self.call_id,
            on_hangup=_on_cara_hangup,
            on_forced_audio=_on_forced_audio,
            on_transfer=_on_cara_transfer,
            tool_executor=self.tool_executor,
            on_purge_output=_purge_output,
        )
        self.nova_sonic_session = getattr(self.nova_sonic, "session", None)
        if self.nova_sonic_session:
            self.nova_sonic_session.on_forced_audio = _on_forced_audio
            self.nova_sonic_session.on_transfer = _on_cara_transfer
            self.nova_sonic_session.tool_executor = self.tool_executor
        self.output_processor = KVSOutputProcessor(
            target_sample_rate=8000,
            contact_id=self.call_id,
        )

        if self.media_server:
            self.sip_output_processor = SIPRTPOutputProcessor(
                media_server=self.media_server,
                contact_id=self.call_id,
                sample_rate=8000,
                on_queue_drained=self.nova_sonic.on_queue_drained,
            )
            # SIP pipeline only needs NovaSonic AI processor and SIP RTP output
            pipeline_processors = [
                self.nova_sonic,
                self.sip_output_processor,
            ]
            self.media_server.register_session(self.call_id, self, remote_rtp_addr)
            self.media_server.set_recording_metadata(
                self.call_id,
                patient_id=self.patient.patient_id if self.patient else None,
                campaign_id=self.campaign_id,
            )
        else:
            self.sip_output_processor = None
            pipeline_processors = [
                self.input_processor,
                self.nova_sonic,
                self.output_processor,
            ]

        # 2. Pipeline Assembly
        self.pipeline = Pipeline(pipeline_processors)
        try:
            self.task = PipelineTask(
                self.pipeline,
                idle_timeout_secs=None,
                cancel_on_idle_timeout=False,
                cancel_runner_on_idle_timeout=False,
            )
        except TypeError:
            try:
                self.task = PipelineTask(self.pipeline, idle_timeout=None)
            except TypeError:
                self.task = PipelineTask(self.pipeline)
        self.runner = PipelineRunner()

    def mark_answered(self):
        """The patient's phone was answered: start the disclosure and caller audio."""
        self._answered = True
        self._answered_event.set()

    async def _feed_silence_until_answered(self):
        """Keep the Nova stream fed while the phone rings. The line carries
        ringback then, never the patient, so real audio is not forwarded."""
        while not self._answered_event.is_set():
            await self.task.queue_frame(
                AudioRawFrame(audio=RINGING_SILENCE_FRAME, sample_rate=8000, num_channels=1)
            )
            try:
                await asyncio.wait_for(self._answered_event.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

    async def _start_caller_audio(self):
        """Deliver caller audio held since pickup, oldest first, then go live."""
        held = len(self._early_audio)
        while self._early_audio:
            await self._queue_caller_frame(self._early_audio.popleft())
        self._is_running = True
        if held:
            logger.info(f"[EARLY_AUDIO] Sent {held} held caller frames ({held * 20} ms) to Nova for {self.call_id}")

    async def _queue_caller_frame(self, audio_chunk: bytes):
        frame = AudioRawFrame(audio=audio_chunk, sample_rate=8000, num_channels=1)
        try:
            await self.task.queue_frame(frame)
        except Exception as ex:
            logger.debug(f"Audio push frame exception: {ex}")

    def _uses_forced_opening(self) -> bool:
        """Polly plays the opening line when the Nova text cue is off. A direct
        handoff call keeps today's behaviour: its line must trigger the transfer tool."""
        return (
            OPENING_ENABLED
            and not OPENING_CUE_ENABLED
            and self.sip_output_processor is not None
            and self.patient is not None
            and self.patient.call_mode != CallMode.MODE_1_DIRECT_HANDOFF
        )

    def _opening_text(self) -> str:
        return self.patient.render_template(SCRIPT_DEFINITIONS.opening_greeting)

    async def _synthesize_opening(self) -> Optional[bytes]:
        try:
            return await synthesize_forced_closing(self._opening_text(), self.patient.voice)
        except Exception:
            logger.exception(f"[OPENING] Could not synthesize the opening line for {self.call_id}")
            return None

    async def _open_if_caller_silent(self, disclosure_end: float):
        """After the disclosure, open the call if the caller doesn't speak.

        Caller speech only delays the decision (Nova answers it as today); it
        never ends the call. The cap makes sure the line is never left silent.
        """
        speech = self.caller_speech
        try:
            while not self._stopped:
                await asyncio.sleep(OPENING_POLL_S)
                session = self.nova_sonic.session
                if not session or not session.awaiting_opening():
                    return
                if speech.speaking:
                    continue
                now = time.monotonic()
                quiet = speech.quiet_for(now, since=disclosure_end)
                reason = None
                if speech.last_speech_at is None and quiet >= OPENING_SILENCE_S:
                    reason = f"no caller speech for {quiet:.1f}s after the disclosure"
                elif speech.last_speech_at is not None and quiet >= OPENING_AFTER_SPEECH_S:
                    reason = f"no reply {quiet:.1f}s after the caller last spoke"
                elif now - disclosure_end >= OPENING_CAP_S and quiet >= OPENING_CAP_PAUSE_S:
                    reason = f"{OPENING_CAP_S:.1f}s cap after the disclosure"
                if reason:
                    await self._start_opening(session, reason)
                    return
        finally:
            self._listening_for_opening = False

    async def _start_opening(self, session, reason: str):
        if self.patient is not None and self.patient.call_mode == CallMode.MODE_1_DIRECT_HANDOFF:
            # Nova answered the cue with the normal opening instead of the
            # direct line (call 60364398); the direct line follows the hello
            logger.info(f"[OPENING] {reason} for {self.call_id} — direct handoff call, waiting for the caller")
            return
        if OPENING_CUE_ENABLED:
            logger.info(f"[OPENING] {reason} for {self.call_id} — cueing Nova Sonic to open the call")
            await session.send_opening_cue(CALL_CONNECTED_CUE)
            return
        if not self._uses_forced_opening():
            return
        try:
            audio = await self._opening_audio_task if self._opening_audio_task else None
        except asyncio.CancelledError:
            return  # call ended while the line was being synthesized
        if not audio or not session.awaiting_opening():
            return
        logger.info(f"[OPENING] {reason} for {self.call_id} — playing Cara's opening line")
        session.note_forced_opening(self._opening_text(), len(audio) / 8000.0)
        await self._on_forced_audio(audio)

    async def _play_opening_disclosure(self):
        """
        Plays the mandatory call-recording disclosure before Nova
        Sonic's session opens. Guarantees this is always the first
        thing the caller hears, with zero dependency on model
        latency or turn logic.
        """
        disclosure_path = os.path.join(
            os.path.dirname(__file__), "..", "assets",
            "call_recording_disclosure_pcmu_8k.wav"
        )
        if not os.path.exists(disclosure_path):
            logger.warning(
                f"[DISCLOSURE] Call recording disclosure file not found at {disclosure_path}"
            )
            return

        with open(disclosure_path, "rb") as f:
            pcmu_bytes = f.read()[44:]  # skip WAV header

        if hasattr(self, "sip_output_processor") and self.sip_output_processor:
            FRAME_SIZE = 160  # 20ms at 8kHz PCMU
            for i in range(0, len(pcmu_bytes), FRAME_SIZE):
                await self.sip_output_processor._outbound_queue.put(
                    pcmu_bytes[i:i + FRAME_SIZE]
                )
            while not self.sip_output_processor._outbound_queue.empty():
                await asyncio.sleep(0.02)

            logger.info(
                f"[DISCLOSURE] Call recording disclosure played for "
                f"{self.call_id}"
            )

    async def run(self):
        """Execute the pipeline loop."""
        logger.info(f"Starting Cara Voice Pipeline for call {self.call_id} (Patient {self.patient.patient_id})")

        # Start pipeline runner in a background task; this opens Nova Sonic
        runner_task = asyncio.create_task(self.runner.run(self.task))
        if self._uses_forced_opening():
            self._opening_audio_task = asyncio.create_task(self._synthesize_opening())

        if not self._answered_event.is_set():
            logger.info(f"[PREPARE] Pipeline for {self.call_id} is up before pickup; waiting for the answer")
            await self._feed_silence_until_answered()
            if self._stopped:
                await self._finish(runner_task)
                return
            logger.info(f"[PREPARE] {self.call_id} answered; pipeline already running")

        await self._start_caller_audio()

        # Play mandatory opening disclosure; its echo must not count as the caller
        self.caller_speech.set_muted(True)
        await self._play_opening_disclosure()
        self.caller_speech.set_muted(False)
        if OPENING_ENABLED and self.sip_output_processor is not None and not self._stopped:
            asyncio.create_task(self._open_if_caller_silent(time.monotonic()))
        else:
            self._listening_for_opening = False

        await self._finish(runner_task)

    async def _finish(self, runner_task: asyncio.Task):
        try:
            await runner_task
        except Exception as e:
            logger.info(f"Pipeline runner task completed for {self.call_id}: {e}")

    async def push_audio(self, audio_chunk: bytes):
        """Feed inbound telephony audio from SIP/RTP or KVS into the Pipecat pipeline."""
        if not audio_chunk or self._stopped:
            return
        if self._listening_for_opening:
            self.caller_speech.feed(audio_chunk, time.monotonic())
        if not self._is_running:
            self._early_audio.append(audio_chunk)
            return
        logger.info(f"[AUDIO_IN] Ingested audio frame ({len(audio_chunk)} bytes) for Contact {self.call_id}")
        await self._queue_caller_frame(audio_chunk)

    async def stop(self):
        """Cancel and terminate the pipeline and persist final call outcome to DynamoDB."""
        self._is_running = False
        self._stopped = True
        answered_event = getattr(self, "_answered_event", None)
        if answered_event:
            answered_event.set()  # releases run() if the phone was still ringing
        opening_audio = getattr(self, "_opening_audio_task", None)
        if opening_audio and not opening_audio.done():
            opening_audio.cancel()
        if self.media_server:
            self.media_server.unregister_session(self.call_id)
        try:
            await self.task.cancel()
        except Exception:
            pass
        try:
            sig_server = self.sip_signaling_server or globals().get("sip_signaling_server")
            if sig_server:
                await sig_server.send_bye(self.call_id)
        except Exception:
            pass

        if not getattr(self, "_answered", True):
            # Built while ringing and never answered: the SIP layer already
            # recorded the failure (busy / no answer) and queued the retry.
            logger.info(f"[PREPARE] {self.call_id} was never answered; pipeline torn down")
            return

        # Persist the final outcome. No-contact outcomes (and a patient hanging
        # up mid-conversation) are queued for a retry; a callback the patient
        # asked for was already written by reschedule_call.
        try:
            final_status = "NO_ENGAGEMENT"
            if self.nova_sonic and self.nova_sonic.session:
                final_status = getattr(self.nova_sonic.session, "final_disposition", "NO_ENGAGEMENT")
            patient_id = self.patient.patient_id if self.patient else None
            if patient_id and not patient_id.startswith("P-"):
                if final_status in RETRYABLE_CALL_OUTCOMES:
                    await self.storage.schedule_retry(
                        patient_id=patient_id,
                        call_id=self.call_id,
                        reason=final_status,
                    )
                elif final_status != "RESCHEDULED":
                    self.storage.update_call_outcome(
                        patient_id=patient_id,
                        call_id=self.call_id,
                        call_status=final_status,
                    )
                    summary = format_call_summary(final_status)
                    await self.storage.mark_call_ended(patient_id, summary)
                logger.info(
                    f"[CALL_ENDED] Recorded outcome patient_id={patient_id} "
                    f"outcome={final_status} call_id={self.call_id}"
                )
                death_cue = (
                    getattr(self.nova_sonic.session, "death_cue_text", None)
                    if self.nova_sonic and self.nova_sonic.session
                    else None
                )
                if death_cue and final_status != "DECEASED":
                    logger.info(
                        f"[DEATH_CUE_REVIEW] call {self.call_id} ended as {final_status} "
                        f"after a possible death mention: {death_cue!r}"
                    )
                    await self.storage.flag_possible_death(patient_id, death_cue)
        except Exception:
            logger.exception(f"[CALL_ENDED_ERROR] Failed to update DynamoDB for call {self.call_id}")


class KVSStreamConsumerWorker:
    """Consumes audio fragments from Amazon Connect Kinesis Video Stream and manages call pipelines."""

    def __init__(
        self,
        stream_name: str = DEFAULT_STREAM_NAME,
        region_name: str = DEFAULT_REGION,
        profile_name: str = DEFAULT_PROFILE,
        media_server: Optional[SIPRTPMediaServer] = None,
        sip_signaling_server: Optional[SIPSignalingServer] = None,
    ):
        self.stream_name = stream_name
        self.region_name = region_name
        self.profile_name = profile_name
        self.session = get_aws_session(profile=profile_name, region=region_name)
        self.media_server = media_server
        self.sip_signaling_server = sip_signaling_server
        self.active_sessions: Dict[str, CaraVoicePipeline] = {}
        self.monitored_streams: Set[str] = set()
        self._running = False

    def get_kvs_media_client_for_stream(self, target_stream: str) -> Any:
        """Fetch the data endpoint for GET_MEDIA and return the kinesis-video-media client."""
        kvs = self.session.client("kinesisvideo")
        try:
            endpoint_resp = kvs.get_data_endpoint(
                StreamName=target_stream,
                APIName="GET_MEDIA",
            )
            data_endpoint = endpoint_resp.get("DataEndpoint")
            logger.info(f"Resolved KVS GET_MEDIA data endpoint for '{target_stream}': {data_endpoint}")
            return self.session.client("kinesis-video-media", endpoint_url=data_endpoint)
        except Exception as e:
            logger.warning(f"Could not resolve KVS data endpoint for '{target_stream}': {e}")
            return None

    async def create_session(
        self,
        contact_id: str,
        patient_id: Optional[str] = None,
        first_name: str = "",
        last_name: str = "",
        practice_name: str = "Metro Health Clinic",
        phone_number: str = "+15822671755",
        call_mode: str = "MODE_2_NORMAL_COACHING",
        callback_number: str = "",
        provider_name: str = "",
        remote_rtp_addr: Optional[Tuple[str, int]] = None,
        invite_headers: Optional[Dict[str, str]] = None,
        human_agent_phone: str = "",
        timezone: str = "",
        voice: str = "",
        answered: bool = True,
    ) -> CaraVoicePipeline:
        """Instantiate and start a new CaraVoicePipeline for an active contact."""
        if contact_id in self.active_sessions:
            return self.active_sessions[contact_id]

        # Attempt to fetch patient attributes from Amazon Connect if available
        if DEFAULT_CONNECT_INSTANCE and contact_id != "live-contact" and "-" in contact_id and len(contact_id) >= 30:
            try:
                connect = self.session.client("connect")
                attrs_resp = connect.get_contact_attributes(
                    InstanceId=DEFAULT_CONNECT_INSTANCE,
                    InitialContactId=contact_id,
                )
                attrs = attrs_resp.get("Attributes", {})
                first_name = attrs.get("FirstName", first_name)
                last_name = attrs.get("LastName", last_name)
                practice_name = attrs.get("PracticeName", practice_name)
                call_mode = attrs.get("CallMode", call_mode)
            except Exception as attr_err:
                logger.debug(f"Could not fetch Connect attributes for {contact_id}: {attr_err}")

        if not first_name:
            logger.warning(f"No patient name for contact {contact_id}; Cara will use a neutral greeting")

        resolved_patient_id = patient_id or f"P-{contact_id[:8]}"
        mode_enum = CallMode.MODE_1_DIRECT_HANDOFF if call_mode == "MODE_1_DIRECT_HANDOFF" else CallMode.MODE_2_NORMAL_COACHING
        patient_ctx = PatientContext(
            patient_id=resolved_patient_id,
            first_name=first_name,
            last_name=last_name,
            practice_name=practice_name,
            phone_number=phone_number,
            callback_number=callback_number,
            provider_name=provider_name,
            call_mode=mode_enum,
            direct_agent=mode_enum == CallMode.MODE_1_DIRECT_HANDOFF,
            human_agent_phone=human_agent_phone,
            timezone=timezone,
            voice=voice,
        )
        pipeline = CaraVoicePipeline(
            patient_context=patient_ctx,
            call_id=contact_id,
            connect_instance_id=DEFAULT_CONNECT_INSTANCE,
            media_server=self.media_server,
            sip_signaling_server=self.sip_signaling_server or globals().get("sip_signaling_server"),
            remote_rtp_addr=remote_rtp_addr,
            invite_headers=invite_headers,
            answered=answered,
        )
        self.active_sessions[contact_id] = pipeline
        asyncio.create_task(pipeline.run())
        logger.info(f"Created active voice session for contact {contact_id} (Patient ID: {resolved_patient_id}, {first_name} {last_name})")
        return pipeline

    async def _consume_single_stream(self, target_stream: str):
        """Consume and demux audio from a single KVS stream into active sessions."""
        from gateway.kvs_transport import MatroskaKVSStreamDemuxer
        demuxer = MatroskaKVSStreamDemuxer(target_track=1)
        media_client = self.get_kvs_media_client_for_stream(target_stream)
        if not media_client:
            return

        contact_id = target_stream.split("-contact-")[-1] if "-contact-" in target_stream else target_stream
        logger.info(f"[TELEPHONY] Attached stream consumer to contact {contact_id} (Stream: {target_stream})")
        session = await self.create_session(contact_id=contact_id)

        try:
            # Unconditionally use NOW selector to read real-time live edge audio
            selector = {"StartSelectorType": "NOW"}
            response = media_client.get_media(
                StreamName=target_stream,
                StartSelector=selector,
            )
            payload_stream = response.get("Payload")
            if payload_stream:
                logger.info(f"Connected to payload stream for '{target_stream}' (Contact: {contact_id})")
                while self._running:
                    # Read asynchronously in thread pool so it does NOT block the asyncio event loop
                    chunk = await asyncio.to_thread(payload_stream.read, 4096)
                    if not chunk:
                        await asyncio.sleep(0.05)
                        continue
                    pcm_frames = demuxer.feed_bytes(chunk)
                    for pcm in pcm_frames:
                        logger.info(
                            f"[AUDIO_IN] Received {len(pcm)} bytes from caller microphone for Contact {contact_id}"
                        )
                        await session.push_audio(pcm)
        except Exception as e:
            logger.warning(f"Stream consumer iteration note for '{target_stream}': {e}")
        finally:
            if contact_id in self.active_sessions and contact_id != "live-contact" and contact_id != self.stream_name:
                logger.info(f"[TELEPHONY] Disconnected and cleaning up session for contact {contact_id}")
                s = self.active_sessions.pop(contact_id, None)
                if s:
                    asyncio.create_task(s.stop())
            self.monitored_streams.discard(target_stream)

    async def run_worker_loop(self):
        """Continuously discover active KVS streams and stream audio into active Pipecat pipelines."""
        self._running = True
        logger.info(f"KVS Stream Consumer Worker listening on '{self.stream_name}' prefix ({self.region_name})...")
        kvs_control = self.session.client("kinesisvideo")

        # Start consumer for base stream
        asyncio.create_task(self._consume_single_stream(self.stream_name))
        self.monitored_streams.add(self.stream_name)

        while self._running:
            try:
                # Discover new contact streams
                stream_list = kvs_control.list_streams(
                    StreamNameCondition={"ComparisonOperator": "BEGINS_WITH", "ComparisonValue": "careharmony-outreach-media-stream"}
                ).get("StreamInfoList", [])

                for s_info in stream_list:
                    s_name = s_info.get("StreamName")
                    if s_name and s_name not in self.monitored_streams and s_info.get("Status") == "ACTIVE":
                        self.monitored_streams.add(s_name)
                        logger.info(f"[DISCOVERY] Found active Connect KVS stream: {s_name}")
                        asyncio.create_task(self._consume_single_stream(s_name))
            except Exception as ex:
                logger.warning(f"KVS discovery iteration note: {ex}")

            await asyncio.sleep(2)

    def stop(self):
        """Stop worker and clean up all sessions."""
        self._running = False
        for cid, session in list(self.active_sessions.items()):
            asyncio.create_task(session.stop())
        self.active_sessions.clear()
        self.monitored_streams.clear()
        logger.info("KVS Stream Consumer Worker stopped.")


# ==============================================================================
# FASTAPI LIFECYCLE & SERVER
# ==============================================================================

app = FastAPI(title="CareHarmony Pipecat Voice Gateway")
worker: Optional[KVSStreamConsumerWorker] = None
rtp_media_server: Optional[SIPRTPMediaServer] = None
sip_signaling_server: Optional[SIPSignalingServer] = None


def _session_args(call_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """create_session() arguments from the patient details given at dial time."""
    return {
        "contact_id": call_id,
        "patient_id": meta.get("patient_id") or meta.get("empi") or f"P-{call_id[:8]}",
        "first_name": meta.get("first_name", ""),
        "last_name": meta.get("last_name", ""),
        "practice_name": meta.get("practice_name", "Metro Health Clinic"),
        "call_mode": meta.get("call_mode", "MODE_2_NORMAL_COACHING"),
        "callback_number": meta.get("practice_callback_number", ""),
        "provider_name": meta.get("provider_name", ""),
        "human_agent_phone": meta.get("human_agent_phone", ""),
        "timezone": meta.get("timezone", ""),
        "voice": meta.get("voice", ""),
    }


# Outbound calls whose pipeline is still being built before pickup
_preparing_calls: Dict[str, asyncio.Task] = {}


async def handle_sip_call_preparing(call_id: str, patient_meta: Optional[Dict[str, Any]] = None):
    """Build Cara and open Nova Sonic while the patient's phone rings.

    Both are then ready the moment the call is answered, instead of taking
    about a second after pickup while the caller's first words were lost.
    """
    global worker
    if not worker:
        return
    _preparing_calls[call_id] = asyncio.current_task()
    try:
        await worker.create_session(answered=False, **_session_args(call_id, patient_meta or {}))
    except Exception:
        logger.exception(f"[PREPARE] Could not build the pipeline early for {call_id}; it will be built at pickup")
        return
    finally:
        _preparing_calls.pop(call_id, None)

    if sip_signaling_server and call_id not in sip_signaling_server.active_dialogs:
        # The call failed while the pipeline was being built
        session = worker.active_sessions.pop(call_id, None)
        if session:
            await session.stop()


async def _on_recording_saved(call_id: str, s3_uri: str, duration: float, metadata: Dict[str, Any]):
    """Put the recording's S3 location on the patient's outreach record."""
    patient_id = metadata.get("patient_id")
    if not patient_id or patient_id.startswith("P-"):
        return
    storage = await asyncio.to_thread(OutreachRecordService)
    await asyncio.to_thread(storage.record_call_recording, patient_id, call_id, s3_uri, duration)


async def _record_call_started(patient_id: str, call_id: str):
    try:
        storage = await asyncio.to_thread(OutreachRecordService)
        await storage.mark_call_started(patient_id=patient_id, call_id=call_id)
    except Exception as e:
        logger.warning(f"mark_call_started error for {patient_id}: {e}")


async def handle_sip_call_started(
    call_id: str,
    remote_sdp: SDPMessage,
    remote_rtp_addr: Tuple[str, int],
    patient_meta: Optional[Dict[str, Any]] = None,
):
    """Callback when the call is answered (outbound 200 OK, or an inbound INVITE we accepted)."""
    global worker
    if not worker:
        return
    args = _session_args(call_id, patient_meta or {})
    preparing = _preparing_calls.get(call_id)
    if preparing:
        await asyncio.wait([preparing])  # answered while still being built

    pipeline = worker.active_sessions.get(call_id)
    if pipeline is None:
        logger.info(
            f"[SIP_CALL] Initiating CaraVoicePipeline for Call-ID {call_id} -> Remote RTP {remote_rtp_addr} "
            f"(Patient ID: {args['patient_id']}, Patient: {args['first_name']} {args['last_name']}, "
            f"Practice: {args['practice_name']})"
        )
        pipeline = await worker.create_session(remote_rtp_addr=remote_rtp_addr, **args)
    else:
        logger.info(f"[SIP_CALL] {call_id} answered; using the pipeline built while ringing")
    pipeline.mark_answered()

    # Off the critical path: the database write must not delay the disclosure
    asyncio.create_task(_record_call_started(args["patient_id"], call_id))


async def handle_sip_call_ended(call_id: str):
    """Callback when a SIP BYE is received."""
    global worker
    if worker and call_id in worker.active_sessions:
        logger.info(f"[SIP_CALL] Cleaning up session for Call-ID {call_id}")
        session = worker.active_sessions.pop(call_id, None)
        if session:
            await session.stop()


async def _on_call_failed(call_id: str, patient_id: str, reason: str, sip_code: Optional[int] = None):
    """
    Fires when an outbound call never connects. Any pipeline built
    while it rang is torn down without recording an outcome (see
    CaraVoicePipeline.stop), so update DynamoDB directly.
    """
    logger.info(
        f"[PIPELINE_CALL_FAILED] call_id={call_id} "
        f"patient_id={patient_id} reason={reason} sip_code={sip_code}"
    )
    if not patient_id or patient_id.startswith("P-"):
        logger.error(
            f"[PIPELINE_CALL_FAILED_ERROR] patient_id not "
            f"resolved: {patient_id} — cannot update DynamoDB"
        )
        return
    try:
        storage_service = OutreachRecordService()
        await storage_service.schedule_retry(
            patient_id=patient_id,
            call_id=call_id,
            reason=reason,
            sip_code=sip_code,
        )
    except Exception:
        logger.exception(
            f"[PIPELINE_CALL_FAILED_ERROR] DynamoDB update "
            f"failed for {patient_id}"
        )


@app.on_event("startup")
async def startup_event():
    global worker, rtp_media_server, sip_signaling_server
    # Load the caller speech model and the Polly client once, before any call needs them
    await asyncio.to_thread(load_vad_model)
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Kolkata")
    except Exception:
        logger.error("[STARTUP] No timezone data in this image: callback times will be read in UTC")
    try:
        await asyncio.to_thread(_get_polly_client)
    except Exception as e:
        logger.warning(f"Polly client not ready at startup: {e}")
    # 1. Start SIP/RTP Media Server (Port 10000)
    rtp_media_server = SIPRTPMediaServer(
        host="0.0.0.0",
        port=DEFAULT_RTP_PORT,
        echo_mode=DEFAULT_ECHO_MODE,
    )
    rtp_media_server.on_recording_saved = _on_recording_saved
    await rtp_media_server.start()
    logger.info(
        f"[RECORDING] Call recording {'on, bucket ' + rtp_media_server.recording_bucket if rtp_media_server.recording_bucket else 'off'}"
    )

    # 2. Start SIP RFC 3261 Signaling Server (Port 5060)
    sip_signaling_server = SIPSignalingServer(
        host="0.0.0.0",
        port=DEFAULT_SIP_PORT,
        public_ip=DEFAULT_PUBLIC_IP,
        media_server=rtp_media_server,
        on_call_started=handle_sip_call_started,
        on_call_ended=handle_sip_call_ended,
        on_call_failed=_on_call_failed,
        on_call_preparing=handle_sip_call_preparing,
    )
    sip_signaling_server.on_call_failed = _on_call_failed
    await sip_signaling_server.start()

    # 3. Start KVS Stream Consumer Worker
    worker = KVSStreamConsumerWorker(
        media_server=rtp_media_server,
        sip_signaling_server=sip_signaling_server,
    )
    asyncio.create_task(worker.run_worker_loop())
    logger.info("Pipecat Gateway background workers (SIP Signaling + RTP Media + KVS) started.")


@app.on_event("shutdown")
async def shutdown_event():
    global worker, rtp_media_server, sip_signaling_server
    if worker:
        worker.stop()
    if rtp_media_server:
        # Calls cut off by a redeploy still get their recording uploaded
        await rtp_media_server.finish_all_recordings()
        rtp_media_server.stop()
    if sip_signaling_server:
        sip_signaling_server.stop()
    logger.info("Pipecat Gateway background workers stopped.")


@app.get("/health")
def health_check():
    return {
        "status": "HEALTHY",
        "service": "careharmony-voice-gateway",
        "rtp_port": DEFAULT_RTP_PORT,
    }


@app.get("/ready")
def readiness_check():
    active_count = len(worker.active_sessions) if worker else 0
    return {
        "status": "READY",
        "active_sessions": active_count,
        "stream_name": DEFAULT_STREAM_NAME,
        "region": DEFAULT_REGION,
        "rtp_port": DEFAULT_RTP_PORT,
    }


@app.post("/sessions/start")
async def start_session_endpoint(payload: Dict[str, Any]):
    """Allow programmatic initiation of an active call pipeline."""
    global worker
    if not worker:
        return Response(content=json.dumps({"error": "Worker not initialized"}), status_code=500)

    contact_id = payload.get("contact_id", f"contact-{os.urandom(4).hex()}")
    remote_rtp = None
    if "remote_ip" in payload and "remote_port" in payload:
        remote_rtp = (payload["remote_ip"], int(payload["remote_port"]))

    pipeline = await worker.create_session(
        contact_id=contact_id,
        patient_id=payload.get("patient_id") or payload.get("empi"),
        first_name=payload.get("first_name", ""),
        last_name=payload.get("last_name", ""),
        practice_name=payload.get("practice_name", "Metro Health Clinic"),
        call_mode=payload.get("call_mode", "MODE_2_NORMAL_COACHING"),
        callback_number=payload.get("practice_callback_number", ""),
        provider_name=payload.get("provider_name", ""),
        remote_rtp_addr=remote_rtp,
    )
    return {
        "status": "STARTED",
        "contact_id": contact_id,
        "session_config": pipeline.nova_sonic.get_session_config(),
    }


@app.post("/calls/originate")
async def originate_call_endpoint(payload: Dict[str, Any]):
    """Originate an outbound SIP call via Chime Voice Connector."""
    global sip_signaling_server
    if not sip_signaling_server:
        return Response(content=json.dumps({"error": "SIP Signaling server not initialized"}), status_code=500)

    destination_phone = payload.get("destination_phone")
    if not destination_phone:
        return Response(content=json.dumps({"error": "destination_phone is required"}), status_code=400)

    first_name = payload.get("patient_first_name", payload.get("first_name", ""))
    last_name = payload.get("patient_last_name", payload.get("last_name", ""))
    if not first_name:
        return Response(content=json.dumps({"error": "patient_first_name is required"}), status_code=400)

    source_phone = payload.get("source_phone", "+12819568434")
    vc_host = payload.get("vc_host", "cafawc0zxzbcpxusmsonrd.voiceconnector.chime.aws")
    patient_meta = {
        "patient_id": payload.get("patient_id") or payload.get("empi") or "TESTPT-0000",
        "empi": payload.get("empi") or payload.get("patient_id") or "TESTPT-0000",
        "first_name": first_name,
        "last_name": last_name,
        "practice_name": payload.get("practice_name", "Metro Health Clinic"),
        "practice_callback_number": payload.get("practice_callback_number", ""),
        "provider_name": payload.get("provider_name", ""),
        "call_mode": payload.get("call_mode", "MODE_2_NORMAL_COACHING"),
        "human_agent_phone": payload.get("human_agent_phone", ""),
        "timezone": payload.get("timezone", ""),
        "voice": payload.get("voice", ""),
    }

    call_id = await sip_signaling_server.originate_call(
        destination_phone=destination_phone,
        source_phone=source_phone,
        vc_host=vc_host,
        patient_meta=patient_meta,
    )
    return {
        "status": "INITIATED",
        "call_id": call_id,
        "destination_phone": destination_phone,
        "source_phone": source_phone,
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    logger.info(f"Starting CareHarmony Pipecat Voice Gateway on port {port} (RTP UDP {DEFAULT_RTP_PORT})...")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
