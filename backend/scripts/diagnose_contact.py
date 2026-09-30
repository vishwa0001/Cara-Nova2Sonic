"""Diagnostic utility to inspect Amazon Connect Contact ID and CloudWatch logs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError, ProfileNotFound

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("diagnose_contact")

AWS_PROFILE_DEFAULT = "careharmony"
AWS_REGION_DEFAULT = "us-east-1"
DEFAULT_INSTANCE_ID = "32e49641-5486-4e08-b943-bcffa62bb77c"
DEFAULT_CONTACT_ID = "2f3c021d-a624-4267-b635-43e743585d13"


def get_aws_session(profile: Optional[str] = None, region: Optional[str] = None) -> boto3.Session:
    prof = profile or os.getenv("AWS_PROFILE", AWS_PROFILE_DEFAULT)
    reg = region or os.getenv("AWS_REGION", AWS_REGION_DEFAULT)
    try:
        session = boto3.Session(profile_name=prof, region_name=reg)
        session.get_credentials()
        return session
    except (ProfileNotFound, Exception) as e:
        logger.debug(f"AWS Profile '{prof}' fallback: {e}")
        return boto3.Session(region_name=reg)


def diagnose_contact(
    instance_id: str = DEFAULT_INSTANCE_ID,
    contact_id: str = DEFAULT_CONTACT_ID,
    profile: Optional[str] = None,
    region: Optional[str] = None,
):
    session = get_aws_session(profile=profile, region=region)
    connect = session.client("connect")
    logs = session.client("logs")
    ecs = session.client("ecs")

    print("\n" + "=" * 80)
    print(f" DIAGNOSING CONTACT ID: {contact_id}")
    print(f" CONNECT INSTANCE ID : {instance_id}")
    print("=" * 80)

    # 1. Contact Details
    print("\n--- [1] Amazon Connect Contact Record Details ---")
    try:
        contact_resp = connect.describe_contact(
            InstanceId=instance_id,
            ContactId=contact_id,
        )
        contact = contact_resp.get("Contact", {})
        print(f"InitiationTimestamp         : {contact.get('InitiationTimestamp')}")
        print(f"ConnectedToSystemTimestamp  : {contact.get('ConnectedToSystemTimestamp')}")
        print(f"ConnectedToEndpointTimestamp: {contact.get('ConnectedToEndpointTimestamp')}")
        print(f"DisconnectTimestamp         : {contact.get('DisconnectTimestamp')}")
        print(f"DisconnectReason            : {contact.get('DisconnectReason')}")
        print(f"InitiationMethod            : {contact.get('InitiationMethod')}")
        print(f"Channel                     : {contact.get('Channel')}")
        print(f"QueueInfo                   : {json.dumps(contact.get('QueueInfo', {}))}")
        print(f"AgentInfo                   : {json.dumps(contact.get('AgentInfo', {}))}")
        print(f"Full Contact Attributes:\n{json.dumps(contact, indent=2, default=str)}")
    except Exception as e:
        print(f"Error describing contact: {e}")

    # 2. Instance Storage Configs (Media Streams)
    print("\n--- [2] Amazon Connect Instance Storage Configurations (MEDIA_STREAMS) ---")
    try:
        storage_resp = connect.list_instance_storage_configs(
            InstanceId=instance_id,
            ResourceType="MEDIA_STREAMS",
        )
        storage_configs = storage_resp.get("StorageConfigs", [])
        print(f"Found {len(storage_configs)} storage configs for MEDIA_STREAMS:")
        for sc in storage_configs:
            print(json.dumps(sc, indent=2))
        if not storage_configs:
            print("WARNING: NO MEDIA_STREAMS storage configuration found on this Amazon Connect instance!")
            print("         Without this, StartMediaStreaming blocks in Contact Flows will fail immediately.")
    except Exception as e:
        print(f"Error listing storage configs: {e}")

    # 3. CloudWatch Logs for Amazon Connect Flow
    print("\n--- [3] CloudWatch Logs for Contact Flow ---")
    try:
        # Discover log groups
        log_groups_resp = logs.describe_log_groups(logGroupNamePrefix="/aws/connect")
        log_groups = log_groups_resp.get("logGroups", [])
        print(f"Found {len(log_groups)} Connect log groups:")
        for lg in log_groups:
            lg_name = lg.get("logGroupName", "")
            print(f"Checking log group: {lg_name}")
            try:
                filter_resp = logs.filter_log_events(
                    logGroupName=lg_name,
                    filterPattern=f'"{contact_id}"',
                    limit=50,
                )
                events = filter_resp.get("events", [])
                print(f"  Found {len(events)} events for contact {contact_id}:")
                for ev in events:
                    print(f"    [{ev.get('timestamp')}] {ev.get('message')}")
            except Exception as ev_err:
                print(f"  Error filtering logs in {lg_name}: {ev_err}")
    except Exception as e:
        print(f"Error checking Connect CloudWatch logs: {e}")

    # 4. ECS Task & Service Status
    print("\n--- [4] ECS Fargate Pipecat Gateway Task Status & Logs ---")
    cluster_name = "careharmony-voice-cluster"
    service_name = "careharmony-voice-service"
    try:
        srv_resp = ecs.describe_services(cluster=cluster_name, services=[service_name])
        services = srv_resp.get("services", [])
        if services:
            srv = services[0]
            print(f"Service '{service_name}' Status: {srv.get('status')}")
            print(f"Running Count: {srv.get('runningCount')}, Desired Count: {srv.get('desiredCount')}")
            print(f"Pending Count: {srv.get('pendingCount')}")
            print(f"Deployments: {json.dumps(srv.get('deployments', []), indent=2, default=str)}")
            print(f"Events (Last 5):")
            for ev in srv.get("events", [])[:5]:
                print(f"  [{ev.get('createdAt')}] {ev.get('message')}")
        
        # List tasks
        tasks_resp = ecs.list_tasks(cluster=cluster_name, serviceName=service_name)
        task_arns = tasks_resp.get("taskArns", [])
        print(f"\nActive/Running Task ARNs ({len(task_arns)}): {task_arns}")
        if task_arns:
            desc_tasks = ecs.describe_tasks(cluster=cluster_name, tasks=task_arns)
            for t in desc_tasks.get("tasks", []):
                print(f"Task {t.get('taskArn')}: LastStatus={t.get('lastStatus')}, DesiredStatus={t.get('desiredStatus')}")
                print(f"  Containers: {[{c.get('name'): c.get('lastStatus')} for c in t.get('containers', [])]}")

        # List stopped tasks to see if any failed
        stopped_resp = ecs.list_tasks(cluster=cluster_name, desiredStatus="STOPPED")
        stopped_arns = stopped_resp.get("taskArns", [])
        print(f"Stopped Task ARNs ({len(stopped_arns)}): {stopped_arns[:5]}")
        if stopped_arns:
            desc_stopped = ecs.describe_tasks(cluster=cluster_name, tasks=stopped_arns[:5])
            for t in desc_stopped.get("tasks", []):
                print(f"Stopped Task {t.get('taskArn')}:")
                print(f"  StoppedReason: {t.get('stoppedReason')}")
                print(f"  StopCode: {t.get('stopCode')}")
                for c in t.get("containers", []):
                    print(f"    Container '{c.get('name')}': exitCode={c.get('exitCode')}, reason={c.get('reason')}")
    except Exception as e:
        print(f"Error querying ECS: {e}")

    # 5. ECS Log Group
    print("\n--- [5] ECS Gateway CloudWatch Logs (/ecs/careharmony-voice-gateway) ---")
    try:
        streams_resp = logs.describe_log_streams(
            logGroupName="/ecs/careharmony-voice-gateway",
            orderBy="LastEventTime",
            descending=True,
            limit=3,
        )
        streams = streams_resp.get("logStreams", [])
        print(f"Found {len(streams)} log streams:")
        for st in streams:
            st_name = st.get("logStreamName", "")
            print(f"\n--- Log Stream: {st_name} ---")
            log_events = logs.get_log_events(
                logGroupName="/ecs/careharmony-voice-gateway",
                logStreamName=st_name,
                limit=30,
            )
            for ev in log_events.get("events", []):
                print(f"  [{ev.get('timestamp')}] {ev.get('message')}")
    except Exception as e:
        print(f"Error querying ECS logs: {e}")

    print("\n" + "=" * 80)
    print(" DIAGNOSTIC COMPLETE")
    print("=" * 80 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Diagnose Amazon Connect Contact and Logs.")
    parser.add_argument("--instance-id", default=DEFAULT_INSTANCE_ID, help="Amazon Connect instance ID")
    parser.add_argument("--contact-id", default=DEFAULT_CONTACT_ID, help="Contact ID to investigate")
    parser.add_argument("--profile", default=AWS_PROFILE_DEFAULT, help="AWS CLI profile")
    parser.add_argument("--region", default=AWS_REGION_DEFAULT, help="AWS region")
    args = parser.parse_args()

    diagnose_contact(
        instance_id=args.instance_id,
        contact_id=args.contact_id,
        profile=args.profile,
        region=args.region,
    )


if __name__ == "__main__":
    main()
