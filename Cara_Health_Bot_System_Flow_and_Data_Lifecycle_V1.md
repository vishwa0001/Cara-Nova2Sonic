# Cara Health Bot: System Architecture, Data Lifecycle & Runtime Flow

> **Backend / AWS Implementation & Developer Debugging Guide**  
> *Prepared from backend source and project documentation • 2026-09-17*


## 1. Purpose of this document

This document explains how the supplied Cara Health Bot backend works from the moment a campaign is created through patient data ingestion, scheduling, outbound dialing, Amazon Connect execution, conversational AI, Lambda callbacks, result persistence, retries/callback scheduling, and campaign completion.

It is written as a developer/debugging guide: for each major step, it identifies the service involved, what data is written or read, what triggers the next step, and which source file owns the behavior. It includes complete architectural models, NLU multi-bot hierarchy and intent catalogs, privacy boundaries, safety filters, session context flows, and operational debugging playbooks.


## 2. System at a glance

```text
┌──────────────────────┐
│ Frontend / Campaign UI│
└──────────┬───────────┘
           │ HTTP
           ▼
┌──────────────────────┐
│ campaign_api Lambda  │
└───────┬────────┬─────┘
        │        │
writes  │        │ presigned upload
        ▼        ▼
┌────────────┐  ┌───────────────┐
│ DynamoDB   │  │ S3 campaign   │
│ campaign   │  │ config + CSV  │
└────────────┘  └───────┬───────┘
                        │ ObjectCreated
                        ▼
             ┌─────────────────────┐
             │ campaign_intake     │
             │ Lambda              │
             └───────┬─────────────┘
                     │
┌────────────────────┼──────────────────┐
│                    │                  │
▼                    ▼                  ▼
DynamoDB            EventBridge        EventBridge
patient rows        Scheduler          pre-warm
│                    │                  │
│                    ▼                  │
│             campaign-start           │
│                    │                  │
└────────────────────┼──────────────────┘
                     ▼
             ┌─────────────────────┐
             │ campaign_dialer     │
             │ Lambda              │
             └─────────┬───────────┘
                       │ StartOutboundVoiceContact
                       ▼
             ┌─────────────────────┐
             │ Amazon Connect      │
             │ Contact Flow        │
             └─────────┬───────────┘
                       │
┌──────────────────────┼─────────────────────┐
│                      │                     │
▼                      ▼                     ▼
Identity Lex          Session Context Lambda   Q in Connect
│                      │                     │
│                      ▼                     ▼
│                Q session data       Nova Lite
│                                            │
│                                            ▼
│                                     Nova 2 Sonic
│                                            │
└──────────────────────┬─────────────────────┘
                       │
             Connect contact attributes
                       │
                       ▼
             DISCONNECTED event
                       │
                       ▼
             EventBridge rule
                       │
                       ▼
             campaign_dialer
                       │
      finalize / callback / next patient
                       │
                       ▼
                  DynamoDB
```


## 3. Main AWS components

| Component | Purpose | Source / ownership |
| --- | --- | --- |
| Amazon Connect | Places/handles the outbound call and executes the published Contact Flow. | contact-flows/cara-health-bot-flow.json + deployer.py |
| Amazon Lex V2 - Identity | Deterministic identity/recipient gate before conversational processing. | builders.py + deployer.py |
| Amazon Lex V2 - Availability | Determines whether a third party says the intended customer is available. | builders.py + deployer.py |
| Amazon Lex V2 - Conversation | Voice conversation entry point; delegates conversational turns to Amazon Q in Connect. | builders.py + deployer.py |
| Amazon Q in Connect | Orchestrates conversational responses, objections, callbacks, refusal, transfer decisions. | prompts/life-coach.yaml + builders.py |
| Amazon Nova Lite | Configured orchestration model for the Q prompt. | config.json |
| Amazon Nova 2 Sonic | Configured speech model for the conversation Lex bot. | config.json |
| session_context Lambda | Initializes Q session context, checks agent availability, validates names, evaluates transfer legs, and handles supported Lex fulfillment. | lambda/session_context.py |
| campaign_api Lambda | Frontend-facing campaign management API. | lambda/campaign_api.py |
| campaign_intake Lambda | Triggered by patient CSV upload; validates/imports patient rows and creates schedules. | lambda/campaign_intake.py |
| campaign_dialer Lambda | Sequential call worker; starts calls, handles DISCONNECTED events, finalizes outcomes and schedules callbacks. | lambda/campaign_dialer.py |
| Agent Availability Lambda | Returns transfer-time agent availability and related metadata. | lambda/agent_availability.py |
| Amazon S3 | Stores campaign config.json and patients.csv; also stores Connect recordings for the base bot. | campaign_deployer.py / config.py |
| DynamoDB | Persists campaign state and patient state. | campaign_deployer.py + campaign_api/intake/dialer |
| EventBridge Scheduler | Schedules campaign start, pre-warm, and patient callback calls. | campaign_intake.py + campaign_dialer.py |
| EventBridge Rule | Listens for Amazon Connect DISCONNECTED events and invokes campaign_dialer. | campaign_deployer.py |


## 4. Complete System Architecture & Telephony Flow (from ARCHITECTURE.md)

```text
Outbound API call
    |
    v
Amazon Connect
    |
    +--> flow logging + stereo IVR/call recording
    |
    v
cara-health-bot-identity (Lex V2, 0.90 NLU confidence threshold)
    |
    +-- IdentityConfirmed ------------------------------+
    |                                                   |
    |                                                   v
    |                                       persist identityConfirmed=true
    |                                                   |
    |                                                   v
    |                                        transfer notice -> queue -> agent
    |
    +-- IdentityDenied / wrong number ---> goodbye -> disconnect
    |
    +-- ambiguous/fallback attempt 1 ---> one clarification
    |                                      |
    |                                      +-- confirmed -> human transfer
    |                                      +-- third party -> availability
    |                                      +-- otherwise -> goodbye
    |
    +-- ThirdPartyDetected
             |
             v
      cara-health-bot-availability (separate deterministic Lex V2)
             |
             +-- TargetAvailableNow
             |       |
             |       v
             |   persist THIRD_PARTY_THEN_TARGET
             |       |
             |       v
             |   "Please pass the phone to <name>."
             |       |
             |       v
             |   fresh identity check (up to 2 attempts)
             |       |
             |       +-- IdentityConfirmed -> human transfer
             |       +-- otherwise -> goodbye
             |
             +-- TargetUnavailable
             |       |
             |       +-- callbackDate : AMAZON.Date (required)
             |       +-- callbackTime : AMAZON.Time (required)
             |       |
             |       v
             |   save callback attributes -> thank relative -> disconnect
             |
             +-- AvailabilityUnknown / repeated fallback
                     |
                     v
                 generic goodbye -> disconnect
```

### 4.1 Why availability uses a separate Lex bot

A bare `yes` is deliberately ambiguous during identity confirmation, but in an availability question a `yes` should mean the intended customer is available now. Keeping availability in its own Lex bot prevents those meanings from competing inside one NLU model.

### 4.2 Privacy boundary

The relative/third-party branch is intentionally narrow. It does not disclose why the business is calling, any account or case details, or any sensitive information. It only asks whether the named intended customer can come to the phone and, if unavailable, a better callback day and time.

### 4.3 Human transfer

The flow resolves the STANDARD Connect queue configured by `humanTransferQueueName` and substitutes its ARN into `UpdateContactTargetQueue`. `TransferContactToQueue` is reachable only after `IdentityConfirmed` for the actual target, including after a third party passes the phone.

Deployment creates/reuses `CaraHealthBotQueue`, `CaraHealthBotRoutingProfile`, and `CaraHealthBotHumanAgent`, associates the queue for `VOICE`, and creates/reuses `caraagent` with `SOFT_PHONE`. The password is consumed from `CARA_AGENT_PASSWORD` only on first creation and is never persisted in project state.

### 4.4 Identity confidence hardening

The identity Lex locale uses a 0.90 NLU confidence threshold and explicitly trains question-like responses such as “may I know whom I am talking with?” as `IdentityAmbiguous`. The previously observed 0.78 false-positive `IdentityConfirmed` therefore falls below the locale threshold. This is still conversational identity confirmation, not strong authentication.

### 4.5 Recording/transcript

The flow records Agent + Customer participants and enables IVR recording. `scripts/transcript.py` uses the single Connect stereo recording and Amazon Transcribe to print `Cara:` and `Customer:` turns.


## 5. NLU Architecture & Multi-Bot Hierarchy (In Detail)

The Cara conversational architecture utilizes **three distinct Amazon Lex V2 bots**, each engineered for a specialized phase of the interaction.

```text
                                  ┌────────────────────────────────────────────────┐
                                  │                Outbound Call                   │
                                  └───────────────────────┬────────────────────────┘
                                                          │
                                                          ▼
                                  ┌────────────────────────────────────────────────┐
                                  │        cara-health-bot-identity (Lex V2)       │
                                  │   - 0.90 NLU Confidence Threshold              │
                                  │   - Full-name slot validation                  │
                                  │   - Questions treated as Ambiguous             │
                                  └──────┬─────────────────┬───────────────────────┘
                                         │                 │
                      ThirdPartyDetected │                 │ IdentityConfirmed
                                         ▼                 ▼
         ┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
         │     cara-health-bot-availability     │   │     cara-health-bot-conversation     │
         │   - Privacy-isolated relative gate   │   │   - Amazon Nova 2 Sonic Speech Model │
         │   - "Yes" = Target is available now  │   │   - Delegates to Amazon Q in Connect │
         │   - Slot elicitation: Date + Time    │   │   - 180+ Deterministic Safety NLU    │
         │   - Zero PHI disclosure boundary     │   │   - Zero-latency Rebuttal Intents    │
         └──────────────────────────────────────┘   └──────────────────────────────────────┘
```

---

### 5.1 Bot 1: Identity Lex Bot (`cara-health-bot-identity`)

- **Role:** Deterministic expected-customer identity confirmation gate before entering Q Connect session or routing to human transfer.
- **Locale & Voice:** `en_US`, Amazon Polly Neural voice (`Ruth` or configured `voice_id`).
- **NLU Intent Confidence Threshold:** **`0.90`** (Hardened).
- **Behavioral Rule:** Questions (e.g., *"Who is calling?"*, *"What is this about?"*) are strictly classified as `IdentityAmbiguous` rather than affirmations.

#### Complete Intent and Slot Catalog:

| Intent Name | Description / Sample Utterances | Slots | Routing Action |
| --- | --- | --- | --- |
| `IdentityConfirmed` | Caller confirms they are the intended customer: *"yes"*, *"speaking"*, *"this is me"*, *"that's me"*, *"you got me"*, *"this is he"*, *"this is she"*, *"yes I am"*, *"correct that is me"*, *"yep it's me"*, *"certainly this is me"*. | None | Persist `identityConfirmed=true`, route to human transfer queue or initialize Q coaching session. |
| `IdentityNamedConfirmation` | Caller confirms identity and explicitly states their full or first name: *"yes this is {firstName} {lastName}"*, *"I am {firstName} {lastName}"*, *"{firstName} {lastName} speaking"*, *"it's {firstName}"*. | `{firstName}` (`AMAZON.FirstName`, Optional)<br>`{lastName}` (`AMAZON.LastName`, Optional) | Contact Flow invokes `session_context.verifyIdentityName` to compare spoken names against patient record. |
| `IdentityDenied` | Caller denies being the target customer: *"no"*, *"this isn't John"*, *"wrong person"*, *"no you have the wrong person"*, *"I am not Sarah"*. | None | Set `identityResult=Denied`, play polite exit message, disconnect. |
| `IdentityAmbiguous` | Caller asks questions or gives non-committal response: *"who is this?"*, *"who is calling?"*, *"what is this regarding?"*, *"may I know whom I am talking with?"*, *"why are you calling?"*. | None | Plays 1 clarification prompt explaining the call's origin and allows a 2nd identity confirmation attempt. |
| `ThirdPartyDetected` | Relative, spouse, or caregiver answered: *"this is his wife"*, *"I'm her daughter"*, *"he is my husband"*, *"she's not by the phone right now"*, *"this is his caregiver"*. | None | Set `identityResult=ThirdParty`, transition flow to `cara-health-bot-availability`. |
| `PatientUnavailable` | Relative indicates target cannot come to phone: *"he is sleeping"*, *"she's at work"*, *"not home right now"*, *"he's in the hospital"*. | None | Route directly to third-party callback slot elicitation. |
| `WrongNumber` | Caller states this is an incorrect phone number: *"wrong number"*, *"nobody by that name here"*, *"you have the wrong number"*, *"no one here by that name"*. | None | Set `identityResult=Denied`, log wrong number, disconnect. |
| `RepresentativeDetected` | Caller states they are an authorized representative/POA: *"I am his medical power of attorney"*, *"I handle her healthcare decisions"*, *"I am his legal guardian"*. | None | Branch to representative handling or scheduled callback. |
| `Deceased` | Caller informs that the patient has passed away: *"he passed away"*, *"she died last month"*, *"the patient is deceased"*. | None | Set `disposition=DECEASED`, play condolences, hard disconnect with no future retries. |
| `CallRefusal` | Caller demands not to be called: *"don't call me"*, *"take me off your list"*, *"stop calling"*, *"not interested"*. | None | Set `disposition=REFUSED`, log DNC, disconnect. |
| `SafetyMedical` | Acute medical emergency during identity turn: *"having chest pain"*, *"can't breathe"*, *"call 911"*, *"bleeding heavily"*. | None | Static 911 prompt and immediate hard disconnect. |
| `SafetyBehavioral` | Acute psychiatric crisis during identity turn: *"want to hurt myself"*, *"feeling suicidal"*, *"crisis hotline"*. | None | Static 988 Suicide & Crisis Lifeline prompt and immediate hard disconnect. |
| `FallbackIntent` | Unrecognized audio, background noise, or silence. | None | Triggers clarification attempt before exiting gracefully. |

---

### 5.2 Bot 2: Availability Lex Bot (`cara-health-bot-availability`)

- **Role:** Handles relative/third-party conversations to determine whether the patient is present to take the phone or collect callback details.
- **Privacy Boundary:** The bot **never discloses** medical information, diagnosis details, or coaching topics.
- **NLU Intent Confidence Threshold:** `0.70`.

#### Complete Intent and Slot Catalog:

| Intent Name | Description / Sample Utterances | Slots | Routing Action |
| --- | --- | --- | --- |
| `TargetAvailableNow` | Relative confirms patient can take phone: *"yes"*, *"yeah"*, *"one moment"*, *"let me get him"*, *"she's right here"*, *"hold on please"*, *"sure I'll pass the phone"*. | None | Plays *"Please pass the phone to [Name]"*, pauses, then executes a **fresh identity check** (up to 2 attempts). |
| `TargetUnavailable` | Relative indicates patient cannot talk: *"he's not available"*, *"she's at work"*, *"call back tomorrow"*, *"he's asleep"*, *"not right now"*. | `callbackDate`: `AMAZON.Date` (Optional)<br>`callbackTime`: `AMAZON.Time` (Optional) | Elicits callback date and time slots, stores attributes on contact, thanks relative, and disconnects. |
| `AvailabilityUnknown` | Relative is unsure or non-committal: *"I don't know"*, *"not sure"*, *"maybe later"*, *"hard to say"*. | None | Plays generic callback notice, sets disposition `THIRD_PARTY_UNAVAILABLE`, disconnects. |
| `RepresentativeWillingToProceed` | Relative wants to speak on patient's behalf: *"I can speak for him"*, *"I handle his medical care"*, *"you can talk to me"*. | None | Routes to authorized representative transfer path. |
| `WrongNumber` | Explicit wrong-number statement made to availability bot: *"wrong number"*, *"they don't live here"*, *"no John here"*. | None | Directly exits to `identityResult=Denied` wrong-number cleanup (v3.2.2 hardening). |
| `Deceased` | Relative reports patient is deceased. | None | Plays condolence prompt, marks patient terminal. |
| `SafetyMedical` / `SafetyBehavioral` | Emergency phrases during availability turn. | None | Immediate 911 / 988 emergency exit and hard disconnect. |
| `FallbackIntent` | Unrecognized relative response. | None | Reprompts once, then closes politely. |

---

### 5.3 Bot 3: Conversational / Coaching Lex Bot (`cara-health-bot-conversation`)

- **Role:** Voice entry point for confirmed patients. Powered by Amazon Nova 2 Sonic speech model and integrated with Amazon Q in Connect for generative coaching dialogue.
- **Speech Model:** `us.amazon.nova-2-sonic-v1:0` (Unified Speech Settings with native bidirectional audio).
- **NLU Intent Confidence Threshold:** `0.70`.

#### Complete Intent and Slot Catalog:

| Intent Name | Purpose & Utterances | Mechanism | Routing / Consequence |
| --- | --- | --- | --- |
| `AmazonQinConnect` | Delegates multi-turn coaching dialogue to Amazon Q in Connect. | `qInConnectIntentConfiguration` pointing to Assistant ARN. | Q in Connect orchestrates response via Amazon Nova Lite. |
| `SafetyMedical` | Hard deterministic safety filter for acute medical emergencies (180+ utterances: *"chest pain"*, *"shortness of breath"*, *"stroke symptoms"*, *"bleeding"*, *"passed out"*, *"calling 911"*). | Direct Lex Intent Matching (Zero LLM latency) | Connect flow catches `SafetyMedical`, plays static emergency message, hard disconnects. |
| `SafetyBehavioral` | Hard deterministic safety filter for acute psychiatric/suicide crises (180+ utterances: *"kill myself"*, *"suicidal"*, *"end it all"*, *"want to die"*, *"overdose"*). | Direct Lex Intent Matching (Zero LLM latency) | Connect flow catches `SafetyBehavioral`, plays static 988 Lifeline prompt, hard disconnects. |
| `CallRefusal` | Catches explicit refusals or DNC demands during coaching: *"stop calling"*, *"take me off your list"*, *"I refuse"*, *"do not call again"*. | Direct Lex Intent Matching | Flow routes to refusal wrap-up and logs `disposition=REFUSED`. |
| `RebuttalReasonForCall` | Deterministic objection handler for *"why are you calling?"*, *"what is this about?"*, *"what is CareHarmony?"*. | Direct Lex Intent Matching | Plays configured practice benefit message, reprompts engagement. |
| `RebuttalWhoAreYou` | Deterministic objection handler for *"who are you?"*, *"are you a robot?"*, *"are you an AI?"*. | Direct Lex Intent Matching | Plays transparent AI disclosure (*"I'm Cara, an AI health assistant calling on behalf of [Practice]..."*). |
| `FallbackIntent` | Unrecognized dialogue turns. | Delegated to Q | Handled via Q orchestration fallback logic. |


## 6. Base Cara deployment vs. campaign runtime

The project has two related layers. The base Cara deployment provisions the Connect/Lex/Q/session-context infrastructure and publishes the Contact Flow. The campaign add-on then reuses those resources instead of creating another Connect instance or another conversational flow.

| Layer | Creates / configures | Does not own |
| --- | --- | --- |
| Base Cara | Connect instance, source phone, Q assistant, Q prompt, Q orchestration agent, identity Lex, availability Lex, conversation Lex, session-context Lambda, human queue/agent, Contact Flow, logging/recording. | Campaign-specific S3/DynamoDB/Scheduler/EventBridge resources. |
| Campaign add-on | Campaign S3 bucket, campaign DynamoDB tables, campaign API/intake/dialer/availability Lambdas, Scheduler entries, Connect DISCONNECTED EventBridge rule. | A second Connect instance or replacement conversational flow. |


## 7. Configuration flow

config.json is the central configuration source for the base Cara deployment. config.py converts the JSON into ProjectConfig. The prompt path is prompts/life-coach.yaml and the Contact Flow path is contact-flows/cara-health-bot-flow.json.

```text
config.json
   │
   ├── model / voice / Lex names
   ├── human-transfer configuration
   ├── caraBehavior
   └── retry configuration
        │
        ▼
cara_health_bot/config.py
        │
        ▼
ProjectConfig
        │
        ├──────────────► builders.py
        │                  ├─ Lex requests
        │                  ├─ Q prompt request
        │                  ├─ Q tools
        │                  └─ Contact Flow rendering
        │
        └──────────────► deployer.py
                           └─ AWS provisioning/update/verification
```


## 8. Campaign API data insertion

campaign_api._create_upload creates the campaign record before the CSV is uploaded. It also stores a campaign-level config.json in S3. The schedule is kept in both the user's local representation and a UTC representation.

| Data | Where | Meaning |
| --- | --- | --- |
| campaignId / batchId | DynamoDB + S3 path | Unique campaign identifier. |
| status=UPLOAD_PENDING | Campaign table | Campaign exists but patient CSV has not yet been processed. |
| scheduledAt | DynamoDB + S3 config | Normalized local wall-clock schedule. |
| scheduledFor / callTime | DynamoDB + S3 config | UTC execution time. |
| timezone | DynamoDB + S3 config | IANA timezone supplied by UI. |
| s3Key | Campaign table | campaigns/<campaignId>/patients.csv. |
| directAgentEnabled | DynamoDB + S3 config | Campaign-level direct-agent mode. |
| humanAgentPhoneNumber | DynamoDB + S3 config | Human agent phone used for campaign transfer. |


## 9. CSV upload and campaign intake

```text
S3
  campaigns/<campaignId>/patients.csv
             │
             │ ObjectCreated
             ▼
campaign_intake.handler
             │
             ├─ derive campaignId from S3 key
             ├─ load campaigns/<campaignId>/config.json
             ├─ resolve call time
             ├─ parse CSV
             ├─ validate required fields
             ├─ normalize phone numbers
             ├─ reject duplicate EMPI / phone values
             ├─ build patient records
             │
             ├──────────────► DynamoDB campaign row
             │
             └──────────────► DynamoDB patient rows
                                      │
                                      ▼
                              status = PENDING
                                      │
                                      ▼
                            EventBridge Scheduler
                             campaign-start + prewarm
```

The required frontend CSV fields are empi, first_name, last_name, gender, phone_number, practice_name, and practice_callback_number. provider_name is optional.


## 10. DynamoDB data model

The deployed campaign code uses two DynamoDB tables. The CampaignNames defaults are TalkingBotCallBatches-dev and TalkingBotPatientRecords-dev. This is the authoritative naming in campaign_deployer.py; CAMPAIGN.md contains a different descriptive table name, so operators should use the deployed resource names from deployment-state/output.

| Table | Key | Important fields |
| --- | --- | --- |
| TalkingBotCallBatches-dev | PK: batchId | status, totalRows, validRows, patientCount, scheduledAt, scheduledFor, timezone, s3Bucket, s3Key, startedAt, completedAt |
| TalkingBotPatientRecords-dev | PK: patientId + batchId | rowNumber, status, attemptCount, names, EMPI, phoneNumber, practice/provider data, callMode, contactId, identityResult, disposition, callback data |

Patient records are created with deterministic row IDs such as:

`patientId = <campaignId>#row-<sequence>`


## 11. Campaign scheduler creation

After intake successfully writes campaign and patient state, it creates two one-time EventBridge Scheduler entries targeting the campaign_dialer Lambda.

| Schedule | Trigger payload | Purpose |
| --- | --- | --- |
| campaign-start | {"trigger":"campaign-start","campaignId":"..."} | Starts the first pending patient call at the campaign's UTC start time. |
| prewarm | {"trigger":"prewarm","campaignId":"..."} | Runs approximately 60 seconds before the call to warm the availability Lambda when possible. |


## 12. Sequential dialing logic

```text
EventBridge Scheduler
        │
        ▼
campaign_dialer.handler
        │
        ▼
_start_next_call(campaignId)
        │
        ├─ atomically set campaign PENDING/RUNNING
        │
        ├─ check for existing IN_PROGRESS patient
        │
        ├─ find first PENDING patient
        │
        ├─ atomically claim patient:
        │      PENDING -> IN_PROGRESS
        │      attemptCount + 1
        │
        ├─ optionally pre-warm availability Lambda
        │
        └─ _place_call()
                  │
                  ▼
          Connect StartOutboundVoiceContact
                  │
                  ▼
             contactId returned
                  │
                  ▼
        save contactId to patient row
```

The conditional DynamoDB updates are the key idempotency mechanism: only one invocation can successfully claim a PENDING patient, and an existing IN_PROGRESS patient prevents a duplicate campaign start from dialing another patient.


## 13. What data is injected into Amazon Connect

campaign_dialer._place_call builds Connect Attributes from the patient/campaign record. These attributes become the runtime data used by the Contact Flow.

| Connect attribute | Source | Used for |
| --- | --- | --- |
| campaignId | Campaign | Campaign correlation. |
| patientId | Patient | Patient correlation. |
| customerName | First + last name | Identity prompts. |
| expectedPhone | Patient phone | Expected destination context. |
| empi | Patient | Patient identity / availability context. |
| firstName / lastName | Patient | Prompts and identity checks. |
| practiceName | Patient | Opening and representative messaging. |
| providerName | Patient | Opening script when available. |
| practiceCallbackNumber | Patient | Voicemail script. |
| humanAgentPhoneNumber | Campaign / patient / fallback | Transfer target. |
| identityPrompt / identityClarification | Derived from patient | Identity Lex prompt. |
| coachingGreeting | Derived from patient | Main conversational Lex opening. |
| callMode / direct_agent | Campaign configuration | Routing mode. |


## 14. Amazon Connect Contact Flow runtime

The published flow is a fixed state machine. render_contact_flow() does not build the transitions dynamically; it reads the JSON template and replaces placeholders such as Lex alias ARNs, Q assistant IDs, Lambda ARN, queue ARN, messages, and configured behavior.

```text
Outbound contact starts
        │
        ▼
Flow logging / recording
        │
        ▼
Identity Lex #1
        │
        ├── IdentityConfirmed ───────────────┐
        │                                     │
        ├── IdentityDenied / WrongNumber     │
        │        └── goodbye -> disconnect    │
        │                                     │
        ├── IdentityAmbiguous / fallback     │
        │        └── clarification / retry    │
        │                                     │
        └── ThirdPartyDetected                │
                 │                            │
                 ▼                            │
          Availability Lex                   │
                 │                            │
          ┌──────┴─────────┐                  │
          ▼                ▼                  │
     TargetAvailable   TargetUnavailable     │
          │                │                 │
          ▼                ▼                 │
     Fresh identity    callback date/time    │
          │                │                 │
          └───────┬────────┴─────────────────┘
                  ▼
          Session-context Lambda
                  │
                  ▼
             Main Q/Lex
                  │
        ┌─────────┼───────────────┐
        ▼         ▼               ▼
     Answer    Callback       Human transfer
        │         │               │
        └─────────┴───────────────┘
                  │
                  ▼
               End call
```


## 15. Agent availability API call and transfer-time resolution

Agent availability is checked at transfer time through the session-context Lambda. The Contact Flow invokes session_context with operation=checkAgentAvailability and passes the current patientId and EMPI.

```text
Contact Flow
      │
      ▼
session_context Lambda
      │
      ├─ operation = checkAgentAvailability
      ├─ patientId
      └─ empi
      │
      ▼
Agent Availability service
      │
      ├─ availability status
      ├─ agentPhone
      ├─ agentId
      ├─ agentName
      └─ checkedAt
      │
      ▼
session_context resolves the transfer phone
      │
      ▼
Contact Flow receives $.External.*
      │
      ▼
available == true ?
   │              │
  yes             no
   │              │
   ▼              ▼
store agent      callback /
details           unavailable path
```

The availability call has two supported invocation paths. The primary path invokes the configured agent-availability Lambda synchronously using the AWS Lambda SDK with RequestResponse. If that path fails, session_context falls back to the configured Lambda Function URL over HTTP.

Inputs:
- patientId
- empi

Response:
- available
- agentPhone
- agentId
- agentName
- agentCheckedAt

The Contact Flow then stores the returned values as contact attributes:
humanAgentPhoneNumber, agentAvailabilityCheckedAt, agentId, and agentName.

The transfer phone is resolved with the configured UI/campaign humanAgentPhoneNumber taking priority, followed by the availability API's agentPhone and finally the configured fallback phone. This keeps the transfer target aligned with the campaign configuration while still allowing a live availability lookup.


## 16. Human agent transfer and data handoff

Once the customer has given consent to speak with a human and the availability check confirms an available representative, the Contact Flow prepares the transfer.

```text
Customer
   │
   ▼
Consent / EscalateToHuman
   │
   ▼
Check agent availability
   │
   ▼
Update Connect contact attributes
   │
   ├─ humanAgentPhoneNumber
   ├─ agentId
   ├─ agentName
   └─ agentAvailabilityCheckedAt
   │
   ▼
TransferParticipantToThirdParty
   │
   ▼
Human representative
   │
   ▼
Transfer outcome evaluation
   │
   ▼
Continue / finalize / callback handling
```

The patient and campaign context is carried through the Amazon Connect contact as contact attributes. The relevant context established earlier in the call includes campaignId, patientId, EMPI, customerName, firstName, lastName, practiceName, providerName, expectedPhone, callMode, and the human-agent routing information.

Immediately before transfer, the flow also:
1. stores the resolved representative phone and availability metadata on the current contact;
2. sets CallerIdNumber from the customer's endpoint;
3. plays the handoff message using the patient's first name and practice name;
4. transfers the participant to humanAgentPhoneNumber using TransferParticipantToThirdParty with a 25-second connection limit and ContinueFlowExecution enabled.

The transfer therefore carries both the live routing information and the established call context in the Connect contact. The contact attributes remain available to the downstream flow and post-call processing.

After the transfer attempt, the session-context Lambda's evaluateTransferOutcome operation examines the associated contacts for the current Connect contact, identifies the latest EXTERNAL_OUTBOUND transfer leg, and evaluates that child contact. The result is returned as transferOutcome together with transferDurationSeconds, childContactId, and transferEvalReason. This information is then used by the Contact Flow to determine whether the representative connected or whether the transfer needs the configured no-pickup/callback handling.


## 17. Session-context Lambda: why it is called

After identity confirmation, the Contact Flow creates/uses an Amazon Q session and invokes the session-context Lambda with operation=initialize. This Lambda copies trusted conversation context into the Q Connect session.

```text
Connect Contact Flow
       │
       │ operation=initialize
       │ assistantId
       │ sessionArn
       │ customerName
       │ expectedPhone
       │ direct_agent
       │ practiceName
       │ firstName
       │ providerName
       ▼
session_context.handler
       │
       ├─ validate session ARN
       ├─ verify session belongs to configured assistant
       ├─ build Custom session data
       │
       └─ qconnect.update_session_data(...)
                  │
                  ▼
             Amazon Q session
```

| Q Custom session key | Value |
| --- | --- |
| customerName | Confirmed customer name |
| expectedPhone | Expected destination phone |
| identityPolicyVersion | v7-full-name-validated |
| identityConfirmed | true |
| conversationState | PATIENT_CONFIRMED |
| direct_agent | yes/no |
| practiceName | When available |
| providerName | When available |
| firstName | When available |


## 18. Other session-context Lambda operations

| operation | Purpose | Result |
| --- | --- | --- |
| initialize | Persist verified identity/context into Amazon Q session. | contextStatus=READY |
| checkAgentAvailability | Fetch live availability and resolve transfer phone. | available, agentPhone, agentId, agentName, agentCheckedAt |
| verifyIdentityName | Compare expected customer name with spoken first/last name. | identityMatch |
| recognize_text | Run Lex recognize_text for debugging/evaluation. | matchedIntent, interpretations, sessionState |
| evaluateTransferOutcome | Inspect associated external Connect transfer leg. | transferOutcome, duration, child contact, disconnect reason |


## 19. Amazon Q in Connect conversational layer

The conversation Lex bot contains an AmazonQinConnect intent. That intent delegates the conversational turn to the Q orchestration agent. The Q prompt is generated from prompts/life-coach.yaml and receives caraBehavior JSON through the ${CaraBehaviorConfig} placeholder.

```text
life-coach.yaml
      │
      │ ${CaraBehaviorConfig}
      ▼
render_cara_prompt()
      │
      ▼
q_prompt_create_request()
      │
      │ modelId = us.amazon.nova-lite-v1:0
      │ apiFormat = MESSAGES
      ▼
Amazon Q orchestration prompt
      │
      ▼
Q orchestration agent
      │
      ├─ answer approved questions
      ├─ handle objections
      ├─ request callback
      ├─ escalate to human
      ├─ end conversation
      └─ safety handling
```


## 20. Q return-to-control tools

| Tool | When it should be used | Connect consequence |
| --- | --- | --- |
| EscalateToHuman | Only after clear affirmative consent to speak with a human. | Contact Flow reads Tool=EscalateToHuman and proceeds to transfer path. |
| RequestCallback | Customer is busy/not available now or requests a future callback time. | Contact Flow stores callback state; campaign dialer may later schedule the callback. |
| EndConversation | Refusal, do-not-call, safety, or other valid conversation-ending reason. | Contact Flow stores conversationState=END and caraEndReason, then plays appropriate closing. |

A critical implementation rule is that speaking a sentence is not the same as invoking a return-to-control tool. For example, saying 'Understood, thank you...' does not itself terminate the Contact Flow; EndConversation must be returned so the flow can execute the END branch.


## 21. Connect DISCONNECTED -> campaign_dialer

The campaign deployer creates an EventBridge rule filtered to Amazon Connect DISCONNECTED events for the Cara instance. The rule targets campaign_dialer.

```text
Amazon Connect
      │
      │ DISCONNECTED
      ▼
EventBridge Rule
  source = aws.connect
  eventType = DISCONNECTED
  instanceArn = Cara instance
      │
      ▼
campaign_dialer.handler
      │
      ▼
lookup patient by contactId-index
      │
      ▼
DescribeContact
      │
      ▼
classify outcome
```


## 22. How the dialer maps a call result

| Signal / attribute | Typical classification | Next action |
| --- | --- | --- |
| identityResult=Confirmed | Identity Confirmed | Finalize patient and start next patient. |
| identityResult=Denied | Wrong Person / Identity Denied | Finalize patient; no normal retry. |
| identityResult=Ambiguous | Identity Unclear | Finalize according to current classification. |
| caraEndReason/refusal or NOT_INTERESTED | Call Refused | Finalize as refusal. |
| Voicemail / voicemail AMD | Voicemail Left | Schedule retry/callback according to retry policy. |
| No answer / telecom not connected | No Answer / Not Connected | Schedule default 24-hour retry when allowed. |
| AMD unresolved | AMD_TIMEOUT_UNRESOLVED | Schedule retry or exhaust after maximum attempts. |
| Callback requested | Callback Scheduled | Create one-time Scheduler entry for callback. |


## 23. Callback scheduling lifecycle

```text
During call
   │
   ├─ patient asks for callback
   │       OR
   └─ third party gives callback date/time
            │
            ▼
     Connect attributes / Lex session state
            │
            ▼
     campaign_dialer reads contact
            │
            ▼
       _callback_plan()
            │
            ▼
   _create_callback_schedule()
            │
            ▼
   EventBridge Scheduler
     patient-callback
            │
            ▼
   campaign_dialer.handler
            │
            ▼
   _start_callback_call()
            │
            ├─ ensure no other patient IN_PROGRESS
            ├─ claim CALLBACK_SCHEDULED patient
            └─ StartOutboundVoiceContact
```

The scheduler target is the same campaign_dialer Lambda, preserving the single-patient-at-a-time behavior.


## 24. Finalization and campaign completion

```text
DISCONNECTED
   │
   ▼
lookup patient by contactId
   │
   ▼
classify contact
   │
   ├─ callback needed ──► CALLBACK_SCHEDULED
   │                         │
   │                         └─ Scheduler entry
   │
   └─ terminal result ───► COMPLETED / terminal status
                              │
                              ▼
                       start_next_call()
                              │
                              ▼
                    any PENDING/IN_PROGRESS/
                    CALLBACK_SCHEDULED left?
                       │             │
                      yes            no
                       │             │
                  next patient       ▼
                                campaign COMPLETED
```


## 25. Idempotency and race protection

- Campaign state uses conditional DynamoDB updates to move PENDING -> RUNNING.
- A patient is atomically claimed with PENDING -> IN_PROGRESS.
- A duplicate campaign-start event cannot claim a second patient while one is already IN_PROGRESS.
- Finalization is conditionally performed from IN_PROGRESS, so duplicate DISCONNECTED events are ignored after completion.
- Patient lookup normally uses contactId-index instead of scanning the entire table.
- Callback scheduling is conditional on the patient still being CALLBACK_SCHEDULED.
- Setup failures are bounded per Lambda invocation and can trigger asynchronous continuation.
These controls are important because EventBridge, Lambda retries, Connect events, and duplicate notifications can all produce repeated invocations.


## 26. Recording and transcript flow

```text
Amazon Connect
    │
    ├─ IVR recording enabled
    ├─ Agent + Customer participants recorded
    └─ stereo recording stored in Cara S3 recording bucket
              │
              ▼
scripts/transcript.py
              │
              └─ retrieves recording metadata / audio
                 and uses Amazon Transcribe to print:
                    Cara: ...
                    Customer: ...
```

The transcript helper supports JSON output, presigned URLs, and a dialogue-only mode. This is particularly useful when debugging a live conversation against CloudWatch Contact Flow and Lex logs.


## 27. Debugging playbook

| Symptom | First place to inspect | What to verify |
| --- | --- | --- |
| Call never starts | campaign_dialer + DynamoDB | Campaign status, patient status, Scheduler trigger, StartOutboundVoiceContact errors. |
| CSV upload does nothing | S3 notification + campaign_intake logs | Object key exactly campaigns/<id>/patients.csv; Lambda permission; validation errors. |
| Identity is wrong | Identity Lex logs + Connect events | Intent, confidence, threshold (0.90), IdentityConfirmed/Denied/Ambiguous result. |
| Q answers incorrectly | life-coach.yaml + Q prompt/version + Lex conversation logs | Prompt version, session context, intent, model response/tool selection. |
| Empty/invalid SSML | Lex conversation log + Q response | x-amz-lex:q-in-connect-response; whether Q returned empty text. |
| Refusal does not terminate | Q tool output + Contact Flow session attributes | Tool=EndConversation and endReason=refusal. |
| Human transfer fails | session_context availability + Connect transfer branch | available flag, agent phone, queue, external leg. |
| Next patient does not start | campaign_dialer DISCONNECTED event | contactId lookup, classification, finalization, remaining PENDING rows. |
| Callback not firing | DynamoDB + EventBridge Scheduler | CALLBACK_SCHEDULED state, schedule name/time, target payload. |


## 28. Source files used

- ARCHITECTURE.md
- CAMPAIGN.md
- README.md
- LESSONS_AND_REGRESSIONS.md
- config.json
- cara_health_bot/config.py
- cara_health_bot/builders.py
- cara_health_bot/deployer.py
- cara_health_bot/campaign_deployer.py
- contact-flows/cara-health-bot-flow.json
- prompts/life-coach.yaml
- lambda/campaign_api.py
- lambda/campaign_intake.py
- lambda/campaign_dialer.py
- lambda/session_context.py
- lambda/agent_availability.py
- scripts/transcript.py
- scripts/verify_live_flow.py

Note: This document describes the behavior supported by the supplied backend source. Where a frontend implementation was not included in the supplied backend package, the frontend portion is described from the documented API contract.
