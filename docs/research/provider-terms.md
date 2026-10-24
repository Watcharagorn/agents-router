# Provider terms for native delegation and multiple accounts

Research date: 2026-10-14. This is an engineering reading of published rules, not legal advice and not permission from any vendor. Terms change, so recheck the linked pages before relying on a row. The detailed evidence is in [claude-multi-account.md](claude-multi-account.md), [codex-multi-account.md](codex-multi-account.md) and [cli-permissions-and-headless.md](cli-permissions-and-headless.md).

## Two separate questions

1. Native delegation: may an agent start the vendor's own CLI on your machine, signed in with your own account, and hand it a task? This is what Agents Router does by default.
2. Multiple accounts and rotation: may one person keep several subscriptions of the same vendor, share one setup across them, and have a tool move work to the next account when one hits its usage limit?

Having a headless flag answers neither question. Running the official binary keeps authentication where the vendor put it, which is materially different from a proxy that pulls tokens out and calls the API itself. But vendors can still restrict how an account is used.

## Summary

| Vendor | Native delegation | Several accounts, shared setup | Automatic rotation at a limit |
|---|---|---|---|
| Anthropic (Claude Code) | Allowed for the unmodified binary with your own subscription; `claude -p` with subscription limits confirmed on 2026-10-07 | Documented: one config dir per account | Not addressed in the terms. The explicit restrictions target third-party token use, not your own CLI logins; the terms do assume "ordinary, individual usage" |
| OpenAI (Codex) | Strong precedent: OpenAI publishes a Claude Code → Codex plugin; docs recommend API keys for programmatic use | Not forbidden in the excerpts found; credentials must not be shared with others | Terms excerpt mentions circumventing "any rate limits or restrictions"; OpenAI's own plugin delegates to the CLI |
| Google (Antigravity CLI) | Not supported on consumer plans when wrapped by another coding agent; use an enterprise or AI Studio key | n/a on consumer plans | Explicitly not supported on consumer plans |
| Google (Gemini CLI) | Enterprise and API-key routes only; consumer access ended 2026-06-18 | n/a for consumers | n/a |
| Cursor | Headless CLI documented; API keys recommended for automation; no wrapper ban found | Not researched in depth | Not researched |

## Anthropic

From [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance):

- "OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans and is designed to support ordinary use of Claude Code and other native Anthropic applications."
- "developers may not collect, store, or intermediate Claude.ai credentials or session tokens"
- "Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription"
- "Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK."

From [authentication](https://code.claude.com/docs/en/authentication): "To stay signed in to multiple accounts at once, such as work and personal accounts, give each account its own configuration directory."

From the Help Center article [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540), update of 2026-10-07: "You can still use the Claude Agent SDK, claude -p, and third-party apps with your subscription limits." (read through a summarizing fetch; check the live page).

From the [Consumer Terms](https://www.anthropic.com/legal/consumer-terms): automated or non-human access is not allowed "Except when you are accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it", and users must not interfere with the services, including by "bypassing any of our systems or protective measures."

From the [Usage Policy](https://www.anthropic.com/legal/aup) (the fetched copy is marked effective 2026-11-12): do not "create multiple accounts, to avoid detection" or "Circumvent a ban through the use of a different account".

Reading: per-account config dirs with the native `/login`, where the tool only chooses the folder and never touches a token, is the lowest-risk design. Storing `setup-token` tokens in the tool is closer to "collect, store, or intermediate". Rotating to keep working past a limit isn't forbidden in so many words, but whether a usage limit counts as a "protective measure" is undefined. One unverified public report describes thirteen Max accounts banned without a stated reason; that scale is far above a personal setup, but it shows enforcement can arrive without explanation.

## OpenAI

- Codex supports [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) and an [app-server](https://learn.chatgpt.com/docs/app-server), and OpenAI ships [codex-plugin-cc](https://github.com/openai/codex-plugin-cc), which runs the local Codex from Claude Code. That is the clearest vendor precedent for native delegation.
- [Codex auth docs](https://learn.chatgpt.com/docs/auth): "Use API key authentication for programmatic Codex CLI workflows, such as CI/CD jobs." They also say to treat `auth.json` "like a password".
- Terms of Use, seen only as a search excerpt because the page returned a bot challenge: users may not "circumvent any rate limits or restrictions or bypass any protective measures". An older version also says "You may not share your account credentials or make your account available to anyone else".
- OpenAI also publishes an approved [token-sharing path for Codex app-server integrations](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server), so "third-party login is always forbidden" is too broad. Each claim has to be tied to a specific interface and plan.

Reading: native delegation to Codex is well supported. Several accounts with one shared setup isn't forbidden in what was found. Automatic rotation at a limit matches the circumvention wording closely and should not be on by default.

## Google

[Antigravity FAQ](https://antigravity.google/docs/faq#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching): "Wrapping `agy` inside third-party coding agents or applications, or automating account rotation, is not supported under consumer plans." Headless mode is described as intended for direct scripting and CI, and the FAQ points wrapper use to a Gemini Enterprise or AI Studio API key. See also the [terms](https://www.antigravity.google/terms).

Gemini CLI: Google [ended consumer and free Code Assist access on 2026-06-18](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/). Enterprise and paid API-key routes continue.

Reading: the Antigravity adapter in this repository is only appropriate with an account route Google allows for wrapping (enterprise or API key). Account rotation is out for AGY consumer plans, and Agents Router will refuse to enable it there.

## Cursor

The [headless CLI](https://cursor.com/docs/cli/headless) and [authentication](https://cursor.com/docs/cli/reference/authentication) docs cover scripting and recommend a Cursor API key for automated environments. No restriction comparable to Google's was found in the [terms](https://cursor.com/terms-of-service). This is a bounded finding, not a clearance.

## What this means for Agents Router

- The tool never reads, stores or forwards login tokens. Each account signs in through the vendor's own CLI, and every model call is that CLI's own. This is the line the published restrictions draw: they target third-party tools that hold subscription tokens or route requests through them, which this project doesn't do.
- Sharing one setup across your own accounts and moving work between them is the core feature. For Claude Code and Codex it is on by default (`failover = "on-limit"`); set it to `off` per runtime if you prefer to stop at a limit.
- Antigravity is the exception: Google's FAQ explicitly rules out automating account rotation on consumer plans, so that runtime uses one account and never fails over.
- Workers start in the vendor's safer permission mode; bypass flags are never a default.
