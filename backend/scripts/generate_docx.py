"""
Script to generate the comprehensive, publication-grade Cara System Documentation DOCX.
Includes all architecture walkthroughs, 4 embedded diagrams, Lambda & DynamoDB references,
state lifecycle diagrams, recent fixes, and glossary.
"""

import os
import sys
from datetime import datetime
import docx
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn

# --- Color Palette Constants ---
NAVY_HEX = "1E3A8A"        # Deep Navy
SLATE_HEX = "334155"       # Dark Slate
LIGHT_BG_HEX = "F8FAFC"    # Subtle background
BORDER_HEX = "CBD5E1"      # Subtle border
HEADER_BG_HEX = "1E3A8A"   # Table header background
CALLOUT_BG_HEX = "EFF6FF"  # Light blue callout
CALLOUT_BORDER = "2563EB"  # Blue accent
WARNING_BG_HEX = "FFFBEB"  # Light amber
WARNING_BORDER = "D97706"  # Amber accent

NAVY_RGB = RGBColor(30, 58, 138)
SLATE_RGB = RGBColor(51, 65, 85)
MUTED_RGB = RGBColor(100, 116, 139)
WHITE_RGB = RGBColor(255, 255, 255)


def set_cell_shading(cell, color_hex):
    """Apply background color to a table cell."""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{color_hex}"/>')
    tcPr.append(shd)


def set_cell_margins(cell, top=140, bottom=140, left=180, right=180):
    """Set inner padding for a table cell in dxa (1 pt = 20 dxa)."""
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        f'<w:top w:w="{top}" w:type="dxa"/>'
        f'<w:bottom w:w="{bottom}" w:type="dxa"/>'
        f'<w:left w:w="{left}" w:type="dxa"/>'
        f'<w:right w:w="{right}" w:type="dxa"/>'
        f'</w:tcMar>'
    )
    tcPr.append(tcMar)


def set_table_borders(table, border_color=BORDER_HEX):
    """Apply clean horizontal borders to a table."""
    tblPr = table._tbl.tblPr
    tblBorders = parse_xml(
        f'<w:tblBorders {nsdecls("w")}>'
        f'<w:top w:val="single" w:sz="6" w:space="0" w:color="{border_color}"/>'
        f'<w:bottom w:val="single" w:sz="6" w:space="0" w:color="{border_color}"/>'
        f'<w:insideH w:val="single" w:sz="4" w:space="0" w:color="{border_color}"/>'
        f'<w:left w:val="none"/>'
        f'<w:right w:val="none"/>'
        f'<w:insideV w:val="none"/>'
        f'</w:tblBorders>'
    )
    tblPr.append(tblBorders)


def add_callout(doc, text, title="NOTE", callout_type="info"):
    """Add a professional styled callout box with a thick left border."""
    bg_color = CALLOUT_BG_HEX if callout_type == "info" else WARNING_BG_HEX
    border_color = CALLOUT_BORDER if callout_type == "info" else WARNING_BORDER
    title_color = NAVY_RGB if callout_type == "info" else RGBColor(180, 83, 9)

    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    cell = tbl.cell(0, 0)
    cell.width = Inches(6.8)

    # Set cell shading
    set_cell_shading(cell, bg_color)
    set_cell_margins(cell, top=140, bottom=140, left=200, right=180)

    # Set left border only
    tcPr = cell._tc.get_or_add_tcPr()
    borders = parse_xml(
        f'<w:tcBorders {nsdecls("w")}>'
        f'<w:top w:val="none"/>'
        f'<w:bottom w:val="none"/>'
        f'<w:right w:val="none"/>'
        f'<w:left w:val="single" w:sz="24" w:space="0" w:color="{border_color}"/>'
        f'</w:tcBorders>'
    )
    tcPr.append(borders)

    # Content
    p = cell.paragraphs[0]
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.15
    run_title = p.add_run(f"[{title}] ")
    run_title.bold = True
    run_title.font.name = "Calibri"
    run_title.font.size = Pt(10)
    run_title.font.color.rgb = title_color

    run_text = p.add_run(text)
    run_text.font.name = "Calibri"
    run_text.font.size = Pt(9.5)
    run_text.font.color.rgb = SLATE_RGB

    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def add_styled_heading(doc, text, level):
    """Add a clean, beautifully styled heading with appropriate spacing."""
    p = doc.add_heading(text, level=level)
    p.paragraph_format.keep_with_next = True
    run = p.runs[0]
    run.font.name = "Calibri"
    
    if level == 1:
        run.font.size = Pt(18)
        run.font.color.rgb = NAVY_RGB
        run.bold = True
        p.paragraph_format.space_before = Pt(18)
        p.paragraph_format.space_after = Pt(6)
    elif level == 2:
        run.font.size = Pt(14)
        run.font.color.rgb = RGBColor(30, 64, 175)
        run.bold = True
        p.paragraph_format.space_before = Pt(14)
        p.paragraph_format.space_after = Pt(4)
    elif level == 3:
        run.font.size = Pt(11.5)
        run.font.color.rgb = SLATE_RGB
        run.bold = True
        p.paragraph_format.space_before = Pt(10)
        p.paragraph_format.space_after = Pt(2)
    return p


def add_body_paragraph(doc, text, bold_prefix=None, space_after=6):
    """Add body text with Calibri 10.5pt and slate color."""
    p = doc.add_paragraph()
    p.paragraph_format.line_spacing = 1.15
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(0)
    
    if bold_prefix:
        r_prefix = p.add_run(bold_prefix)
        r_prefix.bold = True
        r_prefix.font.name = "Calibri"
        r_prefix.font.size = Pt(10.5)
        r_prefix.font.color.rgb = SLATE_RGB

    r_text = p.add_run(text)
    r_text.font.name = "Calibri"
    r_text.font.size = Pt(10.5)
    r_text.font.color.rgb = SLATE_RGB
    return p


def add_bullet_item(doc, text, bold_prefix=None):
    """Add bullet list item with tight spacing."""
    p = doc.add_paragraph(style='List Bullet')
    p.paragraph_format.line_spacing = 1.15
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.space_before = Pt(0)

    if bold_prefix:
        r_prefix = p.add_run(bold_prefix)
        r_prefix.bold = True
        r_prefix.font.name = "Calibri"
        r_prefix.font.size = Pt(10)
        r_prefix.font.color.rgb = SLATE_RGB

    r_text = p.add_run(text)
    r_text.font.name = "Calibri"
    r_text.font.size = Pt(10)
    r_text.font.color.rgb = SLATE_RGB
    return p


def add_diagram_image(doc, image_path, caption):
    """Embed an image centered with full width and a styled caption."""
    if not os.path.exists(image_path):
        p_err = doc.add_paragraph(f"[Image missing: {image_path}]")
        p_err.runs[0].font.color.rgb = RGBColor(220, 38, 38)
        return

    p_img = doc.add_paragraph()
    p_img.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_img.paragraph_format.space_before = Pt(8)
    p_img.paragraph_format.space_after = Pt(4)
    run_img = p_img.add_run()
    run_img.add_picture(image_path, width=Inches(6.8))

    p_cap = doc.add_paragraph()
    p_cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_cap.paragraph_format.space_before = Pt(2)
    p_cap.paragraph_format.space_after = Pt(12)
    p_cap.paragraph_format.keep_with_next = True
    r_cap = p_cap.add_run(f"Figure: {caption}")
    r_cap.italic = True
    r_cap.font.name = "Calibri"
    r_cap.font.size = Pt(9)
    r_cap.font.color.rgb = MUTED_RGB


def format_table_headers(table, col_widths, headers):
    """Format table header row with Navy background, white bold text, and column widths."""
    hdr_cells = table.rows[0].cells
    for i, header_text in enumerate(headers):
        hdr_cells[i].text = header_text
        set_cell_shading(hdr_cells[i], HEADER_BG_HEX)
        set_cell_margins(hdr_cells[i], top=120, bottom=120, left=140, right=140)
        hdr_cells[i].width = Inches(col_widths[i])
        
        p = hdr_cells[i].paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.space_before = Pt(0)
        for run in p.runs:
            run.font.name = "Calibri"
            run.font.size = Pt(9.5)
            run.font.color.rgb = WHITE_RGB
            run.bold = True


def format_table_row(row, col_widths, values, is_even=False):
    """Format data rows with alternating shading and clean typography."""
    cells = row.cells
    bg_color = LIGHT_BG_HEX if is_even else "FFFFFF"
    for i, val in enumerate(values):
        cells[i].text = str(val)
        set_cell_shading(cells[i], bg_color)
        set_cell_margins(cells[i], top=100, bottom=100, left=140, right=140)
        cells[i].width = Inches(col_widths[i])
        
        p = cells[i].paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.line_spacing = 1.15
        for run in p.runs:
            run.font.name = "Calibri"
            run.font.size = Pt(9)
            run.font.color.rgb = SLATE_RGB


def build_documentation_docx(output_path, docs_dir):
    """Generate the full documentation document."""
    doc = Document()

    # Set page margins to 0.75 inches for clean professional density
    sections = doc.sections
    for section in sections:
        section.top_margin = Inches(0.75)
        section.bottom_margin = Inches(0.75)
        section.left_margin = Inches(0.75)
        section.right_margin = Inches(0.75)

    # Document Header / Title
    p_title = doc.add_paragraph()
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_title.paragraph_format.space_before = Pt(12)
    p_title.paragraph_format.space_after = Pt(2)
    r_title = p_title.add_run("CAREHARMONY CARA")
    r_title.bold = True
    r_title.font.name = "Calibri"
    r_title.font.size = Pt(24)
    r_title.font.color.rgb = NAVY_RGB

    p_sub = doc.add_paragraph()
    p_sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_sub.paragraph_format.space_before = Pt(0)
    p_sub.paragraph_format.space_after = Pt(8)
    r_sub = p_sub.add_run("Autonomous Outbound Clinical Coaching & Call Management Architecture")
    r_sub.font.name = "Calibri"
    r_sub.font.size = Pt(13)
    r_sub.font.color.rgb = RGBColor(13, 148, 136) # Teal

    p_meta = doc.add_paragraph()
    p_meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_meta.paragraph_format.space_after = Pt(18)
    r_meta = p_meta.add_run(f"Comprehensive Technical Reference & Operational Specification | Published {datetime.now().strftime('%B %Y')}")
    r_meta.font.name = "Calibri"
    r_meta.font.size = Pt(9.5)
    r_meta.italic = True
    r_meta.font.color.rgb = MUTED_RGB

    # -------------------------------------------------------------
    # SECTION 1: EXECUTIVE SUMMARY
    # -------------------------------------------------------------
    add_styled_heading(doc, "1. Executive Summary & System Overview", level=1)
    
    add_body_paragraph(
        doc,
        "The Careharmony Cara platform is an enterprise-grade, autonomous voice AI platform engineered "
        "to conduct proactive, high-touch clinical coaching and chronic care management (CCM) outreach to patients. "
        "Built entirely on a serverless, HIPAA-compliant AWS infrastructure, Cara bridges the critical engagement gap "
        "between scheduled clinic appointments. The system handles large patient cohorts by executing outbound conversational "
        "telephone outreach, verifying patient identity in strict adherence with healthcare privacy standards, delivering personalized "
        "motivational health coaching, detecting medical or behavioral red flags, and dynamically escalating calls to licensed clinical "
        "staff when necessary."
    )

    add_callout(
        doc,
        "Cara is designed with clinical safety as the paramount architectural constraint. Unlike conventional conversational AI "
        "systems that rely purely on unstructured language models, Cara combines deterministic state tracking, strict Lex V2 intent "
        "classification, and Amazon Bedrock foundation models (Amazon Nova Lite). Acute medical emergencies (e.g., chest pain, stroke "
        "symptoms) and acute behavioral crises trigger deterministic hard exits directly to 911 / 988 emergency guidance, bypassing generative models.",
        title="CLINICAL SAFETY PRINCIPLE",
        callout_type="warning"
    )

    add_body_paragraph(
        doc,
        "From an operational standpoint, the platform ingests structured patient registries via secure CSV upload, schedules campaign execution "
        "through AWS EventBridge Scheduler, initiates outbound calls via Amazon Connect PSTN telephony with Answering Machine Detection (AMD), "
        "and guides each patient through a calibrated multi-stage conversational tree. Every interaction is recorded, analyzed for clinical signals, "
        "and written back to DynamoDB with fine-grained status metadata, enabling seamless auto-rescheduling for missed calls and automated "
        "closed-loop reporting for clinic administrators."
    )

    add_styled_heading(doc, "Key Platform Capabilities", level=3)
    add_bullet_item(doc, "Supports thousands of patient records with automatic phone number normalization (E.164), duplicate detection, and time-zone-aware scheduling.", "High-Throughput Intake: ")
    add_bullet_item(doc, "Multi-turn Lex V2 identity verification verifies patient first and last names against phonetic and nickname dictionaries before disclosing protected health information (PHI).", "HIPAA-Compliant Identity Verification: ")
    add_bullet_item(doc, "Synthesized by Amazon Polly generative neural voices and Amazon Bedrock Nova Lite, delivering conversational motivational coaching within concise conversational boundaries.", "Personalized Chronic Care Coaching: ")
    add_bullet_item(doc, "Dynamically checks human nurse availability and executes seamless SIP/PSTN transfers, preserving patient caller ID and performing post-transfer outcome evaluation.", "Intelligent Escalation & Handoff: ")
    add_bullet_item(doc, "Pre-warm schedules wake serverless microVMs and Lex runtimes 60 seconds prior to dialing, eliminating initial dead air and call abandonment.", "Sub-Second Cold Start Mitigation: ")

    # -------------------------------------------------------------
    # SECTION 2: HIGH-LEVEL ARCHITECTURE
    # -------------------------------------------------------------
    doc.add_page_break()
    add_styled_heading(doc, "2. High-Level Architecture & Data Flow", level=1)

    add_body_paragraph(
        doc,
        "The Cara architecture adheres strictly to event-driven, serverless cloud design patterns. By leveraging AWS managed services, "
        "the system scales elastically from single-patient ad-hoc callbacks to large multi-thousand-patient chronic care outreach campaigns "
        "without requiring persistent compute infrastructure. All components communicate via secured REST APIs, EventBridge event buses, "
        "or native AWS service-to-service integrations."
    )

    # Embed Architecture Diagram
    add_diagram_image(
        doc,
        os.path.join(docs_dir, "architecture_diagram.png"),
        "Figure 1: High-Level Cara Architecture and End-to-End Data Pipeline across AWS Services."
    )

    add_body_paragraph(
        doc,
        "The end-to-end data lifecycle encompasses six distinct architectural zones:",
        bold_prefix="Architectural Workflow Zones: "
    )

    add_bullet_item(doc, "Frontend portals and EHR integrations upload patient cohorts via Campaign API Gateway into Amazon S3, triggering automated intake parsing and schema validation.", "1. Intake & Validation Zone: ")
    add_bullet_item(doc, "Validated patient records and campaign batches are committed to DynamoDB. EventBridge Scheduler provisions one-time launch and pre-warm timers.", "2. State & Scheduling Zone: ")
    add_bullet_item(doc, "The campaign dialer sequentially claims pending patient records using atomic database locking and dispatches outbound telephony requests via Amazon Connect with AMD.", "3. Telephony & Dispatch Zone: ")
    add_bullet_item(doc, "Amazon Connect orchestrates audio delivery via Polly Neural voices, processes intent matching through Lex V2 bots, and manages session context via the Session Context Lambda.", "4. Conversational Voice Engine: ")
    add_bullet_item(doc, "Contextual coaching prompts are dynamically generated using Amazon Q in Connect (Wisdom) and Bedrock Nova Lite, respecting strict clinical guardrails.", "5. AI Clinical Intelligence: ")
    add_bullet_item(doc, "Live transfer outcomes are evaluated, missed calls are queued for 24-hour auto-reschedule, and final outcomes are persisted to DynamoDB.", "6. Escalation & Feedback Loop: ")

    # -------------------------------------------------------------
    # SECTION 3: DETAILED PHASE-BY-PHASE WALKTHROUGH
    # -------------------------------------------------------------
    doc.add_page_break()
    add_styled_heading(doc, "3. Detailed Phase-by-Phase Walkthrough", level=1)

    # Phase A: Intake
    add_styled_heading(doc, "Phase A: CSV Upload & Campaign Intake", level=2)
    add_body_paragraph(
        doc,
        "The intake process begins when an administrative user or healthcare coordinator initiates a campaign via the REST API. "
        "The API (`campaign_api.py`) exposes endpoints for generating pre-signed S3 upload URLs (`POST /uploads`). The client uploads "
        "a CSV file containing patient demographics and scheduling parameters directly to the designated S3 bucket under the key prefix "
        "`campaigns/{campaignId}/patients.csv`. This direct-to-S3 pattern ensures that large patient lists bypass API Gateway payload limits."
    )
    add_body_paragraph(
        doc,
        "Upon successful S3 object creation, an S3 Event Notification asynchronously triggers the `campaign_intake.py` Lambda function. "
        "The intake handler performs strict validation across each row:",
        bold_prefix="Intake Validation Pipeline: "
    )
    add_bullet_item(doc, "Verifies the presence of required headers: PatientId, FirstName, LastName, PhoneNumber, TimeZone, ScheduledDate, and ScheduledSlot.", "Header Verification: ")
    add_bullet_item(doc, "Parses raw telephone strings and normalizes them into international E.164 format (+1XXXXXXXXXX for US numbers). Invalid or malformed numbers are flagged immediately.", "Phone Normalization: ")
    add_bullet_item(doc, "Scans for duplicate patient identifiers and duplicate phone numbers within the batch, rejecting redundant entries to prevent multiple concurrent calls to the same patient.", "Deduplication: ")
    add_bullet_item(doc, "Converts local appointment slots into UTC timestamps based on the patient's declared IANA or standard US timezone (e.g., America/New_York, EST).", "Timezone Calculation: ")
    
    add_body_paragraph(
        doc,
        "Once validated, the handler writes a master batch record to `TalkingBotCallBatches-dev` and individual patient items to "
        "`TalkingBotPatientRecords-dev` with initial status `PENDING`. If validation fails, the campaign record is marked as `VALIDATION_FAILED` "
        "with specific row-by-row error diagnostics."
    )

    # Phase B: Scheduling
    add_styled_heading(doc, "Phase B: Scheduling & EventBridge Orchestration", level=2)
    add_body_paragraph(
        doc,
        "Cara leverages Amazon EventBridge Scheduler for serverless, millisecond-accurate scheduling. EventBridge Scheduler replaces "
        "polling daemons with zero-maintenance, cron/at-expression triggers that execute targets with automatic retry policies."
    )
    add_body_paragraph(
        doc,
        "During campaign intake, `campaign_intake.py` provisions two distinct schedules per campaign via the `events:CreateSchedule` API:",
        bold_prefix="Dual-Schedule Strategy: "
    )
    add_bullet_item(doc, "Configured to execute exactly 60 seconds prior to the scheduled campaign launch time (T - 60s). If the campaign is configured to launch immediately, it is scheduled at now + 2s. The payload directs the dialer to invoke `run_prewarm`, which warms Lambda microVMs, Lex V2 bot runtimes, and Amazon Bedrock models.", "1. Pre-warm Schedule (cara-prewarm-{campaignId}): ")
    add_bullet_item(doc, "Configured to fire at the exact target launch time (T0). The payload delivers `{\"action\": \"campaign-start\", \"batchId\": \"...\", \"campaignId\": \"...\"}` to `campaign_dialer.py`, kicking off the sequential outbound calling loop.", "2. Campaign Launch Schedule (cara-start-{campaignId}): ")

    # Phase C: Outbound Dialing
    add_styled_heading(doc, "Phase C: Outbound Dialing (campaign_dialer.py)", level=2)
    add_body_paragraph(
        doc,
        "The `campaign_dialer.py` Lambda function serves as the central dialing engine. When triggered by the `campaign-start` event, "
        "the dialer reads the target batch record and initiates a sequential dialing loop over all `PENDING` patients."
    )
    add_body_paragraph(
        doc,
        "To avoid race conditions and prevent duplicate call attempts in multi-invoker environments, the dialer employs atomic conditional "
        "updates in DynamoDB. Before placing a call, the function executes an `UpdateItem` operation setting `status = 'IN_PROGRESS'` with "
        "a condition expression `attribute_not_exists(#st) OR #st = :pending`. If another execution thread has already claimed the patient, "
        "the condition fails and the dialer advances to the next candidate."
    )
    add_body_paragraph(
        doc,
        "Once claimed, the dialer issues an Amazon Connect `StartOutboundVoiceContact` API call with the following operational parameters:",
        bold_prefix="Telephony Configuration: "
    )
    add_bullet_item(doc, "The targeted telephone number in validated E.164 format.", "DestinationPhoneNumber: ")
    add_bullet_item(doc, "The registered Amazon Connect DID claiming telephony origin.", "SourcePhoneNumber: ")
    add_bullet_item(doc, "Enables real-time acoustic analysis. Configured with `EnableAnswerMachineDetection: True` and `AwaitAnswerMachinePrompt: True` to allow the carrier beep to be detected if a voicemail is reached.", "AnswerMachineDetectionConfig: ")
    add_bullet_item(doc, "Injects operational context into the telephony channel: `patientId`, `batchId`, `patientFirstName`, `patientLastName`, `patientFullName`, `coachName` ('Cara'), `carePlanTopic`, `appointmentDate`, `callMode` ('OUTBOUND'), and `userSessionId`.", "Contact Attributes: ")

    # Phase D: Contact Flow Execution
    doc.add_page_break()
    add_styled_heading(doc, "Phase D: Contact Flow Execution (cara-health-bot-flow.json)", level=2)
    add_body_paragraph(
        doc,
        "The Amazon Connect contact flow `cara-health-bot-flow.json` governs the real-time audio experience of the patient. "
        "Comprising 101 distinct action nodes, the flow implements a multi-branch state machine that routes the call through AMD verification, "
        "voice configuration, identity validation, clinical coaching, and escalation."
    )

    # Embed Contact Flow Diagram
    add_diagram_image(
        doc,
        os.path.join(docs_dir, "contact_flow_diagram.png"),
        "Figure 2: Amazon Connect Contact Flow Execution Tree and Decision Logic."
    )

    add_body_paragraph(
        doc,
        "The contact flow executes across the following major milestones:",
        bold_prefix="Sequential Node Walkthrough: "
    )
    add_bullet_item(doc, "Initializes contact tracing, enables audio media streaming, and configures CloudWatch log publishing.", "Node f0000000-0000-4000-8000-000000000000 (Start & Logging): ")
    add_bullet_item(doc, "Evaluates the telephony carrier signal. If an answering machine or voicemail beep is detected, execution routes to a voicemail drop branch that plays a non-PHI notification and terminates the call. If a live human is detected, execution proceeds immediately to identity verification.", "Node f0000000-0000-4000-8000-000000000001 (Check AMD Status): ")
    add_bullet_item(doc, "Differentiates between outbound campaign calls and incoming patient callbacks. Inbound calls branch to a dedicated callback greeting before re-joining the verification loop.", "Node e1000000-0000-4000-8000-000000000001 (Check Call Direction): ")
    add_bullet_item(doc, "Configures Amazon Polly generative neural voice 'Danielle' or 'Ruth' for lifelike, empathetic conversational cadence.", "Voice Configuration Nodes: ")
    add_bullet_item(doc, "Engages the patient using Amazon Lex V2 (`cara-health-bot-identity`). Prompts the patient to confirm their identity.", "Node 11111111-1111-4111-8111-111111111111 (Identity Verification Bot): ")
    add_bullet_item(doc, "Engages a dedicated Lex bot (`cara-health-bot-availability`) to confirm whether the patient has 3 to 5 minutes to talk. If unavailable, offers an immediate callback.", "Node 44444444-4444-4444-8444-444444444444 (Check Availability Bot): ")
    add_bullet_item(doc, "Invokes `session_context.py` with operation `initialize` to establish an Amazon Q in Connect / Wisdom knowledge session.", "Node 33333333-3333-4333-8333-333333333333 (Initialize Wisdom Session): ")
    add_bullet_item(doc, "Enters the multi-turn clinical coaching loop powered by Amazon Bedrock Nova Lite (`amazon.nova-lite-v1:0`).", "Node 55555555-5555-4555-8555-555555555555 (AI Coaching Bot): ")
    add_bullet_item(doc, "Invoked when coaching intent indicates escalation. Checks specialist availability and initiates external SIP/PSTN transfer.", "Node e1000000-0000-4000-8000-000000000004 (Transfer to Agent): ")
    add_bullet_item(doc, "Invokes `session_context.py` with operation `evaluateTransferOutcome` to examine third-party leg disconnect codes.", "Node f2000000-0000-4000-8000-000000000030 (Evaluate Transfer Outcome): ")
    add_bullet_item(doc, "Terminates the telephony session cleanly after playing appropriate closing remarks.", "Node f2000000-0000-4000-8000-000000000099 (Terminal Disconnect): ")

    # Phase E: Identity Verification
    add_styled_heading(doc, "Phase E: Identity Verification (Lex Identity Bot)", level=2)
    add_body_paragraph(
        doc,
        "In compliance with HIPAA regulations, Cara cannot disclose medical conditions, care plans, or clinic affiliations until the "
        "called party's identity has been confirmed. The `cara-health-bot-identity` Lex V2 bot conducts a calibrated three-turn dialog."
    )
    add_body_paragraph(
        doc,
        "Turn 1 begins with a greeting: 'Hello, this is Cara calling on behalf of your care team. May I speak with [FirstName]?' "
        "If the patient responds affirmatively ('Speaking', 'Yes, this is she', 'This is [FirstName]'), identity is confirmed. "
        "If the speech is ambiguous, Turn 2 prompts for full name confirmation. If the patient states a name variant (e.g., 'This is Bob' "
        "when the record indicates 'Robert'), Turn 3 invokes the `session_context.py` Lambda with operation `verifyIdentityName`."
    )
    add_body_paragraph(
        doc,
        "The `verifyIdentityName` algorithm performs token-based matching across known nicknames, phonetic Soundex comparisons, and string "
        "similarity metrics. If matching succeeds, the flow advances to availability checking. If the patient indicates that the target "
        "person is unavailable or that this is a wrong number, the call branches to a polite exit, recording the outcome as `NOT_INTERESTED` "
        "or offering a callback for a later time."
    )

    # Phase F: AI Coaching Session
    add_styled_heading(doc, "Phase F: AI Coaching Session (Wisdom / Bedrock Nova Lite)", level=2)
    add_body_paragraph(
        doc,
        "Once identity and availability are verified, the contact flow activates the conversational coaching loop. Cara couples "
        "Amazon Lex V2 (`cara-health-bot-coaching`) with Amazon Bedrock Nova Lite (`amazon.nova-lite-v1:0`) and Amazon Q in Connect (formerly Wisdom)."
    )
    add_body_paragraph(
        doc,
        "The coaching dialog is governed by a clinical prompt template (`backend/prompts/life-coach.yaml`). The prompt instructs the model "
        "to act as an empathetic, motivational chronic care coach specializing in hypertension, diabetes, medication adherence, and lifestyle "
        "modifications. Crucially, the prompt enforces a 30-word conversational turn limit. Voice interfaces require concise, rapid turns; "
        "lengthy monologues overwhelm patients and lead to premature disconnects."
    )
    add_callout(
        doc,
        "Deterministic Emergency Safety Net: If a patient mentions acute medical red flags (chest pressure, stroke symptoms, severe shortness "
        "of breath) or behavioral crises (suicidal ideation), Lex V2 matches the deterministic intents `SafetyMedical` or `SafetyBehavioral`. "
        "These intents completely bypass Bedrock Nova Lite, routing immediately to hardcoded static prompts ('Please hang up and call 911 immediately' "
        "or dialing the 988 Suicide & Crisis Lifeline) followed by immediate call disconnect.",
        title="SAFETY GUARDRAILS",
        callout_type="warning"
    )

    # Phase G & H: Escalation & Transfer Outcome
    doc.add_page_break()
    add_styled_heading(doc, "Phase G & H: Human Escalation & Transfer Outcome Evaluation", level=2)
    add_body_paragraph(
        doc,
        "When a patient requests to speak with a human care coordinator, or when clinical guidance warrants registered nurse escalation, "
        "the contact flow initiates the transfer protocol. Prior to transferring, the flow invokes `session_context.py` with operation "
        "`checkAgentAvailability`, which pings the `agent_availability.py` service to verify on-duty specialist staffing and queue depth."
    )
    add_body_paragraph(
        doc,
        "If a human agent is available, the flow executes the `TransferParticipantToThirdParty` node. To prevent clinic receptionists from "
        "seeing a generic contact center trunk number, the flow executes `UpdateContactAttributes` immediately prior to transfer, setting "
        "`CallerIdNumber = $.CustomerEndpoint.Address`. This ensures the patient's actual mobile or home phone number is displayed on the "
        "receiving nurse's caller ID."
    )
    add_body_paragraph(
        doc,
        "Following transfer termination, the contact flow invokes `session_context.py` with operation `evaluateTransferOutcome`. "
        "Evaluating third-party transfer outcomes in Amazon Connect requires specialized logic: the parent contact spawns an `EXTERNAL_OUTBOUND` "
        "child leg. The `evaluateTransferOutcome` handler queries `list_associated_contacts` to isolate the child leg and inspects its "
        "`DisconnectReason` attribute:",
        bold_prefix="Transfer Outcome Matrix: "
    )
    add_bullet_item(doc, "Indicates the patient hung up after speaking with the specialist. The call is marked as `COMPLETED`.", "CUSTOMER_DISCONNECT: ")
    add_bullet_item(doc, "Indicates the specialist completed the consultation and concluded the call. The call is marked as `COMPLETED`.", "AGENT_DISCONNECT: ")
    add_bullet_item(doc, "Indicates the external PSTN connection failed, line was busy, or specialist did not pick up within the timeout window. The flow offers the patient an immediate scheduled callback and records status `AGENT_UNAVAILABLE` or `CALLBACK_SCHEDULED`.", "TELECOM_PROBLEM / NO_ANSWER: ")

    # Phase I & J: Call Status Updates & Auto-Reschedule
    add_styled_heading(doc, "Phase I & J: Call Status Updates & Auto-Reschedule Logic", level=2)
    add_body_paragraph(
        doc,
        "Call outcomes and patient lifecycle states are maintained in DynamoDB (`TalkingBotPatientRecords-dev`). "
        "Transitions occur deterministically across specific telephony and conversational milestones."
    )

    # Embed Status State Diagram
    add_diagram_image(
        doc,
        os.path.join(docs_dir, "status_state_diagram.png"),
        "Figure 3: Comprehensive Patient Record State Machine and Transition Triggers."
    )

    add_body_paragraph(
        doc,
        "The system recognizes eight distinct patient record statuses:",
        bold_prefix="Status Inventory: "
    )
    add_bullet_item(doc, "Initial state upon CSV ingestion. Patient is queued awaiting dialer dispatch.", "PENDING: ")
    add_bullet_item(doc, "Dialer has atomically claimed the record; outbound call is ringing or active.", "IN_PROGRESS: ")
    add_bullet_item(doc, "Call concluded successfully. Patient completed coaching or successfully transferred to nurse.", "COMPLETED: ")
    add_bullet_item(doc, "Patient explicitly declined coaching, declared wrong number, or requested not to be contacted.", "NOT_INTERESTED: ")
    add_bullet_item(doc, "Patient or voicemail requested a callback, or missed call was auto-rescheduled for +24 hours.", "CALLBACK_SCHEDULED: ")
    add_bullet_item(doc, "Callback requested but without a specific agreed-upon time window.", "CALLBACK_UNSPECIFIED: ")
    add_bullet_item(doc, "Nurse escalation failed and patient declined to schedule a future callback.", "AGENT_UNAVAILABLE: ")
    add_bullet_item(doc, "Connect API rejected outbound call placement (invalid phone format or trunk failure).", "CALL_SETUP_FAILED: ")
    add_bullet_item(doc, "Patient reached maximum allowed reschedule attempts (default 10) without answering.", "MAX_RETRIES_EXHAUSTED: ")

    add_body_paragraph(
        doc,
        "Auto-Reschedule Algorithm for Missed Calls: When Answering Machine Detection flags a voicemail, or when Connect reports "
        "`NO_ANSWER` or telecom failure, `campaign_dialer.py` evaluates the patient's `rescheduleAttempts` counter. If the counter is "
        "below `maxRescheduleAttempts` (default 10), the dialer advances the patient's `callSlotStart` timestamp by exactly 24 hours "
        "(86,400 seconds) and updates the status to `CALLBACK_SCHEDULED`. The record's Global Secondary Index (`StatusSlotIndex`) enables "
        "the scheduler to query all rescheduled calls due for execution in upcoming time windows."
    )

    # Phase K: Pre-Warm & Cold Start Mitigation
    add_styled_heading(doc, "Phase K: Pre-Warm & Cold Start Mitigation", level=2)
    add_body_paragraph(
        doc,
        "In voice telephony, sub-second latency is critical. A delay of 2.0 to 3.0 seconds during initial call connection causes dead air, "
        "leading patients to assume a dead connection and hang up before Cara's greeting plays. Serverless AWS components inherently suffer "
        "from cold starts when microVMs, Lex V2 dialog runtimes, and foundation model inference engines spin up from zero."
    )
    add_body_paragraph(
        doc,
        "Cara addresses cold starts through a three-tier pre-warming architecture:",
        bold_prefix="Cold Start Elimination Architecture: "
    )
    add_bullet_item(doc, "EventBridge triggers `campaign_dialer.py` with `action=\"prewarm\"` at T - 60 seconds. The dialer executes `run_prewarm()`, which issues synthetic test requests across `session_context`, `agent_availability`, Lex V2 `recognize_text`, and Bedrock runtime.", "1. Pre-Campaign Warm Trigger: ")
    add_bullet_item(doc, "The critical `session_context.py` Lambda runs on the `Live` alias backed by provisioned concurrency, guaranteeing zero-millisecond microVM cold starts for telephony hook invocations.", "2. Provisioned Concurrency: ")
    add_bullet_item(doc, "During sequential campaign execution, `campaign_dialer.py` tracks the timestamp of the last dialed call. If more than 5 minutes have elapsed between calls, `_maybe_rewarm_agent_availability()` dispatches an asynchronous background ping to keep downstream specialist endpoints warm.", "3. Mid-Campaign Rewarming: ")

    # -------------------------------------------------------------
    # SECTION 4: SINGLE-CALL END-TO-END SEQUENCE
    # -------------------------------------------------------------
    doc.add_page_break()
    add_styled_heading(doc, "4. Single-Call End-to-End Sequence Walkthrough", level=1)
    add_body_paragraph(
        doc,
        "To illustrate how the individual components interact during runtime execution, the sequence diagram below maps the eighteen "
        "discrete chronological steps executed for a single outbound coaching call that results in a successful nurse handoff."
    )

    # Embed Sequence Diagram
    add_diagram_image(
        doc,
        os.path.join(docs_dir, "sequence_diagram.png"),
        "Figure 4: Complete End-to-End Execution Sequence for a Single Outbound Patient Call."
    )

    add_body_paragraph(
        doc,
        "Chronological Step Details:",
        bold_prefix="Step-by-Step Breakdown: "
    )
    steps_data = [
        ("Step 1: Campaign Trigger", "EventBridge Scheduler fires at T0, sending a 'campaign-start' event to campaign_dialer.py."),
        ("Step 2: Atomic Claim", "Dialer performs conditional update on DynamoDB, changing patient status from PENDING to IN_PROGRESS."),
        ("Step 3: Call Placement", "Dialer calls Amazon Connect StartOutboundVoiceContact with AMD enabled and patient attributes injected."),
        ("Step 4: PSTN Ringing", "Amazon Connect dials patient's phone number over public telephony network."),
        ("Step 5: Voice Pickup & AMD", "Patient answers telephone. AMD analyzes initial acoustic energy and confirms a live human speaker."),
        ("Step 6: Contact Flow Entry", "Connect launches cara-health-bot-flow.json, plays Polly neural greeting, and prompts for identity."),
        ("Step 7: Identity Prompt", "Amazon Connect invokes cara-health-bot-identity Lex bot asking 'May I speak with [FirstName]?'"),
        ("Step 8: Identity Verification", "Patient responds. Lex invokes session_context (verifyIdentityName) to confirm name token match."),
        ("Step 9: Availability Check", "Connect invokes cara-health-bot-availability Lex bot asking if patient has 3-5 minutes."),
        ("Step 10: Availability Confirmation", "Patient confirms availability ('Yes, I can talk now')."),
        ("Step 11: Wisdom Init", "Connect invokes session_context (initialize) to establish Amazon Q / Wisdom clinical context."),
        ("Step 12: Coaching Invocation", "Connect engages cara-health-bot-coaching Lex bot connected to Bedrock Nova Lite."),
        ("Step 13: Coaching Dialogue", "Multi-turn motivational interview occurs regarding blood pressure management and diet."),
        ("Step 14: Escalation Request", "Patient requests human consultation ('Can I speak with a nurse?'). Coaching bot detects escalation intent."),
        ("Step 15: Specialist Check", "Connect invokes session_context (checkAgentAvailability), which confirms specialist is online."),
        ("Step 16: External Transfer", "Flow sets CallerIdNumber attribute and executes TransferParticipantToThirdParty to nurse phone."),
        ("Step 17: Nurse Consultation", "Patient and nurse converse over third-party bridge. Call completes cleanly."),
        ("Step 18: Outcome & Persistence", "Connect invokes session_context (evaluateTransferOutcome), confirming CUSTOMER_DISCONNECT. Record marked COMPLETED in DynamoDB.")
    ]
    for s_title, s_desc in steps_data:
        add_bullet_item(doc, s_desc, f"{s_title}: ")

    # -------------------------------------------------------------
    # SECTION 5: LAMBDA REFERENCE TABLE
    # -------------------------------------------------------------
    doc.add_page_break()
    add_styled_heading(doc, "5. Lambda Reference Table", level=1)
    add_body_paragraph(
        doc,
        "The Cara backend is powered by five core AWS Lambda functions. The table below outlines their invocation triggers, input contracts, "
        "output contracts, side effects, and error handling mechanisms."
    )

    lambda_table = doc.add_table(rows=6, cols=5)
    lambda_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    lambda_table.autofit = False
    set_table_borders(lambda_table)

    col_widths = [1.3, 1.1, 1.4, 1.4, 1.6]
    headers = ["Lambda Name", "Trigger Source", "Input Payload", "Output / Return", "Side Effects & Error Handling"]
    format_table_headers(lambda_table, col_widths, headers)

    lambdas_info = [
        (
            "campaign_api.py",
            "API Gateway / Function URL (REST)",
            "HTTP JSON: POST /uploads, GET /campaigns, GET /campaigns/{id}, POST /reschedule, GET /live",
            "HTTP 200/400/500 JSON: presignedUrl, campaign status, patient arrays, live operational metrics",
            "Generates S3 presigned URLs, queries DynamoDB batches and patients. Returns CORS headers and structured error JSON."
        ),
        (
            "campaign_intake.py",
            "S3 ObjectCreated Event (patients.csv)",
            "S3 Event Notification: bucket name and object key (campaigns/{campaignId}/patients.csv)",
            "Execution summary: {status: SUCCESS/FAILED, totalPatients: N, batchId: UUID}",
            "Parses and normalizes CSV, writes to CallBatches and PatientRecords tables. Creates EventBridge launch and prewarm schedules."
        ),
        (
            "campaign_dialer.py",
            "EventBridge Scheduler & Connect Events",
            "{action: 'campaign-start'/'prewarm', batchId: UUID} or Connect DISCONNECTED event",
            "{status: 'STARTED'/'WARMED', dialedCount: N, errors: N}",
            "Locks patient record to IN_PROGRESS. Calls Connect StartOutboundVoiceContact. Auto-reschedules missed calls for +24 hours."
        ),
        (
            "session_context.py",
            "Connect Contact Flow & Lex Hook",
            "Connect Flow Event: Details.Parameters {operation: initialize / verifyIdentityName / checkAgentAvailability / evaluateTransferOutcome}",
            "Key-value dictionary returned to Connect contact attributes or Lex dialog response",
            "Validates patient name tokens, establishes Wisdom session, checks specialist availability, parses child transfer DisconnectReason."
        ),
        (
            "agent_availability.py",
            "Direct Lambda Invoke / Function URL",
            "JSON payload: {operation: 'check'/'ping', patientId: string, empi: string}",
            "JSON: {available: boolean, specialistId: string, phone: string, status: string}",
            "Validates care coordinator shift status and queue depth. Supports lightweight 'ping' operation for cold-start pre-warming."
        )
    ]

    for idx, row_data in enumerate(lambdas_info):
        format_table_row(lambda_table.rows[idx + 1], col_widths, row_data, is_even=(idx % 2 == 1))

    # -------------------------------------------------------------
    # SECTION 6: DYNAMODB SCHEMA REFERENCE
    # -------------------------------------------------------------
    doc.add_page_break()
    add_styled_heading(doc, "6. DynamoDB Schema Reference", level=1)
    add_body_paragraph(
        doc,
        "Cara utilizes two primary Amazon DynamoDB tables for state persistence. Designed for single-digit millisecond latency, "
        "the tables employ strategic partition keys and Global Secondary Indexes (GSIs) to facilitate atomic claims and time-slot scheduling."
    )

    add_styled_heading(doc, "Table 1: TalkingBotCallBatches-dev", level=2)
    add_body_paragraph(doc, "Maintains campaign-level aggregation, scheduling metadata, and execution progress counters.")
    
    batch_table = doc.add_table(rows=6, cols=4)
    batch_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    batch_table.autofit = False
    set_table_borders(batch_table)
    b_col_widths = [1.5, 1.1, 1.2, 3.0]
    b_headers = ["Attribute Name", "Type", "Key Type", "Description & Usage"]
    format_table_headers(batch_table, b_col_widths, b_headers)

    batch_fields = [
        ("batchId", "String", "Partition Key (PK)", "Unique UUID identifying the campaign batch."),
        ("campaignId", "String", "Standard", "Human-readable campaign identifier from upload filename."),
        ("status", "String", "Standard", "Campaign state: PENDING, RUNNING, COMPLETED, VALIDATION_FAILED, CANCELLED."),
        ("totalPatients", "Number", "Standard", "Total count of valid patient records within the batch."),
        ("counts (completed, etc.)", "Number", "Standard", "Atomic progress counters: completedCalls, rescheduledCalls, failedCalls.")
    ]
    for idx, row_data in enumerate(batch_fields):
        format_table_row(batch_table.rows[idx + 1], b_col_widths, row_data, is_even=(idx % 2 == 1))

    add_styled_heading(doc, "Table 2: TalkingBotPatientRecords-dev", level=2)
    add_body_paragraph(doc, "Maintains patient demographics, telephony state, clinical coaching history, and reschedule counters.")

    patient_table = doc.add_table(rows=9, cols=4)
    patient_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    patient_table.autofit = False
    set_table_borders(patient_table)
    p_col_widths = [1.5, 1.1, 1.2, 3.0]
    p_headers = ["Attribute Name", "Type", "Key Type", "Description & Usage"]
    format_table_headers(patient_table, p_col_widths, p_headers)

    patient_fields = [
        ("patientId", "String", "Partition Key (PK)", "Unique patient medical identifier or MRN."),
        ("batchId", "String", "Sort Key (SK)", "Batch identifier linking patient to campaign run."),
        ("status", "String", "GSI PK (StatusSlotIndex)", "Lifecycle state: PENDING, IN_PROGRESS, COMPLETED, NOT_INTERESTED, etc."),
        ("callSlotStart", "String", "GSI SK (StatusSlotIndex)", "ISO-8601 UTC timestamp of target dialing window. Enables slot queries."),
        ("contactId", "String", "GSI PK (contactId-index)", "Amazon Connect unique contact identifier. Used for CTR event lookup."),
        ("phoneNumber", "String", "Standard", "E.164 normalized patient telephone number (+1XXXXXXXXXX)."),
        ("rescheduleAttempts", "Number", "Standard", "Current count of auto-reschedules executed for this patient."),
        ("coachingSummary", "String", "Standard", "Summarized clinical dialogue, key takeaways, and patient commitments.")
    ]
    for idx, row_data in enumerate(patient_fields):
        format_table_row(patient_table.rows[idx + 1], p_col_widths, row_data, is_even=(idx % 2 == 1))

    # -------------------------------------------------------------
    # SECTION 7: KNOWN ISSUES & RECENT ARCHITECTURAL FIXES
    # -------------------------------------------------------------
    doc.add_page_break()
    add_styled_heading(doc, "7. Known Issues & Recent Architectural Fixes", level=1)
    add_body_paragraph(
        doc,
        "During live clinical testing and high-volume dialing evaluations, several subtle race conditions and platform gotchas were "
        "identified and resolved. This section documents these architectural fixes to provide critical context on why specific retry "
        "and polling logic exists in the codebase."
    )

    add_styled_heading(doc, "1. DisconnectReason Telephony Race Condition Fix", level=2)
    add_body_paragraph(
        doc,
        "Issue: In Amazon Connect, when an outbound call transfers a participant to a third-party PSTN destination via "
        "`TransferParticipantToThirdParty`, Connect creates a secondary `EXTERNAL_OUTBOUND` contact leg. When the call ends, "
        "Amazon Connect generates Contact Trace Records (CTR) and updates contact attributes asynchronously. In initial testing, "
        "`evaluateTransferOutcome` immediately inspected the parent contact's `DisconnectReason`. Because of a 200–800ms propagation delay, "
        "`DisconnectReason` returned `None`, causing the system to misclassify successfully completed nurse consultations as dropped calls."
    )
    add_body_paragraph(
        doc,
        "Fix: `session_context.py` was updated to iterate through `list_associated_contacts` specifically looking for child contacts with "
        "`InitiationMethod = 'EXTERNAL_OUTBOUND'`. The handler executes an exponential-backoff polling loop against `describe_contact` "
        "(up to 3 attempts with 300ms intervals) to ensure the terminal disconnect reason (`CUSTOMER_DISCONNECT` or `AGENT_DISCONNECT`) "
        "is reliably captured before finalizing the database update."
    )

    add_styled_heading(doc, "2. Pre-Warm Dual-Layer Implementation", level=2)
    add_body_paragraph(
        doc,
        "Issue: Amazon Lex V2 bots and Amazon Bedrock foundation models experience severe cold-start latency (1.5 to 3.5 seconds) "
        "when first invoked after periods of inactivity. During early live patient tests, this latency resulted in dead air after the patient "
        "answered. Patients repeatedly said 'Hello? Is anyone there?' and hung up before Cara's greeting played, resulting in high false-positive "
        "no-pickup rates."
    )
    add_body_paragraph(
        doc,
        "Fix: Implemented an automated dual-layer pre-warming system. Layer 1 provisions an EventBridge schedule 60 seconds prior to campaign launch, "
        "dispatching a synthetic wake-up call to Lex, Bedrock, and Lambda. Layer 2 integrates `_maybe_rewarm_agent_availability()` within "
        "`campaign_dialer.py`, sending keepalive pings whenever inter-call pauses exceed 5 minutes. Additionally, `session_context.py` "
        "was moved to a `Live` alias backed by provisioned concurrency."
    )

    add_styled_heading(doc, "3. Caller ID Preservation on External Transfers", level=2)
    add_body_paragraph(
        doc,
        "Issue: When Cara transferred a patient to an external physician's office or clinic call center, Amazon Connect presented the "
        "care management program's generic outbound toll-free number rather than the patient's personal telephone number. Clinic receptionists "
        "were unable to identify the calling patient without extensive manual intake."
    )
    add_body_paragraph(
        doc,
        "Fix: Modified `cara-health-bot-flow.json` to execute `UpdateContactAttributes` immediately prior to `TransferParticipantToThirdParty`. "
        "The node sets `CallerIdNumber = $.CustomerEndpoint.Address`, forcing Amazon Connect telephony trunks to project the patient's "
        "actual originating telephone number to the receiving clinic."
    )

    # -------------------------------------------------------------
    # SECTION 8: GLOSSARY OF TERMS
    # -------------------------------------------------------------
    doc.add_page_break()
    add_styled_heading(doc, "8. Glossary of Technical Terms", level=1)
    add_body_paragraph(
        doc,
        "This glossary defines key acronyms, AWS services, and telephony concepts used throughout the Cara platform architecture."
    )

    glossary_items = [
        ("Amazon Connect", "An omnichannel cloud contact center service that manages PSTN telephony, inbound/outbound queues, media routing, and interactive contact flow trees."),
        ("Amazon Lex V2", "A conversational AI service for building conversational interfaces into voice and text applications using deep learning speech recognition (ASR) and natural language understanding (NLU)."),
        ("Amazon Bedrock", "A fully managed service offering foundation models from leading AI providers. Cara utilizes Amazon Nova Lite (amazon.nova-lite-v1:0) for cost-effective, low-latency conversational coaching."),
        ("Amazon Q in Connect (Wisdom)", "A generative-AI powered assistant for contact centers (formerly Amazon Connect Wisdom) that ingests clinical knowledge bases and provides real-time context to conversational bots."),
        ("AWS Lambda", "A serverless, event-driven compute service that executes backend code in response to API Gateway requests, S3 notifications, EventBridge schedules, and Connect contact flows."),
        ("Provisioned Concurrency", "An AWS Lambda feature that pre-allocates execution environments, eliminating cold-start latency for time-critical voice telephony hooks."),
        ("Amazon DynamoDB & GSI", "A fully managed NoSQL database service. Global Secondary Indexes (GSIs) allow querying records on alternative attributes (such as patient status and callSlotStart) without table scans."),
        ("Amazon EventBridge Scheduler", "A serverless scheduling service that enables creating, running, and managing millions of time-based tasks and cron jobs across AWS services."),
        ("AMD (Answering Machine Detection)", "An acoustic analysis algorithm implemented by telephony carriers and Amazon Connect to detect whether an answering party is a live human or an automated voicemail system."),
        ("CTR (Contact Trace Record)", "A detailed JSON event generated by Amazon Connect documenting every interaction, branch, duration, and disconnect reason for a telephony contact."),
        ("E.164 Format", "An internationally recognized telephone numbering plan format ensuring global routability (e.g., +1 followed by a 10-digit US telephone number)."),
        ("PHI (Protected Health Information)", "Individually identifiable healthcare data protected under the United States Health Insurance Portability and Accountability Act (HIPAA).")
    ]

    for term, defn in glossary_items:
        add_body_paragraph(doc, defn, bold_prefix=f"{term}: ", space_after=4)

    # Save document
    doc.save(output_path)
    print(f"Successfully generated documentation DOCX at: {output_path}")


if __name__ == "__main__":
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    docs_folder = os.path.join(base_dir, "docs")
    target_docx = os.path.join(docs_folder, "Cara_System_Documentation.docx")
    
    os.makedirs(docs_folder, exist_ok=True)
    build_documentation_docx(target_docx, docs_folder)
