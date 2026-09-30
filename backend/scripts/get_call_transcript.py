import argparse
import json
import re
import sys
from datetime import datetime, timezone
import boto3

TRANSCRIPT_PATTERN = re.compile(
    r"\[TRANSCRIPT\] Contact (\S+) \| Role: (\w+) \| Content: (.+)"
)


def fetch_transcript_from_logs(call_id: str, log_group: str) -> dict:
    """
    Pulls all [TRANSCRIPT] log lines for a given call_id from
    CloudWatch and assembles them into an ordered JSON transcript.
    """
    session = boto3.Session(profile_name="careharmony", region_name="us-east-1")
    logs = session.client("logs")

    streams = logs.describe_log_streams(
        logGroupName=log_group,
        orderBy="LastEventTime",
        descending=True,
        limit=20,
    )["logStreams"]

    turns = []
    for stream in streams:
        kwargs = {
            "logGroupName": log_group,
            "logStreamName": stream["logStreamName"],
            "startFromHead": True,
        }
        next_token = None
        while True:
            if next_token:
                kwargs["nextToken"] = next_token
            resp = logs.get_log_events(**kwargs)
            for event in resp["events"]:
                msg = event["message"]
                if call_id not in msg:
                    continue
                match = TRANSCRIPT_PATTERN.search(msg)
                if match:
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
            new_token = resp.get("nextForwardToken")
            if not resp["events"] or new_token == next_token:
                break
            next_token = new_token

        if turns:
            break  # found the right stream, stop scanning others

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
                    "from live ASR logs (CloudWatch)."
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
        "--output",
        default=None,
        help="Output file path; defaults to stdout",
    )
    args = parser.parse_args()

    result = fetch_transcript_from_logs(args.call_id, args.log_group)

    if result["turn_count"] == 0:
        print(
            f"No transcript found for call_id={args.call_id}. "
            f"Confirm the call_id is correct and the log stream "
            f"is still within CloudWatch's retention window.",
            file=sys.stderr,
        )
        sys.exit(1)

    output_json = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"Written to {args.output}")
    else:
        print(output_json)


if __name__ == "__main__":
    main()
