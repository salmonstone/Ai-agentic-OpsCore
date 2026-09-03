"""
Terraform (HCL) reader — pure parsing. No analysis, no cluster, no AWS, no network.

Reads .tf files from a directory and extracts blocks (resource, data, provider,
variable, module, output, terraform/backend) into typed dicts. Works completely
offline — no `terraform` binary, no `terraform init`, no credentials.

The parser is HEURISTIC (brace-aware, not a full HCL2 evaluator): it does NOT
resolve variable interpolations, functions, or for-expressions. That is fine for
a static review scan — we pattern-match on resource types and literal attribute
values. Anything it can't simplify is kept as the raw expression string.

If a `terraform show -json` plan file is provided, parse_plan_json() reads the
real planned actions (create / update / delete / replace) so the skill can report
true blast radius instead of guessing from static config.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from agent.observability.logging import get_logger

log = get_logger(__name__)

# Directories we never descend into when discovering .tf files.
_SKIP_DIRS = {".terraform", ".git", "node_modules", ".venv", "__pycache__", "dist"}

# Block header:  IDENT  ("label" | label)*  {
#   resource "aws_instance" "web" {      → kind=resource labels=[aws_instance, web]
#   provider "aws" {                     → kind=provider labels=[aws]
#   terraform {                          → kind=terraform labels=[]
#   locals {                             → kind=locals labels=[]
_HEADER_RE = re.compile(
    r'([A-Za-z_][A-Za-z0-9_-]*)'          # block kind
    r'((?:\s+(?:"[^"]*"|[A-Za-z0-9_.-]+))*)'  # zero or more labels
    r'\s*\{'                               # opening brace
)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_tf_files(root: Path) -> list[Path]:
    """Return every *.tf file under root, skipping vendored/hidden dirs."""
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix == ".tf" else []
    out: list[Path] = []
    for p in sorted(root.rglob("*.tf")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Low-level scanning helpers
# ---------------------------------------------------------------------------

def _strip_comments(text: str) -> str:
    """Remove #, // and /* */ comments without touching string literals."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:          # keep escaped char verbatim
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        # not in a string
        if c in '"\'':
            in_str = True
            quote = c
            out.append(c)
            i += 1
        elif c == "#" or (c == "/" and i + 1 < n and text[i + 1] == "/"):
            while i < n and text[i] != "\n":     # line comment
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _match_delim(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Index of the delimiter matching text[open_idx], honoring strings. -1 if none."""
    depth = 0
    i, n = open_idx, len(text)
    in_str = False
    quote = ""
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                in_str = False
        else:
            if c in '"\'':
                in_str = True
                quote = c
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _simplify(raw: str):
    """Best-effort convert an HCL value string to a Python value."""
    v = raw.strip()
    if not v:
        return ""
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    if v[0] == "[":                              # simple list literal
        end = _match_delim(v, 0, "[", "]")
        if end != -1:
            return _split_list(v[1:end])
    return v                                     # reference / expression — keep raw


def _split_list(inner: str) -> list:
    """Split a bracket body on top-level commas, simplifying each element."""
    items: list = []
    depth = 0
    in_str = False
    quote = ""
    cur = []
    for c in inner:
        if in_str:
            cur.append(c)
            if c == quote:
                in_str = False
            continue
        if c in '"\'':
            in_str = True
            quote = c
            cur.append(c)
        elif c in "[{(":
            depth += 1
            cur.append(c)
        elif c in "]})":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            token = "".join(cur).strip()
            if token:
                items.append(_simplify(token))
            cur = []
        else:
            cur.append(c)
    token = "".join(cur).strip()
    if token:
        items.append(_simplify(token))
    return items


# ---------------------------------------------------------------------------
# Body parser — attributes + nested blocks
# ---------------------------------------------------------------------------

def _parse_body(body: str) -> tuple[dict, list[dict]]:
    """Parse a block body into (attributes, subblocks). One level deep is enough."""
    attrs: dict = {}
    blocks: list[dict] = []
    i, n = 0, len(body)

    while i < n:
        c = body[i]
        if c.isspace():
            i += 1
            continue

        m = re.match(r'[A-Za-z_][A-Za-z0-9_-]*', body[i:])
        if not m:
            i += 1
            continue
        ident = m.group(0)
        j = i + len(ident)
        while j < n and body[j] in " \t":
            j += 1

        if j < n and body[j] == "=":             # attribute:  key = value
            j += 1
            while j < n and body[j] in " \t":
                j += 1
            if j >= n:
                break
            ch = body[j]
            if ch == '"':
                end = _match_delim(body, j, '"', '"') if False else _find_str_end(body, j)
                attrs[ident] = body[j + 1:end]
                i = end + 1
            elif ch == "[":
                end = _match_delim(body, j, "[", "]")
                attrs[ident] = _split_list(body[j + 1:end]) if end != -1 else []
                i = (end + 1) if end != -1 else n
            elif ch == "{":
                end = _match_delim(body, j, "{", "}")
                attrs[ident] = "{...}"            # inline object — record presence only
                i = (end + 1) if end != -1 else n
            elif body[j:j + 2] == "<<":           # heredoc
                i = _skip_heredoc(body, j, attrs, ident)
            else:
                nl = body.find("\n", j)
                nl = n if nl == -1 else nl
                attrs[ident] = _simplify(body[j:nl])
                i = nl
        else:                                     # nested block:  ident [labels] { ... }
            brace = body.find("{", j)
            if brace == -1:
                i = j
                continue
            # collect labels between ident and brace
            labels = re.findall(r'"([^"]*)"|([A-Za-z0-9_.-]+)', body[j:brace])
            label_vals = [a or b for a, b in labels]
            end = _match_delim(body, brace, "{", "}")
            if end == -1:
                break
            sub_attrs, _sub = _parse_body(body[brace + 1:end])
            blocks.append({"kind": ident, "labels": label_vals, "attributes": sub_attrs})
            i = end + 1

    return attrs, blocks


def _find_str_end(text: str, open_idx: int) -> int:
    """Index of the closing quote for the string opening at open_idx."""
    i = open_idx + 1
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i
        i += 1
    return n - 1


def _skip_heredoc(body: str, j: int, attrs: dict, ident: str) -> int:
    """Capture a heredoc value and return the index just past it."""
    m = re.match(r"<<-?\s*([A-Za-z0-9_]+)\n", body[j:])
    if not m:
        nl = body.find("\n", j)
        return len(body) if nl == -1 else nl
    term = m.group(1)
    start = j + m.end()
    end_m = re.search(rf"\n\s*{re.escape(term)}\b", body[start:])
    if not end_m:
        attrs[ident] = body[start:]
        return len(body)
    attrs[ident] = body[start:start + end_m.start()]
    return start + end_m.end()


# ---------------------------------------------------------------------------
# Top-level parse
# ---------------------------------------------------------------------------

def parse_hcl(text: str, filename: str = "") -> list[dict]:
    """Parse HCL source into a flat list of top-level blocks."""
    clean = _strip_comments(text)
    blocks: list[dict] = []
    pos = 0
    while True:
        m = _HEADER_RE.search(clean, pos)
        if not m:
            break
        brace_idx = m.end() - 1
        end = _match_delim(clean, brace_idx, "{", "}")
        if end == -1:
            break
        kind = m.group(1)
        labels = [
            a or b for a, b in re.findall(r'"([^"]*)"|([A-Za-z0-9_.-]+)', m.group(2))
        ]
        body = clean[brace_idx + 1:end]
        attrs, subs = _parse_body(body)
        line = clean.count("\n", 0, m.start()) + 1
        blocks.append({
            "kind":       kind,
            "labels":     labels,
            "attributes": attrs,
            "blocks":     subs,
            "file":       filename,
            "line":       line,
            "raw":        clean[m.start():end + 1],
        })
        pos = end + 1
    return blocks


def load_dir(root: Path) -> dict:
    """
    Parse every .tf file under root and bucket the blocks by kind.

    Returns a dict with lists for resources / data / providers / variables /
    modules / outputs, plus backend info and file metadata. Each resource/data
    entry gains `type` and `name` from its labels for convenience.
    """
    files = find_tf_files(root)
    out: dict = {
        "root":       str(root),
        "files":      [],
        "file_count": len(files),
        "resources":  [],
        "data":       [],
        "providers":  [],
        "variables":  [],
        "modules":    [],
        "outputs":    [],
        "backends":   [],
        "parse_errors": [],
    }

    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:                 # pragma: no cover - unreadable file
            out["parse_errors"].append(f"{f}: {exc}")
            continue
        rel = str(f.relative_to(root)) if root.is_dir() else f.name
        line_count = text.count("\n") + 1
        out["files"].append({"path": rel, "lines": line_count})

        try:
            blocks = parse_hcl(text, rel)
        except Exception as exc:
            out["parse_errors"].append(f"{rel}: {exc}")
            continue

        for b in blocks:
            kind, labels = b["kind"], b["labels"]
            if kind == "resource" and len(labels) >= 2:
                b["type"], b["name"] = labels[0], labels[1]
                out["resources"].append(b)
            elif kind == "data" and len(labels) >= 2:
                b["type"], b["name"] = labels[0], labels[1]
                out["data"].append(b)
            elif kind == "provider" and labels:
                b["name"] = labels[0]
                out["providers"].append(b)
            elif kind == "variable" and labels:
                b["name"] = labels[0]
                out["variables"].append(b)
            elif kind == "module" and labels:
                b["name"] = labels[0]
                out["modules"].append(b)
            elif kind == "output" and labels:
                b["name"] = labels[0]
                out["outputs"].append(b)
            elif kind == "terraform":
                for sub in b["blocks"]:
                    if sub["kind"] == "backend":
                        out["backends"].append({
                            "type": (sub["labels"][0] if sub["labels"] else "unknown"),
                            "attributes": sub["attributes"],
                            "file": b["file"],
                        })

    log.info(
        "terraform.loaded",
        files=out["file_count"],
        resources=len(out["resources"]),
        providers=len(out["providers"]),
        errors=len(out["parse_errors"]),
    )
    return out


# ---------------------------------------------------------------------------
# Plan JSON (terraform show -json)  — real blast radius
# ---------------------------------------------------------------------------

def parse_plan_json(path: Path) -> list[dict]:
    """
    Read a `terraform show -json <planfile>` document and return the planned
    resource changes as [{address, type, actions, replace}], where actions is a
    subset of {no-op, create, read, update, delete}. A ["delete","create"]
    action pair means the resource will be REPLACED (destroy + recreate).
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    changes: list[dict] = []
    for rc in data.get("resource_changes", []):
        change = rc.get("change", {}) or {}
        actions = change.get("actions", []) or []
        changes.append({
            "address": rc.get("address", ""),
            "type":    rc.get("type", ""),
            "name":    rc.get("name", ""),
            "actions": actions,
            "replace": actions == ["delete", "create"] or actions == ["create", "delete"],
        })
    return changes
