"""roles_propose: setup's first role table (Q5) and rebalance from remaining quota (Q8, D11)."""

import time
from unittest.mock import patch

import helpers  # noqa: F401  (isolates the test process from your real folders)
from test_setup import SetupBase

from agents_router import accounts, roles_propose
from agents_router.config import load_config


class ProposeBase(SetupBase):
    def setUp(self):
        super().setUp()
        (self.root / "agt").mkdir(exist_ok=True)
        self.runtimes = {"claude", "codex", "antigravity"}
        self._available = patch.object(roles_propose, "_available", side_effect=lambda loaded, name: name in self.runtimes)
        self._available.start()

    def tearDown(self):
        self._available.stop()
        super().tearDown()

    def settings(self, text):
        (self.root / "agt" / "settings.toml").write_text(text, encoding="utf-8")

    def used(self, runtime, share, account="primary"):
        accounts.update_usage(runtime, account, windows={"five_hour": {"utilization": share, "resets_at": time.time() + 3600}})


class SetupProposalTests(ProposeBase):
    def test_read_roles_mix_vendors_builder_strongest_first_decision_maker_one(self):
        proposal = roles_propose.propose(load_config(), "setup")
        table = proposal["roles"]
        self.assertEqual(list(table), ["decision-maker", "reviewer", "builder", "verifier"])
        self.assertEqual(table["builder"]["providers"], ["claude", "codex"])
        self.assertEqual(len(table["decision-maker"]["providers"]), 1)
        for name in ("reviewer", "verifier"):
            runtimes = [item.split(":")[0] for item in table[name]["providers"]]
            self.assertEqual(len(set(runtimes)), 2, (name, runtimes))
        self.assertNotEqual(table["reviewer"]["providers"][0].split(":")[0], "claude")  # not the Builder's vendor first
        self.assertEqual(set(proposal["reasons"]), set(table))
        roles_propose.check(table)  # a valid settings table

    def test_a_runtime_near_its_limit_goes_after_those_with_room(self):
        self.used("claude", 0.95)
        table = roles_propose.propose(load_config(), "setup")["roles"]
        self.assertEqual(table["builder"]["providers"], ["codex", "antigravity"])
        self.runtimes = {"claude", "codex"}
        table = roles_propose.propose(load_config(), "setup")["roles"]
        self.assertEqual(table["builder"]["providers"], ["codex", "claude"])

    def test_no_cli_no_table(self):
        self.runtimes = set()
        self.assertEqual(roles_propose.propose(load_config(), "setup"), {"roles": {}, "reasons": {}})


class RebalanceTests(ProposeBase):
    TABLE = """
[roles.builder]
providers = ["claude", "codex"]

[roles.reviewer]
providers = ["claude", "antigravity"]

[roles.verifier]
enabled = false
providers = ["claude"]
"""

    def test_S4_rebalance_proposes_with_reasons(self):
        self.settings(self.TABLE)
        self.used("claude", 0.92)
        proposal = roles_propose.propose(load_config(), "rebalance")
        self.assertEqual(proposal["roles"]["builder"]["providers"], ["codex", "claude"])
        self.assertIn("8% left (under 20%)", proposal["reasons"]["builder"])
        self.assertEqual(proposal["roles"]["reviewer"]["providers"], ["antigravity"])  # left out while the others run
        self.assertIn("left out", proposal["reasons"]["reviewer"])
        # A role that is off is skipped, and kept whole so writing the table back loses nothing.
        self.assertEqual(proposal["roles"]["verifier"], {"providers": ["claude"], "enabled": False})
        self.assertEqual(proposal["reasons"]["verifier"], "off; skipped")

    def test_S4_rebalance_shows_quota(self):
        self.settings(self.TABLE)
        self.used("claude", 0.92)
        resets = accounts.load_usage("claude")["primary"]["windows"]["five_hour"]["resets_at"]
        at = time.strftime("%H:%M", time.localtime(resets))
        proposal = roles_propose.propose(load_config(), "rebalance")
        quota = {item["runtime"]: item for item in proposal["quota"]}
        self.assertEqual(set(quota), {"claude", "codex", "antigravity"})
        claude = quota["claude"]
        self.assertEqual((claude["account"], claude["left_percent"], claude["resets_at"]), ("primary", 8, resets))
        self.assertIsNotNone(claude["observed_at"])
        self.assertEqual(claude["text"], f"primary 8% left (5h, resets {at})")
        self.assertEqual(quota["antigravity"]["text"], "primary unknown (no usage data)")
        self.assertIsNone(quota["antigravity"]["left_percent"])
        self.assertEqual(quota["codex"]["text"], "primary unknown (no usage data)")
        # the reason for a low account names when it resets
        self.assertIn(f"claude has 8% left (under 20%) until it resets at {at}", proposal["reasons"]["builder"])

    def test_S4_quota_text_is_the_room_line_wording(self):
        from agents_router import usage_view

        self.used("codex", 0.92)
        config = load_config()
        account = config.runtime("codex").primary_account()
        record = accounts.load_usage("codex")["primary"]
        self.assertIn(usage_view.account_quota("codex", account, record, time.time())["text"],
                      usage_view.room_line(config, ["codex"]))

    def test_setup_has_no_quota_block(self):
        self.assertNotIn("quota", roles_propose.propose(load_config(), "setup"))

    def test_nothing_low_no_change(self):
        self.settings(self.TABLE)
        self.used("claude", 0.5)
        proposal = roles_propose.propose(load_config(), "rebalance")
        self.assertEqual(proposal["roles"]["builder"]["providers"], ["claude", "codex"])
        self.assertEqual(proposal["reasons"]["builder"], "no change")

    def test_antigravity_counts_only_its_cooldown(self):
        self.settings('[roles.builder]\nproviders = ["antigravity", "codex"]\n')
        self.used("antigravity", 0.99)  # agy prints no usage figures; a stray one is ignored (D11)
        self.assertEqual(roles_propose.propose(load_config(), "rebalance")["roles"]["builder"]["providers"],
                         ["antigravity", "codex"])
        accounts.update_usage("antigravity", "primary", cooldown_until=time.time() + 600)
        proposal = roles_propose.propose(load_config(), "rebalance")
        self.assertEqual(proposal["roles"]["builder"]["providers"], ["codex", "antigravity"])
        self.assertIn("at its limit until", proposal["reasons"]["builder"])

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            roles_propose.propose(load_config(), "nope")


if __name__ == "__main__":
    import unittest

    unittest.main()
