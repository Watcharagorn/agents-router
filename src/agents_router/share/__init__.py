"""`agt share`: keep agents and skills the same across Claude Code, Codex, Antigravity and Kiro.

The modules here only read and write the CLIs' own files. `frontmatter`,
`codex_toml` and `kiro_json` are the format adapters; `model` turns each
provider's agent file into the same core fields; `normalize`, `names`,
`toolmap` and `exclude` hold the rules every sync applies.
"""
