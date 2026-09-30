import boto3
import json
import time

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
logs = session.client("logs")

contact_ids = ["527de138-de79-4f76-a0a5-97796fd75dfe", "1613b3d3-0983-433b-94a3-17bf7faaac0b"]

for cid in contact_ids:
    print(f"\n================ CONTACT: {cid} ================")
    for lg in ["/aws/lex/cara-health-bot-conversations-258673", "/aws/connect/cara-health-bot-258673"]:
        print(f"--- Log Group: {lg} ---")
        q_resp = logs.start_query(
            logGroupName=lg,
            startTime=int(time.time()) - (24 * 3600),
            endTime=int(time.time()) + 600,
            queryString=f'fields @timestamp, @message | filter @message like "{cid}" | sort @timestamp asc | limit 100'
        )
        qid = q_resp["queryId"]
        for _ in range(20):
            res = logs.get_query_results(queryId=qid)
            if res["status"] in {"Complete", "Failed", "Cancelled"}:
                break
            time.sleep(1)
        
        results = res.get("results", [])
        for r in results:
            msg = next((c["value"] for c in r if c["field"] == "@message"), "")
            print(msg)
