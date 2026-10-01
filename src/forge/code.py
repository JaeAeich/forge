"""Code helpers: tree-sitter symbol extraction, unified-diff parsing, MinHash, test-file heuristics."""

import hashlib
import re
from typing import Any

import numpy as np
import tree_sitter_python as tsp
from tree_sitter import Language, Parser

_PY = Language(tsp.language())

CONFIG_FILES = {
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
    "sitecustomize.py",
    "usercustomize.py",
    "noxfile.py",
}


def parser() -> Parser:
    return Parser(_PY)


def is_test_path(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or name == "tests.py"
        or any(p in ("tests", "test", "testing") for p in parts[:-1])
    )


def is_test_infra(path: str) -> bool:
    return is_test_path(path) or path.split("/")[-1] in CONFIG_FILES or path.endswith(".pth")


def symbols(source: str) -> list[tuple[str, str, int, int]]:
    """Functions/classes (incl. methods) -> [(qualname, kind, start_line, end_line)] 1-based inclusive."""
    tree = parser().parse(source.encode())
    out = []

    def walk(node: Any, prefix: str) -> None:  # tree_sitter.Node (Any: its optional accessors aren't narrowed here)
        for ch in node.children:
            n = ch
            if ch.type == "decorated_definition":
                n = ch.child_by_field_name("definition") or ch
            if n.type in ("function_definition", "class_definition"):
                name = n.child_by_field_name("name").text.decode()
                q = f"{prefix}.{name}" if prefix else name
                out.append(
                    (
                        q,
                        "class" if n.type == "class_definition" else "function",
                        ch.start_point[0] + 1,
                        ch.end_point[0] + 1,
                    )
                )
                body = n.child_by_field_name("body")
                if body is not None:
                    walk(body, q)

    walk(tree.root_node, "")
    return out


def enclosing_symbols(source: str, lines: set[int]) -> list[str]:
    syms = symbols(source)
    hit = []
    for ln in lines:
        best = None
        for q, k, s, e in syms:
            if s <= ln <= e and (best is None or e - s < best[3] - best[2]):
                best = (q, k, s, e)
        hit.append(best[0] if best else "<module>")
    return sorted(set(hit))


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(patch: str) -> dict[str, dict[str, Any]]:
    """-> {path: {"added": {new_lineno}, "added_text": [...], "removed_text": [...], "new": bool}}

    A rename/copy target counts as new; a rename source is listed too (it was removed), so callers that
    restore or flag test infra see both ends."""
    files: dict[str, dict[str, Any]] = {}
    cur, new_ln, in_hunk = None, 0, False

    def entry(path: str) -> dict[str, Any]:
        return files.setdefault(path, {"added": set(), "added_text": [], "removed_text": [], "new": False})

    for line in patch.splitlines():
        if line.startswith("diff --git"):
            cur, in_hunk = entry(line.split(" b/", 1)[-1]), False
        elif cur is None:
            continue
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            new_ln, in_hunk = (int(m.group(3)) if m else 0), True
        elif not in_hunk:  # extended headers: only before the first hunk ("--- x" inside a hunk is content)
            if line.startswith(("new file mode", "copy to ", "rename to ")):
                cur["new"] = True
            if line.startswith("rename from "):
                entry(line.removeprefix("rename from "))
        elif line.startswith("\\"):  # "\ No newline at end of file"
            continue
        elif line.startswith("+"):
            cur["added"].add(new_ln)
            cur["added_text"].append(line[1:])
            new_ln += 1
        elif line.startswith("-"):
            cur["removed_text"].append(line[1:])
        else:
            new_ln += 1
    return files


# ---- MinHash (near-duplicate detection) ----
_P = (1 << 61) - 1
_rng = np.random.default_rng(1234)
NPERM = 64
_A = _rng.integers(1, _P, NPERM, dtype=np.uint64)
_B = _rng.integers(0, _P, NPERM, dtype=np.uint64)
_TOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|\S")


def shingles(text: str, k: int = 5) -> set[str]:
    toks = _TOK.findall(text)
    return {" ".join(toks[i : i + k]) for i in range(max(1, len(toks) - k + 1))}


def minhash(text: str) -> np.ndarray:
    sh = shingles(text)
    hs = np.array(
        [int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "little") & _P for s in sh],
        dtype=np.uint64,
    )
    # (a*h + b) mod p, computed in float-safe way via python ints would be slow; use uint64 wraparound hashing.
    vals = (hs[None, :] * _A[:, None] + _B[:, None]) >> np.uint64(3)
    return vals.min(axis=1)


BANDS = 16  # LSH: 16 bands x 4 rows; two chunks sharing any band are near-duplicate candidates


def band_keys(sig: np.ndarray) -> list[int]:
    """One signed 64-bit key per LSH band (fits a Postgres BIGINT index)."""
    rows = len(sig) // BANDS
    return [
        int.from_bytes(
            hashlib.blake2b(sig[b * rows : (b + 1) * rows].tobytes(), digest_size=8).digest(), "little", signed=True
        )
        for b in range(BANDS)
    ]


def jaccard_est(a: np.ndarray, b: np.ndarray) -> float:
    return float((a == b).mean())
