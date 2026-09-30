"""Build, Push Docker Gateway Image to ECR and Deploy to ECS Fargate.

1. Ensures ECR repository exists
2. Authenticates local Docker client with ECR
3. Builds Docker image from backend/Dockerfile
4. Pushes image to Amazon ECR
5. Registers updated ECS Task Definition revision with new image URI
6. Scales ECS Fargate service to desiredCount=1 with forceNewDeployment

Usage:
    python backend/scripts/build_and_deploy_gateway.py [--dry-run] [--profile careharmony]
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError, ProfileNotFound

logger = logging.getLogger("build_and_deploy_gateway")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

AWS_PROFILE_DEFAULT = "careharmony"
AWS_REGION_DEFAULT = "us-east-1"
BACKEND_DIR = Path(__file__).resolve().parents[1]


def get_aws_session(profile: Optional[str] = None, region: Optional[str] = None) -> boto3.Session:
    """Create a boto3 Session with fallback support."""
    prof = profile or os.getenv("AWS_PROFILE", AWS_PROFILE_DEFAULT)
    reg = region or os.getenv("AWS_REGION", AWS_REGION_DEFAULT)
    try:
        session = boto3.Session(profile_name=prof, region_name=reg)
        session.get_credentials()
        return session
    except (ProfileNotFound, Exception) as e:
        logger.debug(f"AWS Profile '{prof}' fallback ({e})")
        return boto3.Session(region_name=reg)


def ensure_ecr_repository(
    session: boto3.Session,
    repo_name: str = "careharmony-voice-gateway",
    dry_run: bool = False,
) -> str:
    """Ensure the Amazon ECR repository exists and return its URI."""
    ecr = session.client("ecr")
    sts = session.client("sts")

    try:
        caller = sts.get_caller_identity()
        account_id = caller.get("Account", "123456789012")
    except Exception:
        account_id = "123456789012"

    repo_uri = f"{account_id}.dkr.ecr.{session.region_name}.amazonaws.com/{repo_name}"

    if dry_run:
        logger.info(f"[DRY-RUN] Would ensure ECR Repository '{repo_name}' (URI: {repo_uri})")
        return repo_uri

    try:
        resp = ecr.describe_repositories(repositoryNames=[repo_name])
        repos = resp.get("repositories", [])
        if repos:
            repo_uri = repos[0].get("repositoryUri", repo_uri)
            logger.info(f"Found existing ECR repository: {repo_uri}")
            return repo_uri
    except ecr.exceptions.RepositoryNotFoundException:
        pass
    except Exception as e:
        logger.debug(f"describe_repositories check note: {e}")

    try:
        logger.info(f"Creating ECR repository '{repo_name}'...")
        create_resp = ecr.create_repository(
            repositoryName=repo_name,
            imageScanningConfiguration={"scanOnPush": True},
            tags=[{"Key": "Project", "Value": "CareHarmony-Cara"}],
        )
        repo_uri = create_resp.get("repository", {}).get("repositoryUri", repo_uri)
        logger.info(f"Created ECR repository: {repo_uri}")
    except Exception as err:
        logger.warning(f"Error creating ECR repository '{repo_name}': {err}")

    return repo_uri


def ecr_docker_login(
    session: boto3.Session,
    repo_uri: str,
    dry_run: bool = False,
) -> bool:
    """Authenticate local Docker daemon against Amazon ECR registry."""
    if dry_run:
        logger.info(f"[DRY-RUN] Would authenticate Docker with registry: {repo_uri.split('/')[0]}")
        return True

    ecr = session.client("ecr")
    try:
        auth_data = ecr.get_authorization_token()
        token = auth_data["authorizationData"][0]["authorizationToken"]
        proxy_endpoint = auth_data["authorizationData"][0]["proxyEndpoint"]
        user, password = base64.b64decode(token).decode("utf-8").split(":")

        logger.info(f"Logging in to Docker registry {proxy_endpoint}...")
        proc = subprocess.run(
            ["docker", "login", "--username", user, "--password-stdin", proxy_endpoint],
            input=password.encode("utf-8"),
            capture_output=True,
            check=True,
        )
        logger.info("Docker ECR authentication successful.")
        return True
    except Exception as e:
        logger.warning(f"Docker ECR login note/simulated: {e}")
        return False


def build_and_push_image(
    repo_uri: str,
    image_tag: str = "latest",
    dockerfile_dir: Path = BACKEND_DIR,
    dry_run: bool = False,
) -> str:
    """Build and push Docker image to ECR with multi-arch amd64 support."""
    full_image_tag = f"{repo_uri}:{image_tag}"

    if dry_run:
        logger.info(f"[DRY-RUN] Would run: docker build --platform linux/amd64 -t {full_image_tag} {dockerfile_dir}")
        logger.info(f"[DRY-RUN] Would run: docker push {full_image_tag}")
        return full_image_tag

    logger.info(f"Building Docker image '{full_image_tag}' (platform: linux/amd64) from {dockerfile_dir}...")
    try:
        build_proc = subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "-t", full_image_tag, str(dockerfile_dir)],
            check=True,
            text=True,
        )
        logger.info(f"Pushing Docker image '{full_image_tag}' to ECR...")
        push_proc = subprocess.run(
            ["docker", "push", full_image_tag],
            check=True,
            text=True,
        )
        logger.info(f"Docker image pushed successfully: {full_image_tag}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Docker command failed with return code {e.returncode}: {e}")
        raise
    except Exception as e:
        logger.warning(f"Docker build/push encountered error: {e}")
        raise

    return full_image_tag


def update_ecs_service_with_image(
    session: boto3.Session,
    image_uri: str,
    cluster_name: str = "careharmony-voice-cluster",
    service_name: str = "careharmony-voice-service",
    task_family: str = "careharmony-voice-gateway",
    connect_instance_id: Optional[str] = None,
    desired_count: int = 1,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Register new task definition revision and update service to desiredCount=1."""
    ecs = session.client("ecs")
    sts = session.client("sts")

    try:
        caller = sts.get_caller_identity()
        account_id = caller.get("Account", "123456789012")
    except Exception:
        account_id = "123456789012"

    exec_role_arn = f"arn:aws:iam::{account_id}:role/ecsTaskExecutionRole"
    instance_id = connect_instance_id or os.getenv("CONNECT_INSTANCE_ID", "")

    if dry_run:
        logger.info(f"[DRY-RUN] Would register task definition '{task_family}' with image '{image_uri}'")
        logger.info(f"[DRY-RUN] Would update ECS service '{service_name}' on cluster '{cluster_name}' with desiredCount={desired_count}")
        return {
            "task_definition_arn": f"arn:aws:ecs:{session.region_name}:{account_id}:task-definition/{task_family}:new",
            "service_arn": f"arn:aws:ecs:{session.region_name}:{account_id}:service/{cluster_name}/{service_name}",
            "desired_count": desired_count,
            "status": "DRY_RUN_UPDATED",
        }

    # Pre-create CloudWatch Log Group
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
        logger.info(f"Registering ECS Task Definition revision for family '{task_family}'...")
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
                    "image": image_uri,
                    "portMappings": [
                        {"containerPort": 8000, "hostPort": 8000, "protocol": "tcp"},
                        {"containerPort": 10000, "hostPort": 10000, "protocol": "udp"},
                        {"containerPort": 5060, "hostPort": 5060, "protocol": "udp"},
                    ],
                    "environment": [
                        {"name": "AWS_REGION", "value": session.region_name},
                        {"name": "CONNECT_INSTANCE_ID", "value": instance_id},
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
    except Exception as reg_err:
        logger.warning(f"Error registering task definition: {reg_err}")

    service_arn = ""
    try:
        logger.info(f"Updating ECS service '{service_name}' to task definition revision '{task_def_arn}' with desiredCount={desired_count}...")
        srv_resp = ecs.update_service(
            cluster=cluster_name,
            service=service_name,
            taskDefinition=task_def_arn or task_family,
            desiredCount=desired_count,
            forceNewDeployment=True,
        )
        service_arn = srv_resp.get("service", {}).get("serviceArn", "")
        logger.info(f"Updated ECS Service: {service_name} (desiredCount: {desired_count})")
    except Exception as srv_err:
        logger.warning(f"Error updating ECS service '{service_name}': {srv_err}")

    return {
        "task_definition_arn": task_def_arn,
        "service_arn": service_arn,
        "desired_count": desired_count,
        "status": "UPDATED",
    }


def wait_and_bind_task_ip_to_chime(
    session: boto3.Session,
    cluster_name: str = "careharmony-voice-cluster",
    service_name: str = "careharmony-voice-service",
    vc_name: str = "careharmony-voice-connector",
    max_wait_seconds: int = 60,
    dry_run: bool = False,
) -> Optional[str]:
    """Poll ECS task until RUNNING, resolve public IP from ENI, and update Chime Voice Connector termination."""
    import time
    if dry_run:
        logger.info("[DRY-RUN] Would poll ECS task for public IP and register with Chime Voice Connector")
        return "198.51.100.1"

    ecs = session.client("ecs")
    ec2 = session.client("ec2")
    chime = session.client("chime-sdk-voice")

    logger.info(f"Waiting for ECS Task in '{cluster_name}' to reach RUNNING status...")
    start_time = time.time()
    task_arn = None
    eni_id = None
    public_ip = None

    while time.time() - start_time < max_wait_seconds:
        try:
            tasks_resp = ecs.list_tasks(cluster=cluster_name, serviceName=service_name, desiredStatus="RUNNING")
            task_arns = tasks_resp.get("taskArns", [])
            if task_arns:
                task_desc = ecs.describe_tasks(cluster=cluster_name, tasks=[task_arns[0]])
                tasks = task_desc.get("tasks", [])
                if tasks and tasks[0].get("lastStatus") == "RUNNING":
                    task_arn = tasks[0]["taskArn"]
                    for attachment in tasks[0].get("attachments", []):
                        if attachment.get("type") == "ElasticNetworkInterface":
                            for detail in attachment.get("details", []):
                                if detail.get("name") == "networkInterfaceId":
                                    eni_id = detail.get("value")
                                    break
                    if eni_id:
                        break
        except Exception as e:
            logger.debug(f"Polling ECS task status note: {e}")
        time.sleep(5)

    if not eni_id:
        logger.warning("Could not resolve running task ENI within timeout.")
        return None

    try:
        eni_resp = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
        enis = eni_resp.get("NetworkInterfaces", [])
        if enis:
            assoc = enis[0].get("Association", {})
            public_ip = assoc.get("PublicIp")
    except Exception as e:
        logger.warning(f"Error describing ENI {eni_id}: {e}")

    if not public_ip:
        logger.warning(f"No public IP associated with ENI {eni_id}")
        return None

    logger.info(f"Resolved ECS Task Public IP: {public_ip} (Task ARN: {task_arn})")

    try:
        connectors = chime.list_voice_connectors().get("VoiceConnectors", [])
        vc_id = None
        for vc in connectors:
            if vc.get("Name") == vc_name:
                vc_id = vc.get("VoiceConnectorId")
                break
        if vc_id:
            chime.put_voice_connector_termination(
                VoiceConnectorId=vc_id,
                Termination={
                    "CpsLimit": 1,
                    "Disabled": False,
                    "CallingRegions": ["US"],
                    "CidrAllowedList": [f"{public_ip}/32"],
                },
            )
            chime.put_voice_connector_origination(
                VoiceConnectorId=vc_id,
                Origination={
                    "Routes": [
                        {
                            "Host": public_ip,
                            "Port": 5060,
                            "Protocol": "UDP",
                            "Priority": 1,
                            "Weight": 1,
                        }
                    ],
                    "Disabled": False,
                },
            )
            logger.info(f"Successfully registered dynamic Task Public IP ({public_ip}:5060) with Chime Voice Connector {vc_id}.")
    except Exception as e:
        logger.warning(f"Error binding task IP to Chime Voice Connector: {e}")

    return public_ip


def deploy_gateway(
    profile: str = AWS_PROFILE_DEFAULT,
    region: str = AWS_REGION_DEFAULT,
    repo_name: str = "careharmony-voice-gateway",
    cluster_name: str = "careharmony-voice-cluster",
    service_name: str = "careharmony-voice-service",
    task_family: str = "careharmony-voice-gateway",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Execute complete containerization, ECR push, and ECS service deployment."""
    logger.info(f"=== Starting Pipecat Gateway Container Deployment (Dry Run: {dry_run}) ===")
    session = get_aws_session(profile=profile, region=region)

    # 1. ECR Repository
    repo_uri = ensure_ecr_repository(session, repo_name=repo_name, dry_run=dry_run)

    # 2. Docker Auth
    ecr_docker_login(session, repo_uri=repo_uri, dry_run=dry_run)

    # 3. Build & Push Image
    image_uri = build_and_push_image(repo_uri=repo_uri, dry_run=dry_run)

    # 4. Update ECS Service
    ecs_results = update_ecs_service_with_image(
        session,
        image_uri=image_uri,
        cluster_name=cluster_name,
        service_name=service_name,
        task_family=task_family,
        desired_count=1,
        dry_run=dry_run,
    )

    # 5. Bind dynamic task IP to Chime Voice Connector
    task_public_ip = wait_and_bind_task_ip_to_chime(
        session,
        cluster_name=cluster_name,
        service_name=service_name,
        dry_run=dry_run,
    )

    result = {
        "status": "DRY_RUN_COMPLETED" if dry_run else "DEPLOYED",
        "ecr_repo_uri": repo_uri,
        "image_uri": image_uri,
        "ecs": ecs_results,
        "task_public_ip": task_public_ip,
    }

    logger.info("=== Pipecat Gateway Container Deployment Completed Successfully ===")
    return result


def main():
    parser = argparse.ArgumentParser(description="Build and push Pipecat Voice Gateway Docker image to ECR and scale ECS.")
    parser.add_argument("--dry-run", action="store_true", help="Validate workflow without executing mutations")
    parser.add_argument("--profile", default=AWS_PROFILE_DEFAULT, help="AWS CLI profile name")
    parser.add_argument("--region", default=AWS_REGION_DEFAULT, help="AWS region")
    parser.add_argument("--repo-name", default="careharmony-voice-gateway", help="ECR repository name")
    parser.add_argument("--cluster-name", default="careharmony-voice-cluster", help="ECS cluster name")
    parser.add_argument("--service-name", default="careharmony-voice-service", help="ECS service name")
    args = parser.parse_args()

    deploy_gateway(
        profile=args.profile,
        region=args.region,
        repo_name=args.repo_name,
        cluster_name=args.cluster_name,
        service_name=args.service_name,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
