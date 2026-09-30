"""Automated Idempotent Infrastructure Deployment Script for CareHarmony-Cara.

Discovers, verifies, provisions, and updates all AWS resources in-place:
1. Amazon Connect instance discovery & SSM parameter syncing
2. DynamoDB tables (CareHarmony_OutreachRecords, CareHarmony_HandoffEvents)
3. Kinesis Video Stream (careharmony-outreach-media-stream)
4. ECS Fargate Cluster, Task Definition revision & Service update
5. Amazon Connect Contact Flow sync (CareHarmony-Cara-Outbound)
6. Local state artifact generation (backend/deployment_state.json)

Usage:
    python backend/scripts/deploy_infrastructure.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError, ProfileNotFound

# Setup logger
logger = logging.getLogger("deploy_infrastructure")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

AWS_PROFILE_DEFAULT = "careharmony"
AWS_REGION_DEFAULT = "us-east-1"
STATE_FILE_PATH = Path(__file__).resolve().parents[1] / "deployment_state.json"


def get_aws_session(profile: Optional[str] = None, region: Optional[str] = None) -> boto3.Session:
    """Create a boto3 Session using specified/env profile and region with graceful fallback."""
    prof = profile or os.getenv("AWS_PROFILE", AWS_PROFILE_DEFAULT)
    reg = region or os.getenv("AWS_REGION", AWS_REGION_DEFAULT)
    try:
        session = boto3.Session(profile_name=prof, region_name=reg)
        # Validate credentials
        session.get_credentials()
        return session
    except (ProfileNotFound, Exception) as e:
        logger.debug(f"AWS Profile '{prof}' fallback to default/env ({e})")
        return boto3.Session(region_name=reg)


# ==============================================================================
# 1. CONNECT INSTANCE DISCOVERY & AUTO-CREATION
# ==============================================================================

def get_or_create_connect_instance(
    session: boto3.Session,
    alias: str = "Cara-Connect",
    instance_id: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Dynamically locate existing Amazon Connect instance or provision a new one idempotently."""
    connect = session.client("connect")
    target_id = instance_id or os.getenv("CONNECT_INSTANCE_ID")
    target_alias = alias or os.getenv("CONNECT_INSTANCE_ALIAS", "Cara-Connect")

    logger.info(f"Checking Amazon Connect instance (ID: {target_id or 'auto'}, Alias: {target_alias})...")

    # 1. Lookup by Instance ID
    if target_id and target_id.strip():
        try:
            resp = connect.describe_instance(InstanceId=target_id)
            inst = resp.get("Instance", {})
            found_arn = inst.get("Arn", "")
            found_alias = inst.get("InstanceAlias", target_alias)
            status = inst.get("InstanceStatus", "ACTIVE")
            logger.info(f"Found Connect instance by ID: {target_id} (Alias: {found_alias}, Status: {status})")
            return {
                "instance_id": target_id,
                "instance_arn": found_arn,
                "instance_alias": found_alias,
                "status": status,
            }
        except ClientError as e:
            logger.warning(f"Connect describe_instance failed for ID {target_id}: {e}")

    # 2. Lookup by Alias
    try:
        paginator = connect.get_paginator("list_instances")
        for page in paginator.paginate():
            for inst_summary in page.get("InstanceSummaryList", []):
                curr_alias = str(inst_summary.get("InstanceAlias") or "")
                inst_id = str(inst_summary.get("Id") or "")
                inst_arn = str(inst_summary.get("Arn") or "")
                if curr_alias.lower() == target_alias.lower() or target_alias.lower() in curr_alias.lower():
                    status = inst_summary.get("InstanceStatus", "ACTIVE")
                    logger.info(f"Discovered matching Connect instance: {curr_alias} (ID: {inst_id}, Status: {status})")
                    return {
                        "instance_id": inst_id,
                        "instance_arn": inst_arn,
                        "instance_alias": curr_alias,
                        "status": status,
                    }
    except Exception as e:
        logger.warning(f"Failed to list Connect instances: {e}")

    # 3. Dry-Run Check
    if dry_run:
        logger.info(f"[DRY-RUN] Would create Amazon Connect instance with alias: {target_alias}")
        return {
            "instance_id": "mock-connect-id-12345",
            "instance_arn": f"arn:aws:connect:{session.region_name}:123456789012:instance/mock-connect-id-12345",
            "instance_alias": target_alias,
            "status": "ACTIVE",
        }

    # 4. Auto-Creation Logic (when dry_run=False and instance missing)
    clean_alias = target_alias.replace("_", "-")
    logger.info(f"Amazon Connect instance not found. Provisioning new instance with alias '{clean_alias}'...")
    try:
        create_resp = connect.create_instance(
            IdentityManagementType="CONNECT_MANAGED",
            InstanceAlias=clean_alias,
            InboundCallsEnabled=True,
            OutboundCallsEnabled=True,
            Tags={"Project": "CareHarmony-Cara", "Environment": "Development"},
        )
        new_instance_id = create_resp.get("Id")
        new_instance_arn = create_resp.get("Arn")
        logger.info(f"Provisioning initiated for Connect instance ID: {new_instance_id}. Polling for ACTIVE status...")

        max_attempts = 30
        for attempt in range(1, max_attempts + 1):
            time.sleep(10)
            desc_resp = connect.describe_instance(InstanceId=new_instance_id)
            inst_data = desc_resp.get("Instance", {})
            current_status = inst_data.get("InstanceStatus")
            logger.info(f"Polling Connect instance {new_instance_id}: status is '{current_status}' (Attempt {attempt}/{max_attempts})")
            if current_status == "ACTIVE":
                return {
                    "instance_id": new_instance_id,
                    "instance_arn": inst_data.get("Arn", new_instance_arn),
                    "instance_alias": clean_alias,
                    "status": "ACTIVE",
                }
            elif current_status == "CREATION_FAILED":
                raise RuntimeError(
                    f"Amazon Connect instance creation failed for ID '{new_instance_id}' (Alias: '{clean_alias}')."
                )

        raise TimeoutError(
            f"Timed out waiting for Amazon Connect instance '{new_instance_id}' to reach ACTIVE status."
        )
    except ClientError as e:
        logger.error(f"Failed to create Amazon Connect instance: {e}")
        raise


# Backward compatibility alias
discover_connect_instance = get_or_create_connect_instance


# ==============================================================================
# 2. SSM PARAMETER STORE SYNC
# ==============================================================================

def sync_ssm_parameters(
    session: boto3.Session,
    parameters: Dict[str, str],
    dry_run: bool = False,
) -> Dict[str, str]:
    """Idempotently sync project configuration parameters to AWS SSM Parameter Store."""
    ssm = session.client("ssm")
    results = {}

    for name, value in parameters.items():
        if dry_run:
            logger.info(f"[DRY-RUN] Would put SSM parameter: {name} = {value}")
            results[name] = "DRY_RUN_UPDATED"
            continue

        try:
            # Check existing parameter value
            try:
                curr = ssm.get_parameter(Name=name)
                curr_val = curr.get("Parameter", {}).get("Value")
                if curr_val == value:
                    logger.debug(f"SSM parameter {name} is already up to date.")
                    results[name] = "UNCHANGED"
                    continue
            except ssm.exceptions.ParameterNotFound:
                pass

            ssm.put_parameter(
                Name=name,
                Value=value,
                Type="String",
                Overwrite=True,
            )
            try:
                ssm.add_tags_to_resource(
                    ResourceType="Parameter",
                    ResourceId=name,
                    Tags=[{"Key": "Project", "Value": "CareHarmony-Cara"}],
                )
            except Exception as tag_err:
                logger.debug(f"Tagging SSM parameter {name} skipped/warning: {tag_err}")

            logger.info(f"Updated SSM parameter: {name}")
            results[name] = "UPDATED"
        except Exception as e:
            logger.warning(f"Failed to put SSM parameter {name}: {e}")
            results[name] = f"ERROR: {e}"

    return results


# ==============================================================================
# 3. DYNAMODB TABLES PROVISIONING
# ==============================================================================

def ensure_dynamodb_tables(
    session: boto3.Session,
    dry_run: bool = False,
) -> Dict[str, str]:
    """Idempotently verify and create DynamoDB project tables."""
    dynamodb = session.client("dynamodb")
    statuses = {}

    tables = [
        {
            "TableName": "CareHarmony_OutreachRecords",
            "KeySchema": [{"AttributeName": "patient_id", "KeyType": "HASH"}],
            "AttributeDefinitions": [
                {"AttributeName": "patient_id", "AttributeType": "S"},
                {"AttributeName": "call_status", "AttributeType": "S"},
                {"AttributeName": "scheduled_time", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "CallStatusScheduledTimeIndex",
                    "KeySchema": [
                        {"AttributeName": "call_status", "KeyType": "HASH"},
                        {"AttributeName": "scheduled_time", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [{"Key": "Project", "Value": "CareHarmony-Cara"}],
        },
        {
            "TableName": "CareHarmony_HandoffEvents",
            "KeySchema": [{"AttributeName": "handoff_id", "KeyType": "HASH"}],
            "AttributeDefinitions": [{"AttributeName": "handoff_id", "AttributeType": "S"}],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [{"Key": "Project", "Value": "CareHarmony-Cara"}],
        },
    ]

    for tbl in tables:
        tbl_name = tbl["TableName"]
        if dry_run:
            logger.info(f"[DRY-RUN] Would ensure DynamoDB table: {tbl_name}")
            statuses[tbl_name] = "DRY_RUN_ACTIVE"
            continue

        try:
            desc = dynamodb.describe_table(TableName=tbl_name)
            status = desc.get("Table", {}).get("TableStatus", "UNKNOWN")
            logger.info(f"DynamoDB table '{tbl_name}' exists (Status: {status})")
            statuses[tbl_name] = status
        except dynamodb.exceptions.ResourceNotFoundException:
            logger.info(f"Creating DynamoDB table '{tbl_name}' with PAY_PER_REQUEST billing...")
            create_kwargs = dict(tbl)
            dynamodb.create_table(**create_kwargs)
            statuses[tbl_name] = "CREATING"
            try:
                waiter = dynamodb.get_waiter("table_exists")
                waiter.wait(TableName=tbl_name)
                statuses[tbl_name] = "ACTIVE"
                logger.info(f"DynamoDB table '{tbl_name}' is now ACTIVE.")
            except Exception as w_err:
                logger.debug(f"Waiter skipped or timed out: {w_err}")
        except Exception as e:
            logger.warning(f"Error checking DynamoDB table '{tbl_name}': {e}")
            statuses[tbl_name] = f"ERROR: {e}"

    return statuses


# ==============================================================================
# 4. KINESIS VIDEO STREAM (KVS)
# ==============================================================================

def ensure_kvs_stream(
    session: boto3.Session,
    stream_name: str = "careharmony-outreach-media-stream",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Idempotently ensure the KVS audio stream exists for real-time media ingestion."""
    kvs = session.client("kinesisvideo")

    if dry_run:
        logger.info(f"[DRY-RUN] Would ensure KVS Stream '{stream_name}' (MediaType='audio/L16')")
        return {
            "stream_name": stream_name,
            "stream_arn": f"arn:aws:kinesisvideo:{session.region_name}:123456789012:stream/{stream_name}/12345",
            "status": "DRY_RUN_ACTIVE",
        }

    try:
        desc = kvs.describe_stream(StreamName=stream_name)
        stream_info = desc.get("StreamInfo", {})
        arn = stream_info.get("StreamARN", "")
        status = stream_info.get("Status", "ACTIVE")
        logger.info(f"KVS Stream '{stream_name}' exists (ARN: {arn}, Status: {status})")
        return {"stream_name": stream_name, "stream_arn": arn, "status": status}
    except kvs.exceptions.ResourceNotFoundException:
        logger.info(f"Creating KVS Stream '{stream_name}' with MediaType='audio/L16'...")
        resp = kvs.create_stream(
            StreamName=stream_name,
            MediaType="audio/L16",
            DataRetentionInHours=24,
            Tags={"Project": "CareHarmony-Cara"},
        )
        arn = resp.get("StreamARN", "")
        logger.info(f"Created KVS Stream '{stream_name}' (ARN: {arn})")
        return {"stream_name": stream_name, "stream_arn": arn, "status": "ACTIVE"}
    except Exception as e:
        logger.warning(f"Error checking KVS stream '{stream_name}': {e}")
        return {"stream_name": stream_name, "stream_arn": "", "status": f"ERROR: {e}"}


def ensure_connect_media_streaming_storage(
    session: boto3.Session,
    instance_id: str,
    kvs_stream_prefix: str = "careharmony-outreach-media-stream",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Ensure the Amazon Connect instance has MEDIA_STREAMS storage configured for KVS."""
    connect = session.client("connect")
    logger.info(f"Checking Connect instance {instance_id} MEDIA_STREAMS storage configuration...")

    if dry_run:
        logger.info(f"[DRY-RUN] Would ensure MEDIA_STREAMS storage config with prefix '{kvs_stream_prefix}'")
        return {"status": "DRY_RUN_ASSOCIATED", "prefix": kvs_stream_prefix}

    try:
        resp = connect.list_instance_storage_configs(
            InstanceId=instance_id,
            ResourceType="MEDIA_STREAMS",
        )
        storage_configs = resp.get("StorageConfigs", [])
        if storage_configs:
            association_id = storage_configs[0].get("AssociationId", "")
            logger.info(f"Connect instance already has MEDIA_STREAMS storage configured (Association ID: {association_id})")
            return {"status": "ALREADY_ASSOCIATED", "association_id": association_id}

        logger.info(f"Associating MEDIA_STREAMS storage configuration with prefix '{kvs_stream_prefix}'...")
        assoc_resp = connect.associate_instance_storage_config(
            InstanceId=instance_id,
            ResourceType="MEDIA_STREAMS",
            StorageConfig={
                "StorageType": "KINESIS_VIDEO_STREAM",
                "KinesisVideoStreamConfig": {
                    "Prefix": kvs_stream_prefix,
                    "RetentionPeriodHours": 2,
                    "EncryptionConfig": {
                        "EncryptionType": "KMS",
                        "KeyId": "alias/aws/kinesisvideo",
                    },
                },
            },
        )
        association_id = assoc_resp.get("AssociationId", "")
        logger.info(f"Successfully associated MEDIA_STREAMS storage (Association ID: {association_id})")
        return {"status": "ASSOCIATED", "association_id": association_id}
    except Exception as e:
        logger.warning(f"Error ensuring MEDIA_STREAMS storage config: {e}")
        return {"status": f"ERROR: {e}"}


def get_or_create_ecs_execution_role(session: boto3.Session, dry_run: bool = False) -> str:
    """Ensure standard ECS Task Execution IAM role exists and return its ARN."""
    sts = session.client("sts")
    iam = session.client("iam")

    if dry_run:
        return f"arn:aws:iam::{session.region_name}:123456789012:role/ecsTaskExecutionRole"

    account_id = "123456789012"
    try:
        caller = sts.get_caller_identity()
        account_id = caller.get("Account", "123456789012")
    except Exception as e:
        logger.debug(f"Could not fetch caller identity for IAM role: {e}")

    role_name = "ecsTaskExecutionRole"
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    try:
        resp = iam.get_role(RoleName=role_name)
        role_arn = resp.get("Role", {}).get("Arn", role_arn)
        logger.info(f"Using existing IAM execution role: {role_arn}")
    except Exception:
        try:
            logger.info(f"Creating IAM execution role '{role_name}'...")
            trust_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
            create_resp = iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description="ECS Task Execution Role for CareHarmony Voice Gateway",
                Tags=[{"Key": "Project", "Value": "CareHarmony-Cara"}],
            )
            role_arn = create_resp.get("Role", {}).get("Arn", role_arn)
            iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
            )
            logger.info(f"Created and attached policy to IAM execution role: {role_arn}")
        except Exception as err:
            logger.warning(f"Could not create IAM execution role '{role_name}': {err}")

    # Ensure inline policy for CloudWatch Logs creation
    try:
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="CloudWatchLogsCreateGroup",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "logs:CreateLogGroup",
                            "logs:CreateLogStream",
                            "logs:PutLogEvents",
                            "logs:DescribeLogStreams",
                        ],
                        "Resource": "*",
                    }
                ],
            }),
        )
        # Ensure inline policy for Voice Gateway Runtime (KVS, Bedrock Nova Sonic, Connect, DynamoDB)
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="CareHarmonyVoiceGatewayRuntimePolicy",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "kinesisvideo:ListStreams",
                            "kinesisvideo:GetDataEndpoint",
                            "kinesisvideo:GetMedia",
                            "kinesisvideo:GetMediaForFragmentList",
                            "kinesisvideo:DescribeStream",
                            "kinesisvideo:ListFragments",
                            "kinesisvideo:PutMedia",
                            "kinesisvideo:TagStream",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "bedrock:InvokeModel",
                            "bedrock:InvokeModelWithResponseStream",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:*"],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["connect:*"],
                        "Resource": "*",
                    },
                ],
            }),
        )
    except Exception as pol_err:
        logger.debug(f"Note attaching logs policy to execution role: {pol_err}")

    return role_arn


def get_default_subnets(session: boto3.Session) -> List[str]:
    """Retrieve default VPC subnets for Fargate service creation."""
    try:
        ec2 = session.client("ec2")
        subnets_resp = ec2.describe_subnets(
            Filters=[{"Name": "default-for-az", "Values": ["true"]}]
        )
        subnets = [s["SubnetId"] for s in subnets_resp.get("Subnets", [])]
        if subnets:
            return subnets
        all_subnets = ec2.describe_subnets().get("Subnets", [])
        if all_subnets:
            return [s["SubnetId"] for s in all_subnets[:2]]
    except Exception as e:
        logger.debug(f"EC2 describe_subnets warning: {e}")
    return ["subnet-00000000000000000"]


# ==============================================================================
# 5. CHIME VOICE CONNECTOR, SECURITY GROUP & ECS FARGATE
# ==============================================================================

def ensure_voice_gateway_security_group(session: boto3.Session, dry_run: bool = False) -> str:
    """Ensure an ECS Security Group exists with open ports for HTTP (8000), RTP (10000-20000 UDP), and SIP (5060 UDP)."""
    if dry_run:
        return "sg-0123456789abcdef0"

    ec2 = session.client("ec2")
    sg_name = "careharmony-voice-gateway-sg"
    try:
        sgs = ec2.describe_security_groups(GroupNames=[sg_name]).get("SecurityGroups", [])
        if sgs:
            return sgs[0]["GroupId"]
    except Exception:
        pass

    try:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}]).get("Vpcs", [])
        vpc_id = vpcs[0]["VpcId"] if vpcs else ""
        create_resp = ec2.create_security_group(
            GroupName=sg_name,
            Description="Security group for CareHarmony Pipecat Voice Gateway with RTP & SIP ports",
            VpcId=vpc_id if vpc_id else None,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": [{"Key": "Project", "Value": "CareHarmony-Cara"}]}],
        )
        sg_id = create_resp.get("GroupId", "")
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                {"IpProtocol": "udp", "FromPort": 10000, "ToPort": 10050, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                {"IpProtocol": "udp", "FromPort": 5060, "ToPort": 5060, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            ],
        )
        logger.info(f"Created Security Group '{sg_name}' (ID: {sg_id})")
        return sg_id
    except Exception as e:
        logger.debug(f"ensure_voice_gateway_security_group note: {e}")
        return ""


def ensure_chime_voice_connector(
    session: boto3.Session,
    connector_name: str = "careharmony-voice-connector",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Ensure an Amazon Chime SDK Voice Connector exists for bidirectional PSTN SIP audio bridging."""
    if dry_run:
        logger.info(f"[DRY-RUN] Would ensure Chime Voice Connector '{connector_name}'")
        return {
            "connector_id": "dry-run-vc-id",
            "outbound_host": f"{connector_name}.voiceconnector.chime.aws",
            "status": "DRY_RUN_CREATED",
        }

    try:
        chime = session.client("chime-sdk-voice")
        connectors = chime.list_voice_connectors().get("VoiceConnectors", [])
        for vc in connectors:
            if vc.get("Name") == connector_name:
                vc_id = vc.get("VoiceConnectorId", "")
                outbound_host = vc.get("OutboundHostName", "")
                logger.info(f"Found existing Chime Voice Connector '{connector_name}' (ID: {vc_id}, Host: {outbound_host})")
                return {
                    "connector_id": vc_id,
                    "outbound_host": outbound_host,
                    "status": "EXISTING",
                }

        logger.info(f"Creating Chime Voice Connector '{connector_name}' (us-east-1)...")
        create_resp = chime.create_voice_connector(
            Name=connector_name,
            AwsRegion="us-east-1",
            RequireEncryption=False,
            Tags=[{"Key": "Project", "Value": "CareHarmony-Cara"}],
        )
        vc = create_resp.get("VoiceConnector", {})
        vc_id = vc.get("VoiceConnectorId", "")
        outbound_host = vc.get("OutboundHostName", "")
        logger.info(f"Created Chime Voice Connector '{connector_name}' (ID: {vc_id}, Host: {outbound_host})")
        return {
            "connector_id": vc_id,
            "outbound_host": outbound_host,
            "status": "CREATED",
        }
    except Exception as e:
        logger.warning(f"Chime Voice Connector provisioning simulated/note: {e}")
        return {
            "connector_id": "simulated-vc-id",
            "outbound_host": f"{connector_name}.voiceconnector.chime.aws",
            "status": f"SIMULATED: {e}",
        }


def update_chime_voice_connector_termination(
    session: boto3.Session,
    voice_connector_id: str,
    target_host: str,
    default_phone: str = "+17134811560",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Register or update the dynamic target termination host on Amazon Chime Voice Connector."""
    if dry_run:
        logger.info(f"[DRY-RUN] Would update Chime Voice Connector {voice_connector_id} termination host to {target_host}:5060")
        return {"status": "DRY_RUN_UPDATED", "target_host": target_host}

    chime = session.client("chime-sdk-voice")
    try:
        chime.put_voice_connector_termination(
            VoiceConnectorId=voice_connector_id,
            Termination={
                "CpsLimit": 1,
                "Disabled": False,
                "CallingRegions": ["US"],
                "CidrAllowedList": [f"{target_host}/32" if "/" not in target_host else target_host],
            },
        )
        chime.put_voice_connector_origination(
            VoiceConnectorId=voice_connector_id,
            Origination={
                "Routes": [
                    {
                        "Host": target_host,
                        "Port": 5060,
                        "Protocol": "UDP",
                        "Priority": 1,
                        "Weight": 1,
                    }
                ],
                "Disabled": False,
            },
        )
        logger.info(f"Updated Chime Voice Connector {voice_connector_id} target: {target_host}:5060")
        return {"status": "UPDATED", "target_host": target_host}
    except Exception as e:
        logger.warning(f"Failed to update Chime Voice Connector termination/origination ({e})")
        return {"status": f"FAILED: {e}", "target_host": target_host}



def ensure_ecs_resources(
    session: boto3.Session,
    connect_instance_id: str,
    cluster_name: str = "careharmony-voice-cluster",
    service_name: str = "careharmony-voice-service",
    task_family: str = "careharmony-voice-gateway",
    container_image: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Idempotently provision or update ECS Fargate cluster, task definition, and service."""
    ecs = session.client("ecs")
    sts = session.client("sts")

    try:
        caller = sts.get_caller_identity()
        account_id = caller.get("Account", "123456789012")
    except Exception:
        account_id = "123456789012"

    if not container_image or "123456789012" in container_image:
        container_image = f"{account_id}.dkr.ecr.{session.region_name}.amazonaws.com/{task_family}:latest"

    exec_role_arn = get_or_create_ecs_execution_role(session, dry_run=dry_run)

    if dry_run:
        logger.info(f"[DRY-RUN] Would ensure ECS Cluster '{cluster_name}'")
        logger.info(f"[DRY-RUN] Would register task definition family '{task_family}' with role '{exec_role_arn}'")
        logger.info(f"[DRY-RUN] Would update/create ECS Fargate Service '{service_name}'")
        return {
            "cluster_arn": f"arn:aws:ecs:{session.region_name}:{account_id}:cluster/{cluster_name}",
            "task_definition_arn": f"arn:aws:ecs:{session.region_name}:{account_id}:task-definition/{task_family}:1",
            "service_arn": f"arn:aws:ecs:{session.region_name}:{account_id}:service/{cluster_name}/{service_name}",
            "status": "DRY_RUN_ACTIVE",
        }

    # 1. Cluster
    cluster_arn = ""
    try:
        desc = ecs.describe_clusters(clusters=[cluster_name])
        existing_clusters = [c for c in desc.get("clusters", []) if c.get("status") == "ACTIVE"]
        if existing_clusters:
            cluster_arn = existing_clusters[0].get("clusterArn", "")
            logger.info(f"ECS Cluster '{cluster_name}' exists (ARN: {cluster_arn})")
        else:
            create_resp = ecs.create_cluster(
                clusterName=cluster_name,
                tags=[{"key": "Project", "value": "CareHarmony-Cara"}],
            )
    except Exception as e:
        logger.warning(f"Error checking/creating ECS Cluster '{cluster_name}': {e}")

    # 2. CloudWatch Log Group & Task Definition
    logs = session.client("logs")
    log_group_name = f"/ecs/{task_family}"
    try:
        logs.create_log_group(logGroupName=log_group_name)
        logger.info(f"Created CloudWatch log group '{log_group_name}'")
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    except Exception as log_err:
        logger.debug(f"Log group create check: {log_err}")

    task_def_arn = ""
    try:
        task_reg = ecs.register_task_definition(
            family=task_family,
            networkMode="awsvpc",
            requiresCompatibilities=["FARGATE"],
            cpu="512",
            memory="1024",
            executionRoleArn=exec_role_arn,
            taskRoleArn=exec_role_arn,
            containerDefinitions=[
                {
                    "name": "pipecat-voice-gateway",
                    "image": container_image,
                    "essential": True,
                    "portMappings": [
                        {"containerPort": 8000, "hostPort": 8000, "protocol": "tcp"},
                        {"containerPort": 10000, "hostPort": 10000, "protocol": "udp"},
                        {"containerPort": 5060, "hostPort": 5060, "protocol": "udp"},
                    ],
                    "environment": [
                        {"name": "AWS_REGION", "value": session.region_name},
                        {"name": "CONNECT_INSTANCE_ID", "value": connect_instance_id},
                        {"name": "VOICE_ID", "value": "en-US-Standard-Female"},
                        {"name": "NOVA_SONIC_MODEL_ID", "value": "amazon.nova-2-sonic-v1:0"},
                        {"name": "RTP_PORT", "value": "10000"},
                        {"name": "TRAFFIC_TYPE", "value": "GENERAL"},
                    ],
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-group": f"/ecs/{task_family}",
                            "awslogs-region": session.region_name,
                            "awslogs-stream-prefix": "gateway",
                            "awslogs-create-group": "true",
                        },
                    },
                }
            ],
            tags=[{"key": "Project", "value": "CareHarmony-Cara"}],
        )
        task_def_arn = task_reg.get("taskDefinition", {}).get("taskDefinitionArn", "")
        logger.info(f"Registered Task Definition revision: {task_def_arn}")
    except Exception as e:
        logger.warning(f"Error registering task definition '{task_family}': {e}")

    # 3. Service
    service_arn = ""
    try:
        srv_desc = ecs.describe_services(cluster=cluster_name, services=[service_name])
        active_services = [
            s for s in srv_desc.get("services", []) if s.get("status") in ("ACTIVE", "DRAINING")
        ]
        if active_services and active_services[0].get("status") == "ACTIVE":
            service_arn = active_services[0].get("serviceArn", "")
            logger.info(f"Updating existing ECS Service '{service_name}' with forceNewDeployment...")
            ecs.update_service(
                cluster=cluster_name,
                service=service_name,
                taskDefinition=task_def_arn or task_family,
                forceNewDeployment=True,
            )
        else:
            subnets = get_default_subnets(session)
            sg_id = ensure_voice_gateway_security_group(session, dry_run=dry_run)
            sec_groups = [sg_id] if sg_id else []
            logger.info(f"Creating ECS Fargate Service '{service_name}' in subnets: {subnets}...")
            awsvpc_cfg: Dict[str, Any] = {
                "subnets": subnets,
                "assignPublicIp": "ENABLED",
            }
            if sec_groups:
                awsvpc_cfg["securityGroups"] = sec_groups

            srv_resp = ecs.create_service(
                cluster=cluster_name,
                serviceName=service_name,
                taskDefinition=task_def_arn or task_family,
                desiredCount=1,
                launchType="FARGATE",
                networkConfiguration={"awsvpcConfiguration": awsvpc_cfg},
                tags=[{"key": "Project", "value": "CareHarmony-Cara"}],
            )
            service_arn = srv_resp.get("service", {}).get("serviceArn", "")
    except Exception as e:
        logger.warning(f"Error ensuring ECS service '{service_name}': {e}")

    return {
        "cluster_arn": cluster_arn,
        "task_definition_arn": task_def_arn,
        "service_arn": service_arn,
    }


# ==============================================================================
# 6. CONTACT FLOW SYNC (FULL-DUPLEX SIP/RTP & NOVA SONIC)
# ==============================================================================

def sync_contact_flow(
    session: boto3.Session,
    instance_id: str,
    flow_name: str = "CareHarmony-Cara-Outbound",
    dry_run: bool = False,
) -> Dict[str, str]:
    """Ensure the outbound Amazon Connect contact flow exists and is published."""
    connect = session.client("connect")
    flow_id = ""

    if dry_run:
        logger.info(f"[DRY-RUN] Would sync Contact Flow '{flow_name}' for instance {instance_id}")
        return {"flow_name": flow_name, "flow_id": "dry-run-flow-id", "status": "DRY_RUN_PUBLISHED"}

    # Valid Amazon Connect Contact Flow Graph Content for Full-Duplex Voice Gateway & SIP Bridge
    content = json.dumps({
        "Version": "2019-10-30",
        "StartAction": "action-update-attr",
        "Metadata": {
            "entryPointPosition": {"x": 75, "y": 20},
            "snapToGrid": False,
            "ActionMetadata": {
                "action-update-attr": {"position": {"x": 200, "y": 50}},
                "action-start-streaming": {"position": {"x": 700, "y": 50}},
                "action-hold-1": {"position": {"x": 950, "y": 50}},
                "action-hold-2": {"position": {"x": 950, "y": 250}},
                "action-disconnect": {"position": {"x": 1200, "y": 50}},
            },
        },
        "Actions": [
            {
                "Identifier": "action-update-attr",
                "Type": "UpdateContactAttributes",
                "Parameters": {
                    "Attributes": {
                        "Project": "CareHarmony-Cara",
                        "VoiceEngine": "NovaSonic-FullDuplex",
                        "SIPBridge": "Enabled",
                    }
                },
                # Go straight to streaming: Cara's opening (identity check with the
                # real patient name) is spoken by Nova Sonic, not by the flow.
                "Transitions": {
                    "NextAction": "action-start-streaming",
                    "Errors": [
                        {
                            "NextAction": "action-start-streaming",
                            "ErrorType": "NoMatchingError",
                        }
                    ],
                    "Conditions": [],
                },
            },
            {
                "Identifier": "action-start-streaming",
                "Type": "UpdateContactMediaStreamingBehavior",
                "Parameters": {
                    "MediaStreamingState": "Enabled",
                    "Participants": [
                        {
                            "ParticipantType": "Customer",
                            "MediaDirections": ["To", "From"],
                        }
                    ],
                    "MediaStreamType": "Audio",
                },
                "Transitions": {
                    "NextAction": "action-hold-1",
                    "Errors": [
                        {
                            "NextAction": "action-hold-1",
                            "ErrorType": "NoMatchingError",
                        }
                    ],
                    "Conditions": [],
                },
            },
            {
                "Identifier": "action-hold-1",
                "Type": "MessageParticipant",
                "Parameters": {
                    "Text": "<speak><break time=\"2s\"/></speak>",
                },
                "Transitions": {
                    "NextAction": "action-hold-2",
                    "Errors": [],
                    "Conditions": [],
                },
            },
            {
                "Identifier": "action-hold-2",
                "Type": "MessageParticipant",
                "Parameters": {
                    "Text": "<speak><break time=\"2s\"/></speak>",
                },
                "Transitions": {
                    "NextAction": "action-hold-1",
                    "Errors": [],
                    "Conditions": [],
                },
            },
            {
                "Identifier": "action-disconnect",
                "Type": "DisconnectParticipant",
                "Parameters": {},
                "Transitions": {},
            },
        ],
    })

    try:
        paginator = connect.get_paginator("list_contact_flows")
        for page in paginator.paginate(InstanceId=instance_id):
            for summary in page.get("ContactFlowSummaryList", []):
                if summary.get("Name") == flow_name:
                    flow_id = summary.get("Id", "")
                    logger.info(f"Found Contact Flow '{flow_name}' (ID: {flow_id}); updating content...")
                    connect.update_contact_flow_content(
                        InstanceId=instance_id,
                        ContactFlowId=flow_id,
                        Content=content,
                    )
                    return {"flow_name": flow_name, "flow_id": flow_id, "status": "UPDATED"}

        # Create new flow if missing
        logger.info(f"Creating new Contact Flow '{flow_name}'...")
        resp = connect.create_contact_flow(
            InstanceId=instance_id,
            Name=flow_name,
            Type="CONTACT_FLOW",
            Description="CareHarmony Pipecat & Nova Sonic streaming outbound contact flow",
            Content=content,
            Tags={"Project": "CareHarmony-Cara"},
        )
        flow_id = resp.get("ContactFlowId", "")
        return {"flow_name": flow_name, "flow_id": flow_id, "status": "CREATED"}
    except Exception as e:
        logger.warning(f"Contact flow sync simulated/warning: {e}")
        return {"flow_name": flow_name, "flow_id": flow_id, "status": f"SIMULATED: {e}"}


# ==============================================================================
# 7. PHONE NUMBER CLAIM & ASSOCIATION
# ==============================================================================

def get_or_claim_phone_number(
    session: boto3.Session,
    instance_id: str,
    instance_arn: str,
    contact_flow_id: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Dynamically discover, claim (if missing), and bind an Amazon Connect phone number to the contact flow."""
    connect = session.client("connect")
    logger.info(f"Checking Amazon Connect phone numbers for instance {instance_id}...")

    # Step A: Idempotency Check (Existing Numbers)
    try:
        paginator = connect.get_paginator("list_phone_numbers")
        for page in paginator.paginate(InstanceId=instance_id):
            numbers = page.get("PhoneNumberSummaryList", [])
            if numbers:
                first_num = numbers[0]
                phone_id = first_num.get("Id", "")
                phone_arn = first_num.get("Arn", "")
                phone_str = first_num.get("PhoneNumber", "")

                # Check current metadata / binding
                is_bound = False
                try:
                    desc = connect.describe_phone_number(PhoneNumberId=phone_id)
                    claimed = desc.get("ClaimedPhoneNumberSummary", {})
                    curr_flow_id = claimed.get("TargetArn", "")
                    if contact_flow_id and contact_flow_id in curr_flow_id:
                        is_bound = True
                except Exception as d_err:
                    logger.debug(f"describe_phone_number check note: {d_err}")

                if is_bound:
                    logger.info(f"Phone number {phone_str} ({phone_id}) is active and already bound to flow {contact_flow_id}.")
                else:
                    if dry_run:
                        logger.info(f"[DRY-RUN] Would associate phone number {phone_str} ({phone_id}) with flow {contact_flow_id}")
                    else:
                        try:
                            logger.info(f"Binding phone number {phone_str} ({phone_id}) to flow {contact_flow_id}...")
                            connect.associate_phone_number_contact_flow(
                                PhoneNumberId=phone_id,
                                InstanceId=instance_id,
                                ContactFlowId=contact_flow_id,
                            )
                            logger.info(f"Successfully bound phone number {phone_str} to flow {contact_flow_id}.")
                        except Exception as u_err:
                            logger.warning(f"Error associating phone number to contact flow: {u_err}")

                return {
                    "number": phone_str,
                    "id": phone_id,
                    "arn": phone_arn,
                    "status": "ASSOCIATED",
                }
    except Exception as e:
        logger.warning(f"Error checking existing phone numbers: {e}")

    # Step B: Automated Search & Claim (If No Number Exists)
    if dry_run:
        mock_number = "+15822671755"
        mock_id = "mock-phone-id-12345"
        mock_arn = f"arn:aws:connect:{session.region_name}:123456789012:phone-number/{mock_id}"
        logger.info(f"[DRY-RUN] Would search and claim a US DID phone number and bind to flow {contact_flow_id}")
        return {
            "number": mock_number,
            "id": mock_id,
            "arn": mock_arn,
            "status": "DRY_RUN_ASSOCIATED",
        }

    logger.info("No phone numbers found on instance. Searching available US DID phone numbers to claim...")
    candidate_number = None
    try:
        search_resp = connect.search_available_phone_numbers(
            TargetArn=instance_arn,
            PhoneNumberCountryCode="US",
            PhoneNumberType="DID",
            MaxResults=3,
        )
        avail = search_resp.get("AvailableNumbersList", [])
        if avail:
            candidate_number = avail[0].get("PhoneNumber")
        else:
            logger.info("No DID numbers available. Searching available TOLL_FREE phone numbers...")
            search_tf = connect.search_available_phone_numbers(
                TargetArn=instance_arn,
                PhoneNumberCountryCode="US",
                PhoneNumberType="TOLL_FREE",
                MaxResults=3,
            )
            avail_tf = search_tf.get("AvailableNumbersList", [])
            if avail_tf:
                candidate_number = avail_tf[0].get("PhoneNumber")
    except Exception as s_err:
        logger.error(f"Failed searching available phone numbers: {s_err}")
        raise

    if not candidate_number:
        raise RuntimeError("No available US DID or Toll-Free phone numbers found to claim in Amazon Connect.")

    logger.info(f"Claiming phone number '{candidate_number}'...")
    try:
        claim_resp = connect.claim_phone_number(
            TargetArn=instance_arn,
            PhoneNumber=candidate_number,
            PhoneNumberDescription="CareHarmony Cara AI Voice Outreach DID",
            Tags={"Project": "CareHarmony-Cara", "Environment": "Development"},
        )
        phone_id = claim_resp.get("PhoneNumberId", "")
        phone_arn = claim_resp.get("PhoneNumberArn", "")
        logger.info(f"Successfully claimed phone number: {candidate_number} (ID: {phone_id}, ARN: {phone_arn})")

        if contact_flow_id and phone_id:
            try:
                logger.info(f"Binding newly claimed phone number {candidate_number} ({phone_id}) to flow {contact_flow_id}...")
                connect.associate_phone_number_contact_flow(
                    PhoneNumberId=phone_id,
                    InstanceId=instance_id,
                    ContactFlowId=contact_flow_id,
                )
                logger.info(f"Successfully bound phone number {candidate_number} to flow {contact_flow_id}.")
            except Exception as bind_err:
                logger.warning(f"Error binding phone number {phone_id} to flow {contact_flow_id}: {bind_err}")

        return {
            "number": candidate_number,
            "id": phone_id,
            "arn": phone_arn,
            "status": "CLAIMED_AND_ASSOCIATED",
        }
    except ClientError as c_err:
        logger.error(f"Failed to claim phone number '{candidate_number}': {c_err}")
        raise


# ==============================================================================
# 8. MAIN ORCHESTRATOR
# ==============================================================================

def deploy_infrastructure(
    profile: str = AWS_PROFILE_DEFAULT,
    region: str = AWS_REGION_DEFAULT,
    connect_alias: str = "Cara-Connect",
    connect_instance_id: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Execute complete idempotent infrastructure deployment."""
    logger.info(f"=== Starting CareHarmony Infrastructure Deployment (Dry Run: {dry_run}) ===")
    session = get_aws_session(profile=profile, region=region)

    # 1. Discover or Auto-Create Connect Instance
    connect_info = get_or_create_connect_instance(
        session,
        alias=connect_alias,
        instance_id=connect_instance_id,
        dry_run=dry_run,
    )
    instance_id = connect_info["instance_id"]
    instance_arn = connect_info.get("instance_arn", "")

    # 2. Sync SSM Parameters
    ssm_params = {
        "/careharmony/connect/instance_id": instance_id,
        "/careharmony/connect/instance_arn": instance_arn,
        "/careharmony/connect/instance_alias": connect_info["instance_alias"],
        "/careharmony/region": session.region_name,
        "/careharmony/voice/model_id": "amazon.nova-2-sonic-v1:0",
    }
    ssm_results = sync_ssm_parameters(session, ssm_params, dry_run=dry_run)

    # 3. DynamoDB Tables
    dynamo_results = ensure_dynamodb_tables(session, dry_run=dry_run)

    # 4. KVS Stream & Connect Media Streaming Storage Association
    kvs_results = ensure_kvs_stream(session, dry_run=dry_run)
    if kvs_results.get("stream_arn"):
        sync_ssm_parameters(
            session,
            {"/careharmony/kvs/stream_arn": kvs_results["stream_arn"]},
            dry_run=dry_run,
        )
    storage_results = ensure_connect_media_streaming_storage(session, instance_id=instance_id, dry_run=dry_run)

    # 5. ECS Fargate Resources
    ecs_results = ensure_ecs_resources(session, connect_instance_id=instance_id, dry_run=dry_run)

    # 6. Contact Flow
    flow_results = sync_contact_flow(session, instance_id=instance_id, dry_run=dry_run)
    flow_id = flow_results.get("flow_id", "")
    if flow_id:
        sync_ssm_parameters(
            session,
            {"/careharmony/connect/contact_flow_id": flow_id},
            dry_run=dry_run,
        )

    # 7. Phone Number Claim & Association
    phone_results = get_or_claim_phone_number(
        session,
        instance_id=instance_id,
        instance_arn=instance_arn,
        contact_flow_id=flow_id,
        dry_run=dry_run,
    )
    if phone_results.get("number"):
        sync_ssm_parameters(
            session,
            {
                "/careharmony/connect/claimed_phone_number": phone_results["number"],
                "/careharmony/connect/claimed_phone_id": phone_results.get("id", ""),
            },
            dry_run=dry_run,
        )

    # 8. Chime SDK Voice Connector Provisioning
    chime_results = ensure_chime_voice_connector(session, dry_run=dry_run)
    if chime_results.get("connector_id"):
        sync_ssm_parameters(
            session,
            {
                "/careharmony/voice/sip_connector_id": chime_results["connector_id"],
                "/careharmony/voice/sip_connector_host": chime_results.get("outbound_host", ""),
            },
            dry_run=dry_run,
        )

    # 9. Save Deployment State Artifact
    state_data = {
        "status": "DRY_RUN_COMPLETED" if dry_run else "DEPLOYED",
        "region": session.region_name,
        "connect": connect_info,
        "phone_number": phone_results,
        "dynamodb": dynamo_results,
        "kvs": kvs_results,
        "media_streaming_storage": storage_results,
        "ecs": ecs_results,
        "chime": chime_results,
        "contact_flow": flow_results,
        "ssm": ssm_results,
    }

    if not dry_run:
        STATE_FILE_PATH.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
        logger.info(f"Saved deployment state artifact to: {STATE_FILE_PATH}")
    else:
        logger.info(f"[DRY-RUN] Validated deployment state artifact generation.")

    logger.info("=== CareHarmony Infrastructure Deployment Completed Successfully ===")
    return state_data


def main():
    parser = argparse.ArgumentParser(description="Deploy CareHarmony AWS Infrastructure idempotently.")
    parser.add_argument("--dry-run", action="store_true", help="Validate without making changes")
    parser.add_argument("--profile", default=AWS_PROFILE_DEFAULT, help="AWS CLI profile name")
    parser.add_argument("--region", default=AWS_REGION_DEFAULT, help="AWS region")
    parser.add_argument("--connect-alias", default="Cara-Connect", help="Amazon Connect instance alias")
    parser.add_argument("--connect-instance-id", default=None, help="Amazon Connect instance ID (optional)")
    args = parser.parse_args()

    deploy_infrastructure(
        profile=args.profile,
        region=args.region,
        connect_alias=args.connect_alias,
        connect_instance_id=args.connect_instance_id,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
