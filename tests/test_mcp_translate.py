import json
import os
from pathlib import Path
import sys
import unittest

from helpers import TempHome

from cli_subagents import jobs, mcp_translate

FAKE_CODEX = Path(__file__).with_name("fake_codex.py")
SECRET = "s3cr3t-value-123"


def overrides(parts):
    return [parts[i + 1] for i in range(0, len(parts), 2)]


class TranslateTests(TempHome):
    def collect(self):
        return mcp_translate.collect_servers(self.primary, str(self.project), environ={"HOME_VAR": "/h"})

    def test_collect_scopes_project_file_and_self_filter(self):
        state = {
            "mcpServers": {
                "a": {"command": "npx", "args": ["x", "${HOME_VAR}/y"]},
                "cli-subagents": {"command": "csa"},
                "n": {"command": "z", "env": {"K": "${NOPE}"}},
            },
            "projects": {
                str(self.project): {
                    "mcpServers": {"b": {"type": "http", "url": "https://e/mcp"}},
                    "enabledMcpjsonServers": ["p"],
                }
            },
        }
        (self.primary / ".claude.json").write_text(json.dumps(state), encoding="utf-8")
        (self.project / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"p": {"command": "p"}, "q": {"command": "q"}}}), encoding="utf-8"
        )
        got = self.collect()
        self.assertEqual(sorted(got.servers), ["a", "b", "p"])
        self.assertEqual(got.servers["a"]["args"], ["x", "/h/y"])
        text = "\n".join(got.warnings)
        self.assertIn("NOPE", text)
        self.assertIn("'q'", text)

    def test_stdio_conversion_keeps_values_off_argv(self):
        out = mcp_translate.to_codex(
            {"fs": {"command": "npx", "args": ["-y", "pkg"], "cwd": "C:\\w", "env": {"API_KEY": SECRET}}}, set(), {"PATH": "p"}
        )
        self.assertEqual(out.names, ["fs"])
        self.assertEqual(out.env, {"API_KEY": SECRET})
        flat = overrides(out.overrides)
        self.assertIn('mcp_servers.fs.command="npx"', flat)
        self.assertIn('mcp_servers.fs.args=["-y", "pkg"]', flat)
        self.assertIn('mcp_servers.fs.env_vars=["API_KEY"]', flat)
        self.assertIn('mcp_servers.fs.cwd="C:\\\\w"', flat)
        self.assertNotIn(SECRET, " ".join(out.overrides))

    def test_http_conversion_uses_env_references(self):
        out = mcp_translate.to_codex(
            {"web": {"type": "http", "url": "https://e/mcp", "headers": {"Authorization": f"Bearer {SECRET}", "X-Team": "t1"}}},
            set(),
            {},
        )
        flat = overrides(out.overrides)
        self.assertIn('mcp_servers.web.url="https://e/mcp"', flat)
        self.assertTrue(any(item.startswith("mcp_servers.web.bearer_token_env_var=") for item in flat))
        self.assertTrue(any("env_http_headers" in item and "X-Team" in item for item in flat))
        self.assertNotIn(SECRET, " ".join(out.overrides))
        self.assertIn(SECRET, out.env.values())
        self.assertIn("t1", out.env.values())

    def test_skips_with_reasons(self):
        out = mcp_translate.to_codex(
            {
                "mine": {"command": "m"},
                "ev": {"type": "sse", "url": "https://e/sse"},
                "arg": {"command": "c", "args": [f"--api-key={SECRET}"]},
                "urlq": {"type": "http", "url": "https://e/m?token=1"},
                "clash": {"command": "c", "env": {"PATH": "other"}},
                "helper": {"type": "http", "url": "https://e", "headersHelper": "x"},
                "ok": {"command": "ok"},
            },
            {"mine"},
            {"PATH": "p"},
        )
        self.assertEqual(out.names, ["ok"])
        text = "\n".join(out.warnings)
        for name in ("ev", "arg", "urlq", "clash", "helper"):
            self.assertIn(f"'{name}'", text)
        self.assertNotIn("'mine'", text)  # a name the user already has isn't an error
        self.assertNotIn(SECRET, text)

    def test_same_env_var_two_values_skips_second(self):
        out = mcp_translate.to_codex(
            {"a": {"command": "a", "env": {"K": "1"}}, "b": {"command": "b", "env": {"K": "2"}}}, set(), {}
        )
        self.assertEqual(out.names, ["a"])
        self.assertEqual(len(out.warnings), 1)

    def test_agy_shape(self):
        servers, warnings = mcp_translate.to_agy(
            {
                "s": {"command": "c", "args": ["a"], "env": {"K": "v"}},
                "h": {"type": "http", "url": "https://e", "headers": {"A": "b"}},
                "w": {"type": "ws", "url": "ws://x"},
            }
        )
        self.assertEqual(servers["s"], {"command": "c", "args": ["a"], "env": {"K": "v"}})
        self.assertEqual(servers["h"], {"serverUrl": "https://e", "headers": {"A": "b"}})
        self.assertEqual(len(warnings), 1)


class CodexWiringTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ["CLI_SUBAGENTS_CODEX_COMMAND"] = json.dumps([sys.executable, str(FAKE_CODEX)])
        claude = self.root / "claude"
        claude.mkdir()
        (claude / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "fs": {"command": "npx", "env": {"API_KEY": SECRET}},
                        "mine": {"command": "other"},
                        "badsse": {"type": "sse", "url": "https://e"},
                    }
                }
            ),
            encoding="utf-8",
        )
        self.claude_dir = claude
        self.codex_dir = self.root / "codexhome"
        self.codex_dir.mkdir()
        (self.codex_dir / "config.toml").write_text('[mcp_servers.mine]\ncommand = "theirs"\n', encoding="utf-8")

    def tearDown(self):
        os.environ.pop("CLI_SUBAGENTS_CODEX_COMMAND", None)
        os.environ.pop("API_KEY", None)
        super().tearDown()

    def config(self, extra=""):
        (self.home / "config.toml").write_text(
            f"[claude]\nprimary = {json.dumps(str(self.claude_dir))}\n"
            f"[codex]\nprimary = {json.dumps(str(self.codex_dir))}\n{extra}\n",
            encoding="utf-8",
        )

    def run_job(self, host="claude"):
        job = jobs.create_job(runtime="codex", prompt="t", cwd=str(self.project), host=host)
        status = jobs.wait_for(job["job_id"], 60)
        self.assertEqual(status["outcome"], "success", status)
        return self.records()[-1], status

    def test_claude_parent_shares_servers(self):
        self.config()
        record, status = self.run_job()
        joined = " ".join(record["argv"])
        self.assertIn("mcp_servers.fs.command", joined)
        self.assertIn("mcp_servers.fs.env_vars", joined)
        self.assertNotIn('mcp_servers.mine.command="other"', joined)
        self.assertNotIn(SECRET, joined)
        self.assertEqual(record["env"]["API_KEY"], SECRET)
        self.assertNotIn(SECRET, json.dumps(status))

    def test_off_switch_and_other_hosts(self):
        self.config("share_parent_mcp = false")
        record, _ = self.run_job()
        self.assertNotIn("mcp_servers.fs.command", " ".join(record["argv"]))
        self.config()
        record, _ = self.run_job(host="codex")
        self.assertNotIn("mcp_servers.fs.command", " ".join(record["argv"]))
        self.assertNotIn("API_KEY", record["env"])


if __name__ == "__main__":
    unittest.main()
