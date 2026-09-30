#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cara_health_bot.config import load_config

def main():
    cfg = load_config()
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "careharmony-main"), region_name=cfg.region)
    lambda_client = session.client("lambda")
    connect_client = session.client("connect")

    print("=== Checking Live Provisioned Concurrency & Contact Flow ===")
    
    # 1. Check Lambda Alias
    alias_info = lambda_client.get_alias(
        FunctionName=cfg.session_context_lambda_name,
        Name="Live"
    )
    print(f"1. Lambda Alias 'Live': Version = {alias_info.get('FunctionVersion')}, Description = {alias_info.get('Description')}")

    # 2. Check Provisioned Concurrency
    pcfg = lambda_client.get_provisioned_concurrency_config(
        FunctionName=cfg.session_context_lambda_name,
        Qualifier="Live"
    )
    status = pcfg.get("Status")
    requested = pcfg.get("RequestedProvisionedConcurrentExecutions")
    allocated = pcfg.get("AllocatedProvisionedConcurrentExecutions")
    available = pcfg.get("AvailableProvisionedConcurrentExecutions")
    print(f"2. Provisioned Concurrency: Status = {status}, Requested = {requested}, Allocated = {allocated}, Available = {available}")
    assert status in ["READY", "IN_PROGRESS"], f"Status is {status}"
    assert (allocated or 0) >= 1, f"Allocated is {allocated}"

    # 3. Check Connect Association
    state_file = ROOT / "deployment-state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    res = state.get("resources") or state
    instance_id = res["connectInstanceId"]
    flow_id = res["contactFlowId"]
    
    associated = connect_client.list_lambda_functions(InstanceId=instance_id).get("LambdaFunctions", [])
    live_arn_match = any(":Live" in arn and cfg.session_context_lambda_name in arn for arn in associated)
    print(f"3. Connect Associated Lambda Functions ({len(associated)} total): Live alias associated = {live_arn_match}")
    assert live_arn_match, "Live alias not found in Connect associated lambda functions"

    # 4. Check Contact Flow Content
    flow = connect_client.describe_contact_flow(
        InstanceId=instance_id,
        ContactFlowId=flow_id
    )["ContactFlow"]
    content = flow.get("Content", "")
    live_arn_in_flow = f"{cfg.session_context_lambda_name}:Live" in content
    print(f"4. Contact Flow {flow_id}: Status = {flow.get('Status')}, Contains ':Live' Lambda ARN = {live_arn_in_flow}")
    assert live_arn_in_flow, "Published Contact Flow does NOT reference :Live alias ARN!"

    print("\nALL LIVE PROVISIONED CONCURRENCY & FLOW CHECKS PASSED!")

if __name__ == "__main__":
    main()
