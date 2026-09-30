"""CareHarmony Mock Client Integration APIs (FastAPI).

Implements the CareHarmony Client Integration endpoints:
1. GET  /api/v1/agents/availability  (SLA < 500ms)
2. POST /api/v1/webhooks/call-handoff (Persists to DynamoDB CareHarmony_HandoffEvents)
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from typing import Any, List, Optional

import boto3
from botocore.exceptions import ClientError, ProfileNotFound
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger("mock_client_api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="CareHarmony Mock Client Integration API",
    version="2.0.0",
    description="Mock PES Availability & Handoff Webhook service for CareHarmony Outreach IVR",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# AWS Session Helper
# ------------------------------------------------------------------------------

def get_boto3_session(profile_name: str = "careharmony", region_name: str = "us-east-1") -> boto3.Session:
    """Create a boto3 Session trying the given profile first, falling back to default/env credentials."""
    explicit_profile = os.environ.get("AWS_PROFILE", profile_name)
    try:
        session = boto3.Session(profile_name=explicit_profile, region_name=region_name)
        # Check credentials
        session.get_credentials()
        return session
    except (ProfileNotFound, Exception) as err:
        logger.debug(f"AWS Profile '{explicit_profile}' not found or invalid ({err}). Falling back to default credentials.")
        return boto3.Session(region_name=region_name)


# ------------------------------------------------------------------------------
# Pydantic Schemas
# ------------------------------------------------------------------------------

class AgentInfo(BaseModel):
    agentId: Optional[str] = None
    agentName: Optional[str] = None
    agentPhone: Optional[str] = None
    checkedAt: Optional[str] = None


class AvailabilityResponse(BaseModel):
    available: bool
    agentPhone: Optional[str] = None
    agentId: Optional[str] = None
    agentName: Optional[str] = None
    checkedAt: str


class TranscriptTurn(BaseModel):
    speaker: str = Field(..., description="'agent', 'caller', or 'system'")
    text: str
    timestamp: str


class CallHandoffPayload(BaseModel):
    callId: str = Field(..., description="Unique telephony call / contact identifier")
    patientId: str = Field(..., description="Patient record identifier")
    empi: str = Field(..., description="Enterprise Master Person Index")
    patientName: str = Field(..., description="Full name of patient")
    patientPhone: str = Field(..., description="E.164 phone number of patient")
    campaignId: str = Field(..., description="Outreach campaign ID")
    transferReason: str = Field(..., description="Reason for transfer (e.g., 'CONSENT_GRANTED', 'DIRECT_HANDOFF')")
    callMode: str = Field(..., description="'direct_handoff' or 'normal_coaching'")
    transferredAt: str = Field(..., description="ISO 8601 timestamp of transfer trigger")
    assignedAgent: Optional[AgentInfo] = None
    callSummary: str = Field(..., description="Concise LLM conversation summary")
    transcript: List[TranscriptTurn] = Field(default_factory=list, description="Ordered dialogue transcript")


class HandoffResponse(BaseModel):
    status: str = "success"
    handoffId: str
    receivedAt: str


# ------------------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------------------

@app.get("/healthz", tags=["System"])
def health_check() -> dict[str, str]:
    return {"status": "ok", "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()}


@app.get(
    "/api/v1/agents/availability",
    response_model=AvailabilityResponse,
    status_code=status.HTTP_200_OK,
    tags=["Agent Availability"],
    summary="Check PES Live Agent Availability (<500ms SLA)",
)
def get_agent_availability(
    callId: Optional[str] = Query(None, description="Current telephony call ID"),
    campaignId: Optional[str] = Query(None, description="Campaign identifier"),
    forceStatus: Optional[str] = Query(
        None, description="Deterministic override: 'available' or 'busy'"
    ),
) -> AvailabilityResponse:
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Check forceStatus override or environment flags
    is_busy = False
    if forceStatus:
        is_busy = forceStatus.lower() in {"busy", "0", "false", "unavailable"}
    elif os.environ.get("FORCE_AGENT_UNAVAILABLE", "").lower() in {"1", "true", "yes"}:
        is_busy = True

    if is_busy:
        return AvailabilityResponse(
            available=False,
            agentPhone=None,
            agentId=None,
            agentName=None,
            checkedAt=now_iso,
        )

    # Available response with configured / mock E.164 agent phone
    agent_phone = os.environ.get("FIXED_AGENT_PHONE", "+15822671755")
    agent_id = os.environ.get("FIXED_AGENT_ID", "agent-001")
    agent_name = os.environ.get("FIXED_AGENT_NAME", "Sarah Jenkins")

    return AvailabilityResponse(
        available=True,
        agentPhone=agent_phone,
        agentId=agent_id,
        agentName=agent_name,
        checkedAt=now_iso,
    )


@app.post(
    "/api/v1/webhooks/call-handoff",
    response_model=HandoffResponse,
    status_code=status.HTTP_200_OK,
    tags=["Webhooks"],
    summary="Receive post-call handoff payload and persist to DynamoDB",
)
def post_call_handoff(payload: CallHandoffPayload) -> HandoffResponse:
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    handoff_id = f"handoff-evt-{uuid.uuid4().hex[:8]}"

    table_name = os.environ.get("HANDOFF_EVENTS_TABLE", "CareHarmony_HandoffEvents")
    item = {
        "handoff_id": handoff_id,
        "call_id": payload.callId,
        "patient_id": payload.patientId,
        "empi": payload.empi,
        "patient_name": payload.patientName,
        "patient_phone": payload.patientPhone,
        "campaign_id": payload.campaignId,
        "transfer_reason": payload.transferReason,
        "call_mode": payload.callMode,
        "transferred_at": payload.transferredAt,
        "assigned_agent": payload.assignedAgent.model_dump() if payload.assignedAgent else None,
        "call_summary": payload.callSummary,
        "transcript": [turn.model_dump() for turn in payload.transcript],
        "received_at": now_iso,
    }

    # Attempt persistence to DynamoDB
    try:
        session = get_boto3_session()
        dynamodb = session.resource("dynamodb")
        table = dynamodb.Table(table_name)
        table.put_item(Item=item)
        logger.info(f"Successfully persisted handoff event {handoff_id} for call {payload.callId} in {table_name}")
    except Exception as e:
        # If running offline/mock mode without live DynamoDB table, log warning but accept payload
        logger.warning(f"DynamoDB persistence for handoff event failed: {e}; returning success response.")

    return HandoffResponse(
        status="success",
        handoffId=handoff_id,
        receivedAt=now_iso,
    )
