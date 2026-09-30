import boto3, json

session = boto3.Session(profile_name='careharmony-main', region_name='us-east-1')
lex = session.client('lexv2-models')

bot_id = '4S3WG7D9ZQ'
intents = lex.list_intents(botId=bot_id, botVersion='9', localeId='en_US')
for i in intents['intentSummaries']:
    detail = lex.describe_intent(botId=bot_id, botVersion='9', localeId='en_US', intentId=i['intentId'])
    name = i['intentName']
    print(f'=== {name} on Version 9 ===')
    print('  intentClosingSetting:', detail.get('intentClosingSetting'))
    print('  fulfillmentCodeHook:', detail.get('fulfillmentCodeHook'))
    print('  initialResponseSetting:', detail.get('initialResponseSetting'))
