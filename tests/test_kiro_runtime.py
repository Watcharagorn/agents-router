"""Kiro CLI as a worker runtime, against tests/fake_kiro.py only (never the real kiro-cli, never `login`)."""

import dataclasses
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from helpers import FAKE_KIRO, TempHome

from agents_router import config, jobs, messages
from agents_router.runtimes import base, get_runtime, kiro

COMMAND = json.loads(FAKE_KIRO)
READ = "--trust-tools=fs_read,glob,grep,web_search,web_fetch"
EDIT = "--trust-tools=fs_read,glob,grep,web_search,web_fetch,fs_write"
NO_FLAGS = kiro.KiroCli("2.30.0", (2, 30, 0))
KIRO_2 = kiro.KiroCli("2.37.1", (2, 37, 1), frozenset(kiro.PROBE_FLAGS))
KIRO_3 = kiro.KiroCli("3.0.0", (3, 0, 0), frozenset(kiro.PROBE_FLAGS))


def spec(**fields):
    values = {"job_id": "j1", "runtime": "kiro", "prompt": "Count the files.", "cwd": str(Path.cwd()), "access": "read"}
    return base.JobSpec(**{**values, **fields})


def v3_events(session, answer, engine="v3"):
    """The probed stream-json shapes (kiro-cli 2.37.1), trimmed: one tool call, then the answer."""

    def update(**fields):
        return {"type": "sessionUpdate", "data": {"sessionId": session, "update": fields}}

    return [
        {"type": "runStarted", "data": {"payloadSchema": "acp", "acpProtocolVersion": 1, "engine": engine}},
        update(sessionUpdate="agent_message_chunk", content={"type": "text", "text": "Looking at it."}),
        update(sessionUpdate="tool_call", toolCallId="toolu_1", title="Read File", kind="read"),
        update(sessionUpdate="tool_call_update", toolCallId="toolu_1", status="completed"),
        update(sessionUpdate="agent_message_chunk", content={"type": "text", "text": answer}),
        {"type": "runFinished", "data": {"sessionId": session, "status": "success", "stopReason": "end_turn",
                                         "finalText": answer, "finalTextTruncated": False}},
    ]


def runner_line(parser, line):
    """One stdout line through the runner's own line handling."""

    from agents_router import runner

    runner.Runner._handle_line(SimpleNamespace(spec=SimpleNamespace(runtime="kiro")), (line + "\n").encode(), parser,
                               SimpleNamespace(name="default", login=None))


class KiroUnitTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name)
        self.argv_log = self.home / "kiro-argv.jsonl"
        self._env = patch.dict(os.environ, {"AGT_KIRO_COMMAND": FAKE_KIRO, "FAKE_KIRO_ARGV_LOG": str(self.argv_log)})
        self._env.start()
        for key in ("FAKE_KIRO_PRESET", "FAKE_KIRO_VERSION", "FAKE_KIRO_FLAGS", "FAKE_KIRO_SCENARIO"):
            os.environ.pop(key, None)
        self.runtime = get_runtime("kiro")
        self.config = config.load_config().runtime("kiro")
        self.account = self.config.primary_account()

    def tearDown(self):
        self._env.stop()
        self._temp.cleanup()

    def test_one_trust_list_for_both_engines(self):
        self.assertEqual(kiro.access_args("read"), [READ])
        self.assertEqual(kiro.access_args("edit"), [EDIT])
        self.assertEqual(kiro.access_args("full"), ["--trust-all-tools"])
        for access in ("read", "edit"):
            self.assertNotIn("execute_bash", kiro.access_args(access)[0])  # no shell without a gate
            self.assertNotIn("shell", kiro.access_args(access)[0])

    def test_engine_is_read_from_kiros_settings_and_none_when_unset(self):
        self.assertIsNone(kiro.engine(self.home))
        (self.home / "settings").mkdir()
        cli_json = self.home / "settings" / "cli.json"
        for content, expected in (({"chat.agentEngine": "v3"}, "v3"), ({"chat": {"agentEngine": "V2"}}, "v2"),
                                  ({"chat.agentEngine": "3"}, "v3"), ({}, None), ({"chat.agentEngine": ""}, None),
                                  ({"chat.agentEngine": "v1"}, "v1"), ({"chat": {"agentEngine": " V1 "}}, "v1"),
                                  ([], None), ({"chat": "v3"}, None)):
            cli_json.write_text(json.dumps(content), encoding="utf-8")
            self.assertEqual(kiro.engine(self.home), expected, content)
        cli_json.write_text("{not json", encoding="utf-8")
        self.assertIsNone(kiro.engine(self.home))

    def test_the_engine_flag_follows_the_setting_kiros_settings_and_the_version(self):
        rows = [  # (engine, chat.agentEngine, kiro-cli, flag)
            ("v2", None, KIRO_3, "v2"), ("v3", None, KIRO_2, "v3"), ("v2", "v3", KIRO_3, "v2"), ("v3", "v2", KIRO_2, "v3"),
            ("auto", "v2", KIRO_3, "v2"), ("auto", "v3", KIRO_2, "v3"),
            ("auto", None, KIRO_3, "v3"), ("auto", None, KIRO_2, "v2"),
            ("auto", None, NO_FLAGS, None), ("auto", "v3", NO_FLAGS, None), ("auto", "v2", NO_FLAGS, None),
            ("auto", "v1", KIRO_3, None), ("auto", "v1", KIRO_2, None), ("v3", "v1", KIRO_3, "v3"),
            ("v2", None, NO_FLAGS, None),  # a forced v2 on a CLI without --v2: that CLI runs V2 anyway
            ("auto", None, kiro.KiroCli(None, None, frozenset(kiro.PROBE_FLAGS)), "v2"),  # flags but no version
        ]
        for setting, configured, cli, flag in rows:
            self.assertEqual(kiro.resolve_engine(setting, configured, cli)[0], flag, (setting, configured, cli))

    def test_a_forced_v3_without_the_flag_fails_clearly(self):
        for cli, version in ((NO_FLAGS, "2.30.0"), (kiro.KiroCli(), "(unknown version)")):
            with self.assertRaises(RuntimeError) as caught:
                kiro.resolve_engine("v3", "v3", cli)
            self.assertEqual(str(caught.exception),
                             f'kiro-cli {version} has no V3 engine; update Kiro or set [kiro] engine = "auto"')

    def test_resolve_engine_explains_its_choice(self):
        failed = kiro.KiroCli("3.0.0", (3, 0, 0), error="chat --help exited 1")
        unknown = kiro.KiroCli(None, None, frozenset(kiro.PROBE_FLAGS))
        for setting in ("auto", "v2", "v3"):
            for configured in (None, "v2", "v3", "v1"):
                for cli in (NO_FLAGS, KIRO_2, KIRO_3, failed, unknown):
                    if setting == "v3" and "--v3" not in cli.flags:
                        with self.assertRaises(RuntimeError):
                            kiro.resolve_engine(setting, configured, cli)
                        continue
                    flag, reason = kiro.resolve_engine(setting, configured, cli)
                    self.assertTrue(reason.startswith(flag or "none"), (setting, configured, cli, reason))
        self.assertEqual(kiro.resolve_engine("auto", None, KIRO_2)[1], "v2 (auto: kiro-cli 2.37.1)")
        self.assertEqual(kiro.resolve_engine("auto", "v1", KIRO_3)[1], "none (chat.agentEngine is v1; Kiro's own setting)")
        self.assertEqual(kiro.resolve_engine("v2", None, NO_FLAGS)[1], "none (kiro-cli 2.30.0 has no --v2; Kiro's own default)")

    def test_probe_reads_the_version_and_the_flags_without_asking_a_model(self):
        self.assertEqual(kiro.probe(COMMAND), NO_FLAGS)
        for preset, expected in (("kiro-2.37.1", KIRO_2), ("kiro-3.0.0", KIRO_3)):
            with patch.dict(os.environ, {"FAKE_KIRO_PRESET": preset}):
                self.assertEqual(kiro.probe(COMMAND), expected)
        with patch.dict(os.environ, {"FAKE_KIRO_VERSION": "3.1.4", "FAKE_KIRO_FLAGS": "--output-format"}):
            self.assertEqual(kiro.probe(COMMAND), kiro.KiroCli("3.1.4", (3, 1, 4), frozenset({"--output-format"})))
        logged = [json.loads(line) for line in self.argv_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(set(map(tuple, logged)), {("--version",), ("chat", "--help")})  # no chat run at all

    def test_a_failing_or_missing_probe_means_no_flags_and_says_why(self):
        with patch.dict(os.environ, {"FAKE_KIRO_PRESET": "kiro-3.0.0", "FAKE_KIRO_SCENARIO": "help-fails"}):
            self.assertEqual(kiro.probe(COMMAND), kiro.KiroCli("3.0.0", (3, 0, 0), error="chat --help exited 1"))
        with patch.dict(os.environ, {"FAKE_KIRO_PRESET": "kiro-3.0.0", "FAKE_KIRO_SCENARIO": "help-hangs"}):
            hung = kiro.probe(COMMAND, timeout=1)
        self.assertEqual((hung.flags, hung.error), (frozenset(), "chat --help timed out after 1 s"))
        missing = kiro.probe([str(self.home / "no-such-kiro-cli")])
        self.assertEqual((missing.version, missing.flags), (None, frozenset()))
        self.assertRegex(missing.error, r"^couldn't run it \(.+\)$")

    def test_a_forced_v3_on_a_failed_probe_says_the_flags_couldnt_be_read(self):
        for scenario, reason in (("help-fails", "chat --help exited 1"), ("help-hangs", "chat --help timed out after 1 s")):
            with patch.dict(os.environ, {"FAKE_KIRO_PRESET": "kiro-3.0.0", "FAKE_KIRO_SCENARIO": scenario}), \
                    patch.object(kiro, "PROBE_TIMEOUT", 1):
                with self.assertRaises(RuntimeError) as caught:
                    self.launch_argv("read", "v3")
                self.assertEqual(str(caught.exception), f"couldn't read kiro-cli's flags: {reason}")
                self.assertEqual(self.launch_argv("read", "v2"), [*COMMAND, "chat", "--no-interactive", "--wrap", "never", READ])
                self.assertEqual(self.launch_argv("read"), [*COMMAND, "chat", "--no-interactive", "--wrap", "never", READ])
        with self.assertRaises(RuntimeError) as caught:
            kiro.resolve_engine("v3", None, kiro.KiroCli(error="couldn't run it (No such file or directory)"))
        self.assertEqual(str(caught.exception), "couldn't read kiro-cli's flags: couldn't run it (No such file or directory)")

    def launch_argv(self, access, engine="auto", configured=None, **fake):
        account = dataclasses.replace(self.account, home=self.home)
        if configured:
            (self.home / "settings").mkdir(exist_ok=True)
            (self.home / "settings" / "cli.json").write_text(json.dumps({"chat.agentEngine": configured}), encoding="utf-8")
        with patch.dict(os.environ, fake):
            launch = self.runtime.launch(dataclasses.replace(self.config, engine=engine), account, spec(access=access), "x", {})
        self.assertFalse([arg for arg in launch.argv if arg.startswith("--trust-workspace")], launch.argv)
        return launch.argv

    def test_the_job_command_names_the_engine_and_uses_one_trust_list(self):
        base_argv = [*COMMAND, "chat", "--no-interactive", "--wrap", "never"]
        stream = ["--output-format", "stream-json"]  # both presets list --output-format
        for preset, flag in (("kiro-3.0.0", "--v3"), ("kiro-2.37.1", "--v2")):
            for access, trust in (("read", READ), ("edit", EDIT), ("full", "--trust-all-tools")):
                self.assertEqual(self.launch_argv(access, FAKE_KIRO_PRESET=preset), [*base_argv, flag, *stream, trust],
                                 (preset, access))
        for engine, flag in (("v2", "--v2"), ("v3", "--v3")):
            for access, trust in (("read", READ), ("edit", EDIT), ("full", "--trust-all-tools")):
                self.assertEqual(self.launch_argv(access, engine, FAKE_KIRO_PRESET="kiro-3.0.0"),
                                 [*base_argv, flag, *stream, trust])
        self.assertEqual(self.launch_argv("read"), [*base_argv, READ])  # an older kiro-cli: no engine flag, as before

    def test_stream_json_is_asked_for_only_when_chat_help_lists_it(self):
        stream = ["--output-format", "stream-json"]
        has = lambda argv: argv[argv.index("--output-format"):argv.index("--output-format") + 2] == stream \
            if "--output-format" in argv else False
        self.assertTrue(has(self.launch_argv("read", FAKE_KIRO_FLAGS="--output-format")))
        self.assertTrue(has(self.launch_argv("read", FAKE_KIRO_PRESET="kiro-2.37.1")))
        self.assertFalse(has(self.launch_argv("read", FAKE_KIRO_FLAGS="--v2,--v3")))
        self.assertNotIn("--output-format", self.launch_argv("read"))
        with patch.dict(os.environ, {"FAKE_KIRO_PRESET": "kiro-3.0.0", "FAKE_KIRO_SCENARIO": "help-fails"}):
            self.assertNotIn("--output-format", self.launch_argv("read"))

    def resume_argv(self, session, engine="auto", **fake):
        account = dataclasses.replace(self.account, home=self.home)
        with patch.dict(os.environ, fake):
            launch = self.runtime.launch(dataclasses.replace(self.config, engine=engine), account,
                                         spec(resume_session=session), "x", {})
        return launch

    def test_a_resumed_session_keeps_its_engine_whatever_the_setting(self):
        v3, v2 = "sess_3e26aa00-0000-4000-8000-000000000001", "61d5aa00-0000-4000-8000-000000000002"
        for engine in ("auto", "v2", "v3"):
            for preset in ("kiro-2.37.1", "kiro-3.0.0"):
                argv = self.resume_argv(v3, engine, FAKE_KIRO_PRESET=preset).argv
                self.assertIn("--v3", argv)
                self.assertNotIn("--v2", argv)
                self.assertEqual(argv[argv.index("--resume-id") + 1], v3)
                argv = self.resume_argv(v2, engine, FAKE_KIRO_PRESET=preset).argv
                self.assertIn("--v2", argv, (engine, preset))
                self.assertNotIn("--v3", argv)
        # a forced v3 on a kiro-cli without the flags: a known session passes no engine flag and isn't refused
        for session in (v3, v2):
            argv = self.resume_argv(session, "v3").argv
            self.assertFalse({"--v2", "--v3"} & set(argv), argv)
            self.assertEqual(argv[argv.index("--resume-id") + 1], session)
        # an id of no known shape: the engine a new job would get, and a warning that says so
        odd = self.resume_argv("abc123", FAKE_KIRO_PRESET="kiro-3.0.0")
        self.assertIn("--v3", odd.argv)
        self.assertIn("couldn't tell Kiro session abc123's engine", odd.warnings[0])
        self.assertEqual(kiro.session_engine(v3), "v3")
        self.assertEqual(kiro.session_engine(v2), "v2")
        self.assertIsNone(kiro.session_engine("abc123"))

    def test_a_resumed_session_requires_a_successful_engine_flag_probe(self):
        sessions = (("sess_3e26aa00-0000-4000-8000-000000000001", "v3"),
                    ("61d5aa00-0000-4000-8000-000000000002", "v2"))
        for session, engine in sessions:
            for scenario, reason in (("help-fails", "chat --help exited 1"),
                                     ("help-hangs", "chat --help timed out after 1 s")):
                with self.subTest(session=session, scenario=scenario), patch.object(kiro, "PROBE_TIMEOUT", 1):
                    with self.assertRaises(RuntimeError) as caught:
                        self.resume_argv(session, FAKE_KIRO_PRESET="kiro-2.37.1", FAKE_KIRO_SCENARIO=scenario)
                    self.assertEqual(str(caught.exception),
                                     f"couldn't read kiro-cli's flags: {reason}; session {session} needs --{engine}")

    def test_auto_follows_chat_agent_engine(self):
        self.assertIn("--v2", self.launch_argv("read", configured="v2", FAKE_KIRO_PRESET="kiro-3.0.0"))
        self.assertIn("--v3", self.launch_argv("read", configured="v3", FAKE_KIRO_PRESET="kiro-2.37.1"))
        self.assertIn("--v3", self.launch_argv("read", FAKE_KIRO_PRESET="kiro-3.0.0"))  # unset: the version rule

    def test_auto_leaves_an_engine_agt_doesnt_name_to_kiro(self):
        argv = self.launch_argv("read", configured="v1", FAKE_KIRO_PRESET="kiro-3.0.0")
        self.assertFalse({"--v2", "--v3"} & set(argv), argv)

    def test_a_forced_v3_launch_on_an_old_kiro_cli_is_refused(self):
        with self.assertRaises(RuntimeError) as caught:
            self.launch_argv("read", "v3")
        self.assertIn("kiro-cli 2.30.0 has no V3 engine", str(caught.exception))

    def test_launch(self):
        launch = self.runtime.launch(self.config, self.account, spec(model="claude-sonnet", effort="high"), "Count the files.", {})
        self.assertEqual(launch.argv[2:5], ["chat", "--no-interactive", "--wrap"])
        self.assertIn("--trust-tools=fs_read,glob,grep,web_search,web_fetch", launch.argv)
        self.assertEqual(launch.argv[launch.argv.index("--model") + 1], "claude-sonnet")
        self.assertEqual(launch.argv[launch.argv.index("--effort") + 1], "high")
        self.assertNotIn("Count the files.", launch.argv)  # the task goes on stdin, never the command line
        self.assertTrue(launch.stdin.endswith("Count the files."))
        self.assertEqual((launch.env["AGT_JOB_ID"], launch.env["AGT_DEPTH"]), ("j1", "1"))
        edit = self.runtime.launch(self.config, self.account, spec(access="edit"), "x", {})
        self.assertIn("not run shell commands", edit.warnings[0])

    def test_answers_and_failures(self):
        parser = self.runtime.new_parser()
        for line in ("Looked at it.", "Done: 12 files."):
            parser.feed({"type": "text", "text": line})
        result = parser.finish(0, "")
        self.assertEqual((result.outcome, result.text), (base.SUCCESS, "Looked at it.\nDone: 12 files."))
        self.assertEqual(kiro.KiroParser().finish(1, "You have reached the usage limit for this month.").outcome, base.USAGE_LIMIT)
        self.assertEqual(kiro.KiroParser().finish(1, "error: Not logged in. Run kiro-cli login").outcome, base.AUTH)
        failed = kiro.KiroParser().finish(3, "")
        self.assertEqual(failed.outcome, base.ERROR)
        self.assertIn("MCP server didn't start", failed.message)
        self.assertEqual(kiro.KiroParser().finish(0, "").outcome, base.ERROR)  # no answer at all

    def stream_parser(self, *events, lines=()):
        parser = self.runtime.new_parser()
        for line in [*lines, *(json.dumps(event) for event in events)]:
            runner_line(parser, line)
        return parser

    def test_a_v3_stream_gives_the_answer_session_and_tools(self):
        parser = self.stream_parser(*v3_events("sess_aaaa-1", "Done: 12 files."))
        self.assertEqual(parser.progress(), {"tool_uses": 1, "last_tool": "Read File",
                                             "last_text": "Looking at it.Done: 12 files."})
        result = parser.finish(0, "2026-11-04T10:00:00Z [INFO] kiro_agent::engine: starting\n[DEBUG] x\n")
        self.assertEqual((result.outcome, result.text, result.session_id), (base.SUCCESS, "Done: 12 files.", "sess_aaaa-1"))

    def test_a_v2_stream_with_metadata_events(self):
        session = "61d5aa00-0000-4000-8000-000000000002"
        events = v3_events(session, "Done.", engine="v2")
        events.insert(-1, {"type": "metadata", "data": {"sessionId": session, "meteringUsage": [{"value": 0.08, "unit": "credit"}]}})
        result = self.stream_parser(*events).finish(0, "")
        self.assertEqual((result.outcome, result.text, result.session_id), (base.SUCCESS, "Done.", session))

    def test_a_truncated_final_text_is_read_from_the_chunks_since_the_last_tool(self):
        events = v3_events("sess_b", "Done: the whole answer.")
        events[-1]["data"].update(finalText="Done: the wh", finalTextTruncated=True)
        result = self.stream_parser(*events).finish(0, "")
        self.assertEqual((result.outcome, result.text), (base.SUCCESS, "Done: the whole answer."))

    def test_stream_progress_keeps_a_tail_without_truncating_the_answer(self):
        answer = "The whole answer." * 400
        events = v3_events("sess_long", answer)
        events[-1]["data"].update(finalText=answer[:10], finalTextTruncated=True)
        events.insert(-1, {"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Done."}}}})
        parser = self.stream_parser(*events, lines=("Text before the stream.",))
        self.assertEqual(parser.last_text, (answer + "Done.")[-4000:])
        self.assertEqual(parser.progress()["last_text"], (answer + "Done.")[-600:])
        self.assertEqual(parser.finish(0, "").text, answer + "Done.")

    def test_a_run_that_did_not_succeed_is_an_error(self):
        events = v3_events("sess_c", "half")
        events[-1]["data"].update(status="error", stopReason="error")
        result = self.stream_parser(*events).finish(0, "[INFO] kiro_agent: shutting down\n")
        self.assertEqual((result.outcome, result.session_id), (base.ERROR, "sess_c"))
        self.assertIn("status error", result.message)
        self.assertNotIn("[INFO]", result.message + result.error_text)

    def test_a_stream_without_run_finished_fails_on_the_exit_code_and_hides_log_lines(self):
        parser = self.stream_parser(*v3_events("sess_d", "partial")[:-1])
        stderr = "2026-11-04T10:00:00Z [INFO] kiro_agent::engine: starting\n[WARN] slow\nerror: the connection was reset\n"
        result = parser.finish(1, stderr)
        self.assertEqual((result.outcome, result.session_id), (base.ERROR, "sess_d"))
        self.assertEqual(result.message, "kiro-cli exited with status 1: error: the connection was reset")
        self.assertNotIn("[INFO]", result.error_text)
        self.assertNotIn("[WARN]", result.error_text)
        quiet = self.stream_parser(*v3_events("sess_d", "partial")[:-1]).finish(1, "[INFO] only noise\n")
        self.assertEqual(quiet.message, "kiro-cli exited with status 1: the run ended before Kiro reported a result")

    def test_usage_limits_and_auth_errors_in_a_stream(self):
        events = v3_events("sess_e", "")
        events[-1]["data"].update(status="error", finalText="", message="You have reached the usage limit for this month.")
        result = self.stream_parser(*events).finish(0, "[INFO] quota service: ok\n")
        self.assertEqual((result.outcome, result.message), (base.USAGE_LIMIT, "You have reached the usage limit for this month."))
        limit = self.stream_parser(*v3_events("sess_e", "x")[:-1]).finish(
            1, "[INFO] engine up\nerror: monthly usage limit reached\n")
        self.assertEqual((limit.outcome, limit.message), (base.USAGE_LIMIT, "error: monthly usage limit reached"))
        auth = self.stream_parser(*v3_events("sess_e", "x")[:-1]).finish(1, "[INFO] engine up\nerror: Not logged in\n")
        self.assertEqual((auth.outcome, auth.message), (base.AUTH, "error: Not logged in"))

    def test_other_session_updates_and_stray_lines_are_ignored(self):
        events = v3_events("sess_f", "ok")
        for kind in ("session_info_update", "config_option_update", "available_commands_update", "something_new"):
            events.insert(1, {"type": "sessionUpdate", "data": {"sessionId": "sess_f", "update": {
                "sessionUpdate": kind, "_meta": {"kiro": {"kind": "context_usage"}}}}})
        events.insert(1, {"type": "unknownEvent", "data": {}})
        result = self.stream_parser(*events, lines=()).finish(0, "")
        self.assertEqual((result.outcome, result.text, result.session_id), (base.SUCCESS, "ok", "sess_f"))

    def test_plain_text_lines_that_look_like_json_stay_text(self):
        parser = self.runtime.new_parser()
        lines = ("42", "[1, 2]", '"quoted"', "null", '{"a": 1}', '{"type": "note"}', '{"type": "runFinished"}', "Count: 3")
        for line in lines:
            runner_line(parser, line)
        result = parser.finish(0, "")
        self.assertEqual((result.outcome, result.text, result.session_id), (base.SUCCESS, "\n".join(lines), None))

    def test_unrecognised_json_objects_are_answers_until_a_kiro_stream_starts(self):
        for kind in ("record", "text", "unknownEvent"):
            with self.subTest(kind=kind):
                line = json.dumps({"type": kind, "data": {"value": 7}})
                parser = self.runtime.new_parser()
                runner_line(parser, line)
                result = parser.finish(0, "")
                self.assertEqual((result.outcome, result.text, result.session_id), (base.SUCCESS, line, None))
                self.assertFalse(parser.stream)
                for started in kiro.STREAM_EVENTS:
                    stream = self.runtime.new_parser()
                    runner_line(stream, json.dumps({"type": started, "data": {}}))
                    runner_line(stream, line)
                    self.assertTrue(stream.stream)
                    self.assertEqual(stream.answer(), "")

    def test_unrecognised_json_answer_keeps_numeric_precision_verbatim(self):
        line = '{"type":"x","data":{"v":0.1000000000000000055511151231257827,"big":1e400}}'
        parser = self.runtime.new_parser()
        runner_line(parser, line)
        result = parser.finish(0, "")
        self.assertEqual((result.outcome, result.text.encode("utf-8"), result.session_id),
                         (base.SUCCESS, line.encode("utf-8"), None))

    def test_the_runner_still_wraps_every_line_of_a_plain_text_parser(self):
        class Plain:
            plain_text = True

            def __init__(self):
                self.events = []

            def feed(self, event):
                self.events.append(event)

        parser = Plain()
        runner_line(parser, '{"type": "runStarted", "data": {}}')
        self.assertEqual(parser.events, [{"type": "text", "text": '{"type": "runStarted", "data": {}}'}])

    def test_login_status_is_read_only(self):
        self.assertTrue(self.runtime.auth_status(self.config, self.account)["loggedIn"])
        with patch.dict(os.environ, {"FAKE_KIRO_LOGGED_OUT": "1"}):
            self.assertFalse(self.runtime.auth_status(self.config, self.account)["loggedIn"])
        self.assertEqual(self.runtime.version(self.config), "kiro-cli 2.30.0")

    def test_one_account_only(self):
        from agents_router import setup

        self.assertEqual(self.config.primary, Path.home() / ".kiro")
        with self.assertRaises(ValueError) as caught:
            setup.register_account(config.load_config(), "kiro", "work", self.home / "work")
        self.assertIn("one login", str(caught.exception))


class KiroJobTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ["AGT_KIRO_COMMAND"] = FAKE_KIRO

    def tearDown(self):
        for key in ("AGT_KIRO_COMMAND", "FAKE_KIRO_SCENARIO", "FAKE_KIRO_PRESET", "FAKE_KIRO_DELAY"):
            os.environ.pop(key, None)
        super().tearDown()

    def test_a_job_runs_and_answers(self):
        status = jobs.wait_for(jobs.create_job(runtime="kiro", prompt="count the files", cwd=str(self.project), access="read")["job_id"], 60)
        self.assertEqual((status["state"], status["outcome"]), ("done", "success"), status)
        self.assertEqual(jobs.read_result(status["job_id"])["result"], "Looked at it.\nDone: count the files")
        record = self.records()[-1]
        self.assertIn("--trust-tools=fs_read,glob,grep,web_search,web_fetch", record["argv"])
        self.assertIn("count the files", record["stdin"])

    def test_the_relay_path_follows_a_kiro_job(self):
        import contextlib
        import io

        from agents_router import cli

        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("sys.stdin", io.StringIO("")):
            code = cli.main(["run", "--follow", "--runtime", "kiro", "--access", "read", "--cwd", str(self.project),
                             "count the files"])
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0, lines)
        self.assertTrue(lines[0].startswith("[agt] Agents Router · Kiro · model default · access read"), lines)
        body = lines[lines.index("===== RESULT =====") + 1:lines.index("===== END RESULT =====")]
        self.assertEqual(body, ["| Looked at it.", "| Done: count the files"])
        self.assertRegex(lines[-1], r"^\[agt\] DONE · success · .* · job \S+ · token [0-9a-f]{32}$")

    def test_a_full_role_runs_kiro_with_every_tool_trusted(self):
        import contextlib
        import io

        from agents_router import cli

        (self.home / "settings.toml").write_text('[roles.general-worker]\naccess = "full"\nproviders = ["kiro"]\n',
                                                 encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("sys.stdin", io.StringIO("")):
            code = cli.main(["run", "--role", "general-worker", "--cwd", str(self.project), "count the files"])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        (job,) = [path.name for path in (self.home / "jobs").iterdir()]
        self.assertEqual(jobs.read_spec(job).access, "full")
        argv = self.records()[-1]["argv"]
        self.assertIn("--trust-all-tools", argv)
        self.assertFalse(any(arg.startswith("--trust-tools=") for arg in argv), argv)

    def test_the_engine_flag_reaches_the_job(self):
        os.environ["FAKE_KIRO_PRESET"] = "kiro-3.0.0"
        try:
            status = jobs.wait_for(jobs.create_job(runtime="kiro", prompt="t", cwd=str(self.project), access="edit")["job_id"], 60)
        finally:
            os.environ.pop("FAKE_KIRO_PRESET")
        self.assertEqual(status["outcome"], "success", status)
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[:8], ["chat", "--no-interactive", "--wrap", "never", "--v3", "--output-format", "stream-json", EDIT])

    def test_a_forced_v3_job_on_an_old_kiro_cli_fails_with_the_reason(self):
        (self.home / "settings.toml").write_text('[kiro]\nengine = "v3"\n', encoding="utf-8")
        status = jobs.wait_for(jobs.create_job(runtime="kiro", prompt="t", cwd=str(self.project), access="read")["job_id"], 60)
        self.assertEqual(status["outcome"], "error", status)
        self.assertIn('kiro-cli 2.30.0 has no V3 engine; update Kiro or set [kiro] engine = "auto"', status["message"])
        self.assertEqual(self.records(), [])  # no chat run started

    def test_the_engine_setting_is_checked(self):
        settings = self.home / "settings.toml"
        for text in ('[kiro]\nengine = "v4"\n', '[kiro]\nengine = 3\n', '[codex]\nengine = "v3"\n'):
            settings.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError, msg=text) as caught:
                config.load_config()
            self.assertIn("engine must be under [kiro]", str(caught.exception))
        settings.write_text('[kiro]\nengine = "v2"\n', encoding="utf-8")
        self.assertEqual(config.load_config().runtime("kiro").engine, "v2")
        settings.unlink()
        self.assertEqual(config.load_config().runtime("kiro").engine, "auto")

    def run_job(self, prompt="count the files", **kwargs):
        return jobs.wait_for(jobs.create_job(runtime="kiro", prompt=prompt, cwd=str(self.project), access="read",
                                             **kwargs)["job_id"], 60)

    def test_a_stream_json_job_reports_answer_session_and_tools(self):
        os.environ["FAKE_KIRO_PRESET"] = "kiro-3.0.0"
        status = self.run_job()
        self.assertEqual((status["state"], status["outcome"]), ("done", "success"), status)
        self.assertEqual(jobs.read_result(status["job_id"])["result"], "Done: count the files")
        self.assertRegex(status["session_id"], r"^sess_[0-9a-f-]+$")
        argv = self.records()[-1]["argv"]
        self.assertIn("--v3", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertFalse(any("[INFO]" in warning for warning in status.get("warnings") or []), status)

    def test_a_v2_stream_job_has_a_plain_uuid_session(self):
        os.environ["FAKE_KIRO_PRESET"] = "kiro-2.37.1"
        status = self.run_job()
        self.assertEqual(status["outcome"], "success", status)
        self.assertRegex(status["session_id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        self.assertEqual(jobs.read_result(status["job_id"])["result"], "Done: count the files")

    def test_stream_failures_end_as_errors_without_log_lines(self):
        os.environ["FAKE_KIRO_PRESET"] = "kiro-3.0.0"
        for scenario, expected in (("no-finish", "kiro-cli exited with status 1: error: the connection to Kiro was reset"),
                                   ("run-error", "Kiro's run ended with status error (error)")):
            os.environ["FAKE_KIRO_SCENARIO"] = scenario
            status = self.run_job()
            self.assertEqual((status["outcome"], status["message"]), ("error", expected), status)
            self.assertNotIn("[INFO]", json.dumps(status))
        os.environ["FAKE_KIRO_SCENARIO"] = "truncated"
        status = self.run_job()
        self.assertEqual(jobs.read_result(status["job_id"])["result"], "Done: count the files")

    def test_a_message_to_a_finished_v3_job_resumes_its_session_on_v3(self):
        from agents_router import tools

        os.environ["FAKE_KIRO_PRESET"] = "kiro-3.0.0"
        first = self.run_job()
        session = first["session_id"]
        reply = tools.call("subagent_message", {"job_id": first["job_id"], "text": "one more thing"})
        status = jobs.wait_for(reply["new_job"]["job_id"], 60)
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual(status["session_id"], session)  # the fake echoes the resumed session back
        record = self.records()[-1]
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--resume-id") + 1], session)
        self.assertIn("--v3", argv)
        self.assertIn("one more thing", record["stdin"])

    def test_a_plain_uuid_session_resumes_on_v2_whatever_the_setting(self):
        from agents_router import tools

        os.environ["FAKE_KIRO_PRESET"] = "kiro-2.37.1"
        first = self.run_job()
        (self.home / "settings.toml").write_text('[kiro]\nengine = "v3"\n', encoding="utf-8")
        reply = tools.call("subagent_message", {"job_id": first["job_id"], "text": "one more thing"})
        status = jobs.wait_for(reply["new_job"]["job_id"], 60)
        self.assertEqual(status["outcome"], "success", status)  # the fake refuses a V2 session on --v3
        argv = self.records()[-1]["argv"]
        self.assertEqual(argv[argv.index("--resume-id") + 1], first["session_id"])
        self.assertIn("--v2", argv)
        self.assertNotIn("--v3", argv)

    def test_a_message_sent_while_a_kiro_job_runs_becomes_a_follow_up_turn(self):
        from agents_router import tools

        os.environ.update(FAKE_KIRO_PRESET="kiro-3.0.0", FAKE_KIRO_DELAY="4")
        job = jobs.create_job(runtime="kiro", prompt="first task", cwd=str(self.project), access="read")["job_id"]
        deadline = time.time() + 30
        while jobs.read_status(job).get("state") != "running" and time.time() < deadline:
            time.sleep(0.2)
        reply = tools.call("subagent_message", {"job_id": job, "text": "also update the docs"})
        self.assertEqual(reply["delivered"], "queued for the running subagent")
        status = jobs.wait_for(job, 60)
        self.assertEqual((status["outcome"], status.get("follow_up_turns")), ("success", 1), status)
        first, last = self.records()[-2:]
        self.assertNotIn("also update the docs", first["stdin"])  # Kiro has no agt-parent tools: it can't see it mid-run
        self.assertIn("also update the docs", last["stdin"])
        self.assertEqual(last["argv"][last["argv"].index("--resume-id") + 1], status["session_id"])
        self.assertIn("--v3", last["argv"])
        self.assertEqual(jobs.read_result(job)["result"], "Done: also update the docs")  # the follow-up turn's answer
        self.assertFalse([note for note in status.get("warnings") or [] if "delivered" in note], status)

    def test_a_message_a_plain_text_job_never_got_is_reported_undelivered(self):
        from agents_router import tools

        os.environ["FAKE_KIRO_DELAY"] = "4"  # the default fake: plain text, no session id
        job = jobs.create_job(runtime="kiro", prompt="first task", cwd=str(self.project), access="read")["job_id"]
        deadline = time.time() + 30
        while jobs.read_status(job).get("state") != "running" and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(tools.call("subagent_message", {"job_id": job, "text": "also update the docs"})["delivered"],
                         "queued for the running subagent")
        status = jobs.wait_for(job, 60)
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual(len(self.records()), 1)  # no follow-up turn
        note = "1 message wasn't delivered: this job can't take follow-ups"
        self.assertIn(note, tools.call("subagent_status", {"job_id": job})["warnings"])  # agt status
        self.assertIn(note, tools.call("subagent_result", {"job_id": job})["warnings"])  # agt result
        self.assertEqual(messages.undelivered_note(2), "2 messages weren't delivered: this job can't take follow-ups")

    def test_a_plain_text_job_has_no_session_to_continue(self):
        status = self.run_job()  # the default fake: no --output-format, plain text
        self.assertEqual((status["outcome"], status.get("session_id")), ("success", None), status)
        with self.assertRaises(ValueError) as caught:
            jobs.create_job(runtime="kiro", prompt="more", cwd=str(self.project), access="read", resume_job=status["job_id"])
        self.assertIn("has no session to continue", str(caught.exception))

    def test_a_plain_text_json_object_answer_is_preserved(self):
        os.environ["FAKE_KIRO_SCENARIO"] = "json-answer"
        status = self.run_job()
        self.assertEqual((status["outcome"], status.get("session_id")), ("success", None), status)
        self.assertEqual(jobs.read_result(status["job_id"])["result"],
                         json.dumps({"type": "record", "data": {"value": 7}}))

    def test_a_usage_limit_is_reported_as_one(self):
        os.environ["FAKE_KIRO_SCENARIO"] = "limit"
        status = jobs.wait_for(jobs.create_job(runtime="kiro", prompt="t", cwd=str(self.project), access="read")["job_id"], 60)
        self.assertEqual(status["outcome"], "usage_limit", status)


if __name__ == "__main__":
    unittest.main()
