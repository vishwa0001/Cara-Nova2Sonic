#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cara_health_bot.config import load_config
from cara_health_bot.deployer import CaraHealthBotDeployer
import scripts.sync_all_deployed_components as syncer

def main():
    cfg = load_config()
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "careharmony-main"), region_name=cfg.region)
    sts = session.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    print(f"Deploying to Account: {account_id}, Region: {cfg.region}")

    # Load resources
    state_file = ROOT / "deployment-state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    res = state.get("resources") or state
    bot_id = res["botId"]
    assistant_arn = res["assistantArn"]

    print(f"\n1. Ensuring Lex Coaching Bot {bot_id} has CallRefusal intent & building new version...")
    deployer = CaraHealthBotDeployer(cfg)
    deployer.account_id = account_id
    role_name = res["lexRuntimeRoleName"]
    role_arn = res["lexRuntimeRoleArn"]
    connect_instance_arn = res["connectInstanceArn"]
    
    bot_id, version, bot_alias_id, bot_alias_arn = deployer.ensure_lex(
        role_name, role_arn, assistant_arn, connect_instance_arn
    )
    print(f"   -> Lex coaching bot updated: version={version}, aliasId={bot_alias_id}, aliasArn={bot_alias_arn}")

    print("\n2. Running full sync of Contact Flow, AI Prompt, AI Agent, Lambda...")
    syncer.main()

    print("\n3. Testing Lex NLU recognition on the newly deployed coaching bot via Lambda...")
    lambda_client = session.client("lambda")
    test_phrases = [
        "no I am not interested",
        "no i'm not interested",
        "not interested",
        "no thanks",
        "stop calling me",
        "I want to kill myself",  # Safety check: must stay SafetyBehavioral
        "chest pain",            # Safety check: must stay SafetyMedical
    ]

    for phrase in test_phrases:
        payload = {
            "Details": {
                "Parameters": {
                    "operation": "recognize_text",
                    "botId": bot_id,
                    "botAliasId": bot_alias_id,
                    "text": phrase,
                    "sessionId": f"test-nlu-{int(time.time())}"
                }
            }
        }
        res_raw = lambda_client.invoke(FunctionName="cara-health-bot-session-context", Payload=json.dumps(payload))
        res_data = json.loads(res_raw["Payload"].read().decode("utf-8"))
        top_intent = res_data.get("matchedIntent")
        print(f"   Utterance: {phrase!r:<30} -> Top Intent: {top_intent}")

    print("\nDeployment and NLU verification complete!")

if __name__ == "__main__":
    main()
