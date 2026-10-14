# Claude multi-account delegation: existing tools, terms, risk

Research date: 2026-10-14. Not legal advice. Quotes are copied from the pages as fetched on that date; where a page was read through a summarizing fetch tool this is stated.

## Summary

Several open-source tools already manage multiple Claude Code accounts. Most either swap the credential files of one config dir (claude-swap, caam) or give each account its own `CLAUDE_CONFIG_DIR` (clauth); a separate group are local proxies that pool OAuth tokens (ccflare, TeamClaude, the proxy half of ccs). clauth is the closest match to this project: it launches `claude` under per-profile config dirs, mirrors the user's `~/.claude` into them with symlinks, syncs the shared keys of `.claude.json` (including `mcpServers`) while keeping `oauthAccount` per profile, detects limits from usage numbers, and exposes a `delegate` MCP tool that runs a headless prompt on another account. Anthropic's current text says Claude Code's OAuth login is "designed to support ordinary use", forbids developers from collecting, storing or intermediating Claude.ai credentials or session tokens, and documents `CLAUDE_CONFIG_DIR` as the way to stay signed in to several accounts. Nothing I found says that one person holding several subscriptions is forbidden, and nothing says rotating between them to get past a limit is allowed. The 2026-10-07 note on the Help Center says `claude -p` still works with subscription limits. The design where each account logs in natively and the tool only picks a config dir (design c) is the lowest risk; token storage (b) and anything built to extend limits (d) raise it.

## Existing tools

Stars, license and last push come from the GitHub API on 2026-10-14. I read source for clauth, and the documentation of caam, claude-swap and teamclaude; other rows are README-level or metadata only and are marked.

| Tool | Stars | License | Last push | Windows | Category and account separation | Sharing MCP, settings, skills | Limit handling |
|---|---|---|---|---|---|---|---|
| [uwuclxdy/clauth](https://github.com/uwuclxdy/clauth) | 242 | MIT | 2026-10-14 | yes (Git Bash/MSYS2; real symlinks need developer mode or admin, otherwise copy mode) | Stores a snapshot of `.credentials.json` per profile under `~/.clauth/`. `clauth start` runs `claude` with a per-session `CLAUDE_CONFIG_DIR` tree. Can also swap the global login | Per-session tree is a forest of symlinks into `~/.claude/`; `.claude.json` is synced across profiles except an explicit per-profile key list; `--isolated` drops all of it | Yes. Polls 5h/7d usage, fallback chain with thresholds, daemon mode. MCP plugin with `profiles`, `switch_profile`, `delegate`, `monitor` |
| [realiti4/claude-swap](https://github.com/realiti4/claude-swap) | 3434 | MIT | 2026-10-14 | yes | Backs up and restores OAuth tokens (token swap on the default login). A parallel mode uses separate profiles | Parallel profiles use the normal `~/.claude`; user-scope MCP servers are copied from the default profile on each launch, MCP OAuth logins are not | Yes. `cswap auto` switches at a usage threshold, strategies `best` and `next-available` |
| [Dicklesworthstone/coding_agent_account_manager](https://github.com/Dicklesworthstone/coding_agent_account_manager) (caam) | 209 | NOASSERTION | 2026-10-13 | yes (release zip, Scoop) | Vault of auth files; `activate` swaps them; `shallow-spawn` gives each identity its own `HOME` with only `.credentials.json` and `.claude.json` real | Everything else symlinked to the real home; `skills`, `plugins`, `commands`, `agents` linked; `mcpServers` and preferences copied from the real `.claude.json` on every spawn | Yes. `caam run` puts the profile in cooldown on a rate limit, picks the next and re-runs the command |
| [kaitranntt/ccs](https://github.com/kaitranntt/ccs) | 2889 | MIT | 2026-10-13 | not stated in README | Mostly a provider router built on a local OAuth proxy (CLIProxyAPI), plus "isolated account contexts". I did not read the isolation code | Not documented in README | README mentions round-robin and fill-first inside the proxy, and quota display; failover by `claude` itself not described |
| [KarpelesLab/teamclaude](https://github.com/KarpelesLab/teamclaude) | 405 | MIT | 2026-10-12 | not checked | Local proxy that holds OAuth tokens, injects them and rewrites `account_uuid` in request bodies | Not applicable (one client config) | Yes, rotates on quota inside the proxy |
| [snipeship/ccflare](https://github.com/snipeship/ccflare), [tombii/better-ccflare](https://github.com/tombii/better-ccflare) | 1051 / 272 | MIT | 2026-04-19 / 2026-10-09 | not checked | Load-balancing proxy in front of the API with many accounts behind one endpoint. Different category, listed for contrast | Not applicable | Yes, in the proxy |
| [ftery0/claude-account-switch](https://github.com/ftery0/claude-account-switch) | 4 | MIT | 2026-10-02 | not checked | One `CLAUDE_CONFIG_DIR` per profile, shell integration | Not checked | No |
| [hoangvu12/claude-switch](https://github.com/hoangvu12/claude-switch) | 7 | none | 2026-07-12 | not checked | `claude` shim, profile per config dir; its README says on macOS OAuth lives in the Keychain, so only API-key profiles are isolated there | Not checked | No |
| [Nemo-Illusionist/claude-code-account-switcher](https://github.com/Nemo-Illusionist/claude-code-account-switcher) | 29 | MIT | 2026-10-05 | yes (per repo description) | Binds accounts to directories | Not checked | No |
| [ming86/cc-account-switcher](https://github.com/ming86/cc-account-switcher) | 450 | MIT | 2025-07-01 | not checked | Credential-swap scripts. Stale | Not checked | No |

Leads I could not confirm: "ccm" (no matching repository found), "cux" (only a Peerlist listing, no repository found), "ccsw" (described in a search summary; no repository found by name). Do not rely on them.

Notes

- clauth, `src/claude_json.rs`: the code comment explains why one shared `.claude.json` fails. Claude Code trusts the cached `oauthAccount` and does not re-derive it from the loaded token, so a shared file leaks one account's identity into another. clauth therefore keeps a separate `.claude.json` per profile and syncs everything except a list of per-profile keys (`oauthAccount`, usage and entitlement caches, `primaryApiKey`, model-access caches and others). Sync is "latest write wins" at file granularity with atomic writes.
- clauth, `src/runtime.rs`: each `clauth start` session gets its own runtime tree; `.credentials.json` there is a symlink to the profile's canonical credentials so concurrent sessions share one refresh-token chain. When symlinks are denied (Windows without developer mode) it falls back to recursive copy plus a watchdog that mirrors changes, with the stated cost that shared sessions then cannot have independent credentials.
- clauth, claude-swap, caam and teamclaude all store copies of OAuth credentials themselves. By the text quoted below ("collect, store, or intermediate"), that is the part of their designs closest to the rule. A design that only chooses a config dir avoids it.
- caam documents a case where syncing a vault across machines replayed a stale refresh token and revoked the whole token family ("bricked a real account", its issue 19). Token copying has a failure mode beyond terms.
- caam's FAQ claims its use is not against the terms. TeamClaude's `docs/compliance.md` says "Anthropic hasn't explicitly blessed *automated* pooling" and gives the maintainer's good-faith reading. Both are maintainers' opinions, not Anthropic statements.
- ccflare and teamclaude send the subscription token from a process that is not `claude`. That is the category this project avoids. The better-ccflare README says Anthropic bans Claude OAuth accounts used outside the Claude CLI when explaining why it excludes them from its Codex CLI path.

## How config sharing works in practice

What lives where (Claude Code docs and the tools above):

- The login is in `<config dir>/.credentials.json` on Linux and Windows. On macOS it is in the Keychain, with the entry keyed to the config dir, so a different `CLAUDE_CONFIG_DIR` reads a different entry ([authentication docs](https://code.claude.com/docs/en/authentication), "Credential management").
- `.claude.json` holds user-scope `mcpServers`, per-project state, tips and the cached `oauthAccount`.
- `settings.json`, `CLAUDE.md`, `skills/`, `agents/`, `commands/`, `plugins/` and `projects/` (session history) are per config dir.

Techniques seen in the tools:

1. Symlinks from the new dir into the main one for `skills`, `agents`, `commands`, `plugins`, `CLAUDE.md`. Works on Unix. On Windows symlinks need developer mode or admin; clauth detects failure and falls back to copying. None of the tools I read uses junctions.
2. Copy only the `mcpServers` key (and a whitelist of preference keys) from the main `.claude.json` into each profile's `.claude.json` before launch (caam, claude-swap). Account-bound keys must not be copied. MCP servers that use OAuth need a login per profile; claude-swap says so.
3. Leave the profile's `.claude.json` alone and pass `--mcp-config <file>` on every launch. This is a Claude Code flag; none of the tools I read use it for this. It keeps one source of truth and avoids writing a copied `mcpServers` into each profile. I did not test it. Plugins and skills still need method 1.
4. Share `projects/` so history is shared (caam, clauth). A Claude Code issue reports a regression when `projects/` is a symlink ([#98044](https://github.com/anthropics/claude-code/issues/98044)), so this is fragile.

Known Claude Code problems that matter here (github.com/anthropics/claude-code, status as seen 2026-10-14):

- `.claude.json` corruption under concurrent processes: [#29217](https://github.com/anthropics/claude-code/issues/29217), [#28973](https://github.com/anthropics/claude-code/issues/28973), [#29051](https://github.com/anthropics/claude-code/issues/29051), [#64600](https://github.com/anthropics/claude-code/issues/64600), [#28869](https://github.com/anthropics/claude-code/issues/28869), [#73590](https://github.com/anthropics/claude-code/issues/73590) (Windows, silently wipes user-scope `mcpServers`), [#28922](https://github.com/anthropics/claude-code/issues/28922) ("reported 8 times since June 2025"). All are closed; I did not verify the fixes. A separate config dir per account avoids a shared file between accounts, but several workers on one account still share one.
- Credentials under concurrent instances: [#76561](https://github.com/anthropics/claude-code/issues/76561) (open) says credential writes replace symlinks, so instances sharing `.credentials.json` can log each other out. This argues against symlinking `.credentials.json` between concurrent processes. Related: [#86616](https://github.com/anthropics/claude-code/issues/86616) (macOS keychain corruption with about 20 concurrent instances) and [#99314](https://github.com/anthropics/claude-code/issues/99314) (refresh-token race).
- `CLAUDE_CONFIG_DIR` leaks: [#80580](https://github.com/anthropics/claude-code/issues/80580) (user-level `CLAUDE.md` still read from `$HOME/.claude`), [#97680](https://github.com/anthropics/claude-code/issues/97680) and [#98789](https://github.com/anthropics/claude-code/issues/98789) (some paths still write to `~/.claude`), [#100762](https://github.com/anthropics/claude-code/issues/100762), [#92252](https://github.com/anthropics/claude-code/issues/92252) (setting it to the default path selects a different Keychain entry), [#100995](https://github.com/anthropics/claude-code/issues/100995) (`claude -p` says "Login expired" in a fresh dir while `claude auth status` reports logged in), [#101027](https://github.com/anthropics/claude-code/issues/101027) (Windows).
- Feature requests for built-in multi-account: [#30031](https://github.com/anthropics/claude-code/issues/30031), [#89785](https://github.com/anthropics/claude-code/issues/89785), [#22872](https://github.com/anthropics/claude-code/issues/22872).
- `setup-token` problems: [#84903](https://github.com/anthropics/claude-code/issues/84903) (401 on every call), [#8938](https://github.com/anthropics/claude-code/issues/8938), [#79597](https://github.com/anthropics/claude-code/issues/79597) (a model gated in the interactive picker for setup-token accounts, `-p` works).
- I found no issue specifically about sharing `mcpServers` between config dirs.

## Terms

All quotes are verbatim and short, from the page named. Where a fetch tool summarized the page, the quoted fragments came back inside quotation marks from the page text; check the live pages before citing them elsewhere.

### Claude Code legal and compliance

[code.claude.com/docs/en/legal-and-compliance](https://code.claude.com/docs/en/legal-and-compliance), section "Usage policy":

- "Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK."
- "OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Team, and Enterprise subscription plans and is designed to support ordinary use of Claude Code and other native Anthropic applications."
- "Anthropic does not permit third-party developers to offer Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan credentials on behalf of their users."
- "Moreover, developers may not collect, store, or intermediate Claude.ai credentials or session tokens — sign-in to a Claude account must complete through Anthropic's own flow."
- "Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription, including where a platform hosts Claude Code as described under *Can customers offer Claude Code in their products?* above."
- "Anthropic reserves the right to take measures to enforce these restrictions and may do so without prior notice."

Same page, "Can customers offer Claude Code in their products?": "The Claude Code binary must not be modified." and "Customers may not pay for, resell, or intermediate Claude usage on their end users' behalf."

### Claude Code authentication docs

[code.claude.com/docs/en/authentication](https://code.claude.com/docs/en/authentication):

- "To stay signed in to multiple accounts at once, such as work and personal accounts, give each account its own configuration directory."
- "Each directory has its own settings, session history, and claude.ai login or API key."
- "For CI pipelines, scripts, or other environments where interactive browser login isn't available, generate a one-year OAuth token with `claude setup-token`"
- "A long-lived OAuth token generated by `claude setup-token`. Use this for CI pipelines and scripts where browser login isn't available."
- "This token authenticates with your Claude subscription and requires a Pro, Max, Team, or Enterprise plan."
- "It can only make model requests, so it can't establish Remote Control sessions or fetch claude.ai connectors. MCP servers you configure locally still work."

### Consumer Terms

[anthropic.com/legal/consumer-terms](https://www.anthropic.com/legal/consumer-terms). The fetched version header read "Effective October 8, 2025".

- Section 3, prohibited use (item 7 as numbered by the fetch): "Except when you are accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it, to access the Services through automated or non-human means, whether through a bot, script, or otherwise."
- Section 2: "You may not share your Account login information, Anthropic API key, or Account credentials with anyone else"
- After the list in Section 3: you must not "abuse, harm, interfere with, or disrupt our Services," including "bypassing any of our systems or protective measures."
- The Terms as fetched do not state a limit on the number of accounts, and do not contain a prohibition worded around usage or rate limits. The fetch also reported that Claude Code is not named in the Terms body. I could not diff the full text, so read "not addressed" as "not found in the fetched text".

### Usage Policy

[anthropic.com/legal/aup](https://www.anthropic.com/legal/aup). The fetched version showed "Effective November 12, 2026", a date after the research date, so it may be an announced upcoming version. Check which version is in force.

- "Coordinate malicious activity across multiple accounts, or create multiple accounts, to avoid detection"
- "Circumvent a ban through the use of a different account"
- "Intentionally bypass or interfere with capabilities, restrictions, or guardrails established within our products"
- "Resell, proxy, or otherwise provide access to Claude through unauthorized means"
- Enforcement: Anthropic "may warn you or throttle, limit, suspend, or terminate your access".

### Help Center

- [Using Claude Code with your Pro or Max plan](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan): "These limits are shared across Claude and Claude Code, meaning all activity in both tools counts against the same limits." The article says nothing about `claude -p`, automation or multiple accounts.
- [Verifying your phone number](https://support.claude.com/en/articles/8287232): a search summary reports a maximum of three Claude accounts per phone number, with an older cached version worded more strictly. I did not read the page text myself, so this is unconfirmed. It is the only official place I found that implies a person may hold several accounts.
- I found no official Help Center article about holding multiple Pro or Max subscriptions. The Pro and Max usage article URLs I tried returned 404.

### The 2026-10-07 note

This is an update note at the top of the Help Center article [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540), not a separate announcement. Fragments as returned by the fetch tool:

- "Update October 7, 2026"
- "Claude Max and Team plans now include monthly API credits" (the note says these can be used for the Claude Agent SDK, `claude -p`, the Claude API and Claude Managed Agents)
- "You can still use the Claude Agent SDK, claude -p, and third-party apps with your subscription limits."

Context: the same article carries an earlier note, "Update June 15, 2026": "We've paused the previously-announced changes to Claude Agent SDK usage." The paused plan was a separate monthly credit for Agent SDK and `claude -p` that would have stopped those from counting against plan limits. So what changed on 2026-10-07 is that Max and Team plans gained API credits, claimed by linking a Console organization (per a search summary of [the credits article](https://support.claude.com/en/articles/17154008), which says the credit does not cover interactive Claude Code). What stayed is that `claude -p` and SDK use still draw from subscription limits. I read the exact sentence through a summarizing fetch and a search snippet agreed with it, but I did not see the raw page. The sentence says `claude -p` use under a subscription is accepted. It does not address multiple accounts, and it sits in a billing article, not on the legal page.

### Earlier enforcement context (Feb 2026)

[The Register, 2026-02-20](https://www.theregister.com/software/2026/02/20/anthropic-clarifies-ban-on-third-party-tool-access-to-claude/5014546) reports Anthropic revising its legal pages to say subscription OAuth tokens may not be used in other products, tools or services, and quotes an Anthropic engineer saying third-party harnesses "are prohibited by our Terms of Service". The current legal page text above is worded differently from that report, so the page has changed since; I saw the rewrite mentioned only in secondary sources.

## Enforcement reports (anecdotal)

None of these is verified and none comes from Anthropic. I found no report of a ban that names config-dir rotation of `claude -p` across a few accounts.

- Hacker News, 2026-07-14, ["Anthropic banned my thirteen 20x accounts, what now?"](https://news.ycombinator.com/item?id=48903047): the author says they ran 13 Max 20x subscriptions for their own business with 10 to 15 Claude Code terminals at once. The appeal was rejected without a reason and new accounts were banned quickly. No reason from Anthropic is given. Commenters speculate (abuse heuristics, ban evasion after the appeal). It is the closest report to multi-account use I found, and its scale is far above the use case here.
- Hacker News, 2026-06-19, ["None-US Claude users: beware if used Fable"](https://news.ycombinator.com/item?id=48597861): a suspension citing a supported-country policy, unrelated to multi-account or automation. Included only to show bans can arrive without a stated cause.
- January to February 2026: enforcement against third-party harnesses using subscription OAuth tokens, reported by The Register with an Anthropic engineer quoted.
- A user asked in [teamclaude issue 107](https://github.com/KarpelesLab/teamclaude/issues/107) (2026-07) whether automatic failover between several own subscriptions complies with the terms. The project's answer is in its `docs/compliance.md` and is the maintainer's reading only.
- A GitHub search of anthropics/claude-code issues for ban, suspension and disabled-account wording returned nothing relevant. I could not search Reddit with the tools available, so this section has no Reddit data.

## Risk assessment

Ratings are my inference from the quoted text and the reports above. "Risk" means risk of account action, and of breaching the terms as written.

| Design | Rating | Reasons |
|---|---|---|
| (a) Proxy pooling subscription OAuth tokens (ccflare, teamclaude style) | High | The process holding the token is not `claude`, so requests are not Claude Code's own. The legal page says developers "may not collect, store, or intermediate Claude.ai credentials or session tokens" and may not "route requests through Free, Pro, or Max plan credentials on behalf of their users". A single-user local proxy is arguably the owner's own use, but the text makes no such exception, and the February 2026 enforcement targeted token use outside the official client. Consumer Terms item 7 (automated means) and "bypassing any of our systems" add to it. Some proxies rewrite identity fields (teamclaude rewrites `account_uuid`), which can look like evasion. |
| (b) Tool stores `claude setup-token` tokens, launches `claude -p` with `CLAUDE_CODE_OAUTH_TOKEN` | Medium | For: the binary is unmodified, Anthropic documents this token for "scripts", and `claude -p` is confirmed to work with subscription limits. Against: the tool would "collect" and "store" subscription tokens, which the legal page forbids for "developers" in the context of products offering login to users. It is ambiguous whether a personal tool run by the account owner counts. The sentence about an end user signing in to the unmodified binary "with their own Claude subscription" supports the owner, but it is about signing in to the binary, not about a third tool holding the token. A stored one-year token is also a larger leak surface than a login. Known 401 problems ([#84903](https://github.com/anthropics/claude-code/issues/84903)). |
| (c) One `CLAUDE_CONFIG_DIR` per account, each logged in with `/login`, tool only picks the dir | Low | Anthropic's own docs describe this setup for several accounts. The tool never reads, copies or sends a token, so the "collect, store, or intermediate" clause is not engaged, and all traffic is the unmodified `claude`. Remaining exposure: scripted `claude -p` falls under the automated-access sentence in the Consumer Terms ("where we otherwise explicitly permit it"), and the 2026-10-07 Help Center sentence is the nearest thing to explicit permission; and "ordinary, individual usage" is undefined. Operational risk, not terms risk: the config-dir leaks and concurrent-write bugs listed above. |
| (d) Any of the above used to keep working after an account hits its limit | Raises each design by about one step: (a) stays High, (b) Medium to High, (c) Medium | No quoted sentence prohibits using a second legitimately paid subscription when the first is exhausted. Against that: the usage-limit text says limits "assume ordinary, individual usage", the Consumer Terms forbid "bypassing any of our systems or protective measures", and the Usage Policy lists creating multiple accounts "to avoid detection" and evading restrictions. Whether a usage limit is a "protective measure" or "restriction" is not defined and is the central ambiguity. Rotation also produces the pattern an automated anti-abuse system plausibly looks for (many accounts, high volume), which the thirteen-account report hints at, though that report is unverified and much larger in scale. A third-party maintainer says Claude Code's own limit flow offers signing in to a different account; I did not verify this and give it no weight. |

## Open questions

1. Does "collect, store, or intermediate" apply to an open-source tool that a user runs locally on their own tokens? The text addresses "developers" of products with "their users"; the single-user case is not addressed.
2. Is a usage limit a "protective measure" or "restriction" within the Consumer Terms and the Usage Policy? If yes, rotation to extend limits is risky whatever the mechanism.
3. Does holding several personal paid subscriptions count as "create multiple accounts, to avoid detection"? The Usage Policy sentence has a purpose clause; accounts created openly and each paid for may not match it, but the text does not say.
4. What does "ordinary, individual usage" allow for unattended `claude -p` fan-out? The 2026-10-07 note allows `claude -p` with subscription limits but gives no volume guidance.
5. Which Usage Policy version is in force on 2026-10-14 (the fetched copy says effective 2026-11-12)?
6. Is the Consumer Terms text I fetched current (header says October 8, 2025)? Press reports describe changes in 2026.
7. Technical: does `claude -p` with `--mcp-config` behave the same under each config dir, including for plugin-provided MCP servers? Not tested.
8. Technical: what exit code and message does `claude -p` give at a usage limit, and can it be detected without parsing text? Not researched in this pass.
9. Not done: reading ccs's isolation code, finding "ccm" and "cux" repositories, searching Reddit.

## Sources

Accessed 2026-10-14.

- https://code.claude.com/docs/en/legal-and-compliance
- https://code.claude.com/docs/en/authentication
- https://www.anthropic.com/legal/consumer-terms
- https://www.anthropic.com/legal/aup
- https://support.claude.com/en/articles/15036540 (Agent SDK with your Claude plan; update notes of 2026-06-15 and 2026-10-07)
- https://support.claude.com/en/articles/17154008 (monthly API credits; search summary only)
- https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan
- https://support.claude.com/en/articles/8287232 (phone number verification; search summary only)
- https://www.theregister.com/software/2026/02/20/anthropic-clarifies-ban-on-third-party-tool-access-to-claude/5014546
- https://github.com/uwuclxdy/clauth (README, src/claude_json.rs, src/runtime.rs)
- https://github.com/realiti4/claude-swap (README)
- https://github.com/Dicklesworthstone/coding_agent_account_manager (README)
- https://github.com/kaitranntt/ccs (README)
- https://github.com/KarpelesLab/teamclaude (README, docs/compliance.md, issue 107)
- https://github.com/snipeship/ccflare and https://github.com/tombii/better-ccflare (README, metadata)
- https://github.com/ftery0/claude-account-switch, https://github.com/hoangvu12/claude-switch, https://github.com/Nemo-Illusionist/claude-code-account-switcher, https://github.com/ming86/cc-account-switcher (metadata)
- Claude Code issues linked inline, under https://github.com/anthropics/claude-code/issues
- https://news.ycombinator.com/item?id=48903047 and https://news.ycombinator.com/item?id=48597861
