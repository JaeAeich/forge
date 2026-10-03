"""Grader strength via mutation testing: mutate the *fixed* code on the lines the gold patch touched and check the
hidden tests notice. Surviving mutants = behaviors the reward function doesn't actually check (i.e. a weak
grader an agent could pass with a wrong fix)."""

import ast
import copy
import json
import random
import zlib
from typing import Any

from . import code, db
from .sandbox import Sandbox
from .validate import gold_files, hidden_tests, run_tests

MAX_MUTANTS = 8

CMP_SWAP = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}
BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult, ast.Div: ast.Mult}


def _sites(tree: ast.AST, lines: set[int]) -> list[tuple[str, Any]]:
    sites = []
    for node in ast.walk(tree):
        ln = getattr(node, "lineno", None)
        if ln not in lines:
            continue
        if isinstance(node, ast.Compare) and type(node.ops[0]) in CMP_SWAP:
            sites.append(("cmp", node))
        elif isinstance(node, ast.BinOp) and type(node.op) in BIN_SWAP:
            sites.append(("binop", node))
        elif isinstance(node, ast.BoolOp):
            sites.append(("boolop", node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            sites.append(("bool", node))
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            sites.append(("int", node))
        elif isinstance(node, ast.Return) and node.value is not None:
            sites.append(("return_none", node))
        elif isinstance(node, ast.If):
            sites.append(("negate_if", node))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sites.append(("drop_not", node))
    return sites


def _apply(kind: str, node: Any) -> Any:
    if kind == "cmp":
        node.ops[0] = CMP_SWAP[type(node.ops[0])]()
    elif kind == "binop":
        node.op = BIN_SWAP[type(node.op)]()
    elif kind == "boolop":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif kind == "bool":
        node.value = not node.value
    elif kind == "int":
        node.value = node.value + 1
    elif kind == "return_none":
        node.value = ast.Constant(None)
    elif kind == "negate_if":
        node.test = ast.UnaryOp(ast.Not(), node.test)
    elif kind == "drop_not":
        return node.operand
    return node


def mutants(source: str, lines: set[int], seed: int = 0) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    n_sites = len(_sites(tree, lines))
    order = list(range(n_sites))
    random.Random(seed).shuffle(order)
    out = []
    for i in order[:MAX_MUTANTS]:
        t = copy.deepcopy(tree)
        kind, node = _sites(t, lines)[i]  # ast.walk order is deterministic
        new = _apply(kind, node)
        if new is not node:  # drop_not: replace node in its parent
            for parent in ast.walk(t):
                for field, val in ast.iter_fields(parent):
                    if val is node:
                        setattr(parent, field, new)
                    elif isinstance(val, list):
                        for j, v in enumerate(val):
                            if v is node:
                                val[j] = new
        try:
            mutated = ast.unparse(ast.fix_missing_locations(t))
        except Exception:
            continue
        out.append({"op": kind, "line": getattr(node, "lineno", 0), "source": mutated})
    return out


def mutate_task(task_id: str) -> dict[str, Any]:
    task = db.one("SELECT * FROM tasks WHERE id=?", task_id)
    f2p = json.loads(task["f2p"])
    parsed = code.parse_patch(task["gold_patch"])
    gold = gold_files(task)
    tests = hidden_tests(task)
    all_mutants = []
    for path, src in gold.items():
        lines = parsed.get(path, {}).get("added", set())
        for m in mutants(src, lines, seed=zlib.crc32(task_id.encode())):
            all_mutants.append((path, m))
    if not all_mutants:
        db.update("tasks", "id", task_id, mutation_score=None, mutants="[]")
        return {"mutants": 0}
    results: list[dict[str, Any]] = []
    with Sandbox.create(task["image"]) as sb:
        sb.put({f"/repo/{f}": c for f, c in {**tests, **gold}.items()})
        for path, m in all_mutants:
            sb.put({f"/repo/{path}": m["source"]})
            _, res, _ = run_tests(sb, list(tests), timeout=30)  # hangs count as killed
            killed = any(res.get(t) != "PASSED" for t in f2p)
            results.append({"file": path, "op": m["op"], "line": m["line"], "killed": killed})
            sb.put({f"/repo/{path}": gold[path]})
    score = sum(r["killed"] for r in results) / len(results)
    db.update("tasks", "id", task_id, mutation_score=round(score, 3), mutants=json.dumps(results))
    return {"mutants": len(results), "score": score}
