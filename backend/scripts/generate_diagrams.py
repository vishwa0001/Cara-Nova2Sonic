"""Generate polished, publication-quality architecture and flow diagrams for Cara System Documentation."""
import os
import matplotlib.pyplot as plt
import matplotlib.patches as patches

OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "docs"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

plt.rcParams["font.sans-serif"] = "DejaVu Sans"
plt.rcParams["font.family"] = "sans-serif"


def create_architecture_diagram():
    fig, ax = plt.subplots(figsize=(17, 11), dpi=250)
    ax.set_xlim(0, 17)
    ax.set_ylim(0, 11)
    ax.axis("off")

    ax.text(8.5, 10.5, "Cara Health Bot — End-to-End System Architecture", 
            ha="center", va="center", fontsize=20, fontweight="bold", color="#0F172A")
    ax.text(8.5, 10.15, "Autonomous Outbound Voice Outreach, Dynamic AI Coaching, Telephony Integration & Closed-Loop Tracking", 
            ha="center", va="center", fontsize=10.5, color="#475569", style="italic")

    def draw_container(x, y, w, h, title, border_color):
        rect = patches.FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.1,rounding_size=0.2",
                                      facecolor="#F8FAFC", edgecolor=border_color, linewidth=1.5, linestyle="--", alpha=0.6)
        ax.add_patch(rect)
        ax.text(x + 0.25, y + h - 0.3, title, fontsize=9.5, fontweight="bold", color=border_color)

    draw_container(0.5, 4.4, 4.3, 5.3, "1. INGESTION & CAMPAIGN API", "#64748B")
    draw_container(5.2, 5.6, 5.4, 4.1, "2. DYNAMODB STATE & SCHEDULING", "#D97706")
    draw_container(11.0, 5.0, 5.5, 4.7, "3. DIALER & TELEPHONY (AMAZON CONNECT)", "#059669")
    draw_container(5.2, 0.4, 11.3, 4.3, "4. CONVERSATIONAL AI, NLU & HUMAN HANDOFF", "#7C3AED")

    def draw_box(x, y, w, h, title, subtitle, bg, border, text_color="#0F172A", fontsize=9.5):
        rect = patches.FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.08,rounding_size=0.15",
                                      facecolor=bg, edgecolor=border, linewidth=1.5)
        ax.add_patch(rect)
        ax.text(x + w/2, y + h/2 + (0.13 if subtitle else 0), title,
                ha="center", va="center", fontsize=fontsize, fontweight="bold", color=text_color)
        if subtitle:
            ax.text(x + w/2, y + h/2 - 0.17, subtitle,
                    ha="center", va="center", fontsize=7.5, color="#475569")

    # Nodes
    draw_box(0.8, 8.2, 3.7, 1.0, "Frontend Client", "React / Vite Dashboard", "#E0F2FE", "#0284C7")
    draw_box(0.8, 6.5, 3.7, 1.1, "Campaign API Lambda", "Function URL (Auth / CORS)\nPOST /uploads, GET /campaigns", "#EEF2FF", "#4F46E5")
    draw_box(0.8, 4.8, 1.7, 1.1, "S3 Bucket", "patients.csv\nconfig.json", "#FEF3C7", "#D97706")
    draw_box(2.8, 4.8, 1.7, 1.1, "Campaign Intake", "Lambda (S3 Trigger)\nCSV Validation", "#EEF2FF", "#4F46E5")

    draw_box(5.5, 8.1, 4.8, 1.2, "EventBridge Scheduler", "1. Campaign Start Schedule (at UTC)\n2. Prewarm (T-60s) | 3. Patient Callbacks", "#FEF3C7", "#D97706")
    draw_box(5.5, 6.0, 4.8, 1.6, "DynamoDB State Tables", "TalkingBotCallBatches-dev (Campaign Batches)\nTalkingBotPatientRecords-dev (Patient Records)\nGSI: StatusSlotIndex, contactId-index", "#FEF3C7", "#B45309")

    draw_box(11.3, 8.0, 4.9, 1.3, "Campaign Dialer Lambda", "Atomic sequential dialing (_claim_patient)\nstart_outbound_voice_contact (AMD enabled)", "#EEF2FF", "#4F46E5")
    draw_box(11.3, 5.4, 4.9, 1.8, "Amazon Connect Instance", "Voice Contact Flow: cara-health-bot-flow\nAMD (Answer Machine Detection)\nDual-Channel Stereo IVR Recording", "#ECFDF5", "#059669")

    draw_box(5.5, 2.7, 3.3, 1.5, "Lex Identity Bot", "V2 NLU (0.90 conf threshold)\nConversational identity confirmation\nClarification & double-check turn", "#FDF2F8", "#DB2777")
    draw_box(9.2, 2.7, 3.3, 1.5, "Lex Availability Bot", "Privacy-safe relative intake\nTargetAvailableNow / Unavailable\nCaptures date & time slots", "#FDF2F8", "#DB2777")
    draw_box(12.9, 2.7, 3.3, 1.5, "Lex Coaching Bot", "Bedrock Nova Lite (Q in Connect)\n180+ acute emergency bypass\nTools: Escalate, Callback, End", "#FDF2F8", "#DB2777")

    draw_box(5.5, 0.7, 3.3, 1.5, "Session-Context Lambda", "Live Alias (512MB / Prov. Conc.)\ninitialize, verifyIdentityName,\nevaluateTransferOutcome", "#EEF2FF", "#4F46E5")
    draw_box(9.2, 0.7, 3.3, 1.5, "Agent Availability", "Real-time specialist phone check\nDirect SDK / Function URL\nDynamic fail-safe fallback", "#EEF2FF", "#4F46E5")
    draw_box(12.9, 0.7, 3.3, 1.5, "Human Specialist (Agent)", "External Phone / Softphone Transfer\nTransferParticipantToThirdParty\nCaller ID preserved (E.164)", "#F0F9FF", "#0284C7")

    def draw_arrow(start, end, label="", label_pos=(0, 0), rad=0.0, color="#334155", lw=1.3):
        style = "Simple,tail_width=0.4,head_width=3.5,head_length=5.5"
        arrow = patches.FancyArrowPatch(start, end, connectionstyle=f"arc3,rad={rad}",
                                        arrowstyle=style, color=color, linewidth=lw)
        ax.add_patch(arrow)
        if label:
            mx = (start[0] + end[0]) / 2 + label_pos[0]
            my = (start[1] + end[1]) / 2 + label_pos[1]
            ax.text(mx, my, label, fontsize=7.2, fontweight="bold", color=color,
                    bbox=dict(boxstyle="round,pad=0.15", facecolor="#FFFFFF", edgecolor="#CBD5E1", lw=0.8, alpha=0.92),
                    ha="center", va="center")

    draw_arrow((2.65, 8.2), (2.65, 7.6), "1. Upload CSV")
    draw_arrow((1.65, 6.5), (1.65, 5.9), "2. Presigned PUT")
    draw_arrow((2.5, 5.35), (2.8, 5.35), "3. S3 Event")
    draw_arrow((4.5, 5.1), (5.5, 6.2), "4. Persist PENDING", rad=0.05)
    draw_arrow((4.5, 5.6), (5.5, 8.4), "5. Schedule Call & Prewarm", rad=0.05)
    draw_arrow((7.9, 8.1), (7.9, 7.6), "6. Status PENDING")
    draw_arrow((10.3, 8.7), (11.3, 8.7), "7. Fires Schedule Trigger\n(campaign-start)")
    draw_arrow((13.7, 8.0), (13.7, 7.2), "8. StartOutboundVoiceContact\n(Passes Attributes & AMD)")
    draw_arrow((11.3, 6.6), (10.3, 6.6), "9. Atomic Claim\nPENDING -> IN_PROGRESS")
    draw_arrow((11.3, 5.5), (7.1, 4.2), "10. Identity Turn", rad=0.08)
    draw_arrow((12.5, 5.4), (10.8, 4.2), "11. Third Party Check", rad=0.05)
    draw_arrow((14.5, 5.4), (14.5, 4.2), "12. NLU / AI Coaching")
    draw_arrow((7.1, 2.7), (7.1, 2.2), "verifyIdentityName")
    draw_arrow((10.8, 2.2), (10.8, 2.7), "checkAgentAvailability")
    draw_arrow((14.5, 2.7), (14.5, 2.2), "Transfer to Specialist")
    draw_arrow((11.3, 5.8), (9.5, 6.0), "13. DISCONNECTED Event\nEvaluate & Finalize", rad=-0.22, color="#B91C1C")
    draw_arrow((11.3, 9.1), (9.5, 9.1), "14. Auto-Reschedule (24h)\nor Dial Next Patient", rad=0.2, color="#059669")

    plt.tight_layout()
    output_path = os.path.join(OUTPUT_DIR, "architecture_diagram.png")
    plt.savefig(output_path, dpi=250, bbox_inches="tight")
    plt.close()
    print(f"Generated {output_path}")


def create_contact_flow_diagram():
    fig, ax = plt.subplots(figsize=(17, 12), dpi=250)
    ax.set_xlim(0, 17)
    ax.set_ylim(0, 12)
    ax.axis("off")

    ax.text(8.5, 11.6, "Amazon Connect Voice Contact Flow Execution (cara-health-bot-flow.json)", 
            ha="center", va="center", fontsize=18, fontweight="bold", color="#0F172A")
    ax.text(8.5, 11.25, "Deterministic NLU Gating, Context Propagation, Branch Evaluation & Error Recovery", 
            ha="center", va="center", fontsize=10.5, color="#475569", style="italic")

    def draw_node(x, y, w, h, title, subtitle, bg, border, shape="box"):
        if shape == "diamond":
            diamond = patches.Polygon([[x + w/2, y], [x + w, y + h/2], [x + w/2, y + h], [x, y + h/2]],
                                      closed=True, facecolor=bg, edgecolor=border, linewidth=1.5)
            ax.add_patch(diamond)
        else:
            rect = patches.FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.08,rounding_size=0.12",
                                          facecolor=bg, edgecolor=border, linewidth=1.5)
            ax.add_patch(rect)
        ax.text(x + w/2, y + h/2 + (0.13 if subtitle else 0), title,
                ha="center", va="center", fontsize=8.5, fontweight="bold", color="#0F172A")
        if subtitle:
            ax.text(x + w/2, y + h/2 - 0.16, subtitle,
                    ha="center", va="center", fontsize=7.0, color="#475569")

    def draw_edge(start, end, label="", label_pos=(0, 0), rad=0.0, color="#334155"):
        style = "Simple,tail_width=0.4,head_width=3.5,head_length=5.5"
        arrow = patches.FancyArrowPatch(start, end, connectionstyle=f"arc3,rad={rad}",
                                        arrowstyle=style, color=color, linewidth=1.2)
        ax.add_patch(arrow)
        if label:
            mx = (start[0] + end[0]) / 2 + label_pos[0]
            my = (start[1] + end[1]) / 2 + label_pos[1]
            ax.text(mx, my, label, fontsize=7.0, fontweight="bold", color=color,
                    bbox=dict(boxstyle="round,pad=0.12", facecolor="#FFFFFF", edgecolor="#CBD5E1", lw=0.6, alpha=0.92),
                    ha="center", va="center")

    # Step 1: Telephony Init & AMD
    draw_node(0.6, 9.8, 3.4, 0.9, "Start & Telephony Init", "FlowLogging, Voice, Stereo Recording", "#E0F2FE", "#0284C7")
    draw_node(4.7, 9.75, 3.2, 1.0, "CheckOutboundCallStatus", "Answer Machine Detection (AMD)", "#FEF3C7", "#D97706", "diamond")
    draw_node(4.7, 8.2, 3.2, 0.8, "Voicemail Handler", "Play message -> Disconnect", "#FEE2E2", "#DC2626")

    draw_edge((4.0, 10.25), (4.7, 10.25), "Call Placed")
    draw_edge((6.3, 9.75), (6.3, 9.0), "VoicemailBeep", color="#DC2626")

    # Step 2: Call Mode
    draw_node(8.8, 9.75, 3.2, 1.0, "Compare callMode", "DIRECT_HANDOFF vs NORMAL", "#FEF3C7", "#D97706", "diamond")
    draw_edge((7.9, 10.25), (8.8, 10.25), "CallAnswered")

    # Direct Handoff
    draw_node(13.0, 9.8, 3.4, 0.9, "Direct Handoff Check", "Invoke checkAgentAvailability", "#EEF2FF", "#4F46E5")
    draw_edge((12.0, 10.25), (13.0, 10.25), "DIRECT_HANDOFF")

    # Step 3: Identity Verification (Normal Path)
    draw_node(0.6, 6.8, 3.4, 1.2, "Identity Bot Turn 1", "0.90 NLU confidence threshold\nIdentifies target customer", "#FDF2F8", "#DB2777")
    draw_edge((10.0, 9.75), (2.3, 8.0), "NORMAL Mode", rad=0.18)

    draw_node(0.6, 4.8, 3.4, 1.0, "Identity Clarification", "Turn 2: Reprompt / Clarify\nTurn 3: Double-Check Name", "#FDF2F8", "#DB2777")
    draw_edge((2.3, 6.8), (2.3, 5.8), "Ambiguous / Fallback")

    draw_node(0.6, 3.2, 3.4, 0.8, "Identity Failure Exit", "Graceful apology -> Disconnect", "#FEE2E2", "#DC2626")
    draw_edge((2.3, 4.8), (2.3, 4.0), "Denied / Repeated Fail", color="#DC2626")

    draw_node(4.7, 6.8, 3.2, 1.0, "verifyIdentityName", "session-context Lambda\nNormalized Token Matching", "#EEF2FF", "#4F46E5")
    draw_edge((4.0, 7.3), (4.7, 7.3), "NamedConfirmation", label_pos=(0, 0.12))

    # Third Party Branch
    draw_node(4.7, 4.8, 3.2, 1.1, "Availability Lex Bot", "Relative / Third Party Branch\nTargetAvailableNow? Slots: date/time", "#FDF2F8", "#DB2777")
    draw_edge((3.0, 6.8), (4.7, 5.5), "ThirdPartyDetected", rad=0.08)

    draw_node(4.7, 3.2, 3.2, 0.9, "Pass Phone to Target", "Hand-off to actual customer\nTriggers Fresh Identity Check", "#ECFDF5", "#059669")
    draw_edge((6.3, 4.8), (6.3, 4.1), "TargetAvailableNow", color="#059669")
    draw_edge((4.7, 3.65), (2.5, 4.8), "Fresh Identity Check", rad=0.12)

    draw_node(8.6, 4.8, 3.2, 0.9, "Callback Intake", "Save callback attributes\n(callbackDate / callbackTime)", "#ECFDF5", "#059669")
    draw_edge((7.9, 5.35), (8.6, 5.35), "TargetUnavailable")

    # Step 4: AI Coaching Session
    draw_node(8.8, 6.8, 3.4, 1.2, "Wisdom Session & Init", "CreateWisdomSession\nsession-context: initialize", "#EEF2FF", "#4F46E5")
    draw_edge((4.0, 7.8), (8.8, 7.8), "IdentityConfirmed", rad=-0.08, color="#059669")
    draw_edge((7.9, 7.1), (8.8, 7.1), "Name Matched", color="#059669")

    draw_node(13.0, 6.8, 3.4, 1.2, "Coaching Bot Session", "Bedrock Nova Lite + Q in Connect\nSafetyMedical / Behavioral net", "#FDF2F8", "#DB2777")
    draw_edge((12.2, 7.4), (13.0, 7.4), "Context READY")

    # Safety Exit right next to coaching bot
    draw_node(13.0, 8.3, 3.4, 0.8, "Emergency Safety Exits", "SafetyMedical / SafetyBehavioral -> Exit", "#FEE2E2", "#DC2626")
    draw_edge((14.7, 8.0), (14.7, 8.3), "Emergency Trigger", color="#DC2626")

    # Step 5: Transfer & Evaluation
    draw_node(13.0, 4.8, 3.4, 1.0, "checkAgentAvailability", "Session Context Lambda\nLive Specialist Resolution", "#EEF2FF", "#4F46E5")
    draw_edge((14.7, 6.8), (14.7, 5.8), "EscalateToHuman")

    draw_node(13.0, 3.0, 3.4, 1.0, "Transfer to Agent", "TransferParticipantToThirdParty\nCallerIdNumber = Customer Phone", "#ECFDF5", "#059669")
    draw_edge((14.7, 4.8), (14.7, 4.0), "available == true", color="#059669")

    draw_node(13.0, 1.2, 3.4, 1.0, "evaluateTransferOutcome", "DisconnectReason Evaluation\nTHIRD_PARTY_DISCONNECT vs Timeout", "#EEF2FF", "#4F46E5")
    draw_edge((14.7, 3.0), (14.7, 2.2), "Transfer Completed / Ended")

    draw_node(8.6, 1.2, 3.2, 1.0, "No-Pickup Callback Flow", "Team busy prompt -> Availability\nBot takes patient callback time", "#FEF3C7", "#D97706")
    draw_edge((13.0, 1.7), (11.8, 1.7), "NO_PICKUP / Timeout", color="#D97706")

    draw_node(4.7, 1.2, 3.2, 1.0, "Graceful Disconnect", "DisconnectParticipant\nStereo Audio & Metadata Flushed", "#F8FAFC", "#64748B")
    draw_edge((13.0, 1.35), (7.9, 1.35), "COMPLETED", rad=-0.08, color="#059669")
    draw_edge((8.6, 1.7), (7.9, 1.7), "Callback Noted")

    # Other exits
    draw_node(0.6, 1.2, 3.4, 1.0, "Deceased / Wrong Number", "Conditional responses & updates\nPolite goodbye -> Disconnect", "#F1F5F9", "#64748B")
    draw_edge((2.3, 3.2), (2.3, 2.2), "Call Concluded")

    plt.tight_layout()
    output_path = os.path.join(OUTPUT_DIR, "contact_flow_diagram.png")
    plt.savefig(output_path, dpi=250, bbox_inches="tight")
    plt.close()
    print(f"Generated {output_path}")


def create_status_state_diagram():
    fig, ax = plt.subplots(figsize=(17, 10.5), dpi=250)
    ax.set_xlim(0, 17)
    ax.set_ylim(0, 10.5)
    ax.axis("off")

    ax.text(8.5, 10.0, "Cara Health Bot — Campaign & Patient Status Lifecycle State Diagram", 
            ha="center", va="center", fontsize=18, fontweight="bold", color="#0F172A")
    ax.text(8.5, 9.65, "Atomic Transitions, Optimistic Locking, Automated Rescheduling & Disposition Mapping", 
            ha="center", va="center", fontsize=10.5, color="#475569", style="italic")

    def draw_badge(x, y, w, h, title, subtitle, bg, border):
        rect = patches.FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.08,rounding_size=0.15",
                                      facecolor=bg, edgecolor=border, linewidth=1.5)
        ax.add_patch(rect)
        ax.text(x + w/2, y + h/2 + (0.11 if subtitle else 0), title,
                ha="center", va="center", fontsize=9, fontweight="bold", color="#0F172A")
        if subtitle:
            ax.text(x + w/2, y + h/2 - 0.16, subtitle,
                    ha="center", va="center", fontsize=7.0, color="#475569")

    def draw_transition(start, end, label="", label_pos=(0, 0), rad=0.0, color="#334155"):
        style = "Simple,tail_width=0.4,head_width=3.5,head_length=5.5"
        arrow = patches.FancyArrowPatch(start, end, connectionstyle=f"arc3,rad={rad}",
                                        arrowstyle=style, color=color, linewidth=1.2)
        ax.add_patch(arrow)
        if label:
            mx = (start[0] + end[0]) / 2 + label_pos[0]
            my = (start[1] + end[1]) / 2 + label_pos[1]
            ax.text(mx, my, label, fontsize=6.8, fontweight="bold", color=color,
                    bbox=dict(boxstyle="round,pad=0.1", facecolor="#FFFFFF", edgecolor="#CBD5E1", lw=0.6, alpha=0.92),
                    ha="center", va="center")

    # Campaign Lifecycle (Top)
    ax.text(0.8, 8.9, "CAMPAIGN LEVEL STATES (TalkingBotCallBatches-dev):", fontsize=10, fontweight="bold", color="#1E293B")
    draw_badge(0.8, 7.8, 2.6, 0.8, "UPLOAD_PENDING", "POST /uploads", "#F1F5F9", "#64748B")
    draw_badge(4.0, 7.8, 2.6, 0.8, "PENDING", "CSV validated & saved", "#FEF3C7", "#D97706")
    draw_badge(7.2, 7.8, 2.6, 0.8, "RUNNING", "Dialer active", "#DBEAFE", "#2563EB")
    draw_badge(10.4, 7.8, 2.6, 0.8, "COMPLETED", "All rows finalized", "#DCFCE7", "#16A34A")
    draw_badge(13.6, 7.8, 2.6, 0.8, "VALIDATION_FAILED", "Schema/data errors", "#FEE2E2", "#DC2626")
    draw_badge(4.0, 6.6, 2.6, 0.7, "CANCELLED", "User cancel action", "#F3F4F6", "#9CA3AF")

    draw_transition((3.4, 8.2), (4.0, 8.2), "S3 Upload")
    draw_transition((6.6, 8.2), (7.2, 8.2), "Start Event")
    draw_transition((9.8, 8.2), (10.4, 8.2), "Queue Empty")
    draw_transition((2.1, 8.6), (14.9, 8.6), "CSV Invalidation", rad=-0.15, color="#DC2626")
    draw_transition((5.3, 7.8), (5.3, 7.3), "POST /cancel", color="#6B7280")

    # Patient Lifecycle (Bottom Grid: 4 rows x 3 columns)
    ax.text(0.8, 5.7, "PATIENT ROW LEVEL STATES (TalkingBotPatientRecords-dev):", fontsize=10, fontweight="bold", color="#1E293B")

    # Initial & Active
    draw_badge(0.8, 4.4, 2.6, 0.95, "PENDING", "Initial patient state\nReady for outreach", "#FEF3C7", "#D97706")
    draw_badge(4.2, 4.4, 2.8, 0.95, "IN_PROGRESS", "Atomic dial claim\n(attemptCount++)", "#DBEAFE", "#2563EB")

    # Row 1: Positive Endpoints
    draw_badge(8.0, 4.4, 3.4, 0.95, "COMPLETED", "Identity confirmed, coaching,\nor agent transfer succeeded", "#DCFCE7", "#16A34A")
    draw_badge(12.5, 4.4, 3.5, 0.95, "AGENT_UNAVAILABLE", "Handoff failed & caller\nchose no callback", "#FEE2E2", "#DC2626")

    # Row 2: Direct Exits
    draw_badge(8.0, 3.0, 3.4, 0.95, "NOT_INTERESTED", "Patient refusal / decline\nor 'do not call' request", "#FEE2E2", "#DC2626")
    draw_badge(12.5, 3.0, 3.5, 0.95, "CALL_SETUP_FAILED", "Connect API failure\n(rate limit / invalid number)", "#FEE2E2", "#B91C1C")

    # Row 3: Callbacks & Retries
    draw_badge(8.0, 1.6, 3.4, 0.95, "CALLBACK_SCHEDULED", "Specific date/time requested\nor 24h auto-reschedule", "#E0F2FE", "#0284C7")
    draw_badge(12.5, 1.6, 3.5, 0.95, "MAX_RETRIES_EXHAUSTED", "Auto-reschedules reached cap\n(maxRescheduleAttempts=10)", "#FEF2F2", "#991B1B")

    # Row 4: Unspecified Callback
    draw_badge(8.0, 0.3, 3.4, 0.95, "CALLBACK_UNSPECIFIED", "Callback requested without\nspecific time slot", "#E0E7FF", "#4F46E5")

    # Transitions
    draw_transition((3.4, 4.88), (4.2, 4.88), "Dial Placed", color="#2563EB")
    draw_transition((7.0, 4.88), (8.0, 4.88), "Call Success / Transfer", color="#16A34A")
    draw_transition((11.4, 4.88), (12.5, 4.88), "Agent Busy / No Callback", color="#DC2626")

    draw_transition((7.0, 4.5), (8.0, 3.5), "Refusal / Decline", color="#DC2626")
    draw_transition((7.0, 4.2), (12.5, 3.4), "API Failure", rad=-0.05, color="#B91C1C")

    draw_transition((5.6, 4.4), (8.0, 2.1), "Voicemail / Busy / Telecom / Unresolved", rad=0.05, color="#0284C7")
    draw_transition((5.6, 4.4), (8.0, 0.78), "Unspecified Time", rad=0.15, color="#4F46E5")

    # 24h Reschedule Loop: curves from bottom of CALLBACK_SCHEDULED back to bottom of PENDING
    draw_transition((8.0, 1.8), (2.1, 4.4), "EventBridge Schedule fires at T+24h (attempts < 10)", rad=0.25, color="#0284C7")
    draw_transition((11.4, 2.08), (12.5, 2.08), "Attempts >= 10", color="#991B1B")

    plt.tight_layout()
    output_path = os.path.join(OUTPUT_DIR, "status_state_diagram.png")
    plt.savefig(output_path, dpi=250, bbox_inches="tight")
    plt.close()
    print(f"Generated {output_path}")


def create_sequence_diagram():
    fig, ax = plt.subplots(figsize=(17, 12.5), dpi=250)
    ax.set_xlim(0, 17)
    ax.set_ylim(0, 12.5)
    ax.axis("off")

    ax.text(8.5, 12.0, "Cara Health Bot — Single Call End-to-End Sequence Diagram", 
            ha="center", va="center", fontsize=18, fontweight="bold", color="#0F172A")
    ax.text(8.5, 11.65, "Chronological Flow from Schedule Trigger to Outreach, AI Coaching, Agent Handoff & Disposition", 
            ha="center", va="center", fontsize=10.5, color="#475569", style="italic")

    actors = [
        ("EventBridge\nScheduler", 1.2),
        ("Campaign\nDialer", 3.4),
        ("DynamoDB\nState", 5.6),
        ("Amazon\nConnect", 8.0),
        ("Patient\n(Caller)", 10.4),
        ("Lex / Q\nin Connect", 12.8),
        ("Human\nSpecialist", 15.2),
    ]

    for name, x in actors:
        rect = patches.FancyBboxPatch((x - 0.95, 10.3), 1.9, 0.85, boxstyle="round,pad=0.05,rounding_size=0.1",
                                      facecolor="#F1F5F9", edgecolor="#475569", linewidth=1.2)
        ax.add_patch(rect)
        ax.text(x, 10.72, name, ha="center", va="center", fontsize=8.5, fontweight="bold", color="#0F172A")
        ax.plot([x, x], [0.6, 10.3], color="#94A3B8", linestyle="--", linewidth=1.0)

    step_y = 9.8
    step_gap = 0.50

    def draw_msg(x_from, x_to, text, is_async=False, color="#1E293B", note=""):
        nonlocal step_y
        style = "Simple,tail_width=0.3,head_width=3.0,head_length=5.0"
        arrow = patches.FancyArrowPatch((x_from, step_y), (x_to, step_y),
                                        arrowstyle=style, color=color, linewidth=1.1,
                                        linestyle="--" if is_async else "-")
        ax.add_patch(arrow)
        tx = (x_from + x_to) / 2
        ax.text(tx, step_y + 0.11, text, ha="center", va="bottom", fontsize=7.5, fontweight="bold", color=color,
                bbox=dict(boxstyle="round,pad=0.08", facecolor="#FFFFFF", edgecolor="#CBD5E1", lw=0.6, alpha=0.92))
        if note:
            ax.text(tx, step_y - 0.13, note, ha="center", va="top", fontsize=6.8, color="#64748B")
        step_y -= step_gap

    draw_msg(1.2, 3.4, "1. Trigger (campaign-start / prewarm)", note="Fires at scheduledAt UTC (T-60s prewarm)")
    draw_msg(3.4, 5.6, "2. Atomic Claim: PENDING -> IN_PROGRESS", note="ConditionalCheckExpression on status=PENDING")
    draw_msg(3.4, 8.0, "3. start_outbound_voice_contact", note="Passes full patient attributes + AMD config")
    draw_msg(8.0, 10.4, "4. Telephony Outbound Dial & Ringing", note="Customer receives outbound call on phone")
    draw_msg(10.4, 8.0, "5. Call Answered (Human Pick up)", note="AMD classifies HUMAN_ANSWERED")
    draw_msg(8.0, 10.4, "6. Opening Greeting & Identity Verification Prompt", note="Connect plays prompt via Polly Neural TTS")
    draw_msg(10.4, 12.8, "7. Spoken Response: 'Yes, speaking'", note="Voice streamed directly to Lex Identity Bot")
    draw_msg(12.8, 8.0, "8. Identity Confirmed (0.90 NLU confidence)", note="Lex returns IdentityConfirmed intent")
    draw_msg(8.0, 12.8, "9. Create Wisdom Session & Initialize Custom Context", note="session-context Lambda sets customerName, phone")
    draw_msg(8.0, 10.4, "10. AI Coaching Greeting & Conversation Turn", note="Bedrock Nova Lite generates dynamic empathetic response")
    draw_msg(10.4, 12.8, "11. 'I need help with my medications'", note="Customer speech streamed to Q in Connect")
    draw_msg(12.8, 8.0, "12. Tool Invocation: EscalateToHuman", note="Model triggers clinical specialist escalation")
    draw_msg(8.0, 15.2, "13. checkAgentAvailability & TransferParticipantToThirdParty", note="Connect transfers leg to specialist phone")
    draw_msg(15.2, 10.4, "14. Human Specialist & Patient Live Discussion", note="Specialist speaks directly with verified patient")
    draw_msg(10.4, 8.0, "15. Call Completed & Disconnected", note="THIRD_PARTY_DISCONNECT / CUSTOMER_DISCONNECT")
    draw_msg(8.0, 3.4, "16. EventBridge DISCONNECTED Event", is_async=True, note="Contains contactId, instanceArn")
    draw_msg(3.4, 8.0, "17. describe_contact (Read Attributes & DisconnectReason)", note="Evaluates transfer outcome & duration")
    draw_msg(3.4, 5.6, "18. Finalize Patient: IN_PROGRESS -> COMPLETED", note="Appends attempt record to callAttempts list")

    plt.tight_layout()
    output_path = os.path.join(OUTPUT_DIR, "sequence_diagram.png")
    plt.savefig(output_path, dpi=250, bbox_inches="tight")
    plt.close()
    print(f"Generated {output_path}")


if __name__ == "__main__":
    create_architecture_diagram()
    create_contact_flow_diagram()
    create_status_state_diagram()
    create_sequence_diagram()
    print("All diagrams successfully re-created in docs/")
