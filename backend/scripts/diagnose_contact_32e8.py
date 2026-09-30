import sys
import boto3
import json
import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

session = boto3.Session(profile_name="careharmony", region_name="us-east-1")
logs = session.client("logs")
connect = session.client("connect")
kvs = session.client("kinesisvideo")

contact_id = "32e8ff3d-76aa-4ac8-bff5-3903aab5f8cf"
inst_id = "32e49641-5486-4e08-b943-bcffa62bb77c"

print("======================================================================")
print(f"1. CONNECT DESCRIBE CONTACT: {contact_id}")
print("======================================================================")
try:
    c_desc = connect.describe_contact(InstanceId=inst_id, ContactId=contact_id)
    print(json.dumps(c_desc.get("Contact", {}), indent=2, default=str))
except Exception as e:
    print(f"Error describing contact: {e}")

print("\n======================================================================")
print("2. RECENT KINESIS VIDEO STREAMS")
print("======================================================================")
try:
    st_list = kvs.list_streams().get("StreamInfoList", [])
    for s in st_list:
        print(f"Stream: {s['StreamName']}, Status: {s['Status']}, Created: {s['CreationTime']}")
except Exception as e:
    print(f"Error listing KVS streams: {e}")

print("\n======================================================================")
print("3. ALL RECENT ECS LOG STREAMS & LOG EVENTS (Last 1 hour)")
print("======================================================================")
start_ms = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).timestamp() * 1000)
try:
    streams = logs.describe_log_streams(
        logGroupName="/ecs/careharmony-voice-gateway",
        orderBy="LastEventTime",
        descending=True,
        limit=5,
    ).get("logStreams", [])
    for st in streams:
        st_name = st["logStreamName"]
        print(f"\n--- Stream: {st_name} (Last event: {st.get('lastEventTimestamp')}) ---")
        evs = logs.get_log_events(
            logGroupName="/ecs/careharmony-voice-gateway",
            logStreamName=st_name,
            startTime=start_ms,
            limit=200,
        ).get("events", [])
        for ev in evs:
            print(f"  [{ev['timestamp']}] {ev['message']}")
except Exception as e:
    print(f"Error reading ECS logs: {e}")
