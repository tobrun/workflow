#!/usr/bin/env python3
"""Function-scoped mutation testing: mutate named functions one change at a time and run their fast tests.

    cd pkg && python3 ../tools/harden/mutate.py parser.py parse_header split_body \\
        --tests tests.test_parser.HeaderTests tests.test_parser.BodyTests

The ecosystem frameworks (mutmut, cosmic-ray) mutate whole modules and run one test command per mutant; in this
repository most modules pair with integration tests that take tens of seconds, so a whole-module run costs hours.
This mutates only the named functions (the pure logic a branch added) and runs only the unit tests that cover them.
Operators: comparison flips, `and`/`or` swaps, dropped `not`, arithmetic swaps, integer and boolean constant
changes, and returns replaced by `None`. The file is restored after every mutant, also on interrupt.
Exit 1 when any mutant survives; survivors print as `file:line operator (function)`.
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

# A same-size mutant written within the same second as the original would reuse its cached bytecode (pyc
# validation is size and mtime), so every run reads source only.
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def drop_bytecode(path: Path) -> None:
    for cached in (path.parent / "__pycache__").glob(f"{path.stem}.*.pyc"):
        cached.unlink(missing_ok=True)


COMPARE = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
           ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
ARITH = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}


def sites(tree: ast.Module, names: set[str]) -> list[tuple[ast.AST, str, str]]:
    """(node, operator label, enclosing function) for every mutable node inside the named functions."""
    found = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) or func.name not in names:
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Compare) and type(node.ops[0]) in COMPARE:
                found.append((node, f"{type(node.ops[0]).__name__} -> {COMPARE[type(node.ops[0])].__name__}", func.name))
            elif isinstance(node, ast.BoolOp):
                found.append((node, f"{type(node.op).__name__} -> {'Or' if isinstance(node.op, ast.And) else 'And'}",
                              func.name))
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                found.append((node, "drop not", func.name))
            elif isinstance(node, ast.BinOp) and type(node.op) in ARITH:
                found.append((node, f"{type(node.op).__name__} -> {ARITH[type(node.op)].__name__}", func.name))
            elif isinstance(node, ast.Constant) and type(node.value) in (int, bool):
                found.append((node, f"constant {node.value!r} changed", func.name))
            elif isinstance(node, ast.Return) and node.value is not None and not (
                    isinstance(node.value, ast.Constant) and node.value.value is None):
                found.append((node, "return None", func.name))
    return found


def mutate(node: ast.AST) -> ast.AST:
    if isinstance(node, ast.Compare):
        node.ops[0] = COMPARE[type(node.ops[0])]()
    elif isinstance(node, ast.BoolOp):
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif isinstance(node, ast.UnaryOp):
        return node.operand
    elif isinstance(node, ast.BinOp):
        node.op = ARITH[type(node.op)]()
    elif isinstance(node, ast.Constant):
        node.value = (not node.value) if isinstance(node.value, bool) else node.value + 1
    elif isinstance(node, ast.Return):
        node.value = ast.Constant(None)
    return node


class Swap(ast.NodeTransformer):
    def __init__(self, target_index: int, order: list[ast.AST]):
        self.target = order[target_index]

    def generic_visit(self, node: ast.AST) -> ast.AST:
        node = super().generic_visit(node)
        return mutate(node) if node is self.target else node


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("functions", nargs="+")
    parser.add_argument("--tests", nargs="+", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    path = Path(args.path)
    original = path.read_text(encoding="utf-8")
    command = [sys.executable, "-m", "unittest", "-q", *args.tests]
    drop_bytecode(path)
    baseline = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout, check=False, env=ENV)
    if baseline.returncode != 0:
        print(f"the tests fail before any mutation:\n{baseline.stderr[-2000:]}")
        return 2
    count = len(sites(ast.parse(original), set(args.functions)))
    survivors, killed = [], 0
    try:
        for index in range(count):
            tree = ast.parse(original)
            node, label, func = sites(tree, set(args.functions))[index]
            line = node.lineno
            order = [site[0] for site in sites(tree, set(args.functions))]
            mutated = ast.unparse(ast.fix_missing_locations(Swap(index, order).visit(tree)))
            path.write_text(mutated + "\n", encoding="utf-8")
            drop_bytecode(path)
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout, check=False,
                                        env=ENV)
                alive = result.returncode == 0
            except subprocess.TimeoutExpired:
                alive = False
            if alive:
                survivors.append(f"{path}:{line} {label} ({func})")
            else:
                killed += 1
    finally:
        path.write_text(original, encoding="utf-8")
        drop_bytecode(path)
    for survivor in survivors:
        print(f"SURVIVED {survivor}")
    print(f"{path}: {count} mutants, {killed} killed, {len(survivors)} survived")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
