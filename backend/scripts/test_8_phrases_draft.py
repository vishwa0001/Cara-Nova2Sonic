import boto3, json, time

session = boto3.Session(profile_name='careharmony-main', region_name='us-east-1')
lex = session.client('lexv2-models')
lambda_client = session.client('lambda')

bot_id = '4S3WG7D9ZQ'
alias_id = 'TSTALIASID'

# Wait for DRAFT to be stable Built/ReadyExpressTesting
print('Waiting for DRAFT status...')
for _ in range(30):
    loc = lex.describe_bot_locale(botId=bot_id, botVersion='DRAFT', localeId='en_US')
    status = loc.get('botLocaleStatus')
    print('DRAFT status:', status)
    if status in ['Built', 'ReadyExpressTesting']:
        break
    time.sleep(2)

phrases = [
    # 6 previously failed rebuttal/conversational phrases
    "who are you",
    "why are you calling",
    "my son handles my care",
    "I already have a similar service",
    "I'm happy with my doctor",
    "can you please reschedule the call at tomorrow ten a.m.?",
    # 2 acute crisis phrases
    "I want to kill myself",
    "I am having crushing chest pain"
]

print('\n=== TESTING ALL 8 PHRASES ON DRAFT (TSTALIASID, Threshold=0.75) ===')
results = []
for p in phrases:
    payload = {
        'Details': {
            'Parameters': {
                'operation': 'recognize_text',
                'botId': bot_id,
                'botAliasId': alias_id,
                'text': p,
                'sessionId': f'draft-test-{int(time.time()*1000)%100000}'
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
    results.append({
        'phrase': p,
        'matchedIntent': matched,
        'scores': score_map
    })
    print(f'Phrase: "{p}"')
    print(f'  -> Matched Intent: {matched}')
    print(f'  -> Scores: {score_map}')
