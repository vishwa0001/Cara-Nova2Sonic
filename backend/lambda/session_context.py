from __future__ import annotations

import datetime as dt
import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
from typing import Any

import boto3

SESSION_ARN = re.compile(
    r"^arn:aws:wisdom:(?P<region>[^:]+):(?P<account>\d{12}):session/(?P<assistant>[a-f0-9-]{36})/(?P<session>[a-f0-9-]{36})$"
)

def _get_client(service_name: str, region_name: str | None = None) -> Any:
    return boto3.client(service_name, region_name=region_name or os.environ.get("AWS_REGION", "us-east-1"))


def _required(params: dict[str, Any], key: str) -> str:
    value = str(params.get(key) or "").strip()
    if not value:
        raise ValueError(f"Missing required Lambda invocation parameter: {key}")
    return value


def _optional(params: dict[str, Any], key: str) -> str:
    return str(params.get(key) or "").strip()


def _name_token(value: str) -> str:
    # Normalize case, accents, punctuation, apostrophes and hyphens without
    # introducing fuzzy/nickname matching. Identity must remain conservative.
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in decomposed if ch.isalnum() and not unicodedata.combining(ch))


def _expected_name_parts(value: str) -> list[str]:
    return [_name_token(part) for part in re.findall(r"[^\s]+", value) if _name_token(part)]


def _verify_identity_name(expected: str, first: str, last: str) -> str:
    expected_parts = _expected_name_parts(expected)
    first_norm = _name_token(first)
    last_norm = _name_token(last)

    if not expected_parts or not first_norm:
        return "ambiguous"

    # Existing single-name callers remain supported.
    if len(expected_parts) == 1:
        if last_norm:
            return "false"
        return "true" if first_norm == expected_parts[0] else "false"

    # For a configured full name, require both first and last name. Ignore
    # middle names in the configured value, but do not accept a first-name-only
    # match as full identity confirmation.
    if not last_norm:
        return "ambiguous"
    return (
        "true"
        if first_norm == expected_parts[0] and last_norm == expected_parts[-1]
        else "false"
    )


def _fetch_agent_availability(patient_id: str, empi: str) -> dict[str, str]:
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = os.environ.get("AGENT_AVAILABILITY_URL", "https://w2adk4gg5z3rkcmwx3uczlne4y0ezfig.lambda-url.us-east-1.on.aws/?available=true")
    fixed_phone = os.environ.get("FIXED_AGENT_PHONE", "+15822671755")
    lambda_func = os.environ.get("AGENT_AVAILABILITY_FUNCTION", "cara-health-bot-agent-availability")

    # 1. Primary path: Direct SDK Lambda invoke for sub-millisecond execution
    try:
        lambda_client = _get_client("lambda")
        invoke_payload = {
            "queryStringParameters": {
                "patientId": patient_id or "",
                "empi": empi or "",
            }
        }
        resp = lambda_client.invoke(
            FunctionName=lambda_func,
            InvocationType="RequestResponse",
            Payload=json.dumps(invoke_payload),
        )
        payload_raw = resp["Payload"].read().decode("utf-8")
        parsed = json.loads(payload_raw) if payload_raw else {}
        if "body" in parsed:
            data = json.loads(parsed["body"]) if isinstance(parsed["body"], str) else parsed["body"]
        else:
            data = parsed

        if isinstance(data, dict) and "available" in data:
            available = bool(data.get("available"))
            agent_phone = str(data.get("agentPhone") or fixed_phone)
            agent_id = str(data.get("agentId") or "")
            agent_name = str(data.get("agentName") or "")
            checked_at = str(data.get("checkedAt") or now_iso)
            return {
                "available": "true" if available else "false",
                "agentPhone": agent_phone,
                "agentId": agent_id,
                "agentName": agent_name,
                "agentCheckedAt": checked_at,
            }
    except Exception as invoke_err:
        print(f"[WARN] Direct Lambda invoke for agent availability failed: {invoke_err}; falling back to HTTP Function URL")

    # 2. Fallback path: HTTP via Function URL
    try:
        target_url = url
        params = {}
        if patient_id:
            params["patientId"] = patient_id
        if empi:
            params["empi"] = empi
        if params:
            query = urllib.parse.urlencode(params)
            target_url = f"{url}?{query}" if "?" not in url else f"{url}&{query}"
        with urllib.request.urlopen(target_url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            available = bool(data.get("available"))
            agent_phone = str(data.get("agentPhone") or fixed_phone)
            agent_id = str(data.get("agentId") or "")
            agent_name = str(data.get("agentName") or "")
            checked_at = str(data.get("checkedAt") or now_iso)
            return {
                "available": "true" if available else "false",
                "agentPhone": agent_phone,
                "agentId": agent_id,
                "agentName": agent_name,
                "agentCheckedAt": checked_at,
            }
    except Exception as e:
        print(f"[WARN] _fetch_agent_availability HTTP fallback also failed: {e}")
        return {
            "available": "false",
            "agentPhone": fixed_phone,
            "agentId": "",
            "agentName": "",
            "agentCheckedAt": now_iso,
        }



def _handle_lex_fulfillment(event: dict[str, Any]) -> dict[str, Any]:
    session_state = event.get("sessionState") or {}
    session_attrs = session_state.get("sessionAttributes") or {}
    q_response = (session_attrs.get("x-amz-lex:q-in-connect-response") or "").strip()
    tool = (session_attrs.get("Tool") or "").strip()

    if not q_response:
        if tool == "EscalateToHuman":
            q_response = "I'll connect you with a specialist now."
        elif tool == "RequestCallback":
            q_response = "I'll schedule a callback for you."
        elif tool == "EndConversation":
            q_response = "Understood. Thank you for your time, goodbye."
        else:
            q_response = "I'm here to help."
        session_attrs["x-amz-lex:q-in-connect-response"] = q_response

    intent_obj = session_state.get("intent") or {}
    intent_name = intent_obj.get("name") or "AmazonQinConnect"

    return {
        "sessionState": {
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": intent_name,
                "state": "Fulfilled",
            },
            "sessionAttributes": session_attrs,
        },
        "messages": [
            {
                "contentType": "PlainText",
                "content": q_response,
            }
        ],
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # Handle Lex V2 fulfillment code hook invocations
    if event.get("invocationSource") == "FulfillmentCodeHook" or ("sessionState" in event and "Details" not in event):
        return _handle_lex_fulfillment(event)

    # Do not log the incoming event because it contains customer-specific data.
    details = event.get("Details") or {}
    contact_data = details.get("ContactData") or {}
    attributes = contact_data.get("Attributes") or {}
    params = details.get("Parameters") or {}
    operation = str(params.get("operation") or "initialize").strip()

    if operation == "checkAgentAvailability":
        patient_id = _optional(params, "patientId") or _optional(attributes, "patientId")
        empi = _optional(params, "empi") or _optional(attributes, "empi")

        # 1. Fetch fresh live availability from mock API at transfer time
        avail_data = _fetch_agent_availability(patient_id=patient_id, empi=empi)
        is_available = avail_data.get("available") == "true"

        # 2. Priority resolution order:
        # UI/campaign-configured humanAgentPhoneNumber > API agentPhone > FIXED_AGENT_PHONE default
        configured_ui_phone = (
            _optional(params, "humanAgentPhoneNumber")
            or _optional(attributes, "humanAgentPhoneNumber")
            or _optional(params, "agentPhone")
            or _optional(attributes, "agentPhone")
        )
        resolved_phone = (
            configured_ui_phone
            or avail_data.get("agentPhone")
            or os.environ.get("FIXED_HUMAN_AGENT_PHONE_NUMBER", os.environ.get("FIXED_AGENT_PHONE", "+15822671755"))
        )
        agent_id = avail_data.get("agentId") or ""
        agent_name = avail_data.get("agentName") or ""
        checked_at = avail_data.get("agentCheckedAt") or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        print(f"[session_context] [INFO] Transfer-time agent availability check: patientId={patient_id} empi={empi} available={is_available} resolvedPhone={resolved_phone} agentId={agent_id} agentName={agent_name}")
        return {
            "available": "true" if is_available else "false",
            "agentPhone": resolved_phone,
            "agentId": agent_id,
            "agentName": agent_name,
            "agentCheckedAt": checked_at,
        }

    if operation == "verifyIdentityName":
        expected = _required(params, "expectedCustomerName")
        result = _verify_identity_name(
            expected,
            _optional(params, "spokenFirstName"),
            _optional(params, "spokenLastName"),
        )
        return {"identityMatch": result}

    if operation == "recognize_text":
        bot_id = _required(params, "botId")
        bot_alias_id = _required(params, "botAliasId")
        text = _required(params, "text")
        session_id = _optional(params, "sessionId") or "eval-session"
        locale_id = _optional(params, "localeId") or "en_US"
        lex_rt = _get_client("lexv2-runtime", region_name="us-east-1")
        resp = lex_rt.recognize_text(
            botId=bot_id,
            botAliasId=bot_alias_id,
            localeId=locale_id,
            sessionId=session_id,
            text=text,
        )
        interps = resp.get("interpretations", [])
        top_intent = (resp.get("sessionState") or {}).get("intent", {}).get("name")
        return {
            "matchedIntent": str(top_intent or ""),
            "interpretations": json.dumps(interps),
            "sessionState": json.dumps(resp.get("sessionState") or {}),
        }

    if operation == "evaluateTransferOutcome":
        contact_id = (
            _optional(params, "contactId")
            or contact_data.get("ContactId")
            or contact_data.get("InitialContactId")
            or ""
        )
        instance_arn = str(contact_data.get("InstanceARN") or "").strip()
        instance_id = ""
        if instance_arn and "/" in instance_arn:
            instance_id = instance_arn.split("/")[-1]
        if not instance_id:
            instance_id = os.environ.get("CONNECT_INSTANCE_ID", "8e7c7423-5e98-44ab-a748-b3ca0bd7a896")

        if not contact_id or not instance_id:
            print(f"[session_context] [WARN] evaluateTransferOutcome: missing contactId ({contact_id}) or instanceId ({instance_id})")
            return {
                "transferOutcome": "NO_PICKUP",
                "transferDurationSeconds": "0",
                "transferEvalReason": "MISSING_CONTACT_OR_INSTANCE_ID",
            }

        connect_client = _get_client("connect")
        try:
            resp = connect_client.list_associated_contacts(
                InstanceId=instance_id,
                ContactId=contact_id,
            )
            external_legs = [
                item for item in resp.get("ContactSummaryList", [])
                if item.get("InitiationMethod") == "EXTERNAL_OUTBOUND"
            ]
            if not external_legs:
                print(f"[session_context] [INFO] evaluateTransferOutcome: No EXTERNAL_OUTBOUND contact leg found for contact {contact_id}")
                return {
                    "transferOutcome": "NO_PICKUP",
                    "transferDurationSeconds": "0",
                    "transferEvalReason": "NO_EXTERNAL_OUTBOUND_LEG",
                }

            # Sort by InitiationTimestamp descending to examine the latest transfer leg
            external_legs.sort(key=lambda x: str(x.get("InitiationTimestamp") or ""), reverse=True)
            latest_leg = external_legs[0]
            child_id = str(latest_leg.get("ContactId") or "")
            init_ts = latest_leg.get("InitiationTimestamp")
            disc_ts = latest_leg.get("DisconnectTimestamp")

            # Describe child contact to inspect DisconnectReason
            child_desc = connect_client.describe_contact(
                InstanceId=instance_id,
                ContactId=child_id,
            ).get("Contact", {})
            disc_reason = str(child_desc.get("DisconnectReason") or "").strip()

            duration = 0.0
            if init_ts and disc_ts:
                if hasattr(disc_ts, "timestamp") and hasattr(init_ts, "timestamp"):
                    duration = (disc_ts - init_ts).total_seconds()
                else:
                    d_obj = dt.datetime.fromisoformat(str(disc_ts).replace("Z", "+00:00"))
                    i_obj = dt.datetime.fromisoformat(str(init_ts).replace("Z", "+00:00"))
                    duration = (d_obj - i_obj).total_seconds()
            elif not disc_ts and init_ts:
                # Call leg is still active / connected
                print(f"[session_context] [INFO] evaluateTransferOutcome: Transfer leg {child_id} is active (no disconnect timestamp)")
                return {
                    "transferOutcome": "COMPLETED",
                    "transferDurationSeconds": "ACTIVE",
                    "childContactId": child_id,
                    "disconnectReason": "ACTIVE",
                    "transferEvalReason": "LEG_ACTIVE_CONNECTED",
                }

            # DisconnectReason evaluation:
            # THIRD_PARTY_DISCONNECT (agent hung up) or CUSTOMER_DISCONNECT (customer hung up) -> COMPLETED
            # ConnectionTimeLimitExceeded, CallFailed, TELECOM_TIMEOUT, TELECOM_UNANSWERED, TELECOM_PROBLEM -> NO_PICKUP
            if disc_reason in ("THIRD_PARTY_DISCONNECT", "CUSTOMER_DISCONNECT"):
                outcome = "COMPLETED"
            elif disc_reason in ("ConnectionTimeLimitExceeded", "CallFailed", "TELECOM_TIMEOUT", "TELECOM_UNANSWERED", "TELECOM_PROBLEM"):
                outcome = "NO_PICKUP"
            else:
                outcome = "NO_PICKUP"

            print(f"[session_context] [INFO] evaluateTransferOutcome: childContactId={child_id} disconnectReason={disc_reason} duration={duration:.1f}s outcome={outcome}")
            return {
                "transferOutcome": outcome,
                "transferDurationSeconds": str(int(duration)),
                "childContactId": child_id,
                "disconnectReason": disc_reason,
                "transferEvalReason": f"DISCONNECT_REASON_{disc_reason or 'UNKNOWN'}",
            }
        except Exception as e:
            print(f"[session_context] [ERROR] evaluateTransferOutcome failed: {e}")
            return {
                "transferOutcome": "NO_PICKUP",
                "transferDurationSeconds": "0",
                "transferEvalReason": f"ERROR_{type(e).__name__}",
            }

    if operation != "initialize":
        raise ValueError("Unsupported operation")

    # At this point identity has already been deterministically confirmed by
    # either a contextual confirmation (for example, "speaking") or the
    # full-name validator above.
    assistant_id = _required(params, "assistantId")
    session_arn = _required(params, "sessionArn")
    customer_name = _required(params, "customerName")
    expected_phone = _required(params, "expectedPhone")
    direct_agent = _optional(params, "direct_agent") or _optional(params, "directAgent") or "no"
    practice_name = _optional(params, "practiceName")
    first_name = _optional(params, "firstName")
    provider_name = _optional(params, "providerName") or _optional(params, "provider_name")

    match = SESSION_ARN.fullmatch(session_arn)
    if not match:
        raise ValueError("Unexpected Amazon Q session ARN")
    if match.group("assistant") != assistant_id:
        raise ValueError("Session ARN does not belong to the configured assistant")

    session_data = [
        {"key": "customerName", "value": {"stringValue": customer_name}},
        {"key": "expectedPhone", "value": {"stringValue": expected_phone}},
        {"key": "identityPolicyVersion", "value": {"stringValue": "v7-full-name-validated"}},
        {"key": "identityConfirmed", "value": {"stringValue": "true"}},
        {"key": "conversationState", "value": {"stringValue": "PATIENT_CONFIRMED"}},
        {"key": "direct_agent", "value": {"stringValue": direct_agent or "no"}},
    ]
    if practice_name:
        session_data.append({"key": "practiceName", "value": {"stringValue": practice_name}})
    if provider_name:
        session_data.append({"key": "providerName", "value": {"stringValue": provider_name}})
    if first_name:
        session_data.append({"key": "firstName", "value": {"stringValue": first_name}})

    _get_client("qconnect", region_name=match.group("region")).update_session_data(
        assistantId=assistant_id,
        sessionId=session_arn,
        namespace="Custom",
        data=session_data,
    )
    return {"contextStatus": "READY", "identityConfirmed": "true"}
