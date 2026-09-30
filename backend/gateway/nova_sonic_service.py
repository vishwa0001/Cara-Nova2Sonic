"""Amazon Bedrock Nova 2 Sonic Speech-to-Speech (S2S) Duplex Streaming Service for Pipecat.

Target Model: amazon.nova-2-sonic-v1:0 (Region: us-east-1).
Provides true end-to-end full-duplex speech-to-speech via HTTP/2 bidirectional event streaming
(invoke_model_with_bidirectional_stream). Replaces legacy multi-stage VAD + Bedrock Converse + Polly
pipeline with direct native audio streaming (<500ms turnaround, <40ms interruption cutoff).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import boto3
import numpy as np
import scipy.signal
from botocore.exceptions import ProfileNotFound
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    StartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from aws_sdk_bedrock_runtime.client import (
    AsyncBedrockRuntimeClient,
    AsyncBedrockRuntimeConfig,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
)
from smithy_http.aio.crt import AWSCRTHTTPClient

from domain_rules import (
    CallMode,
    PatientContext,
    SCRIPT_DEFINITIONS,
    generate_nova_sonic_system_prompt,
)
from gateway.beep_detector import BeepDetector
from gateway.tools import (
    TOOL_SCHEMAS,
    OutreachToolExecutor,
    infer_disposition_from_speech,
)
from storage.outreach_records import format_call_summary

# Apply patch for Pipecat 1.11.0 AudioRawFrame missing __post_init__ attributes
_orig_audio_post_init = AudioRawFrame.__post_init__


def _patched_audio_post_init(self):
    Frame.__post_init__(self)
    _orig_audio_post_init(self)


AudioRawFrame.__post_init__ = _patched_audio_post_init

logger = logging.getLogger("nova_sonic_service")

# Reopen attempts when Nova Sonic fails a stream before Cara has spoken.
MAX_STREAM_RESTARTS = 2
STREAM_RESTART_DELAY_SECONDS = 0.5

VOICEMAIL_GREETING_PATTERNS = [
    "leave a message", "leave your message", "not available",
    "please leave", "after the tone", "after the beep",
    "voice mailbox", "voicemail", "mailbox is full",
    "record your message", "unable to take your call",
]

UNCLEAR_REPLY_PATTERNS = {
    "um", "uh", "hmm", "huh", "what", "sorry", "yeah",
    "okay", "ok", "mm", "mhm",
}

def _is_unclear_reply(text: str) -> bool:
    """
    Heuristic: a reply is 'unclear' if it's very short and/or
    consists only of filler words with no substantive content —
    the kind of response that doesn't actually answer the
    double-check question.
    """
    if not text:
        return True
    cleaned = text.strip().lower().rstrip(".,!?")
    words = cleaned.split()
    if len(words) == 0:
        return True
    if len(words) <= 2 and all(w in UNCLEAR_REPLY_PATTERNS for w in words):
        return True
    return False


def _parse_tool_input(event_data: dict) -> dict:
    """
    Extract tool arguments from a Nova Sonic toolUse event.

    Nova Sonic delivers the arguments as a JSON string in the
    `content` field; `input`/`parameters` are kept as fallbacks.
    Returns an empty dict if nothing parseable is present.
    """
    raw = (
        event_data.get("content")
        or event_data.get("input")
        or event_data.get("parameters")
        or {}
    )
    try:
        # Tolerate double-encoded JSON strings.
        while isinstance(raw, str):
            raw = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _parse_generation_stage(event_data: dict) -> Optional[str]:
    """
    Read the generationStage ("SPECULATIVE" or "FINAL") from a Nova
    Sonic contentStart event. Nova sends every assistant sentence
    twice: a SPECULATIVE copy before its audio and a FINAL copy after.
    `additionalModelFields` is a JSON string. Returns None if absent.
    """
    fields = event_data.get("additionalModelFields")
    try:
        while isinstance(fields, str):
            fields = json.loads(fields) if fields.strip() else {}
    except (ValueError, TypeError):
        return None
    if not isinstance(fields, dict):
        return None
    stage = fields.get("generationStage")
    return stage.upper() if isinstance(stage, str) else None


# Every scripted closing line ends with "goodbye" (see domain_rules.py
# OutreachScript and the safety scripts), so a completed assistant turn
# containing it means Cara has said farewell and the call must end.
CLOSING_MARKER = "goodbye"


def _is_closing_turn(text: str) -> bool:
    return CLOSING_MARKER in (text or "").lower()


def _normalize_quotes(text: str) -> str:
    return (text or "").lower().replace("\u2019", "'").replace("\u2014", "-")


# Pieces of the two callback closings in domain_rules (unspecified / specific)
_CALLBACK_UNSPECIFIED_MARKER = "circle back soon"
_CALLBACK_SPECIFIC_MARKER = "aim to reach you then"


def _is_callback_closing(text: str) -> bool:
    t = _normalize_quotes(text)
    return _CALLBACK_UNSPECIFIED_MARKER in t or _CALLBACK_SPECIFIC_MARKER in t


# Tool result that turns a Direct to Human Agent call into a normal call
_SWITCH_TO_NORMAL_RESULT = {
    "success": True,
    "status": "switch_to_normal_call",
    "message": "The specialist could not be reached. Continue as a normal call from the SECTION 1 opening sentence.",
}


# The system prompt wraps every scripted line in quotation marks and
# Nova copies them into its text output (call 5c32159a). Strip them so
# transcripts and the DB hold the sentence only.
_SCRIPT_QUOTES = str.maketrans("", "", "\"“”")


def _strip_script_quotes(text: str) -> str:
    return (text or "").translate(_SCRIPT_QUOTES)


# Words a caller uses when the patient has died. Phone ASR mishears
# these (call 5c32159a heard "passed away" as "was very busy"), so the
# gateway never acts on them itself: it only flags the call for human
# review when one was heard but the call did not end as DECEASED.
# "no more calls" is a do-not-call request, not a death.
_DEATH_CUE_PATTERN = re.compile(
    r"\b(passed( away| on)?|died|dead|deceased|no longer with us|funeral"
    r"|no more(?! calls?\b| phone))\b"
)


def _has_death_cue(text: str) -> bool:
    return bool(_DEATH_CUE_PATTERN.search((text or "").lower()))


# ==============================================================================
# AUDIO RESAMPLING HELPERS
# ==============================================================================

def resample_8k_to_16k(pcm8k_bytes: bytes) -> bytes:
    """Resample 8kHz 16-bit linear PCM telephony audio to 16kHz for Nova Sonic audioInput.
    
    Uses polyphase filter resampling (2x upsampling) via scipy.signal.resample_poly.
    Fallback: NumPy linear interpolation.
    """
    if not pcm8k_bytes:
        return b""
    try:
        samples = np.frombuffer(pcm8k_bytes, dtype=np.int16)
        resampled = scipy.signal.resample_poly(samples, up=2, down=1).astype(np.int16)
        return resampled.tobytes()
    except Exception as e:
        logger.debug(f"scipy resample_poly 8k->16k fallback to numpy: {e}")
        samples = np.frombuffer(pcm8k_bytes, dtype=np.int16)
        num_target = len(samples) * 2
        resampled = np.interp(
            np.linspace(0, len(samples), num_target, endpoint=False),
            np.arange(len(samples)),
            samples,
        ).astype(np.int16)
        return resampled.tobytes()


def resample_24k_to_8k(pcm24k_bytes: bytes) -> bytes:
    """Resample 24kHz 16-bit linear PCM audio from Nova Sonic audioOutput to 8kHz for RTP output.
    
    Uses polyphase filter resampling (3x downsampling) via scipy.signal.resample_poly.
    Fallback: NumPy linear interpolation.
    """
    if not pcm24k_bytes:
        return b""
    try:
        samples = np.frombuffer(pcm24k_bytes, dtype=np.int16)
        resampled = scipy.signal.resample_poly(samples, up=1, down=3).astype(np.int16)
        return resampled.tobytes()
    except Exception as e:
        logger.debug(f"scipy resample_poly 24k->8k fallback to numpy: {e}")
        samples = np.frombuffer(pcm24k_bytes, dtype=np.int16)
        num_target = len(samples) // 3
        resampled = np.interp(
            np.linspace(0, len(samples), num_target, endpoint=False),
            np.arange(len(samples)),
            samples,
        ).astype(np.int16)
        return resampled.tobytes()


async def _synthesize_forced_closing_helper(text: str) -> bytes:
    """Helper to synthesize forced closing lines via Polly, bypassing Nova Sonic."""
    try:
        from gateway.pipeline import synthesize_forced_closing
    except ImportError:
        from backend.gateway.pipeline import synthesize_forced_closing
    return await synthesize_forced_closing(text)


# ==============================================================================
# NOVA SONIC SPEECH-TO-SPEECH DUPLEX SESSION
# ==============================================================================

class NovaSonicSession:
    """Manages full-duplex Speech-to-Speech streaming with Bedrock Amazon Nova 2 Sonic."""

    def __init__(
        self,
        session: Optional[boto3.Session] = None,
        model_id: str = "amazon.nova-2-sonic-v1:0",
        system_prompt: str = "",
        contact_id: str = "",
        patient_context: Optional[PatientContext] = None,
        on_audio_out: Optional[Callable[[bytes], Any]] = None,
        on_interrupt: Optional[Callable[[], Any]] = None,
        on_transcript: Optional[Callable[[str, str], Any]] = None,
        on_hangup: Optional[Callable[[], Any]] = None,
        on_forced_audio: Optional[Callable[[bytes], Any]] = None,
        on_transfer: Optional[Callable[[str], Any]] = None,
        tool_executor: Optional[OutreachToolExecutor] = None,
        voice_id: str = "ruth",
        region_name: Optional[str] = None,
    ):
        self.boto_session = session
        self.model_id = model_id
        self.system_prompt = system_prompt
        self.contact_id = contact_id
        self.patient = patient_context
        self.patient_context = patient_context
        self.voice_id = voice_id or os.getenv("NOVA_SONIC_VOICE", "ruth")
        # NOVA_SONIC_REGION moves only the model stream to another region;
        # every other AWS call keeps using AWS_REGION.
        self.region_name = (
            region_name
            or os.getenv("NOVA_SONIC_REGION")
            or os.getenv("AWS_REGION", "us-east-1")
        )

        # Callbacks wired into Pipecat pipeline
        self.on_audio_out = on_audio_out
        self.on_interrupt = on_interrupt
        self.on_transcript = on_transcript
        self.on_hangup = on_hangup
        self.on_forced_audio = on_forced_audio
        self.on_transfer = on_transfer

        # Tool Executor
        self.tool_executor = tool_executor or OutreachToolExecutor(
            patient_context=self.patient,
            contact_id=self.contact_id,
        )

        # Speaking state tracking
        self._is_assistant_speaking = False
        self._disconnect_after_speaking = False

        # Stream & Task state
        self.client: Optional[AsyncBedrockRuntimeClient] = None
        self.stream: Any = None
        self.receiver: Any = None
        self.is_active = False
        self._reader_task: Optional[asyncio.Task] = None
        # True while the human agent is being dialed; Cara stops listening
        self._transfer_pending = False
        # A direct-mode call switches to the normal script once, the first
        # time the specialist can't be reached
        self._direct_fallback_used = False
        # reschedule_call has booked the callback on this call
        self._reschedule_done = False
        self._last_user_text = ""
        # Times the Nova Sonic stream was reopened after it failed before
        # Cara said anything (see _recover_from_stream_error).
        self._stream_restarts = 0

        # Voicemail silence detection state (GENERAL traffic)
        self._greeting_spoken_at: Optional[float] = None
        self._user_spoke = False
        self._watchdog_task: Optional[asyncio.Task] = None
        self._beep_detector = BeepDetector()
        self._beep_confirmed = False
        self._voicemail_handled = False

        # Identifiers
        self.prompt_name = str(uuid.uuid4())
        self.system_content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())
        # Outcome if the call ended right now. It moves to PATIENT_HANGUP once
        # the patient speaks, and only a closing, tool result or voicemail /
        # identity path settles it after that.
        self.final_disposition = "NO_ENGAGEMENT"
        self._last_assistant_text = ""
        self._last_completed_assistant_turn = ""
        # generationStage of the TEXT content block currently streaming
        # (SPECULATIVE / FINAL / None when Nova doesn't say)
        self._text_stage: Optional[str] = None
        self._closing_hangup_armed = False
        # First caller line that sounded like the patient died, if any
        self.death_cue_text: Optional[str] = None

        # Identity double-check escalation tracking
        self._double_check_spoken = False
        self._identity_escalation_triggered = False
        self._identity_unclear_count = 0
        self._identity_double_check_watchdog_task: Optional[asyncio.Task] = None

        # Audio streaming counters & metrics
        self._audio_in_bytes = 0
        self._audio_out_bytes = 0
        self._last_counter_log = time.time()

    async def _initiate_hangup(self):
        """
        End the Nova Sonic session and signal the pipeline to send BYE.
        Called after the final farewell audio has fully drained to RTP.
        """
        try:
            await asyncio.sleep(0.1)  # 100ms grace: last RTP packet clears
            logger.info(
                f"[HANGUP] Initiating SIP BYE for {self.contact_id}"
            )
            await self.end()           # close Nova Sonic stream cleanly
            if self.on_hangup:
                await self.on_hangup() # signal pipeline to send BYE
        except Exception:
            logger.exception(
                f"[HANGUP_ERROR] Error during hangup for {self.contact_id}"
            )

    async def _arm_closing_hangup(self, spoken_text: str):
        """
        Hang up from the gateway once Cara has spoken a closing line.

        Nova Sonic does not reliably call terminate_call after saying
        "Goodbye" (toolChoice is auto), which left the line open and let
        the script restart (call 375604a0). The old Connect flow followed
        every closing message with a Disconnect block; this does the same,
        so terminate_call only refines the disposition.
        """
        if self._disconnect_after_speaking or self._closing_hangup_armed or not self.is_active:
            return
        self._closing_hangup_armed = True

        disposition = infer_disposition_from_speech(spoken_text)
        callback_closing = _is_callback_closing(spoken_text)
        if callback_closing:
            self.final_disposition = "RESCHEDULED"
            asyncio.create_task(self._book_callback_if_missed(spoken_text))
        elif disposition:
            self.final_disposition = disposition
        elif self.final_disposition == "PATIENT_HANGUP":
            # Cara closed the conversation, so the patient did not cut it short
            self.final_disposition = "COMPLETED"
        logger.info(
            f"[CLOSING_DETECTED] Closing line spoken without terminate_call for "
            f"{self.contact_id} (disposition={self.final_disposition}) — "
            f"will disconnect after audio drains"
        )

        if disposition and self.tool_executor:
            async def _record_outcome():
                try:
                    await self.tool_executor.terminate_call(
                        disposition=disposition,
                        reason="Closing line spoken; gateway-initiated hangup",
                        last_spoken_text=spoken_text,
                    )
                except Exception:
                    logger.exception(f"[CLOSING_DB_ERROR] Failed to record outcome for {self.contact_id}")

            asyncio.create_task(_record_outcome())

        asyncio.create_task(
            self._terminal_trigger_watchdog("CLOSING_LINE", timeout_seconds=20.0)
        )

        if self._is_assistant_speaking:
            self._disconnect_after_speaking = True  # _on_queue_drained hangs up
        else:
            # Farewell audio already drained before END_TURN arrived
            asyncio.create_task(self._initiate_hangup())

    async def _terminal_trigger_watchdog(
        self, trigger_name: str, timeout_seconds: float = 10.0
    ):
        """
        Safety net: if a terminal trigger fires but the call hasn't
        disconnected within timeout_seconds, force hangup regardless
        of cause. Call this as a background task immediately when
        any forced-closing handler starts.
        """
        await asyncio.sleep(timeout_seconds)
        if self.is_active:
            logger.error(
                f"[WATCHDOG_FORCED_HANGUP] {trigger_name} did not "
                f"result in disconnect within {timeout_seconds}s for "
                f"{self.contact_id} — forcing hangup as last resort"
            )
            self._disconnect_after_speaking = True
            if self.on_hangup:
                try:
                    await self.on_hangup()
                except Exception:
                    logger.exception(
                        f"[WATCHDOG_HANGUP_ERROR] for {self.contact_id}"
                    )

    async def _send_tool_result(self, tool_use_id: str, tool_name: str, result: Any):
        """Return a tool result to Nova Sonic (contentStart, toolResult, contentEnd)."""
        tool_content_name = f"tool-result-{tool_use_id}"
        await self._send_event({
            "contentStart": {
                "promptName": self.prompt_name,
                "contentName": tool_content_name,
                "interactive": False,
                "type": "TOOL",
                "role": "TOOL",
                "toolResultInputConfiguration": {
                    "toolUseId": tool_use_id,
                    "type": "TEXT",
                    "textInputConfiguration": {
                        "mediaType": "text/plain",
                    },
                },
            }
        })
        await self._send_event({
            "toolResult": {
                "promptName": self.prompt_name,
                "contentName": tool_content_name,
                "content": json.dumps(result),
            }
        })
        await self._send_event({
            "contentEnd": {
                "promptName": self.prompt_name,
                "contentName": tool_content_name,
            }
        })
        logger.info(
            f"[TOOL_RESULT_SENT] Contact {self.contact_id} tool={tool_name} use_id={tool_use_id} status=success"
        )

    async def _run_transfer(self, tool_use_id: str, agent_phone: str):
        """Dial the human agent and hand the patient over, or give Cara the call back.

        Cara's "please hold" line plays while the agent's phone rings. If a
        person answers, Nova Sonic is closed and the gateway relays audio
        between patient and agent. If not, the held tool result tells Cara
        the specialist didn't pick up, and she runs the callback flow.
        """
        try:
            answered = bool(await self.on_transfer(agent_phone))
        except Exception:
            logger.exception(f"[TRANSFER_ERROR] Dialing agent failed for {self.contact_id}")
            answered = False

        if answered:
            self.final_disposition = "TRANSFERRED"
            logger.info(f"[TRANSFER_CONNECTED] {self.contact_id} handed to agent {agent_phone}; closing Nova Sonic")
            if self._watchdog_task:
                self._watchdog_task.cancel()
            await self.end()
            return

        if not self.is_active:
            return  # patient hung up while the agent was ringing
        logger.info(f"[TRANSFER_NO_ANSWER] Agent {agent_phone} did not answer for {self.contact_id}; Cara offers a callback")
        self._transfer_pending = False  # Cara listens to the patient again
        if self._take_direct_fallback():
            logger.info(f"[DIRECT_FALLBACK] {self.contact_id} switches to the normal Cara call")
            result = dict(_SWITCH_TO_NORMAL_RESULT)
        else:
            result = {
                "success": True,
                "status": "agent_no_answer",
                "message": "The specialist's line rang but nobody picked up. Offer the patient a callback.",
            }
        await self._send_tool_result(tool_use_id, "evaluate_and_transfer_pes", result)

    def _take_direct_fallback(self) -> bool:
        """True (once) when a Direct to Human Agent call should continue as a normal call."""
        is_direct = bool(self.patient) and self.patient.call_mode == CallMode.MODE_1_DIRECT_HANDOFF
        if not is_direct or self._direct_fallback_used:
            return False
        self._direct_fallback_used = True
        return True

    async def _book_callback_if_missed(self, spoken_text: str):
        """Cara said a callback closing but Nova didn't call reschedule_call
        (call 3ee07acd): book it from the gateway so the patient is called back."""
        await asyncio.sleep(1.5)  # give Nova's own reschedule_call a moment
        if self._reschedule_done or not self.tool_executor:
            return
        unspecified = _CALLBACK_UNSPECIFIED_MARKER in _normalize_quotes(spoken_text)
        preferred = "unspecified" if unspecified else (self._last_user_text or "unspecified")
        logger.info(
            f"[CALLBACK_GUARD] Callback closing spoken without reschedule_call for "
            f"{self.contact_id}; booking it (preferred_time={preferred!r})"
        )
        try:
            result = await self.tool_executor.reschedule_call(
                preferred_time=preferred,
                reason="Callback closing spoken; booked by gateway",
            )
            self._reschedule_done = bool(isinstance(result, dict) and result.get("success"))
        except Exception:
            logger.exception(f"[CALLBACK_GUARD_ERROR] for {self.contact_id}")

    async def _handle_voicemail_detected(self):
        """
        Fires on beep detection OR voicemail-greeting-pattern match.
        Synthesizes the voicemail message directly via Polly, plays it
        through RTP bypassing Nova Sonic entirely, then forces terminate_call
        and hangup from the gateway side.
        """
        if self._voicemail_handled:
            return  # idempotent guard
        self._voicemail_handled = True

        asyncio.create_task(
            self._terminal_trigger_watchdog("VOICEMAIL", timeout_seconds=10.0)
        )

        logger.info(
            f"[VOICEMAIL_FORCED_CLOSE] Bypassing Nova Sonic stream, "
            f"synthesizing voicemail message directly for "
            f"{self.contact_id}"
        )

        try:
            voicemail_text = (
                self.patient.render_template(SCRIPT_DEFINITIONS.voicemail_message)
                if self.patient
                else SCRIPT_DEFINITIONS.voicemail_message
            )
            pcmu_bytes = await _synthesize_forced_closing_helper(voicemail_text)

            if self.on_forced_audio:
                await self.on_forced_audio(pcmu_bytes)

            self.final_disposition = "VOICEMAIL"
            if self.tool_executor:
                await self.tool_executor.terminate_call(
                    disposition="VOICEMAIL",
                    reason="No user speech detected or voicemail pattern matched",
                )
            self._disconnect_after_speaking = True

            # Wait for audio to drain through RTP before hanging up (8000 bytes/sec + margin)
            drain_delay = (len(pcmu_bytes) / 8000.0) + 0.3

            async def _delayed_hangup():
                await asyncio.sleep(drain_delay)
                await self._initiate_hangup()

            asyncio.create_task(_delayed_hangup())

        except Exception:
            logger.exception(
                f"[VOICEMAIL_FORCED_CLOSE_ERROR] Failed for "
                f"{self.contact_id} — forcing immediate hangup"
            )
            self._disconnect_after_speaking = True
            asyncio.create_task(self._initiate_hangup())

    async def _handle_identity_unconfirmed(self):
        """
        Fires when the caller gives a second unclear/mumbled reply
        after the double-check question was already asked once.

        Does NOT inject a message into Nova Sonic's stream (this
        deadlocks the bidirectional session — confirmed via
        production incident). Instead, synthesizes the closing line
        directly via Polly, plays it through RTP bypassing Nova
        Sonic entirely, then forces terminate_call and hangup from
        the gateway side.
        """
        if getattr(self, "_identity_closing_done", False):
            return  # idempotent guard
        self._identity_closing_done = True
        self._identity_escalation_triggered = True

        asyncio.create_task(
            self._terminal_trigger_watchdog("IDENTITY_UNCONFIRMED", timeout_seconds=10.0)
        )

        logger.info(
            f"[IDENTITY_UNCONFIRMED_FORCED_CLOSE] Bypassing Nova "
            f"Sonic stream, synthesizing closing directly for "
            f"{self.contact_id}"
        )

        try:
            closing_text = (
                self.patient.render_template(SCRIPT_DEFINITIONS.identity_unconfirmed_closing)
                if self.patient
                else SCRIPT_DEFINITIONS.identity_unconfirmed_closing
            )
            pcmu_bytes = await _synthesize_forced_closing_helper(closing_text)

            # Play directly through the RTP output, same mechanism
            # as the proven call-recording disclosure
            if self.on_forced_audio:
                await self.on_forced_audio(pcmu_bytes)

            # Force the disposition and disconnect from OUR side —
            # do not wait for or depend on any Nova Sonic tool call
            self.final_disposition = "IDENTITY_UNCONFIRMED"
            if self.tool_executor:
                await self.tool_executor.terminate_call(
                    disposition="IDENTITY_UNCONFIRMED",
                    reason="Caller gave unclear responses after double-check",
                )
            self._disconnect_after_speaking = True

            # Wait for audio to drain through RTP before hanging up (8000 bytes/sec + margin)
            drain_delay = (len(pcmu_bytes) / 8000.0) + 0.3

            async def _delayed_hangup():
                await asyncio.sleep(drain_delay)
                await self._initiate_hangup()

            asyncio.create_task(_delayed_hangup())

        except Exception:
            logger.exception(
                f"[IDENTITY_UNCONFIRMED_FORCED_CLOSE_ERROR] Failed "
                f"for {self.contact_id} — forcing immediate hangup "
                f"without spoken closing as last resort"
            )
            self._disconnect_after_speaking = True
            asyncio.create_task(self._initiate_hangup())

    async def _identity_double_check_watchdog(self, timeout_seconds: float = 7.0):
        try:
            await asyncio.sleep(timeout_seconds)
            if (
                self._double_check_spoken
                and not self._identity_escalation_triggered
                and self.is_active
            ):
                await self._register_identity_unclear_signal("timeout")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                f"[IDENTITY_WATCHDOG_ERROR] for {self.contact_id}"
            )

    async def _register_identity_unclear_signal(
        self, source: str, detail: str = ""
    ):
        """
        Called whenever the caller's response to the identity
        double-check is unclear — either because Nova Sonic
        transcribed something unclear (source='text'), or because
        the watchdog timed out with no transcript at all
        (source='timeout', e.g. pure non-lexical mumbling).

        Implements a 2-step reprompt ladder, matching standard
        industry IVR practice: first unclear signal gets a
        simplified yes/no reprompt and a second chance; only the
        second unclear signal in a row ends the call.
        """
        if self._identity_escalation_triggered:
            return  # already past this point, ignore further signals

        self._identity_unclear_count += 1
        logger.info(
            f"[IDENTITY_UNCLEAR] count={self._identity_unclear_count} "
            f"source={source} detail={detail!r} contact={self.contact_id}"
        )

        if self._identity_unclear_count >= 2:
            await self._handle_identity_unconfirmed()
            return

        # First unclear signal — give a second chance with a
        # simplified reprompt, spoken via the same proven Polly
        # forced-audio path (bypasses Nova Sonic's stream entirely,
        # so this cannot trigger the mid-stream deadlock).
        logger.info(
            f"[IDENTITY_REPROMPT] Playing simplified yes/no reprompt "
            f"for {self.contact_id} (1st unclear signal)"
        )
        try:
            reprompt_text = (
                self.patient.render_template(SCRIPT_DEFINITIONS.identity_simple_reprompt)
                if self.patient
                else SCRIPT_DEFINITIONS.identity_simple_reprompt
            )
            pcmu_bytes = await _synthesize_forced_closing_helper(reprompt_text)
            if self.on_forced_audio:
                await self.on_forced_audio(pcmu_bytes)
            logger.info(
                f"[IDENTITY_REPROMPT_PLAYED] {len(pcmu_bytes)} bytes "
                f"for {self.contact_id}"
            )
        except Exception:
            logger.exception(
                f"[IDENTITY_REPROMPT_ERROR] Failed to play reprompt "
                f"for {self.contact_id} — proceeding to watchdog "
                f"re-arm anyway so we still eventually escalate"
            )

        # Re-arm a fresh watchdog window for the second chance —
        # this is the only way to catch a second silent/non-lexical
        # response, same as the first window.
        if self._identity_double_check_watchdog_task:
            self._identity_double_check_watchdog_task.cancel()
        self._identity_double_check_watchdog_task = asyncio.create_task(
            self._identity_double_check_watchdog()
        )

    async def _voicemail_watchdog(self):
        """Voicemail silence detection fallback for GENERAL traffic."""
        await asyncio.sleep(10)
        if not self._user_spoke and self.is_active and not self._voicemail_handled:
            logger.info(
                f"[AMD_FALLBACK] No user speech in 10s — "
                f"treating as voicemail for {self.contact_id}"
            )
            await self._handle_voicemail_detected()

    async def _on_queue_drained(self):
        """Callback invoked when downstream RTP queue finishes draining audio frames."""
        if self._is_assistant_speaking:
            self._is_assistant_speaking = False
            logger.info(
                f"[NOVA_SILENT] Assistant queue drained "
                f"contact={self.contact_id}"
            )
        if self._disconnect_after_speaking:
            logger.info(
                f"[DISCONNECT_TRIGGERED] Queue drained after farewell — "
                f"initiating hangup for {self.contact_id}"
            )
            self._disconnect_after_speaking = False
            asyncio.create_task(self._initiate_hangup())

    async def _send_event(self, event_dict: Dict[str, Any]):
        """Serialize and transmit a single JSON event to Nova Sonic bidirectional stream."""
        if not self.stream or not self.stream.input_stream:
            return
        payload_bytes = json.dumps({"event": event_dict}).encode("utf-8")
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=payload_bytes)
        )
        await self.stream.input_stream.send(chunk)

    async def start(self):
        """Open bidirectional stream to Nova Sonic, transmit initialization headers, and start reader task."""
        try:
            logger.info(
                f"[NOVA_SONIC_START] Opening bidirectional stream to {self.model_id} for Contact {self.contact_id} "
                f"(Voice: {self.voice_id}, Region: {self.region_name})..."
            )
            http_transport = AWSCRTHTTPClient()
            
            # Resolve config with credentials
            aws_profile = os.getenv("AWS_PROFILE")
            if aws_profile:
                try:
                    config = await AsyncBedrockRuntimeConfig.resolve(
                        region=self.region_name,
                        profile=aws_profile,
                        transport=http_transport,
                    )
                except Exception:
                    config = await AsyncBedrockRuntimeConfig.resolve(
                        region=self.region_name,
                        transport=http_transport,
                    )
            else:
                config = await AsyncBedrockRuntimeConfig.resolve(
                    region=self.region_name,
                    transport=http_transport,
                )

            self.client = AsyncBedrockRuntimeClient(config=config)
            self.stream = await self.client.invoke_model_with_bidirectional_stream(
                InvokeModelWithBidirectionalStreamOperationInput(model_id=self.model_id)
            )
            self.is_active = True
            logger.info(f"[NOVA_SONIC_STREAM] Successfully established DuplexEventStream for Contact {self.contact_id}")

            # 1. Send sessionStart with inference and turnDetection configurations
            session_start_event = {
                "sessionStart": {
                    "inferenceConfiguration": {
                        "maxTokens": 1024,
                        "topP": 0.9,
                        "temperature": 0.7,
                    },
                    "turnDetectionConfiguration": {
                        "endpointingSensitivity": "HIGH",
                    },
                }
            }
            await self._send_event(session_start_event)
            logger.info(f"[NOVA_SONIC_EVT] sessionStart event sent for Contact {self.contact_id}")

            # 2. Format tools for Bedrock S2S toolConfiguration: inputSchema.json must be a JSON string
            formatted_tools = []
            for tool in TOOL_SCHEMAS:
                ts = tool.get("toolSpec", tool)
                schema_json = ts.get("inputSchema", {}).get("json", {})
                if isinstance(schema_json, dict):
                    schema_json_str = json.dumps(schema_json)
                else:
                    schema_json_str = str(schema_json)
                formatted_tools.append({
                    "toolSpec": {
                        "name": ts.get("name"),
                        "description": ts.get("description"),
                        "inputSchema": {"json": schema_json_str},
                    }
                })

            prompt_start_event = {
                "promptStart": {
                    "promptName": self.prompt_name,
                    "textOutputConfiguration": {
                        "mediaType": "text/plain",
                    },
                    "audioOutputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": 24000,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "voiceId": self.voice_id,
                        "encoding": "base64",
                        "audioType": "SPEECH",
                    },
                    "toolUseOutputConfiguration": {
                        "mediaType": "application/json",
                    },
                    "toolConfiguration": {
                        "tools": formatted_tools,
                        "toolChoice": {"auto": {}},
                    },
                }
            }
            await self._send_event(prompt_start_event)
            logger.info(f"[NOVA_SONIC_EVT] promptStart event sent for Contact {self.contact_id} (Prompt: {self.prompt_name}) with {len(formatted_tools)} registered tools")

            # 3. Send SYSTEM prompt (contentStart -> textInput -> contentEnd)
            await self._send_event({
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": self.system_content_name,
                    "role": "SYSTEM",
                    "type": "TEXT",
                    "interactive": False,
                    "textInputConfiguration": {
                        "mediaType": "text/plain",
                    },
                }
            })
            await self._send_event({
                "textInput": {
                    "promptName": self.prompt_name,
                    "contentName": self.system_content_name,
                    "content": self.system_prompt,
                }
            })
            await self._send_event({
                "contentEnd": {
                    "promptName": self.prompt_name,
                    "contentName": self.system_content_name,
                }
            })
            logger.info(f"[NOVA_SONIC_EVT] System prompt content block sent and closed for Contact {self.contact_id}")

            # 4. Open USER AUDIO content block (Keep OPEN for the entire call duration)
            await self._send_event({
                "contentStart": {
                    "promptName": self.prompt_name,
                    "contentName": self.audio_content_name,
                    "type": "AUDIO",
                    "interactive": True,
                    "role": "USER",
                    "audioInputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": 16000,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "audioType": "SPEECH",
                        "encoding": "base64",
                    },
                }
            })
            logger.info(
                f"[NOVA_SONIC_EVT] USER AUDIO contentStart opened for Contact {self.contact_id} "
                f"(Block: {self.audio_content_name}) — stream active"
            )

            # 5. Start response reader task as background asyncio.Task AFTER all init events are in the pipe
            self._reader_task = asyncio.create_task(self._read_responses())

            # 6. Return immediately — do NOT await _reader_task here

        except Exception as e:
            logger.error(f"[NOVA_SONIC_ERROR] Failed to start Nova Sonic session for Contact {self.contact_id}: {e}", exc_info=True)
            self.is_active = False

    async def push_caller_audio(self, pcm8k: bytes):
        """Resample inbound 8kHz PCM16 telephony frame to 16kHz and send as audioInput event to Nova Sonic.
        
        This is called continuously for every 20ms RTP frame, including while Nova Sonic is speaking.
        """
        if not self.is_active or not pcm8k or not self.stream:
            return
        if self._disconnect_after_speaking or self._transfer_pending:
            return  # stop feeding audio once farewell or transfer is committed

        # Beep detection — only matters if we haven't already confirmed voicemail this call
        if not self._beep_confirmed and self._beep_detector.feed_frame(pcm8k):
            self._beep_confirmed = True
            logger.info(
                f"[BEEP_DETECTED] Voicemail beep confirmed for "
                f"{self.contact_id} — triggering voicemail path"
            )
            asyncio.create_task(self._handle_voicemail_detected())
            return  # don't forward this frame to Nova Sonic

        try:
            pcm16k = resample_8k_to_16k(pcm8k)
            b64_audio = base64.b64encode(pcm16k).decode("ascii")

            audio_event = {
                "audioInput": {
                    "promptName": self.prompt_name,
                    "contentName": self.audio_content_name,
                    "content": b64_audio,
                }
            }
            await self._send_event(audio_event)

            self._audio_in_bytes += len(pcm8k)
            now = time.time()
            if now - self._last_counter_log >= 5.0:
                logger.info(
                    f"[AUDIO_METRICS] Contact {self.contact_id} | Total Audio Ingested: {self._audio_in_bytes} bytes "
                    f"| Total Audio Streamed Out: {self._audio_out_bytes} bytes"
                )
                self._last_counter_log = now

        except Exception as e:
            logger.error(f"[NOVA_SONIC_AUDIO_IN_ERR] Error pushing audio for Contact {self.contact_id}: {e}")

    async def _read_responses(self):
        """Asynchronously read and dispatch incoming events from Nova Sonic."""
        logger.info(f"[NOVA_SONIC_READER] Started response reader task for Contact {self.contact_id}")
        try:
            _, self.receiver = await self.stream.await_output()
            logger.info(f"[NOVA_SONIC_RECEIVER] Output receiver ready for Contact {self.contact_id}")
            async for output_event in self.receiver:
                if not self.is_active:
                    break

                try:
                    if not hasattr(output_event, "value") or not hasattr(output_event.value, "bytes_"):
                        continue

                    raw_bytes = output_event.value.bytes_
                    if not raw_bytes:
                        continue

                    parsed = json.loads(raw_bytes.decode("utf-8"))
                    evt_dict = parsed.get("event", parsed)

                    for event_type, event_data in evt_dict.items():
                        if event_type == "audioOutput":
                            if not self._is_assistant_speaking:
                                self._is_assistant_speaking = True
                                logger.info(f"[NOVA_SPEAKING] Assistant started speaking contact={self.contact_id}")

                            b64_content = event_data.get("content", "")
                            if b64_content:
                                pcm24k = base64.b64decode(b64_content)
                                pcm8k = resample_24k_to_8k(pcm24k)
                                self._audio_out_bytes += len(pcm8k)
                                logger.info(
                                    f"[AUDIO_OUT] Received {len(pcm24k)} bytes 24kHz -> resampled {len(pcm8k)} bytes 8kHz "
                                    f"for Contact {self.contact_id}"
                                )
                                if self.on_audio_out:
                                    await self.on_audio_out(pcm8k)

                        elif event_type == "textOutput":
                            role = event_data.get("role", "ASSISTANT")
                            text = event_data.get("content", "")

                            # Nova sends each assistant sentence twice: SPECULATIVE
                            # before its audio, FINAL after. Use only the SPECULATIVE
                            # copy so transcripts aren't doubled and closing detection
                            # isn't late (FINAL can land after the audio has drained).
                            if role.upper() == "ASSISTANT" and self._text_stage == "FINAL":
                                continue

                            if role.upper() == "ASSISTANT":
                                text = _strip_script_quotes(text)

                            if self.on_transcript:
                                await self.on_transcript(role, text)

                            # Voicemail silence detection tracking
                            if role.upper() == "ASSISTANT":
                                self._last_assistant_text = (
                                    self._last_assistant_text + " " + (text or "")
                                ).strip()
                                double_check_marker = "just to double check"
                                if double_check_marker in self._last_assistant_text.lower():
                                    self._double_check_spoken = True

                                if self._greeting_spoken_at is None:
                                    self._greeting_spoken_at = asyncio.get_event_loop().time()
                                    if os.getenv("TRAFFIC_TYPE", "GENERAL") == "GENERAL" and not self._watchdog_task:
                                        self._watchdog_task = asyncio.create_task(self._voicemail_watchdog())

                                # If disconnect is pending after terminate_call, check if spoken text matches
                                # a specific terminal disposition (covers model calling terminate_call before speaking)
                                if getattr(self, "_disconnect_after_speaking", False):
                                    inferred_post = infer_disposition_from_speech(self._last_assistant_text)
                                    if inferred_post and self.final_disposition != inferred_post:
                                        logger.info(
                                            f"[DISPOSITION_INFERRED] Using script-matched disposition={inferred_post} "
                                            f"for contact={self.contact_id} (updated during post-tool speech)"
                                        )
                                        self.final_disposition = inferred_post
                                        if hasattr(self, "tool_executor") and self.tool_executor:
                                            active_patient_id = self.tool_executor.patient_id or (
                                                self.tool_executor.patient.patient_id if self.tool_executor.patient else None
                                            )
                                            active_call_id = self.tool_executor.call_id or self.contact_id
                                            if active_patient_id and not active_patient_id.startswith("P-"):
                                                try:
                                                    self.tool_executor.storage.update_call_outcome(
                                                        patient_id=active_patient_id,
                                                        call_id=active_call_id,
                                                        call_status=inferred_post,
                                                    )
                                                    summary = format_call_summary(inferred_post, reason=inferred_post)
                                                    asyncio.create_task(
                                                        self.tool_executor.storage.mark_call_ended(active_patient_id, summary)
                                                    )
                                                    logger.info(
                                                        f"[TOOL_DB] terminate_call updated patient_id={active_patient_id} status={inferred_post} call_id={active_call_id}"
                                                    )
                                                except Exception as ex_db:
                                                    logger.error(f"[TOOL_DB_ERROR] Failed to update outcome post-speech: {ex_db}")
                            elif role.upper() == "USER":
                                self._last_user_text = text or self._last_user_text
                                if self.final_disposition == "NO_ENGAGEMENT":
                                    self.final_disposition = "PATIENT_HANGUP"
                                if self.death_cue_text is None and _has_death_cue(text):
                                    self.death_cue_text = text
                                    logger.info(
                                        f"[DEATH_CUE] Caller may have said the patient died "
                                        f"for {self.contact_id}: {text!r}"
                                    )
                                transcript_lower = (text or "").lower()
                                looks_like_voicemail_greeting = any(
                                    pat in transcript_lower for pat in VOICEMAIL_GREETING_PATTERNS
                                )
                                if looks_like_voicemail_greeting:
                                    logger.info(
                                        f"[VM_GREETING_PATTERN] Detected voicemail greeting "
                                        f"phrase in transcript for {self.contact_id}: "
                                        f"'{text[:80]}' — NOT disarming watchdog"
                                    )
                                    self._beep_confirmed = True  # also treat as VM signal
                                    asyncio.create_task(self._handle_voicemail_detected())
                                elif (
                                    self._double_check_spoken
                                    and not self._identity_escalation_triggered
                                    and _is_unclear_reply(text)
                                ):
                                    asyncio.create_task(
                                        self._register_identity_unclear_signal("text", text)
                                    )
                                    return  # don't process this reply through normal flow
                                else:
                                    self._user_spoke = True
                                    if (
                                        self._double_check_spoken
                                        and self._identity_double_check_watchdog_task
                                        and not self._identity_double_check_watchdog_task.done()
                                    ):
                                        self._identity_double_check_watchdog_task.cancel()
                                        self._identity_unclear_count = 0
                                        logger.info(
                                            f"[IDENTITY_WATCHDOG_DISARMED] Clear response received "
                                            f"for {self.contact_id}: {text!r}"
                                        )

                        elif event_type == "toolUse":
                            tool_use_id = event_data.get("toolUseId") or event_data.get("id") or ""
                            tool_name = (
                                event_data.get("toolName")
                                or event_data.get("name")
                                or ""
                            )
                            tool_input = _parse_tool_input(event_data)

                            logger.info(
                                f"[TOOL_USE] Contact {self.contact_id} tool={tool_name} id={tool_use_id} input={tool_input}"
                            )

                            _tool_map = {
                                "evaluate_and_transfer_pes": self.tool_executor.evaluate_and_transfer_pes,
                                "reschedule_call": self.tool_executor.reschedule_call,
                                "terminate_call": self.tool_executor.terminate_call,
                            }

                            if tool_name == "terminate_call":
                                tool_input = dict(tool_input)  # don't mutate the original event
                                tool_input["last_spoken_text"] = (
                                    self._last_completed_assistant_turn or self._last_assistant_text
                                )
                                tool_input["_original_disposition"] = tool_input.get("disposition", "")

                            executor_fn = _tool_map.get(tool_name)
                            if executor_fn:
                                try:
                                    result = await executor_fn(**tool_input)
                                except Exception as ex:
                                    logger.error(f"[TOOL_EXEC_ERROR] Error executing {tool_name}: {ex}")
                                    result = {"success": False, "error": str(ex)}
                            else:
                                logger.warning(f"[TOOL_UNKNOWN] Unknown tool {tool_name}")
                                result = {"success": False, "error": f"Unknown tool {tool_name}"}

                            if (
                                tool_name == "evaluate_and_transfer_pes"
                                and isinstance(result, dict)
                                and result.get("status") == "transferring"
                                and result.get("agent_phone")
                                and self.on_transfer
                                and not self._transfer_pending
                            ):
                                # Hold the tool result until we know whether the
                                # agent picked up; Cara already said "please hold".
                                self._transfer_pending = True
                                asyncio.create_task(
                                    self._run_transfer(tool_use_id, result["agent_phone"])
                                )
                                continue

                            if (
                                tool_name == "evaluate_and_transfer_pes"
                                and isinstance(result, dict)
                                and result.get("status") == "unavailable"
                                and self._take_direct_fallback()
                            ):
                                result = dict(result, **_SWITCH_TO_NORMAL_RESULT)

                            await self._send_tool_result(tool_use_id, tool_name, result)

                            if tool_name == "terminate_call" or (
                                isinstance(result, dict) and result.get("action") == "DISCONNECT"
                            ):
                                dispo = (isinstance(result, dict) and result.get("disposition")) or "COMPLETED"
                                self.final_disposition = dispo
                                logger.info(
                                    f"[DISCONNECT_PENDING] terminate_call result received (disposition={dispo}) — "
                                    f"will disconnect after audio drains for {self.contact_id}"
                                )
                                self._disconnect_after_speaking = True

                            elif tool_name == "reschedule_call" and (
                                isinstance(result, dict) and result.get("success")
                            ):
                                self.final_disposition = "RESCHEDULED"
                                self._reschedule_done = True
                                logger.info(
                                    f"[DISCONNECT_PENDING] reschedule_call succeeded — "
                                    f"will disconnect after farewell audio drains for {self.contact_id}"
                                )
                                self._disconnect_after_speaking = True


                        elif event_type == "contentStart":
                            if event_data.get("type") == "TEXT":
                                self._text_stage = _parse_generation_stage(event_data)

                        elif event_type == "contentEnd":
                            stop_reason = event_data.get("stopReason")
                            logger.info(f"[CONTENT_END] Contact {self.contact_id} | stopReason: {stop_reason}")
                            # A FINAL text block's END_TURN just closes the late duplicate
                            # copy; the real turn boundary is the audio block's END_TURN.
                            if stop_reason == "END_TURN" and self._text_stage == "FINAL":
                                continue
                            if stop_reason == "END_TURN" and _is_closing_turn(self._last_assistant_text):
                                await self._arm_closing_hangup(self._last_assistant_text)
                            if stop_reason in ("END_TURN", "TOOL_USE", "INTERRUPTED"):
                                self._last_completed_assistant_turn = self._last_assistant_text
                                self._last_assistant_text = ""
                                double_check_marker = "just to double check"
                                if double_check_marker in self._last_completed_assistant_turn.lower():
                                    self._double_check_spoken = True
                                    if self._identity_double_check_watchdog_task:
                                        self._identity_double_check_watchdog_task.cancel()
                                    self._identity_double_check_watchdog_task = asyncio.create_task(
                                        self._identity_double_check_watchdog()
                                    )
                            if stop_reason == "INTERRUPTED":
                                if self._is_assistant_speaking:
                                    self._is_assistant_speaking = False
                                    logger.info(f"[NOVA_SILENT] Assistant interrupted contact={self.contact_id}")
                                logger.info(
                                    f"[BARGE_IN] Nova Sonic reported user interruption (INTERRUPTED) for Contact {self.contact_id} "
                                    f"— triggering immediate queue purge (<40ms cutoff)"
                                )
                                if self.on_interrupt:
                                    await self.on_interrupt()

                        elif event_type == "completionEnd":
                            logger.info(f"[COMPLETION_END] Turn completion received for Contact {self.contact_id}")

                        else:
                            logger.debug(f"[NOVA_SONIC_OTHER_EVT] Contact {self.contact_id} | Type: {event_type} | Data: {event_data}")

                except Exception as e:
                    logger.error(f"[READER_EVENT_ERROR] contact={self.contact_id} error={e} event={output_event}", exc_info=True)

        except asyncio.CancelledError:
            logger.info(f"[NOVA_SONIC_READER] Reader task cancelled for Contact {self.contact_id}")
        except Exception as e:
            logger.error(f"[NOVA_SONIC_READER_CRASH] Reader task crashed for Contact {self.contact_id}: {e}", exc_info=True)
            # is_active is already False when end() closed the stream on purpose
            stream_died_mid_call = self.is_active
            self.is_active = False
            if stream_died_mid_call:
                await self._recover_from_stream_error()

    async def _recover_from_stream_error(self):
        """Never leave the patient on a silent line after the model stream dies.

        Nova Sonic sometimes rejects a new stream right after the system
        prompt ("The system encountered an unexpected error"). Before Cara has
        said anything, reopen the stream a couple of times; otherwise (or once
        retries run out) hang up so the call is retried instead of sitting in
        silence until the patient gives up.
        """
        if self._disconnect_after_speaking:
            return
        if self._audio_out_bytes == 0 and self._stream_restarts < MAX_STREAM_RESTARTS:
            self._stream_restarts += 1
            logger.warning(
                f"[NOVA_SONIC_RESTART] Reopening stream for {self.contact_id} "
                f"(attempt {self._stream_restarts}/{MAX_STREAM_RESTARTS})"
            )
            await self._close_stream_quietly()
            await asyncio.sleep(STREAM_RESTART_DELAY_SECONDS)
            self.prompt_name = str(uuid.uuid4())
            self.system_content_name = str(uuid.uuid4())
            self.audio_content_name = str(uuid.uuid4())
            await self.start()
            if self.is_active or self._disconnect_after_speaking:
                return  # reopened, or a later recovery already hung up
        logger.error(
            f"[NOVA_SONIC_DEAD] Model stream unavailable for {self.contact_id} "
            f"after {self._stream_restarts} restart(s) — hanging up instead of holding a silent line"
        )
        self._disconnect_after_speaking = True
        if self.on_hangup:
            try:
                await self.on_hangup()
            except Exception:
                logger.exception(f"[HANGUP_ERROR] Error during hangup for {self.contact_id}")

    async def _close_stream_quietly(self):
        try:
            if self.stream and self.stream.input_stream:
                await self.stream.input_stream.close()
        except Exception:
            pass
        self.stream = None
        self.receiver = None

    async def end(self):
        """Gracefully close the Nova Sonic session and terminate background streaming tasks."""
        if not self.is_active:
            return
        self.is_active = False
        self._is_assistant_speaking = False
        if self._identity_double_check_watchdog_task:
            self._identity_double_check_watchdog_task.cancel()
        logger.info(f"[NOVA_SONIC_END] Terminating session for Contact {self.contact_id}...")

        try:
            # 1. Close USER AUDIO content block
            await self._send_event({
                "contentEnd": {
                    "promptName": self.prompt_name,
                    "contentName": self.audio_content_name,
                }
            })
            # 2. Close prompt
            await self._send_event({
                "promptEnd": {
                    "promptName": self.prompt_name,
                }
            })
            # 3. Close session
            await self._send_event({
                "sessionEnd": {}
            })
        except Exception as e:
            logger.debug(f"Note during Nova Sonic teardown events for {self.contact_id}: {e}")

        # Cancel reader and watchdog tasks
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()

        # Close input stream
        try:
            if self.stream and self.stream.input_stream:
                await self.stream.input_stream.close()
        except Exception:
            pass

        logger.info(f"[NOVA_SONIC_END] Teardown complete for Contact {self.contact_id}")


# ==============================================================================
# PIPECAT FRAME PROCESSOR: NOVA SONIC LLM SERVICE
# ==============================================================================

class NovaSonicLLMService(FrameProcessor):
    """Real-time Speech-to-Speech FrameProcessor interfacing with Bedrock Amazon Nova 2 Sonic."""

    def __init__(
        self,
        patient_context: PatientContext,
        model_id: str = "amazon.nova-2-sonic-v1:0",
        endpointing_sensitivity: str = "HIGH",
        voice: str = "ruth",
        contact_id: Optional[str] = None,
        on_hangup: Optional[Callable[[], Any]] = None,
        on_forced_audio: Optional[Callable[[bytes], Any]] = None,
        on_transfer: Optional[Callable[[str], Any]] = None,
        tool_executor: Optional[OutreachToolExecutor] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.patient = patient_context
        self.model_id = model_id
        self.endpointing_sensitivity = endpointing_sensitivity
        self.voice = voice or os.getenv("NOVA_SONIC_VOICE", "ruth")
        self.contact_id = contact_id or f"P-{patient_context.patient_id}"
        self.system_prompt = generate_nova_sonic_system_prompt(self.patient)
        self.on_hangup = on_hangup
        self.on_forced_audio = on_forced_audio
        self.on_transfer = on_transfer
        self.tool_executor = tool_executor
        self.session: Optional[NovaSonicSession] = None
        self._is_running = True

    async def on_queue_drained(self):
        """Forward queue drained event to active session."""
        if self.session:
            await self.session._on_queue_drained()

    async def _handle_audio_out(self, pcm_8k: bytes):
        """Callback from NovaSonicSession delivering synthesized speech."""
        out_frame = AudioRawFrame(audio=pcm_8k, sample_rate=8000, num_channels=1)
        await self.push_frame(out_frame, FrameDirection.DOWNSTREAM)

    async def _handle_interrupt(self):
        """Callback from NovaSonicSession when user barge-in is detected by Nova Sonic."""
        logger.info(f"[INTERRUPT] User interruption detected for Contact {self.contact_id}; emitting InterruptionFrame")
        await self.push_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    async def _handle_transcript(self, role: str, text: str):
        """Callback from NovaSonicSession delivering real-time ASR & model transcript."""
        logger.info(f"[TRANSCRIPT] Contact {self.contact_id} | Role: {role} | Content: {text}")
        await self.push_frame(TextFrame(text=text), FrameDirection.DOWNSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        try:
            await super().process_frame(frame, direction)
        except Exception:
            pass

        if isinstance(frame, StartFrame):
            self._is_running = True
            self.session = NovaSonicSession(
                model_id=self.model_id,
                system_prompt=self.system_prompt,
                contact_id=self.contact_id,
                patient_context=self.patient,
                on_audio_out=self._handle_audio_out,
                on_interrupt=self._handle_interrupt,
                on_transcript=self._handle_transcript,
                on_hangup=self.on_hangup,
                on_forced_audio=self.on_forced_audio,
                on_transfer=self.on_transfer,
                tool_executor=self.tool_executor,
                voice_id=self.voice,
            )
            await self.session.start()
            logger.info(f"Initialized Nova Sonic session for patient {self.patient.patient_id} (Contact: {self.contact_id})")
            await self.push_frame(frame, direction)

        elif isinstance(frame, AudioRawFrame):
            # Stream incoming caller audio directly and continuously into Nova Sonic S2S
            if self.session and self.session.is_active:
                await self.session.push_caller_audio(frame.audio)
            # Note: Do NOT push caller's mic audio downstream to the outbound speaker (prevents caller echo)

        elif isinstance(frame, InterruptionFrame):
            if self.session:
                await self.session.on_interrupt()
            await self.push_frame(frame, direction)

        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._is_running = False
            if self.session:
                await self.session.end()
            await self.push_frame(frame, direction)

        else:
            await self.push_frame(frame, direction)

    def get_session_config(self) -> Dict[str, Any]:
        """Return runtime session configuration dictionary for Bedrock Nova Sonic."""
        return {
            "model_id": self.model_id,
            "modelId": self.model_id,
            "voice": self.voice,
            "endpointing_sensitivity": self.endpointing_sensitivity,
            "contact_id": self.contact_id,
            "patient_id": self.patient.patient_id,
            "systemPrompt": self.system_prompt,
            "sessionSettings": {
                "endpointingSensitivity": self.endpointing_sensitivity,
                "voice": self.voice,
            },
        }
