import boto3
import json
import time
import sys

def main():
    contact_id = sys.argv[1] if len(sys.argv) > 1 else '7bc4b99b-74c7-4f2c-8482-2206330d13fe'
    session = boto3.Session(profile_name='careharmony', region_name='us-east-1')
    logs = session.client('logs')
    connect = session.client('connect')

    print(f"=== Diagnosing Contact: {contact_id} ===")
    try:
        c = connect.describe_contact(InstanceId='32e49641-5486-4e08-b943-bcffa62bb77c', ContactId=contact_id)['Contact']
        print(f"Initiation: {c.get('InitiationTimestamp')}")
        print(f"Connected:  {c.get('ConnectedToSystemTimestamp')}")
        print(f"Disconnect: {c.get('DisconnectTimestamp')}")
        print(f"Reason:     {c.get('DisconnectReason')}")
        print(f"Recordings: {json.dumps(c.get('Recordings', []), indent=2, default=str)}")
    except Exception as e:
        print(f"DescribeContact error: {e}")

    print("\n=== CloudWatch Contact Flow Execution Logs ===")
    now = int(time.time())
    query = f"fields @timestamp, @message | filter ContactId = '{contact_id}' or @message like '{contact_id}' | sort @timestamp asc"
    try:
        q = logs.start_query(
            logGroupName='/aws/connect/careharmony-connect-instance',
            startTime=now - 7200,
            endTime=now + 300,
            queryString=query
        )
        while True:
            res = logs.get_query_results(queryId=q['queryId'])
            if res['status'] in ['Complete', 'Failed', 'Cancelled']:
                break
            time.sleep(0.5)
        
        print(f"Found {len(res['results'])} log entries:")
        for r in res['results']:
            vals = {f['field']: f['value'] for f in r}
            print(f"[{vals.get('@timestamp')}] {vals.get('@message')}")
    except Exception as e:
        print(f"Logs query error: {e}")

    print("\n=== ECS Voice Gateway Logs (Around Contact Time) ===")
    try:
        # Check streams in /ecs/careharmony-voice-gateway
        streams = logs.describe_log_streams(
            logGroupName='/ecs/careharmony-voice-gateway',
            orderBy='LastEventTime',
            descending=True,
            limit=3
        )['logStreams']
        for stream in streams:
            print(f"\n--- Log Stream: {stream['logStreamName']} ---")
            events = logs.get_log_events(
                logGroupName='/ecs/careharmony-voice-gateway',
                logStreamName=stream['logStreamName'],
                limit=30,
                startFromHead=False
            )['events']
            for ev in events:
                msg = ev['message'].strip()
                # print safely
                print(f"  {msg.encode('ascii', errors='replace').decode('ascii')}")
    except Exception as e:
        print(f"ECS logs error: {e}")

if __name__ == '__main__':
    main()
