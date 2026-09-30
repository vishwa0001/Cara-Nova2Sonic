# Client Integration Specification: Agent Availability API & Handoff Webhook

CareHarmony's Cara platform requires two integration endpoints from your engineering team to enable real-time human agent handoffs during live patient calls:

1. **Agent Availability API (GET)**: Queried synchronously by Cara during an active call to check if a live agent is free to receive a transfer.
2. **Handoff Webhook (POST)**: Called by Cara when a transfer is initiated (or when follow-up is required) to transmit customer context, call summaries, and transcripts to your CRM / contact center system.

---

## 1. Agent Availability API

### Overview & Purpose

During a live call, Cara evaluates whether a human representative is available to accept an immediate warm or direct transfer. If an agent is available, Cara initiates the transfer to the provided phone number. If unavailable, Cara gracefully offers a callback or schedules follow-up routing.

- **Recommended Endpoint:** `GET /api/v1/agents/availability`
- **Latency Requirement:** `< 500 ms` (critical for seamless in-call conversational flow)

### Request Query Parameters

| Parameter    | Type     | Required | Description                                                          |
| :----------- | :------- | :------- | :------------------------------------------------------------------- |
| `patientId`  | `string` | Optional | Unique patient record identifier in your system (e.g., `PT-984321`). |
| `empi`       | `string` | Optional | Enterprise Master Person Index / MRN if available.                   |
| `campaignId` | `string` | Optional | Campaign identifier (e.g., `chronic-care-q3`).                       |
| `callId`     | `string` | Optional | Unique telephony session identifier for tracing.                     |

**Example Request:**

```http
GET /api/v1/agents/availability?patientId=PT-984321&campaignId=chronic-care-q3 HTTP/1.1
Host: api.yourdomain.com
Authorization: Bearer <YOUR_TOKEN_OR_API_KEY>
Accept: application/json
```

---

### Expected Response

- **Status Code:** `200 OK`
- **Content-Type:** `application/json`

#### Scenario A: Human Agent is Available

```json
{
  "available": true,
  "agentPhone": "+15822671755",
  "agentId": "agent-001",
  "agentName": "Sarah Jenkins",
  "checkedAt": "2026-09-04T13:14:00Z"
}
```

#### Scenario B: No Human Agent Available

```json
{
  "available": false,
  "agentPhone": null,
  "agentId": null,
  "agentName": null,
  "checkedAt": "2026-09-04T13:14:00Z"
}
```

#### Response Field Reference

| Field        | Type                | Description                                                                                                               |
| :----------- | :------------------ | :------------------------------------------------------------------------------------------------------------------------ |
| `available`  | `boolean`           | **Required.** `true` if an agent is ready to receive a call; `false` otherwise.                                           |
| `agentPhone` | `string` (E.164)    | **Required if available.** The destination phone number to which Cara will transfer the live call (e.g., `+15822671755`). |
| `agentId`    | `string`            | Optional. Unique identifier of the assigned agent or queue.                                                               |
| `agentName`  | `string`            | Optional. Agent display name for personalized conversational messaging.                                                   |
| `checkedAt`  | `string` (ISO 8601) | Timestamp of the availability evaluation.                                                                                 |

---

## 2. Handoff Webhook

### Overview & Purpose

When a call is transferred or flagged for human follow-up, Cara pushes interaction context, customer details, and call transcripts to your system so the receiving human agent has complete background information.

- **Recommended Endpoint:** `POST /api/v1/webhooks/call-handoff`
- **Content-Type:** `application/json`

### Request Payload (Sent by Cara)

```json
{
  "callId": "c9a782b1-54f2-4f28-b8cf-89241b123456",
  "patientId": "PT-984321",
  "empi": "EMPI-123456",
  "patientName": "John Doe",
  "patientPhone": "+18145551212",
  "campaignId": "chronic-care-q3",
  "transferReason": "LIVE_AGENT_REQUEST",
  "callMode": "DIRECT_HUMAN_HANDOFF",
  "transferredAt": "2026-09-04T13:14:30Z",
  "assignedAgent": {
    "agentId": "agent-001",
    "agentName": "Sarah Jenkins",
    "agentPhone": "+15822671755"
  },
  "callSummary": "Patient verified identity and requested to speak with a care coordinator regarding medication side effects.",
  "callRecordingUrl": "https://cara-health-bot-recordings-701348334422-us-east-1.s3.amazonaws.com/connect-recordings/ivr/2026/09/04/c4b8-20260904T1314_UTC.wav",
  "callRecordingS3Uri": "s3://cara-health-bot-recordings-701348334422-us-east-1/connect-recordings/ivr/2026/09/04/c4b8-20260904T1314_UTC.wav",
  "transcript": [
    {
      "speaker": "Cara",
      "message": "Hi, this is Cara from CareHarmony. Am I speaking with John Doe?",
      "timestamp": "2026-09-04T13:14:05Z"
    },
    {
      "speaker": "Patient",
      "message": "Yes, I have some questions about my blood pressure medication.",
      "timestamp": "2026-09-04T13:14:12Z"
    }
  ]
}
```

#### Request Payload Field Reference

| Field                | Type                | Description                                                                          |
| :------------------- | :------------------ | :----------------------------------------------------------------------------------- |
| `callId`             | `string`            | Unique telephony identifier for the call session.                                    |
| `patientId`          | `string`            | Patient identifier from the campaign intake record.                                  |
| `empi`               | `string`            | Enterprise Master Person Index / MRN if configured.                                  |
| `patientName`        | `string`            | Full name of the patient.                                                            |
| `patientPhone`       | `string` (E.164)    | Patient's contacted phone number.                                                    |
| `campaignId`         | `string`            | Campaign name or ID.                                                                 |
| `transferReason`     | `string`            | Reason code (e.g., `LIVE_AGENT_REQUEST`, `SAFETY_ESCALATION`, `CALLBACK_REQUESTED`). |
| `callMode`           | `string`            | Execution mode (e.g., `DIRECT_HUMAN_HANDOFF`, `STANDARD_CALL`).                      |
| `transferredAt`      | `string` (ISO 8601) | UTC timestamp when handoff was triggered.                                            |
| `assignedAgent`      | `object`            | Details of the assigned agent (`agentId`, `agentName`, `agentPhone`).                |
| `callSummary`        | `string`            | AI-generated summary of the conversation prior to handoff.                           |
| `callRecordingUrl`   | `string` (URL)      | Direct HTTPS S3 / presigned access URL for the audio recording.                      |
| `callRecordingS3Uri` | `string` (URI)      | Raw S3 URI (`s3://...`) for the automated interaction recording.                     |
| `transcript`         | `array[object]`     | Sequential array of dialogue turns (`speaker`, `message`, `timestamp`).              |

---

### Expected Response

- **Status Code:** `200 OK` or `202 Accepted`
- **Content-Type:** `application/json`

```json
{
  "status": "success",
  "handoffId": "handoff-evt-88392",
  "receivedAt": "2026-09-04T13:14:31Z"
}
```

---

## 3. Authentication & Security

Cara can authenticate with your endpoints using either of the following standard approaches (please confirm which your team will implement):

1. **API Key Header:** `x-api-key: <SECRET_KEY>`
2. **Bearer Token / OAuth 2.0:** `Authorization: Bearer <TOKEN>`

All endpoints must be served over **HTTPS**.
