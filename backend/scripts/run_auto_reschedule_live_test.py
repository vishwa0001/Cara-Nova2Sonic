import csv
import datetime as dt
import json
import os
import sys
import time
import boto3

def run_auto_reschedule_test():
    session = boto3.Session(profile_name="careharmony", region_name="us-east-1")
    s3 = session.client("s3")
    dynamo = session.resource("dynamodb")
    batches_table = dynamo.Table("TalkingBotCallBatches-dev")
    patients_table = dynamo.Table("TalkingBotPatientRecords-dev")

    campaign_id = f"test-auto-resched-{int(time.time())}"
    bucket_name = "cara-health-bot-campaigns-701348334422-us-east-1"
    
    now_dt = dt.datetime.now(dt.timezone.utc)
    scheduled_iso = (now_dt + dt.timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    config_data = {
        "campaignId": campaign_id,
        "batchId": campaign_id,
        "fileName": "patients.csv",
        "customerCount": 1,
        "patientCount": 1,
        "totalRows": 1,
        "scheduledAt": scheduled_iso,
        "timezone": "UTC",
        "callTime": scheduled_iso,
        "uploadedAt": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    phone = "+18148316822"

    csv_content = (
        "empi,first_name,last_name,gender,phone_number,practice_name,practice_callback_number,direct_agent\n"
        f"TEST-AUTORES-01,Kevin,Peterson,Male,{phone},Mercy Health,+18005550100,no\n"
    )

    print(f"Creating auto-reschedule test campaign: {campaign_id}")
    batches_table.put_item(
        Item={
            **config_data,
            "status": "UPLOAD_PENDING",
            "s3Bucket": bucket_name,
            "s3Key": f"campaigns/{campaign_id}/patients.csv",
            "createdAt": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updatedAt": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )

    s3.put_object(
        Bucket=bucket_name,
        Key=f"campaigns/{campaign_id}/config.json",
        Body=json.dumps(config_data).encode("utf-8"),
        ContentType="application/json",
    )

    s3.put_object(
        Bucket=bucket_name,
        Key=f"campaigns/{campaign_id}/patients.csv",
        Body=csv_content.encode("utf-8"),
        ContentType="text/csv",
    )
    print(f"Campaign {campaign_id} uploaded to S3. Dialing initiated to {phone}...")
    print("\nINSTRUCTIONS FOR LIVE TEST:")
    print("1. Phone will ring at +18148316822")
    print("2. Answer: Cara says: 'Hi, may I speak with Kevin Peterson?'")
    print("3. You say: 'Yes, this is Kevin.'")
    print("4. Cara says: '... Is now a good time?'")
    print("5. You say: 'Not right now, call me later.' (Do NOT give a specific time!)")
    print("6. Cara says: 'No problem — we'll try you back soon. Take care, goodbye.' and hangs up.")
    print("\nWaiting for call to connect and disconnect...")

    patient_id = f"{campaign_id}#row-1"
    start_wait = time.time()
    last_status = None
    while time.time() - start_wait < 300:
        time.sleep(3)
        item = patients_table.get_item(Key={"patientId": patient_id, "batchId": campaign_id}).get("Item")
        if item:
            curr_status = item.get("status")
            if curr_status != last_status:
                print(f"[{int(time.time() - start_wait)}s] Patient status changed to: {curr_status} | ContactId: {item.get('contactId')}")
                last_status = curr_status
            if curr_status in {"CALLBACK_SCHEDULED", "CALLBACK_UNSPECIFIED", "COMPLETED", "NOT_INTERESTED", "FAILED"}:
                print("\n================ FINAL DYNAMODB RECORD ================")
                print(json.dumps(item, indent=2, default=str))
                return campaign_id, item

    print("Timed out waiting for final record.")
    return campaign_id, None

if __name__ == "__main__":
    cid, rec = run_auto_reschedule_test()
