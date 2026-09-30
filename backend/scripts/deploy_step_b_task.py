import boto3
import json

session = boto3.Session(profile_name="careharmony", region_name="us-east-1")
ecs = session.client("ecs")

container_def = [
    {
        "name": "pipecat-voice-gateway",
        "image": "701348334422.dkr.ecr.us-east-1.amazonaws.com/careharmony-voice-gateway:latest",
        "essential": True,
        "portMappings": [
            {"containerPort": 8000, "hostPort": 8000, "protocol": "tcp"},
            {"containerPort": 10000, "hostPort": 10000, "protocol": "udp"},
            {"containerPort": 5060, "hostPort": 5060, "protocol": "udp"},
        ],
        "environment": [
            {"name": "AWS_REGION", "value": "us-east-1"},
            {"name": "NOVA_SONIC_MODEL_ID", "value": "amazon.nova-2-sonic-v1:0"},
            {"name": "RTP_PORT", "value": "10000"},
            {"name": "RTP_ECHO_MODE", "value": "false"},
            {"name": "PUBLIC_IP", "value": "auto"},
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": "/ecs/careharmony-voice-gateway",
                "awslogs-create-group": "true",
                "awslogs-region": "us-east-1",
                "awslogs-stream-prefix": "gateway",
            },
        },
    }
]

reg_resp = ecs.register_task_definition(
    family="careharmony-voice-gateway",
    taskRoleArn="arn:aws:iam::701348334422:role/ecsTaskExecutionRole",
    executionRoleArn="arn:aws:iam::701348334422:role/ecsTaskExecutionRole",
    networkMode="awsvpc",
    cpu="512",
    memory="1024",
    requiresCompatibilities=["FARGATE"],
    containerDefinitions=container_def,
)

task_def_arn = reg_resp["taskDefinition"]["taskDefinitionArn"]
print(f"Registered Task Definition: {task_def_arn}")

update_resp = ecs.update_service(
    cluster="careharmony-voice-cluster",
    service="careharmony-voice-service",
    taskDefinition=task_def_arn,
    forceNewDeployment=True,
)
print("Updated ECS service to Step (b) deployment with RTP_ECHO_MODE=false.")

# Discover new task public IP and register with Chime Voice Connector
import time
print("Waiting for new ECS task to start and obtain public IP...")
ec2 = session.client("ec2")
chime = session.client("chime-sdk-voice")

public_ip = None
for _ in range(18):
    time.sleep(5)
    tasks_resp = ecs.list_tasks(cluster="careharmony-voice-cluster", serviceName="careharmony-voice-service", desiredStatus="RUNNING")
    arns = tasks_resp.get("taskArns", [])
    if not arns:
        continue
    described = ecs.describe_tasks(cluster="careharmony-voice-cluster", tasks=arns).get("tasks", [])
    for t in described:
        if t.get("taskDefinitionArn") == task_def_arn and t.get("lastStatus") == "RUNNING":
            for a in t.get("attachments", []):
                for d in a.get("details", []):
                    if d.get("name") == "networkInterfaceId":
                        eni_id = d["value"]
                        eni_resp = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
                        public_ip = eni_resp["NetworkInterfaces"][0].get("Association", {}).get("PublicIp")
                        break
        if public_ip:
            break
    if public_ip:
        break

if public_ip:
    print(f"Discovered new ECS Task Public IP: {public_ip}")
    vc_name = "careharmony-voice-connector"
    connectors = chime.list_voice_connectors().get("VoiceConnectors", [])
    vc_id = None
    for vc in connectors:
        if vc.get("Name") == vc_name:
            vc_id = vc.get("VoiceConnectorId")
            break
    if vc_id:
        term = chime.get_voice_connector_termination(VoiceConnectorId=vc_id).get("Termination", {})
        cidrs = term.get("CidrAllowedList", [])
        if f"{public_ip}/32" not in cidrs:
            cidrs.append(f"{public_ip}/32")
        if len(cidrs) > 10:
            cidrs = cidrs[-10:]
        chime.put_voice_connector_termination(
            VoiceConnectorId=vc_id,
            Termination={
                "CpsLimit": term.get("CpsLimit", 1),
                "DefaultPhoneNumber": term.get("DefaultPhoneNumber", "+12819568434"),
                "CallingRegions": term.get("CallingRegions", ["US"]),
                "CidrAllowedList": cidrs,
                "Disabled": False,
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
        print(f"Successfully registered ECS Task Public IP ({public_ip}:5060) with Chime Voice Connector {vc_id}.")
else:
    print("Warning: Could not discover new task public IP within 90s.")

