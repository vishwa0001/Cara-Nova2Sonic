import datetime
import json
import os
import random


def handler(event, context):
    # Handle CORS preflight
    http_method = (
        event.get("requestContext", {})
        .get("http", {})
        .get("method")
        or event.get("httpMethod")
    )
    if http_method == "OPTIONS":
        return {
            "statusCode": 204,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET,OPTIONS",
                "Access-Control-Allow-Headers": "content-type",
            },
            "body": "",
        }

    query_params = (
        event.get("queryStringParameters")
        or event.get("rawQueryString")
        or {}
    )
    if isinstance(query_params, str):
        import urllib.parse
        query_params = dict(urllib.parse.parse_qsl(query_params))

    details_params = (event.get("Details") or {}).get("Parameters") or {}
    if (
        event.get("operation") == "ping"
        or details_params.get("operation") == "ping"
        or (isinstance(query_params, dict) and str(query_params.get("operation", "")).lower() == "ping")
    ):
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps(
                {
                    "status": "ok",
                    "prewarmed": True,
                    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                }
            ),
        }

    patient_id = str(query_params.get("patientId") or "").lower()
    empi = str(query_params.get("empi") or "").lower()
    raw_query = str(event.get("rawQueryString") or "").lower()

    if str(query_params.get("available", "")).lower() in {"1", "true", "yes"}:
        available = True
    elif "unavail" in empi or "unavail" in patient_id or "unavail" in raw_query or str(query_params.get("available", "")).lower() in {"0", "false", "no"}:
        available = False
    elif os.environ.get("FORCE_AGENT_AVAILABLE", "").lower() in {"1", "true", "yes"}:
        available = True
    elif os.environ.get("FORCE_AGENT_UNAVAILABLE", "").lower() in {"1", "true", "yes"}:
        available = False
    else:
        # Default to available (True) for testing purposes
        available = True
    agent_phone = os.environ.get("FIXED_AGENT_PHONE", "+15822671755")
    agent_id = os.environ.get("FIXED_AGENT_ID", "agent-001")
    agent_name = os.environ.get("FIXED_AGENT_NAME", "Sarah Jenkins")

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(
            {
                "available": available,
                "agentPhone": agent_phone if available else None,
                "agentId": agent_id if available else None,
                "agentName": agent_name if available else None,
                "checkedAt": datetime.datetime.utcnow().isoformat() + "Z",
            }
        ),
    }

