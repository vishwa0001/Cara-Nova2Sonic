import boto3
import json
import datetime

session = boto3.Session(profile_name="careharmony", region_name="us-east-1")
logs = session.client("logs")
connect = session.client("connect")

contact_id = "0c1a55de-ad08-424d-abaf-100f13d164be"
start_ms = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)).timestamp() * 1000)

print(f"=== 1. Connect Contact Flow Logs for {contact_id} ===")
log_groups = logs.describe_log_groups(logGroupNamePrefix="/aws/connect").get("logGroups", [])
for lg in log_groups:
    lg_name = lg["logGroupName"]
    try:
        events = logs.filter_log_events(
            logGroupName=lg_name,
            filterPattern=contact_id,
            startTime=start_ms,
        ).get("events", [])
        if events:
            print(f"Found {len(events)} events in {lg_name}:")
            for ev in events:
                print(f"  [{ev['timestamp']}] {ev['message']}")
    except Exception as e:
        print(f"Error checking {lg_name}: {e}")

print(f"\n=== 2. Connect Describe Contact for {contact_id} ===")
try:
    contact_desc = connect.describe_contact(
        InstanceId="32e49641-5486-4e08-b943-bcffa62bb77c",
        ContactId=contact_id,
    )
    print(json.dumps(contact_desc.get("Contact", {}), indent=2, default=str))
except Exception as e:
    print(f"Error describing contact: {e}")

print(f"\n=== 3. ECS Voice Gateway Logs (last 30m) ===")
try:
    streams = logs.describe_log_streams(
        logGroupName="/ecs/careharmony-voice-gateway",
        orderBy="LastEventTime",
        descending=True,
        limit=5,
    ).get("logStreams", [])
    for st in streams:
        st_name = st["logStreamName"]
        evs = logs.get_log_events(
            logGroupName="/ecs/careharmony-voice-gateway",
            logStreamName=st_name,
            startTime=start_ms,
            limit=100,
        ).get("events", [])
        if evs:
            print(f"\n--- Stream: {st_name} ({len(evs)} events) ---")
            for ev in evs:
                msg = ev['message'].encode('ascii', errors='replace').decode('ascii')
                print(f"  [{ev['timestamp']}] {msg}")
except Exception as e:
    print(f"Error querying ECS logs: {e}")
