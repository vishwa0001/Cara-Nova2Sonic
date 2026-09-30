import sys
import boto3

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

session = boto3.Session(profile_name="careharmony", region_name="us-east-1")
logs = session.client("logs")
stream = "gateway/pipecat-voice-gateway/5cd0c1dfa88c4d99a1657e6642f4f4d3"

try:
    events = logs.get_log_events(
        logGroupName="/ecs/careharmony-voice-gateway",
        logStreamName=stream,
        startFromHead=True,
    ).get("events", [])

    print(f"Total events in stream {stream}: {len(events)}")
    for e in events:
        print(f"[{e['timestamp']}] {e['message']}")
except Exception as e:
    print(f"Waiting for log stream: {e}")
