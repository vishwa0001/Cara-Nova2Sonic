import boto3, json

session = boto3.Session(profile_name='careharmony-main', region_name='us-east-1')
lex = session.client('lexv2-models')
bot_id = '4S3WG7D9ZQ'
alias_id = 'TZSUXEY14O'

alias_resp = lex.describe_bot_alias(botId=bot_id, botAliasId=alias_id)
live_version = alias_resp.get('botVersion')
print('Live version on alias TZSUXEY14O:', live_version)
print('Alias last updated:', str(alias_resp.get('lastUpdatedDateTime')))

versions = lex.list_bot_versions(botId=bot_id)
print('All bot versions:')
for v in versions.get('botVersionSummaries', []):
    print(' - Version', v.get('botVersion'), 'status=', v.get('botStatus'), 'created=', str(v.get('creationDateTime')))

intents = lex.list_intents(botId=bot_id, botVersion=live_version, localeId='en_US')
for i in intents.get('intentSummaries', []):
    if i['intentName'] == 'GeneralConversation':
        detail = lex.describe_intent(botId=bot_id, botVersion=live_version, localeId='en_US', intentId=i['intentId'])
        print('GeneralConversation config on live version', live_version, ':')
        print('intentClosingSetting:', detail.get('intentClosingSetting'))
        print('fulfillmentCodeHook:', detail.get('fulfillmentCodeHook'))
        print('initialResponseSetting:', detail.get('initialResponseSetting'))
