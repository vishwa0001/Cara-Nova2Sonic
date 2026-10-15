"""HTTP API Lambda for Cara Health Bot campaign management.

Exposes RESTful endpoints for presigned S3 upload URLs, campaign listing,
patient records lookup, and campaign cancellation.
Deployed with AWS Lambda Function URL for direct frontend interaction.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

try:
    from outreach_records import (
        DEFAULT_TABLE_NAME,
        generate_campaign_id,
        normalize_phone,
        utc_now_iso,
        format_disposition,
        format_call_summary,
    )
except ImportError:
    try:
        from storage.outreach_records import (
            DEFAULT_TABLE_NAME,
            generate_campaign_id,
            normalize_phone,
            utc_now_iso,
            format_disposition,
            format_call_summary,
        )
    except ImportError:
        from backend.storage.outreach_records import (
            DEFAULT_TABLE_NAME,
            generate_campaign_id,
            normalize_phone,
            utc_now_iso,
            format_disposition,
            format_call_summary,
        )

logger = logging.getLogger("campaign_api")
logger.setLevel(logging.INFO)

S3_BUCKET_NAME = os.environ.get(
    "CAMPAIGN_BUCKET_NAME", "cara-health-bot-campaigns-701348334422-us-east-1"
)
TABLE_NAME = os.environ.get("OUTREACH_RECORDS_TABLE", DEFAULT_TABLE_NAME)
# Voice keys the UI can pick; the gateway maps each to Nova + Polly voices.
VOICES = ("joanna", "tiffany", "matthew", "amy", "olivia", "kiara")
DEFAULT_VOICE = "joanna"
DIALING_MODES = ("SEQUENTIAL", "PARALLEL")
DEFAULT_DIALING_MODE = "SEQUENTIAL"
# Most calls the current setup can run at once; the dialer enforces the same cap.
MAX_CONCURRENT_CALLS_LIMIT = 25


def _build_response(status_code: int, body: Any) -> Dict[str, Any]:
    """Generate standard CORS-enabled HTTP response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amz-Security-Token",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _get_s3_client():
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"),
    )


def _get_dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _valid_timezone(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        return ""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return name
    except Exception:
        logger.warning(f"Ignoring unknown timezone {name!r}")
        return ""


def _call_mode_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """UI-facing call mode fields derived from a stored outreach record."""
    direct = record.get("call_mode") == "MODE_1_DIRECT_HANDOFF"
    return {
        "callMode": "DIRECT_HUMAN_HANDOFF" if direct else "NORMAL",
        "directAgentEnabled": direct,
        "humanAgentPhoneNumber": record.get("human_agent_phone", ""),
        "voice": record.get("voice") or DEFAULT_VOICE,
        # Batches scheduled before the dialing mode setting existed have neither.
        "dialingMode": record.get("dialing_mode") or None,
        "maxConcurrentCalls": int(record["max_concurrent_calls"]) if record.get("max_concurrent_calls") else None,
    }


def handle_presigned_url(body: Dict[str, Any]) -> Dict[str, Any]:
    """Generate S3 presigned PUT URL for client-side direct CSV upload."""
    filename = str(body.get("filename") or body.get("fileName") or "patients.csv").strip()
    scheduled_time = str(body.get("scheduled_time") or body.get("scheduledAt") or "").strip()
    if not scheduled_time:
        scheduled_time = utc_now_iso()

    # Campaign-level "Direct to Human Agent" toggle. It rides along as signed
    # S3 object metadata so intake can stamp call_mode on every row.
    direct_agent = _truthy(body.get("directAgentEnabled", body.get("direct_agent_enabled")))
    raw_agent_phone = str(body.get("humanAgentPhoneNumber") or body.get("human_agent_phone") or "").strip()
    agent_phone = ""
    if raw_agent_phone:
        try:
            agent_phone = normalize_phone(raw_agent_phone)
        except ValueError:
            return _build_response(400, {"error": "humanAgentPhoneNumber is not a valid phone number"})
    elif direct_agent:
        return _build_response(400, {"error": "humanAgentPhoneNumber is required when Direct to Human Agent is on"})

    voice = str(body.get("voice") or DEFAULT_VOICE).strip().lower()
    if voice not in VOICES:
        return _build_response(400, {"error": f"voice must be one of: {', '.join(VOICES)}"})

    dialing_mode = str(body.get("dialingMode") or DEFAULT_DIALING_MODE).strip().upper()
    if dialing_mode not in DIALING_MODES:
        return _build_response(400, {"error": f"dialingMode must be one of: {', '.join(DIALING_MODES)}"})
    max_concurrent_calls = 1
    if dialing_mode == "PARALLEL":
        try:
            max_concurrent_calls = int(body.get("maxConcurrentCalls"))
        except (TypeError, ValueError):
            max_concurrent_calls = 0
        if not 1 <= max_concurrent_calls <= MAX_CONCURRENT_CALLS_LIMIT:
            return _build_response(400, {
                "error": f"maxConcurrentCalls must be a whole number from 1 to {MAX_CONCURRENT_CALLS_LIMIT}"
            })

    campaign_id = generate_campaign_id()
    s3_key = f"campaigns/{campaign_id}/patients.csv"

    metadata = {
        "scheduled-time": scheduled_time,
        # The browser sends this header as encodeURIComponent(name); sign the
        # same encoding or names with spaces ("list (5).csv") get a 403.
        "original-filename": quote(filename, safe="!~*'()"),
        "direct-agent": "yes" if direct_agent else "no",
        "voice": voice,
        "dialing-mode": dialing_mode,
        "max-concurrent-calls": str(max_concurrent_calls),
    }
    if agent_phone:
        metadata["human-agent-phone"] = agent_phone
    # Timezone chosen in the UI; the gateway reads patients' callback
    # times ("tomorrow at 10") in it.
    campaign_tz = _valid_timezone(body.get("timezone"))
    if campaign_tz:
        metadata["timezone"] = campaign_tz

    s3_client = _get_s3_client()

    try:
        # Generate presigned PUT URL with 15-minute expiration
        upload_url = s3_client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": S3_BUCKET_NAME,
                "Key": s3_key,
                "ContentType": "text/csv",
                "Metadata": metadata,
            },
            ExpiresIn=900,  # 15 minutes
        )

        return _build_response(200, {
            "upload_url": upload_url,
            "uploadUrl": upload_url,
            "campaign_id": campaign_id,
            "campaignId": campaign_id,
            "s3_key": s3_key,
            "scheduled_time": scheduled_time,
            "fileName": filename,
            "directAgentEnabled": direct_agent,
            "humanAgentPhoneNumber": agent_phone,
            "voice": voice,
            "dialingMode": dialing_mode,
            "maxConcurrentCalls": max_concurrent_calls,
            # Exact x-amz-meta-* headers the PUT must send to match the signature.
            "uploadHeaders": {f"x-amz-meta-{k}": v for k, v in metadata.items()},
        })
    except Exception as e:
        logger.error(f"Error generating presigned URL: {e}", exc_info=True)
        return _build_response(500, {"error": f"Failed to generate presigned URL: {str(e)}"})


def _awaiting_retry(record: Dict[str, Any]) -> bool:
    """True when the record has been dialed and is queued for another
    attempt: a no-contact retry (PENDING with attempts on it) or a
    patient-requested callback (RESCHEDULED)."""
    st = record.get("call_status", "PENDING")
    if st == "RESCHEDULED":
        return True
    return st == "PENDING" and int(record.get("attempt_count", 0) or 0) > 0


def handle_list_campaigns() -> Dict[str, Any]:
    """Scan CareHarmony_OutreachRecords and aggregate records by campaign_id."""
    ddb = _get_dynamodb_resource()
    table = ddb.Table(TABLE_NAME)

    try:
        # Scan table for campaign aggregation
        response = table.scan()
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))

        campaign_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for it in items:
            cid = it.get("campaign_id") or "UNKNOWN_CAMPAIGN"
            campaign_groups[cid].append(it)

        campaigns_summary: List[Dict[str, Any]] = []

        for cid, recs in campaign_groups.items():
            if cid == "UNKNOWN_CAMPAIGN":
                continue

            total_rows = len(recs)
            status_counts = defaultdict(int)
            scheduled_time = ""
            created_at = ""

            not_yet_dialed = 0
            awaiting_retry = 0
            earliest_retry = ""
            for r in recs:
                st = r.get("call_status", "PENDING")
                status_counts[st] += 1
                if _awaiting_retry(r):
                    awaiting_retry += 1
                    retry_at = str(r.get("scheduled_time") or "")
                    if retry_at and (not earliest_retry or retry_at < earliest_retry):
                        earliest_retry = retry_at
                elif st == "PENDING":
                    not_yet_dialed += 1
                if not scheduled_time and r.get("scheduled_time"):
                    scheduled_time = r["scheduled_time"]
                if not created_at and r.get("created_at"):
                    created_at = r["created_at"]

            # Derive overall campaign status. A PENDING record that has
            # already been dialed is waiting on its 24h retry, so the
            # campaign has started and must not read as SCHEDULED.
            # nextAttemptAt is only set while the campaign sits idle
            # waiting on those retries.
            next_attempt_at = ""
            if status_counts["DIALING"] > 0 or status_counts["IN_PROGRESS"] > 0:
                campaign_status = "RUNNING"
            elif not_yet_dialed == total_rows:
                campaign_status = "SCHEDULED"
            elif not_yet_dialed > 0:
                campaign_status = "RUNNING"
            elif awaiting_retry > 0:
                campaign_status = "PROCESSING"
                next_attempt_at = earliest_retry
            elif status_counts["CANCELLED"] == total_rows:
                campaign_status = "CANCELLED"
            else:
                campaign_status = "COMPLETED"

            file_size_bytes = int(recs[0].get("file_size_bytes") or 0)

            campaigns_summary.append({
                "campaign_id": cid,
                "campaignId": cid,
                "status": campaign_status,
                "fileName": recs[0].get("source_filename", "patients.csv"),
                "file_size_bytes": file_size_bytes,
                "fileSize": file_size_bytes,
                "total_rows": total_rows,
                "totalRows": total_rows,
                "customerCount": total_rows,
                "valid_rows": total_rows,
                "error_rows": 0,
                "scheduled_time": scheduled_time,
                "scheduledAt": scheduled_time,
                "created_at": created_at,
                "uploadedAt": created_at,
                "statusCounts": dict(status_counts),
                "awaitingRetryCount": awaiting_retry,
                "nextAttemptAt": next_attempt_at,
                **_call_mode_fields(recs[0]),
            })

        # Sort campaigns by created_at / scheduled_time descending
        campaigns_summary.sort(
            key=lambda c: str(c.get("created_at") or c.get("scheduled_time") or ""),
            reverse=True,
        )

        return _build_response(200, {
            "campaigns": campaigns_summary,
            "uploads": campaigns_summary,  # Compatibility alias
            "count": len(campaigns_summary),
        })

    except Exception as e:
        logger.error(f"Error listing campaigns: {e}", exc_info=True)
        return _build_response(500, {"error": f"Failed to list campaigns: {str(e)}"})


CSV_EXPORT_HEADERS = [
    "empi", "call_id", "call_start_datetime", "call_end_datetime",
    "disposition", "call_summary", "requested_callback_date_time",
    "outbound_call_phone_number", "call_type",
]


def _map_call_type(call_mode: str) -> str:
    """Maps internal call_mode enum to the simple export value."""
    if call_mode == "MODE_1_DIRECT_HANDOFF":
        return "DIRECT"
    return "NORMAL"  # MODE_2_NORMAL_COACHING and default


def _compute_retry_delay_label(end_iso: str, scheduled_iso: str) -> str:
    """Produces a short human label like '4h', '24h', '15m'."""
    try:
        from datetime import datetime
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        end_dt = datetime.strptime(end_iso.replace(".000Z", "Z"), fmt)
        sched_dt = datetime.strptime(scheduled_iso.replace(".000Z", "Z"), fmt)
        delta_hours = (sched_dt - end_dt).total_seconds() / 3600
        if delta_hours <= 0:
            # Stale pre-fix record where scheduled_time was never
            # updated on failure — don't show a misleading label
            return "pending retry"
        if delta_hours < 1:
            return f"{int(delta_hours * 60)}m"
        return f"{int(delta_hours)}h"
    except Exception:
        return "pending retry"


def _build_csv_error_response(status_code: int, message: str) -> Dict[str, Any]:
    """
    CSV export errors still need CORS headers, but return JSON
    since there's no file to attach.
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps({"error": message}),
    }


def handle_export_campaign_csv(campaign_id: str) -> Dict[str, Any]:
    """
    Exports all patient records for a campaign as a downloadable
    CSV file. Returns raw CSV text with headers set for browser
    download, not a JSON envelope like the other endpoints.
    """
    try:
        ddb = _get_dynamodb_resource()
        table = ddb.Table(TABLE_NAME)
        response = table.scan(
            FilterExpression="campaign_id = :cid",
            ExpressionAttributeValues={":cid": campaign_id},
        )
        records = response.get("Items", [])

        # Handle pagination if the campaign has many records
        while "LastEvaluatedKey" in response:
            response = table.scan(
                FilterExpression="campaign_id = :cid",
                ExpressionAttributeValues={":cid": campaign_id},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            records.extend(response.get("Items", []))

        if not records:
            logger.warning(
                f"[EXPORT] No records found for campaign_id={campaign_id}"
            )
            return _build_csv_error_response(
                404, f"No records found for campaign {campaign_id}"
            )

        output = io.StringIO()
        writer = csv.DictWriter(
            output, fieldnames=CSV_EXPORT_HEADERS, extrasaction="ignore"
        )
        writer.writeheader()
        for record in records:
            call_status = record.get("call_status", "")
            reason = record.get("last_failure_reason") or record.get("failure_reason")
            attempt_count = int(record.get("attempt_count", 0))
            scheduled_time = record.get("scheduled_time", "")

            is_auto_rescheduled = (
                call_status == "PENDING"
                and attempt_count > 0
                and bool(scheduled_time)
            )

            retry_delay_desc = None
            if is_auto_rescheduled and record.get("call_end_datetime"):
                retry_delay_desc = _compute_retry_delay_label(
                    record.get("call_end_datetime"), scheduled_time
                )

            disposition_str = format_disposition(
                call_status,
                reason=reason,
                rescheduled=is_auto_rescheduled,
                retry_delay_desc=retry_delay_desc,
                scheduled_time=scheduled_time,
            )

            requested_callback_date_time = ""
            if call_status in ("RESCHEDULED",) or (
                call_status == "PENDING" and int(record.get("attempt_count", 0)) > 0
            ):
                requested_callback_date_time = record.get("scheduled_time", "")

            row = {
                "empi": record.get("empi", ""),
                "call_id": record.get("last_call_id") or record.get("call_id", ""),
                "call_start_datetime": record.get("call_start_datetime", ""),
                "call_end_datetime": record.get("call_end_datetime", ""),
                "disposition": disposition_str,
                "call_summary": record.get("call_summary", ""),
                "requested_callback_date_time": requested_callback_date_time,
                "outbound_call_phone_number": record.get("phone_number", ""),
                "call_type": _map_call_type(record.get("call_mode", "")),
            }
            writer.writerow(row)

        csv_content = output.getvalue()
        output.close()

        logger.info(
            f"[EXPORT] Generated CSV for campaign_id={campaign_id} "
            f"with {len(records)} rows"
        )

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "text/csv",
                "Content-Disposition": (
                    f'attachment; filename="{campaign_id}_export.csv"'
                ),
                "Access-Control-Allow-Origin": "*",
            },
            "body": csv_content,
        }

    except Exception as e:
        logger.exception(
            f"[EXPORT_ERROR] Failed to export campaign_id={campaign_id}"
        )
        return _build_csv_error_response(500, str(e))


def handle_get_campaign_records(campaign_id: str) -> Dict[str, Any]:
    """Retrieve all patient records for a specific campaign."""
    ddb = _get_dynamodb_resource()
    table = ddb.Table(TABLE_NAME)

    try:
        response = table.scan(
            FilterExpression="campaign_id = :cid",
            ExpressionAttributeValues={":cid": campaign_id},
        )
        records = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = table.scan(
                FilterExpression="campaign_id = :cid",
                ExpressionAttributeValues={":cid": campaign_id},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            records.extend(response.get("Items", []))

        return _build_response(200, {
            "campaign_id": campaign_id,
            "campaignId": campaign_id,
            "records": records,
            "patientResults": records,
            "count": len(records),
            **(_call_mode_fields(records[0]) if records else {}),
        })
    except Exception as e:
        logger.error(f"Error retrieving records for {campaign_id}: {e}", exc_info=True)
        return _build_response(500, {"error": f"Failed to get campaign records: {str(e)}"})


def handle_cancel_campaign(campaign_id: str) -> Dict[str, Any]:
    """Cancel all pending calls for a campaign."""
    ddb = _get_dynamodb_resource()
    table = ddb.Table(TABLE_NAME)

    try:
        response = table.scan(
            FilterExpression="campaign_id = :cid AND call_status = :pending",
            ExpressionAttributeValues={":cid": campaign_id, ":pending": "PENDING"},
        )
        pending_records = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = table.scan(
                FilterExpression="campaign_id = :cid AND call_status = :pending",
                ExpressionAttributeValues={":cid": campaign_id, ":pending": "PENDING"},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            pending_records.extend(response.get("Items", []))

        cancelled_count = 0
        now_iso = utc_now_iso()

        for rec in pending_records:
            pid = rec["patient_id"]
            table.update_item(
                Key={"patient_id": pid},
                UpdateExpression="SET call_status = :cancelled, last_updated_at = :ts",
                ExpressionAttributeValues={
                    ":cancelled": "CANCELLED",
                    ":ts": now_iso,
                },
            )
            cancelled_count += 1

        logger.info(f"Cancelled {cancelled_count} records for campaign {campaign_id}")
        return _build_response(200, {
            "success": True,
            "campaign_id": campaign_id,
            "cancelled_count": cancelled_count,
            "status": "CANCELLED",
        })

    except Exception as e:
        logger.error(f"Error cancelling campaign {campaign_id}: {e}", exc_info=True)
        return _build_response(500, {"error": f"Failed to cancel campaign: {str(e)}"})


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Main routing handler for HTTP API requests."""
    # Handle CORS preflight
    http_method = (
        (event.get("requestContext") or {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or ""
    ).upper()

    if http_method == "OPTIONS":
        return _build_response(200, {"message": "OK"})

    raw_path = (
        (event.get("requestContext") or {}).get("http", {}).get("path")
        or event.get("rawPath")
        or event.get("path")
        or "/"
    ).strip("/")

    logger.info(f"[API_REQUEST] Method: {http_method}, Path: {raw_path}")

    # Parse JSON body if present
    body: Dict[str, Any] = {}
    raw_body = event.get("body")
    if raw_body:
        try:
            body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
        except Exception:
            body = {}

    # Routes
    # 1. POST /campaigns/presigned-url or /campaigns/upload-url
    if http_method == "POST" and (raw_path == "campaigns/presigned-url" or raw_path == "campaigns/upload-url"):
        return handle_presigned_url(body)

    # 2. GET /campaigns or /scheduled-calls
    if http_method == "GET" and (raw_path == "campaigns" or raw_path == "scheduled-calls"):
        return handle_list_campaigns()

    # 3. GET /campaigns/{campaign_id}/export
    if http_method == "GET" and raw_path.startswith("campaigns/") and raw_path.endswith("/export"):
        parts = raw_path.split("/")
        cid = parts[1]
        return handle_export_campaign_csv(cid)

    # 4. GET /campaigns/{campaign_id}/records
    if http_method == "GET" and raw_path.startswith("campaigns/") and raw_path.endswith("/records"):
        parts = raw_path.split("/")
        cid = parts[1]
        return handle_get_campaign_records(cid)

    # 5. POST /campaigns/{campaign_id}/cancel
    if http_method == "POST" and raw_path.startswith("campaigns/") and raw_path.endswith("/cancel"):
        parts = raw_path.split("/")
        cid = parts[1]
        return handle_cancel_campaign(cid)

    # Root / health check
    if raw_path in ("", "health"):
        return _build_response(200, {"status": "HEALTHY", "service": "cara-campaign-api"})

    return _build_response(404, {"error": f"Route not found: {http_method} /{raw_path}"})
