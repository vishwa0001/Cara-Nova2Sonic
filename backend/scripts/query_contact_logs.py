import boto3, time, json

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
logs = session.client("logs")

def query_logs(log_group, query):
    print(f"\n=== Log Group: {log_group} ===")
    q_resp = logs.start_query(
        logGroupName=log_group,
        startTime=int(time.time()) - 86400 * 2,
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
        msg = next((c["value"] for c in r if c["field"] == "@message"), "")
        try:
            parsed = json.loads(msg)
            print(json.dumps(parsed, indent=2))
        except:
            print(msg)

query_logs("/aws/connect/cara-health-bot-258673", 'fields @timestamp, @message | filter @message like "45b17ce4-47c1-4ada-8b52-9df10d331a71" | sort @timestamp asc | limit 100')
