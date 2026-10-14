import unittest

from reviewer_client import parse_reviewer_output


class ReviewerClientTests(unittest.TestCase):
    def test_accepts_exact_schema(self):
        self.assertEqual(
            parse_reviewer_output('{"decision":"allow","risk_level":"low","user_authorization":"medium","reason":"Scoped test."}')["decision"],
            "allow",
        )

    def test_accepts_json_fence_only(self):
        self.assertEqual(
            parse_reviewer_output('```json\n{"decision":"deny","risk_level":"critical","user_authorization":"unknown","reason":"Risky."}\n```')["decision"],
            "deny",
        )

    def test_accepts_known_pty_fallback_diagnostic(self):
        output = '[pty unavailable: missing dependency]\n{"decision":"allow","risk_level":"low","user_authorization":"high","reason":"Safe."}'
        self.assertEqual(parse_reviewer_output(output)["decision"], "allow")

    def test_rejects_extra_fields(self):
        with self.assertRaises(ValueError):
            parse_reviewer_output('{"decision":"allow","risk_level":"low","user_authorization":"high","reason":"x","command":"run"}')

    def test_rejects_surrounding_prose(self):
        with self.assertRaises(ValueError):
            parse_reviewer_output('Result: {"decision":"allow","risk_level":"low","user_authorization":"high","reason":"x"}')

    def test_rejects_unknown_decision(self):
        with self.assertRaises(ValueError):
            parse_reviewer_output('{"decision":"maybe","risk_level":"low","user_authorization":"high","reason":"x"}')

    def test_rejects_critical_allow(self):
        with self.assertRaises(ValueError):
            parse_reviewer_output('{"decision":"allow","risk_level":"critical","user_authorization":"high","reason":"x"}')

    def test_rejects_high_risk_with_low_authorization(self):
        with self.assertRaises(ValueError):
            parse_reviewer_output('{"decision":"allow","risk_level":"high","user_authorization":"low","reason":"x"}')


if __name__ == "__main__":
    unittest.main()
