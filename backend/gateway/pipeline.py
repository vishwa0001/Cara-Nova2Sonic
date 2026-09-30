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
from typing import Any, Dict, Optional, Set, Tuple

import boto3
from botocore.exceptions import ClientError, ProfileNotFound
from fastapi import FastAPI, Response
import uvicorn

from pipecat.frames.frames import AudioRawFrame, StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask

from domain_rules import CallMode, PatientContext
from gateway.kvs_transport import KVSInputProcessor, KVSOutputProcessor
from gateway.nova_sonic_service import NovaSonicLLMService
from gateway.sip_transport import SDPMessage, SIPRTPMediaServer, SIPRTPOutputProcessor, SIPSignalingServer
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


async def synthesize_forced_closing(text: str) -> bytes:
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
            VoiceId="Ruth",
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
    ):
        self.call_id = call_id
        self.campaign_id = campaign_id
        self.media_server = media_server
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
            return await sig_server.dial_agent_and_bridge(self.call_id, agent_phone)

        # 1. Processors
        self.storage = OutreachRecordService()
        self.tool_executor = OutreachToolExecutor(
            api_base_url=api_base_url,
            connect_instance_id=connect_instance_id or DEFAULT_CONNECT_INSTANCE,
            patient_context=self.patient,
            contact_id=self.call_id,
            storage_service=self.storage,
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

        self.input_processor = KVSInputProcessor(target_sample_rate=16000)
        self.nova_sonic = NovaSonicLLMService(
            patient_context=self.patient,
            contact_id=self.call_id,
            on_hangup=_on_cara_hangup,
            on_forced_audio=_on_forced_audio,
            on_transfer=_on_cara_transfer,
            tool_executor=self.tool_executor,
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
        self._is_running = False

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
        self._is_running = True
        logger.info(f"Starting Cara Voice Pipeline for call {self.call_id} (Patient {self.patient.patient_id})")
        
        # Start pipeline runner in a background task
        runner_task = asyncio.create_task(self.runner.run(self.task))

        # Play mandatory opening disclosure
        await self._play_opening_disclosure()

        try:
            await runner_task
        except Exception as e:
            logger.info(f"Pipeline runner task completed for {self.call_id}: {e}")

    async def push_audio(self, audio_chunk: bytes):
        """Feed inbound telephony audio from SIP/RTP or KVS into the Pipecat pipeline."""
        if self._is_running and audio_chunk:
            logger.info(f"[AUDIO_IN] Ingested audio frame ({len(audio_chunk)} bytes) for Contact {self.call_id}")
            frame = AudioRawFrame(audio=audio_chunk, sample_rate=8000, num_channels=1)
            try:
                await self.task.queue_frame(frame)
            except Exception as ex:
                logger.debug(f"Audio push frame exception: {ex}")

    async def stop(self):
        """Cancel and terminate the pipeline and persist final call outcome to DynamoDB."""
        self._is_running = False
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
        )
        pipeline = CaraVoicePipeline(
            patient_context=patient_ctx,
            call_id=contact_id,
            connect_instance_id=DEFAULT_CONNECT_INSTANCE,
            media_server=self.media_server,
            sip_signaling_server=self.sip_signaling_server or globals().get("sip_signaling_server"),
            remote_rtp_addr=remote_rtp_addr,
            invite_headers=invite_headers,
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


async def handle_sip_call_started(
    call_id: str,
    remote_sdp: SDPMessage,
    remote_rtp_addr: Tuple[str, int],
    patient_meta: Optional[Dict[str, Any]] = None,
):
    """Callback when an inbound SIP INVITE is accepted and remote RTP is negotiated."""
    global worker
    if worker:
        meta = patient_meta or {}
        patient_id = meta.get("patient_id") or meta.get("empi") or f"P-{call_id[:8]}"
        first_name = meta.get("first_name", "")
        last_name = meta.get("last_name", "")
        practice_name = meta.get("practice_name", "Metro Health Clinic")
        call_mode = meta.get("call_mode", "MODE_2_NORMAL_COACHING")
        practice_callback_number = meta.get("practice_callback_number", "")
        provider_name = meta.get("provider_name", "")
        human_agent_phone = meta.get("human_agent_phone", "")
        campaign_timezone = meta.get("timezone", "")
        logger.info(
            f"[SIP_CALL] Initiating CaraVoicePipeline for Call-ID {call_id} -> Remote RTP {remote_rtp_addr} "
            f"(Patient ID: {patient_id}, Patient: {first_name} {last_name}, Practice: {practice_name})"
        )
        try:
            storage = OutreachRecordService()
            await storage.mark_call_started(
                patient_id=patient_id,
                call_id=call_id,
            )
        except Exception as e:
            logger.warning(f"mark_call_started error for {patient_id}: {e}")

        await worker.create_session(
            contact_id=call_id,
            patient_id=patient_id,
            first_name=first_name,
            last_name=last_name,
            practice_name=practice_name,
            call_mode=call_mode,
            callback_number=practice_callback_number,
            provider_name=provider_name,
            remote_rtp_addr=remote_rtp_addr,
            human_agent_phone=human_agent_phone,
            timezone=campaign_timezone,
        )


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
    Fires when an outbound call never connects. No
    CaraVoicePipeline session exists for this call_id (only
    created on 200 OK), so update DynamoDB directly.
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
    # 1. Start SIP/RTP Media Server (Port 10000)
    rtp_media_server = SIPRTPMediaServer(
        host="0.0.0.0",
        port=DEFAULT_RTP_PORT,
        echo_mode=DEFAULT_ECHO_MODE,
    )
    await rtp_media_server.start()

    # 2. Start SIP RFC 3261 Signaling Server (Port 5060)
    sip_signaling_server = SIPSignalingServer(
        host="0.0.0.0",
        port=DEFAULT_SIP_PORT,
        public_ip=DEFAULT_PUBLIC_IP,
        media_server=rtp_media_server,
        on_call_started=handle_sip_call_started,
        on_call_ended=handle_sip_call_ended,
        on_call_failed=_on_call_failed,
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
def shutdown_event():
    global worker, rtp_media_server, sip_signaling_server
    if worker:
        worker.stop()
    if rtp_media_server:
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
