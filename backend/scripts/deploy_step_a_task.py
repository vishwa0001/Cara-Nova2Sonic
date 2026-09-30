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
            {"name": "RTP_ECHO_MODE", "value": "true"},
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
print("Updated ECS service to new deployment.")
