"""Unit tests for campaign_dialer dynamic ECS task discovery and caching."""
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = ROOT / "lambda"
STORAGE_DIR = ROOT / "storage"
sys.path.insert(0, str(STORAGE_DIR))
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(ROOT))

import campaign_dialer


@pytest.fixture(autouse=True)
def reset_dialer_cache(monkeypatch):
    """Ensure cache and env vars are clean before every test."""
    campaign_dialer._gateway_url_cache["url"] = None
    campaign_dialer._gateway_url_cache["resolved_at"] = 0.0
    monkeypatch.delenv("GATEWAY_ORIGINATE_URL", raising=False)
    yield
    campaign_dialer._gateway_url_cache["url"] = None
    campaign_dialer._gateway_url_cache["resolved_at"] = 0.0


def test_discover_current_ecs_task_ip_success():
    """Test successful discovery of task public IP from ECS + EC2 APIs."""
    mock_ecs = MagicMock()
    mock_ec2 = MagicMock()

    mock_ecs.list_tasks.return_value = {
        "taskArns": ["arn:aws:ecs:us-east-1:701348334422:task/careharmony-voice-cluster/task-123"]
    }
    mock_ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-east-1:701348334422:task/careharmony-voice-cluster/task-123",
                "attachments": [
                    {
                        "type": "ElasticNetworkInterface",
                        "details": [
                            {"name": "subnetId", "value": "subnet-1"},
                            {"name": "networkInterfaceId", "value": "eni-0123456789abcdef0"},
                        ],
                    }
                ],
            }
        ]
    }
    mock_ec2.describe_network_interfaces.return_value = {
        "NetworkInterfaces": [
            {
                "NetworkInterfaceId": "eni-0123456789abcdef0",
                "Association": {"PublicIp": "54.210.10.99"},
            }
        ]
    }

    with patch("campaign_dialer.boto3.client") as mock_boto:
        def client_factory(service_name, **kwargs):
            if service_name == "ecs":
                return mock_ecs
            if service_name == "ec2":
                return mock_ec2
            return MagicMock()

        mock_boto.side_effect = client_factory

        ip = campaign_dialer._discover_current_ecs_task_ip()
        assert ip == "54.210.10.99"

        url = campaign_dialer.resolve_ecs_gateway_url()
        assert url == "http://54.210.10.99:8000/calls/originate"


def test_resolve_ecs_gateway_url_caching():
    """Test that multiple calls within TTL use cache without querying ECS again."""
    mock_ecs = MagicMock()
    mock_ec2 = MagicMock()

    mock_ecs.list_tasks.return_value = {
        "taskArns": ["arn:aws:ecs:us-east-1:701348334422:task/careharmony-voice-cluster/task-123"]
    }
    mock_ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "attachments": [
                    {
                        "details": [
                            {"name": "networkInterfaceId", "value": "eni-0123"},
                        ],
                    }
                ],
            }
        ]
    }
    mock_ec2.describe_network_interfaces.return_value = {
        "NetworkInterfaces": [
            {"Association": {"PublicIp": "3.85.12.34"}}
        ]
    }

    with patch("campaign_dialer.boto3.client") as mock_boto:
        mock_boto.side_effect = lambda s, **kw: mock_ecs if s == "ecs" else mock_ec2

        # First call: discovers IP
        url1 = campaign_dialer.resolve_ecs_gateway_url()
        assert url1 == "http://3.85.12.34:8000/calls/originate"
        assert mock_ecs.list_tasks.call_count == 1

        # Second call: uses in-memory cache
        url2 = campaign_dialer.resolve_ecs_gateway_url()
        assert url2 == "http://3.85.12.34:8000/calls/originate"
        assert mock_ecs.list_tasks.call_count == 1  # No additional ECS call


def test_resolve_ecs_gateway_url_static_override(monkeypatch):
    """Test that non-AUTO GATEWAY_ORIGINATE_URL acts as an override."""
    monkeypatch.setenv("GATEWAY_ORIGINATE_URL", "http://127.0.0.1:9000/custom-originate")

    with patch("campaign_dialer._discover_current_ecs_task_ip") as mock_discover:
        url = campaign_dialer.resolve_ecs_gateway_url()
        assert url == "http://127.0.0.1:9000/custom-originate"
        mock_discover.assert_not_called()


def test_resolve_ecs_gateway_url_auto_env_var(monkeypatch):
    """Test that GATEWAY_ORIGINATE_URL=AUTO triggers dynamic discovery."""
    monkeypatch.setenv("GATEWAY_ORIGINATE_URL", "AUTO")

    with patch("campaign_dialer._discover_current_ecs_task_ip", return_value="18.200.5.10"):
        url = campaign_dialer.resolve_ecs_gateway_url()
        assert url == "http://18.200.5.10:8000/calls/originate"


def test_sanitize_dialer_error():
    """Test mapping raw network exceptions to clean categories."""
    assert campaign_dialer.sanitize_dialer_error("<urlopen error timed out>") == "GATEWAY_TIMEOUT"
    assert campaign_dialer.sanitize_dialer_error("connection timeout on socket") == "GATEWAY_TIMEOUT"
    assert campaign_dialer.sanitize_dialer_error("[Errno 111] Connection refused") == "GATEWAY_REFUSED"
    assert campaign_dialer.sanitize_dialer_error("Name or service not known") == "GATEWAY_DNS_FAILURE"
    assert campaign_dialer.sanitize_dialer_error("getaddrinfo failed") == "GATEWAY_DNS_FAILURE"
    assert campaign_dialer.sanitize_dialer_error("No route to host") == "GATEWAY_UNREACHABLE"
    assert campaign_dialer.sanitize_dialer_error("HTTP Error 502: Bad Gateway") == "GATEWAY_SERVER_ERROR"
    assert campaign_dialer.sanitize_dialer_error("HTTP Error 403: Forbidden") == "GATEWAY_REQUEST_REJECTED"
    assert campaign_dialer.sanitize_dialer_error("Some unexpected socket glitch") == "GATEWAY_ERROR"


def test_format_disposition_gateway_reasons():
    """Test that GATEWAY_* reasons produce clean human-readable disposition text."""
    from outreach_records import format_disposition

    assert format_disposition("FAILED", reason="GATEWAY_TIMEOUT") == "GATEWAY_TIMEOUT - Voice Gateway Unreachable (Timeout)"
    assert format_disposition("FAILED", reason="GATEWAY_REFUSED") == "GATEWAY_REFUSED - Voice Gateway Connection Refused"
    assert format_disposition("FAILED", reason="GATEWAY_DNS_FAILURE") == "GATEWAY_DNS_FAILURE - Voice Gateway Address Resolution Failed"
    assert format_disposition("FAILED", reason="GATEWAY_UNREACHABLE") == "GATEWAY_UNREACHABLE - Voice Gateway Network Unreachable"
    assert format_disposition("FAILED", reason="GATEWAY_SERVER_ERROR") == "GATEWAY_SERVER_ERROR - Voice Gateway Internal Error"
    assert format_disposition("FAILED", reason="GATEWAY_REQUEST_REJECTED") == "GATEWAY_REQUEST_REJECTED - Voice Gateway Rejected Request"
    assert format_disposition("FAILED", reason="GATEWAY_ERROR") == "GATEWAY_ERROR - Voice Gateway Connection Failed"


def test_format_call_summary_gateway_reasons():
    """Test that GATEWAY_* reasons generate readable call_summary sentences."""
    from outreach_records import format_call_summary

    summary = format_call_summary("FAILED", reason="GATEWAY_TIMEOUT")
    assert summary == "Call could not be placed: voice gateway was unreachable (Timeout)"

    summary2 = format_call_summary("FAILED", reason="GATEWAY_DNS_FAILURE")
    assert summary2 == "Call could not be placed: voice gateway was unreachable (Dns Failure)"


@pytest.mark.asyncio
async def test_schedule_retry_empty_call_id():
    """Test that schedule_retry handles empty call_id without writing last_call_id."""
    from outreach_records import OutreachRecordService

    service = OutreachRecordService(table_name="TestTable")
    res = await service.schedule_retry(
        patient_id="PT-TEST-001",
        call_id="",
        reason="GATEWAY_TIMEOUT",
        sip_code=None,
    )

    assert res["patient_id"] == "PT-TEST-001"
    assert res["call_status"] in ("PENDING", "EXHAUSTED")
    assert res["last_failure_reason"] == "GATEWAY_TIMEOUT"
    assert "last_call_id" not in res

    record = service._in_memory_store.get("PT-TEST-001")
    assert record is not None
    assert record["call_end_datetime"] != ""
    assert record["call_summary"].startswith(
        "Call could not be placed: voice gateway was unreachable (Timeout)"
    )
    assert "last_call_id" not in record

