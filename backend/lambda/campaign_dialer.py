"""Cara Health Bot Sequential Campaign Dialer Lambda.

Triggered every minute by EventBridge Schedule (or manually).
Queries CareHarmony_OutreachRecords GSI CallStatusScheduledTimeIndex for due PENDING and RESCHEDULED records,
and originates SIP calls via the ECS Voice Gateway (/calls/originate) at the configured CPS rate.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

try:
    from outreach_records import DEFAULT_TABLE_NAME, OutreachRecordService, utc_now_iso
except ImportError:
    try:
        from storage.outreach_records import DEFAULT_TABLE_NAME, OutreachRecordService, utc_now_iso
    except ImportError:
        from backend.storage.outreach_records import DEFAULT_TABLE_NAME, OutreachRecordService, utc_now_iso

logger = logging.getLogger("campaign_dialer")
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("OUTREACH_RECORDS_TABLE", DEFAULT_TABLE_NAME)
INDEX_NAME = "CallStatusScheduledTimeIndex"
CPS_LIMIT = max(1, int(os.environ.get("CPS_LIMIT", "1")))
TRAFFIC_TYPE = os.environ.get("TRAFFIC_TYPE", "GENERAL")
_gateway_url_cache: Dict[str, Any] = {"url": None, "resolved_at": 0.0}
GATEWAY_URL_CACHE_TTL = 60  # seconds


def _get_dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def _discover_current_ecs_task_ip() -> str:
    """
    Queries ECS for the currently running careharmony-voice-
    gateway task and returns its public IP via the attached
    ENI. This is called on every dialer invocation since
    Fargate IPs are not stable across task restarts.
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    ecs = boto3.client("ecs", region_name=region)
    ec2 = boto3.client("ec2", region_name=region)

    task_arns = ecs.list_tasks(
        cluster="careharmony-voice-cluster",
        serviceName="careharmony-voice-service",
        desiredStatus="RUNNING",
    ).get("taskArns", [])

    if not task_arns:
        raise RuntimeError(
            "No RUNNING tasks found for careharmony-voice-service"
        )

    task = ecs.describe_tasks(
        cluster="careharmony-voice-cluster",
        tasks=[task_arns[0]],
    )["tasks"][0]

    eni_id = next(
        (
            d["value"]
            for a in task.get("attachments", [])
            for d in a.get("details", [])
            if d.get("name") == "networkInterfaceId"
        ),
        None,
    )
    if not eni_id:
        raise RuntimeError(
            f"Task {task_arns[0]} has no ElasticNetworkInterface attachment"
        )

    eni_desc = ec2.describe_network_interfaces(
        NetworkInterfaceIds=[eni_id]
    )["NetworkInterfaces"]
    if not eni_desc:
        raise RuntimeError(
            f"No network interface found for ENI {eni_id}"
        )

    public_ip = eni_desc[0].get("Association", {}).get("PublicIp")
    if not public_ip:
        raise RuntimeError(
            f"Task {task_arns[0]} has no public IP assigned (ENI {eni_id})"
        )

    return public_ip


def resolve_ecs_gateway_url() -> str:
    """
    Resolves the ECS gateway's current /calls/originate URL.
    Always queries the live ECS task's public IP dynamically —
    Fargate IPs change on every task restart/redeploy, so a
    cached/static URL goes stale immediately.

    GATEWAY_ORIGINATE_URL is only used as an override when
    explicitly set to a non-empty value that does NOT match
    the sentinel "AUTO" — this is for local testing against a
    fixed endpoint, not for production use.
    """
    now = time.time()
    if (
        _gateway_url_cache["url"]
        and now - _gateway_url_cache["resolved_at"] < GATEWAY_URL_CACHE_TTL
    ):
        return _gateway_url_cache["url"]

    override = os.environ.get("GATEWAY_ORIGINATE_URL", "").strip()
    if override and override.upper() != "AUTO":
        logger.warning(
            f"[DIALER_GATEWAY] Using STATIC override URL: "
            f"{override} — this will go stale on ECS restart. "
            f"Remove GATEWAY_ORIGINATE_URL env var or set it "
            f"to 'AUTO' to use dynamic discovery instead."
        )
        _gateway_url_cache["url"] = override
        _gateway_url_cache["resolved_at"] = now
        return override

    # Dynamic discovery — always current
    try:
        ip = _discover_current_ecs_task_ip()
        url = f"http://{ip}:8000/calls/originate"
        logger.info(f"[DIALER_GATEWAY] Resolved dynamic gateway URL: {url}")
        _gateway_url_cache["url"] = url
        _gateway_url_cache["resolved_at"] = now
        return url
    except Exception as e:
        logger.error(
            f"[DIALER_GATEWAY_ERROR] Dynamic ECS discovery failed: {e}"
        )
        raise


# PENDING covers new records and automatic retries; RESCHEDULED is a
# callback the patient asked for during a call.
DIALABLE_STATUSES = ("PENDING", "RESCHEDULED")


def query_due_records(table, now_iso: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Query GSI for dialable records whose scheduled_time <= now_iso."""
    due: List[Dict[str, Any]] = []
    for status in DIALABLE_STATUSES:
        remaining = limit - len(due)
        if remaining <= 0:
            break
        due.extend(_query_due_with_status(table, status, now_iso, remaining))
    return due


def _query_due_with_status(table, status: str, now_iso: str, limit: int) -> List[Dict[str, Any]]:
    try:
        response = table.query(
            IndexName=INDEX_NAME,
            KeyConditionExpression=Key("call_status").eq(status) & Key("scheduled_time").lte(now_iso),
            Limit=limit,
        )
        return response.get("Items", [])
    except ClientError as e:
        logger.error(f"Error querying GSI {INDEX_NAME}: {e}", exc_info=True)
        # Fallback to scan with filter expression
        try:
            scan_resp = table.scan(
                FilterExpression="call_status = :status AND scheduled_time <= :now",
                ExpressionAttributeValues={":status": status, ":now": now_iso},
                Limit=limit,
            )
            return scan_resp.get("Items", [])
        except Exception as scan_err:
            logger.error(f"Fallback scan failed: {scan_err}", exc_info=True)
            return []

def sanitize_dialer_error(raw_error: str) -> str:
    """
    Maps raw network/HTTP exception strings from the dialer's
    own urlopen() call into a clean, consistent failure reason
    category — matching the style of SIP_FAILURE_REASONS in
    sip_transport.py, but for pre-SIP connectivity failures.
    """
    err_lower = (raw_error or "").lower()

    if "timed out" in err_lower or "timeout" in err_lower:
        return "GATEWAY_TIMEOUT"
    if "connection refused" in err_lower:
        return "GATEWAY_REFUSED"
    if "name or service not known" in err_lower or "getaddrinfo" in err_lower:
        return "GATEWAY_DNS_FAILURE"
    if "no route to host" in err_lower:
        return "GATEWAY_UNREACHABLE"
    if "http error 5" in err_lower:
        return "GATEWAY_SERVER_ERROR"
    if "http error 4" in err_lower:
        return "GATEWAY_REQUEST_REJECTED"

    logger.warning(
        f"[DIALER_ERROR_UNMAPPED] Unrecognized dialer error "
        f"pattern: {raw_error!r} — using generic GATEWAY_ERROR"
    )
    return "GATEWAY_ERROR"


def originate_single_call(gateway_url: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch HTTP request to ECS Voice Gateway to originate an outbound SIP call."""
    payload = {
        "destination_phone": record.get("phone_number", ""),
        "patient_first_name": record.get("first_name", "Valued"),
        "patient_last_name": record.get("last_name", "Patient"),
        "practice_name": record.get("practice_name", "Metro Health Clinic"),
        "practice_callback_number": record.get("practice_callback_number", "2819568434"),
        "provider_name": record.get("provider_name", ""),
        "patient_id": record.get("patient_id", ""),
        "empi": record.get("empi", record.get("patient_id", "")),
        "call_mode": record.get("call_mode", "MODE_2_NORMAL_COACHING"),
        "human_agent_phone": record.get("human_agent_phone", ""),
        "timezone": record.get("timezone", ""),
    }

    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        gateway_url,
        data=req_data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8")
            resp_json = json.loads(resp_body) if resp_body else {}
            return {
                "success": True,
                "status_code": resp.status,
                "call_id": resp_json.get("call_id") or resp_json.get("contact_id") or "sip-originated",
                "response": resp_json,
            }
    except urllib.error.HTTPError as he:
        err_body = he.read().decode("utf-8", errors="replace")
        logger.error(f"[DIALER_HTTP_ERR] HTTP {he.code} for patient {record.get('patient_id')}: {err_body}")
        return {"success": False, "status_code": he.code, "error": err_body}
    except Exception as e:
        logger.error(f"[DIALER_NET_ERR] Error contacting gateway for patient {record.get('patient_id')}: {e}")
        return {"success": False, "error": str(e)}


def lambda_handler(event: Dict[str, Any] = None, context: Any = None) -> Dict[str, Any]:
    """Execute batch campaign dialer pass."""
    now_iso = utc_now_iso()
    logger.info(f"[DIALER_START] Invocation at {now_iso} (Traffic: {TRAFFIC_TYPE}, CPS: {CPS_LIMIT})")

    ddb = _get_dynamodb_resource()
    table = ddb.Table(TABLE_NAME)
    storage_svc = OutreachRecordService(TABLE_NAME)
    gateway_url = resolve_ecs_gateway_url()

    logger.info(f"[DIALER_GATEWAY] Using Gateway URL: {gateway_url}")

    due_records = query_due_records(table, now_iso, limit=50)
    logger.info(f"[DIALER_FOUND] Found {len(due_records)} due records to dial.")

    if not due_records:
        return {
            "statusCode": 200,
            "dialed_count": 0,
            "message": "No due records found at this time.",
            "timestamp": now_iso,
        }

    delay_seconds = 1.0 / CPS_LIMIT
    dialed_count = 0
    failed_count = 0
    results = []

    for rec in due_records:
        pid = rec["patient_id"]
        phone = rec.get("phone_number", "")
        current_attempts = int(rec.get("attempt_count", 0))

        logger.info(f"[DIALING_ATTEMPT] Originating call to patient_id={pid} phone={phone}...")

        call_res = originate_single_call(gateway_url, rec)
        update_ts = utc_now_iso()

        if call_res.get("success"):
            new_status = "DIALING"
            dialed_count += 1
            call_id = call_res.get("call_id", "")
            logger.info(f"[DIALER] patient_id={pid} status=DIALING call_id={call_id}")

            try:
                table.update_item(
                    Key={"patient_id": pid},
                    UpdateExpression="SET call_status = :st, attempt_count = :ac, call_id = :cid, last_updated_at = :ts",
                    ExpressionAttributeValues={
                        ":st": new_status,
                        ":ac": current_attempts + 1,
                        ":cid": call_id,
                        ":ts": update_ts,
                    },
                )
            except Exception as ddb_err:
                logger.warning(f"DynamoDB status update error for {pid}: {ddb_err}")

            results.append({"patient_id": pid, "status": "DIALING", "call_id": call_id})

        else:
            new_status = "FAILED"
            failed_count += 1
            raw_err_msg = str(call_res.get("error", "Call origination failed"))
            sanitized_reason = sanitize_dialer_error(raw_err_msg)

            logger.error(
                f"[DIALER_ERROR] patient_id={pid} "
                f"reason={sanitized_reason} raw_error={raw_err_msg}"
            )

            try:
                asyncio.run(
                    storage_svc.schedule_retry(
                        patient_id=pid,
                        call_id="",  # no call_id exists — never reached SIP layer
                        reason=sanitized_reason,
                        sip_code=None,
                        count_attempt=True,
                    )
                )
            except Exception:
                logger.exception(
                    f"[DIALER_ERROR] schedule_retry failed for patient_id={pid}"
                )

            results.append({"patient_id": pid, "status": "FAILED", "error": sanitized_reason})

        # Pacing delay between calls according to CPS limit
        time.sleep(delay_seconds)

    logger.info(f"[DIALER_COMPLETE] Completed dialing run. Dialed: {dialed_count}, Failed: {failed_count}")

    return {
        "statusCode": 200,
        "dialed_count": dialed_count,
        "failed_count": failed_count,
        "results": results,
        "timestamp": utc_now_iso(),
    }
