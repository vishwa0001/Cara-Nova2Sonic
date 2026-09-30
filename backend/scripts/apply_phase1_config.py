import boto3
import json

session = boto3.Session(profile_name="careharmony", region_name="us-east-1")
ec2 = session.client("ec2")
chime_sdk = session.client("chime-sdk-voice")
elbv2 = session.client("elbv2")

sg_id = "sg-0d0265556114020b0"
vc_id = "cafawc0zxzbcpxusmsonrd"
static_eip = "100.49.33.242"
eip_alloc_id = "eipalloc-0653d24d77b8e9b82"

# Official AWS Chime Voice Connector CIDRs for us-east-1 and us-west-2
# Signaling: 3.80.16.0/23, 99.77.254.0/24, 99.77.253.0/24, 99.77.232.0/24
# Media: 99.77.240.0/20, 99.77.128.0/18
CHIME_CIDRS = [
    "3.80.16.0/23",
    "99.77.254.0/24",
    "99.77.253.0/24",
    "99.77.232.0/24",
    "99.77.240.0/20",
    "99.77.128.0/18",
]

print("=== 1. Updating Security Group Ingress Rules for Chime Voice Connector ===")
for cidr in CHIME_CIDRS:
    # 1. UDP 5060 (SIP Signaling)
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "udp",
                    "FromPort": 5060,
                    "ToPort": 5060,
                    "IpRanges": [{"CidrIp": cidr, "Description": f"Chime VC SIP 5060 ({cidr})"}],
                }
            ],
        )
        print(f"Added Ingress rule: UDP 5060 from {cidr}")
    except Exception as e:
        if "InvalidPermission.Duplicate" in str(e):
            print(f"Rule already exists: UDP 5060 from {cidr}")
        else:
            print(f"Error adding UDP 5060 from {cidr}: {e}")

    # 2. UDP 10000-20000 (RTP Audio Media)
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "udp",
                    "FromPort": 10000,
                    "ToPort": 20000,
                    "IpRanges": [{"CidrIp": cidr, "Description": f"Chime VC RTP 10000-20000 ({cidr})"}],
                }
            ],
        )
        print(f"Added Ingress rule: UDP 10000-20000 from {cidr}")
    except Exception as e:
        if "InvalidPermission.Duplicate" in str(e):
            print(f"Rule already exists: UDP 10000-20000 from {cidr}")
        else:
            print(f"Error adding UDP 10000-20000 from {cidr}: {e}")

# Also allow VPC internal traffic for NLB health checks and forwarding
try:
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "-1",
                "IpRanges": [{"CidrIp": "172.31.0.0/16", "Description": "VPC Subnets / NLB"}],
            }
        ],
    )
    print("Added Ingress rule: All traffic from VPC 172.31.0.0/16")
except Exception as e:
    if "InvalidPermission.Duplicate" in str(e):
        print("Rule already exists: VPC 172.31.0.0/16")
    else:
        print("Error adding VPC 172.31.0.0/16 rule:", e)

print("\n=== 2. Updating Chime Voice Connector Origination & Termination with Static EIP ===")
try:
    # Origination Route -> Point to Static EIP: 5060
    chime_sdk.put_voice_connector_origination(
        VoiceConnectorId=vc_id,
        Origination={
            "Routes": [
                {
                    "Host": static_eip,
                    "Port": 5060,
                    "Protocol": "UDP",
                    "Priority": 1,
                    "Weight": 1,
                }
            ],
            "Disabled": False,
        },
    )
    print(f"Updated Voice Connector Origination route to {static_eip}:5060 UDP")

    # Termination Allowed CIDR -> Include Static EIP /32
    term = chime_sdk.get_voice_connector_termination(VoiceConnectorId=vc_id)["Termination"]
    term["CidrAllowedList"] = [f"{static_eip}/32"]
    chime_sdk.put_voice_connector_termination(
        VoiceConnectorId=vc_id,
        Termination=term,
    )
    print(f"Updated Voice Connector Termination allowed CIDR to {static_eip}/32")
except Exception as e:
    print("Error updating Voice Connector routes/termination:", e)
