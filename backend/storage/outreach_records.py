"""DynamoDB Patient Outreach & Agent Data Linkage Layer."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import uuid
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ProfileNotFound

try:
    from domain_rules import (
        DEFAULT_CALLBACK_OFFSET_HOURS,
        DEFAULT_MAX_RESCHEDULE_ATTEMPTS,
        CallDisposition,
    )
except ImportError:
    try:
        from backend.domain_rules import (
            DEFAULT_CALLBACK_OFFSET_HOURS,
            DEFAULT_MAX_RESCHEDULE_ATTEMPTS,
            CallDisposition,
        )
    except ImportError:
        DEFAULT_CALLBACK_OFFSET_HOURS = 24
        DEFAULT_MAX_RESCHEDULE_ATTEMPTS = 3
        class CallDisposition:
            PENDING = "PENDING"
            TRANSFERRED = "TRANSFERRED"
            RESCHEDULED = "RESCHEDULED"
            COMPLETED = "COMPLETED"

logger = logging.getLogger("outreach_records")

DEFAULT_TABLE_NAME = "CareHarmony_OutreachRecords"
# Retry policy: any attempt that ends without a resolved conversation with
# the patient (never connected, voicemail, no engagement, identity never
# confirmed, or the patient hung up mid-conversation) is redialed 24h later,
# up to 3 retries after the first attempt.
MAX_RETRIES = 3
MAX_CALL_ATTEMPTS = MAX_RETRIES + 1
RETRY_DELAY = dt.timedelta(hours=24)

# Connected-call outcomes that still count as "no real contact"
RETRYABLE_CALL_OUTCOMES = frozenset({
    "NO_ENGAGEMENT",
    "VOICEMAIL",
    "IDENTITY_UNCONFIRMED",
    "PATIENT_HANGUP",
})

# Statuses the campaign dialer picks up (lambda/campaign_dialer.py
# DIALABLE_STATUSES). A record flagged for review leaves them so it is
# not called again until a person has checked it.
DIALABLE_STATUSES = ("PENDING", "RESCHEDULED")
NEEDS_REVIEW_STATUS = "NEEDS_REVIEW"


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_phone(raw: str) -> str:
    """Normalize phone number to international E.164 format with + prefix."""
    if not raw or not isinstance(raw, str):
        raise ValueError(f"Unparseable phone: {raw}")

    cleaned = raw.strip()
    if cleaned.startswith("+"):
        digits_only = re.sub(r"\D", "", cleaned[1:])
        if 10 <= len(digits_only) <= 15:
            return f"+{digits_only}"
        raise ValueError(f"Unparseable phone: {raw}")

    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    elif 10 <= len(digits) <= 15:
        return f"+{digits}"

    raise ValueError(f"Unparseable phone: {raw}")


def generate_campaign_id() -> str:
    """Generate campaign identifier in format CAMP-{YYYYMMDD}-{UUID8}."""
    date_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    unique_suffix = uuid.uuid4().hex[:8].upper()
    return f"CAMP-{date_str}-{unique_suffix}"


def get_dynamodb_resource(profile_name: str = "careharmony", region_name: str = "us-east-1") -> Any:
    explicit_profile = os.environ.get("AWS_PROFILE", profile_name)
    try:
        session = boto3.Session(profile_name=explicit_profile, region_name=region_name)
        session.get_credentials()
        return session.resource("dynamodb")
    except (ProfileNotFound, Exception):
        return boto3.Session(region_name=region_name).resource("dynamodb")


def get_patient_by_empi(
    empi: str,
    table_name: str = DEFAULT_TABLE_NAME,
    dynamodb_resource: Any = None,
) -> Optional[Dict[str, Any]]:
    """Looks up an existing record by empi via the EmpiIndex GSI."""
    try:
        ddb = dynamodb_resource or get_dynamodb_resource()
        target_table_name = os.environ.get("OUTREACH_RECORDS_TABLE", table_name)
        table = ddb.Table(target_table_name)
        resp = table.query(
            IndexName="EmpiIndex",
            KeyConditionExpression="empi = :e",
            ExpressionAttributeValues={":e": empi},
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except Exception as e:
        logger.warning(f"Error querying EmpiIndex for {empi}: {e}")
        return None


def write_patient_record(
    patient: Dict[str, Any],
    campaign_id: str,
    scheduled_time: str,
    file_size_bytes: int = 0,
    table_name: str = DEFAULT_TABLE_NAME,
    dynamodb_resource: Any = None,
    source_filename: str = "patients.csv",
) -> Dict[str, Any]:
    """Validate, format, and persist a single patient CSV row to DynamoDB.
    
    Decouples patient_id (fresh UUID per record) from empi (business identifier).
    Each upload is an independent, campaign-scoped record.
    """
    try:
        empi = str(patient.get("empi") or patient.get("patient_id") or "").strip()
        if not empi:
            raise ValueError("Missing required patient identifier 'empi'")

        raw_phone = str(patient.get("phone_number") or patient.get("phoneNumber") or "").strip()
        if not raw_phone:
            raise ValueError("Missing required field 'phone_number'")
        normalized_phone = normalize_phone(raw_phone)

        first_name = str(patient.get("first_name") or patient.get("firstName") or "").strip().title()
        last_name = str(patient.get("last_name") or patient.get("lastName") or "").strip().title()
        gender = str(patient.get("gender") or "").strip()
        practice_name = str(patient.get("practice_name") or patient.get("practiceName") or "").strip()
        practice_callback_number = str(
            patient.get("practice_callback_number") or patient.get("practiceCallbackNumber") or ""
        ).strip()
        provider_name = str(patient.get("provider_name") or patient.get("providerName") or "").strip()
        call_mode = str(patient.get("call_mode") or "MODE_2_NORMAL_COACHING").strip()

        # ALWAYS generate a fresh patient_id — no reuse, no lookup.
        # Each upload is an independent, campaign-scoped record.
        patient_id = str(uuid.uuid4())

        now_iso = utc_now_iso()

        item: Dict[str, Any] = {
            # Primary key (UUID)
            "patient_id": patient_id,
            # Business identifier (from CSV)
            "empi": empi,
            "first_name": first_name,
            "last_name": last_name,
            "gender": gender,
            "phone_number": normalized_phone,
            "practice_name": practice_name,
            "practice_callback_number": practice_callback_number,
            "provider_name": provider_name,
            # Campaign metadata (auto-populated at ingest)
            "campaign_id": campaign_id,
            "source_filename": source_filename,
            "call_mode": call_mode,
            "call_status": "PENDING",
            "scheduled_time": scheduled_time,
            "reschedule_count": 0,
            "attempt_count": 0,
            "created_at": now_iso,
            "last_updated_at": now_iso,
        }

        human_agent_phone = str(patient.get("human_agent_phone") or "").strip()
        if human_agent_phone:
            item["human_agent_phone"] = human_agent_phone
        campaign_timezone = str(patient.get("timezone") or "").strip()
        if campaign_timezone:
            item["timezone"] = campaign_timezone
        voice = str(patient.get("voice") or "").strip()
        if voice:
            item["voice"] = voice

        if file_size_bytes and file_size_bytes > 0:
            item["file_size_bytes"] = file_size_bytes

        # Put item into DynamoDB
        ddb = dynamodb_resource or get_dynamodb_resource()
        target_table_name = os.environ.get("OUTREACH_RECORDS_TABLE", table_name)
        table = ddb.Table(target_table_name)
        table.put_item(Item=item)

        return {"success": True, "patient_id": patient_id, "empi": empi, "item": item}


    except Exception as e:
        logger.warning(f"Failed to write patient record: {e}")
        return {"success": False, "error": str(e), "row": patient}


class OutreachRecordService:
    """Manages patient outreach state, live agent linkage, and callback rescheduling in DynamoDB."""

    def __init__(self, table_name: str = DEFAULT_TABLE_NAME, dynamodb_resource: Any = None):
        self.table_name = os.environ.get("OUTREACH_RECORDS_TABLE", table_name)
        self._dynamodb = dynamodb_resource or get_dynamodb_resource()
        self._table = self._dynamodb.Table(self.table_name)
        # In-memory store fallback for offline unit tests
        self._in_memory_store: Dict[str, Dict[str, Any]] = {}

    def get_patient_by_empi(self, empi: str) -> Optional[Dict[str, Any]]:
        """Looks up an existing record by empi via the EmpiIndex GSI."""
        try:
            resp = self._table.query(
                IndexName="EmpiIndex",
                KeyConditionExpression="empi = :e",
                ExpressionAttributeValues={":e": empi},
                Limit=1,
            )
            items = resp.get("Items", [])
            return items[0] if items else None
        except Exception as e:
            logger.debug(f"DynamoDB get_patient_by_empi query fallback ({e})")
            for item in self._in_memory_store.values():
                if item.get("empi") == empi:
                    return item
            return None

    def get_record(self, patient_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve patient outreach record by patient_id."""
        try:
            resp = self._table.get_item(Key={"patient_id": patient_id})
            return resp.get("Item")
        except Exception as e:
            logger.debug(f"DynamoDB get_item fallback to in-memory store ({e})")
            return self._in_memory_store.get(patient_id)

    def put_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Insert or update a complete patient outreach record."""
        patient_id = record["patient_id"]
        now_iso = utc_now_iso()
        record.setdefault("reschedule_count", 0)
        record.setdefault("attempt_count", 0)
        record.setdefault("call_status", "PENDING")
        record.setdefault("created_at", now_iso)
        record["last_updated_at"] = now_iso

        try:
            self._table.put_item(Item=record)
        except Exception as e:
            logger.debug(f"DynamoDB put_item fallback to in-memory store ({e})")
            self._in_memory_store[patient_id] = dict(record)

        return record

    def link_assigned_agent(self, patient_id: str, agent_data: Dict[str, Any]) -> Dict[str, Any]:
        """Link live PES agent metadata to patient record upon availability confirmation."""
        now_iso = utc_now_iso()
        assigned_agent = {
            "agentId": agent_data.get("agentId", "agent-001"),
            "agentName": agent_data.get("agentName", "Sarah Jenkins"),
            "agentPhone": agent_data.get("agentPhone", "+15822671755"),
            "checkedAt": agent_data.get("checkedAt", now_iso),
        }

        try:
            status_val = (
                CallDisposition.TRANSFERRED.value
                if hasattr(CallDisposition.TRANSFERRED, "value")
                else CallDisposition.TRANSFERRED
            )
            self._table.update_item(
                Key={"patient_id": patient_id},
                UpdateExpression="SET assigned_agent = :agent, call_status = :status, last_updated_at = :ts",
                ExpressionAttributeValues={
                    ":agent": assigned_agent,
                    ":status": status_val,
                    ":ts": now_iso,
                },
            )
        except Exception as e:
            logger.debug(f"DynamoDB update_item fallback to in-memory store ({e})")
            record = self._in_memory_store.setdefault(patient_id, {"patient_id": patient_id, "reschedule_count": 0})
            record["assigned_agent"] = assigned_agent
            record["call_status"] = "TRANSFERRED"
            record["last_updated_at"] = now_iso

        return assigned_agent

    def update_reschedule(
        self, patient_id: str, requested_time: Optional[str] = None
    ) -> Dict[str, Any]:
        """Calculate next callback timestamp, increment reschedule_count (max 3), and update status."""
        record = self.get_record(patient_id) or {"patient_id": patient_id, "reschedule_count": 0}
        current_count = int(record.get("reschedule_count", 0))
        new_count = current_count + 1
        now = dt.datetime.now(dt.timezone.utc)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        if new_count >= DEFAULT_MAX_RESCHEDULE_ATTEMPTS:
            status = "EXHAUSTED"
            scheduled_time = None
        else:
            status = (
                CallDisposition.RESCHEDULED.value
                if hasattr(CallDisposition.RESCHEDULED, "value")
                else "RESCHEDULED"
            )
            if requested_time and requested_time.strip():
                scheduled_time = requested_time.strip()
            else:
                next_time = now + dt.timedelta(hours=DEFAULT_CALLBACK_OFFSET_HOURS)
                scheduled_time = next_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        updated_fields = {
            "reschedule_count": new_count,
            "call_status": status,
            "scheduled_time": scheduled_time,
            "last_updated_at": now_iso,
        }

        try:
            self._table.update_item(
                Key={"patient_id": patient_id},
                UpdateExpression="SET reschedule_count = :rc, call_status = :cs, scheduled_time = :st, last_updated_at = :ts",
                ExpressionAttributeValues={
                    ":rc": new_count,
                    ":cs": status,
                    ":st": scheduled_time,
                    ":ts": now_iso,
                },
            )
        except Exception as e:
            logger.debug(f"DynamoDB update_item fallback to in-memory store ({e})")
            rec = self._in_memory_store.setdefault(patient_id, {"patient_id": patient_id})
            rec.update(updated_fields)

        return updated_fields

    def update_call_outcome(
        self,
        patient_id: str,
        call_id: Optional[str] = None,
        call_status: str = "COMPLETED",
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Updates a patient record's call_status and last_updated_at after a call ends.
        Called from tools.py and pipeline.py.
        Idempotent — safe to call multiple times with the same status.
        """
        now = utc_now_iso()
        update_expr = "SET call_status = :s, last_updated_at = :t"
        expr_vals = {
            ":s": call_status,
            ":t": now,
        }
        if call_id:
            update_expr += ", call_id = :c"
            expr_vals[":c"] = call_id

        try:
            self._table.update_item(
                Key={"patient_id": patient_id},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_vals,
            )
            logger.info(f"[DDB_OUTCOME] Updated patient_id={patient_id} call_status={call_status} call_id={call_id}")
        except Exception as e:
            logger.warning(f"DynamoDB update_call_outcome fallback/error ({e})")
            rec = self._in_memory_store.setdefault(patient_id, {"patient_id": patient_id})
            rec["call_status"] = call_status
            rec["last_updated_at"] = now
            if call_id:
                rec["call_id"] = call_id

        return {"patient_id": patient_id, "call_status": call_status, "last_updated_at": now}

    @property
    def table(self):
        return self._table

    async def mark_call_started(self, patient_id: str, call_id: str):
        """Record the call start timestamp when call is answered (200 OK)."""
        now = utc_now_iso()
        try:
            # Runs at pickup, on the live call's event loop: keep the network call off it
            await asyncio.to_thread(
                self._table.update_item,
                Key={"patient_id": patient_id},
                UpdateExpression="SET call_start_datetime = :cs, last_call_id = :c, last_updated_at = :ts",
                ExpressionAttributeValues={":cs": now, ":c": call_id, ":ts": now},
            )
            logger.info(f"[CALL_STARTED_DB] patient_id={patient_id} call_id={call_id} start={now}")
        except Exception as e:
            logger.debug(f"DynamoDB mark_call_started fallback ({e})")
            rec = self._in_memory_store.setdefault(patient_id, {"patient_id": patient_id})
            rec["call_start_datetime"] = now
            rec["last_call_id"] = call_id
            rec["last_updated_at"] = now

    async def mark_call_ended(self, patient_id: str, call_summary: str):
        """Record the call end timestamp and summary upon teardown or termination."""
        now = utc_now_iso()
        try:
            self._table.update_item(
                Key={"patient_id": patient_id},
                UpdateExpression="SET call_end_datetime = :ce, call_summary = :cs, last_updated_at = :ts",
                ExpressionAttributeValues={":ce": now, ":cs": call_summary, ":ts": now},
            )
            logger.info(f"[CALL_ENDED_DB] patient_id={patient_id} end={now} summary='{call_summary}'")
        except Exception as e:
            logger.debug(f"DynamoDB mark_call_ended fallback ({e})")
            rec = self._in_memory_store.setdefault(patient_id, {"patient_id": patient_id})
            rec["call_end_datetime"] = now
            rec["call_summary"] = call_summary
            rec["last_updated_at"] = now

    async def flag_possible_death(self, patient_id: str, heard: str):
        """
        Mark the record for human review because the caller may have said
        the patient died, but the call did not end as DECEASED (phone ASR
        can mishear it, as in call 5c32159a). Appends a note to whatever
        call_summary the outcome write left, and sets needs_review.

        If that outcome queued another call (PENDING retry or RESCHEDULED
        callback), the status becomes NEEDS_REVIEW so the dialer skips the
        record and a possibly bereaved family is not called again until
        someone checks. The queued status is kept in status_before_review.
        """
        now = utc_now_iso()
        note = f'Needs review: caller may have said the patient passed away (heard: "{heard}")'
        item = self.get_record(patient_id) or {}
        existing = (item.get("call_summary") or "").rstrip(". ")
        summary = f"{existing}. {note}" if existing else note
        current_status = item.get("call_status")

        updates = {
            "call_summary": summary,
            "needs_review": True,
            "review_reason": "POSSIBLE_DECEASED",
            "last_updated_at": now,
        }
        if current_status in DIALABLE_STATUSES:
            updates["call_status"] = NEEDS_REVIEW_STATUS
            updates["status_before_review"] = current_status

        placeholders = {field: f":v{i}" for i, field in enumerate(updates)}
        try:
            self._table.update_item(
                Key={"patient_id": patient_id},
                UpdateExpression="SET " + ", ".join(
                    f"{field} = {placeholders[field]}" for field in updates
                ),
                ExpressionAttributeValues={
                    placeholders[field]: value for field, value in updates.items()
                },
            )
            logger.info(
                f"[REVIEW_FLAG_DB] patient_id={patient_id} "
                f"status={updates.get('call_status', current_status)} summary='{summary}'"
            )
        except Exception as e:
            logger.debug(f"DynamoDB flag_possible_death fallback ({e})")
            rec = self._in_memory_store.setdefault(patient_id, {"patient_id": patient_id})
            rec.update(updates)

    async def schedule_retry(
        self,
        patient_id: str,
        call_id: str,
        reason: str,
        sip_code: Any = None,
        count_attempt: bool = False,
    ) -> Dict[str, Any]:
        """
        Record an attempt that ended without resolving the call with the
        patient and queue the next one.

        reason is what happened: a SIP failure (NO_ANSWER, BUSY, ...), a
        gateway error, or a connected-call outcome from
        RETRYABLE_CALL_OUTCOMES. The record goes back to PENDING with
        scheduled_time RETRY_DELAY from now, or to EXHAUSTED once the
        first attempt and MAX_RETRIES retries have all been used.

        The dialer counts an attempt when it places a call, so
        count_attempt is only True for attempts that never got that far
        (the dialer could not reach the gateway).
        """
        now_dt = dt.datetime.now(dt.timezone.utc)
        now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        item = self.get_record(patient_id) or {}
        current_attempts = int(item.get("attempt_count", 0))
        if count_attempt:
            new_attempt_count = current_attempts + 1
        else:
            new_attempt_count = max(current_attempts, 1)

        exhausted = new_attempt_count >= MAX_CALL_ATTEMPTS
        final_status = "EXHAUSTED" if exhausted else "PENDING"
        next_scheduled = None
        if not exhausted:
            next_scheduled = (now_dt + RETRY_DELAY).strftime("%Y-%m-%dT%H:%M:%SZ")

        summary = format_retry_summary(
            reason,
            attempt_count=new_attempt_count,
            next_scheduled=next_scheduled,
        )

        update_expr_parts = [
            "call_status = :s",
            "attempt_count = :a",
            "last_failure_reason = :r",
            "last_sip_code = :sc",
            "last_updated_at = :t",
            "call_end_datetime = :ce",
            "call_summary = :cs",
        ]
        expr_values = {
            ":s": final_status,
            ":a": new_attempt_count,
            ":r": reason,
            ":sc": str(sip_code) if sip_code is not None else "N/A",
            ":t": now_iso,
            ":ce": now_iso,
            ":cs": summary,
        }

        # Only include last_call_id in the update if we actually have one
        if call_id:
            update_expr_parts.append("last_call_id = :c")
            expr_values[":c"] = call_id

        if next_scheduled:
            update_expr_parts.append("scheduled_time = :st")
            expr_values[":st"] = next_scheduled

        logger.info(
            f"[CALL_RETRY] patient_id={patient_id} reason={reason} "
            f"sip_code={sip_code} attempt={new_attempt_count}/"
            f"{MAX_CALL_ATTEMPTS} -> {final_status}"
            + (f", retry at {next_scheduled}" if next_scheduled else " (no further retry)")
        )

        update_expr = "SET " + ", ".join(update_expr_parts)

        try:
            self._table.update_item(
                Key={"patient_id": patient_id},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_values,
            )
        except Exception as e:
            logger.debug(f"DynamoDB update_item fallback to in-memory store ({e})")
            rec = self._in_memory_store.setdefault(patient_id, {"patient_id": patient_id})
            rec["call_status"] = final_status
            rec["attempt_count"] = new_attempt_count
            if call_id:
                rec["last_call_id"] = call_id
            rec["last_failure_reason"] = reason
            rec["last_sip_code"] = expr_values[":sc"]
            rec["last_updated_at"] = now_iso
            rec["call_end_datetime"] = now_iso
            rec["call_summary"] = summary
            if next_scheduled:
                rec["scheduled_time"] = next_scheduled

        ret = {
            "patient_id": patient_id,
            "call_status": final_status,
            "attempt_count": new_attempt_count,
            "last_failure_reason": reason,
            "scheduled_time": next_scheduled or "N/A",
        }
        if call_id:
            ret["last_call_id"] = call_id
        return ret

def format_disposition(
    call_status: str,
    reason: Optional[str] = None,
    rescheduled: bool = False,
    retry_delay_desc: Optional[str] = None,
    scheduled_time: Optional[str] = None,
) -> str:
    """
    Builds a human-readable disposition string. Every possible
    call_status value must have an explicit branch — no raw
    concatenation fallback for known statuses.
    """
    call_status = (call_status or "UNKNOWN").upper()
    reason = (reason or "").upper()

    # --- Terminal outcomes (call connected and resolved) ---
    if call_status == "COMPLETED":
        return "COMPLETED - Call Completed"
    if call_status == "NO_ENGAGEMENT":
        return "NO_ENGAGEMENT - Call Connected, No Response Given"
    if call_status == "TRANSFERRED":
        return "TRANSFERRED - Connected to Agent"
    if call_status == "REFUSED":
        return "REFUSED - Patient Declined Outreach"
    if call_status == "DO_NOT_CALL":
        return "DO_NOT_CALL - Patient Requested No Further Contact"
    if call_status == "WRONG_NUMBER":
        return "WRONG_NUMBER - Invalid Contact Number"
    if call_status == "DECEASED":
        return "DECEASED - Record Flagged"
    if call_status == "SAFETY_MEDICAL":
        return "SAFETY_MEDICAL - Emergency Escalation Triggered"
    if call_status == "SAFETY_BEHAVIORAL":
        return "SAFETY_BEHAVIORAL - Crisis Escalation Triggered"
    if call_status == "VOICEMAIL":
        return "VOICEMAIL - Message Left"
    if call_status == "IDENTITY_UNCONFIRMED":
        return "IDENTITY_UNCONFIRMED - Unable to Confirm Patient Identity"
    if call_status == NEEDS_REVIEW_STATUS:
        return "NEEDS_REVIEW - Possible Deceased, Calls Paused"

    # --- Rescheduled (either live decline or a retryable failure) ---
    if call_status == "RESCHEDULED":
        return "RESCHEDULED - Callback Requested"

    if call_status == "PENDING" and rescheduled:
        # A retryable failure was auto-rescheduled — this is the
        # "AMD_TIMEOUT_UNRESOLVED - Auto-Rescheduled (24h)" case
        reason_label = reason or "UNRESOLVED"
        delay_label = retry_delay_desc or "pending retry"
        return f"{reason_label}_UNRESOLVED - Auto-Rescheduled ({delay_label})"

    if call_status == "PENDING":
        # Still in queue, not yet attempted or mid-retry-cycle
        # with no delay description available
        return "PENDING - Awaiting Next Attempt"

    # --- Exhausted retries ---
    if call_status == "EXHAUSTED":
        if reason:
            return f"EXHAUSTED - Max Attempts Reached (Last: {reason})"
        return "EXHAUSTED - Max Attempts Reached"

    # --- Explicit failure reasons (call never connected) ---
    FAILURE_LABELS = {
        "BUSY": "BUSY - Line Was Busy",
        "NO_ANSWER": "NO_ANSWER - No Response",
        "UNAVAILABLE": "UNAVAILABLE - Recipient Unreachable",
        "REJECTED": "REJECTED - Call Declined by Carrier",
        "CANCELLED": "CANCELLED - Call Cancelled Before Connection",
        "INVALID_NUMBER": "INVALID_NUMBER - Number Not in Service",
        "SERVICE_UNAVAILABLE": "SERVICE_UNAVAILABLE - Carrier Error",
        "TIMEOUT": "TIMEOUT - No Response Before Timeout",
        "GATEWAY_TIMEOUT": "GATEWAY_TIMEOUT - Voice Gateway Unreachable (Timeout)",
        "GATEWAY_REFUSED": "GATEWAY_REFUSED - Voice Gateway Connection Refused",
        "GATEWAY_DNS_FAILURE": "GATEWAY_DNS_FAILURE - Voice Gateway Address Resolution Failed",
        "GATEWAY_UNREACHABLE": "GATEWAY_UNREACHABLE - Voice Gateway Network Unreachable",
        "GATEWAY_SERVER_ERROR": "GATEWAY_SERVER_ERROR - Voice Gateway Internal Error",
        "GATEWAY_REQUEST_REJECTED": "GATEWAY_REQUEST_REJECTED - Voice Gateway Rejected Request",
        "GATEWAY_ERROR": "GATEWAY_ERROR - Voice Gateway Connection Failed",
    }
    if call_status == "FAILED":
        if reason in FAILURE_LABELS:
            return FAILURE_LABELS[reason]
        if reason:
            return f"FAILED - {reason.replace('_', ' ').title()}"
        return "FAILED - Call Did Not Connect"

    # Reason itself may BE the status in some write paths
    # (e.g. call_status written as "BUSY" directly)
    if call_status in FAILURE_LABELS:
        return FAILURE_LABELS[call_status]

    # Legacy/unexpected status — never silently pass through raw
    logger.warning(
        f"[FORMAT_DISPOSITION] Unhandled call_status="
        f"'{call_status}' reason='{reason}' — using generic label"
    )
    return f"{call_status} - Status Unrecognized"


def format_call_summary(
    call_status: str,
    reason: Optional[str] = None,
    disposition_text: Optional[str] = None,
) -> str:
    """Builds the free-text call_summary field."""
    if call_status == "NO_ENGAGEMENT":
        return "Call was answered but recipient never spoke"
    if call_status == "RESCHEDULED":
        return "Callback requested by patient"
    if call_status in ("NO_ANSWER", "PENDING") and reason == "NO_ANSWER":
        return (
            "Callback requested: Silent answer or unresolved "
            "AMD without answer or voicemail resolution"
        )
    if call_status == "TRANSFERRED":
        return "Patient connected to live agent"
    if call_status == "REFUSED":
        return "Patient declined outreach"
    if reason and reason.startswith("GATEWAY_"):
        gateway_detail = reason.replace("GATEWAY_", "").replace("_", " ").title()
        return f"Call could not be placed: voice gateway was unreachable ({gateway_detail})"
    if call_status in ("BUSY", "UNAVAILABLE", "FAILED"):
        return f"Call failed to connect: {reason or call_status}"
    if call_status == "VOICEMAIL":
        return "Voicemail message left"
    if call_status == "IDENTITY_UNCONFIRMED":
        return (
            "Call ended — patient identity could not be confirmed "
            "due to unclear responses"
        )
    return f"Call ended with status {call_status}"


def format_retry_summary(
    reason: str,
    attempt_count: int,
    next_scheduled: Optional[str] = None,
) -> str:
    """Builds call_summary for an attempt that ended without reaching the patient."""
    if reason == "PATIENT_HANGUP":
        what = "Patient hung up mid-conversation"
    elif reason == "NO_ENGAGEMENT":
        what = "Call was answered but recipient never spoke"
    elif reason == "VOICEMAIL":
        what = "Reached voicemail; message left"
    elif reason == "IDENTITY_UNCONFIRMED":
        what = "Patient identity could not be confirmed"
    elif reason.startswith("GATEWAY_"):
        gateway_detail = reason.replace("GATEWAY_", "").replace("_", " ").title()
        what = f"Call could not be placed: voice gateway was unreachable ({gateway_detail})"
    else:
        what = f"Call failed to connect: {reason}"

    if next_scheduled:
        # Attempt N failing schedules retry N
        return f"{what}. Retry {attempt_count} of {MAX_RETRIES} scheduled for {next_scheduled}"
    return f"{what}. No further retries after {attempt_count} attempts"
