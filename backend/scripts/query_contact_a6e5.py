import boto3
import time
import json

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
logs = session.client("logs")

contact_id = "a6e548e4-5306-4132-968f-0f056913d1b0"

log_groups = [
    "/aws/connect/cara-health-bot-258673",
    "/aws/lex/cara-health-bot-conversations-258673",
    "/aws/lambda/cara-health-bot-campaign-dialer",
    "/aws/lambda/cara-health-bot-session-context",
    "/aws/lambda/cara-health-bot-agent-availability",
]

for lg in log_groups:
    print(f"\n==================== Log Group: {lg} ====================")
    try:
        q_resp = logs.start_query(
            logGroupName=lg,
            startTime=int(time.time()) - 86400 * 2,
            endTime=int(time.time()) + 600,
            queryString=f'fields @timestamp, @message | filter @message like "{contact_id}" | sort @timestamp asc | limit 200'
        )
        query_id = q_resp["queryId"]
        for _ in range(15):
            res = logs.get_query_results(queryId=query_id)
            if res["status"] in {"Complete", "Failed", "Cancelled"}:
                break
            time.sleep(1)
        results = res.get("results", [])
        print(f"Results: {len(results)}")
        for r in results:
            msg = next((c["value"] for c in r if c["field"] == "@message"), "")
            try:
                parsed = json.loads(msg)
                print(json.dumps(parsed, indent=2))
            except:
                print(msg)
    except Exception as e:
        print(f"Error querying {lg}: {e}")
