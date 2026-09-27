"""Every third-party module the code imports must be installable from the lock.

Most integration imports are lazy (inside functions), so a package that is
used but never declared in pyproject.toml passes every other test and only
crashes at runtime. boto3 and voyageai both shipped that way: they worked
only because they had once been pip-installed by hand, and `uv sync --locked`
silently removed them. This scans the source statically, so it catches an
undeclared import on a code path no test exercises.
"""
import ast
import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "agent"

# Imported inside try/except and reported as "not installed" by the setup
# wizard — deliberately not a hard dependency.
OPTIONAL = {
    "openai",   # setup.py: only tests an OpenAI key if the user picks that provider
    "evals",    # repo-root eval suite, importable only from a source checkout
}


def _third_party_imports() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top != "agent" and top not in sys.stdlib_module_names:
                    found.setdefault(top, str(path.relative_to(SRC)))
    return found


def test_every_imported_package_is_installed():
    missing = {
        mod: where for mod, where in _third_party_imports().items()
        if mod not in OPTIONAL and importlib.util.find_spec(mod) is None
    }
    assert not missing, (
        "Imported but not installed; add to pyproject.toml with `uv add`: "
        + ", ".join(f"{m} (first seen in {w})" for m, w in sorted(missing.items()))
    )
