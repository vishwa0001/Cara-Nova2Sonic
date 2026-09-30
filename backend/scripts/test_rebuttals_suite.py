#!/usr/bin/env python3
"""
Test script for Rebuttals Content Fix:
1. Multi-turn test for 5+ rebuttal categories (Quick Redirect -> Pushback / Full Answer -> Transfer Resume)
2. 30-case Coaching Lex NLU regression test
"""
import os
import re
import json
import yaml
import boto3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
bedrock = session.client("bedrock-runtime")
lex = session.client("lexv2-runtime")

COACHING_BOT_ID = "4S3WG7D9ZQ"
COACHING_ALIAS_ID = "TZSUXEY14O"

with open(ROOT / "prompts" / "life-coach.yaml", "r", encoding="utf-8") as f:
    prompt_cfg = yaml.safe_load(f)

raw_system = prompt_cfg.get("system", "")
system_prompt = (
    raw_system.replace("{{$.Custom.customerName}}", "Kevin Peterson")
    .replace("{{$.Custom.firstName}}", "Kevin")
    .replace("{{$.Custom.practiceName}}", "Mercy Health")
    .replace("{{$.Custom.providerName}}", "Dr. Smith")
    .replace("{{$.Custom.direct_agent}}", "false")
    .replace("{{$.Custom.expectedPhone}}", "+18148316822")
    .replace("{{$.Custom.identityConfirmed}}", "true")
    .replace("{{$.locale}}", "en-US")
    .replace("${CaraBehaviorConfig}", json.dumps({"agentName": "Cara", "practiceName": "Mercy Health"}))
)

tools = [
    {
        "toolSpec": {
            "name": "EscalateToHuman",
            "description": "Return control when the confirmed customer is ready for a human specialist.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "conversationSummary": {"type": "string", "description": "Brief neutral summary."},
                        "customerIntent": {"type": "string", "description": "Brief phrase describing what customer wants."}
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "RequestCallback",
            "description": "Return control when the confirmed customer wants to be called later.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "callbackWhen": {"type": "string", "description": "Timing for callback."},
                        "callbackReason": {"type": "string", "description": "Reason for callback."}
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "EndConversation",
            "description": "End conversation on refusal, do-not-call, or safety.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "endReason": {"type": "string", "enum": ["refusal", "do_not_call", "safety_medical", "safety_behavioral", "other"]}
                    },
                    "required": ["endReason"]
                }
            }
        }
    }
]

def _norm(s):
    return re.sub(r"\s+", " ", s.replace("—", "-").replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()

def invoke_model(messages):
    response = bedrock.converse(
        modelId="us.amazon.nova-lite-v1:0",
        system=[{"text": system_prompt}],
        messages=messages,
        inferenceConfig={"temperature": 0.0, "maxTokens": 400},
        toolConfig={"tools": tools}
    )
    output = response["output"]["message"]
    text = ""
    tool_use = None
    for content in output.get("content", []):
        if "text" in content:
            text += content["text"]
        if "toolUse" in content:
            tool_use = content["toolUse"]
    return text, tool_use, output

def test_rebuttal_category(category_name, trigger_utterance, pushback_utterance, expected_quick, expected_full):
    print(f"\n=======================================================")
    print(f"TESTING CATEGORY: {category_name}")
    print(f"Trigger: '{trigger_utterance}'")
    print(f"Pushback: '{pushback_utterance}'")
    print(f"=======================================================")
    
    # Turn 1: Caller asks question
    messages = [
        {"role": "user", "content": [{"text": trigger_utterance}]}
    ]
    t1_text, t1_tool, t1_msg = invoke_model(messages)
    print(f"[Turn 1 Response]:\n{t1_text}\n(Tool: {t1_tool})")
    
    clean_t1 = re.sub(r"<message>|</message>", "", t1_text).strip()
    norm_t1 = _norm(clean_t1)
    norm_eq = _norm(expected_quick)
    quick_passed = norm_eq in norm_t1 or all(w in norm_t1 for w in norm_eq.split()[:8])
    print(f"  -> Quick Redirect match: {'PASS' if quick_passed else 'FAIL'}")
    
    # Turn 2: Caller pushes back / asks for more detail
    messages.append(t1_msg)
    messages.append({"role": "user", "content": [{"text": pushback_utterance}]})
    t2_text, t2_tool, t2_msg = invoke_model(messages)
    print(f"\n[Turn 2 Response (Pushback / Full Answer)]:\n{t2_text}\n(Tool: {t2_tool})")
    
    clean_t2 = re.sub(r"<message>|</message>", "", t2_text).strip()
    norm_t2 = _norm(clean_t2)
    norm_ef = _norm(expected_full)
    full_passed = norm_ef in norm_t2 or all(w in norm_t2 for w in norm_ef.split()[:8])
    print(f"  -> Full Answer match: {'PASS' if full_passed else 'FAIL'}")
    
    # Turn 3: Caller agrees to transfer ("Sure, connect me")
    messages.append(t2_msg)
    messages.append({"role": "user", "content": [{"text": "Sure, go ahead and connect me."}]})
    t3_text, t3_tool, t3_msg = invoke_model(messages)
    print(f"\n[Turn 3 Response (Consent -> Resume Transfer)]:\n{t3_text}\n(Tool: {t3_tool})")
    
    resume_passed = (t3_tool is not None and t3_tool.get("name") == "EscalateToHuman") or ("connect" in t3_text.lower() and "specialist" in t3_text.lower())
    print(f"  -> Resume Transfer (EscalateToHuman / Transfer notice): {'PASS' if resume_passed else 'FAIL'}")
    
    overall = quick_passed and full_passed and resume_passed
    return overall

def run_30_nlu_regression():
    print("\n=======================================================")
    print("RUNNING 30-CASE COACHING LEX NLU REGRESSION SUITE")
    print("=======================================================")
    test_cases = [
        # Rebuttal topics (expected: RebuttalReasonForCall, RebuttalWhoAreYou, or AmazonQinConnect)
        ("What is this call about?", {"RebuttalReasonForCall", "AmazonQinConnect"}),
        ("Why are you calling me?", {"RebuttalReasonForCall", "AmazonQinConnect"}),
        ("Who is this?", {"RebuttalWhoAreYou", "AmazonQinConnect"}),
        ("Who are you with?", {"RebuttalWhoAreYou", "AmazonQinConnect"}),
        ("Why do I need this?", {"AmazonQinConnect", "FallbackIntent"}),
        ("I feel fine and don't want extra costs.", {"AmazonQinConnect", "FallbackIntent"}),
        ("I feel healthy.", {"AmazonQinConnect", "FallbackIntent"}),
        ("My son handles my care.", {"AmazonQinConnect", "FallbackIntent"}),
        ("My daughter handles my medical stuff.", {"AmazonQinConnect", "FallbackIntent"}),
        ("I already have a similar service.", {"AmazonQinConnect", "FallbackIntent"}),
        ("Why is there a copay?", {"AmazonQinConnect", "FallbackIntent"}),
        ("Why are you charging Medicare and my insurance?", {"AmazonQinConnect", "FallbackIntent"}),
        ("Where did you get my information?", {"AmazonQinConnect", "FallbackIntent"}),
        ("Why did my doctor refer me to this?", {"AmazonQinConnect", "FallbackIntent"}),
        ("Does this replace my primary doctor?", {"AmazonQinConnect", "FallbackIntent"}),
        ("How do I know this is not a scam?", {"AmazonQinConnect", "FallbackIntent"}),
        ("How much time will this take?", {"AmazonQinConnect", "FallbackIntent"}),
        ("I'm happy with the care I get from my doctor already.", {"AmazonQinConnect", "FallbackIntent"}),
        # Declines (expected: CallRefusal)
        ("I'm not interested", {"CallRefusal"}),
        ("No thanks, I don't want this", {"CallRefusal"}),
        ("Stop calling me", {"CallRefusal"}),
        ("No, I'm not interested", {"CallRefusal"}),
        # Callbacks (expected: AmazonQinConnect or FallbackIntent handled by Q)
        ("I don't have time right now", {"AmazonQinConnect", "FallbackIntent"}),
        ("Call me back tomorrow at 10 AM", {"AmazonQinConnect", "FallbackIntent"}),
        ("Can you call me later?", {"AmazonQinConnect", "FallbackIntent"}),
        # Affirmatives (expected: AmazonQinConnect or FallbackIntent handled by Q)
        ("Yes please connect me", {"AmazonQinConnect", "FallbackIntent"}),
        ("Sure, go ahead", {"AmazonQinConnect", "FallbackIntent"}),
        # Safety Emergencies (expected: SafetyMedical / SafetyBehavioral)
        ("I want to kill myself", {"SafetyBehavioral"}),
        ("I'm having severe crushing chest pain", {"SafetyMedical"}),
        ("Someone is abusing me", {"SafetyBehavioral"}),
    ]
    
    lambda_client = session.client("lambda")
    passed_count = 0
    for idx, (phrase, expected_intents) in enumerate(test_cases, 1):
        payload = {
            "Details": {
                "Parameters": {
                    "operation": "recognize_text",
                    "botId": COACHING_BOT_ID,
                    "botAliasId": COACHING_ALIAS_ID,
                    "text": phrase,
                    "sessionId": f"nlu-reg-test-{idx}",
                }
            }
        }
        resp = lambda_client.invoke(
            FunctionName="cara-health-bot-session-context",
            Payload=json.dumps(payload)
        )
        res = json.loads(resp["Payload"].read())
        top_intent = res.get("matchedIntent") or "None"
        is_pass = top_intent in expected_intents
        if is_pass:
            passed_count += 1
        print(f"[{idx:02d}/30] '{phrase}' -> Top: {top_intent} [{'PASS' if is_pass else 'FAIL'}]")
        
    print(f"\nNLU Regression Result: {passed_count}/{len(test_cases)}")
    return passed_count == len(test_cases), passed_count

def main():
    categories_to_test = [
        (
            "reason_for_call",
            "What is this call about?",
            "Can you give me more detail about that?",
            "We're calling on behalf of Mercy Health about your care. Hold one second and I'll get you to the team who can walk you through it.",
            "We're CareHarmony, reaching out on Mercy Health's behalf about your care. I'll connect you with the team so they can cover everything in detail."
        ),
        (
            "who_are_you",
            "Who are you with?",
            "Tell me more about who you are and what you do.",
            "This is Cara with CareHarmony, calling on Mercy Health's behalf. Let me get you to the care team for the full picture.",
            "We're CareHarmony — we partner with Mercy Health to support your care between visits. I'll connect you now so the team can explain exactly how it works."
        ),
        (
            "why_needed",
            "Why do I need this?",
            "Why would my doctor recommend that? Give me the full answer.",
            "Great question — hang tight while I connect you with the team, they can explain it far better than I can.",
            "A lot of people put their health on the back burner until something goes wrong. This program is built to catch things early, before they turn into a crisis, by giving you a dedicated professional who checks in and handles the tasks that pile up."
        ),
        (
            "cost",
            "I feel fine and don't want extra costs.",
            "Why would Medicare cover this? Can you explain the full cost details?",
            "I hear you — the team can walk through the cost side in detail. One moment while I connect you.",
            "I get not wanting extra out-of-pocket cost while managing your health — this is meant to help, not add a burden. The idea is to keep you out of the ER or urgent care, which tends to cost far more and take a bigger toll on your health."
        ),
        (
            "scam",
            "How do I know this is not a scam?",
            "I need more proof before I talk to anyone.",
            "Totally fair to ask — we work directly with your doctor's office. Let me connect you with your nurse, who can verify everything.",
            "That's a smart thing to check. We work hand-in-hand with your doctor's office, and your nurse can verify all of this directly with you once connected."
        ),
    ]
    
    results = {}
    for cat, trig, push, quick, full in categories_to_test:
        passed = test_rebuttal_category(cat, trig, push, quick, full)
        results[cat] = "PASS" if passed else "FAIL"
        
    print("\n=======================================================")
    print("5 CATEGORY MULTI-TURN SUMMARY:")
    for cat, res in results.items():
        print(f"  - {cat}: {res}")
        
    nlu_pass, nlu_count = run_30_nlu_regression()
    
    print("\n=======================================================")
    print(f"FINAL SUMMARY: 5 Categories: {all(v == 'PASS' for v in results.values())} | NLU: {nlu_count}/30")
    print("=======================================================")

if __name__ == "__main__":
    main()
