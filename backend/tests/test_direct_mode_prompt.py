"""With the campaign's "Direct to Human Agent" toggle on, Cara says one line
and transfers: no identity check, no "is now a good time?"."""

import unittest

from backend.domain_rules import CallMode, PatientContext, generate_nova_sonic_system_prompt

DIRECT_LINE = ("Hi Tim, Tia Sahu has some important information to share with you. "
               "Please hold while I connect you now.")


def _prompt(mode):
    return generate_nova_sonic_system_prompt(PatientContext(
        patient_id="p1", first_name="Tim", last_name="Cook", practice_name="Tia Sahu", call_mode=mode))


class DirectModePromptTests(unittest.TestCase):
    def test_direct_mode_says_one_line_then_transfers(self):
        prompt = _prompt(CallMode.MODE_1_DIRECT_HANDOFF)
        block = prompt[prompt.index("[DIRECT TO HUMAN AGENT CALL]"):prompt.index("[SECTION 1")]
        self.assertIn(f'"{DIRECT_LINE}"', block)
        self.assertIn("do NOT confirm who you are speaking with", block)
        self.assertIn("call the evaluate_and_transfer_pes tool", block)
        self.assertIn('status="switch_to_normal_call"', block)
        self.assertIn("Your next sentence is the SECTION 1 opening sentence", block)
        self.assertNotIn("follow the agent_no_answer branch", block)

    def test_direct_mode_keeps_the_normal_script_for_the_fallback(self):
        direct = _prompt(CallMode.MODE_1_DIRECT_HANDOFF)
        normal = _prompt(CallMode.MODE_2_NORMAL_COACHING)
        after = lambda p: p[p.index("[SECTION 1"):]
        self.assertEqual(after(direct), after(normal))

    def test_normal_mode_has_no_direct_block(self):
        prompt = _prompt(CallMode.MODE_2_NORMAL_COACHING)
        self.assertNotIn("DIRECT TO HUMAN AGENT", prompt)
        section3 = prompt[prompt.index("[SECTION 3"):prompt.index("If they agree")]
        self.assertIn("Is now a good time?", section3)
        self.assertIn("Do not call evaluate_and_transfer_pes yet.", section3)


if __name__ == "__main__":
    unittest.main()
