import boto3
import json

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
lex = session.client("lexv2-runtime")

bot_id = "4S3WG7D9ZQ"
alias_id = "TZSUXEY14O"

test_utterances = [
    # Caregiver / Family phrases
    "my son handles my care",
    "Uh, my son handles my care",
    "my daughter handles my care",
    "my kid takes care of that",
    "my wife handles my care",
    "my husband takes care of that",
    "my daughter takes care of my medical stuff",
    "my son takes care of me",
    "my family handles my care",
    "someone else takes care of my medical needs",
    "my children handle that",
    
    # Other Rebuttals from Section 5
    "What is this call about?",
    "Why are you calling?",
    "Who is this?",
    "Who are you with?",
    "Why do I need this?",
    "I feel fine and don't want extra costs.",
    "I feel healthy.",
    "I already have a similar service.",
    "Why is there a copay?",
    "Why are you charging Medicare and my insurance?",
    "Where did you get my information?",
    "Why did my doctor refer me to this?",
    "Does this replace my primary doctor?",
    "How do I know this is not a scam?",
    "How much time will this take?",
    "I'm happy with the care I get from my doctor already.",
    
    # Declines / Callbacks
    "I'm not interested",
    "no I am not interested",
    "I don't have time right now",
    "call me back tomorrow",
    
    # Safety benchmarks (should match Safety)
    "I want to kill myself",
    "having severe chest pain",
    "someone is abusing me",
]

print(f"=== TESTING COACHING BOT LEX NLU (Bot: {bot_id}, Alias: {alias_id}) ===")
for i, u in enumerate(test_utterances, 1):
    resp = lex.recognize_text(
        botId=bot_id,
        botAliasId=alias_id,
        localeId="en_US",
        sessionId=f"coaching-test-{i}",
        text=u,
    )
    interpretations = resp.get("interpretations", [])
    top = interpretations[0] if interpretations else {}
    intent_name = top.get("intent", {}).get("name")
    nlu_confidence = top.get("nluConfidence", {}).get("score", 0.0)
    all_scores = {it.get("intent", {}).get("name"): it.get("nluConfidence", {}).get("score") for it in interpretations if it.get("intent")}
    print(f"[{i:02d}] '{u}'")
    print(f"     -> Top: {intent_name} (Conf: {nlu_confidence}) | All: {all_scores}")
