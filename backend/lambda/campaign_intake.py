"""Cara Health Bot campaign intake Lambda.

Triggered by S3 ObjectCreated event on campaigns/<campaignId>/patients.csv.
Parses CSV, validates required patient headers, normalizes phone numbers,
and writes full patient records into DynamoDB CareHarmony_OutreachRecords.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import unquote, unquote_plus

import boto3
from botocore.exceptions import ClientError

try:
    from outreach_records import (
        DEFAULT_TABLE_NAME,
        generate_campaign_id,
        normalize_phone,
        utc_now_iso,
        write_patient_record,
    )
except ImportError:
    try:
        from storage.outreach_records import (
            DEFAULT_TABLE_NAME,
            generate_campaign_id,
            normalize_phone,
            utc_now_iso,
            write_patient_record,
        )
    except ImportError:
        from backend.storage.outreach_records import (
            DEFAULT_TABLE_NAME,
            generate_campaign_id,
            normalize_phone,
            utc_now_iso,
            write_patient_record,
        )

logger = logging.getLogger("campaign_intake")
logger.setLevel(logging.INFO)

REQUIRED_HEADERS = [
    "empi",
    "first_name",
    "last_name",
    "phone_number",
    "practice_name",
    "practice_callback_number",
]

MAX_ROWS_LIMIT = 1000


def _clean_header_key(name: str) -> str:
    """Normalize header string for fuzzy matching (strip BOM, non-alphanumeric, lowercase)."""
    return re.sub(r"[^a-z0-9]", "", (name or "").replace("\ufeff", "").strip().lower())


def _build_header_map(fieldnames: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """Build canonical mapping from standard field names to actual CSV column headers."""
    available = {_clean_header_key(f): f for f in fieldnames if f}
    mapping: Dict[str, str] = {}
    missing: List[str] = []

    for req in REQUIRED_HEADERS:
        match = available.get(_clean_header_key(req))
        if match:
            mapping[req] = match
        else:
            missing.append(req)

    # Optional columns
    for opt in ["gender", "provider_name", "provider", "call_mode"]:
        match = available.get(_clean_header_key(opt))
        if match:
            canon_key = "provider_name" if opt == "provider" else opt
            mapping[canon_key] = match

    return mapping, missing


def parse_campaign_id_from_key(s3_key: str) -> str:
    """Extract campaign_id from S3 path campaigns/<campaign_id>/patients.csv."""
    parts = s3_key.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "campaigns" and parts[-1].lower().endswith(".csv"):
        return parts[1].strip()
    elif len(parts) >= 2 and parts[0] == "campaigns":
        return parts[1].strip()
    return generate_campaign_id()


def validate_empi_uniqueness(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Checks for duplicate empi values within a single CSV upload.
    Returns (valid_rows, error_rows). Only the FIRST occurrence
    of a duplicated empi is kept as valid; subsequent occurrences
    are flagged as errors.
    """
    seen_empi = set()
    valid_rows = []
    error_rows = []

    for row_number, row in enumerate(rows, start=2):  # header is row 1
        empi = (row.get("empi") or "").strip()
        if not empi:
            error_rows.append({
                "row_number": row_number,
                "empi": empi,
                "error": "Missing empi",
            })
            continue
        if empi in seen_empi:
            error_rows.append({
                "row_number": row_number,
                "empi": empi,
                "error": "Duplicate empi within this CSV (first seen earlier in the same file)",
            })
            continue
        seen_empi.add(empi)
        valid_rows.append(row)

    return valid_rows, error_rows


def process_csv_content(
    csv_text: str,
    campaign_id: str,
    scheduled_time: str,
    file_size_bytes: int = 0,
    table_name: str = DEFAULT_TABLE_NAME,
    dynamodb_resource: Any = None,
    source_filename: str = "patients.csv",
    direct_agent: bool = False,
    human_agent_phone: str = "",
    campaign_timezone: str = "",
) -> Dict[str, Any]:
    """Parse, validate, and write all CSV patient rows into DynamoDB.

    direct_agent is the campaign-level "Direct to Human Agent" toggle; it
    overrides any per-row call_mode column.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames or []
    header_map, missing_headers = _build_header_map(fieldnames)

    if missing_headers:
        error_msg = f"Missing required CSV columns: {', '.join(missing_headers)}"
        logger.error(f"[INTAKE] {error_msg}")
        return {
            "success": False,
            "campaign_id": campaign_id,
            "status": "FAILED",
            "error": error_msg,
            "total_rows": 0,
            "valid_rows": 0,
            "skipped_rows": 0,
            "error_rows": [],
        }

    raw_rows = list(reader)
    total_rows = len(raw_rows)

    if total_rows == 0:
        error_msg = "CSV file contains 0 data rows."
        logger.error(f"[INTAKE] {error_msg}")
        return {
            "success": False,
            "campaign_id": campaign_id,
            "status": "FAILED",
            "error": error_msg,
            "total_rows": 0,
            "valid_rows": 0,
            "skipped_rows": 0,
            "error_rows": [],
        }

    if total_rows > MAX_ROWS_LIMIT:
        error_msg = f"Exceeds {MAX_ROWS_LIMIT} row limit (found {total_rows} rows)."
        logger.error(f"[INTAKE] {error_msg}")
        return {
            "success": False,
            "campaign_id": campaign_id,
            "status": "FAILED",
            "error": error_msg,
            "total_rows": total_rows,
            "valid_rows": 0,
            "skipped_rows": total_rows,
            "error_rows": [],
        }

    mapped_rows = []
    for raw_row in raw_rows:
        row_dict: Dict[str, Any] = {}
        for canon_k, actual_col in header_map.items():
            row_dict[canon_k] = str(raw_row.get(actual_col) or "").strip()
        if direct_agent:
            row_dict["call_mode"] = "MODE_1_DIRECT_HANDOFF"
        if human_agent_phone:
            row_dict["human_agent_phone"] = human_agent_phone
        if campaign_timezone:
            row_dict["timezone"] = campaign_timezone
        mapped_rows.append(row_dict)

    # 1. Pre-pass validation: detect duplicate empi values within the same CSV upload
    valid_rows, dup_errors = validate_empi_uniqueness(mapped_rows)
    all_errors = list(dup_errors)
    results = []
    valid_count = 0

    for row_dict in valid_rows:
        # Write patient record (handles validation, title-casing, E.164 conversion & overwrite)
        result = write_patient_record(
            patient=row_dict,
            campaign_id=campaign_id,
            scheduled_time=scheduled_time,
            file_size_bytes=file_size_bytes,
            table_name=table_name,
            dynamodb_resource=dynamodb_resource,
            source_filename=source_filename,
        )

        if result.get("success"):
            valid_count += 1
            results.append({
                "empi": row_dict.get("empi", ""),
                "patient_id": result.get("patient_id"),
                "success": True,
            })
        else:
            all_errors.append({
                "empi": row_dict.get("empi", ""),
                "error": result.get("error", "Unknown validation error"),
            })
            results.append({
                "empi": row_dict.get("empi", ""),
                "patient_id": None,
                "success": False,
                "error": result.get("error"),
            })

    skipped_count = len(all_errors)
    status = "SCHEDULED" if valid_count > 0 else "FAILED"

    logger.info(
        f"[INTAKE] campaign_id={campaign_id} total={total_rows} valid={valid_count} errors={skipped_count}"
    )

    return {
        "success": valid_count > 0,
        "campaign_id": campaign_id,
        "status": status,
        "total_rows": total_rows,
        "valid_rows": valid_count,
        "skipped_rows": skipped_count,
        "error_rows": all_errors,
        "results": results,
        "scheduled_time": scheduled_time,
    }


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Lambda entrypoint for S3 ObjectCreated events or direct testing."""
    logger.info(f"[INTAKE_INVOCATION] Event: {json.dumps(event, default=str)[:500]}")

    # 1. Check direct test payload
    if "campaign_id" in event and "csv_content" in event:
        campaign_id = event["campaign_id"]
        csv_text = event["csv_content"]
        scheduled_time = event.get("scheduled_time") or utc_now_iso()
        source_filename = event.get("filename") or event.get("fileName") or "patients.csv"
        return process_csv_content(
            csv_text,
            campaign_id,
            scheduled_time,
            source_filename=source_filename,
            direct_agent=str(event.get("direct_agent") or "").lower() in ("yes", "true"),
            human_agent_phone=str(event.get("human_agent_phone") or ""),
        )

    # 2. Extract S3 event records
    records = event.get("Records") or []
    if not records:
        logger.warning("No Records found in S3 event.")
        return {"statusCode": 400, "message": "No Records found in event"}

    s3_client = boto3.client("s3")
    results = []

    for rec in records:
        s3_info = rec.get("s3")
        if not s3_info:
            continue

        bucket_name = s3_info["bucket"]["name"]
        s3_key = unquote_plus(s3_info["object"]["key"])
        campaign_id = parse_campaign_id_from_key(s3_key)

        logger.info(f"[INTAKE] Downloading s3://{bucket_name}/{s3_key} for campaign {campaign_id}...")

        try:
            # Fetch object & metadata
            s3_obj = s3_client.get_object(Bucket=bucket_name, Key=s3_key)
            csv_bytes = s3_obj["Body"].read()
            csv_text = csv_bytes.decode("utf-8-sig", errors="replace")

            # Extract scheduled-time and original-filename from S3 metadata
            metadata = s3_obj.get("Metadata", {})
            scheduled_time = (
                metadata.get("scheduled-time")
                or metadata.get("scheduled_time")
                or metadata.get("x-amz-meta-scheduled-time")
                or utc_now_iso()
            )
            # campaign_api signs the filename URL-encoded.
            source_filename = unquote(
                metadata.get("original-filename")
                or metadata.get("original_filename")
                or metadata.get("x-amz-meta-original-filename")
                or s3_key.split("/")[-1]
            )
            file_size_bytes = len(csv_bytes)
            # Set by campaign_api from the UI's "Direct to Human Agent" toggle.
            direct_agent = str(metadata.get("direct-agent") or "").lower() == "yes"
            human_agent_phone = metadata.get("human-agent-phone") or ""
            campaign_timezone = metadata.get("timezone") or ""

            res = process_csv_content(
                csv_text,
                campaign_id,
                scheduled_time,
                file_size_bytes=file_size_bytes,
                source_filename=source_filename,
                direct_agent=direct_agent,
                human_agent_phone=human_agent_phone,
                campaign_timezone=campaign_timezone,
            )
            results.append(res)

        except Exception as e:
            logger.error(f"[INTAKE_ERROR] Failed processing s3://{bucket_name}/{s3_key}: {e}", exc_info=True)
            results.append({
                "success": False,
                "campaign_id": campaign_id,
                "status": "FAILED",
                "error": str(e),
            })

    return {
        "statusCode": 200,
        "body": results,
    }
