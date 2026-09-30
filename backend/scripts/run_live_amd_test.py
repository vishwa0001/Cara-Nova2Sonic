import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lambda"))

import campaign_dialer as dialer


def parse_args():
    parser = argparse.ArgumentParser(description="Live test runner for AMD scenarios")
    parser.add_argument("--phone", default="+18148316822", help="Phone number to call (default: +18148316822)")
    parser.add_argument("--scenario", choices=["unanswered", "voicemail", "error"], default="unanswered",
                        help="Scenario to run: unanswered (ring-timeout), voicemail, or error")
    parser.add_argument("--customer-name", default="Hitesh Lalwani", help="Customer name for test")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "careharmony-main"), help="AWS profile")
    return parser.parse_args()


def main():
    args = parse_args()
    print(f"=== Starting Live AMD Test: Scenario '{args.scenario}' ===")
    print(f"Using AWS Profile: {args.profile}")
    print(f"Destination Phone: {args.phone}")

    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    sts = session.client("sts")
    try:
        ident = sts.get_caller_identity()
        account_id = ident["Account"]
        arn = ident["Arn"]
        print(f"Caller Identity: Account={account_id}, Arn={arn}")
    except Exception as e:
        print(f"\n[ERROR] Failed to authenticate with AWS: {e}")
        print("\nACTION REQUIRED: Your AWS SSO / session token in profile 'careharmony-main' has expired.")
        print("Please refresh ~/.aws/credentials with new temporary credentials from your AWS SSO portal and re-run.\n")
        sys.exit(1)

    dynamo = session.resource("dynamodb")
    batches_table = dynamo.Table("TalkingBotCallBatches-dev")
    patients_table = dynamo.Table("TalkingBotPatientRecords-dev")
    connect = session.client("connect")
    scheduler = session.client("scheduler")

    state_path = ROOT / "deployment-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    res = state.get("resources") or state.get("outputs") or {}
    instance_id = res.get("connectInstanceId") or res.get("InstanceId")
    flow_id = res.get("contactFlowId") or res.get("ContactFlowId")
    source_phone = res.get("sourcePhoneNumber") or res.get("SourcePhoneNumber")

    # Set env vars for dialer module
    os.environ["BATCHES_TABLE_NAME"] = "TalkingBotCallBatches-dev"
    os.environ["PATIENTS_TABLE_NAME"] = "TalkingBotPatientRecords-dev"
    os.environ["CONNECT_INSTANCE_ID"] = instance_id
    os.environ["CONNECT_CONTACT_FLOW_ID"] = flow_id
    os.environ["CONNECT_SOURCE_PHONE_NUMBER"] = source_phone
    os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "cara-health-bot-campaign-dialer"
    os.environ["DIALER_LAMBDA_ARN"] = f"arn:aws:lambda:us-east-1:{account_id}:function:cara-health-bot-campaign-dialer"
    os.environ["SCHEDULER_ROLE_ARN"] = f"arn:aws:iam::{account_id}:role/cara-health-bot-campaign-scheduler-role"
    os.environ["MAX_RESCHEDULE_ATTEMPTS"] = "10"

    now_ts = int(time.time())
    campaign_id = f"test-amd-{args.scenario}-{now_ts}"
    patient_id = f"pt-{args.scenario}-{now_ts}"

    now_dt = dt.datetime.now(dt.timezone.utc)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"\n1. Creating test batch '{campaign_id}' and patient record '{patient_id}'...")
    batches_table.put_item(
        Item={
            "campaignId": campaign_id,
            "batchId": campaign_id,
            "status": "RUNNING",
            "timezone": "America/New_York",
            "fileName": f"test_{args.scenario}.csv",
            "customerCount": 1,
            "patientCount": 1,
            "createdAt": now_iso,
            "updatedAt": now_iso,
        }
    )

    patient_record = {
        "batchId": campaign_id,
        "patientId": patient_id,
        "empi": patient_id,
        "status": "PENDING",
        "customerName": args.customer_name,
        "firstName": args.customer_name.split()[0],
        "lastName": args.customer_name.split()[-1] if len(args.customer_name.split()) > 1 else "",
        "phoneNumber": args.phone,
        "practiceName": "North Clinic",
        "providerName": "Dr. Gregory House",
        "practiceCallbackNumber": "+18145550100",
        "callbackCount": 0,
        "createdAt": now_iso,
        "updatedAt": now_iso,
    }
    patients_table.put_item(Item=patient_record)
    print("   DynamoDB records initialized.")

    print("\n2. Placing outbound voice contact via dialer._place_call...")
    try:
        contact_id = dialer._place_call(
            patients_table=patients_table,
            connect=connect,
            patient=patient_record,
            campaign_id=campaign_id,
            campaign={"batchId": campaign_id, "timezone": "America/New_York"},
        )
        print(f"   Contact placed successfully! ContactId: {contact_id}")
    except Exception as e:
        print(f"   [ERROR] Failed to place call: {e}")
        sys.exit(1)

    print("\n3. Instructions for Scenario:")
    if args.scenario == "unanswered":
        print(f"   --> CALL PLACED TO {args.phone}. DO NOT ANSWER. Let it ring until it times out completely (~30-45s).")
    elif args.scenario == "voicemail":
        print(f"   --> CALL PLACED TO {args.phone}. Decline the call or let it roll to voicemail so the machine answers and beeps.")
    else:
        print(f"   --> Testing error scenario.")

    print("\n4. Monitoring DynamoDB patient record for outcome & auto-reschedule...")
    start_wait = time.time()
    last_status = "IN_PROGRESS"
    final_item = None

    while time.time() - start_wait < 180:
        time.sleep(4)
        item = patients_table.get_item(Key={"patientId": patient_id, "batchId": campaign_id}).get("Item")
        if not item:
            continue
        curr_status = item.get("status")
        if curr_status != last_status:
            print(f"   [{int(time.time() - start_wait)}s] Status changed to: {curr_status} | Disposition: {item.get('disposition')}")
            last_status = curr_status
        if curr_status in {"CALLBACK_SCHEDULED", "COMPLETED", "MAX_RETRIES_EXHAUSTED", "FAILED", "NOT_INTERESTED"}:
            final_item = item
            break

    if not final_item:
        print("\n[TIMEOUT] Waiting for final record timed out after 180s.")
        sys.exit(1)

    print("\n================ FINAL DYNAMODB RECORD SNAPSHOT ================")
    print(json.dumps(final_item, indent=2, default=str))

    print("\n================ EVENTBRIDGE SCHEDULE CONFIRMATION ================")
    schedule_name = final_item.get("callbackScheduleName")
    if schedule_name:
        try:
            sched = scheduler.get_schedule(Name=schedule_name, GroupName="default")
            print(f"Schedule Name: {sched.get('Name')}")
            print(f"State:         {sched.get('State')}")
            print(f"Expression:    {sched.get('ScheduleExpression')} ({sched.get('ScheduleExpressionTimezone')})")
            print(f"Target:        {sched.get('Target', {}).get('Arn')}")
            print(f"Input:         {sched.get('Target', {}).get('Input')}")
        except ClientError as e:
            print(f"Failed to fetch schedule {schedule_name}: {e}")
    else:
        print("No callbackScheduleName found on record.")

    print("\n================ CONNECT CONTACT DETAILS ================")
    try:
        c_detail = connect.describe_contact(InstanceId=instance_id, ContactId=contact_id).get("Contact", {})
        print(f"Contact ID:                        {contact_id}")
        print(f"Initiation Method:                 {c_detail.get('InitiationMethod')}")
        print(f"Answering Machine Detection:       {c_detail.get('AnsweringMachineDetectionStatus')}")
        print(f"Disconnect Reason:                 {c_detail.get('DisconnectReason')}")
        print(f"Initiation Timestamp:              {c_detail.get('InitiationTimestamp')}")
        print(f"Disconnect Timestamp:              {c_detail.get('DisconnectTimestamp')}")
    except Exception as e:
        print(f"Could not describe contact {contact_id}: {e}")

    print(f"\n=== Completed Test for Scenario: {args.scenario} ===")


if __name__ == "__main__":
    main()
