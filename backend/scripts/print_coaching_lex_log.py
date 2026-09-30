import boto3, json, time

session = boto3.Session(profile_name='careharmony-main', region_name='us-east-1')
logs = session.client('logs')

lg = '/aws/lex/cara-health-bot-conversations-258673'
cid = '86dd9e39-c622-46fe-96c2-259e1f6c3566'

start_query = logs.start_query(
    logGroupName=lg,
    startTime=int(time.time()) - 7200,
    endTime=int(time.time()),
    queryString=f'fields @timestamp, @message | filter @message like /{cid}/ | sort @timestamp asc | limit 20'
)
qid = start_query['queryId']
while True:
    res = logs.get_query_results(queryId=qid)
    if res['status'] in ['Complete', 'Failed', 'Cancelled']:
        break
    time.sleep(0.3)

for r in res.get('results', []):
    msg = [f['value'] for f in r if f['field'] == '@message'][0]
    ts = [f['value'] for f in r if f['field'] == '@timestamp'][0]
    parsed = json.loads(msg)
    if parsed.get('bot', {}).get('name') == 'cara-health-bot-nova-2-sonic':
        print(f'=== LEX LOG FOR COACHING BOT ({ts}) ===')
        print(json.dumps(parsed, indent=2))
