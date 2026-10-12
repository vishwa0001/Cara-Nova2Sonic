"""Pipecat Conversational Tool Handlers for CareHarmony Outreach IVR.

Implements the core conversational tools:
1. evaluate_and_transfer_pes
2. reschedule_call
3. terminate_call
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ProfileNotFound

import re
from datetime import datetime, timedelta, timezone

try:
    from gateway.reschedule_time import names_a_time, parse_reschedule_time
except ImportError:
    from backend.gateway.reschedule_time import names_a_time, parse_reschedule_time

try:
    from domain_rules import (
        CallDisposition, CallMode, PatientContext,
        SCRIPT_DEFINITIONS, SAFETY_MEDICAL, SAFETY_BEHAVIORAL,
    )
except ImportError:
    from backend.domain_rules import (
        CallDisposition, CallMode, PatientContext,
        SCRIPT_DEFINITIONS, SAFETY_MEDICAL, SAFETY_BEHAVIORAL,
    )

try:
    from storage.outreach_records import OutreachRecordService, format_call_summary
except ImportError:
    from backend.storage.outreach_records import OutreachRecordService, format_call_summary

logger = logging.getLogger("gateway_tools")

# Map: a distinctive substring from each terminal script line
# to its correct disposition. Use short, distinctive fragments
# (not full lines) so matching is robust to minor ASR/TTS
# variation, punctuation differences, or partial chunk capture.
DISPOSITION_SCRIPT_MARKERS = [
    ("please accept my condolences", "DECEASED"),
    ("i'll make sure our records reflect this", "DECEASED"),
    ("stop reaching out to this number", "WRONG_NUMBER"),
    ("we stop reaching out to this number", "WRONG_NUMBER"),
    ("i will make sure we do not call again", "DO_NOT_CALL"),
    ("understood — thanks for your time today", "REFUSED"),
    ("understood - thanks for your time today", "REFUSED"),
    ("thanks for your time today", "REFUSED"),
    ("dial 911 right away", "SAFETY_MEDICAL"),
    ("suicide & crisis lifeline", "SAFETY_BEHAVIORAL"),
    ("suicide and crisis lifeline", "SAFETY_BEHAVIORAL"),
    ("we'll try again soon, or you're welcome to call us back", "VOICEMAIL"),
    ("i'll need to speak directly with", "IDENTITY_UNCONFIRMED"),
    ("try reaching them another time", "IDENTITY_UNCONFIRMED"),
]

def _phone_digits(phone: Optional[str]) -> str:
    """Last 10 digits, so "+1 252-500-0184" and "2525000184" compare equal."""
    return re.sub(r"\D", "", phone or "")[-10:]


def _pick_agent_phone(candidates: List[Optional[str]], patient_phone: Optional[str]) -> Optional[str]:
    """First agent number that isn't the patient's own phone. That phone is
    busy with Cara's call, so the agent leg just rings out (call 9af65a8d)."""
    patient = _phone_digits(patient_phone)
    for phone in candidates:
        if phone and (not patient or _phone_digits(phone) != patient):
            return phone
    return None


def infer_disposition_from_speech(last_spoken_text: str) -> Optional[str]:
    """
    Matches the last thing Cara actually said against known
    terminal script lines to infer the correct disposition,
    independent of whatever argument (if any) Nova Sonic passed
    to terminate_call. Returns None if no match found — caller
    should fall back to whatever Nova Sonic provided, or
    COMPLETED as a last resort.
    """
    if not last_spoken_text:
        return None
    normalized = last_spoken_text.lower()
    for marker, disposition in DISPOSITION_SCRIPT_MARKERS:
        if marker in normalized:
            return disposition
    return None

# Bedrock Nova Sonic Compatible Tool Schemas
TOOL_SCHEMAS = [
    {
        "toolSpec": {
            "name": "evaluate_and_transfer_pes",
            "description": (
                "Check agent availability and transfer the caller to a live PES agent. "
                "Call this when the patient agrees to be connected or when call mode is MODE_1_DIRECT_HANDOFF."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "call_summary": {
                            "type": "string",
                            "description": "Brief summary of the conversation so far.",
                        }
                    },
                    "required": [],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "reschedule_call",
            "description": (
                "Schedule a callback for this patient. Pass the patient's exact words for timing if they gave one "
                "(e.g. 'tomorrow at noon', 'in an hour', 'next week', 'in 10 minutes', 'call me back in a month') in "
                "preferred_time. If they gave no specific timing preference, pass preferred_time='unspecified' — the "
                "system will default to 24 hours from now."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "preferred_time": {
                            "type": "string",
                            "description": "Patient's exact words about timing, verbatim. Use 'unspecified' if they gave no preference at all.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why the call is being rescheduled.",
                        },
                    },
                    "required": ["preferred_time", "reason"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "terminate_call",
            "description": (
                "End the call immediately with a specific disposition. You MUST always pass the disposition "
                "parameter — never omit it. Use the disposition that matches what you just said to the caller."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "disposition": {
                            "type": "string",
                            "enum": [
                                "REFUSED",
                                "DO_NOT_CALL",
                                "SAFETY_MEDICAL",
                                "SAFETY_BEHAVIORAL",
                                "WRONG_NUMBER",
                                "DECEASED",
                                "COMPLETED",
                                "VOICEMAIL",
                                "IDENTITY_UNCONFIRMED",
                            ],
                            "description": "REQUIRED. The exact outcome of this call, matching the script branch you just spoke.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Optional short context.",
                        },
                    },
                    "required": ["disposition"],
                }
            },
        }
    },
]


def get_connect_client(profile_name: str = "careharmony", region_name: str = "us-east-1") -> Any:
    explicit_profile = os.environ.get("AWS_PROFILE", profile_name)
    try:
        session = boto3.Session(profile_name=explicit_profile, region_name=region_name)
        session.get_credentials()
        return session.client("connect")
    except (ProfileNotFound, Exception):
        return boto3.Session(region_name=region_name).client("connect")


class OutreachToolExecutor:
    """Executes Pipecat conversational tools during live Nova 2 Sonic turns."""

    def __init__(
        self,
        api_base_url: Optional[str] = None,
        connect_instance_id: Optional[str] = None,
        storage_service: Optional[OutreachRecordService] = None,
        connect_client: Any = None,
        patient_context: Optional[PatientContext] = None,
        contact_id: Optional[str] = None,
        is_sip_call: bool = False,
    ):
        self.api_base_url = api_base_url or os.environ.get("MOCK_API_BASE_URL", "http://127.0.0.1:8000")
        self.connect_instance_id = connect_instance_id or os.environ.get(
            "CONNECT_INSTANCE_ID", "8e7c7423-5e98-44ab-a748-b3ca0bd7a896"
        )
        self.storage = storage_service or OutreachRecordService()
        self.connect = connect_client or get_connect_client()
        self.patient = patient_context
        self.patient_id = (patient_context.patient_id if patient_context else None) or contact_id
        self.patient_id_resolved = bool(
            self.patient_id and not self.patient_id.startswith("P-") and self.patient_id != contact_id
        )
        self.contact_id = contact_id
        self.call_id = contact_id
        # True for calls bridged over Chime SIP/RTP, where no Amazon Connect
        # contact exists — update_contact_attributes always fails there, so
        # callers of this executor skip it rather than wait for it to error.
        self.is_sip_call = is_sip_call
        # Holds references to fire-and-forget background tasks (webhook
        # dispatch, DynamoDB writes) so they aren't garbage-collected mid-flight.
        self._background_tasks: set = set()

    def _fire_and_forget(self, coro) -> None:
        """Schedule `coro` without blocking the caller, keeping a reference
        alive until it completes so the task can't be GC'd mid-run."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def evaluate_and_transfer_pes(
        self,
        call_summary: str = "Patient consented to live specialist transfer.",
        call_id: Optional[str] = None,
        patient_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        call_mode: Optional[str] = None,
        patient_name: Optional[str] = None,
        patient_phone: Optional[str] = None,
        empi: Optional[str] = None,
        transcript: Optional[List[Dict[str, str]]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Check live agent availability; if available, link in DynamoDB, notify webhook, and route to PES."""
        import httpx

        active_call_id = call_id or self.contact_id or "call-001"
        active_patient_id = patient_id or (self.patient.patient_id if self.patient else "P-001")
        active_campaign_id = campaign_id or "CAMPAIGN-001"
        active_call_mode = call_mode or (self.patient.call_mode.value if self.patient else "normal_coaching")
        active_patient_name = patient_name or (self.patient.full_name if self.patient else "Valued Patient")
        active_patient_phone = patient_phone or (self.patient.phone_number if self.patient else "+18145550123")
        active_empi = empi or f"EMPI-{active_patient_id}"

        now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Call GET /api/v1/agents/availability with SLA < 500ms
        avail_url = f"{self.api_base_url.rstrip('/')}/api/v1/agents/availability"
        params = {"callId": active_call_id, "campaignId": active_campaign_id}

        try:
            async with httpx.AsyncClient(timeout=0.5) as client:
                resp = await client.get(avail_url, params=params)
                avail_data = resp.json() if resp.status_code == 200 else {"available": False}
        except Exception as e:
            logger.warning(f"Agent availability request error ({e}); using fallback.")
            avail_data = {"available": False}

        is_available = bool(avail_data.get("available"))
        agent_phone = None
        if is_available:
            # UI "Human Agent Phone" > availability API agentPhone > FIXED_AGENT_PHONE
            agent_phone = _pick_agent_phone(
                [
                    self.patient.human_agent_phone if self.patient else "",
                    avail_data.get("agentPhone"),
                    os.environ.get("FIXED_AGENT_PHONE", "+15822671755"),
                ],
                patient_phone=active_patient_phone,
            )
            if not agent_phone:
                logger.warning(
                    f"[TRANSFER_SAME_PHONE] Every agent number is the patient's own phone for {active_call_id}; "
                    f"not dialing it"
                )

        # 2. Branch: Available
        if agent_phone:
            agent_id = avail_data.get("agentId", "agent-001")
            agent_name = avail_data.get("agentName", "Sarah Jenkins")
            assigned_agent = {
                "agentId": agent_id,
                "agentName": agent_name,
                "agentPhone": agent_phone,
                "checkedAt": avail_data.get("checkedAt", now_iso),
            }

            # A. Persist assignedAgent to DynamoDB CareHarmony_OutreachRecords.
            # Fired off the event loop (boto3 is sync) and not awaited — the
            # caller already hears "please hold" while this resolves.
            self._fire_and_forget(
                asyncio.to_thread(self.storage.link_assigned_agent, active_patient_id, assigned_agent)
            )

            # B. Dispatch POST /api/v1/webhooks/call-handoff.
            # Fired in the background too: this call alone was ~650ms and
            # blocked the turn before "please hold" bought us that time.
            webhook_url = f"{self.api_base_url.rstrip('/')}/api/v1/webhooks/call-handoff"
            handoff_body = {
                "callId": active_call_id,
                "patientId": active_patient_id,
                "empi": active_empi,
                "patientName": active_patient_name,
                "patientPhone": active_patient_phone,
                "campaignId": active_campaign_id,
                "transferReason": "CONSENT_GRANTED",
                "callMode": active_call_mode,
                "transferredAt": now_iso,
                "assignedAgent": assigned_agent,
                "callSummary": call_summary,
                "transcript": transcript or [
                    {"speaker": "system", "text": "Transfer initiated", "timestamp": now_iso}
                ],
            }

            async def _dispatch_handoff_webhook() -> None:
                try:
                    async with httpx.AsyncClient(timeout=2.0) as client:
                        await client.post(webhook_url, json=handoff_body)
                except Exception as w_err:
                    logger.warning(f"Handoff webhook dispatch failed: {w_err}")

            self._fire_and_forget(_dispatch_handoff_webhook())

            # C. Trigger Amazon Connect Contact Attribute update for telephony
            # transfer bridge. Skipped for SIP-originated calls: there's no
            # Connect contact behind them, so this call always fails and is
            # pure dead weight — not worth waiting on it to error out.
            if not self.is_sip_call:
                try:
                    self.connect.update_contact_attributes(
                        InstanceId=self.connect_instance_id,
                        InitialContactId=active_call_id,
                        Attributes={
                            "RouteAction": "TRANSFER_PES",
                            "TargetPhoneNumber": agent_phone,
                            "AssignedAgentId": agent_id,
                            "AssignedAgentName": agent_name,
                        },
                    )
                    logger.info(f"Updated Connect contact {active_call_id} attributes with RouteAction=TRANSFER_PES")
                except Exception as c_err:
                    logger.debug(f"Connect update_contact_attributes simulated/offline: {c_err}")

            return {
                "success": True,
                "status": "transferring",
                "message": "Connecting you now.",
                "agent_phone": agent_phone,
                "agent_name": agent_name,
            }

        # 3. Branch: Unavailable Fallback
        return {
            "success": True,
            "status": "unavailable",
            "message": "No agent available at this time.",
            "agent_phone": None,
        }

    async def reschedule_call(
        self,
        preferred_time: str = "unspecified",
        reason: str = "",
        patient_id: Optional[str] = None,
        call_id: Optional[str] = None,
        requested_time: Optional[str] = None,
        caller_words: str = "",
        **kwargs,
    ) -> Dict[str, Any]:
        """Handle patient callback agreement, increment reschedule_count, and persist next scheduled_time."""
        target_time = requested_time or preferred_time
        # Nova rewords the request: "day after tomorrow" arrived as "after
        # tomorrow" (call 8e29df6f). The caller's own words win when they name a time.
        if caller_words and names_a_time(caller_words) and caller_words.strip() != (target_time or "").strip():
            logger.info(f"[RESCHEDULE] Using the caller's words {caller_words!r} over the tool's {target_time!r}")
            target_time = caller_words
        scheduled_dt = parse_reschedule_time(
            target_time, tz_name=(self.patient.timezone if self.patient else None)
        )
        scheduled_iso = scheduled_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        active_patient_id = patient_id or self.patient_id or (self.patient.patient_id if self.patient else "P-001")
        active_call_id = call_id or self.call_id or self.contact_id

        logger.info(
            f"[RESCHEDULE] patient_id={active_patient_id} "
            f"preferred_time='{target_time}' -> "
            f"scheduled_time={scheduled_iso}"
        )

        if not active_patient_id or active_patient_id.startswith("P-"):
            logger.error(
                f"[RESCHEDULE_ERROR] Cannot update DynamoDB — patient_id not set correctly: {active_patient_id}"
            )
            return {
                "success": False,
                "error": "patient_id not resolved"
            }

        try:
            update_result = self.storage.update_reschedule(
                active_patient_id, scheduled_iso
            )
            reschedule_count = update_result.get("reschedule_count", 1)
            status = update_result.get("call_status", CallDisposition.RESCHEDULED.value if hasattr(CallDisposition.RESCHEDULED, "value") else "RESCHEDULED")

            summary = format_call_summary("RESCHEDULED")
            await self.storage.mark_call_ended(active_patient_id, summary)

            # Skipped for SIP-originated calls: no Connect contact exists
            # behind them, so this call always fails and just adds latency.
            if active_call_id and not self.is_sip_call:
                try:
                    self.connect.update_contact_attributes(
                        InstanceId=self.connect_instance_id,
                        InitialContactId=active_call_id,
                        Attributes={
                            "RouteAction": "END_CALLBACK",
                            "RescheduleCount": str(reschedule_count),
                            "ScheduledTime": str(scheduled_iso),
                        },
                    )
                except Exception as c_err:
                    logger.debug(f"Connect update_contact_attributes simulated/offline: {c_err}")

            return {
                "success": True,
                "scheduled_time": scheduled_iso,
                "reschedule_count": reschedule_count,
                "call_status": status,
                "message": f"Rescheduled for {scheduled_iso}"
            }
        except Exception as e:
            logger.exception(
                f"[RESCHEDULE_ERROR] DynamoDB update failed for "
                f"{active_patient_id}"
            )
            return {
                "success": False,
                "error": str(e)
            }

    async def terminate_call(
        self,
        disposition: str = "",
        reason: str = "",
        call_id: Optional[str] = None,
        patient_id: Optional[str] = None,
        protocol_type: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """End the call immediately for safety escalations, DNC, or clean conversation completion."""
        inferred = infer_disposition_from_speech(kwargs.get("last_spoken_text", ""))

        if inferred:
            if disposition and disposition != inferred:
                logger.warning(
                    f"[DISPOSITION_MISMATCH] Nova Sonic passed "
                    f"disposition={disposition!r} but spoken script "
                    f"matched {inferred!r} — using inferred value as "
                    f"more reliable. contact={self.contact_id}"
                )
            disposition = inferred
            logger.info(
                f"[DISPOSITION_INFERRED] Using script-matched "
                f"disposition={disposition} for contact={self.contact_id} "
                f"(Nova Sonic argument was {'empty' if not kwargs.get('_original_disposition') else 'present'})"
            )
        elif not disposition:
            logger.error(
                f"[TERMINATE_CALL_ERROR] disposition missing/empty AND "
                f"no script match found for contact={self.contact_id} "
                f"— last_spoken='{kwargs.get('last_spoken_text', '')[:100]}' "
                f"— falling back to COMPLETED"
            )
            disposition = "COMPLETED"

        active_call_id = call_id or kwargs.get("call_id") or self.call_id or self.contact_id
        active_patient_id = patient_id or kwargs.get("patient_id") or self.patient_id or (self.patient.patient_id if self.patient else "P-001")

        final_disposition = disposition
        if protocol_type:
            final_disposition = (
                CallDisposition.SAFETY_MEDICAL.value
                if "medical" in protocol_type.lower()
                else CallDisposition.SAFETY_BEHAVIORAL.value
            )
            reason = reason or protocol_type

        STATUS_MAP = {
            "REFUSED": "REFUSED",
            "DO_NOT_CALL": "DO_NOT_CALL",
            "SAFETY_MEDICAL": "SAFETY_MEDICAL",
            "SAFETY_BEHAVIORAL": "SAFETY_BEHAVIORAL",
            "WRONG_NUMBER": "WRONG_NUMBER",
            "DECEASED": "DECEASED",
            "COMPLETED": "COMPLETED",
            "VOICEMAIL": "VOICEMAIL",
            "IDENTITY_UNCONFIRMED": "IDENTITY_UNCONFIRMED",
        }
        db_status = STATUS_MAP.get(final_disposition, "COMPLETED")

        # Direct DynamoDB update
        if active_patient_id and not active_patient_id.startswith("P-"):
            try:
                self.storage.update_call_outcome(
                    patient_id=active_patient_id,
                    call_id=active_call_id,
                    call_status=db_status,
                )
                summary = format_call_summary(db_status, reason=disposition)
                await self.storage.mark_call_ended(active_patient_id, summary)
                logger.info(
                    f"[TOOL_DB] terminate_call updated patient_id={active_patient_id} status={db_status} call_id={active_call_id}"
                )
            except Exception:
                logger.exception(
                    f"[TOOL_DB_ERROR] terminate_call DynamoDB update failed for {active_patient_id}"
                )

        try:
            if active_call_id:
                self.connect.update_contact_attributes(
                    InstanceId=self.connect_instance_id,
                    InitialContactId=active_call_id,
                    Attributes={
                        "RouteAction": "DISCONNECT",
                        "SafetyDisposition": final_disposition,
                        "CaraEndReason": final_disposition,
                        "TerminationReason": reason,
                    },
                )
        except Exception as c_err:
            logger.debug(f"Connect update_contact_attributes simulated/offline: {c_err}")

        return {
            "success": True,
            "action": "DISCONNECT",
            "disposition": final_disposition,
            "reason": reason,
            "message": "Call terminated.",
        }

    # Backward compatibility aliases
    schedule_callback = reschedule_call
    emergency_disconnect = terminate_call


# =====================================================================
# Module-level utility functions for parsing reschedule timing
# =====================================================================


