import boto3
import json
import time

session = boto3.Session(profile_name="careharmony-main", region_name="us-east-1")
logs = session.client("logs")
qconnect = session.client("qconnect")
lex = session.client("lexv2-models")
connect = session.client("connect")

contact_id = "2888e1a7-c6e4-4e2d-a690-ec84ec819662"

print("==================== 1. CLOUDWATCH LOGS FOR CONTACT ====================")
log_groups = [
    "/aws/connect/cara-health-bot-258673",
    "/aws/lex/cara-health-bot-conversations-258673",
]

for lg in log_groups:
    print(f"\n--- Log Group: {lg} ---")
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

print("\n==================== 2. LIVE Q CONNECT AI PROMPT & AGENT ====================")
try:
    with open("backend/deployment-state.json", "r") as f:
        state = json.load(f)["resources"]
    assistant_id = state["assistantId"]
    ai_prompt_id = state["aiPromptId"]
    ai_agent_id = state["aiAgentId"]
    print(f"Assistant ID: {assistant_id}")
    print(f"AI Prompt ID in deployment-state: {ai_prompt_id}, Version: {state.get('aiPromptVersion')}")
    print(f"AI Agent ID in deployment-state: {ai_agent_id}, Version: {state.get('aiAgentVersion')}")

    # List prompt versions
    p_versions = qconnect.list_ai_prompt_versions(assistantId=assistant_id, aiPromptId=ai_prompt_id)
    print("\nPrompt Versions Summary:")
    for pv in p_versions.get("aiPromptVersionSummaries", []):
        print(f"  Version: {pv.get('versionNumber')}")

    # Describe latest prompt version or DRAFT
    prompt_desc = qconnect.get_ai_prompt(assistantId=assistant_id, aiPromptId=ai_prompt_id)
    print(f"\nPrompt (latest/DRAFT): modelId={prompt_desc.get('aiPrompt', {}).get('modelId')}")
    template_text = prompt_desc.get('aiPrompt', {}).get('templateConfiguration', {}).get('textFullAIPromptEditTemplateConfiguration', {}).get('text', '')
    print(f"Prompt text length: {len(template_text)}")
    print("Snippet of tool EscalateToHuman in Prompt:")
    for line in template_text.splitlines():
        if "EscalateToHuman" in line or "I'll connect you" in line or "OUTPUT FORMAT" in line or "message" in line:
            print(f"  {line}")

    # List AI Agent versions
    a_versions = qconnect.list_ai_agent_versions(assistantId=assistant_id, aiAgentId=ai_agent_id)
    print("\nAgent Versions Summary:")
    for av in a_versions.get("aiAgentVersionSummaries", []):
        print(f"  Version: {av.get('versionNumber')}")

    # Get latest agent version details
    agent_desc = qconnect.get_ai_agent(assistantId=assistant_id, aiAgentId=ai_agent_id)
    print("\nAgent (latest):")
    print(json.dumps(agent_desc.get("aiAgent", {}).get("configuration", {}), indent=2))

    # Check Assistant associations
    assocs = qconnect.list_assistant_associations(assistantId=assistant_id)
    print("\nAssistant Associations:")
    for a in assocs.get("assistantAssociationSummaries", []):
        print(json.dumps(a, indent=2))

except Exception as e:
    print(f"Error querying Q Connect: {e}")

print("\n==================== 3. LIVE LEX BOT & ALIAS ====================")
try:
    bot_id = state.get("botId", "4S3WG7D9ZQ")
    alias_id = state.get("botAliasId", "TZSUXEY14O")
    alias_desc = lex.describe_bot_alias(botId=bot_id, botAliasId=alias_id)
    print(f"Lex Bot {bot_id} Alias {alias_id}:")
    print(f"  Bot Version linked to Live alias: {alias_desc.get('botVersion')}")
    print(f"  Status: {alias_desc.get('botAliasStatus')}")

    # Describe the bot version
    version_id = alias_desc.get('botVersion', 'DRAFT')
    intents = lex.list_intents(botId=bot_id, botVersion=version_id, localeId="en_US")
    print(f"\nIntents on version {version_id}:")
    for it in intents.get("intentSummaries", []):
        print(f"  {it.get('intentName')} ({it.get('intentId')})")
        if it.get('intentName') == "AmazonQinConnect":
            intent_desc = lex.describe_intent(botId=bot_id, botVersion=version_id, localeId="en_US", intentId=it.get('intentId'))
            print("  Intent Fulfillment Spec:")
            print(json.dumps(intent_desc.get("fulfillmentCodeHook", {}), indent=2))
            print("  Intent Q in Connect Config:")
            print(json.dumps(intent_desc.get("qInConnectIntentConfiguration", {}), indent=2))

except Exception as e:
    print(f"Error querying Lex: {e}")
