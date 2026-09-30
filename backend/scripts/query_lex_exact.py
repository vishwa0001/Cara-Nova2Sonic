import boto3
import json
import datetime as dt

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
logs = session.client("logs")

# 2026-09-03 11:30:00 to 11:40:00 UTC
start_ts = int(dt.datetime(2026, 9, 3, 11, 30, 0, tzinfo=dt.timezone.utc).timestamp())
end_ts = int(dt.datetime(2026, 9, 3, 11, 40, 0, tzinfo=dt.timezone.utc).timestamp())

log_group = "/aws/lex/cara-health-bot-conversations-258673"
print(f"Querying {log_group} from {start_ts} to {end_ts}...")

q_resp = logs.start_query(
    logGroupName=log_group,
    startTime=start_ts,
    endTime=end_ts,
    queryString="fields @timestamp, @message | sort @timestamp asc | limit 100"
)
qid = q_resp["queryId"]
import time
for _ in range(20):
    res = logs.get_query_results(queryId=qid)
    if res["status"] in {"Complete", "Failed", "Cancelled"}:
        break
    time.sleep(1)

results = res.get("results", [])
print(f"Found {len(results)} events")
for r in results:
    msg = next((c["value"] for c in r if c["field"] == "@message"), "")
    try:
        d = json.loads(msg)
        print("="*40)
        print("Bot:", d.get("bot"))
        print("InputTranscript:", d.get("inputTranscript"))
        print("SessionAttributes:", d.get("sessionState", {}).get("sessionAttributes"))
        print("Intent:", d.get("sessionState", {}).get("intent"))
        print("DialogAction:", d.get("sessionState", {}).get("dialogAction"))
        print("Interpretations:")
        print(json.dumps(d.get("interpretations"), indent=2))
    except Exception as e:
        print("RAW:", msg)
