import boto3, json, time

session = boto3.Session(profile_name='careharmony-main', region_name='us-east-1')
logs = session.client('logs')

contact_ids = [
    ('04539bb0-0631-403b-9b78-e6572894ce23', 'Why are you calling me?'),
    ('aa05798c-a336-4a24-a8c8-7a240154b62e', 'Uh, why are you calling?'),
    ('3e4eba51-de23-4cef-93b0-a89a395294b2', 'Uh, I already have a similar service.'),
    ('0416a17b-0e55-4016-8986-ed886bf0cd68', 'My son handles my care.'),
    ('04b01fa1-aa48-421b-b2c6-17e39b2fdffa', 'My son handles my care.'),
]

for cid, spoken in contact_ids:
    print('======================================================================')
    print(f'CONTACT: {cid} | Spoken: "{spoken}"')
    print('======================================================================')
    query_str = f'fields @timestamp, @message | filter @message like /{cid}/ | sort @timestamp asc | limit 100'
    
    # Lex
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
        p = json.loads(msg)
        bot = p.get('bot', {})
        interps = p.get('interpretations', [])
        top_interp = interps[0] if interps else {}
        top_intent = top_interp.get('intent', {}).get('name')
        top_conf = top_interp.get("nluConfidence")
        if isinstance(top_conf, dict):
            top_conf = top_conf.get("score")
        transcript = p.get("inputTranscript")
        print(f'LEX: Bot={bot.get("name")} v{bot.get("version")} | Utterance="{transcript}" | TopIntent={top_intent} (conf: {top_conf}) | Action={p.get("dialogAction", {}).get("type") or p.get("sessionState", {}).get("dialogAction", {}).get("type")}')
        for i in interps:
            c = i.get("nluConfidence")
            if isinstance(c, dict):
                c = c.get("score")
            print(f'   - Interp: {i.get("intent", {}).get("name")} (conf: {c})')

    # Connect
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
        p = json.loads(msg)
        flow_id = p.get('ContactFlowActionId') or p.get('ActionId') or p.get('Identifier')
        res_val = p.get('Results')
        params = p.get('Parameters')
        param_text = ''
        if params:
            if 'Text' in params:
                param_text = f'Text="{params["Text"][:60]}..."'
            elif 'ComparisonMethod' in params:
                param_text = f'Compare val="{params.get("Value")}" vs "{params.get("SecondValue")}"'
            elif 'BotAliasArn' in params:
                param_text = f'LexBotAlias={params.get("BotAliasArn").split("/")[-2]}'
        print(f'CONNECT: [{ts}] Node={flow_id} | Results={res_val} | {param_text}')

