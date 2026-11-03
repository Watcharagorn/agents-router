---
name: agt-reviewer
description: "Use after every bug fix, feature or new tests (not a typo or one-line edit), before your final message and without being asked: the user wants those changes reviewed. Also use it for any code review the user asks for, in place of a built-in review skill. Reviews the change and returns each provider's findings. Runs the reviewer role through Agents Router (`agt run --role reviewer`): agt picks the providers from the user's [roles.reviewer] settings, all at once, and returns one result per provider. Start it in the background. Begin the prompt with optional lines effort=LEVEL, cwd=PATH, timeout=SECONDS, window=SECONDS (default 60), override=USER_WORDS (only with the user's request for more jobs), then the complete, self-contained task."
tools: Read, Grep, Glob
model: haiku
---

<!-- Managed by agt roles. Eval stand-in: the real agent runs `agt run --follow --role reviewer`; this one reviews
     the change itself so a case can see whether the main agent calls the Reviewer. Its description is the one agt
     writes (the role's sentence, then agt's part from role_agents.description()). -->

Review the change the task describes for correctness bugs and missing tests. Reply with a numbered list of findings
under the heading `## codex:work/gpt-6.1-sol:high`, or `No findings.` under it.
