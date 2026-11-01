"""Copy src/agents_router/guides/setup.md into skills/agents-router-setup/SKILL.md, below its marker line.

The guide has one source; the skill carries a copy for agents that can't run `agt guide setup`.
tests/test_guide.py fails when the two differ. Run from the repository root: python3 scripts/sync_guide.py
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "src" / "agents_router" / "guides" / "setup.md"
SKILL = ROOT / "skills" / "agents-router-setup" / "SKILL.md"
MARKER = "<!-- The guide below is src/agents_router/guides/setup.md, copied by scripts/sync_guide.py. Edit that file. -->\n"


def main() -> int:
    text = SKILL.read_text(encoding="utf-8")
    head, found, _ = text.partition(MARKER)
    if not found:
        print(f"{SKILL}: the marker line is missing", file=sys.stderr)
        return 1
    updated = head + MARKER + "\n" + GUIDE.read_text(encoding="utf-8")
    if updated != text:
        SKILL.write_text(updated, encoding="utf-8")
        print(f"updated {SKILL.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
