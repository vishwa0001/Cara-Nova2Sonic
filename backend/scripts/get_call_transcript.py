import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
import boto3
from botocore.exceptions import ClientError

TRANSCRIPT_PATTERN = re.compile(
    r"\[TRANSCRIPT\] Contact (\S+) \| Role: (\w+) \| Content: (.+)"
)

RECORDING_BUCKET = "careharmony-cara-call-recordings-701348334422-us-east-1"


def _session() -> boto3.Session:
    return boto3.Session(profile_name="careharmony", region_name="us-east-1")


def find_recording(call_id: str, turns: list, url_expires: int = 3600) -> dict:
    """
    Locates the call's recording in S3 and returns its s3:// URI plus a
    pre-signed HTTPS link (the bucket is private). Keys follow
    recordings/YYYY/MM/DD/<call_id>.wav on the pickup date (UTC), so this
    tries the day of the first transcript turn and the day before.
    """
    session = _session()
    s3 = session.client("s3")
    keys = []
    if turns:
        first = datetime.fromisoformat(turns[0]["timestamp"])
        for day in (first, first - timedelta(days=1)):
            keys.append(f"recordings/{day:%Y/%m/%d}/{call_id}.wav")

    for key in dict.fromkeys(keys):
        try:
            head = s3.head_object(Bucket=RECORDING_BUCKET, Key=key)
        except ClientError:
            continue
        meta = head.get("Metadata", {})
        return {
            "s3_uri": f"s3://{RECORDING_BUCKET}/{key}",
            "url": s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": RECORDING_BUCKET, "Key": key},
                ExpiresIn=url_expires,
            ),
            "url_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=url_expires)).isoformat(),
            "duration_sec": float(meta["duration_sec"]) if "duration_sec" in meta else None,
            "transferred": meta.get("transferred") == "true",
            "size_bytes": head["ContentLength"],
        }
    return {
        "s3_uri": None,
        "url": None,
        "reason": "No recording in S3 for this call (calls before 2026-10-14 18:03Z were not recorded)",
    }


def fetch_transcript_from_logs(call_id: str, log_group: str, days: int = 14) -> dict:
    """
    Pulls all [TRANSCRIPT] log lines for a given call_id from
    CloudWatch and assembles them into an ordered JSON transcript.
    """
    logs = _session().client("logs")

    # Search the whole log group: the gateway's stream changes on every
    # deploy, so the call may not be in the newest streams.
    kwargs = {
        "logGroupName": log_group,
        "filterPattern": f'"[TRANSCRIPT] Contact {call_id}"',
        "startTime": int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000),
    }
    turns = []
    while True:
        resp = logs.filter_log_events(**kwargs)
        for event in resp["events"]:
            match = TRANSCRIPT_PATTERN.search(event["message"])
            if not match:
                continue
            matched_call_id, role, content = match.groups()
            if matched_call_id != call_id:
                continue
            turns.append({
                "timestamp": datetime.fromtimestamp(
                    event["timestamp"] / 1000.0, timezone.utc
                ).isoformat(),
                "speaker": role,
                "text": content.strip(),
            })
        if "nextToken" not in resp:
            break
        kwargs["nextToken"] = resp["nextToken"]

    turns.sort(key=lambda t: t["timestamp"])
    for i, turn in enumerate(turns, start=1):
        turn["turn_number"] = i

    return {
        "call_id": call_id,
        "source": "cloudwatch_live_asr",
        "turn_count": len(turns),
        "turns": turns,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Fetch a call transcript as JSON, sourced "
                    "from live ASR logs (CloudWatch), with a download "
                    "link for the call recording when one exists."
    )
    parser.add_argument(
        "--call-id",
        required=True,
        help="The call_id (UUID) to fetch",
    )
    parser.add_argument(
        "--log-group",
        default="/ecs/careharmony-voice-gateway",
        help="CloudWatch Log Group name",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="How many days back to search the logs",
    )
    parser.add_argument(
        "--url-expires",
        type=int,
        default=3600,
        help="Seconds the recording download link stays valid",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path; defaults to stdout",
    )
    args = parser.parse_args()

    result = fetch_transcript_from_logs(args.call_id, args.log_group, args.days)

    if result["turn_count"] == 0:
        print(
            f"No transcript found for call_id={args.call_id}. "
            f"Confirm the call_id is correct and the log stream "
            f"is still within CloudWatch's retention window.",
            file=sys.stderr,
        )
        sys.exit(1)

    result["recording"] = find_recording(args.call_id, result["turns"], args.url_expires)
    output_json = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"Written to {args.output}")
    else:
        print(output_json)


if __name__ == "__main__":
    main()
