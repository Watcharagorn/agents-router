# Release

1. **Check the branch** with `git status` and `git log --oneline main..HEAD`. The tree should be clean, and you should not be on `main`.
2. **Run the full suite:**

   ```bash
   PYTHONPATH=src:tests python3 -m unittest discover -s tests -t tests
   ```

   The only failure allowed is `test_help.test_other_commands_keep_argparse_help` on Python 3.15.
3. **Pass the skill gate** when the change touches the Roles section of `skills/agents-router/SKILL.md`, the role step of `src/agents_router/guides/setup.md`, or `skills/orchestrator-rebalance/`. The graded cases in `tests/skill-evals/` (tag `release-gate`) must pass before release.
   - First check that every case loads. This runs no model and costs nothing:

     ```bash
     claude plugin eval . --eval-dir tests/skill-evals --case no-such-case --trust-plugin --no-publish --ablation none
     ```

     A case that fails to load is printed with `✗` and the reason; "No eval cases found matching" with no `✗` line means all loaded.
   - Then the graded run. It uses real model runs on the person's quota (three runs per case, plus an LLM judge), so ask first and say so:

     ```bash
     PATH="$PWD/tests/skill-evals/bin:$PATH" claude plugin eval . --eval-dir tests/skill-evals --tag release-gate \
       --scaffold --allow-tools Bash,Write,Edit,Agent --ablation none --no-publish --output-dir "$TMPDIR/agt-skill-evals"
     ```

     `tests/skill-evals/bin` goes first on PATH so the runs' `agt` is the eval wrapper: each case's `place-files.sh` builds `.agt-eval/` in its own workspace (its own HOME, AGT_HOME, CLAUDE_CONFIG_DIR and CODEX_HOME, the fake CLIs, agt copied from this clone), and the wrapper refuses to run anywhere else. No run starts a real job or touches the real `~/.agt`.
   - Any case below 1.0 blocks the release; fix the skill, not the grader, unless the grader is wrong.
   - Advice only, never a gate: the with/without benchmark (drop `--ablation none`) and trigger tuning with skill-creator against `tests/skill-evals/<skill>/triggers.json`. Both use quota and vary from run to run, so they also stay out of the unit tests.
4. **Raise the version** in `.claude-plugin/plugin.json`, `pyproject.toml` and `src/agents_router/__init__.py` together:
   - a patch for fixes;
   - a minor version for new behavior.

   Then `python3 scripts/sync_guide.py` if the guide changed.
5. **Commit**, only when the person asks. The subject is `Agents Router X.Y.Z: <what changed, for the person>`, like the history (`git log --format=%s -10`). End the message with the attribution lines the session gives you.
6. **Push and open the pull request**, only when the person asks:

   ```bash
   git push -u origin HEAD
   gh pr create --title "Agents Router X.Y.Z: ..." --body "<summary, test results, live check>"
   ```

   End the body with the attribution footer the session gives you.
7. **After the merge:** run `agt update` (it reinstalls from the recorded source and updates the plugin in every profile). Then tell the person to type `/reload-plugins`, or start new sessions, in each profile.
