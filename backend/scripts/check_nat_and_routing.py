import boto3

session = boto3.Session(profile_name="careharmony", region_name="us-east-1")
ec2 = session.client("ec2")
ecs = session.client("ecs")
chime_sdk = session.client("chime-sdk-voice")

print("=== 1. Route Tables ===")
rts = ec2.describe_route_tables().get("RouteTables", [])
for rt in rts:
    print(f"RouteTable: {rt['RouteTableId']} (VPC: {rt['VpcId']})")
    for r in rt.get("Routes", []):
        gw = r.get("GatewayId") or r.get("NatGatewayId") or "None"
        print(f"  Route: {r.get('DestinationCidrBlock')} -> {gw}")

print("\n=== 2. NAT Gateways ===")
nats = ec2.describe_nat_gateways().get("NatGateways", [])
print(f"Found {len(nats)} NAT Gateways:")
for n in nats:
    eips = [a.get("PublicIp") for a in n.get("NatGatewayAddresses", [])]
    print(f"  NAT: {n['NatGatewayId']}, State: {n['State']}, VPC: {n['VpcId']}, Subnet: {n['SubnetId']}, EIPs: {eips}")

print("\n=== 3. Elastic IPs ===")
eips = ec2.describe_addresses().get("Addresses", [])
for e in eips:
    print(f"  EIP: {e.get('PublicIp')}, AllocId: {e.get('AllocationId')}, AssocId: {e.get('AssociationId')}")

print("\n=== 4. ECS Task Network Configuration ===")
services = ecs.describe_services(cluster="careharmony-voice-cluster", services=["careharmony-voice-service"]).get("services", [])
if services:
    nc = services[0].get("networkConfiguration", {}).get("awsvpcConfiguration", {})
    print("Service Subnets:", nc.get("subnets"))
    print("Security Groups:", nc.get("securityGroups"))
    print("Assign Public IP:", nc.get("assignPublicIp"))
