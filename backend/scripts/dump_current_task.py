import boto3
import sys

sys.stdout.reconfigure(encoding='utf-8')
session = boto3.Session(profile_name='careharmony', region_name='us-east-1')
logs = session.client('logs')
ecs = session.client('ecs')

tasks = ecs.describe_tasks(cluster='careharmony-voice-cluster', tasks=['arn:aws:ecs:us-east-1:701348334422:task/careharmony-voice-cluster/bee28a5669584cbb9aab21b001163463'])
print('Task details:', tasks['tasks'][0]['lastStatus'], tasks['tasks'][0]['containers'][0]['lastStatus'])
print('Task attachments/ENI:', tasks['tasks'][0].get('attachments'))

stream = 'gateway/pipecat-voice-gateway/bee28a5669584cbb9aab21b001163463'
print(f"=== ALL LOGS FOR {stream} ===")
evs = logs.get_log_events(logGroupName='/ecs/careharmony-voice-gateway', logStreamName=stream, startFromHead=True)
for ev in evs['events']:
    print(f"[{ev['timestamp']}] {ev['message']}")
