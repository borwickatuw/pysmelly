"""Structural checks — duplicate code blocks and parameter clumps."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from pysmelly.checks.helpers import is_test_file
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check

NOISE_PARAMS = frozenset({"verbose", "debug", "dry_run", "timeout", "logger", "log", "quiet"})

# Decorators that indicate CLI dispatch or interface conformance —
# functions with these decorators share parameters by design, not by accident.
INTERFACE_DECORATORS = frozenset(
    {
        # abc / typing
        "abstractmethod",
        "override",
        # Click / Typer CLI
        "command",
        "group",
        "option",
        "argument",
        "pass_context",
        "pass_obj",
    }
)


def _dedup_and_format_locations(
    items: list[dict],
    file_key: str,
    func_key: str,
    line_key: str,
    line_end_key: str | None = None,
    max_display: int = 4,
) -> tuple[list[dict], str]:
    """Deduplicate items by (file, func) and format a locations string."""
    seen: set[tuple[str, str]] = set()
    deduped = []
    for item in items:
        key = (item[file_key], item[func_key])
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    def _fmt(item: dict) -> str:
        filename = item[file_key].split("/")[-1]
        func = item[func_key]
        start = item[line_key]
        if line_end_key and line_end_key in item:
            return f"{filename}:{func}():{start}-{item[line_end_key]}"
        return f"{filename}:{func}():{start}"

    locations_str = ", ".join(_fmt(item) for item in deduped[:max_display])
    if len(deduped) > max_display:
        locations_str += f" (+{len(deduped) - max_display} more)"

    return deduped, locations_str


@check(
    "duplicate-blocks",
    severity=Severity.MEDIUM,
    description="Structurally identical code blocks across functions",
)
def check_duplicate_blocks(ctx: AnalysisContext) -> list[Finding]:
    """Find duplicate code blocks across functions.

    Uses AST normalization to match structurally identical code
    even when variable names and literals differ.
    """
    findings = []

    all_blocks = []
    for filepath, tree in ctx.all_trees.items():
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
        deduped, locations_str = _dedup_and_format_locations(
            finding_data["blocks"], "file", "func", "line_start", line_end_key="line_end"
        )
        first = deduped[0]
        findings.append(
            Finding(
                file=first["file"],
                line=first["line_start"],
                check="duplicate-blocks",
                message=(
                    f"{finding_data['num_stmts']} duplicate statements "
                    f"(lines {first['line_start']}-{first['line_end']}) "
                    f"repeated in: {locations_str}"
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
    return "|".join(_ast_signature_parts(node))


def _ast_signature_parts(node: ast.AST):
    """Yield structure-only tokens for AST nodes."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                yield f"call:{child.func.id}"
            elif isinstance(child.func, ast.Attribute):
                yield f"call:.{child.func.attr}"
            else:
                yield "call:?"
            yield f"args:{len(child.args)},kw:{len(child.keywords)}"
        elif isinstance(child, ast.If):
            yield "if"
        elif isinstance(child, ast.For):
            yield "for"
        elif isinstance(child, ast.Return):
            yield "return"
        elif isinstance(child, ast.Assign):
            yield "assign"
        elif isinstance(child, ast.Expr):
            yield "expr"
        elif isinstance(child, ast.Compare):
            ops = ",".join(type(op).__name__ for op in child.ops)
            yield f"cmp:{ops}"
        elif isinstance(child, ast.Attribute):
            yield f".{child.attr}"


def _extract_statement_blocks(
    tree: ast.Module, filepath: Path, min_statements: int = 5
) -> list[dict]:
    """Extract consecutive statement blocks from all code blocks in functions."""
    blocks = []

    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Walk function body without descending into nested functions/classes
        # (they'll be processed as separate func_nodes)
        statement_lists = []
        worklist = [func_node]
        while worklist:
            node = worklist.pop()
            for attr in ("body", "orelse", "finalbody"):
                body = getattr(node, attr, None)
                if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                    statement_lists.append(body)
            if isinstance(node, ast.ExceptHandler) and node.body:
                statement_lists.append(node.body)
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if child is not func_node:
                        continue
                worklist.append(child)

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


@check(
    "duplicate-except-blocks",
    severity=Severity.MEDIUM,
    description="Identical except handlers with same error messages across files",
)
def check_duplicate_except_blocks(ctx: AnalysisContext) -> list[Finding]:
    """Find duplicate except handlers across different files.

    Higher confidence than duplicate-blocks: matches exception type,
    structure, AND string literals together.
    """
    findings = []

    all_handlers: list[dict] = []
    for filepath, tree in ctx.all_trees.items():
        all_handlers.extend(_extract_except_handlers(tree, filepath))

    by_sig: dict[str, list[dict]] = defaultdict(list)
    for handler in all_handlers:
        by_sig[handler["signature"]].append(handler)

    reported: set[frozenset] = set()

    for sig, handlers in by_sig.items():
        if len(handlers) < 2:
            continue

        # Cross-file only — structural similarity with _find_param_clumps, not extractable
        files = {h["file"] for h in handlers}  # pysmelly: ignore[duplicate-blocks]
        if len(files) < 2:
            continue

        locations_key = frozenset((h["file"], h["func"], h["line"]) for h in handlers)
        if locations_key in reported:
            continue
        reported.add(locations_key)

        deduped, locations_str = _dedup_and_format_locations(handlers, "file", "func", "line")
        first = deduped[0]
        findings.append(
            Finding(
                file=first["file"],
                line=first["line"],
                check="duplicate-except-blocks",
                message=(
                    f"except {first['exc_type']}: {first['num_stmts']} duplicate handler "
                    f"statements with same error messages in: {locations_str}"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


def _get_exception_type_name(handler: ast.ExceptHandler) -> str:
    """Normalize exception type to a string for signature matching."""
    if handler.type is None:
        return "bare"
    if isinstance(handler.type, ast.Name):
        return handler.type.id
    if isinstance(handler.type, ast.Attribute):
        return handler.type.attr
    if isinstance(handler.type, ast.Tuple):
        names = []
        for elt in handler.type.elts:
            if isinstance(elt, ast.Name):
                names.append(elt.id)
            elif isinstance(elt, ast.Attribute):
                names.append(elt.attr)
            else:
                names.append("?")
        return ",".join(sorted(names))
    return "?"


def _extract_string_constants(nodes: list[ast.stmt]) -> list[str]:
    """Collect all string constants in an AST subtree list."""
    strings = []
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                strings.append(child.value)
    return sorted(strings)


def _except_handler_signature(handler: ast.ExceptHandler) -> str | None:
    """Build a signature combining exception type, structure, and string literals.

    Returns None for trivial handlers (pass-only, bare-raise-only, or < 2 statements).
    """
    body = handler.body
    if len(body) < 2:
        return None

    # Skip pass-only
    if all(isinstance(s, ast.Pass) for s in body):
        return None

    # Skip bare-raise-only
    if len(body) == 1 and isinstance(body[0], ast.Raise) and body[0].exc is None:
        return None

    exc_type = _get_exception_type_name(handler)
    wrapper = ast.Module(body=body, type_ignores=[])
    struct = _normalize_ast(wrapper)
    strings = _extract_string_constants(body)

    return f"exc:{exc_type}|struct:{struct}|strings:{'|'.join(strings)}"


def _extract_except_handlers(tree: ast.Module, filepath: Path) -> list[dict]:
    """Extract all except handlers from a file with their signatures."""
    handlers = []

    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for node in ast.walk(func_node):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                sig = _except_handler_signature(handler)
                if sig is None:
                    continue
                handlers.append(
                    {
                        "file": str(filepath),
                        "func": func_node.name,
                        "line": handler.lineno,
                        "num_stmts": len(handler.body),
                        "exc_type": _get_exception_type_name(handler),
                        "signature": sig,
                    }
                )

    return handlers


@check(
    "param-clumps",
    severity=Severity.MEDIUM,
    description="Groups of 3+ parameters appearing together in 3+ function signatures",
)
def check_param_clumps(ctx: AnalysisContext) -> list[Finding]:
    """Find groups of parameters that recur together across function signatures.

    When 3+ parameters appear together in 3+ function signatures,
    it's a strong signal to extract a dataclass or config object.
    """
    findings = []
    signatures = _extract_all_signatures(ctx.all_trees)
    clumps = _find_param_clumps(signatures)

    for clump in clumps:
        params = sorted(clump["params"])
        locs = clump["locations"]

        deduped, locs_str = _dedup_and_format_locations(
            locs, "file", "func_name", "line", max_display=6
        )
        params_str = ", ".join(params)
        first = deduped[0]
        findings.append(
            Finding(
                file=first["file"],
                line=first["line"],
                check="param-clumps",
                message=(
                    f"Parameters ({params_str}) appear together in "
                    f"{len(deduped)} functions: {locs_str} "
                    f"— consider extracting a dataclass"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


def _has_interface_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function has CLI dispatch or interface conformance decorators."""
    for deco in node.decorator_list:
        # @abstractmethod, @override
        if isinstance(deco, ast.Name) and deco.id in INTERFACE_DECORATORS:
            return True
        # @click.command(), @click.option(...)
        if isinstance(deco, ast.Call):
            func = deco.func
            if isinstance(func, ast.Attribute) and func.attr in INTERFACE_DECORATORS:
                return True
            if isinstance(func, ast.Name) and func.id in INTERFACE_DECORATORS:
                return True
        # @click.command (without parens), @app.command
        if isinstance(deco, ast.Attribute) and deco.attr in INTERFACE_DECORATORS:
            return True
    return False


def _get_meaningful_params(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    """Extract param names excluding self/cls/vararg/kwarg/noise."""
    params = set()
    for arg in func_node.args.posonlyargs + func_node.args.args + func_node.args.kwonlyargs:
        if arg.arg not in ("self", "cls") and arg.arg not in NOISE_PARAMS:
            params.add(arg.arg)
    return frozenset(params)


def _extract_all_signatures(all_trees: dict[Path, ast.Module]) -> list[dict]:
    """Walk all trees, yield signature info for every qualifying function.

    Includes methods, private functions, and decorated functions (broader
    than build_function_index) but excludes nested functions, test functions,
    test files, and functions with CLI dispatch or interface decorators.
    """
    signatures = []

    for filepath, tree in all_trees.items():
        if is_test_file(filepath):
            continue

        file_str = str(filepath)

        # Collect nested function ids to exclude them
        nested_funcs: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    if child is node:
                        continue
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        nested_funcs.add(id(child))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if id(node) in nested_funcs:
                continue
            if node.name.startswith("test"):
                continue
            if _has_interface_decorator(node):
                continue

            params = _get_meaningful_params(node)
            if len(params) < 3:
                continue

            signatures.append(
                {
                    "params": params,
                    "func_name": node.name,
                    "file": file_str,
                    "line": node.lineno,
                }
            )

    return signatures


def _find_param_clumps(
    signatures: list[dict],
    min_clump_size: int = 3,
    min_occurrences: int = 3,
) -> list[dict]:
    """Find groups of parameters that appear together in multiple signatures.

    Phase 1: Exact matches (identical param sets in 3+ functions).
    Phase 2: Intersection discovery (common subsets across different param sets).
    Then deduplicate: suppress subset clumps when a superset covers the same locations.
    """
    by_params: dict[frozenset[str], list[dict]] = defaultdict(list)
    for sig in signatures:
        by_params[sig["params"]].append(sig)

    # Collect candidate clumps keyed by param set
    candidates: dict[frozenset[str], list[dict]] = {}

    # Phase 1: Exact matches
    for params, sigs in by_params.items():
        if len(params) >= min_clump_size and len(sigs) >= min_occurrences:
            candidates[params] = sigs

    # Phase 2: Intersection discovery
    distinct_sets = list(by_params.keys())
    intersections_seen: set[frozenset[str]] = set()

    for i in range(len(distinct_sets)):
        for j in range(i + 1, len(distinct_sets)):
            intersection = distinct_sets[i] & distinct_sets[j]
            if len(intersection) < min_clump_size:
                continue
            key = frozenset(intersection)
            if key in intersections_seen:
                continue
            intersections_seen.add(key)

            matching = [s for s in signatures if key <= s["params"]]
            if len(matching) >= min_occurrences:
                # Keep the version with more locations
                if key not in candidates or len(matching) > len(candidates[key]):
                    candidates[key] = matching

    clumps = [{"params": k, "locations": v} for k, v in candidates.items()]

    # Deduplicate: suppress subset if locations also subset
    to_remove: set[int] = set()
    for i in range(len(clumps)):
        for j in range(len(clumps)):
            if i == j:
                continue
            if clumps[i]["params"] < clumps[j]["params"]:  # proper subset
                locs_i = {(s["file"], s["func_name"]) for s in clumps[i]["locations"]}
                locs_j = {(s["file"], s["func_name"]) for s in clumps[j]["locations"]}
                if locs_i <= locs_j:
                    to_remove.add(i)

    return [c for i, c in enumerate(clumps) if i not in to_remove]
