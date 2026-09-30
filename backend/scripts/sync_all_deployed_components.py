#!/usr/bin/env python3
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cara_health_bot.config import load_config
from cara_health_bot.builders import (
    render_contact_flow,
    q_prompt_update_request,
    q_agent_update_request,
    q_agent_configuration,
    session_context_lambda_permissions,
)

def main():
    cfg = load_config()
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "careharmony-main"), region_name=cfg.region)
    lambda_client = session.client("lambda")
    connect_client = session.client("connect")
    qconnect_client = session.client("qconnect")
    iam_client = session.client("iam")
    sts_client = session.client("sts")

    account_id = sts_client.get_caller_identity()["Account"]
    print(f"Deploying to account {account_id}, region {cfg.region}...")

    # 1. Update session-context Lambda Code & Memory Configuration (512 MB)
    print("\n1. Updating cara-health-bot-session-context Lambda (code & 512MB memory)...")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        code_path = ROOT / "lambda" / "session_context.py"
        zf.write(code_path, arcname="session_context.py")
    buf.seek(0)
    lambda_client.update_function_code(
        FunctionName="cara-health-bot-session-context",
        ZipFile=buf.read(),
        Publish=True
    )
    import time
    for _ in range(30):
        fc = lambda_client.get_function_configuration(FunctionName="cara-health-bot-session-context")
        if fc.get("LastUpdateStatus") != "InProgress":
            break
        time.sleep(1)

    lambda_client.update_function_configuration(
        FunctionName="cara-health-bot-session-context",
        MemorySize=512,
        Timeout=8,
    )
    for _ in range(30):
        fc = lambda_client.get_function_configuration(FunctionName="cara-health-bot-session-context")
        if fc.get("LastUpdateStatus") != "InProgress":
            break
        time.sleep(1)
    print("   -> cara-health-bot-session-context code and 512MB memory updated successfully.")

    # 1b. Publish version, update Live alias, and configure Provisioned Concurrency (1 unit)
    print("\n1b. Publishing version and configuring Provisioned Concurrency (1 unit) on Live alias...")
    pub_res = lambda_client.publish_version(
        FunctionName="cara-health-bot-session-context",
        Description="Live version with Provisioned Concurrency"
    )
    pub_ver = pub_res["Version"]
    print(f"   -> Published Lambda version {pub_ver}")

    alias_name = "Live"
    try:
        lambda_client.get_alias(FunctionName="cara-health-bot-session-context", Name=alias_name)
        try:
            lambda_client.delete_provisioned_concurrency_config(
                FunctionName="cara-health-bot-session-context",
                Qualifier=alias_name,
            )
            print(f"   -> Deleted old provisioned concurrency on {alias_name} before version shift")
            del_start = time.time()
            while time.time() - del_start < 60:
                try:
                    lambda_client.get_provisioned_concurrency_config(
                        FunctionName="cara-health-bot-session-context",
                        Qualifier=alias_name,
                    )
                    time.sleep(2)
                except Exception as e:
                    if "NotFound" in type(e).__name__ or "NotFound" in str(e):
                        break
        except Exception:
            pass
        lambda_client.update_alias(
            FunctionName="cara-health-bot-session-context",
            Name=alias_name,
            FunctionVersion=pub_ver,
            Description="Live alias for Cara Health Bot session context Lambda with provisioned concurrency",
            RoutingConfig={"AdditionalVersionWeights": {}},
        )
        print(f"   -> Updated alias {alias_name} -> version {pub_ver}")
    except lambda_client.exceptions.ResourceNotFoundException:
        lambda_client.create_alias(
            FunctionName="cara-health-bot-session-context",
            Name=alias_name,
            FunctionVersion=pub_ver,
            Description="Live alias for Cara Health Bot session context Lambda with provisioned concurrency",
        )
        print(f"   -> Created alias {alias_name} -> version {pub_ver}")

    try:
        lambda_client.put_provisioned_concurrency_config(
            FunctionName="cara-health-bot-session-context",
            Qualifier=alias_name,
            ProvisionedConcurrentExecutions=1,
        )
        print(f"   -> Put provisioned concurrency (1 unit) on {alias_name}")
    except Exception as err:
        print(f"   -> [WARN] put_provisioned_concurrency_config: {err}")

    print("   -> Waiting for Provisioned Concurrency to reach READY...")
    for _ in range(36):
        try:
            pcfg = lambda_client.get_provisioned_concurrency_config(
                FunctionName="cara-health-bot-session-context",
                Qualifier=alias_name,
            )
            if pcfg.get("Status") == "READY":
                print(f"   -> Provisioned Concurrency is READY (Allocated: {pcfg.get('AllocatedConcurrentExecutions')})")
                break
        except Exception:
            pass
        time.sleep(5)

    live_lambda_arn = f"arn:aws:lambda:{cfg.region}:{account_id}:function:cara-health-bot-session-context:{alias_name}"

    # Ensure permissions on Live alias
    for stmt, qual in [("CaraHealthBotConnectInvoke_Live", "Live"), ("CaraHealthBotLexInvoke_Live", "Live")]:
        try:
            lambda_client.remove_permission(
                FunctionName="cara-health-bot-session-context",
                StatementId=stmt,
                Qualifier=qual,
            )
        except Exception:
            pass
    try:
        state_file = ROOT / "deployment-state.json"
        state_tmp = json.loads(state_file.read_text(encoding="utf-8"))
        conn_arn = (state_tmp.get("resources") or state_tmp).get("connectInstanceArn", "")
        lambda_client.add_permission(
            FunctionName="cara-health-bot-session-context",
            StatementId="CaraHealthBotConnectInvoke_Live",
            Qualifier="Live",
            Action="lambda:InvokeFunction",
            Principal="connect.amazonaws.com",
            SourceAccount=account_id,
            SourceArn=conn_arn,
        )
        lambda_client.add_permission(
            FunctionName="cara-health-bot-session-context",
            StatementId="CaraHealthBotLexInvoke_Live",
            Qualifier="Live",
            Action="lambda:InvokeFunction",
            Principal="lexv2.amazonaws.com",
            SourceAccount=account_id,
        )
    except Exception as err:
        print(f"   -> [WARN] adding permission to Live alias: {err}")

    # 2. Retrieve deployed resource IDs
    state_file = ROOT / "deployment-state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    res = state.get("resources") or state
    instance_id = res["connectInstanceId"]
    assistant_id = res["assistantId"]
    assistant_arn = res["assistantArn"]
    alias_arn = res["botAliasArn"]
    identity_alias_arn = res["identityBotAliasArn"]
    availability_alias_arn = res["availabilityBotAliasArn"]
    session_context_lambda_arn = live_lambda_arn
    human_transfer_queue_arn = res["humanTransferQueueArn"]
    flow_id = res["contactFlowId"]
    connect_instance_arn = res["connectInstanceArn"]

    # Associate Live alias with Connect instance
    associated = connect_client.list_lambda_functions(InstanceId=instance_id).get("LambdaFunctions", [])
    if live_lambda_arn not in associated:
        try:
            import uuid
            connect_client.associate_lambda_function(
                InstanceId=instance_id,
                FunctionArn=live_lambda_arn,
                ClientToken=str(uuid.uuid4()),
            )
            print(f"   -> Associated {live_lambda_arn} with Connect instance")
        except Exception as err:
            print(f"   -> [WARN] associate_lambda_function: {err}")

    # 2b. Update IAM Policy for session-context Lambda Role
    print("\n2. Updating IAM Policy for session-context Lambda role...")
    policy_doc = session_context_lambda_permissions(cfg.region, account_id, assistant_id, "cara-health-bot-session-context")
    iam_client.put_role_policy(
        RoleName="CaraHealthBotSessionContextLambdaRole",
        PolicyName="CaraHealthBotSessionContextLambdaPermissions",
        PolicyDocument=json.dumps(policy_doc),
    )
    print("   -> IAM Role policy CaraHealthBotSessionContextLambdaPermissions updated with lambda:InvokeFunction.")

    # 3. Update Contact Flow Content
    print("\n2. Updating Amazon Connect contact flow...")
    hold_flows = connect_client.list_contact_flows(InstanceId=instance_id, ContactFlowTypes=["CUSTOMER_HOLD"]).get("ContactFlowSummaryList", [])
    queue_flows = connect_client.list_contact_flows(InstanceId=instance_id, ContactFlowTypes=["CUSTOMER_QUEUE"]).get("ContactFlowSummaryList", [])
    hold_flow_arn = hold_flows[0]["Arn"] if hold_flows else None
    queue_flow_arn = queue_flows[0]["Arn"] if queue_flows else None

    rendered_flow = render_contact_flow(
        cfg,
        assistant_id,
        assistant_arn,
        alias_arn,
        identity_alias_arn,
        availability_alias_arn,
        session_context_lambda_arn,
        human_transfer_queue_arn,
        customer_hold_flow_arn=hold_flow_arn,
        customer_queue_flow_arn=queue_flow_arn,
    )
    connect_client.update_contact_flow_content(
        InstanceId=instance_id,
        ContactFlowId=flow_id,
        Content=rendered_flow
    )
    print(f"   -> Contact flow {flow_id} updated and published.")

    # 4. Update Q in Connect Prompt and AI Agent
    print("\n3. Updating Q in Connect prompt & agent...")
    prompt_id = res["aiPromptId"]
    prompt_content = cfg.prompt_path.read_text(encoding="utf-8")
    prompt_req = q_prompt_update_request(cfg, assistant_id, prompt_id, prompt_content)
    update_res = qconnect_client.update_ai_prompt(**prompt_req)
    version_res = qconnect_client.create_ai_prompt_version(
        assistantId=assistant_id,
        aiPromptId=prompt_id
    )
    prompt_version = version_res.get("versionNumber", 1)
    print(f"   -> AI Prompt updated. New version: {prompt_version}")

    agent_id = res["aiAgentId"]
    # Update AI Agent with new prompt version
    agent_req = q_agent_update_request(cfg, assistant_id, connect_instance_arn, agent_id, f"{prompt_id}:{prompt_version}")
    qconnect_client.update_ai_agent(**agent_req)
    agent_ver_res = qconnect_client.create_ai_agent_version(
        assistantId=assistant_id,
        aiAgentId=agent_id
    )
    agent_version = agent_ver_res.get("versionNumber", 1)
    print(f"   -> AI Agent updated with new prompt version. Agent version: {agent_version}")

    # Point assistant's Connect.SelfService orchestrator to the new agent version
    qconnect_client.update_assistant_ai_agent(
        assistantId=assistant_id,
        aiAgentType="ORCHESTRATION",
        orchestratorUseCase="Connect.SelfService",
        configuration={"aiAgentId": f"{agent_id}:{agent_version}"},
    )
    print(f"   -> Assistant Connect.SelfService orchestrator updated to agent version {agent_version}")

    # 5. Persist state
    state["resources"]["aiPromptVersion"] = str(prompt_version)
    state["resources"]["aiAgentVersion"] = str(agent_version)
    state["resources"]["aiAgentVersionArn"] = f"arn:aws:wisdom:{cfg.region}:{account_id}:ai-agent/{assistant_id}/{agent_id}:{agent_version}"
    state["outputs"]["AIAgentVersionArn"] = state["resources"]["aiAgentVersionArn"]
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"   -> deployment-state.json updated with prompt v{prompt_version}, agent v{agent_version}")

    print("\nAll components successfully synced and deployed to AWS!")

if __name__ == "__main__":
    main()
