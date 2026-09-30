import json
import os
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cara_health_bot.config import load_config

def main():
    profile = os.environ.get("AWS_PROFILE", "careharmony-main")
    cfg = load_config()
    session = boto3.Session(profile_name=profile, region_name=cfg.region)
    connect = session.client("connect")

    state_file = ROOT / "deployment-state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    res = state.get("resources") or state

    instance_id = res["connectInstanceId"]
    flow_id = res["contactFlowId"]

    flow_desc = connect.describe_contact_flow(
        InstanceId=instance_id,
        ContactFlowId=flow_id,
    )["ContactFlow"]

    print(f"Flow Name: {flow_desc['Name']}")
    print(f"Flow ID: {flow_desc['Id']}")
    print(f"Flow Status: {flow_desc['Status']}")
    print(f"Flow State: {flow_desc['State']}")
    assert flow_desc["Status"] == "PUBLISHED"
    assert flow_desc["State"] == "ACTIVE"

    flow_doc = json.loads(flow_desc["Content"])
    actions = {a["Identifier"]: a for a in flow_doc.get("Actions", [])}

    # Verify action e0000000-0000-4000-8000-000000000008
    assert "e0000000-0000-4000-8000-000000000008" in actions, "Action 0008 missing!"
    act_0008 = actions["e0000000-0000-4000-8000-000000000008"]
    assert act_0008["Type"] == "MessageParticipant"
    assert act_0008["Parameters"]["Text"] == "$.Attributes.representativeTransferMessage"
    assert act_0008["Transitions"]["NextAction"] == "b0000000-0000-4000-8000-000000000010"
    print("\n[VERIFIED] Action e0000000-0000-4000-8000-000000000008 is LIVE:")
    print(json.dumps(act_0008, indent=2))

    # Verify action e0000000-0000-4000-8000-000000000005
    act_0005 = actions["e0000000-0000-4000-8000-000000000005"]
    assert act_0005["Type"] == "UpdateContactAttributes"
    assert act_0005["Parameters"]["Attributes"]["recipientType"] == "REPRESENTATIVE"
    assert act_0005["Parameters"]["Attributes"]["coachingGreeting"] == "$.Attributes.representativeCoachingGreeting"
    assert act_0005["Transitions"]["NextAction"] == "33333333-3333-4333-8333-333333333333"
    print("\n[VERIFIED] Action e0000000-0000-4000-8000-000000000005 routed to coaching flow 33333333-3333-4333-8333-333333333333:")
    print(json.dumps(act_0005, indent=2))

    # Verify Identity Bot branches
    identity_1 = actions["10000000-0000-4000-8000-000000000001"]
    first_routes = {
        c["Condition"]["Operands"][0]: c["NextAction"]
        for c in identity_1.get("Transitions", {}).get("Conditions", [])
    }
    assert first_routes["RepresentativeDetected"] == "e0000000-0000-4000-8000-000000000005"
    assert first_routes["ThirdPartyDetected"] == "e0000000-0000-4000-8000-000000000003"
    print("\n[VERIFIED] Identity Bot 1 routes:")
    print(f"  RepresentativeDetected -> {first_routes['RepresentativeDetected']}")
    print(f"  ThirdPartyDetected -> {first_routes['ThirdPartyDetected']}")

    # Verify Availability Bot branches
    avail_1 = actions["a0000000-0000-4000-8000-000000000001"]
    avail_routes = {
        c["Condition"]["Operands"][0]: c["NextAction"]
        for c in avail_1.get("Transitions", {}).get("Conditions", [])
    }
    assert avail_routes["RepresentativeWillingToProceed"] == "e0000000-0000-4000-8000-000000000005"
    print(f"\n[VERIFIED] Availability Bot RepresentativeWillingToProceed -> {avail_routes['RepresentativeWillingToProceed']}")

    # Verify target node 010
    act_0010 = actions["b0000000-0000-4000-8000-000000000010"]
    assert act_0010["Type"] == "InvokeLambdaFunction"
    assert act_0010["Parameters"]["LambdaInvocationAttributes"]["operation"] == "checkAgentAvailability"
    print(f"\n[VERIFIED] Target node b0000000-0000-4000-8000-000000000010 is checkAgentAvailability:")
    print(json.dumps(act_0010, indent=2))

    print("\nALL FLOW VERIFICATIONS PASSED IN AWS CONNECT PRODUCTION!")

if __name__ == "__main__":
    main()
