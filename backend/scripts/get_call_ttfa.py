"""
Print the time-to-first-audio (TTFA) and per-turn response latency for one
call, measured from the voice gateway's CloudWatch logs or local log files.

  Initial RTP TTFA     = first "200 OK (Call Answered)"  ->  first RTP packet sent (seq=0)
  Pure Model TTFA      = caller transcript ("Role: USER") ->  next "NOVA_SPEAKING"
  Total Perceived TTFA = last word packet compute (20ms) + VAD silence (150ms) + pure model TTFA

Turns where Cara dialed an agent in between are skipped, since that gap is
hold time rather than response latency.
"""
import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone

import boto3

# =============================================================================
# Pipeline Milestone Log Markers & Signatures
# =============================================================================

# 1. Telephony Connection & Handshake Milestones
ANSWERED = "Call Answered"                 # SIP 200 OK received; call picked up
FIRST_RTP = "seq=0,"                      # First outgoing telephony RTP packet sent
STREAM_UP = "DuplexEventStream"            # Bidirectional Nova Sonic stream active
FIRST_GREETING = "NOVA_SPEAKING"           # Assistant starts speaking first greeting

# 2. Conversational Turn & Acoustic Endpointing Parameters
USER_SPEECH_COMMITTED = "Role: USER"       # Caller speech finalized by VAD into Role: USER
VAD_SILENCE_THRESHOLD_MS = 150             # Telephony VAD silence window (from backend/config.json)
LAST_WORD_PACKET_COMPUTE_MS = 20           # Time to compute/receive last audio frame of user's final word

# 3. Model Reasoning & Sub-Phases (from Turn Breakdown Table)
LLM_REASONING = "[TOOL_USE]"               # Bedrock Nova Sonic finishes reasoning and calls a tool
TOOL_EXECUTION = "[TOOL_RESULT_SENT]"      # Gateway safety/transfer tool executes in memory
TEXT_TOKEN_GEN = "Role: ASSISTANT"         # Model begins streaming response text tokens
DB_UPDATE = "[DDB_OUTCOME]"                # Outcome/status asynchronously written to DynamoDB
FIRST_VOICE_OUT = "NOVA_SPEAKING"          # First synthesized 20ms PCM audio frame sent over RTP

# 4. Agent Transfers & Exceptions
AGENT_DIAL = "[TRANSFER] Dialing agent"    # Transfer initiated (skip turn latency due to hold)
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


def parse_local_log_file(file_path: str, call_id: str | None = None) -> list:
    """Parse log events from a local file containing timestamps and messages."""
    events = []
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if call_id and call_id not in line:
                continue

            # Check if line starts with epoch milliseconds:
            match_epoch = re.match(r"^(\d{13})\s*\|\s*(.*)$", line)
            if match_epoch:
                ts = int(match_epoch.group(1))
                msg = match_epoch.group(2)
                events.append((ts, msg))
                continue

            # Fallback: parse "YYYY-MM-DD HH:MM:SS" timestamp
            match_dt = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{3})?)\s+(.*)$", line)
            if match_dt:
                dt_str = match_dt.group(1)
                msg = match_dt.group(2)
                fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in dt_str else "%Y-%m-%d %H:%M:%S"
                parsed_dt = datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc)
                ts = int(parsed_dt.timestamp() * 1000)
                events.append((ts, msg))
                continue

    events.sort()
    return events


def first_after(events: list, marker: str, after: int = 0):
    for ts, msg in events:
        if ts >= after and marker in msg:
            return ts, msg
    return None, None


def format_utc_ts(epoch_ms: int | None) -> str:
    if epoch_ms is None:
        return "--"
    dt = datetime.fromtimestamp(epoch_ms / 1000, timezone.utc)
    return dt.strftime("%H:%M:%S.%f")[:-3]


def measure(call_id: str, events: list, vad_ms: int = VAD_SILENCE_THRESHOLD_MS, last_word_ms: int = LAST_WORD_PACKET_COMPUTE_MS) -> dict:
    for _, msg in events:
        leg = AGENT_LEG.search(msg)
        if leg and leg.group(1) == call_id:
            return {
                "call_id": call_id,
                "error": f"this is the agent transfer leg of call {leg.group(2)}; "
                         f"measure that call instead",
            }

    answered, _ = first_after(events, ANSWERED)
    if answered is None:
        return {"call_id": call_id, "error": "no 'Call Answered' line found"}

    first_rtp, _ = first_after(events, FIRST_RTP, answered)
    stream_up, _ = first_after(events, STREAM_UP)
    first_speech, _ = first_after(events, FIRST_GREETING, answered)

    caller_turns = [(ts, msg) for ts, msg in events if ts >= answered and USER_SPEECH_COMMITTED in msg]
    turns = []

    for i, (ts_committed, msg) in enumerate(caller_turns):
        next_caller = caller_turns[i + 1][0] if i + 1 < len(caller_turns) else float("inf")
        reply_ts, _ = first_after(events, FIRST_VOICE_OUT, ts_committed)
        dialled_agent, _ = first_after(events, AGENT_DIAL, ts_committed)

        if reply_ts is None or reply_ts >= next_caller:
            continue
        if dialled_agent is not None and dialled_agent <= reply_ts:
            continue

        # Timestamps of the turn
        t_committed = ts_committed
        t_last_word = t_committed - vad_ms
        t_acoustic_start = t_last_word - last_word_ms

        pure_model_ttfa = reply_ts - t_committed
        perceived_ttfa = reply_ts - t_last_word
        total_ttfa = reply_ts - t_acoustic_start

        # Detect sub-phases within this turn
        milestones = []

        # 1. Acoustic Start / Last Word Packet Compute
        milestones.append({
            "phase": "Last Word Compute",
            "utc": format_utc_ts(t_acoustic_start),
            "epoch": t_acoustic_start,
            "delta_ms": last_word_ms,
            "desc": f"Caller finishes vocalizing last word (processing {last_word_ms}ms audio frame).",
        })

        # 2. VAD Silence Detection
        milestones.append({
            "phase": "Telephony VAD Silence",
            "utc": format_utc_ts(t_last_word),
            "epoch": t_last_word,
            "delta_ms": vad_ms,
            "desc": f"{vad_ms}ms acoustic silence window confirms caller finished speaking.",
        })

        # 3. User Speech Committed
        milestones.append({
            "phase": "User Speech Committed",
            "utc": format_utc_ts(t_committed),
            "epoch": t_committed,
            "delta_ms": 0,
            "desc": "Caller sentence finalized by VAD into Role: USER.",
        })

        prev_ts = t_committed

        # 4. Bedrock LLM Reasoning / Tool Use
        t_reasoning, reasoning_msg = first_after(events, LLM_REASONING, t_committed)
        if t_reasoning is not None and t_reasoning <= reply_ts:
            delta = t_reasoning - prev_ts
            milestones.append({
                "phase": "Bedrock LLM Reasoning",
                "utc": format_utc_ts(t_reasoning),
                "epoch": t_reasoning,
                "delta_ms": delta,
                "desc": "Nova 2.5 Sonic processes reasoning and triggers tool/action.",
            })
            prev_ts = t_reasoning

        # 5. Tool Execution
        t_tool, _ = first_after(events, TOOL_EXECUTION, prev_ts)
        if t_tool is not None and t_tool <= reply_ts:
            delta = t_tool - prev_ts
            milestones.append({
                "phase": "Tool Execution",
                "utc": format_utc_ts(t_tool),
                "epoch": t_tool,
                "delta_ms": delta,
                "desc": "Tool executes in memory.",
            })
            prev_ts = t_tool

        # 6. Text Token Generation
        t_text, _ = first_after(events, TEXT_TOKEN_GEN, prev_ts)
        if t_text is not None and t_text <= reply_ts:
            delta = t_text - prev_ts
            milestones.append({
                "phase": "Text Token Generation",
                "utc": format_utc_ts(t_text),
                "epoch": t_text,
                "delta_ms": delta,
                "desc": "Model begins outputting response text tokens.",
            })
            prev_ts = t_text

        # 7. Database Update
        t_db, _ = first_after(events, DB_UPDATE, prev_ts)
        if t_db is not None and t_db <= reply_ts:
            delta = t_db - prev_ts
            milestones.append({
                "phase": "Database Update",
                "utc": format_utc_ts(t_db),
                "epoch": t_db,
                "delta_ms": delta,
                "desc": "Status/outcome written to DynamoDB asynchronously.",
            })
            prev_ts = t_db

        # 8. First Voice Audio Out
        delta_audio = reply_ts - prev_ts
        milestones.append({
            "phase": "First Voice Audio Out",
            "utc": format_utc_ts(reply_ts),
            "epoch": reply_ts,
            "delta_ms": delta_audio,
            "desc": "First synthesized 20ms PCM audio frame generated and dispatched over RTP (NOVA_SPEAKING).",
        })

        caller_text = msg.split("Content:", 1)[-1].strip() if "Content:" in msg else msg

        turns.append({
            "caller": caller_text,
            "committed_ts": t_committed,
            "reply_ts": reply_ts,
            "last_word_compute_ms": last_word_ms,
            "vad_silence_ms": vad_ms,
            "pure_model_ttfa_ms": pure_model_ttfa,
            "perceived_ttfa_ms": perceived_ttfa,
            "total_ttfa_with_last_word_ms": total_ttfa,
            "milestones": milestones,
        })

    def since_answer(ts):
        return ts - answered if ts is not None else None

    pure_latencies = [t["pure_model_ttfa_ms"] for t in turns]
    perceived_latencies = [t["perceived_ttfa_ms"] for t in turns]
    total_latencies = [t["total_ttfa_with_last_word_ms"] for t in turns]

    return {
        "call_id": call_id,
        "answered_at": datetime.fromtimestamp(answered / 1000, timezone.utc).isoformat(),
        "ttfa_rtp_ms": since_answer(first_rtp),
        "nova_stream_up_ms": since_answer(stream_up),
        "first_cara_speech_ms": since_answer(first_speech),
        "turns": turns,
        "pure_model_ttfa_median_ms": round(statistics.median(pure_latencies)) if pure_latencies else None,
        "pure_model_ttfa_mean_ms": round(statistics.mean(pure_latencies)) if pure_latencies else None,
        "perceived_ttfa_median_ms": round(statistics.median(perceived_latencies)) if perceived_latencies else None,
        "perceived_ttfa_mean_ms": round(statistics.mean(perceived_latencies)) if perceived_latencies else None,
        "total_ttfa_median_ms": round(statistics.median(total_latencies)) if total_latencies else None,
        "total_ttfa_mean_ms": round(statistics.mean(total_latencies)) if total_latencies else None,
    }


def print_report(result: dict) -> None:
    def ms(value):
        return f"{value} ms" if value is not None else "n/a"

    print("=" * 86)
    print(f"CALL LATENCY & TTFA AUDIT: {result['call_id']}")
    print(f"Answered At (UTC): {result['answered_at']}")
    print("=" * 86)
    print(f"  Initial RTP Out TTFA (answer -> first audio packet): {ms(result['ttfa_rtp_ms'])}")
    stream_str = (
        f"0 ms (pre-warmed {abs(result['nova_stream_up_ms'])} ms before answer)"
        if result['nova_stream_up_ms'] is not None and result['nova_stream_up_ms'] <= 0
        else ms(result['nova_stream_up_ms'])
    )
    print(f"  Answer -> Nova Stream Up:                           {stream_str}")
    print(f"  Answer -> First Spoken Greeting:                    {ms(result['first_cara_speech_ms'])}")
    print("-" * 86)

    if not result["turns"]:
        print("  No conversational turns found.")
        return

    print("CONVERSATIONAL TURNS SUMMARY:")
    print(f"  Total Turns: {len(result['turns'])}")
    print(f"  Pure Model TTFA (excl. VAD):              Median {ms(result['pure_model_ttfa_median_ms'])}, Mean {ms(result['pure_model_ttfa_mean_ms'])}")
    print(f"  Perceived TTFA (incl. 150ms VAD):         Median {ms(result['perceived_ttfa_median_ms'])}, Mean {ms(result['perceived_ttfa_mean_ms'])}")
    print(f"  Total TTFA (incl. 20ms last word + VAD):  Median {ms(result['total_ttfa_median_ms'])}, Mean {ms(result['total_ttfa_mean_ms'])}")
    print("-" * 86)

    for i, turn in enumerate(result["turns"], start=1):
        print(f"\nTURN {i}: Caller said: \"{turn['caller']}\"")
        print(f"  * Pure Model TTFA (excl. VAD):             {turn['pure_model_ttfa_ms']} ms")
        print(f"  * Perceived TTFA (incl. 150ms VAD):        {turn['perceived_ttfa_ms']} ms")
        print(f"  * Total TTFA (incl. 20ms last word + VAD): {turn['total_ttfa_with_last_word_ms']} ms")
        print()
        print(f"  {'Milestone / Sub-phase':<24} | {'Timestamp (UTC)':<15} | {'Epoch (ms)':<15} | {'Elapsed':<8} | {'What Occurred'}")
        print(f"  {'-'*24}-|-{'-'*15}-|-{'-'*15}-|-{'-'*8}-|-{'-'*20}")
        for m in turn["milestones"]:
            delta_str = f"{m['delta_ms']:>4} ms"
            print(f"  {m['phase']:<24} | {m['utc']:<15} | {m['epoch']:<15} | {delta_str:<8} | {m['desc']}")
        print(f"  {'-'*84}")
        start_utc = turn["milestones"][2]["utc"] if len(turn["milestones"]) > 2 else "--"
        end_utc = turn["milestones"][-1]["utc"] if turn["milestones"] else "--"
        print(f"  {'Total Model TTFA':<24} | {'--':<15} | {'--':<15} | {turn['pure_model_ttfa_ms']:>4} ms | Start ({start_utc}) -> Voice Out ({end_utc})")
        print(f"  {'Total Turn TTFA':<24} | {'--':<15} | {'--':<15} | {turn['total_ttfa_with_last_word_ms']:>4} ms | Last Word Compute -> Voice Out")
    print("=" * 86)


def main():
    parser = argparse.ArgumentParser(
        description="Print a call's TTFA and per-turn latency breakdown from CloudWatch or local log files."
    )
    parser.add_argument("--call-id", required=False, help="The call_id (UUID) to measure")
    parser.add_argument("--file", required=False, help="Path to local log file to analyze")
    parser.add_argument(
        "--log-group",
        default="/ecs/careharmony-voice-gateway",
        help="CloudWatch Log Group name",
    )
    parser.add_argument("--profile", default="careharmony", help="AWS profile name")
    parser.add_argument("--days", type=int, default=14, help="How many days back to search")
    parser.add_argument("--vad-ms", type=int, default=VAD_SILENCE_THRESHOLD_MS, help="VAD silence window in ms")
    parser.add_argument("--last-word-ms", type=int, default=LAST_WORD_PACKET_COMPUTE_MS, help="Last word packet compute in ms")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of formatted text")
    args = parser.parse_args()

    if args.file:
        events = parse_local_log_file(args.file, args.call_id)
        call_id = args.call_id or "local-file-call"
    else:
        if not args.call_id:
            parser.error("--call-id is required when --file is not specified")
        call_id = args.call_id
        events = fetch_call_events(call_id, args.log_group, args.profile, args.days)

    if not events:
        print(
            f"No gateway logs found for call_id={call_id}. "
            f"Confirm the call_id or file path.",
            file=sys.stderr,
        )
        sys.exit(1)

    result = measure(call_id, events, vad_ms=args.vad_ms, last_word_ms=args.last_word_ms)
    if "error" in result:
        print(f"call_id={call_id}: {result['error']}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
