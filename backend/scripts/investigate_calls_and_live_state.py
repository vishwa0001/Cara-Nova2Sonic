import boto3, json, time

session = boto3.Session(profile_name='careharmony-main', region_name='us-east-1')
lex = session.client('lexv2-models')
logs = session.client('logs')

bot_id = '4S3WG7D9ZQ'
alias_id = 'TZSUXEY14O'

print('=== 1. LIVE ALIAS AND BOT VERSION ===')
alias_info = lex.describe_bot_alias(botId=bot_id, botAliasId=alias_id)
live_ver = alias_info.get('botVersion')
print(f'Alias ID: {alias_id}')
print(f'Live Bot Version on Alias: {live_ver}')
print(f'Alias Last Updated: {alias_info.get("lastUpdatedDateTime")}')
print(f'Alias Status: {alias_info.get("botAliasStatus")}')
print('Bot Alias Locale Settings:')
print(json.dumps(alias_info.get('botAliasLocaleSettings'), indent=2))

ver_info = lex.describe_bot_version(botId=bot_id, botVersion=live_ver)
print(f'\nVersion {live_ver} Created: {ver_info.get("creationDateTime")}')
print(f'Version {live_ver} Status: {ver_info.get("botStatus")}')

print('\n=== 2. GENERALCONVERSATION ON LIVE VERSION ===')
intents = lex.list_intents(botId=bot_id, botVersion=live_ver, localeId='en_US')
for i in intents.get('intentSummaries', []):
    if i['intentName'] == 'GeneralConversation':
        detail = lex.describe_intent(botId=bot_id, botVersion=live_ver, localeId='en_US', intentId=i['intentId'])
        print(f'GeneralConversation ID: {i["intentId"]}')
        print('intentClosingSetting:', detail.get('intentClosingSetting'))
        print('fulfillmentCodeHook:', detail.get('fulfillmentCodeHook'))
        print('initialResponseSetting:', detail.get('initialResponseSetting'))

print('\n=== 3. CLOUDWATCH LOGS FOR 6046a9a8 AND 86dd9e39 ===')
contacts = [
    '6046a9a8-038b-4d1c-89d7-5819d428e253',
    '86dd9e39-c622-46fe-96c2-259e1f6c3566'
]

for cid in contacts:
    print(f'\n**************** CONTACT {cid} ****************')
    for lg in ['/aws/lex/cara-health-bot-conversations-258673', '/aws/connect/cara-health-bot-258673']:
        print(f'--- Log Group: {lg} ---')
        query_str = f'fields @timestamp, @message | filter @message like /{cid}/ | sort @timestamp asc | limit 100'
        start_query = logs.start_query(
            logGroupName=lg,
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
            try:
                parsed = json.loads(msg)
                if 'bot' in parsed:
                    print(f'[{ts}] LEX LOG:')
                    print(f'  Bot: {parsed.get("bot")}')
                    print(f'  Transcript: "{parsed.get("inputTranscript")}"')
                    print(f'  Interpretations: {json.dumps(parsed.get("interpretations"), indent=2)}')
                    print(f'  DialogAction: {json.dumps(parsed.get("dialogAction"))}')
                    print(f'  SessionState: {json.dumps(parsed.get("sessionState"), indent=2)}')
                elif 'ContactId' in parsed:
                    action_type = parsed.get('ContactFlowActionType')
                    action_id = parsed.get('ContactFlowActionId') or parsed.get('ActionId') or parsed.get('Identifier')
                    results = parsed.get('Results')
                    params = parsed.get('Parameters')
                    print(f'[{ts}] CONNECT LOG: Node={action_id} Action={action_type} Results={results} Params={json.dumps(params)[:120]}')
            except Exception:
                print(f'[{ts}] {msg[:200]}')
