# Release

1. **Check the branch** with `git status` and `git log --oneline main..HEAD`. The tree should be clean, and you should not be on `main`.
2. **Run the full suite:**

   ```bash
   PYTHONPATH=src:tests python3 -m unittest discover -s tests -t tests
   ```

   The only failure allowed is `test_help.test_other_commands_keep_argparse_help` on Python 3.15.
3. **Raise the version** in `.claude-plugin/plugin.json`, `pyproject.toml` and `src/agents_router/__init__.py` together:
   - a patch for fixes;
   - a minor version for new behavior.

   Then `python3 scripts/sync_guide.py` if the guide changed.
4. **Commit**, only when the person asks. The subject is `Agents Router X.Y.Z: <what changed, for the person>`, like the history (`git log --format=%s -10`). End the message with the attribution lines the session gives you.
5. **Push and open the pull request**, only when the person asks:

   ```bash
   git push -u origin HEAD
   gh pr create --title "Agents Router X.Y.Z: ..." --body "<summary, test results, live check>"
   ```

   End the body with the attribution footer the session gives you.
6. **After the merge:** run `agt update` (it reinstalls from the recorded source and updates the plugin in every profile). Then tell the person to type `/reload-plugins`, or start new sessions, in each profile.
