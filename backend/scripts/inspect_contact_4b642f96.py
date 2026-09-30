import boto3, json

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
logs = session.client("logs")

contact_id = "4b642f96-60a1-430f-84bc-aac8eabd5a6d"

print("==================== 1. CONNECT LOGS ====================")
try:
    events = logs.filter_log_events(
        logGroupName="/aws/connect/cara-health-bot-258673",
        filterPattern=contact_id,
        limit=100
    )
    for e in events.get("events", []):
        try:
            parsed = json.loads(e["message"])
            mod = parsed.get("ContactFlowModuleType")
            ident = parsed.get("Identifier")
            res = parsed.get("Results")
            err = parsed.get("ErrorDetails")
            params = parsed.get("Parameters")
            print(f"[{e['timestamp']}] Module: {mod} | ID: {ident} | Results: {res}")
            if err:
                print(f"  ERROR: {err}")
            if params:
                print(f"  Params: {params}")
        except:
            print(e["message"])
except Exception as ex:
    print("Connect error:", ex)

print("\n==================== 2. SESSION CONTEXT LAMBDA LOGS ====================")
try:
    events = logs.filter_log_events(
        logGroupName="/aws/lambda/cara-health-bot-session-context",
        filterPattern=contact_id,
        limit=100
    )
    for e in events.get("events", []):
        print(f"[{e['timestamp']}] {e['message'].strip()}")
except Exception as ex:
    print("Lambda error:", ex)

print("\n==================== 3. LEX CONVERSATIONS LOGS ====================")
try:
    events = logs.filter_log_events(
        logGroupName="/aws/lex/cara-health-bot-conversations-258673",
        filterPattern=contact_id,
        limit=100
    )
    for e in events.get("events", []):
        try:
            parsed = json.loads(e["message"])
            transcript = parsed.get("inputTranscript")
            interps = parsed.get("interpretations", [{}])
            intent = interps[0].get("intent", {}).get("name") if interps else None
            tool = parsed.get("sessionState", {}).get("sessionAttributes", {}).get("Tool")
            resp = parsed.get("sessionState", {}).get("sessionAttributes", {}).get("x-amz-lex:q-in-connect-response")
            print(f"Lex turn: transcript={transcript!r} intent={intent} tool={tool} resp={resp!r}")
            print("  full sessionAttributes:", parsed.get("sessionState", {}).get("sessionAttributes"))
        except:
            print(e["message"])
except Exception as ex:
    print("Lex error:", ex)
