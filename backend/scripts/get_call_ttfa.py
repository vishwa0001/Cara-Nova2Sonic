"""
Print the time-to-first-audio (TTFA) and per-turn response latency for one
call, measured from the voice gateway's CloudWatch logs.

  TTFA        = first "200 OK (Call Answered)"  ->  first RTP packet sent (seq=0)
  Turn latency = caller transcript ("Role: USER") ->  next "NOVA_SPEAKING"

Turns where Cara dialled an agent in between are skipped, since that gap is
hold time rather than response latency.
"""
import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone

import boto3

ANSWERED = "Call Answered"
FIRST_RTP = "seq=0,"
STREAM_UP = "DuplexEventStream"
CARA_SPEAKS = "NOVA_SPEAKING"
CALLER_SAYS = "Role: USER"
AGENT_DIAL = "[TRANSFER] Dialing agent"
AGENT_LEG = re.compile(r"\[TRANSFER\] Dialing agent \S+ \(Call-ID (\S+)\) for (\S+)")


def fetch_call_events(call_id: str, log_group: str, profile: str, days: int) -> list:
    """Returns the gateway log lines that mention call_id, oldest first.
    Per-frame audio lines are excluded; they are thousands per call."""
    session = boto3.Session(profile_name=profile, region_name="us-east-1")
    logs = session.client("logs")
    start = datetime.now(timezone.utc) - timedelta(days=days)

    kwargs = {
        "logGroupName": log_group,
        "filterPattern": f'"{call_id}" -"AUDIO_IN" -"AUDIO_OUT"',
        "startTime": int(start.timestamp() * 1000),
    }
    events = []
    while True:
        resp = logs.filter_log_events(**kwargs)
        events += [(e["timestamp"], e["message"]) for e in resp["events"]]
        if "nextToken" not in resp:
            break
        kwargs["nextToken"] = resp["nextToken"]
    events.sort()
    return events


def first_after(events: list, marker: str, after: int = 0):
    for ts, msg in events:
        if ts >= after and marker in msg:
            return ts
    return None


def measure(call_id: str, events: list) -> dict:
    for _, msg in events:
        leg = AGENT_LEG.search(msg)
        if leg and leg.group(1) == call_id:
            return {
                "call_id": call_id,
                "error": f"this is the agent transfer leg of call {leg.group(2)}; "
                         f"measure that call instead",
            }

    answered = first_after(events, ANSWERED)
    if answered is None:
        return {"call_id": call_id, "error": "no 'Call Answered' line found"}

    first_rtp = first_after(events, FIRST_RTP, answered)
    stream_up = first_after(events, STREAM_UP, answered)
    first_speech = first_after(events, CARA_SPEAKS, answered)

    caller_turns = [(ts, msg) for ts, msg in events if ts >= answered and CALLER_SAYS in msg]
    turns = []
    for i, (ts, msg) in enumerate(caller_turns):
        next_caller = caller_turns[i + 1][0] if i + 1 < len(caller_turns) else float("inf")
        reply = first_after(events, CARA_SPEAKS, ts)
        dialled_agent = first_after(events, AGENT_DIAL, ts)
        if reply is None or reply >= next_caller:
            continue
        if dialled_agent is None or dialled_agent > reply:
            turns.append({
                "caller": msg.split("Content:", 1)[-1].strip(),
                "latency_ms": reply - ts,
            })

    def since_answer(ts):
        return ts - answered if ts is not None else None

    latencies = [t["latency_ms"] for t in turns]
    return {
        "call_id": call_id,
        "answered_at": datetime.fromtimestamp(answered / 1000, timezone.utc).isoformat(),
        "ttfa_ms": since_answer(first_rtp),
        "nova_stream_up_ms": since_answer(stream_up),
        "first_cara_speech_ms": since_answer(first_speech),
        "turns": turns,
        "turn_latency_median_ms": round(statistics.median(latencies)) if latencies else None,
        "turn_latency_mean_ms": round(statistics.mean(latencies)) if latencies else None,
    }


def print_report(result: dict) -> None:
    def ms(value):
        return f"{value} ms" if value is not None else "n/a"

    print(f"Call {result['call_id']}  (answered {result['answered_at']})")
    print(f"  TTFA (answer -> first audio packet): {ms(result['ttfa_ms'])}")
    print(f"  Answer -> Nova stream up:            {ms(result['nova_stream_up_ms'])}")
    print(f"  Answer -> Cara first speaks:         {ms(result['first_cara_speech_ms'])}")
    if result["turns"]:
        print(f"  Caller -> Cara turn latency: median {ms(result['turn_latency_median_ms'])}, "
              f"mean {ms(result['turn_latency_mean_ms'])}")
        for i, turn in enumerate(result["turns"], start=1):
            print(f"    {i:>2}. {turn['latency_ms']:>5} ms  after \"{turn['caller']}\"")


def main():
    parser = argparse.ArgumentParser(
        description="Print a call's TTFA and per-turn latency from the voice "
                    "gateway's CloudWatch logs."
    )
    parser.add_argument("--call-id", required=True, help="The call_id (UUID) to measure")
    parser.add_argument(
        "--log-group",
        default="/ecs/careharmony-voice-gateway",
        help="CloudWatch Log Group name",
    )
    parser.add_argument("--profile", default="careharmony", help="AWS profile name")
    parser.add_argument("--days", type=int, default=14, help="How many days back to search")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    args = parser.parse_args()

    events = fetch_call_events(args.call_id, args.log_group, args.profile, args.days)
    if not events:
        print(
            f"No gateway logs found for call_id={args.call_id} in the last {args.days} days. "
            f"Confirm the call_id and try a larger --days.",
            file=sys.stderr,
        )
        sys.exit(1)

    result = measure(args.call_id, events)
    if "error" in result:
        print(f"call_id={args.call_id}: {result['error']}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
