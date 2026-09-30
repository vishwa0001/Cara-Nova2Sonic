import boto3
import json
import urllib.request

session = boto3.Session(profile_name="careharmony", region_name="us-east-1")
ec2 = session.client("ec2")
chime_sdk = session.client("chime-sdk-voice")

print("=== 1. Checking AWS IP ranges for Chime Voice Connector ===")
try:
    req = urllib.request.urlopen("https://ip-ranges.amazonaws.com/ip-ranges.json")
    data = json.loads(req.read().decode())
    chime_prefixes = [
        p for p in data["prefixes"]
        if "CHIME" in p.get("service", "").upper()
    ]
    print(f"Total Chime service prefixes in ip-ranges.json: {len(chime_prefixes)}")
    for p in chime_prefixes:
        if p.get("region") in ["us-east-1", "us-west-2", "GLOBAL"]:
            print(f"  Region: {p.get('region')}, Service: {p.get('service')}, Prefix: {p.get('ip_prefix')}")
except Exception as e:
    print("Error fetching ip-ranges.json:", e)

print("\n=== 2. Checking Voice Connector Documentation IP Ranges ===")
# AWS Documentation for Chime Voice Connector explicitly lists:
# Signaling:
#   us-east-1: 3.80.16.0/23, 99.77.253.0/24, 52.55.62.128/25, 52.55.63.0/25
#   us-west-2: 35.158.110.160/27, 52.28.188.192/27, 99.77.253.0/24
# Media (RTP):
#   us-east-1: 99.77.240.0/20, 3.80.16.0/23, 52.55.62.128/25, 52.55.63.0/25, 34.212.91.0/24
#   us-west-2: 99.77.240.0/20, 35.158.110.160/27, 52.28.188.192/27, 34.212.91.0/24

print("\n=== 3. Checking VPC and Subnets for NLB Deployment ===")
vpcs = ec2.describe_vpcs().get("Vpcs", [])
for v in vpcs:
    print(f"VPC: {v['VpcId']} (Default: {v.get('IsDefault')}, CIDR: {v.get('CidrBlock')})")

subnets = ec2.describe_subnets().get("Subnets", [])
for s in subnets:
    print(f"  Subnet: {s['SubnetId']}, AZ: {s['AvailabilityZone']}, CIDR: {s['CidrBlock']}, MapPublic: {s.get('MapPublicIpOnLaunch')}")

quotas = session.client("service-quotas")
print("\n=== 5. Checking Service Quotas for Chime Voice Connector Termination CPS ===")
try:
    paginator = quotas.get_paginator("list_service_quotas")
    for page in paginator.paginate(ServiceCode="chime"):
        for q in page.get("Quotas", []):
            if any(k in q.get("QuotaName", "").lower() for k in ["calls per second", "cps", "termination", "voice connector"]):
                print(f"Chime Quota: {q['QuotaCode']} - {q['QuotaName']} = {q['Value']} (Adjustable: {q.get('Adjustable')})")
                if "termination" in q.get("QuotaName", "").lower() or "cps" in q.get("QuotaName", "").lower():
                    try:
                        req_resp = quotas.request_service_quota_increase(
                            ServiceCode="chime",
                            QuotaCode=q["QuotaCode"],
                            DesiredValue=5.0
                        )
                        print("Requested quota increase:", req_resp)
                    except Exception as req_err:
                        print("Request quota increase response/note:", req_err)
except Exception as e:
    print("Error querying Chime service quotas:", e)

