import boto3, json

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
logs = session.client("logs")

print("==================== 1. CONNECT LOGS (Latest Stream) ====================")
try:
    events = logs.get_log_events(
        logGroupName="/aws/connect/cara-health-bot-258673",
        logStreamName="2026/09/02/23/stream-Z_nkAmYJPOOu11aUw5UDsA==",
        limit=100
    )
    for e in events.get("events", []):
        try:
            parsed = json.loads(e["message"])
            print(json.dumps(parsed, indent=2))
        except:
            print(e["message"])
except Exception as ex:
    print("Connect error:", ex)

print("\n==================== 2. SESSION CONTEXT LAMBDA LOGS (Latest Stream) ====================")
try:
    events = logs.get_log_events(
        logGroupName="/aws/lambda/cara-health-bot-session-context",
        logStreamName="2026/09/02/[$LATEST]b7acffc203094538afb865e826e8d606",
        limit=100
    )
    for e in events.get("events", []):
        print(e["message"].strip())
except Exception as ex:
    print("Lambda error:", ex)

print("\n==================== 3. LEX CONVERSATIONS LOGS (Latest Stream) ====================")
try:
    events = logs.get_log_events(
        logGroupName="/aws/lex/cara-health-bot-conversations-258673",
        logStreamName="cara-health-bot/en_US/908715bf695ea7bac36c80e09e5e5ef3",
        limit=100
    )
    for e in events.get("events", []):
        try:
            parsed = json.loads(e["message"])
            print(json.dumps(parsed, indent=2))
        except:
            print(e["message"])
except Exception as ex:
    print("Lex error:", ex)
