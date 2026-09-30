import boto3, json

session = boto3.Session(profile_name='careharmony-main', region_name='us-east-1')
lex = session.client('lexv2-models')
lambda_client = session.client('lambda')

bot_id = '4S3WG7D9ZQ'
alias_id = 'TZSUXEY14O'

print('=== 1. VERIFY LIVE ALIAS AND BOT VERSION 10 ===')
alias_info = lex.describe_bot_alias(botId=bot_id, botAliasId=alias_id)
live_ver = alias_info.get('botVersion')
print(f'Alias ID: {alias_id}')
print(f'Live Bot Version on Alias: {live_ver}')
print(f'Alias Last Updated: {alias_info.get("lastUpdatedDateTime")}')

ver_info = lex.describe_bot_version(botId=bot_id, botVersion=live_ver)
print(f'Version {live_ver} Created: {ver_info.get("creationDateTime")}')
print(f'Version {live_ver} Status: {ver_info.get("botStatus")}')

locale_info = lex.describe_bot_locale(botId=bot_id, botVersion=live_ver, localeId='en_US')
print(f'Version {live_ver} Threshold: {locale_info.get("nluIntentConfidenceThreshold")}')
print(f'Version {live_ver} Locale Status: {locale_info.get("botLocaleStatus")}')

print('\n=== 2. INTENTS ON LIVE VERSION 10 ===')
intents = lex.list_intents(botId=bot_id, botVersion=live_ver, localeId='en_US')
intent_names = [i['intentName'] for i in intents.get('intentSummaries', [])]
print('Intents on Version 10:', intent_names)
assert 'GeneralConversation' not in intent_names, 'GeneralConversation must be absent!'
assert set(intent_names) == {'AmazonQinConnect', 'FallbackIntent', 'SafetyMedical', 'SafetyBehavioral'}

print('\n=== 3. TEST RECOGNIZE_TEXT ON LIVE VERSION 10 VIA LAMBDA ===')
test_phrases = [
    # 6 benign / rebuttal phrases
    "who are you",
    "why are you calling",
    "my son handles my care",
    "I already have a similar service",
    "I'm happy with my doctor",
    "can you please reschedule the call at tomorrow ten a.m.?",
    # 2 crisis phrases
    "I want to kill myself",
    "I am having crushing chest pain",
    # transfer consent
    "yes please connect"
]

for p in test_phrases:
    payload = {
        'Details': {
            'Parameters': {
                'operation': 'recognize_text',
                'botId': bot_id,
                'botAliasId': alias_id,
                'text': p,
                'sessionId': f'live-v10-test-{abs(hash(p)) % 100000}'
            }
        }
    }
    resp = lambda_client.invoke(
        FunctionName='cara-health-bot-session-context',
        Payload=json.dumps(payload)
    )
    res = json.loads(resp['Payload'].read())
    matched = res.get('matchedIntent')
    interps = json.loads(res.get('interpretations', '[]'))
    score_map = {}
    for i in interps:
        iname = (i.get('intent') or {}).get('name')
        sc = i.get('nluConfidence')
        score_map[iname] = sc.get('score') if sc else None
    print(f'Phrase: "{p}"')
    print(f'  -> Matched Intent: {matched}')
    print(f'  -> Scores: {score_map}')
