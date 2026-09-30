#!/usr/bin/env python3
"""Time-to-First-Audio (TTFA) Logging & Analysis Tool.

Measures the delay between a contact connecting to the system and the
first audio (greeting or prompt) actually reaching the caller.
Read-only analysis tool — makes no AWS write calls and modifies no resources.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

ROOT = Path(__file__).resolve().parents[1]

# Audio-producing flow module types in Amazon Connect
AUDIO_MODULE_TYPES = {
    "PlayPrompt",
    "GetUserInput",
    "MessageParticipant",
    "StoreCustomerInput",
}


@dataclass
class ContactTTFAResult:
    contact_id: str
    batch_id: str | None = None
    patient_id: str | None = None
    customer_name: str | None = None
    phone_number: str | None = None
    t0_iso: str | None = None
    t0_ms: int | None = None
    t0_source: str | None = None
    t1_iso: str | None = None
    t1_ms: int | None = None
    t1_source: str | None = None
    ttfa_ms: int | None = None
    signal_used: str = "UNKNOWN"
    audio_module_type: str | None = None
    audio_module_id: str | None = None
    audio_prompt_text: str | None = None
    first_flow_module_type: str | None = None
    first_flow_module_ms: int | None = None
    first_lambda_ms: int | None = None
    first_lambda_line: str | None = None
    call_duration_ms: int | None = None
    amd_status: str | None = None
    disconnect_reason: str | None = None
    warning_flag: str = "OK"
    error: str | None = None
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    raw_lambda_logs: list[dict[str, Any]] = field(default_factory=list)
    raw_contact_record: dict[str, Any] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Measure Time-To-First-Audio (TTFA) per call from Amazon Connect CTR and CloudWatch flow logs."
    )
    p.add_argument(
        "contact_ids",
        nargs="*",
        help="One or more ContactIds, comma-separated IDs, or path to a file containing ContactIds",
    )
    p.add_argument(
        "-b",
        "--batch-id",
        type=str,
        default=None,
        help="Campaign/Outreach Batch ID (e.g. camp-20260909164131-ada16174) to query all associated contact IDs from DynamoDB",
    )
    p.add_argument(
        "-f",
        "--file",
        type=str,
        help="Path to file containing ContactIds (one per line)",
    )
    p.add_argument(
        "--profile",
        type=str,
        default=None,
        help="AWS CLI profile name (default: environment / careharmony-main / default)",
    )
    p.add_argument(
        "--region",
        type=str,
        default=None,
        help="AWS region (default: from deployment-state.json or us-east-1)",
    )
    p.add_argument(
        "--t0",
        choices=["connected", "initiation"],
        default="connected",
        help="T0 reference timestamp: 'connected' (ConnectedToSystemTimestamp, fallback InitiationTimestamp) or 'initiation' (InitiationTimestamp)",
    )
    p.add_argument(
        "--threshold-ms",
        type=int,
        default=3000,
        help="TTFA anomaly warning threshold in milliseconds (default: 3000 ms)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print detailed raw CTR events and CloudWatch logs for auditability",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON results instead of tabular text",
    )
    p.add_argument(
        "--array",
        action="store_true",
        help="Output pure JSON array of objects (one object per contact) against batch/contact IDs",
    )
    return p.parse_args()


def load_deployment_state() -> dict[str, Any]:
    candidates = [
        ROOT / "deployment-state.json",
        Path.cwd() / "deployment-state.json",
        Path.cwd() / "backend" / "deployment-state.json",
    ]
    for path in candidates:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def load_outputs() -> dict[str, Any]:
    state = load_deployment_state()
    return state.get("outputs", {})


def get_patient_records_table_name() -> str:
    state = load_deployment_state()
    campaign_cfg = state.get("campaign", {})
    resources = state.get("resources", {})
    return (
        campaign_cfg.get("PatientRecordsTableName")
        or resources.get("PatientRecordsTableName")
        or "TalkingBotPatientRecords-dev"
    )


def fetch_contacts_from_batch(
    session: boto3.Session,
    region: str,
    batch_id: str,
    table_name: str,
) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Attr

    dynamodb = session.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)
    try:
        resp = table.scan(FilterExpression=Attr("batchId").eq(batch_id))
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(
                FilterExpression=Attr("batchId").eq(batch_id),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
        items.sort(key=lambda x: str(x.get("patientId", "")))
        return items
    except Exception as ex:
        print(f"Warning: Failed scanning DynamoDB table {table_name} for batchId {batch_id}: {ex}", file=sys.stderr)
        return []


def get_boto3_session(profile: str | None = None) -> boto3.Session:
    if profile:
        return boto3.Session(profile_name=profile)
    # Check if AWS_PROFILE is set
    if os.environ.get("AWS_PROFILE"):
        return boto3.Session()
    # Try careharmony-main if available, else default session
    try:
        return boto3.Session(profile_name="careharmony-main")
    except Exception:
        return boto3.Session()


def collect_contact_ids(args: argparse.Namespace) -> list[str]:
    raw_ids: list[str] = []
    if args.file:
        file_path = Path(args.file)
        if not file_path.is_file():
            raise FileNotFoundError(f"Contact ID file not found: {args.file}")
        for line in file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw_ids.append(line)

    for item in args.contact_ids:
        # Check if argument is an existing text file
        p = Path(item)
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    raw_ids.append(line)
        elif "," in item:
            for part in item.split(","):
                part = part.strip()
                if part:
                    raw_ids.append(part)
        else:
            item = item.strip()
            if item:
                raw_ids.append(item)

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for cid in raw_ids:
        clean = cid.strip().strip("'\"")
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def to_epoch_ms(dt_val: dt.datetime | None) -> int | None:
    if not dt_val:
        return None
    if isinstance(dt_val, (int, float)):
        return int(dt_val) if dt_val < 10_000_000_000 else int(dt_val)
    if isinstance(dt_val, dt.datetime):
        return int(dt_val.timestamp() * 1000)
    return None


def to_iso_utc(dt_val: dt.datetime | int | None) -> str | None:
    if dt_val is None:
        return None
    if isinstance(dt_val, (int, float)):
        return dt.datetime.fromtimestamp(dt_val / 1000.0, tz=dt.timezone.utc).isoformat()
    if isinstance(dt_val, dt.datetime):
        val = dt_val if dt_val.tzinfo else dt_val.replace(tzinfo=dt.timezone.utc)
        return val.astimezone(dt.timezone.utc).isoformat()
    return str(dt_val)


def analyze_contact(
    connect_client: Any,
    logs_client: Any,
    instance_id: str,
    connect_log_group: str,
    lambda_log_group: str | None,
    contact_id: str,
    t0_pref: str = "connected",
    threshold_ms: int = 3000,
) -> ContactTTFAResult:
    res = ContactTTFAResult(contact_id=contact_id)

    # Step 1: Describe Contact
    try:
        contact_desc = connect_client.describe_contact(
            InstanceId=instance_id, ContactId=contact_id
        )
        record = contact_desc.get("Contact", {})
        res.raw_contact_record = record
    except ClientError as ex:
        res.error = f"Connect DescribeContact failed: {ex.response.get('Error', {}).get('Message', str(ex))}"
        res.warning_flag = "ERROR_DESCRIBE"
        return res
    except Exception as ex:
        res.error = f"DescribeContact error: {ex}"
        res.warning_flag = "ERROR_DESCRIBE"
        return res

    init_ts = record.get("InitiationTimestamp")
    conn_ts = record.get("ConnectedToSystemTimestamp")
    disc_ts = record.get("DisconnectTimestamp")
    res.amd_status = record.get("AnsweringMachineDetectionStatus")
    res.disconnect_reason = record.get("DisconnectReason")

    init_ms = to_epoch_ms(init_ts)
    conn_ms = to_epoch_ms(conn_ts)
    disc_ms = to_epoch_ms(disc_ts)

    if init_ms and disc_ms:
        res.call_duration_ms = disc_ms - init_ms

    # Determine T0
    if t0_pref == "initiation":
        if init_ms:
            res.t0_ms = init_ms
            res.t0_iso = to_iso_utc(init_ts)
            res.t0_source = "CTR.InitiationTimestamp"
        elif conn_ms:
            res.t0_ms = conn_ms
            res.t0_iso = to_iso_utc(conn_ts)
            res.t0_source = "CTR.ConnectedToSystemTimestamp"
    else:
        # Default: ConnectedToSystemTimestamp (fallback to InitiationTimestamp)
        if conn_ms:
            res.t0_ms = conn_ms
            res.t0_iso = to_iso_utc(conn_ts)
            res.t0_source = "CTR.ConnectedToSystemTimestamp"
        elif init_ms:
            res.t0_ms = init_ms
            res.t0_iso = to_iso_utc(init_ts)
            res.t0_source = "CTR.InitiationTimestamp (fallback: not connected yet)"

    # Step 2: Query CloudWatch Connect Flow Logs
    base_ref_ms = init_ms or conn_ms or int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    query_start_ms = max(0, base_ref_ms - 15_000)
    query_end_ms = (disc_ms or base_ref_ms + 120_000) + 15_000

    flow_events: list[dict[str, Any]] = []
    try:
        req = {
            "logGroupName": connect_log_group,
            "startTime": query_start_ms,
            "endTime": query_end_ms,
            "filterPattern": f'"{contact_id}"',
            "limit": 100,
        }
        pages = 0
        prev_token = None
        while pages < 5:
            resp = logs_client.filter_log_events(**req)
            events = resp.get("events", [])
            flow_events.extend(events)
            token = resp.get("nextToken")
            if not token or token == prev_token:
                break
            prev_token = token
            req["nextToken"] = token
            pages += 1
    except Exception as ex:
        res.error = (res.error + " | " if res.error else "") + f"Connect logs query error: {ex}"

    flow_events.sort(key=lambda x: x.get("timestamp", 0))
    res.raw_events = flow_events

    # Step 3: Query Lambda logs for cross-reference (if verbose and log group configured)
    if lambda_log_group:
        lambda_events: list[dict[str, Any]] = []
        try:
            l_req = {
                "logGroupName": lambda_log_group,
                "startTime": query_start_ms,
                "endTime": query_end_ms,
                "filterPattern": f'"{contact_id}"',
                "limit": 20,
            }
            l_resp = logs_client.filter_log_events(**l_req)
            lambda_events.extend(l_resp.get("events", []))
        except Exception:
            pass
        lambda_events.sort(key=lambda x: x.get("timestamp", 0))
        res.raw_lambda_logs = lambda_events
        if lambda_events:
            first_l = lambda_events[0]
            res.first_lambda_ms = first_l.get("timestamp")
            res.first_lambda_line = first_l.get("message", "").strip()

    # Step 4: Extract T1 (first audio block or earliest flow block)
    first_flow_block_ms: int | None = None
    first_flow_block_type: str | None = None

    first_audio_block_ms: int | None = None
    first_audio_block_type: str | None = None
    first_audio_block_id: str | None = None
    first_audio_prompt_text: str | None = None

    for event in flow_events:
        ts = event.get("timestamp")
        msg_str = event.get("message", "")
        try:
            parsed = json.loads(msg_str)
        except Exception:
            parsed = {}

        mod_type = parsed.get("ContactFlowModuleType")
        ident = parsed.get("Identifier")
        params = parsed.get("Parameters", {})

        if mod_type and first_flow_block_ms is None:
            first_flow_block_ms = ts
            first_flow_block_type = mod_type

        # Check if this is an audio block (PlayPrompt / GetUserInput / etc.)
        if mod_type in AUDIO_MODULE_TYPES:
            # We want the first invocation/execution of the block (Parameters usually present)
            if first_audio_block_ms is None:
                first_audio_block_ms = ts
                first_audio_block_type = mod_type
                first_audio_block_id = ident
                if isinstance(params, dict):
                    first_audio_prompt_text = (
                        params.get("Text")
                        or params.get("SSML")
                        or params.get("PromptId")
                        or params.get("AudioId")
                    )

    res.first_flow_module_ms = first_flow_block_ms
    res.first_flow_module_type = first_flow_block_type

    if first_audio_block_ms is not None:
        res.t1_ms = first_audio_block_ms
        res.t1_iso = to_iso_utc(first_audio_block_ms)
        res.t1_source = f"ConnectFlowLog.{first_audio_block_type}"
        res.audio_module_type = first_audio_block_type
        res.audio_module_id = first_audio_block_id
        res.audio_prompt_text = first_audio_prompt_text
        res.signal_used = f"T0={res.t0_source} -> T1=ConnectFlowLog.{first_audio_block_type}"
    elif first_flow_block_ms is not None:
        # Fallback if no audio block executed
        res.t1_ms = first_flow_block_ms
        res.t1_iso = to_iso_utc(first_flow_block_ms)
        res.t1_source = f"ConnectFlowLog.{first_flow_block_type} (fallback: earliest flow block)"
        res.signal_used = f"T0={res.t0_source} -> T1=ConnectFlowLog.{first_flow_block_type} (non-audio fallback)"
    elif res.first_lambda_ms is not None:
        # Fallback if lambda executed but flow logs omitted
        res.t1_ms = res.first_lambda_ms
        res.t1_iso = to_iso_utc(res.first_lambda_ms)
        res.t1_source = "Lambda.SessionContext (fallback: first Lambda invocation)"
        res.signal_used = f"T0={res.t0_source} -> T1=Lambda (fallback)"
    else:
        res.signal_used = "NO_FLOW_OR_AUDIO_EVENTS"

    # Step 5: Compute TTFA and assess warnings
    warnings: list[str] = []
    if res.t0_ms is None:
        warnings.append("MISSING_T0")
    if res.t1_ms is None:
        warnings.append("MISSING_T1")

    if res.t0_ms is not None and res.t1_ms is not None:
        res.ttfa_ms = res.t1_ms - res.t0_ms
        if res.ttfa_ms < 0:
            warnings.append(f"NEGATIVE_TTFA({res.ttfa_ms}ms)")
        elif res.call_duration_ms is not None and res.ttfa_ms > res.call_duration_ms:
            warnings.append(f"EXCEEDS_DURATION(TTFA={res.ttfa_ms}ms>Duration={res.call_duration_ms}ms)")
        elif res.ttfa_ms > threshold_ms:
            warnings.append(f"HIGH_TTFA(>{threshold_ms}ms)")

    if res.amd_status:
        warnings.append(f"AMD={res.amd_status}")

    res.warning_flag = ", ".join(warnings) if warnings else "OK"
    return res


def compute_statistics(results: list[ContactTTFAResult]) -> dict[str, Any]:
    valid_ttfas = [r.ttfa_ms for r in results if r.ttfa_ms is not None and r.ttfa_ms >= 0]
    total = len(results)
    valid_count = len(valid_ttfas)
    missing_or_anomalous_count = sum(
        1 for r in results if r.warning_flag != "OK" or r.ttfa_ms is None
    )

    if not valid_ttfas:
        return {
            "total_calls": total,
            "valid_ttfa_calls": 0,
            "anomalous_or_missing": missing_or_anomalous_count,
            "min_ms": None,
            "max_ms": None,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
        }

    valid_ttfas.sort()
    n = len(valid_ttfas)

    def percentile(data: list[int], p: float) -> float:
        if not data:
            return 0.0
        k = (len(data) - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return float(data[int(k)])
        return data[f] * (c - k) + data[c] * (k - f)

    min_val = valid_ttfas[0]
    max_val = valid_ttfas[-1]
    avg_val = sum(valid_ttfas) / n
    p50_val = percentile(valid_ttfas, 0.50)
    p95_val = percentile(valid_ttfas, 0.95)

    return {
        "total_calls": total,
        "valid_ttfa_calls": valid_count,
        "anomalous_or_missing": missing_or_anomalous_count,
        "min_ms": min_val,
        "max_ms": max_val,
        "avg_ms": round(avg_val, 1),
        "p50_ms": round(p50_val, 1),
        "p95_ms": round(p95_val, 1),
    }


def print_tabular_results(results: list[ContactTTFAResult], threshold_ms: int) -> None:
    headers = ["ContactId", "T0 (ISO UTC)", "T1 (ISO UTC)", "TTFA (ms)", "Signal Used", "Status / Warning"]
    rows: list[list[str]] = []

    for r in results:
        cid_str = r.contact_id
        t0_str = r.t0_iso or "MISSING"
        t1_str = r.t1_iso or "MISSING"
        ttfa_str = f"{r.ttfa_ms} ms" if r.ttfa_ms is not None else "N/A"
        sig_str = r.t1_source or r.signal_used
        warn_str = r.warning_flag

        rows.append([cid_str, t0_str, t1_str, ttfa_str, sig_str, warn_str])

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    hdr_line = "| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |"

    print("\n" + sep)
    print(hdr_line)
    print(sep)
    for row in rows:
        row_line = "| " + " | ".join(str(row[i]).ljust(widths[i]) for i in range(len(row))) + " |"
        print(row_line)
    print(sep + "\n")


def print_summary_table(stats: dict[str, Any]) -> None:
    print("=== BATCH TTFA SUMMARY ===")
    print(f"Total Calls Analyzed:      {stats['total_calls']}")
    print(f"Calls with Valid TTFA:     {stats['valid_ttfa_calls']}")
    print(f"Calls Anomalous / Missing: {stats['anomalous_or_missing']}")
    if stats["valid_ttfa_calls"] > 0:
        print(f"Min TTFA:                  {stats['min_ms']} ms")
        print(f"Max TTFA:                  {stats['max_ms']} ms")
        print(f"Average TTFA:              {stats['avg_ms']} ms")
        print(f"P50 (Median) TTFA:         {stats['p50_ms']} ms")
        print(f"P95 TTFA:                  {stats['p95_ms']} ms")
    print("==========================\n")


def print_verbose_details(results: list[ContactTTFAResult]) -> None:
    print("\n" + "=" * 80)
    print("DETAILED VERBOSE AUDIT LOG")
    print("=" * 80)

    for r in results:
        print(f"\n--- [ContactId: {r.contact_id}] ---")
        print(f"  T0 (Reference):        {r.t0_iso} ({r.t0_ms} ms) [{r.t0_source}]")
        print(f"  T1 (Audio / Flow):     {r.t1_iso} ({r.t1_ms} ms) [{r.t1_source}]")
        print(f"  TTFA:                  {r.ttfa_ms} ms")
        print(f"  Status / Warning:      {r.warning_flag}")
        if r.audio_prompt_text:
            print(f"  First Audio Prompt:    {r.audio_prompt_text!r}")
        if r.first_flow_module_type:
            print(f"  First Flow Module:     {r.first_flow_module_type} at {to_iso_utc(r.first_flow_module_ms)}")
        if r.first_lambda_ms:
            print(f"  First Lambda Call:     {to_iso_utc(r.first_lambda_ms)}")
            print(f"    Lambda Log Snippet:  {r.first_lambda_line}")
        if r.error:
            print(f"  Encountered Error:     {r.error}")

        print("\n  [Raw CTR Contact Record Timestamps & Details]")
        rec = r.raw_contact_record
        for k in ["InitiationTimestamp", "ConnectedToSystemTimestamp", "DisconnectTimestamp",
                  "AnsweringMachineDetectionStatus", "DisconnectReason", "InitiationMethod"]:
            if k in rec:
                print(f"    {k}: {rec.get(k)}")

        print(f"\n  [Raw Connect Flow Events ({len(r.raw_events)} events)]")
        for idx, ev in enumerate(r.raw_events):
            ts = ev.get("timestamp")
            dt_iso = to_iso_utc(ts)
            msg_str = ev.get("message", "")
            try:
                msg = json.loads(msg_str)
                mod = msg.get("ContactFlowModuleType")
                ident = msg.get("Identifier")
                res_val = msg.get("Results")
                params = msg.get("Parameters")
                err = msg.get("ErrorDetails")
                print(f"    [{idx:02d}] {ts} ({dt_iso}) | Module: {mod} | ID: {ident} | Results: {res_val}")
                if err:
                    print(f"         ERROR: {err}")
                if params:
                    print(f"         Parameters: {params}")
            except Exception:
                print(f"    [{idx:02d}] {ts} ({dt_iso}) | {msg_str}")

        if r.raw_lambda_logs:
            print(f"\n  [Raw Lambda Logs ({len(r.raw_lambda_logs)} lines)]")
            for idx, lev in enumerate(r.raw_lambda_logs[:10]):
                print(f"    [{idx:02d}] {lev.get('timestamp')} | {lev.get('message', '').strip()}")
            if len(r.raw_lambda_logs) > 10:
                print(f"    ... ({len(r.raw_lambda_logs) - 10} more Lambda log lines omitted)")

        print("-" * 80)


def main() -> int:
    args = parse_args()

    try:
        outputs = load_outputs()
        region = args.region or outputs.get("Region", "us-east-1")
        instance_id = outputs.get("InstanceId") or outputs.get("connectInstanceId")
        connect_log_group = outputs.get("ConnectLogGroup") or outputs.get("connectLogGroup") or f"/aws/connect/cara-health-bot-258673"
        lambda_log_group = "/aws/lambda/cara-health-bot-session-context"

        if not instance_id:
            config_path = ROOT / "config.json"
            if config_path.is_file():
                try:
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                    instance_id = cfg.get("connectInstanceId") or cfg.get("instanceId")
                except Exception:
                    pass

        if not instance_id:
            print("Error: Could not determine Connect InstanceId from deployment-state.json or config.json.", file=sys.stderr)
            return 2

        session = get_boto3_session(args.profile)
        table_name = get_patient_records_table_name()
        batch_metadata: dict[str, dict[str, Any]] = {}

        contact_ids = collect_contact_ids(args)

        if args.batch_id:
            batch_items = fetch_contacts_from_batch(session, region, args.batch_id, table_name)
            if not batch_items:
                print(f"Warning: No patient records found in table '{table_name}' for batch ID '{args.batch_id}'.", file=sys.stderr)
            for item in batch_items:
                cid = item.get("contactId")
                if cid:
                    clean_cid = str(cid).strip().strip("'\"")
                    if clean_cid:
                        if clean_cid not in contact_ids:
                            contact_ids.append(clean_cid)
                        batch_metadata[clean_cid] = {
                            "batch_id": item.get("batchId") or args.batch_id,
                            "patient_id": item.get("patientId"),
                            "customer_name": item.get("customerName") or item.get("patientName"),
                            "phone_number": item.get("expectedPhone") or item.get("phone") or item.get("phoneNumber"),
                        }

        if not contact_ids:
            print("Error: No ContactId provided or found. Pass one or more ContactIds, --file <path>, or --batch-id <id>.", file=sys.stderr)
            return 1

        connect_client = session.client("connect", region_name=region)
        logs_client = session.client("logs", region_name=region)

        results: list[ContactTTFAResult] = []
        for cid in contact_ids:
            res = analyze_contact(
                connect_client=connect_client,
                logs_client=logs_client,
                instance_id=instance_id,
                connect_log_group=connect_log_group,
                lambda_log_group=lambda_log_group,
                contact_id=cid,
                t0_pref=args.t0,
                threshold_ms=args.threshold_ms,
            )
            if cid in batch_metadata:
                meta = batch_metadata[cid]
                res.batch_id = meta.get("batch_id")
                res.patient_id = meta.get("patient_id")
                res.customer_name = meta.get("customer_name")
                res.phone_number = meta.get("phone_number")
            elif args.batch_id:
                res.batch_id = args.batch_id
            results.append(res)

        stats = compute_statistics(results)

        contact_dicts = [
            {
                "batch_id": r.batch_id,
                "patient_id": r.patient_id,
                "customer_name": r.customer_name,
                "phone_number": r.phone_number,
                "contact_id": r.contact_id,
                "t0_iso": r.t0_iso,
                "t0_ms": r.t0_ms,
                "t0_source": r.t0_source,
                "t1_iso": r.t1_iso,
                "t1_ms": r.t1_ms,
                "t1_source": r.t1_source,
                "ttfa_ms": r.ttfa_ms,
                "signal_used": r.signal_used,
                "audio_module_type": r.audio_module_type,
                "audio_module_id": r.audio_module_id,
                "audio_prompt_text": r.audio_prompt_text,
                "first_flow_module_type": r.first_flow_module_type,
                "first_flow_module_ms": r.first_flow_module_ms,
                "first_lambda_ms": r.first_lambda_ms,
                "call_duration_ms": r.call_duration_ms,
                "amd_status": r.amd_status,
                "disconnect_reason": r.disconnect_reason,
                "warning_flag": r.warning_flag,
                "error": r.error,
            }
            for r in results
        ]

        if args.array:
            print(json.dumps(contact_dicts, indent=2, default=str))
            return 0

        if args.json:
            output_data = {
                "summary": stats,
                "contacts": contact_dicts,
            }
            if args.verbose:
                for i, r in enumerate(results):
                    output_data["contacts"][i]["raw_events"] = r.raw_events
                    output_data["contacts"][i]["raw_lambda_logs"] = r.raw_lambda_logs
                    output_data["contacts"][i]["raw_contact_record"] = r.raw_contact_record
            print(json.dumps(output_data, indent=2, default=str))
            return 0

        # Print standard table
        print_tabular_results(results, args.threshold_ms)

        # Print summary if multiple contacts
        if len(results) > 1:
            print_summary_table(stats)

        # Print verbose if requested
        if args.verbose:
            print_verbose_details(results)

        return 0

    except NoCredentialsError as err:
        print(f"AWS credentials error: {err}", file=sys.stderr)
        return 3
    except (ClientError, BotoCoreError) as err:
        print(f"AWS error: {err}", file=sys.stderr)
        return 3
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
