import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domain_rules import (
    DEFAULT_CALLBACK_OFFSET_HOURS,
    DEFAULT_MAX_RESCHEDULE_ATTEMPTS,
    REBUTTAL_MATRIX,
    SAFETY_BEHAVIORAL,
    SAFETY_MEDICAL,
    SCRIPT_DEFINITIONS,
    CallMode,
    PatientContext,
    generate_nova_sonic_system_prompt,
)


class TestDomainRules(unittest.TestCase):
    def setUp(self):
        self.patient = PatientContext(
            patient_id="PT-1001",
            first_name="Jane",
            last_name="Smith",
            practice_name="Mercy Medical Group",
            phone_number="+18145550123",
            callback_number="+18005550199",
            direct_agent=False,
            call_mode=CallMode.MODE_2_NORMAL_COACHING,
        )

    def test_placeholder_rendering(self):
        template = SCRIPT_DEFINITIONS.opening_greeting
        rendered = self.patient.render_template(template)
        self.assertIn("Mercy Medical Group", rendered)
        self.assertIn("Jane Smith", rendered)
        self.assertNotIn("[PRACTICE NAME]", rendered)
        self.assertNotIn("[FIRST NAME]", rendered)
        self.assertNotIn("[LAST NAME]", rendered)

    def test_rebuttal_matrix_has_15_topics(self):
        self.assertEqual(len(REBUTTAL_MATRIX), 15)
        for topic_id, entry in REBUTTAL_MATRIX.items():
            self.assertEqual(topic_id, entry.topic_id)
            self.assertTrue(len(entry.quick_redirect) > 0)
            self.assertTrue(len(entry.full_answer) > 0)
            rendered_qr = self.patient.render_template(entry.quick_redirect)
            self.assertNotIn("[PRACTICE NAME]", rendered_qr)

    def test_safety_protocols(self):
        self.assertIn("911", SAFETY_MEDICAL.script)
        self.assertIn("988", SAFETY_BEHAVIORAL.script)
        self.assertTrue(len(SAFETY_MEDICAL.trigger_examples) >= 10)
        self.assertTrue(len(SAFETY_BEHAVIORAL.trigger_examples) >= 8)

    def test_telephony_thresholds(self):
        self.assertEqual(DEFAULT_MAX_RESCHEDULE_ATTEMPTS, 3)
        self.assertEqual(DEFAULT_CALLBACK_OFFSET_HOURS, 24)

    def test_nova_sonic_prompt_generation_mode_2(self):
        prompt = generate_nova_sonic_system_prompt(self.patient)
        self.assertIn("Jane Smith", prompt)
        self.assertIn("Mercy Medical Group", prompt)
        self.assertIn("[SECTION 1: OPENING AND IDENTITY]", prompt)
        self.assertIn("911", prompt)
        self.assertIn("988", prompt)

    def test_nova_sonic_prompt_generation_mode_1(self):
        direct_patient = PatientContext(
            patient_id="PT-1002",
            first_name="Robert",
            last_name="Johnson",
            practice_name="Trinity Clinic",
            phone_number="+18145550199",
            direct_agent=True,
            call_mode=CallMode.MODE_1_DIRECT_HANDOFF,
        )
        prompt = generate_nova_sonic_system_prompt(direct_patient)
        self.assertIn("Robert", prompt)
        self.assertIn("Trinity Clinic", prompt)
        self.assertIn("[DIRECT TO HUMAN AGENT CALL]", prompt)
        self.assertIn("evaluate_and_transfer_pes", prompt)


if __name__ == "__main__":
    unittest.main()
