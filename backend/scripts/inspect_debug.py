import boto3
import json
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8')

session = boto3.Session(profile_name='careharmony', region_name='us-east-1')
logs = session.client('logs')
connect = session.client('connect')

contact_id = '32e8ff3d-76aa-4ac8-bff5-3903aab5f8cf'
print(f"=== INVESTIGATING CONTACT {contact_id} ===")

# 1. Contact Details
try:
    desc = connect.describe_contact(
        InstanceId='32e49641-5486-4e08-b943-bcffa62bb77c',
        ContactId=contact_id
    )
    print("Contact Initiation:", desc['Contact'].get('InitiationTimestamp'))
    print("Contact Disconnect:", desc['Contact'].get('DisconnectTimestamp'))
    print("Contact DisconnectReason:", desc['Contact'].get('DisconnectReason'))
    print("Contact Recordings:", desc['Contact'].get('Recordings'))
    print("Contact Attributes:", desc['Contact'].get('Attributes'))
except Exception as e:
    print("Describe contact error:", e)

# 2. Check Connect Flow CloudWatch Logs
print("\n=== CONNECT CLOUDWATCH LOGS ===")
try:
    # Look for contact flow log groups
    lg_resp = logs.describe_log_groups(logGroupNamePrefix='/aws/connect')
    for lg in lg_resp.get('logGroups', []):
        lg_name = lg['logGroupName']
        print(f"Searching log group: {lg_name}")
        # Search for contact_id in log events
        query = f"fields @timestamp, @message | filter @message like /{contact_id}/ or @message like /391680c1-72d8-4444-a2ae-39983c12a662/ | sort @timestamp desc | limit 50"
        q_resp = logs.start_query(
            logGroupName=lg_name,
            startTime=int(datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc).timestamp()),
            endTime=int(datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc).timestamp()),
            queryString=query
        )
        query_id = q_resp['queryId']
        import time
        for _ in range(10):
            time.sleep(1)
            res = logs.get_query_results(queryId=query_id)
            if res['status'] == 'Complete':
                print(f"Found {len(res['results'])} log lines in {lg_name}")
                for r in res['results']:
                    row = {col['field']: col['value'] for col in r}
                    print(f"  {row.get('@timestamp')}: {row.get('@message')}")
                break
except Exception as e:
    print("Connect log search error:", e)

print("\n=== SCANNING ALL RECENT LOG STREAMS ===")
streams = logs.describe_log_streams(
    logGroupName='/ecs/careharmony-voice-gateway',
    orderBy='LastEventTime',
    descending=True,
    limit=20
)
for s in streams['logStreams']:
    first_dt = datetime.fromtimestamp(s.get('firstEventTimestamp', 0) / 1000.0, timezone.utc) if s.get('firstEventTimestamp') else None
    last_dt = datetime.fromtimestamp(s.get('lastEventTimestamp', 0) / 1000.0, timezone.utc) if s.get('lastEventTimestamp') else None
    print(f"Stream: {s['logStreamName']} | First: {first_dt} | Last: {last_dt}")
    if last_dt and last_dt.hour == 14 and last_dt.minute >= 40:
        print(f"--- DETAILED EVENTS FOR {s['logStreamName']} ---")
        evs = logs.get_log_events(logGroupName='/ecs/careharmony-voice-gateway', logStreamName=s['logStreamName'], limit=100)
        for ev in evs['events']:
            dt = datetime.fromtimestamp(ev['timestamp'] / 1000.0, timezone.utc)
            print(f"  [{dt.isoformat()}] {ev['message']}")
