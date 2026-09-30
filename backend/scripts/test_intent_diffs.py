import boto3, json, sys
from pathlib import Path
sys.path.insert(0, str(Path('.').resolve() / 'backend'))
from cara_health_bot.config import load_config
from cara_health_bot.builders import (
    safety_medical_intent_request,
    safety_behavioral_intent_request,
    general_conversation_intent_request,
)

session = boto3.Session(profile_name='careharmony-main', region_name='us-east-1')
lex = session.client('lexv2-models')
cfg = load_config()

def _intent_matches_expected(actual: dict, expected: dict) -> tuple[bool, str]:
    # 1. Compare sample utterances as normalized sets
    actual_samples = {
        x.get("utterance", "").strip().lower()
        for x in actual.get("sampleUtterances", [])
        if x.get("utterance")
    }
    expected_samples = {
        x.get("utterance", "").strip().lower()
        for x in expected.get("sampleUtterances", [])
        if x.get("utterance")
    }
    if actual_samples != expected_samples:
        return False, f"samples mismatch: {len(actual_samples)} vs {len(expected_samples)}"

    # 2. Compare intentClosingSetting
    actual_closing = actual.get("intentClosingSetting")
    expected_closing = expected.get("intentClosingSetting")
    actual_closing_active = (actual_closing or {}).get("active", False)
    expected_closing_active = (expected_closing or {}).get("active", False)
    if actual_closing_active != expected_closing_active:
        return False, f"intentClosingSetting active mismatch: actual={actual_closing_active} vs expected={expected_closing_active}"
    if expected_closing_active and actual_closing.get("nextStep") != expected_closing.get("nextStep"):
        return False, "intentClosingSetting nextStep mismatch"

    # 3. Compare fulfillmentCodeHook
    actual_hook = actual.get("fulfillmentCodeHook")
    expected_hook = expected.get("fulfillmentCodeHook")
    # If expected has no fulfillmentCodeHook (or no postFulfillmentStatusSpecification),
    # actual must NOT have a postFulfillmentStatusSpecification that closes or alters turns
    expected_has_post_spec = bool((expected_hook or {}).get("postFulfillmentStatusSpecification"))
    actual_has_post_spec = bool((actual_hook or {}).get("postFulfillmentStatusSpecification"))
    if actual_has_post_spec != expected_has_post_spec:
        return False, f"fulfillment post_spec mismatch: actual_has_post_spec={actual_has_post_spec} vs expected={expected_has_post_spec}"
    if expected_has_post_spec and actual_hook.get("postFulfillmentStatusSpecification") != expected_hook.get("postFulfillmentStatusSpecification"):
        return False, "fulfillment post_spec content mismatch"

    return True, "OK"

bot_id = "4S3WG7D9ZQ"
print("=== COACHING BOT V8 (SHOULD BE REJECTED) ===")
intents_v8 = lex.list_intents(botId=bot_id, botVersion="8", localeId="en_US")
by_name_v8 = {x["intentName"]: x["intentId"] for x in intents_v8["intentSummaries"]}
for name, builder in [("SafetyMedical", safety_medical_intent_request), ("SafetyBehavioral", safety_behavioral_intent_request), ("GeneralConversation", general_conversation_intent_request)]:
    act = lex.describe_intent(botId=bot_id, botVersion="8", localeId="en_US", intentId=by_name_v8[name])
    exp = builder(cfg, bot_id)
    print(name, _intent_matches_expected(act, exp))

print("\n=== COACHING BOT DRAFT (SHOULD BE ACCEPTED) ===")
intents_draft = lex.list_intents(botId=bot_id, botVersion="DRAFT", localeId="en_US")
by_name_draft = {x["intentName"]: x["intentId"] for x in intents_draft["intentSummaries"]}
for name, builder in [("SafetyMedical", safety_medical_intent_request), ("SafetyBehavioral", safety_behavioral_intent_request), ("GeneralConversation", general_conversation_intent_request)]:
    act = lex.describe_intent(botId=bot_id, botVersion="DRAFT", localeId="en_US", intentId=by_name_draft[name])
    exp = builder(cfg, bot_id)
    print(name, _intent_matches_expected(act, exp))
