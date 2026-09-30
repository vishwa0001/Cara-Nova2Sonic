import boto3
import json
import datetime as dt
import time

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
logs = session.client("logs")

# Search last 7 days in Lex conversation logs
now = int(time.time())
start_time = now - (7 * 24 * 3600)

print("=== SEARCHING LEX LOGS FOR CAREGIVER / FAMILY / CARE PHRASES ===")
query_str = """
fields @timestamp, @message
| filter @message like /(?i)(son|daughter|family|kid|child|wife|husband|caregiver|handles|takes care)/
| sort @timestamp desc
| limit 100
"""

q_resp = logs.start_query(
    logGroupName="/aws/lex/cara-health-bot-conversations-258673",
    startTime=start_time,
    endTime=now + 600,
    queryString=query_str
)
qid = q_resp["queryId"]
for _ in range(25):
    res = logs.get_query_results(queryId=qid)
    if res["status"] in {"Complete", "Failed", "Cancelled"}:
        break
    time.sleep(1)

results = res.get("results", [])
print(f"Found {len(results)} events matching pattern in Lex logs.")
for r in results:
    msg = next((c["value"] for c in r if c["field"] == "@message"), "")
    try:
        d = json.loads(msg)
        bot_name = d.get("bot", {}).get("name")
        bot_ver = d.get("bot", {}).get("version")
        inp = d.get("inputTranscript")
        top_intent = d.get("sessionState", {}).get("intent", {}).get("name")
        interps = d.get("interpretations", [])
        scores = {it.get("intent", {}).get("name"): it.get("nluConfidence") for it in interps}
        contact_id = d.get("sessionState", {}).get("sessionAttributes", {}).get("caraHealthBotContactId")
        print(f"Contact: {contact_id} | Bot: {bot_name} (v{bot_ver}) | Input: {inp!r} | TopIntent: {top_intent} | Scores: {scores}")
    except Exception as e:
        print("Error parsing:", msg[:200])

print("\n=== SEARCHING CONNECT LOGS FOR SAFETY ROUTINGS ===")
q_resp2 = logs.start_query(
    logGroupName="/aws/connect/cara-health-bot-258673",
    startTime=start_time,
    endTime=now + 600,
    queryString='fields @timestamp, @message | filter @message like "Safetybehavioral" or @message like "Safetymedical" | sort @timestamp desc | limit 50'
)
qid2 = q_resp2["queryId"]
for _ in range(25):
    res2 = logs.get_query_results(queryId=qid2)
    if res2["status"] in {"Complete", "Failed", "Cancelled"}:
        break
    time.sleep(1)

results2 = res2.get("results", [])
print(f"Found {len(results2)} safety events in Connect logs.")
for r in results2:
    msg = next((c["value"] for c in r if c["field"] == "@message"), "")
    try:
        d = json.loads(msg)
        cid = d.get("ContactId")
        mod = d.get("ContactFlowModuleType")
        res = d.get("Results")
        ident = d.get("Identifier")
        ts = d.get("Timestamp")
        print(f"[{ts}] Contact: {cid} | Module: {mod} | Ident: {ident} | Results: {res}")
    except Exception as e:
        print("Error:", msg[:200])
