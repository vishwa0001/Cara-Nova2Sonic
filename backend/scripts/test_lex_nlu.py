import boto3

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
lex = session.client("lexv2-runtime")

test_utterances = [
    "No, I'm his brother.",
    "No, I'm his sister.",
    "I'm his sister.",
    "No, I'm his mother.",
    "I'm his mom.",
    "No, I'm his roommate.",
    "I'm his roommate.",
    "No, I'm his neighbor.",
    "No, I'm his friend.",
    "No, I'm his coworker.",
    "No, I'm not him.",
    "No, this isn't him.",
    "You have someone else.",
    "I am his father.",
    "I'm his dad.",
    "No, this is his wife.",
    "No, I'm his son.",
    "No, this is her daughter.",
    "No, I'm his uncle.",
    "No, I'm his aunt.",
]

bot_id = "4M8I8HGPND"
alias_id = "KJZH9WFPLC"

print("--- LIVE LEX NLU RECOGNITION TESTS (IDENTITY BOT V6) ---")
for i, u in enumerate(test_utterances, 1):
    resp = lex.recognize_text(
        botId=bot_id,
        botAliasId=alias_id,
        localeId="en_US",
        sessionId=f"test-session-{i}",
        text=u,
    )
    interpretations = resp.get("interpretations", [])
    top = interpretations[0] if interpretations else {}
    intent_name = top.get("intent", {}).get("name")
    nlu_confidence = top.get("nluConfidence", {}).get("score", 0.0)
    print(f"[{i:02d}] '{u:28}' -> Intent: {intent_name:20} (Confidence: {nlu_confidence:.2f})")
