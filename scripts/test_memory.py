"""
Smoke test for the memory layer.

Run:
    $env:PYTHONPATH = "src"
    uv run python scripts/test_memory.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent.memory.retrieval import remember, retrieve_context

MEMORIES = [
    ("The client Acme Corp requires weekly status reports every Friday at 5pm.", "clients"),
    ("Python async functions must be awaited — calling without await returns a coroutine object.", "engineering"),
    ("Q3 revenue target is $2.4M. Currently tracking at $1.9M as of June.", "finance"),
]

SEP = "-" * 56


def main() -> None:
    print(SEP)
    print("Step 1 — Saving 3 memories")
    print(SEP)

    saved = []
    for content, source in MEMORIES:
        m = remember(content=content, source=source)
        saved.append(m)
        print(f"  [{source}] {m.id[:8]} — saved")

    print()
    print(SEP)
    print("Step 2 — Semantic search: 'quarterly revenue'")
    print(SEP)

    results = retrieve_context("quarterly revenue", limit=3)
    for i, m in enumerate(results, 1):
        print(f"  {i}. [{m.source}] {m.content[:80]}")

    print()
    print(SEP)
    print("Step 3 — Vault markdown files created")
    print(SEP)

    vault = Path("data/vault")
    for md_file in sorted(vault.glob("*.md")):
        lines = md_file.read_text(encoding="utf-8").splitlines()
        print(f"  {md_file.name}  ({len(lines)} lines)")
        for line in lines[:6]:
            print(f"    {line}")
        print()


if __name__ == "__main__":
    main()
