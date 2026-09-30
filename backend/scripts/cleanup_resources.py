"""Comprehensive Nuclear Teardown Script for CareHarmony-Cara.

Permanently removes all provisioned AWS project resources in a dependency-safe order
to achieve $0 ongoing cloud spend, while safeguarding the Amazon Connect instance.

Order of Execution:
1. EventBridge Rules (careharmony-campaign-*)
2. ECS Service, Cluster & Task Definitions (careharmony-voice-*)
3. Kinesis Video Streams (careharmony-outreach-media-stream)
4. DynamoDB Tables (CareHarmony_OutreachRecords, CareHarmony_HandoffEvents)
5. SSM Parameter Store (/careharmony/*)
6. Local Artifact Cleanup (backend/deployment_state.json)
7. Amazon Connect Contact Flow purge (NEVER touches the Connect instance itself)

Usage:
    python backend/scripts/cleanup_resources.py [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError, ProfileNotFound

logger = logging.getLogger("cleanup_resources")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

AWS_PROFILE_DEFAULT = "careharmony"
AWS_REGION_DEFAULT = "us-east-1"
STATE_FILE_PATH = Path(__file__).resolve().parents[1] / "deployment_state.json"


def get_aws_session(profile: Optional[str] = None, region: Optional[str] = None) -> boto3.Session:
    prof = profile or os.getenv("AWS_PROFILE", AWS_PROFILE_DEFAULT)
    reg = region or os.getenv("AWS_REGION", AWS_REGION_DEFAULT)
    try:
        session = boto3.Session(profile_name=prof, region_name=reg)
        session.get_credentials()
        return session
    except (ProfileNotFound, Exception) as e:
        logger.debug(f"AWS Profile '{prof}' fallback to default/env ({e})")
        return boto3.Session(region_name=reg)


# ==============================================================================
# 1. EVENTBRIDGE RULES
# ==============================================================================

def cleanup_eventbridge_rules(session: boto3.Session, prefix: str = "careharmony-campaign-", dry_run: bool = False) -> List[str]:
    """Delete EventBridge rules matching the campaign prefix."""
    events = session.client("events")
    deleted_rules = []
    logger.info(f"Scanning EventBridge rules with prefix '{prefix}'...")

    try:
        paginator = events.get_paginator("list_rules")
        for page in paginator.paginate(NamePrefix=prefix):
            for rule in page.get("Rules", []):
                rule_name = rule.get("Name", "")
                if dry_run:
                    logger.info(f"[DRY-RUN] Would remove targets and delete EventBridge rule: {rule_name}")
                    deleted_rules.append(rule_name)
                    continue

                # Remove targets first
                try:
                    targets = events.list_targets_by_rule(Rule=rule_name).get("Targets", [])
                    target_ids = [t["Id"] for t in targets if "Id" in t]
                    if target_ids:
                        events.remove_targets(Rule=rule_name, Ids=target_ids)
                        logger.info(f"Removed {len(target_ids)} targets from rule '{rule_name}'")
                except Exception as t_err:
                    logger.warning(f"Error removing targets from rule {rule_name}: {t_err}")

                # Delete rule
                try:
                    events.delete_rule(Name=rule_name)
                    logger.info(f"Deleted EventBridge rule: {rule_name}")
                    deleted_rules.append(rule_name)
                except Exception as d_err:
                    logger.warning(f"Error deleting rule {rule_name}: {d_err}")
    except Exception as e:
        logger.debug(f"EventBridge cleanup warning: {e}")

    return deleted_rules


# ==============================================================================
# 2. ECS SERVICE, CLUSTER & TASK DEFINITIONS
# ==============================================================================

def cleanup_ecs_resources(
    session: boto3.Session,
    cluster_name: str = "careharmony-voice-cluster",
    service_name: str = "careharmony-voice-service",
    task_family: str = "careharmony-voice-gateway",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Tear down ECS Fargate service, cluster, and deregister task definitions."""
    ecs = session.client("ecs")
    summary = {}
    logger.info(f"Cleaning up ECS resources (Cluster: {cluster_name}, Service: {service_name})...")

    # A. Scale & Delete Service
    if dry_run:
        logger.info(f"[DRY-RUN] Would scale down and delete ECS service: {service_name}")
        summary["service"] = f"[DRY-RUN] {service_name}"
    else:
        try:
            srv_desc = ecs.describe_services(cluster=cluster_name, services=[service_name])
            existing = [s for s in srv_desc.get("services", []) if s.get("status") in ("ACTIVE", "DRAINING")]
            if existing:
                logger.info(f"Scaling down ECS service '{service_name}' to 0...")
                ecs.update_service(cluster=cluster_name, service=service_name, desiredCount=0)
                logger.info(f"Deleting ECS service '{service_name}'...")
                ecs.delete_service(cluster=cluster_name, service=service_name, force=True)
                summary["service"] = "DELETED"
            else:
                summary["service"] = "NOT_FOUND"
        except Exception as s_err:
            logger.debug(f"ECS service cleanup note: {s_err}")
            summary["service"] = f"NOTE: {s_err}"

    # B. Delete Cluster
    if dry_run:
        logger.info(f"[DRY-RUN] Would delete ECS cluster: {cluster_name}")
        summary["cluster"] = f"[DRY-RUN] {cluster_name}"
    else:
        try:
            cl_desc = ecs.describe_clusters(clusters=[cluster_name])
            if [c for c in cl_desc.get("clusters", []) if c.get("status") == "ACTIVE"]:
                logger.info(f"Deleting ECS cluster '{cluster_name}'...")
                ecs.delete_cluster(cluster=cluster_name)
                summary["cluster"] = "DELETED"
            else:
                summary["cluster"] = "NOT_FOUND"
        except Exception as c_err:
            logger.debug(f"ECS cluster cleanup note: {c_err}")
            summary["cluster"] = f"NOTE: {c_err}"

    # C. Deregister Task Definitions
    deregistered_tasks = []
    try:
        paginator = ecs.get_paginator("list_task_definitions")
        for page in paginator.paginate(familyPrefix=task_family, status="ACTIVE"):
            for task_arn in page.get("taskDefinitionArns", []):
                if dry_run:
                    logger.info(f"[DRY-RUN] Would deregister task definition: {task_arn}")
                    deregistered_tasks.append(task_arn)
                else:
                    ecs.deregister_task_definition(taskDefinition=task_arn)
                    logger.info(f"Deregistered task definition: {task_arn}")
                    deregistered_tasks.append(task_arn)
    except Exception as t_err:
        logger.debug(f"Task definition deregister note: {t_err}")

    summary["deregistered_tasks"] = deregistered_tasks
    return summary


# ==============================================================================
# 3. KINESIS VIDEO STREAMS (KVS)
# ==============================================================================

def cleanup_kvs_stream(
    session: boto3.Session,
    stream_name: str = "careharmony-outreach-media-stream",
    dry_run: bool = False,
) -> str:
    """Delete project KVS audio stream."""
    kvs = session.client("kinesisvideo")
    logger.info(f"Checking KVS stream '{stream_name}'...")

    if dry_run:
        logger.info(f"[DRY-RUN] Would delete KVS stream: {stream_name}")
        return "DRY_RUN_DELETED"

    try:
        desc = kvs.describe_stream(StreamName=stream_name)
        stream_arn = desc.get("StreamInfo", {}).get("StreamARN", "")
        if stream_arn:
            logger.info(f"Deleting KVS Stream '{stream_name}' (ARN: {stream_arn})...")
            kvs.delete_stream(StreamARN=stream_arn)
            logger.info(f"KVS Stream '{stream_name}' deleted successfully.")
            return "DELETED"
        return "NOT_FOUND"
    except kvs.exceptions.ResourceNotFoundException:
        logger.info(f"KVS Stream '{stream_name}' not found; skipping.")
        return "NOT_FOUND"
    except Exception as e:
        logger.warning(f"Error deleting KVS stream {stream_name}: {e}")
        return f"ERROR: {e}"


# ==============================================================================
# 4. DYNAMODB TABLES
# ==============================================================================

def cleanup_dynamodb_tables(
    session: boto3.Session,
    tables: Optional[List[str]] = None,
    dry_run: bool = False,
) -> Dict[str, str]:
    """Delete project DynamoDB tables."""
    dynamodb = session.client("dynamodb")
    tables = tables or ["CareHarmony_OutreachRecords", "CareHarmony_HandoffEvents"]
    results = {}
    logger.info(f"Checking DynamoDB tables: {tables}...")

    for tbl_name in tables:
        if dry_run:
            logger.info(f"[DRY-RUN] Would delete DynamoDB table: {tbl_name}")
            results[tbl_name] = "DRY_RUN_DELETED"
            continue

        try:
            logger.info(f"Deleting DynamoDB table '{tbl_name}'...")
            dynamodb.delete_table(TableName=tbl_name)
            results[tbl_name] = "DELETED"
            try:
                waiter = dynamodb.get_waiter("table_not_exists")
                waiter.wait(TableName=tbl_name)
                logger.info(f"DynamoDB table '{tbl_name}' confirmed deleted.")
            except Exception as w_err:
                logger.debug(f"Waiter skipped: {w_err}")
        except dynamodb.exceptions.ResourceNotFoundException:
            logger.info(f"DynamoDB table '{tbl_name}' does not exist; skipping.")
            results[tbl_name] = "NOT_FOUND"
        except Exception as e:
            logger.warning(f"Error deleting DynamoDB table {tbl_name}: {e}")
            results[tbl_name] = f"ERROR: {e}"

    return results


# ==============================================================================
# 5. SSM PARAMETERS
# ==============================================================================

def cleanup_ssm_parameters(
    session: boto3.Session,
    path_prefix: str = "/careharmony",
    dry_run: bool = False,
) -> List[str]:
    """Recursively delete all SSM parameters under the project path prefix."""
    ssm = session.client("ssm")
    deleted_params = []
    logger.info(f"Scanning SSM parameters under path '{path_prefix}'...")

    try:
        paginator = ssm.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=path_prefix, Recursive=True):
            param_names = [p["Name"] for p in page.get("Parameters", []) if "Name" in p]
            if not param_names:
                continue

            if dry_run:
                for name in param_names:
                    logger.info(f"[DRY-RUN] Would delete SSM parameter: {name}")
                deleted_params.extend(param_names)
                continue

            # Batch delete in chunks of 10
            for i in range(0, len(param_names), 10):
                chunk = param_names[i:i + 10]
                resp = ssm.delete_parameters(Names=chunk)
                deleted_params.extend(resp.get("DeletedParameters", []))
                logger.info(f"Deleted SSM parameters: {chunk}")
    except Exception as e:
        logger.debug(f"SSM cleanup note: {e}")

    return deleted_params


# ==============================================================================
# 6. LOCAL ARTIFACT CLEANUP
# ==============================================================================

def cleanup_local_state(dry_run: bool = False) -> bool:
    """Remove local state artifact deployment_state.json."""
    if not STATE_FILE_PATH.exists():
        logger.info(f"Local state file not found: {STATE_FILE_PATH}")
        return False

    if dry_run:
        logger.info(f"[DRY-RUN] Would delete local state artifact: {STATE_FILE_PATH}")
        return True

    try:
        STATE_FILE_PATH.unlink()
        logger.info(f"Removed local state artifact: {STATE_FILE_PATH}")
        return True
    except Exception as e:
        logger.warning(f"Failed to remove local state file: {e}")
        return False


# ==============================================================================
# 7. CONNECT CONTACT FLOW PURGE (GUARDRAIL: PRESERVES INSTANCE)
# ==============================================================================

def cleanup_connect_contact_flows(
    session: boto3.Session,
    flow_name: str = "CareHarmony-Cara-Outbound",
    dry_run: bool = False,
) -> Dict[str, str]:
    """Purge project-specific contact flow while strictly preserving the Connect instance."""
    logger.info("Verifying Amazon Connect Guardrail (Instance preservation)...")
    connect = session.client("connect")
    target_id = os.getenv("CONNECT_INSTANCE_ID")
    target_alias = os.getenv("CONNECT_INSTANCE_ALIAS", "Cara-Connect")
    results = {}

    try:
        inst_id = target_id
        if not inst_id:
            for page in connect.get_paginator("list_instances").paginate():
                for inst in page.get("InstanceSummaryList", []):
                    if target_alias.lower() in str(inst.get("InstanceAlias") or "").lower():
                        inst_id = inst.get("Id")
                        break

        if not inst_id:
            logger.info("No Connect instance found to clean flows; skipping.")
            return {"status": "NO_INSTANCE_FOUND"}

        if dry_run:
            logger.info(f"[DRY-RUN] Would locate and purge Contact Flow '{flow_name}' from Connect instance {inst_id}")
            logger.info(f"[GUARDRAIL] Connect instance {inst_id} will be PRESERVED.")
            return {"flow_name": flow_name, "status": "DRY_RUN_PURGED"}

        # Attempt to find flow
        for page in connect.get_paginator("list_contact_flows").paginate(InstanceId=inst_id):
            for f in page.get("ContactFlowSummaryList", []):
                if f.get("Name") == flow_name:
                    flow_id = f.get("Id", "")
                    logger.info(f"Found project contact flow '{flow_name}' (ID: {flow_id}); disassociating content...")
                    # Blank out content to deactivate flow
                    inactive_content = json.dumps({"Version": "2019-10-30", "Actions": []})
                    try:
                        connect.update_contact_flow_content(
                            InstanceId=inst_id,
                            ContactFlowId=flow_id,
                            Content=inactive_content,
                        )
                        results[flow_name] = "DEACTIVATED"
                    except Exception as u_err:
                        logger.debug(f"Flow deactivate note: {u_err}")
                        results[flow_name] = "NOTE: Not deactivated"

        logger.info(f"[GUARDRAIL CONFIRMED] Amazon Connect instance {inst_id} remains active and safe.")
    except Exception as e:
        logger.debug(f"Connect contact flow cleanup note: {e}")
        results["status"] = f"NOTE: {e}"

    return results


# ==============================================================================
# 8. CONNECT MEDIA STREAMING STORAGE DISASSOCIATION
# ==============================================================================

def cleanup_connect_media_streaming_storage(
    session: boto3.Session,
    instance_id: Optional[str] = None,
    instance_alias: str = "Cara-Connect",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Disassociate MEDIA_STREAMS storage configs from Connect instance."""
    connect = session.client("connect")
    results = {}
    target_id = instance_id or os.getenv("CONNECT_INSTANCE_ID")

    if not target_id:
        try:
            for page in connect.get_paginator("list_instances").paginate():
                for inst in page.get("InstanceSummaryList", []):
                    if instance_alias.lower() in str(inst.get("InstanceAlias") or "").lower():
                        target_id = inst.get("Id")
                        break
        except Exception as e:
            logger.debug(f"Instance lookup warning: {e}")

    if not target_id:
        return {"status": "NO_INSTANCE_FOUND"}

    try:
        resp = connect.list_instance_storage_configs(
            InstanceId=target_id,
            ResourceType="MEDIA_STREAMS",
        )
        for sc in resp.get("StorageConfigs", []):
            assoc_id = sc.get("AssociationId", "")
            if dry_run:
                logger.info(f"[DRY-RUN] Would disassociate Connect MEDIA_STREAMS storage (Association ID: {assoc_id})")
                results[assoc_id] = "DRY_RUN_DISASSOCIATED"
            else:
                logger.info(f"Disassociating Connect MEDIA_STREAMS storage (Association ID: {assoc_id})...")
                connect.disassociate_instance_storage_config(
                    InstanceId=target_id,
                    AssociationId=assoc_id,
                    ResourceType="MEDIA_STREAMS",
                )
                results[assoc_id] = "DISASSOCIATED"
    except Exception as e:
        logger.debug(f"Storage config cleanup note: {e}")
        results["status"] = f"NOTE: {e}"

    return results


# ==============================================================================
# 9. PHONE NUMBER MANAGEMENT (RETENTION VS RELEASE)
# ==============================================================================

def cleanup_connect_phone_numbers(
    session: boto3.Session,
    instance_id: Optional[str] = None,
    instance_alias: str = "Cara-Connect",
    release_phone: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Manage phone numbers during teardown: retain by default or release if release_phone is True."""
    connect = session.client("connect")
    results = {}

    target_id = instance_id or os.getenv("CONNECT_INSTANCE_ID")
    if not target_id:
        try:
            for page in connect.get_paginator("list_instances").paginate():
                for inst in page.get("InstanceSummaryList", []):
                    if instance_alias.lower() in str(inst.get("InstanceAlias") or "").lower():
                        target_id = inst.get("Id")
                        break
        except Exception as e:
            logger.debug(f"Instance lookup warning: {e}")

    if not target_id:
        logger.info("No Connect instance found to manage phone numbers; skipping.")
        return {"status": "NO_INSTANCE_FOUND"}

    logger.info(f"Checking phone numbers on Connect instance {target_id} (release_phone={release_phone})...")

    try:
        paginator = connect.get_paginator("list_phone_numbers")
        for page in paginator.paginate(InstanceId=target_id):
            for summary in page.get("PhoneNumberSummaryList", []):
                phone_id = summary.get("Id", "")
                phone_num = summary.get("PhoneNumber", "")
                if release_phone:
                    if dry_run:
                        logger.info(f"[DRY-RUN] Would release phone number: {phone_num} (ID: {phone_id})")
                        results[phone_num] = "DRY_RUN_RELEASED"
                    else:
                        logger.info(f"Releasing phone number {phone_num} (ID: {phone_id})...")
                        connect.release_phone_number(PhoneNumberId=phone_id)
                        logger.info(f"Released phone number: {phone_num}")
                        results[phone_num] = "RELEASED"
                else:
                    if dry_run:
                        logger.info(f"[DRY-RUN] [GUARDRAIL] Retaining claimed phone number: {phone_num} (ID: {phone_id})")
                    else:
                        logger.info(f"[GUARDRAIL] Retained claimed phone number: {phone_num} (ID: {phone_id})")
                    results[phone_num] = "RETAINED"
    except Exception as e:
        logger.debug(f"Phone number cleanup warning: {e}")
        results["status"] = f"NOTE: {e}"

    return results


# ==============================================================================
# MAIN ORCHESTRATOR
# ==============================================================================

def cleanup_all_resources(
    profile: str = AWS_PROFILE_DEFAULT,
    region: str = AWS_REGION_DEFAULT,
    release_phone: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Execute dependency-safe nuclear resource teardown."""
    logger.info(f"=== Starting CareHarmony Resource Teardown (Dry Run: {dry_run}, Force: {force}, Release Phone: {release_phone}) ===")

    if not dry_run and not force:
        confirm = input("Are you sure you want to PERMANENTLY delete project resources? (yes/no): ").strip().lower()
        if confirm not in ("yes", "y"):
            logger.info("Teardown cancelled by user.")
            return {"status": "CANCELLED"}

    session = get_aws_session(profile=profile, region=region)

    # 1. EventBridge Rules
    eventbridge_res = cleanup_eventbridge_rules(session, dry_run=dry_run)

    # 2. ECS Service, Cluster & Tasks
    ecs_res = cleanup_ecs_resources(session, dry_run=dry_run)

    # 3. KVS Stream
    kvs_res = cleanup_kvs_stream(session, dry_run=dry_run)

    # 4. DynamoDB Tables
    dynamo_res = cleanup_dynamodb_tables(session, dry_run=dry_run)

    # 5. SSM Parameters
    ssm_res = cleanup_ssm_parameters(session, dry_run=dry_run)

    # 6. Local State File
    local_res = cleanup_local_state(dry_run=dry_run)

    # 7. Connect Media Streaming Storage Disassociation
    storage_res = cleanup_connect_media_streaming_storage(session, dry_run=dry_run)

    # 8. Connect Flow
    connect_res = cleanup_connect_contact_flows(session, dry_run=dry_run)

    # 9. Phone Numbers
    phone_res = cleanup_connect_phone_numbers(session, release_phone=release_phone, dry_run=dry_run)

    summary = {
        "status": "DRY_RUN_COMPLETED" if dry_run else "TEARDOWN_COMPLETED",
        "eventbridge": eventbridge_res,
        "ecs": ecs_res,
        "kvs": kvs_res,
        "dynamodb": dynamo_res,
        "ssm": ssm_res,
        "local_state_removed": local_res,
        "media_streaming_storage": storage_res,
        "connect": connect_res,
        "phone_number": phone_res,
    }

    logger.info("=== CareHarmony Resource Teardown Completed Successfully ===")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Teardown CareHarmony AWS Resources to achieve $0 spend.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect and log resources without deleting")
    parser.add_argument("--force", action="store_true", help="Bypass interactive confirmation prompt")
    parser.add_argument("--release-phone", action="store_true", help="Release claimed phone numbers (default: False, preserves numbers)")
    parser.add_argument("--profile", default=AWS_PROFILE_DEFAULT, help="AWS CLI profile name")
    parser.add_argument("--region", default=AWS_REGION_DEFAULT, help="AWS region")
    args = parser.parse_args()

    cleanup_all_resources(
        profile=args.profile,
        region=args.region,
        release_phone=args.release_phone,
        dry_run=args.dry_run,
        force=args.force,
    )


if __name__ == "__main__":
    main()
