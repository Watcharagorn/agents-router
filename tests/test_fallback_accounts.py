"""Fallback accounts: used only after every regular account."""

import time
import unittest

from helpers import TempHome

from cli_subagents import accounts
from cli_subagents.config import Account, RuntimeConfig, dump_config, load_config, save_config


class FallbackAccountTests(TempHome):
    def runtime(self) -> RuntimeConfig:
        return RuntimeConfig(
            runtime="codex",
            primary=self.primary,
            include_primary=True,
            accounts=[
                Account(name="free", home=self.root / "free", fallback=True),
                Account(name="paid", home=self.root / "paid"),
            ],
        )

    def test_fallback_comes_last_even_when_least_recently_used(self):
        runtime = self.runtime()
        # The regular accounts were used more recently than the fallback one.
        for name in ("primary", "paid"):
            accounts.update_usage("codex", name, last_used=time.time())
        ranked, _ = accounts.ranked_accounts(runtime, now=time.time())
        self.assertEqual([account.name for account in ranked][-1], "free")
        self.assertEqual(len(ranked), 3)

    def test_fallback_is_used_when_the_others_are_excluded(self):
        ranked, _ = accounts.ranked_accounts(self.runtime(), exclude={"primary", "paid"})
        self.assertEqual([account.name for account in ranked], ["free"])

    def test_fallback_survives_a_config_round_trip(self):
        config = load_config()
        config.runtimes["codex"] = self.runtime()
        text = dump_config(config)
        self.assertIn("fallback = true", text)
        save_config(config)
        loaded = load_config().runtime("codex")
        flags = {account.name: account.fallback for account in loaded.accounts}
        self.assertEqual(flags, {"free": True, "paid": False})


if __name__ == "__main__":
    unittest.main()
