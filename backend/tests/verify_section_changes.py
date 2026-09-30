import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import domain_rules
from domain_rules import PatientContext, generate_nova_sonic_system_prompt

ctx = PatientContext(
    patient_id="P-TEST-001",
    first_name="Alex",
    last_name="Rivera",
    practice_name="Metro Health Clinic",
    callback_number="+12819568434",
    amd_result="HUMAN"
)
prompt = generate_nova_sonic_system_prompt(ctx)

print("=== 1. SYSTEM PROMPT CHECKS ===")
prompt_checks = [
    "CRITICAL INSTRUCTION — VERBATIM LINES",
    "Am I able to speak with",
    "Please hold while I connect you now",
    "I'm not authorized to make clinical decisions",
    "AMD Result:",
]
for c in prompt_checks:
    print(f"  [{'PASS' if c in prompt else 'FAIL'}] {c}")

print("\n=== 2. TOOLS.PY CHECKS ===")
with open("gateway/tools.py", "r") as f:
    tools_code = f.read()
for c in ['"status": "transferring"', '"status": "unavailable"']:
    print(f"  [{'PASS' if c in tools_code else 'FAIL'}] {c}")

print("\n=== 3. PIPELINE.PY CHECKS ===")
with open("gateway/pipeline.py", "r") as f:
    pipe_code = f.read()
for c in ["class AMDHandler", "TRAFFIC_TYPE", "amd_result=self.amd_result"]:
    print(f"  [{'PASS' if c in pipe_code else 'FAIL'}] {c}")

print("\n=== 4. SIP_TRANSPORT.PY CHECKS ===")
with open("gateway/sip_transport.py", "r") as f:
    sip_code = f.read()
for c in ["def parse_invite_headers", "X-*"]:
    print(f"  [{'PASS' if c in sip_code else 'FAIL'}] {c}")

print("\n=== 5. NOVA_SONIC_SERVICE.PY CHECKS ===")
with open("gateway/nova_sonic_service.py", "r") as f:
    nova_code = f.read()
for c in ["_voicemail_watchdog", "TRAFFIC_TYPE"]:
    print(f"  [{'PASS' if c in nova_code else 'FAIL'}] {c}")
