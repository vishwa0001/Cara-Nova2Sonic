import boto3, time, json

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
logs = session.client("logs")

def query_logs(log_group, query, start_time_offset=86400*2):
    print(f"\n=== Log Group: {log_group} ===")
    q_resp = logs.start_query(
        logGroupName=log_group,
        startTime=int(time.time()) - start_time_offset,
        endTime=int(time.time()) + 600,
        queryString=query
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
        ts = next((c["value"] for c in r if c["field"] == "@timestamp"), "")
        msg = next((c["value"] for c in r if c["field"] == "@message"), "")
        print(f"[{ts}] {msg.strip()}")

query_logs("/aws/lambda/cara-health-bot-session-context", 'fields @timestamp, @message | filter @message like "Transfer-time" | sort @timestamp desc | limit 20')
query_logs("/aws/connect/cara-health-bot-258673", 'fields @timestamp, @message | sort @timestamp desc | limit 50')
