"""Trigger Automated Outbound Test Call via Amazon Connect and CareHarmony Voice Gateway.

Initiates an outbound call using Amazon Connect's start_outbound_voice_contact API.
Connects the recipient phone number to the CareHarmony-Cara Outbound Contact Flow,
which streams audio via KVS to the Pipecat Bedrock Nova 2 Sonic gateway.

Usage:
    python backend/scripts/trigger_outbound_call.py --destination-phone "+15551234567" [--call-mode MODE_2_NORMAL_COACHING] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import ClientError, ProfileNotFound

# Setup logger
logger = logging.getLogger("trigger_outbound_call")
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
        session.get_credentials()
        return session
    except (ProfileNotFound, Exception) as e:
        logger.debug(f"AWS Profile '{prof}' fallback to default/env ({e})")
        return boto3.Session(region_name=reg)


def validate_e164_phone(phone: str) -> bool:
    """Validate that a phone number adheres to E.164 international format (e.g., +15551234567)."""
    if not phone or not isinstance(phone, str):
        return False
    # Standard E.164: + followed by 7 to 15 digits
    pattern = r"^\+[1-9]\d{6,14}$"
    return bool(re.match(pattern, phone.strip()))


def resolve_active_gateway_url(session: boto3.Session, override_url: Optional[str] = None) -> str:
    """Resolve active voice gateway URL from CLI override, environment, active ECS task, or local host."""
    import urllib.request

    if override_url:
        return override_url.rstrip("/")

    env_url = os.getenv("GATEWAY_URL")
    if env_url:
        return env_url.rstrip("/")

    # Discover ECS task public IP
    try:
        ecs = session.client("ecs")
        ec2 = session.client("ec2")
        tasks_resp = ecs.list_tasks(cluster="careharmony-voice-cluster", serviceName="careharmony-voice-service", desiredStatus="RUNNING")
        task_arns = tasks_resp.get("taskArns", [])
        if not task_arns:
            tasks_resp = ecs.list_tasks(cluster="careharmony-voice-cluster", desiredStatus="RUNNING")
            task_arns = tasks_resp.get("taskArns", [])

        if task_arns:
            desc = ecs.describe_tasks(cluster="careharmony-voice-cluster", tasks=task_arns)
            tasks = desc.get("tasks", [])
            # Sort tasks by createdAt / startedAt descending (most recent first)
            tasks.sort(key=lambda t: t.get("createdAt") or t.get("startedAt"), reverse=True)

            eni_ids = []
            for task in tasks:
                for attachment in task.get("attachments", []):
                    if attachment.get("type") == "ElasticNetworkInterface":
                        for detail in attachment.get("details", []):
                            if detail.get("name") == "networkInterfaceId":
                                eni_ids.append(detail.get("value"))

            if eni_ids:
                eni_desc = ec2.describe_network_interfaces(NetworkInterfaceIds=eni_ids)
                for eni in eni_desc.get("NetworkInterfaces", []):
                    pub_ip = eni.get("Association", {}).get("PublicIp")
                    if pub_ip:
                        candidate_url = f"http://{pub_ip}:8000"
                        try:
                            with urllib.request.urlopen(f"{candidate_url}/health", timeout=2) as resp:
                                if resp.status == 200:
                                    logger.info(f"Discovered active healthy ECS Voice Gateway: {candidate_url}")
                                    return candidate_url
                        except Exception:
                            logger.debug(f"Candidate gateway {candidate_url} did not respond to /health probe.")
    except Exception as e:
        logger.debug(f"ECS task gateway auto-discovery note: {e}")

    # Check if local gateway is running
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=1) as resp:
            if resp.status == 200:
                return "http://127.0.0.1:8000"
    except Exception:
        pass

    return "http://127.0.0.1:8000"


def load_deployment_configuration(
    session: boto3.Session,
    instance_id_override: Optional[str] = None,
    contact_flow_id_override: Optional[str] = None,
    source_phone_override: Optional[str] = None,
) -> Dict[str, str]:
    """Load Amazon Connect instance, contact flow, and source DID configuration from state file or SSM."""
    config: Dict[str, str] = {}

    # 1. Attempt reading from backend/deployment_state.json
    if STATE_FILE_PATH.exists():
        try:
            state_data = json.loads(STATE_FILE_PATH.read_text(encoding="utf-8"))
            connect_data = state_data.get("connect", {})
            phone_data = state_data.get("phone_number", {})
            flow_data = state_data.get("contact_flow", {})

            if connect_data.get("instance_id"):
                config["instance_id"] = connect_data["instance_id"]
            if phone_data.get("number"):
                config["source_phone"] = phone_data["number"]
            chime_data = state_data.get("chime", {})
            if chime_data.get("phone_number"):
                config["chime_phone"] = chime_data["phone_number"]
            if flow_data.get("flow_id"):
                config["contact_flow_id"] = flow_data["flow_id"]
            logger.info(f"Loaded configuration from state file: {STATE_FILE_PATH.name}")
        except Exception as e:
            logger.warning(f"Could not load state file {STATE_FILE_PATH}: {e}")

    # 2. Fallback to SSM Parameter Store for missing parameters
    ssm = session.client("ssm")
    param_keys = {
        "instance_id": "/careharmony/connect/instance_id",
        "source_phone": "/careharmony/connect/claimed_phone_number",
        "contact_flow_id": "/careharmony/connect/contact_flow_id",
    }
    for key, param_name in param_keys.items():
        if key not in config or not config[key]:
            try:
                resp = ssm.get_parameter(Name=param_name)
                val = resp.get("Parameter", {}).get("Value")
                if val:
                    config[key] = val
                    logger.info(f"Retrieved {key} from SSM parameter {param_name}")
            except Exception as e:
                logger.debug(f"SSM parameter {param_name} lookup skipped: {e}")

    # 3. Apply CLI overrides if provided
    if instance_id_override:
        config["instance_id"] = instance_id_override
    if contact_flow_id_override:
        config["contact_flow_id"] = contact_flow_id_override
    if source_phone_override:
        config["source_phone"] = source_phone_override

    return config


def trigger_outbound_call(
    destination_phone: str,
    patient_first_name: str = "",
    patient_last_name: str = "",
    practice_name: str = "Metro Health Clinic",
    call_mode: str = "MODE_2_NORMAL_COACHING",
    patient_id: str = "P-TEST-001",
    instance_id: Optional[str] = None,
    contact_flow_id: Optional[str] = None,
    source_phone: Optional[str] = None,
    gateway_url: Optional[str] = None,
    profile: Optional[str] = AWS_PROFILE_DEFAULT,
    region: Optional[str] = AWS_REGION_DEFAULT,
    enable_amd: bool = False,
    transport: str = "sip",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Place an automated outbound campaign test call via Chime SIP Trunk or Amazon Connect.

    Args:
        destination_phone: Recipient phone number in E.164 format (+1xxxxxxxxxx).
        patient_first_name: Patient first name attribute.
        patient_last_name: Patient last name attribute.
        practice_name: Care practice name attribute.
        call_mode: Outreach mode ('MODE_1_DIRECT_HANDOFF' or 'MODE_2_NORMAL_COACHING').
        patient_id: Identifier for the patient outreach record.
        transport: 'sip' (Chime Voice Connector Full-Duplex SIP Trunk) or 'connect' (Amazon Connect).
        dry_run: If True, validate parameters and output payload without placing call.
    """
    session = get_aws_session(profile=profile, region=region)
    cleaned_dest = destination_phone.strip()
    if not validate_e164_phone(cleaned_dest):
        raise ValueError(
            f"Invalid destination phone number '{destination_phone}'. "
            "Must be in international E.164 format (e.g., '+15551234567')."
        )

    # Validate call mode
    valid_modes = {"MODE_1_DIRECT_HANDOFF", "MODE_2_NORMAL_COACHING"}
    if call_mode not in valid_modes:
        raise ValueError(f"Invalid call mode '{call_mode}'. Must be one of: {', '.join(sorted(valid_modes))}")

    # Load deployment configuration
    config = load_deployment_configuration(
        session,
        instance_id_override=instance_id,
        contact_flow_id_override=contact_flow_id,
        source_phone_override=source_phone,
    )
    if transport.lower() == "sip":
        active_source_phone = source_phone or config.get("chime_phone", "+12819568434")
    else:
        active_source_phone = source_phone or config.get("source_phone", "+17134811560")

    logger.info("=== Outbound Call Configuration ===")
    logger.info(f"Transport          : {transport.upper()} (Chime Voice Connector SIP Trunk)")
    logger.info(f"Target Destination : {cleaned_dest}")
    logger.info(f"Caller DID (Source): {active_source_phone}")
    logger.info(f"Patient Name       : {patient_first_name} {patient_last_name} (ID: {patient_id})")
    logger.info(f"Practice Name      : {practice_name}")
    logger.info(f"Call Mode          : {call_mode}")

    if transport.lower() == "sip":
        vc_host = "cafawc0zxzbcpxusmsonrd.voiceconnector.chime.aws"
        call_id = f"sip-call-{os.urandom(6).hex()}"
        logger.info(f"[SIP_TRUNK] Originating SIP call via Chime Voice Connector ({vc_host})...")
        logger.info(f"[SIP_TRUNK] Caller ID / P-Asserted-Identity: {active_source_phone}")

        if dry_run:
            logger.info("[DRY-RUN] Validated all SIP parameters. Skipping live network call.")
            return {"status": "DRY_RUN_SUCCESS", "transport": "sip", "call_id": call_id}

        import urllib.request
        resolved_gateway_url = resolve_active_gateway_url(session, override_url=gateway_url)
        try:
            req_data = json.dumps({
                "destination_phone": cleaned_dest,
                "source_phone": active_source_phone,
                "vc_host": vc_host,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{resolved_gateway_url}/calls/originate",
                data=req_data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                call_id = resp_json.get("call_id", call_id)
                logger.info(f"[SIP_GATEWAY] Call initiated successfully via gateway ({resolved_gateway_url}): {resp_json}")
        except Exception as e:
            logger.error(f"Failed to connect to voice gateway on {resolved_gateway_url}: {e}")
            raise RuntimeError(f"Could not originate SIP call via voice gateway ({resolved_gateway_url}): {e}") from e

        print("\n" + "=" * 70)
        print("  CAREHARMONY CHIME SIP TRUNK OUTBOUND CALL INITIATED")
        print("=" * 70)
        print(f"  Call ID      : {call_id}")
        print(f"  Destination  : {cleaned_dest}")
        print(f"  Caller DID   : {active_source_phone}")
        print(f"  Voice Trunk  : {vc_host}")
        print(f"  Engine       : Amazon Bedrock Nova 2 Sonic (Speech-to-Speech)")
        print("-" * 70)
        print("  INSTRUCTIONS FOR LIVE TEST:")
        print("  1. Answer the incoming call on your phone.")
        print(f"  2. Cara will greet you directly on behalf of '{practice_name}'.")
        print("  3. Converse freely with natural interruptions (barge-in < 50ms).")
        print("=" * 70 + "\n")

        return {
            "status": "SUCCESS",
            "transport": "sip",
            "call_id": call_id,
            "destination": cleaned_dest,
            "caller_id": active_source_phone,
        }

    # Fallback to Amazon Connect API
    active_instance_id = config.get("instance_id")
    active_contact_flow_id = config.get("contact_flow_id")
    active_source_phone = config.get("source_phone")

    if not active_instance_id:
        raise ValueError("Amazon Connect Instance ID could not be resolved from state file or SSM.")
    if not active_contact_flow_id:
        raise ValueError("Amazon Connect Contact Flow ID could not be resolved from state file or SSM.")
    if not active_source_phone:
        raise ValueError("Amazon Connect Source DID (Phone Number) could not be resolved from state file or SSM.")

    connect = session.client("connect")
    call_params: Dict[str, Any] = {
        "DestinationPhoneNumber": cleaned_dest,
        "ContactFlowId": active_contact_flow_id,
        "InstanceId": active_instance_id,
        "SourcePhoneNumber": active_source_phone,
        "Attributes": {
            "FirstName": patient_first_name,
            "LastName": patient_last_name,
            "PracticeName": practice_name,
            "CallMode": call_mode,
            "PatientId": patient_id,
        },
    }

    if enable_amd:
        call_params["AnswerMachineDetectionConfig"] = {
            "EnableAnswerMachineDetection": True,
            "AwaitAnswerMachinePrompt": True,
        }
        call_params["TrafficType"] = "CAMPAIGN"

    if dry_run:
        logger.info(f"[DRY-RUN] Planned Connect Payload:\n{json.dumps(call_params, indent=2)}")
        return {"status": "DRY_RUN_SUCCESS", "contact_id": "dry-run-123", "params": call_params}

    try:
        response = connect.start_outbound_voice_contact(**call_params)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        error_msg = e.response.get("Error", {}).get("Message", str(e))
        if "TrafficType CAMPAIGN is required" in error_msg or ("TrafficType" in error_msg and "CAMPAIGN" in error_msg):
            logger.warning(
                f"Caught {error_code} ({error_msg}). "
                "Stripping AnswerMachineDetectionConfig & TrafficType and retrying as general outbound call..."
            )
            call_params.pop("AnswerMachineDetectionConfig", None)
            call_params.pop("TrafficType", None)
            response = connect.start_outbound_voice_contact(**call_params)
        else:
            logger.error(f"Failed to start outbound voice contact: {e}")
            raise
    contact_id = response.get("ContactId", "")
    return {"status": "SUCCESS", "contact_id": contact_id, "params": call_params}


def main():
    parser = argparse.ArgumentParser(
        description="Trigger an automated outbound campaign test call via Amazon Connect and Cara Gateway."
    )
    parser.add_argument(
        "--destination-phone",
        required=True,
        help="Target phone number in E.164 format (+1xxxxxxxxxx)",
    )
    parser.add_argument(
        "--patient-first-name",
        required=True,
        help="Patient first name",
    )
    parser.add_argument(
        "--patient-last-name",
        required=True,
        help="Patient last name",
    )
    parser.add_argument(
        "--practice-name",
        default="Metro Health Clinic",
        help="Practice name (default: Metro Health Clinic)",
    )
    parser.add_argument(
        "--call-mode",
        default="MODE_2_NORMAL_COACHING",
        choices=["MODE_1_DIRECT_HANDOFF", "MODE_2_NORMAL_COACHING"],
        help="Outreach mode (default: MODE_2_NORMAL_COACHING)",
    )
    parser.add_argument(
        "--patient-id",
        default="P-TEST-001",
        help="Patient ID (default: P-TEST-001)",
    )
    parser.add_argument(
        "--instance-id",
        default=None,
        help="Optional Amazon Connect Instance ID override",
    )
    parser.add_argument(
        "--contact-flow-id",
        default=None,
        help="Optional Amazon Connect Contact Flow ID override",
    )
    parser.add_argument(
        "--source-phone",
        default=None,
        help="Optional source caller DID override",
    )
    parser.add_argument(
        "--gateway-url",
        default=None,
        help="Optional Voice Gateway HTTP base URL override (e.g. http://100.55.93.215:8000)",
    )
    parser.add_argument(
        "--profile",
        default=AWS_PROFILE_DEFAULT,
        help=f"AWS CLI profile name (default: {AWS_PROFILE_DEFAULT})",
    )
    parser.add_argument(
        "--region",
        default=AWS_REGION_DEFAULT,
        help=f"AWS Region (default: {AWS_REGION_DEFAULT})",
    )
    parser.add_argument(
        "--transport",
        default="sip",
        choices=["sip", "connect"],
        help="Telephony transport: 'sip' (Chime Voice Connector Full-Duplex SIP Trunk) or 'connect' (Amazon Connect)",
    )
    parser.add_argument(
        "--enable-amd",
        action="store_true",
        default=False,
        help="Enable Answering Machine Detection (AMD) with TrafficType='CAMPAIGN' (default: False)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate parameters and generate payload without initiating live call",
    )

    args = parser.parse_args()

    try:
        trigger_outbound_call(
            destination_phone=args.destination_phone,
            patient_first_name=args.patient_first_name,
            patient_last_name=args.patient_last_name,
            practice_name=args.practice_name,
            call_mode=args.call_mode,
            patient_id=args.patient_id,
            instance_id=args.instance_id,
            contact_flow_id=args.contact_flow_id,
            source_phone=args.source_phone,
            gateway_url=args.gateway_url,
            profile=args.profile,
            region=args.region,
            enable_amd=args.enable_amd,
            transport=args.transport,
            dry_run=args.dry_run,
        )
    except Exception as e:
        logger.error(f"Outbound call initiation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
