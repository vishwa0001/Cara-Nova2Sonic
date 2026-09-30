import boto3
import time
import json

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
logs = session.client("logs")

q_resp = logs.start_query(
    logGroupName="/aws/connect/cara-health-bot-258673",
    startTime=int(time.time()) - 86400 * 7,
    endTime=int(time.time()) + 600,
    queryString='fields @timestamp, @message | filter @message like "Invalid SSML input" | sort @timestamp desc | limit 50'
)
query_id = q_resp["queryId"]
for _ in range(15):
    res = logs.get_query_results(queryId=query_id)
    if res["status"] in {"Complete", "Failed", "Cancelled"}:
        break
    time.sleep(1)

results = res.get("results", [])
print(f"Total SSML Error Occurrences in Connect logs: {len(results)}")
for idx, r in enumerate(results, 1):
    msg = next((c["value"] for c in r if c["field"] == "@message"), "")
    try:
        parsed = json.loads(msg)
        print(f"[{idx}] Timestamp: {parsed.get('Timestamp')} | ContactId: {parsed.get('ContactId')}")
    except:
        print(f"[{idx}] {msg[:150]}")
