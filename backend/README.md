# Cara Health Bot

> **Standalone package — v3.1.2.** Cara Health Bot is designed to deploy into a fresh AWS account without any Talking Bot files, resource IDs, or shared Amazon Q assistant. Reruns are idempotent for Cara-owned resources; same-name resources that are not tagged as Cara Health Bot are treated as collisions instead of being silently reused.

AWS CloudShell deployment for an outbound identity-gated conversational voice bot using Amazon Connect, Amazon Lex V2, Amazon Q in Connect, and Nova 2 Sonic.

## Runtime behavior

1. Amazon Connect places the outbound call and starts stereo IVR/call recording in a new Cara-specific S3 bucket.
2. `cara-health-bot-identity` performs the privacy-minimal identity/recipient gate.
3. Safety intents have highest priority and immediately use the configured medical or behavioral safety response, then end.
4. Confirmed customers enter Cara's conversational layer for questions, objections, callbacks, refusals, and natural movement toward a human specialist.
5. Clear refusal ends persuasion. Unknown or business-specific questions use configured fallback responses instead of invented facts.
6. `ThirdPartyDetected` asks only whether the intended customer is available; `PatientUnavailable` moves directly toward callback timing.
7. If the intended customer comes to the phone, identity is checked again before Cara continues.
8. `TRANSFER_READY` context is preserved across normal questions so Cara answers and resumes the already-agreed handoff without restarting the conversation.

The identity gate is conversational confirmation, not strong authentication such as OTP/DOB verification.

## Human agent provisioning

`deploy.sh` creates/reuses Cara Health Bot-owned human-transfer resources: `CaraHealthBotQueue`, `CaraHealthBotRoutingProfile`, and `CaraHealthBotHumanAgent`. It associates the queue for `VOICE`, creates/reuses `caraagent`, and configures `SOFT_PHONE`. Existing Cara resources are updated idempotently; the deployment does not depend on `BasicQueue`, `Basic Routing Profile`, or the built-in `Agent` profile.

For the first creation of `caraagent`, provide `CARA_AGENT_PASSWORD` as an environment variable. The password is **not** written to `config.json`, `deployment-state.json`, or deployment output. On later deploys, if the user already exists, the password is not required.

The agent still needs to sign in to Agent Workspace and set status to `Available`; that is a live agent-session action, not infrastructure provisioning.

## Standalone fresh-account deployment

No Talking Bot directory, deployment state, assistant, queue, user, or other legacy resource is required. Upload this ZIP as `cara-health-bot.zip`, then run:

```bash
cd ~ && \
rm -rf cara-health-bot && \
unzip -oq cara-health-bot.zip && \
cd cara-health-bot && \
chmod +x deploy.sh cleanup.sh && \
CARA_AGENT_PASSWORD='<your-agent-password>' ./deploy.sh
```

On a fresh account, the deployer creates Cara-owned resources including:

```text
Amazon Connect instance:    cara-health-bot-<account-suffix>
Amazon Q assistant:         CaraHealthBotAssistant
Q prompt:                   CaraHealthBotPrompt
Q orchestration agent:      cara-health-bot-orchestrator
Identity Lex bot:           cara-health-bot-identity
Availability Lex bot:       cara-health-bot-availability
Conversation Lex bot:       cara-health-bot-nova-2-sonic
Lambda:                     cara-health-bot-session-context
Human queue:                CaraHealthBotQueue
Routing profile:            CaraHealthBotRoutingProfile
Human security profile:     CaraHealthBotHumanAgent
Human username:             caraagent
Recording bucket:           cara-health-bot-recordings-<account>-us-east-1
Contact flow:               CaraHealthBotNova2Sonic
```

The deployment also searches for and claims an available US source number for the new Connect instance. If the account cannot create the dedicated Q assistant because of service quota, deployment fails with a clear error and does **not** reuse another project's assistant.

`deployment-state.json` is written after deployment and is used by `cleanup.sh` to remove only Cara Health Bot-owned resources later.

## Start a call

```bash
python3 scripts/call.py "+18148316822" --customer-name "John" --i-confirm-consent
```

Use straight quotes, not smart quotes.

## Expected third-party examples

```text
Cara: Hi, may I speak with John?
Relative: This is his wife. He isn't here right now.
Cara: Thanks. I'm trying to reach John directly. Is John available to come to the phone?
Relative: No.
Cara/Lex: What day would be better to reach them?
Relative: Tomorrow.
Cara/Lex: What time would be better to reach them?
Relative: After six.
Cara: Thank you. I'll note that as a better time to reach them. Have a good day.
```

If John is present:

```text
Relative: Yes, he's here.
Cara: Thanks. Please pass the phone to John.
Cara: Hi. May I confirm I'm speaking with John?
John: Yes, this is John.
Cara: Thanks, John. I'm Cara, an automated assistant. I can help with the first part of this call and connect you with a human specialist when appropriate.
```

## Callback attributes

For an unavailable target, the contact record stores:

```text
recipientType=THIRD_PARTY
identityConfirmed=false
targetAvailableNow=false
callbackDate=<Lex AMAZON.Date normalized value>
callbackTime=<Lex AMAZON.Time normalized value>
identityPolicyVersion=v5-confidence-hardened-third-party
```

For a third party who passes the phone and the target then confirms:

```text
recipientType=THIRD_PARTY_THEN_TARGET
identityConfirmed=true
targetAvailableNow=true
identityPolicyVersion=v5-confidence-hardened-third-party
```

## Transcript & Call Recording

```bash
python3 scripts/transcript.py CONTACT_ID
```

Options:
- `--json`: Output structured JSON containing the recording ID, S3 URIs/URLs, presigned sharing URLs, and dialogue turns.
- `--presigned-url`: Generate a secure, time-limited presigned HTTPS URL to share/play the audio recording directly without AWS console credentials.
- `--presigned-expiry-seconds SECONDS`: Custom expiration duration for the presigned URL (default: 3600 / 1 hr).
- `--recording-url-only`: Output only the direct S3 URL for the call recording.
- `--no-header`: Print only the dialogue turns (`Cara:` / `Customer:`).

The transcript helper uses the Connect stereo automated/IVR recording: system audio on one channel and customer audio on the other.

## Documentation & Architecture Guides

- **[System Architecture, Data Lifecycle & Runtime Flow Guide](file:///d:/Code/Careharmony-Cara/backend/Cara_Health_Bot_System_Flow_and_Data_Lifecycle.md)** — Comprehensive end-to-end guide detailing the complete data lifecycle, state transitions, Lambda integrations, DynamoDB schemas, EventBridge rules, agent availability check, transfer evaluation, idempotency controls, and debugging playbook.
- **[ARCHITECTURE.md](file:///d:/Code/Careharmony-Cara/backend/ARCHITECTURE.md)** — Architectural overview and conversation state machine.
- **[CAMPAIGN.md](file:///d:/Code/Careharmony-Cara/backend/CAMPAIGN.md)** — Campaign management API, CSV schema, and dialer operational details.
- **[LESSONS_AND_REGRESSIONS.md](file:///d:/Code/Careharmony-Cara/backend/LESSONS_AND_REGRESSIONS.md)** — Regression prevention and known operational lessons.

## Outbound campaign add-on (v3.3.0)

Cara includes an optional sequential outbound campaign workaround deployed with the same boto3/AWS SDK model as the base project. No CDK bootstrap, ECR repository, or CDKToolkit stack is required. Deploy the normal Cara stack first with `./deploy.sh`, then deploy only the campaign-side S3/DynamoDB/Lambda/Scheduler/EventBridge resources with `./deploy-campaign.sh`. The campaign deployer reuses the existing Connect instance, published contact flow, and source phone number from `deployment-state.json`. See [CAMPAIGN.md](file:///d:/Code/Careharmony-Cara/backend/CAMPAIGN.md).

## Campaign frontend integration (v3.4.0)

The campaign workaround now includes a unified `cara-health-bot-campaign-api` Lambda that matches the supplied React frontend. See [CAMPAIGN.md](file:///d:/Code/Careharmony-Cara/backend/CAMPAIGN.md) for the `/uploads`, `/campaigns`, cancel/reschedule, rich CSV, timezone, and security contract. Campaign API Function URLs default to `AWS_IAM`; public `NONE` mode is explicit POC-only opt-in.

## Debugging & Troubleshooting Playbook

| Symptom | First Place to Inspect | What to Verify |
|---|---|---|
| **Call never starts** | `campaign_dialer` + DynamoDB | Campaign status (`PENDING`/`RUNNING`), patient status, EventBridge Scheduler trigger, `StartOutboundVoiceContact` permissions/errors. |
| **CSV upload does nothing** | S3 notification + `campaign_intake` logs | Object key exactly `campaigns/<id>/patients.csv`; S3 event notification Lambda permission; CSV header validation errors. |
| **Identity is wrong** | Identity Lex logs + Connect events | Spoken name matching, confidence thresholds, `IdentityConfirmed` / `IdentityDenied` / `IdentityAmbiguous` results. |
| **Q answers incorrectly** | `life-coach.yaml` + Q prompt version + Lex logs | Active prompt version, session context attributes, matched intent, model tool selection (`EscalateToHuman`, `RequestCallback`, `EndConversation`). |
| **Empty / invalid SSML** | Lex conversation log + Q response | `x-amz-lex:q-in-connect-response`; verify Q response is non-empty and correctly formatted SSML/plain text. |
| **Refusal does not terminate** | Q tool output + Contact Flow session attributes | Ensure Q returns `Tool=EndConversation` and `endReason=refusal` so Connect branches to the disconnect path. |
| **Human transfer fails** | `session_context` availability + Connect transfer branch | `available` boolean flag, resolved agent phone number, queue status, and external leg completion. |
| **Next patient does not start** | `campaign_dialer` `DISCONNECTED` event | Contact ID lookup in GSI (`contactId-index`), disposition classification, finalization from `IN_PROGRESS`, remaining `PENDING` rows. |
| **Callback not firing** | DynamoDB + EventBridge Scheduler | `CALLBACK_SCHEDULED` status, one-time schedule name/UTC execution time, target payload matching dialer input. |

### Inspecting Live Call Logs & Transcripts

```bash
# View CloudWatch logs for a specific contact
python3 scripts/logs.py <CONTACT_ID>

# Generate formatted dialogue transcript & audio URL
python3 scripts/transcript.py <CONTACT_ID> --presigned-url
```

