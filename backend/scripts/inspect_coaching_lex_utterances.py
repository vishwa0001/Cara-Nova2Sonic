import boto3
import json

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
lex = session.client("lexv2-models")

bot_id = "4S3WG7D9ZQ"
locale_id = "en_US"

# List intents for Bot Version 7 and DRAFT
for ver in ["7", "DRAFT"]:
    print(f"\n================ BOT ID: {bot_id} VERSION: {ver} ================")
    try:
        intents_resp = lex.list_intents(botId=bot_id, botVersion=ver, localeId=locale_id)
        for intent in intents_resp.get("intentSummaries", []):
            intent_id = intent["intentId"]
            intent_name = intent["intentName"]
            desc = lex.describe_intent(botId=bot_id, botVersion=ver, localeId=locale_id, intentId=intent_id)
            utterances = [u["utterance"] for u in desc.get("sampleUtterances", [])]
            print(f"\nIntent: {intent_name} ({len(utterances)} utterances)")
            
            # Check for words like son, daughter, care, handles, caregiver, etc.
            matches = [u for u in utterances if any(w in u.lower() for w in ["son", "daughter", "care", "handle", "child", "family", "mom", "dad"])]
            if matches:
                print(f"  --> MATCHES for family/care words in {intent_name}:")
                for m in matches:
                    print(f"      - {m}")
            
            # Print all utterances if SafetyBehavioral or SafetyMedical
            if intent_name in ["SafetyBehavioral", "SafetyMedical"]:
                print(f"  All sample utterances in {intent_name}:")
                for u in utterances:
                    print(f"    * {u}")
    except Exception as e:
        print(f"Error for version {ver}: {e}")
