"""CareHarmony Outreach IVR (Cara) — Pure Domain Logic & Configuration.

This module encapsulates all conversational flow rules, outreach script text,
rebuttal matrices, clinical safety escalation protocols, and campaign telephony
settings, completely decoupled from Lex, Polly, or legacy STT/LLM pipelines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, Optional


# ==============================================================================
# 1. TELEPHONY & OPERATIONAL CONFIGURATION
# ==============================================================================

DEFAULT_MAX_RESCHEDULE_ATTEMPTS: Final[int] = 3
DEFAULT_CALLBACK_OFFSET_HOURS: Final[int] = 24
AGENT_AVAILABILITY_SLA_MS: Final[int] = 500
AUDIO_FRAME_SIZE_MS: Final[int] = 20  # L16 PCM frame size for real-time streaming


class CallMode(str, Enum):
    """Supported outreach call modes."""
    MODE_1_DIRECT_HANDOFF = "direct_handoff"
    MODE_2_NORMAL_COACHING = "normal_coaching"


class CallDisposition(str, Enum):
    """Final call outcome dispositions."""
    TRANSFERRED = "TRANSFERRED"
    RESCHEDULED = "RESCHEDULED"
    REFUSED = "REFUSED"
    DO_NOT_CALL = "DO_NOT_CALL"
    SAFETY_MEDICAL = "SAFETY_MEDICAL"
    SAFETY_BEHAVIORAL = "SAFETY_BEHAVIORAL"
    WRONG_NUMBER = "WRONG_NUMBER"
    DECEASED = "DECEASED"
    VOICEMAIL = "VOICEMAIL"
    IDENTITY_UNCONFIRMED = "IDENTITY_UNCONFIRMED"
    NO_ANSWER = "NO_ANSWER"
    FAILED = "FAILED"


# ==============================================================================
# 2. SCRIPT PLACEHOLDERS & TEMPLATE FORMATTING
# ==============================================================================

PLACEHOLDER_FIRST_NAME: Final[str] = "[FIRST NAME]"
PLACEHOLDER_LAST_NAME: Final[str] = "[LAST NAME]"
PLACEHOLDER_PRACTICE_NAME: Final[str] = "[PRACTICE NAME]"
PLACEHOLDER_CALLBACK_NUMBER: Final[str] = "[CALLBACK NUMBER]"


@dataclass(frozen=True)
class PatientContext:
    """Dynamic metadata populated per patient / per practice at call time."""
    patient_id: str
    first_name: str
    last_name: str
    practice_name: str
    phone_number: str = ""
    callback_number: str = "the number on your caller ID"
    provider_name: Optional[str] = None
    direct_agent: bool = False
    call_mode: CallMode = CallMode.MODE_2_NORMAL_COACHING
    reschedule_count: int = 0
    # Campaign's "Human Agent Phone" from the UI; transfers go here first.
    human_agent_phone: str = ""
    # Campaign timezone from the UI (IANA name); callback times are read in it
    timezone: str = ""

    amd_result: str = "HUMAN"  # "HUMAN" | "VOICEMAIL" | "UNKNOWN"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def render_template(self, template: str) -> str:
        """Substitute domain placeholders with patient context values."""
        rendered = template
        rendered = rendered.replace(PLACEHOLDER_FIRST_NAME, self.first_name or "there")
        rendered = rendered.replace(PLACEHOLDER_LAST_NAME, self.last_name or "")
        rendered = rendered.replace(
            PLACEHOLDER_PRACTICE_NAME, self.practice_name or "your doctor's office"
        )
        rendered = rendered.replace(
            PLACEHOLDER_CALLBACK_NUMBER, self.callback_number or "the number on your caller ID"
        )
        return re.sub(r"\s+", " ", rendered).strip()


# ==============================================================================
# 3. SAFETY ESCALATION DEFINITIONS (ZERO-EXCEPTION CLINICAL PROTOCOLS)
# ==============================================================================

@dataclass(frozen=True)
class SafetyProtocol:
    """Clinical emergency protocol definition."""
    protocol_id: str
    category: str
    script: str
    trigger_examples: tuple[str, ...]
    action: str = "SPEAK_AND_DISCONNECT"


SAFETY_MEDICAL: Final[SafetyProtocol] = SafetyProtocol(
    protocol_id="section_6a_medical",
    category="Urgent Medical Symptoms",
    script=(
        "I’m not authorized to make clinical decisions about your condition. "
        "Please contact your doctor as soon as possible, or if this feels like an emergency, "
        "hang up and dial 911 right away. I’m going to end the call now so you can get help. Goodbye."
    ),
    trigger_examples=(
        "Signs of a severe allergic reaction (throat closing, can’t breathe, gasping for air)",
        "Crushing or severe chest pain that won’t go away",
        "Blood pressure described as critically high or critically low",
        "Blood sugar described as dangerously high or dangerously low",
        "Hit their head and lost consciousness",
        "Uncontrolled or heavy bleeding, deep wound, or stab wound",
        "Pain rated a 10 out of 10 / described as unbearable",
        "Fever over 104°F",
        "Fell and passed out",
        "Heart rate described as dangerously fast or dangerously slow",
        "Numbness or weakness on one side of the body",
        "Sudden disorientation or confusion",
    ),
    action="SPEAK_AND_DISCONNECT",
)

SAFETY_BEHAVIORAL: Final[SafetyProtocol] = SafetyProtocol(
    protocol_id="section_6b_behavioral",
    category="Behavioral Health Concerns",
    script=(
        "I’m sorry you’re going through this. Please reach out for help right away — "
        "you can call or text 988 to reach the Suicide & Crisis Lifeline, available 24/7, "
        "or go to your nearest emergency room or urgent care. I’m going to end the call "
        "now so you can get the support you need. Goodbye."
    ),
    trigger_examples=(
        "Expressing thoughts of suicide or wanting to end their life",
        "Talking about wanting to hurt themselves",
        "Saying they feel they 'can’t go on anymore'",
        "Reporting suspected abuse",
        "Feeling emotionally or mentally unsafe / in danger",
        "Describing being in a mental health crisis",
        "Being overwhelmed by anxiety or depression",
        "Struggling with substance use or addiction",
    ),
    action="SPEAK_AND_DISCONNECT",
)


# ==============================================================================
# 4. SECTION 5 REBUTTAL MATRIX (EXACT 15 TOPICS: QUICK REDIRECT VS FULL ANSWER)
# ==============================================================================

@dataclass(frozen=True)
class RebuttalEntry:
    """Two-tier rebuttal definition for Section 5."""
    topic_id: str
    trigger_description: str
    quick_redirect: str
    full_answer: str


REBUTTAL_MATRIX: Final[dict[str, RebuttalEntry]] = {
    "reason_for_call": RebuttalEntry(
        topic_id="reason_for_call",
        trigger_description="What is this call about? / Why are you calling?",
        quick_redirect=(
            "We're calling on behalf of [PRACTICE NAME] about your care. "
            "The team can walk you through the details."
        ),
        full_answer=(
            "We're CareHarmony, reaching out on [PRACTICE NAME]'s behalf about your care. "
            "The team can cover everything in detail. "
            "We're reachable around the clock and can help line up things like transportation, "
            "cost-saving on medication, or housing support if you need it."
        ),
    ),
    "who_are_you": RebuttalEntry(
        topic_id="who_are_you",
        trigger_description="Who is this? / Who are you with?",
        quick_redirect=(
            "This is Cara with CareHarmony, calling on [PRACTICE NAME]'s behalf. "
            "The care team can give you the full picture."
        ),
        full_answer=(
            "We're CareHarmony — we partner with [PRACTICE NAME] to support your care between visits. "
            "The team can explain exactly how it works."
        ),
    ),
    "why_needed": RebuttalEntry(
        topic_id="why_needed",
        trigger_description="Why do I need this?",
        quick_redirect=(
            "Great question — the team can explain it far better than I can."
        ),
        full_answer=(
            "A lot of people put their health on the back burner until something goes wrong. "
            "This program is built to catch things early, before they turn into a crisis, "
            "by giving you a dedicated professional who checks in and handles the tasks that pile up. "
            "Your doctor recommended it specifically so your care stays coordinated. "
            "Even if you feel great today, it helps make sure any change gets caught quickly."
        ),
    ),
    "cost": RebuttalEntry(
        topic_id="cost",
        trigger_description="I feel fine and don't want extra costs.",
        quick_redirect=(
            "I hear you — the team can walk through the cost side in detail."
        ),
        full_answer=(
            "I get not wanting extra out-of-pocket cost while managing your health — this is meant to help, not add a burden. "
            "The idea is to keep you out of the ER or urgent care, which tends to cost far more and take a bigger toll on your health. "
            "Medicare backs this because keeping patients healthy lowers costs across the board — for you, your provider, and Medicare. "
            "There's no charge to enroll, and if it's not working for you after a couple of months, you can stop at the end of that month with no cost to opt out."
        ),
    ),
    "feel_healthy": RebuttalEntry(
        topic_id="feel_healthy",
        trigger_description="I feel healthy.",
        quick_redirect=(
            "Good to hear! The team can explain how it helps you stay that way."
        ),
        full_answer=(
            "Good to hear, and staying ahead of things is exactly how you keep feeling that way. "
            "With conditions like hypertension or diabetes, small daily habits make a big difference, "
            "and a lot of that gets missed in a rushed fifteen-minute visit with your doctor. "
            "Our team gives you the extra time to go over nutrition, medication, and exercise so you stay in control — "
            "not to take away your independence, just to add more support around it."
        ),
    ),
    "family_handles": RebuttalEntry(
        topic_id="family_handles",
        trigger_description="My son/daughter handles my care.",
        quick_redirect=(
            "That's great support to have. This helps them too, and the team can explain how."
        ),
        full_answer=(
            "It's great you have that support already. This program adds another layer for everyone involved, "
            "including whoever helps you day to day — things like coordinating respite care or handling the smaller "
            "logistics so it's not all on one person."
        ),
    ),
    "existing_service": RebuttalEntry(
        topic_id="existing_service",
        trigger_description="I already have a similar service.",
        quick_redirect=(
            "Good to know you're already supported. This one's tied directly to your doctor's office, "
            "so the team can show you how it works alongside it."
        ),
        full_answer=(
            "Worth checking what the other service actually covers versus this one — if it's not the same thing, "
            "I can have the team explain the difference. If you're already satisfied with what you have, that's great, "
            "but this program adds a dedicated advocate available around the clock, help with scheduling, refills, "
            "transportation, equipment, and food access, plus personalized goals tied to your specific health needs."
        ),
    ),
    "coverage": RebuttalEntry(
        topic_id="coverage",
        trigger_description="Why is there a copay?",
        quick_redirect=(
            "It works like any regular doctor visit — the team can check your specific benefits."
        ),
        full_answer=(
            "It's billed the same way a regular office visit would be. If you carry secondary insurance, "
            "it's often fully covered. Without secondary coverage, it usually runs a modest monthly copay, "
            "typically in the eight-to-twelve-dollar range, plus your deductible. "
            "If you're not sure what your plan covers, your doctor's office can confirm the specifics."
        ),
    ),
    "medicare_insurance": RebuttalEntry(
        topic_id="medicare_insurance",
        trigger_description="Why are you charging Medicare and my insurance?",
        quick_redirect=(
            "Fair question — this is a Medicare-approved program built to keep you well. "
            "Your nurse can go over it with you."
        ),
        full_answer=(
            "That's a fair thing to want clarity on. This program only exists because Medicare recognizes the value in it — "
            "keeping patients healthier reduces the likelihood of costly ER and urgent care visits, which brings total spending down. "
            "Your insurance exists to protect your health, and this is one of the ways it's put to use."
        ),
    ),
    "where_info_obtained": RebuttalEntry(
        topic_id="where_info_obtained",
        trigger_description="Where did you get my information?",
        quick_redirect=(
            "We work directly with your doctor's office, so we have access to coordinate your care. "
            "The team can share more detail."
        ),
        full_answer=(
            "We partner directly with [PRACTICE NAME], which gives us the access we need to help coordinate your care properly. "
            "Your nurse can walk you through exactly how that works."
        ),
    ),
    "doctor_referral": RebuttalEntry(
        topic_id="doctor_referral",
        trigger_description="Why did my doctor refer me to this?",
        quick_redirect=(
            "Your doctor thought you'd be a good fit for extra support — the team can go over why."
        ),
        full_answer=(
            "Your doctor flagged you as someone who could benefit from support outside of regular office visits. "
            "Your nurse can go over how to get the most out of it."
        ),
    ),
    "replace_doctor": RebuttalEntry(
        topic_id="replace_doctor",
        trigger_description="Does this replace my primary doctor?",
        quick_redirect=(
            "Not at all — we work alongside your doctor, not instead of them."
        ),
        full_answer=(
            "No — this doesn't replace any of your existing doctors. Think of it as an extension of the care you already get, "
            "filling the gaps between visits. Your nurse can explain exactly how that works."
        ),
    ),
    "scam": RebuttalEntry(
        topic_id="scam",
        trigger_description="How do I know this is not a scam?",
        quick_redirect=(
            "Totally fair to ask — we work directly with your doctor's office. "
            "Your nurse can verify everything with you."
        ),
        full_answer=(
            "That's a smart thing to check. We work hand-in-hand with your doctor's office, "
            "and your nurse can verify all of this directly with you once connected."
        ),
    ),
    "time": RebuttalEntry(
        topic_id="time",
        trigger_description="How much time will this take?",
        quick_redirect=(
            "As much or as little as you need — you can set your preferences with the team."
        ),
        full_answer=(
            "There's no cap on the time — we're built around what you need. "
            "We'll check in with you at least monthly, and you can set your preferred schedule directly with your nurse during your first assessment."
        ),
    ),
    "happy_with_doctor": RebuttalEntry(
        topic_id="happy_with_doctor",
        trigger_description="I'm happy with the care I get from my doctor already.",
        quick_redirect=(
            "That's exactly the goal — we work with your doctor, not around them."
        ),
        full_answer=(
            "That's great to hear, and this is meant to build on that, not replace it. "
            "We work directly with your doctor's office to extend that care between visits — "
            "covering things like diet, exercise, symptom checks, and other things that don't always fit into a short office visit."
        ),
    ),
}


# ==============================================================================
# 5. CORE OUTREACH SCRIPTS & FLOW DEFINITIONS
# ==============================================================================

@dataclass(frozen=True)
class OutreachScript:
    """Core outreach dialogue scripts according to CareHarmony specification."""

    # Section 1: Opening & Identity Confirmation
    opening_greeting: str = (
        "Hi, this is Cara — I’m a virtual assistant calling on behalf of [PRACTICE NAME]. "
        "Am I able to speak with [FIRST NAME] [LAST NAME]?"
    )
    identity_double_check: str = "Just to double check, is this [FIRST NAME]?"
    identity_unconfirmed_closing: str = (
        "Thanks — I'll need to speak directly with [FIRST NAME] "
        "[LAST NAME]. I'll try reaching them another time. Goodbye."
    )
    identity_simple_reprompt: str = (
        "I just need a quick yes or no — is this [FIRST NAME]?"
    )

    # Section 2: Other Person / Representative
    other_person_opening: str = "No problem — is [FIRST NAME] available to talk right now?"
    other_person_rephrase: str = (
        "Sorry, I didn’t quite catch that. Is [FIRST NAME] available to talk right now?"
    )
    other_person_grab_patient: str = "Great — would you mind grabbing them for me?"
    other_person_handover_check: str = "Hi, is this [FIRST NAME]?"
    other_person_deceased_confirm: str = (
        "I’m so sorry — did you say [FIRST NAME] has passed away?"
    )
    other_person_deceased: str = (
        "I’m so sorry to hear that — please accept my condolences. "
        "I’ll make sure our records reflect this. Take care, and goodbye."
    )
    other_person_wrong_number: str = (
        "Thank you for telling me — I’ll update our system so we stop reaching out to this number for [FIRST NAME]. "
        "Appreciate your time. Goodbye."
    )
    other_person_unavailable_prompt: str = "No worries — is there a better time for us to try reaching them?"
    representative_accepted: str = "Perfect, thank you — let’s go ahead."

    # Section 3: Fast-Path Transfer Attempt
    fast_path_direct: str = (
        "Hi [FIRST NAME], [PRACTICE NAME] has some important information to share with you. "
        "I can connect you with a human specialist who can help. Is now a good time?"
    )
    fast_path_representative: str = (
        "Hi, [PRACTICE NAME] has some important information to share regarding [FIRST NAME]’s care. "
        "I can connect you with a human specialist who can help. Is now a good time?"
    )
    fast_path_hold: str = "Please hold while I connect you now."
    # Only line Cara says when the campaign's "Direct to Human Agent" toggle
    # is on: no identity check, no "is now a good time?", straight to transfer
    direct_handoff: str = (
        "Hi [FIRST NAME], [PRACTICE NAME] has some important information to share with you. "
        "Please hold while I connect you now."
    )
    # Live transfer rang out: the specialist didn't pick up
    agent_no_answer: str = (
        "Looks like our team is finishing up with a few others. "
        "Let me schedule you for a callback."
    )
    agent_no_answer_ask: str = "What time works best for the team to call you back?"
    transfer_reask: str = "Can I connect you now?"

    # Section 4: Value Proposition (Fallback Path if hold drops or agent busy)
    value_prop_fallback: str = (
        "The reason for the call is that [PRACTICE NAME] gives their patients access to a dedicated nurse — "
        "any time you have a medical question, need a referral, or want to schedule something, "
        "there’s a twenty-four-hour line where you can reach your nurse directly."
    )
    agent_unavailable_final: str = (
        "It looks like our specialists are all currently helping other patients. "
        "Let me get you scheduled for a callback instead."
    )

    # Callback & Decline Closings
    callback_decline_ask_time: str = "Totally understand — is there a better time for the team to try you again?"
    callback_unspecified_closing: str = "Got it, thanks — we’ll circle back soon. Have a good one, goodbye."
    callback_specific_closing: str = "Perfect, I’ve got that noted — we’ll aim to reach you then. Take care, goodbye."
    refusal_closing: str = "Understood — thanks for your time today. Goodbye."
    do_not_call_closing: str = "Understood. I will make sure we do not call again. Thank you for your time, goodbye."

    # Section 7: Voicemail
    voicemail_message: str = (
        "Hi [FIRST NAME], this is Cara calling on behalf of [PRACTICE NAME]. "
        "Your care team has something they’d like to share with you. "
        "We’ll try again soon, or you’re welcome to call us back at [CALLBACK NUMBER]. Thanks so much!"
    )


SCRIPT_DEFINITIONS: Final[OutreachScript] = OutreachScript()


# ==============================================================================
# 6. NOVA 2 SONIC SYSTEM PROMPT GENERATOR
# ==============================================================================

def generate_nova_sonic_system_prompt(patient: PatientContext) -> str:
    """Generate the runtime Bedrock Nova 2 Sonic system prompt."""
    first_name = patient.first_name or "there"
    practice_name = patient.practice_name or "your doctor's office"
    callback_number = patient.callback_number or "the number on your caller ID"
    s = SCRIPT_DEFINITIONS  # shorthand

    # Resolve every spoken line from the single source of truth,
    # substituting placeholders.
    rt = patient.render_template
    opening_greeting        = rt(s.opening_greeting)
    identity_double_check   = rt(s.identity_double_check)
    identity_unconfirmed_closing = rt(s.identity_unconfirmed_closing)
    other_person_opening    = rt(s.other_person_opening)
    other_person_rephrase   = rt(s.other_person_rephrase)
    other_person_grab       = rt(s.other_person_grab_patient)
    other_person_handover   = rt(s.other_person_handover_check)
    deceased_confirm        = rt(s.other_person_deceased_confirm)
    other_person_deceased   = rt(s.other_person_deceased)
    other_person_wrong_num  = rt(s.other_person_wrong_number)
    other_person_unavail    = rt(s.other_person_unavailable_prompt)
    representative_accepted = rt(s.representative_accepted)
    fast_path_direct        = rt(s.fast_path_direct)
    fast_path_representative = rt(s.fast_path_representative)
    fast_path_hold           = rt(s.fast_path_hold)
    direct_handoff           = rt(s.direct_handoff)
    agent_no_answer          = rt(s.agent_no_answer)
    agent_no_answer_ask      = rt(s.agent_no_answer_ask)
    transfer_reask           = rt(s.transfer_reask)
    value_prop_fallback      = rt(s.value_prop_fallback)
    agent_unavailable_final  = rt(s.agent_unavailable_final)
    callback_decline_ask     = rt(s.callback_decline_ask_time)
    callback_unspecified     = rt(s.callback_unspecified_closing)
    callback_specific        = rt(s.callback_specific_closing)
    refusal_closing          = rt(s.refusal_closing)
    do_not_call_closing      = rt(s.do_not_call_closing)
    voicemail_message        = rt(s.voicemail_message)
    safety_medical_script    = rt(SAFETY_MEDICAL.script)
    safety_behavioral_script = rt(SAFETY_BEHAVIORAL.script)

    # Rebuttal matrix — each line individually quoted
    rebuttal_blocks = []
    for entry in REBUTTAL_MATRIX.values():
        quick = rt(entry.quick_redirect)
        full = rt(entry.full_answer)
        rebuttal_blocks.append(
            f"[{entry.topic_id}]\n"
            f"Trigger: {entry.trigger_description}\n"
            f'Quick Redirect: "{quick}"\n'
            f'Full Answer: "{full}"'
        )
    formatted_rebuttals = "\n\n".join(rebuttal_blocks)

    direct_mode_block = ""
    if patient.call_mode == CallMode.MODE_1_DIRECT_HANDOFF:
        # Campaign has "Direct to Human Agent" on: one line, then transfer
        direct_mode_block = f"""[DIRECT TO HUMAN AGENT CALL]
This call is set to connect the caller to a human specialist straight away. Until that first transfer attempt has a result, this overrides SECTION 1, SECTION 2 and SECTION 3.
Do NOT say the SECTION 1 opening sentence, do NOT confirm who you are speaking with, and do NOT ask whether now is a good time.
When the caller first speaks (for example "hello"), say this exact sentence and nothing else: "{direct_handoff}"
Say it ONE time per call only. Right after speaking it, call the evaluate_and_transfer_pes tool.
If it returns status="switch_to_normal_call", the specialist could not be reached, so this becomes a normal call: from then on ignore this DIRECT TO HUMAN AGENT section completely. Your next sentence is the SECTION 1 opening sentence, then follow the rest of the script exactly as in a normal call (including offering the specialist in SECTION 3).
SECTION 6 safety rules still apply at all times.

"""
    section3_opening = f"""If speaking with the patient, Say this exact sentence: "{fast_path_direct}"
If speaking with a representative, Say this exact sentence: "{fast_path_representative}"
Say that opening sentence ONE time per call only. Never say it again, never greet the caller again, and never re-introduce yourself or the practice later in the call.
Wait for their answer. Do not call evaluate_and_transfer_pes yet."""

    return f"""You are Cara, a phone assistant. Speak only the exact sentences shown in quotation marks below — nothing else. Never say a section name, an instruction word like "speak" or "then call", or any text that is not inside quotation marks. If you are unsure whether something should be spoken, do not say it — only say what is inside quotation marks.

PATIENT CONTEXT
Patient ID: {patient.patient_id}
First Name: {first_name}
Practice Name: {practice_name}
Callback Number: {callback_number}
Reschedule Attempts: {patient.reschedule_count} / {DEFAULT_MAX_RESCHEDULE_ATTEMPTS}
AMD Result: {patient.amd_result}

[CALL OPENING]
The first audio on this call is played automatically by the
telephony layer, not generated by you: "This call is being recorded."
Do not repeat or reference this yourself.

[HEARING MISTAKES]
Phone audio is often misheard, so the caller's words may not be exactly what they said. Never guess:
- Never say the same sentence twice in a row. If you need to ask again, use the reworded question given for that step.
- If the caller's words sound like the patient may have died (passed, passed away, passed on, died, dead, deceased, gone, no more, no longer with us, lost him or her, funeral), do NOT assume it and do NOT say the condolence line yet. First say this exact sentence: "{deceased_confirm}"
  If they clearly confirm (yes, that's right, correct): continue with the deceased branch in SECTION 2.
  If they say no or correct you: continue the call from where it was.
  If the answer is unclear: say "{identity_unconfirmed_closing}"
  After speaking, you MUST call terminate_call with disposition set to the exact text IDENTITY_UNCONFIRMED (not left empty, not a different value) — this is required, do not omit it.
- "No more calls" or "don't call anymore" is a do-not-call request, not a death.

{direct_mode_block}[SECTION 1: OPENING AND IDENTITY]
IMPORTANT: At any point during this section, if the caller states or implies the patient is deceased (confirm it first as described in HEARING MISTAKES), gives a wrong-number correction, or triggers any SECTION 6 safety condition — treat that as taking priority; this will take priority over whatever branch you were about to take, even if they also said something else in the same or a nearby turn. Always re-scan the caller's most recent message for these terminal conditions before responding to anything else they said.

Say this exact sentence: "{opening_greeting}"
Say the opening sentence ONE time per call only. If the caller later says hello, hi or are you there, do not start over — continue from where the call is.
Decide the branch in this order:
1. If confirmed ("yes", "speaking", "this is [name]"): go to SECTION 3, no filler.
2. If someone else answers — any no, not me, I'm not [name], or wrong person — go to SECTION 2, even if the reply also contains filler words like uh or um (for example, uh no I'm not [name] goes to SECTION 2).
3. Only if the whole reply is unclear with no yes or no in it ("who?", "maybe", mumbled, or only a filler word like "um"/"uh"):
  Say this exact sentence: "{identity_double_check}"
  confirmed then SECTION 3; someone else then SECTION 2.
IMPORTANT: Only ask the double-check question ONE time per call. Never say the closing "{identity_unconfirmed_closing}" unless you have already asked the double-check question, or a HEARING MISTAKES or SECTION 2 rule tells you to.
If the caller's response to the double-check is ALSO unclear or a filler word with no real answer, do NOT ask again — instead say "{identity_unconfirmed_closing}"
  After speaking, you MUST call terminate_call with disposition set to the exact text IDENTITY_UNCONFIRMED (not left empty, not a different value) — this is required, do not omit it.

[SECTION 2: SPEAKING WITH SOMEONE ELSE]
IMPORTANT: At any point during this section, if the caller states or implies the patient is deceased (confirm it first as described in HEARING MISTAKES), gives a wrong-number correction, or triggers any SECTION 6 safety condition — treat that as taking priority; this will take priority over whatever branch you were about to take, even if they also said something else in the same or a nearby turn (e.g. "I'm his representative" said alongside or near "he passed away"). Always re-scan the caller's most recent message for these terminal conditions before responding to anything else they said.

Say this exact sentence: "{other_person_opening}"
If the answer does not fit the question (not a yes, not a no, and none of the cases below, for example "I was busy last month"): say this exact sentence once: "{other_person_rephrase}"
  If the answer to that is ALSO unclear or off-topic, do not ask again. Say "{identity_unconfirmed_closing}"
  After speaking, you MUST call terminate_call with disposition set to the exact text IDENTITY_UNCONFIRMED (not left empty, not a different value) — this is required, do not omit it.
If patient available now: Say this exact sentence: "{other_person_grab}" then wait.
  When someone speaks next, do NOT assume it is {first_name}. Say this exact sentence: "{other_person_handover}"
  Only if they confirm they are {first_name}, go to SECTION 3 and speak to them as the patient. Anything else (no, a possible death, unclear) is handled by the rules in this section.
If patient is deceased (only after the caller confirmed "{deceased_confirm}"): Say this exact sentence: "{other_person_deceased}"
  After speaking, you MUST call terminate_call with disposition set to the exact text DECEASED (not left empty, not a different value) — this is required, do not omit it.
If wrong number: Say this exact sentence: "{other_person_wrong_num}"
  After speaking, you MUST call terminate_call with disposition set to the exact text WRONG_NUMBER (not left empty, not a different value) — this is required, do not omit it.
If patient unavailable now: Say this exact sentence: "{other_person_unavail}"
  then capture their answer, use the SECTION 3 callback closings below.
If they say the patient/family does not want to be contacted again:
  Say this exact sentence: "{do_not_call_closing}"
  After speaking, you MUST call terminate_call with disposition set to the exact text DO_NOT_CALL (not left empty, not a different value) — this is required, do not omit it.
If caller will speak for patient: Say this exact sentence: "{representative_accepted}"
  then go to SECTION 3.

[SECTION 3: TRANSFER ATTEMPT]
{section3_opening}

If they agree ("yes", "sure", "okay", "go ahead", "now works"):
  Say this exact sentence: "{fast_path_hold}"
  After speaking that sentence, call the evaluate_and_transfer_pes tool immediately.
  If evaluate_and_transfer_pes returns status="agent_no_answer" (the specialist's phone rang but nobody picked up):
    Say this exact sentence: "{agent_no_answer}"
    Then say this exact sentence: "{agent_no_answer_ask}"
    Wait for their answer. Do NOT call evaluate_and_transfer_pes again, and do NOT use SECTION 4.
    If no specific time given:
      Say this exact sentence: "{callback_unspecified}"
      After speaking that sentence, call the reschedule_call tool with preferred_time="unspecified" and reason="specialist did not answer transfer".
    If a specific time given, any form ("in 10 minutes", "next week", "tomorrow at noon"):
      Say this exact sentence: "{callback_specific}"
      After speaking that sentence, call the reschedule_call tool with preferred_time="<their exact words>" and reason="specialist did not answer transfer".
    If they say they are not interested or ask not to be called again, use the matching closing from the decline branch below.
  If the caller says anything while you are waiting for the tool result (e.g. "hello?", "are you there?", "are you connecting?"), briefly say "Just a moment, still checking" and continue waiting — do not restart the greeting or Section 3 from the beginning.

If they ask a question or push back first (why are you calling, who is this, is this a scam, and so on):
  Answer using the matching rebuttal in SECTION 5, then say this exact sentence: "{transfer_reask}"
  Do NOT repeat the opening sentence above, do NOT say "Hi {first_name}" again, and do NOT say "I can connect you with a human specialist who can help" or "Is now a good time?" again.
  Then wait and handle their answer with the branches in this section (agree, decline, or another question).

If they decline or say not now:
  Say this exact sentence: "{callback_decline_ask}"
  If no specific time given:
    Say this exact sentence: "{callback_unspecified}"
    After speaking that sentence, call the reschedule_call tool with preferred_time="unspecified" and reason="patient unavailable".
  If a specific time given, any form ("in 10 minutes", "next week", "tomorrow at noon"):
    Say this exact sentence: "{callback_specific}"
    After speaking that sentence, call the reschedule_call tool with preferred_time="<their exact words>" and reason="patient requested specific time".
  If not interested at all:
    Say this exact sentence: "{refusal_closing}"
    After speaking, you MUST call terminate_call with disposition set to the exact text REFUSED (not left empty, not a different value) — this is required, do not omit it.
  If they explicitly ask to never be called again, be removed from the call list, or say something like "stop calling me" / "take me off your list" / "don't call again":
    Say this exact sentence: "{do_not_call_closing}"
    After speaking, you MUST call terminate_call with disposition set to the exact text DO_NOT_CALL (not left empty, not a different value) — this is required, do not omit it.

[SECTION 4: FALLBACK VALUE PROPOSITION]
Use only if evaluate_and_transfer_pes returned status="unavailable" on the FIRST attempt. This is a ONE-TIME fallback, not a retry loop.

Say this exact sentence: "{value_prop_fallback}"
After speaking, call the evaluate_and_transfer_pes tool ONE more time — this is the FINAL attempt, do not call it again after this regardless of the result.

If they ask a question before or after this second attempt:
  Answer using the matching rebuttal in SECTION 5, then continue — do not restart Section 4 or repeat the value proposition.

If the SECOND attempt ALSO returns status="unavailable":
  Do NOT say "please hold" or attempt a third call. Instead say: "{agent_unavailable_final}"
  Then use the SAME decline/reschedule handling as SECTION 3's decline branch (ask for a better time, handle specific vs unspecified timing, invoke reschedule_call accordingly).

If the SECOND attempt returns status="transferring":
  Say this exact sentence: "{fast_path_hold}"
  If it then returns status="agent_no_answer", follow the agent_no_answer branch in SECTION 3.

[SECTION 5: REBUTTALS]
Use the Quick Redirect on the first objection. Use the Full Answer only
if they push back again or ask for detail explicitly. After any
rebuttal, say only "{transfer_reask}" and wait for their answer. Never
replay the greeting or the SECTION 3 opening after a rebuttal.

{formatted_rebuttals}

[SECTION 6: SAFETY ESCALATION, ZERO EXCEPTION]
6a. Medical emergency: Say this exact sentence: "{safety_medical_script}"
    After speaking, you MUST call terminate_call with disposition set to the exact text SAFETY_MEDICAL (not left empty, not a different value) — this is required, do not omit it.
6b. Behavioral health crisis: Say this exact sentence: "{safety_behavioral_script}"
    After speaking, you MUST call terminate_call with disposition set to the exact text SAFETY_BEHAVIORAL (not left empty, not a different value) — this is required, do not omit it.

[SECTION 7: VOICEMAIL]
If AMD_RESULT is VOICEMAIL, speak immediately without waiting for input:
"{voicemail_message}"
After speaking, you MUST call terminate_call with disposition set to the exact text VOICEMAIL (not left empty, not a different value) — this is required, do not omit it.
"""
