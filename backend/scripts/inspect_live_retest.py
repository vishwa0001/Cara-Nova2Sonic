import boto3, json, time

session = boto3.Session(profile_name='careharmony-main', region_name='us-east-1')
lex = session.client('lexv2-models')
logs = session.client('logs')

bot_id = '4S3WG7D9ZQ'
alias_id = 'TZSUXEY14O'

print('=== 1. CHECKING LIVE BOT VERSION AND ALIAS ===')
alias_resp = lex.describe_bot_alias(botId=bot_id, botAliasId=alias_id)
live_version = alias_resp.get('botVersion')
print('Live version on alias TZSUXEY14O:', live_version)
print('Alias last updated:', alias_resp.get('lastUpdatedDateTime'))

ver_resp = lex.describe_bot_version(botId=bot_id, botVersion=live_version)
print('Version created:', ver_resp.get('creationDateTime'), 'Status:', ver_resp.get('botStatus'))

# List all versions
versions = lex.list_bot_versions(botId=bot_id)
print('All bot versions:')
for v in versions.get('botVersionSummaries', []):
    print(f' - Version {v.get("botVersion")}: status={v.get("botStatus")} created={v.get("creationDateTime")}')

print('\n=== 2. DESCRIBE GENERALCONVERSATION ON LIVE VERSION ===')
intents = lex.list_intents(botId=bot_id, botVersion=live_version, localeId='en_US')
for i in intents.get('intentSummaries', []):
    if i['intentName'] == 'GeneralConversation':
        detail = lex.describe_intent(botId=bot_id, botVersion=live_version, localeId='en_US', intentId=i['intentId'])
        print('GeneralConversation config on live version', live_version, ':')
        print('intentClosingSetting:', detail.get('intentClosingSetting'))
        print('fulfillmentCodeHook:', detail.get('fulfillmentCodeHook'))
        print('initialResponseSetting:', detail.get('initialResponseSetting'))

print('\n=== 3. QUERY CLOUDWATCH FOR CONTACT 0cbfbdb6-2f0d-43db-a7b4-e084539e6343 ===')
cid = '0cbfbdb6-2f0d-43db-a7b4-e084539e6343'
query_str = f'fields @timestamp, @message | filter @message like /{cid}/ | sort @timestamp asc | limit 100'

for lg in ['/aws/lex/cara-health-bot-conversations-258673', '/aws/connect/cara-health-bot-258673']:
    print(f'\n--- Log Group: {lg} ---')
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
                print(f'[{ts}] CONNECT LOG: Node={action_id} Action={action_type} Results={parsed.get("Results")} Error={parsed.get("ErrorType")} Params={json.dumps(parsed.get("Parameters"))[:100]}')
        except Exception:
            print(f'[{ts}] {msg[:200]}')
