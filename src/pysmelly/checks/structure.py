"""Structural checks — function signatures, duplicate code blocks."""

import ast
from collections import defaultdict
from pathlib import Path

from pysmelly.registry import Finding, Severity, check


@check("too-many-params", severity=Severity.MEDIUM)
def check_too_many_params(
    all_trees: dict[Path, ast.Module], verbose: bool
) -> list[Finding]:
    """Find functions with too many parameters (consider a dataclass)."""
    findings = []
    threshold = 6

    for filepath, tree in all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            params = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
            count = len(params)

            if count >= threshold:
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="too-many-params",
                        message=(
                            f"{node.name}() takes {count} parameters — "
                            f"consider grouping into a dataclass"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )

    return findings


@check("duplicate-blocks", severity=Severity.MEDIUM)
def check_duplicate_blocks(
    all_trees: dict[Path, ast.Module], verbose: bool
) -> list[Finding]:
    """Find duplicate code blocks across functions.

    Uses AST normalization to match structurally identical code
    even when variable names and literals differ.
    """
    findings = []

    all_blocks = []
    for filepath, tree in all_trees.items():
        all_blocks.extend(_extract_statement_blocks(tree, filepath))

    by_sig: dict[str, list[dict]] = defaultdict(list)
    for block in all_blocks:
        by_sig[block["signature"]].append(block)

    best_per_pair: dict[frozenset, dict] = {}

    for sig, blocks in by_sig.items():
        if len(blocks) < 2:
            continue

        unique_locations = set()
        for b in blocks:
            unique_locations.add((b["file"], b["func"], b["line_start"]))
        if len(unique_locations) < 2:
            continue

        locations = frozenset((b["file"], b["func"]) for b in blocks)
        if len(locations) < 2:
            continue

        blocks.sort(key=lambda b: -b["num_stmts"])
        best = blocks[0]

        existing = best_per_pair.get(locations)
        if existing is None or best["num_stmts"] > existing["num_stmts"]:
            best_per_pair[locations] = {
                "num_stmts": best["num_stmts"],
                "blocks": blocks,
            }

    for finding_data in sorted(best_per_pair.values(), key=lambda f: -f["num_stmts"]):
        blocks = finding_data["blocks"]
        seen = set()
        deduped = []
        for b in blocks:
            key = (b["file"], b["func"])
            if key not in seen:
                seen.add(key)
                deduped.append(b)

        locations_str = ", ".join(
            f"{b['file'].split('/')[-1]}:{b['func']}():{b['line_start']}" for b in deduped[:4]
        )
        if len(deduped) > 4:
            locations_str += f" (+{len(deduped) - 4} more)"

        first = deduped[0]
        findings.append(
            Finding(
                file=first["file"],
                line=first["line_start"],
                check="duplicate-blocks",
                message=(
                    f"{finding_data['num_stmts']} duplicate statements at: {locations_str}"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


def _normalize_ast(node: ast.AST) -> str:
    """Produce a structure-only string from an AST node.

    Strips variable names, string literals, and numbers so that
    structurally identical code with different names matches.
    """
    parts = []

    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                parts.append(f"call:{child.func.id}")
            elif isinstance(child.func, ast.Attribute):
                parts.append(f"call:.{child.func.attr}")
            else:
                parts.append("call:?")
            parts.append(f"args:{len(child.args)},kw:{len(child.keywords)}")
        elif isinstance(child, ast.If):
            parts.append("if")
        elif isinstance(child, ast.For):
            parts.append("for")
        elif isinstance(child, ast.Return):
            parts.append("return")
        elif isinstance(child, ast.Assign):
            parts.append("assign")
        elif isinstance(child, ast.Expr):
            parts.append("expr")
        elif isinstance(child, ast.Compare):
            ops = ",".join(type(op).__name__ for op in child.ops)
            parts.append(f"cmp:{ops}")
        elif isinstance(child, ast.Attribute):
            parts.append(f".{child.attr}")

    return "|".join(parts)


def _extract_statement_blocks(
    tree: ast.Module, filepath: Path, min_statements: int = 5
) -> list[dict]:
    """Extract consecutive statement blocks from all code blocks in functions."""
    blocks = []

    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        statement_lists = []
        for node in ast.walk(func_node):
            for attr in ("body", "orelse", "finalbody"):
                body = getattr(node, attr, None)
                if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                    statement_lists.append(body)
            if isinstance(node, ast.ExceptHandler) and node.body:
                statement_lists.append(node.body)

        for body in statement_lists:
            for size in range(min_statements, min(len(body) + 1, 20)):
                for start in range(len(body) - size + 1):
                    stmts = body[start : start + size]
                    wrapper = ast.Module(body=stmts, type_ignores=[])
                    signature = _normalize_ast(wrapper)
                    if len(signature) < 40:
                        continue

                    blocks.append(
                        {
                            "file": str(filepath),
                            "func": func_node.name,
                            "line_start": stmts[0].lineno,
                            "line_end": stmts[-1].end_lineno or stmts[-1].lineno,
                            "num_stmts": size,
                            "signature": signature,
                        }
                    )

    return blocks
