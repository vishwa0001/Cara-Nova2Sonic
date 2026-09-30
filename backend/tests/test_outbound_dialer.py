"""Unit tests for outbound test dialing script and gateway deployment script."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_and_deploy_gateway import (
    deploy_gateway,
    ensure_ecr_repository,
    update_ecs_service_with_image,
)
from scripts.trigger_outbound_call import (
    load_deployment_configuration,
    trigger_outbound_call,
    validate_e164_phone,
)


# ==============================================================================
# 1. PHONE NUMBER VALIDATION TESTS
# ==============================================================================

@pytest.mark.parametrize(
    "phone,expected",
    [
        ("+15551234567", True),
        ("+17134811560", True),
        ("+442071838750", True),
        ("5551234567", False),       # Missing '+'
        ("+1", False),               # Too short
        ("invalid", False),          # Non-numeric
        ("", False),                 # Empty
        (None, False),               # None
        ("+1555123456789012345", False),  # Too long (>15 digits)
    ],
)
def test_validate_e164_phone(phone, expected):
    """Ensure E.164 international phone number format validation works properly."""
    assert validate_e164_phone(phone) == expected


# ==============================================================================
# 2. DEPLOYMENT CONFIGURATION LOADING TESTS
# ==============================================================================

def test_load_deployment_configuration_from_state_file(tmp_path):
    """Test loading configuration from deployment_state.json."""
    mock_state = {
        "connect": {"instance_id": "mock-instance-id-123"},
        "phone_number": {"number": "+17134811560"},
        "contact_flow": {"flow_id": "mock-flow-id-456"},
    }
    state_file = tmp_path / "deployment_state.json"
    state_file.write_text(json.dumps(mock_state), encoding="utf-8")

    mock_session = MagicMock()

    with patch("scripts.trigger_outbound_call.STATE_FILE_PATH", state_file):
        config = load_deployment_configuration(mock_session)

    assert config["instance_id"] == "mock-instance-id-123"
    assert config["source_phone"] == "+17134811560"
    assert config["contact_flow_id"] == "mock-flow-id-456"


def test_load_deployment_configuration_ssm_fallback(tmp_path):
    """Test falling back to SSM Parameter Store when state file is missing."""
    empty_file = tmp_path / "nonexistent.json"

    mock_ssm = MagicMock()
    mock_ssm.get_parameter.side_effect = lambda Name: {
        "/careharmony/connect/instance_id": {"Parameter": {"Value": "ssm-instance-id"}},
        "/careharmony/connect/claimed_phone_number": {"Parameter": {"Value": "+18005550199"}},
        "/careharmony/connect/contact_flow_id": {"Parameter": {"Value": "ssm-flow-id"}},
    }.get(Name, {})

    mock_session = MagicMock()
    mock_session.client.return_value = mock_ssm

    with patch("scripts.trigger_outbound_call.STATE_FILE_PATH", empty_file):
        config = load_deployment_configuration(mock_session)

    assert config["instance_id"] == "ssm-instance-id"
    assert config["source_phone"] == "+18005550199"
    assert config["contact_flow_id"] == "ssm-flow-id"


def test_load_deployment_configuration_cli_overrides():
    """Test explicit CLI parameter overrides taking precedence."""
    mock_session = MagicMock()
    config = load_deployment_configuration(
        mock_session,
        instance_id_override="override-inst",
        contact_flow_id_override="override-flow",
        source_phone_override="+19998887777",
    )
    assert config["instance_id"] == "override-inst"
    assert config["contact_flow_id"] == "override-flow"
    assert config["source_phone"] == "+19998887777"


# ==============================================================================
# 3. TRIGGER OUTBOUND CALL TESTS
# ==============================================================================

def test_trigger_outbound_call_dry_run_default():
    """Verify that dry-run mode defaults to general outbound calling without AMD/TrafficType."""
    with patch("scripts.trigger_outbound_call.load_deployment_configuration") as mock_load:
        mock_load.return_value = {
            "instance_id": "test-inst-id",
            "contact_flow_id": "test-flow-id",
            "source_phone": "+17134811560",
        }

        result = trigger_outbound_call(
            destination_phone="+15551234567",
            patient_first_name="Jane",
            patient_last_name="Doe",
            practice_name="Highland Family Medicine",
            call_mode="MODE_2_NORMAL_COACHING",
            patient_id="P-00123",
            enable_amd=False,
            transport="connect",
            dry_run=True,
        )

        assert result["status"] == "DRY_RUN_SUCCESS"
        params = result["params"]
        assert params["DestinationPhoneNumber"] == "+15551234567"
        assert params["SourcePhoneNumber"] == "+17134811560"
        assert params["InstanceId"] == "test-inst-id"
        assert params["ContactFlowId"] == "test-flow-id"
        assert params["Attributes"] == {
            "FirstName": "Jane",
            "LastName": "Doe",
            "PracticeName": "Highland Family Medicine",
            "CallMode": "MODE_2_NORMAL_COACHING",
            "PatientId": "P-00123",
        }
        assert "AnswerMachineDetectionConfig" not in params
        assert "TrafficType" not in params


def test_trigger_outbound_call_dry_run_with_amd():
    """Verify that dry-run mode with enable_amd=True populates AMD config and TrafficType CAMPAIGN."""
    with patch("scripts.trigger_outbound_call.load_deployment_configuration") as mock_load:
        mock_load.return_value = {
            "instance_id": "test-inst-id",
            "contact_flow_id": "test-flow-id",
            "source_phone": "+17134811560",
        }

        result = trigger_outbound_call(
            destination_phone="+15551234567",
            patient_first_name="Alex",
            patient_last_name="Rivera",
            call_mode="MODE_1_DIRECT_HANDOFF",
            enable_amd=True,
            transport="connect",
            dry_run=True,
        )

        assert result["status"] == "DRY_RUN_SUCCESS"
        params = result["params"]
        assert params["AnswerMachineDetectionConfig"] == {
            "EnableAnswerMachineDetection": True,
            "AwaitAnswerMachinePrompt": True,
        }
        assert params["TrafficType"] == "CAMPAIGN"


def test_trigger_outbound_call_live_execution():
    """Test live execution making the start_outbound_voice_contact call without AMD by default."""
    mock_connect = MagicMock()
    mock_connect.start_outbound_voice_contact.return_value = {
        "ContactId": "abc-1234-contact-id"
    }

    mock_session = MagicMock()
    mock_session.client.return_value = mock_connect

    with patch("scripts.trigger_outbound_call.get_aws_session", return_value=mock_session), \
         patch("scripts.trigger_outbound_call.load_deployment_configuration") as mock_load:
        mock_load.return_value = {
            "instance_id": "inst-123",
            "contact_flow_id": "flow-456",
            "source_phone": "+17134811560",
        }

        result = trigger_outbound_call(
            destination_phone="+15559876543",
            patient_first_name="Alex",
            patient_last_name="Rivera",
            call_mode="MODE_1_DIRECT_HANDOFF",
            transport="connect",
            dry_run=False,
        )

        assert result["status"] == "SUCCESS"
        assert result["contact_id"] == "abc-1234-contact-id"
        mock_connect.start_outbound_voice_contact.assert_called_once()
        call_kwargs = mock_connect.start_outbound_voice_contact.call_args[1]
        assert call_kwargs["DestinationPhoneNumber"] == "+15559876543"
        assert call_kwargs["Attributes"]["CallMode"] == "MODE_1_DIRECT_HANDOFF"
        assert "AnswerMachineDetectionConfig" not in call_kwargs


def test_trigger_outbound_call_amd_retry_fallback():
    """Test automatic fallback retry when Connect returns TrafficType CAMPAIGN is required."""
    mock_connect = MagicMock()

    # First call raises InvalidRequestException, second call succeeds
    error_response = {
        "Error": {
            "Code": "InvalidRequestException",
            "Message": "TrafficType CAMPAIGN is required when AnswerMachineDetectionConfig is specified.",
        }
    }
    mock_connect.start_outbound_voice_contact.side_effect = [
        ClientError(error_response, "StartOutboundVoiceContact"),
        {"ContactId": "fallback-contact-id-789"},
    ]

    mock_session = MagicMock()
    mock_session.client.return_value = mock_connect

    with patch("scripts.trigger_outbound_call.get_aws_session", return_value=mock_session), \
         patch("scripts.trigger_outbound_call.load_deployment_configuration") as mock_load:
        mock_load.return_value = {
            "instance_id": "inst-123",
            "contact_flow_id": "flow-456",
            "source_phone": "+17134811560",
        }

        result = trigger_outbound_call(
            destination_phone="+15559876543",
            patient_first_name="Alex",
            patient_last_name="Rivera",
            enable_amd=True,
            transport="connect",
            dry_run=False,
        )

        assert result["status"] == "SUCCESS"
        assert result["contact_id"] == "fallback-contact-id-789"
        assert mock_connect.start_outbound_voice_contact.call_count == 2
        
        # Second call parameters should have stripped AMD config and TrafficType
        retry_kwargs = mock_connect.start_outbound_voice_contact.call_args[1]
        assert "AnswerMachineDetectionConfig" not in retry_kwargs
        assert "TrafficType" not in retry_kwargs


def test_trigger_outbound_call_invalid_inputs():
    """Verify ValueError is raised on invalid inputs (phone format, invalid mode, missing config)."""
    # Invalid phone
    with pytest.raises(ValueError, match="Invalid destination phone number"):
        trigger_outbound_call(destination_phone="5551234567")

    # Invalid call mode
    with pytest.raises(ValueError, match="Invalid call mode"):
        trigger_outbound_call(
            destination_phone="+15551234567",
            call_mode="INVALID_MODE",
        )

    # Missing configuration
    with patch("scripts.trigger_outbound_call.load_deployment_configuration", return_value={}):
        with pytest.raises(ValueError, match="Amazon Connect Instance ID could not be resolved"):
            trigger_outbound_call(destination_phone="+15551234567", transport="connect")


# ==============================================================================
# 4. GATEWAY BUILD & DEPLOYMENT SCRIPT TESTS
# ==============================================================================

def test_ensure_ecr_repository_dry_run():
    """Verify ECR repository discovery / creation in dry-run mode."""
    mock_session = MagicMock()
    mock_session.region_name = "us-east-1"
    uri = ensure_ecr_repository(mock_session, repo_name="careharmony-voice-gateway", dry_run=True)
    assert "careharmony-voice-gateway" in uri
    assert "us-east-1" in uri


def test_update_ecs_service_with_image_dry_run():
    """Verify ECS task definition and service update in dry-run mode."""
    mock_session = MagicMock()
    mock_session.region_name = "us-east-1"
    result = update_ecs_service_with_image(
        mock_session,
        image_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/careharmony-voice-gateway:latest",
        dry_run=True,
    )
    assert "task-definition/careharmony-voice-gateway" in result["task_definition_arn"]
    assert result["desired_count"] == 1
    assert result["status"] == "DRY_RUN_UPDATED"


def test_deploy_gateway_dry_run():
    """Verify end-to-end deploy_gateway dry run."""
    with patch("scripts.build_and_deploy_gateway.get_aws_session") as mock_session_getter:
        mock_session = MagicMock()
        mock_session.region_name = "us-east-1"
        mock_session_getter.return_value = mock_session
        result = deploy_gateway(dry_run=True)
        assert result["status"] == "DRY_RUN_COMPLETED"
        assert "careharmony-voice-gateway" in result["ecr_repo_uri"]
