import boto3, json, time

session = boto3.Session(profile_name='careharmony-main', region_name='us-east-1')
logs = session.client('logs')

contact_ids = [
    '04539bb0-0631-403b-9b78-e6572894ce23',
    'aa05798c-a336-4a24-a8c8-7a240154b62e',
    '3e4eba51-de23-4cef-93b0-a89a395294b2',
    '0416a17b-0e55-4016-8986-ed886bf0cd68',
    '04b01fa1-aa48-421b-b2c6-17e39b2fdffa',
]

for cid in contact_ids:
    print('======================================================================')
    print(f'CONTACT ID: {cid}')
    print('======================================================================')
    
    query_str = f'''
    fields @timestamp, @message
    | filter @message like /{cid}/
    | sort @timestamp asc
    | limit 100
    '''
    
    # 1. Lex Logs
    print('\n>>> LEX LOGS:')
    try:
        start_query = logs.start_query(
            logGroupName='/aws/lex/cara-health-bot-conversations-258673',
            startTime=int(time.time()) - 14400,
            endTime=int(time.time()),
            queryString=query_str
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
            print(f'[{ts}] Bot: {parsed.get("bot")} | Transcript: "{parsed.get("inputTranscript")}"')
            print(f'  Interpretations: {json.dumps(parsed.get("interpretations"), indent=2)}')
            print(f'  DialogAction: {json.dumps(parsed.get("dialogAction"))}')
            print(f'  SessionState: {json.dumps(parsed.get("sessionState"))}')
    except Exception as e:
        print('Lex query error:', e)

    # 2. Connect Logs
    print('\n>>> CONNECT LOGS:')
    try:
        start_query = logs.start_query(
            logGroupName='/aws/connect/cara-health-bot-258673',
            startTime=int(time.time()) - 14400,
            endTime=int(time.time()),
            queryString=query_str
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
            print(f'[{ts}] Action: {parsed.get("ContactFlowActionType")} ({parsed.get("ContactFlowActionId")}) | Results: {parsed.get("Results")} | ErrorType: {parsed.get("ErrorType")} | Params: {json.dumps(parsed.get("Parameters"))[:120]}')
    except Exception as e:
        print('Connect query error:', e)
