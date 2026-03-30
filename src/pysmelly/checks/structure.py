"""Structural checks — duplicate code blocks, parameter clumps, class structure."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from pysmelly.checks.helpers import is_test_file
from pysmelly.context import AnalysisContext
from pysmelly.registry import MAX_DISPLAY_WIDTH, Finding, Severity, check

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


_SIMPLE_AST_TOKENS: dict[type, str] = {
    ast.If: "if",
    ast.For: "for",
    ast.While: "while",
    ast.With: "with",
    ast.Return: "return",
    ast.Assign: "assign",
    ast.Expr: "expr",
    ast.Raise: "raise",
    ast.Assert: "assert",
    ast.Try: "try",
}


def _ast_signature_parts(node: ast.AST):
    """Yield structure-only tokens for AST nodes."""
    for child in ast.walk(node):
        # Nodes with custom token extraction
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                yield f"call:{child.func.id}"
            elif isinstance(child.func, ast.Attribute):
                yield f"call:.{child.func.attr}"
            else:
                yield "call:?"
            yield f"args:{len(child.args)},kw:{len(child.keywords)}"
        elif isinstance(child, ast.Compare):
            ops = ",".join(type(op).__name__ for op in child.ops)
            yield f"cmp:{ops}"
        elif isinstance(child, ast.Attribute):
            yield f".{child.attr}"
        else:
            # Simple nodes: type -> fixed token string
            token = _SIMPLE_AST_TOKENS.get(type(child))
            if token:
                yield token


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
                    if len(signature) < MAX_DISPLAY_WIDTH:
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


# --- middle-man helpers ---


def _get_non_dunder_methods(
    class_node: ast.ClassDef,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Get all methods except dunder methods from a class body."""
    methods = []
    for item in class_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not item.name.startswith("__"):
                methods.append(item)
    return methods


def _get_delegation_target(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    """Check if a method body is pure delegation to self.X.

    Returns the delegated attribute name (X in self.X.method()), or None.
    Handles: return self.X.method(...), self.X.method(...) as expression,
    and return self.X.attr (property-style).
    """
    body = method.body
    # Strip docstring
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]

    if len(body) != 1:
        return None

    stmt = body[0]

    # return self.X.method(...) or return self.X.attr
    if isinstance(stmt, ast.Return) and stmt.value is not None:
        return _extract_self_delegation_attr(stmt.value)

    # self.X.method(...) as expression statement (void delegation)
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return _extract_self_delegation_attr(stmt.value)

    return None


def _extract_self_delegation_attr(expr: ast.expr) -> str | None:
    """Extract the delegated attribute name from self.X.method(...) or self.X.attr."""
    # Unwrap Call to get the function being called
    inner = expr
    if isinstance(inner, ast.Call):
        inner = inner.func

    # self.X.method or self.X.attr
    if (
        isinstance(inner, ast.Attribute)
        and isinstance(inner.value, ast.Attribute)
        and isinstance(inner.value.value, ast.Name)
        and inner.value.value.id == "self"
    ):
        return inner.value.attr

    return None


@check(
    "middle-man",
    severity=Severity.MEDIUM,
    description="Classes that delegate most methods to a single wrapped object",
)
def check_middle_man(ctx: AnalysisContext) -> list[Finding]:
    """Find classes where most methods just delegate to self.X.method().

    Middle-man classes appear after migrations complete but the adapter layer
    stays, or after refactoring extracts real logic from a wrapper, leaving
    a pure pass-through that adds indirection without value.
    """
    findings = []
    min_methods = 3
    min_ratio = 0.75

    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            methods = _get_non_dunder_methods(node)
            if len(methods) < min_methods:
                continue

            # Count delegation targets per attribute
            delegation_counts: dict[str, int] = {}
            for method in methods:
                attr = _get_delegation_target(method)
                if attr:
                    delegation_counts[attr] = delegation_counts.get(attr, 0) + 1

            if not delegation_counts:
                continue

            best_attr = max(delegation_counts, key=lambda k: delegation_counts[k])
            best_count = delegation_counts[best_attr]

            if best_count >= min_methods and best_count / len(methods) >= min_ratio:
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="middle-man",
                        message=(
                            f"{node.name} delegates {best_count}/{len(methods)} "
                            f"methods to self.{best_attr} — consider removing "
                            f"the middleman"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )

    return findings


# --- shadowed-method helpers ---


def _build_class_index(
    all_trees: dict[Path, ast.Module],
) -> dict[str, list[dict]]:
    """Build index of class definitions with their methods and base names.

    Returns {class_name: [{file, line, methods, method_nodes, bases}, ...]}.
    Multiple entries per name handle classes defined in different files.
    """
    index: dict[str, list[dict]] = defaultdict(list)
    for filepath, tree in all_trees.items():
        if is_test_file(filepath):
            continue
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            methods: set[str] = set()
            method_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.add(item.name)
                    method_nodes[item.name] = item
            bases: list[str] = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.append(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.append(base.attr)
            index[node.name].append(
                {
                    "file": str(filepath),
                    "line": node.lineno,
                    "methods": methods,
                    "method_nodes": method_nodes,
                    "bases": bases,
                }
            )
    return dict(index)


def _get_base_methods(base_name: str, class_index: dict[str, list[dict]]) -> set[str]:
    """Get all methods defined directly on a base class."""
    methods: set[str] = set()
    for class_def in class_index.get(base_name, []):
        methods |= class_def["methods"]
    return methods


def _method_calls_super(
    base_name: str, method_name: str, class_index: dict[str, list[dict]]
) -> bool:
    """Check if a base class's method calls super().method_name().

    If it does, this is cooperative multiple inheritance and the
    'shadowed' parent's version still participates via the MRO chain.
    """
    for class_def in class_index.get(base_name, []):
        node = class_def.get("method_nodes", {}).get(method_name)
        if node is None:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            # super().method_name() or super(Cls, self).method_name()
            func = child.func
            if isinstance(func, ast.Attribute) and func.attr == method_name:
                if isinstance(func.value, ast.Call):
                    call_func = func.value.func
                    if isinstance(call_func, ast.Name) and call_func.id == "super":
                        return True
    return False


@check(
    "shadowed-method",
    severity=Severity.MEDIUM,
    description="Diamond inheritance where a method is defined in multiple parents",
)
def check_shadowed_methods(ctx: AnalysisContext) -> list[Finding]:
    """Find diamond inheritance where multiple parents define the same method.

    When a class inherits from A and B, and both define execute(), MRO
    silently picks one — the other's implementation is dead code. This is
    a common source of subtle bugs: ConditionalRecurringTask inherits from
    both RecurringTask and ConditionalTask, both define execute(), and
    MRO picks RecurringTask's — the condition check never runs.

    This is an investigation pointer: Claude Code should verify that the
    MRO resolution is intentional, not accidental.
    """
    findings = []
    class_index = _build_class_index(ctx.all_trees)

    for class_name, defs in class_index.items():
        for class_info in defs:
            bases = class_info["bases"]
            if len(bases) < 2:
                continue

            # Collect methods defined on each base (direct only)
            method_sources: dict[str, list[str]] = defaultdict(list)
            for base_name in bases:
                for method in _get_base_methods(base_name, class_index):
                    method_sources[method].append(base_name)

            child_methods = class_info["methods"]

            for method_name, source_bases in method_sources.items():
                if len(source_bases) < 2:
                    continue
                # Child overrides the method — conflict resolved
                if method_name in child_methods:
                    continue
                # Skip dunders — commonly inherited from object/ABC
                if method_name.startswith("__") and method_name.endswith("__"):
                    continue

                # MRO: leftmost base wins
                winner = source_bases[0]
                losers = [b for b in source_bases[1:] if b != winner]
                if not losers:
                    continue

                # If the winner calls super(), the "losers" still
                # participate via cooperative multiple inheritance
                if _method_calls_super(winner, method_name, class_index):
                    continue

                findings.append(
                    Finding(
                        file=class_info["file"],
                        line=class_info["line"],
                        check="shadowed-method",
                        message=(
                            f"{class_name} inherits {method_name}() from "
                            f"both {winner} and {', '.join(losers)} — "
                            f"MRO uses {winner}'s version, "
                            f"{', '.join(losers)}'s is silently shadowed"
                        ),
                        severity=Severity.MEDIUM,
                    )
                )

    return findings


# --- large-class ---


@check(
    "large-class",
    severity=Severity.LOW,
    description="Classes with many methods — review for single responsibility",
)
def check_large_class(ctx: AnalysisContext) -> list[Finding]:
    """Find classes with 20+ non-dunder methods.

    Classes grow by accretion — each feature adds a method, nobody splits
    the class. ApplicationManager with 49 methods handles users, database,
    email, logging, reports, files, and sorting all in one class.

    Investigation pointer: Claude Code can review whether the class has
    multiple distinct responsibilities that should be separate classes.
    """
    findings = []
    min_methods = 20

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            methods = [
                item
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not (item.name.startswith("__") and item.name.endswith("__"))
            ]
            if len(methods) >= min_methods:
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="large-class",
                        message=(
                            f"{node.name} has {len(methods)} methods "
                            f"— review for single responsibility"
                        ),
                        severity=Severity.LOW,
                    )
                )

    return findings


# --- long-function ---


@check(
    "long-function",
    severity=Severity.LOW,
    description="Functions spanning 100+ lines — review for decomposition",
)
def check_long_function(ctx: AnalysisContext) -> list[Finding]:
    """Find functions spanning 100+ lines.

    Different from ruff's C901 (cyclomatic complexity): a 200-line function
    with simple sequential steps won't trigger C901 but is still a
    decomposition candidate. Long functions grow by accretion — each
    change adds a few lines, nobody extracts helpers.

    Investigation pointer: Claude Code can identify natural phase boundaries
    (validation, transformation, formatting) and suggest extraction.
    """
    findings = []
    min_lines = 100

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.end_lineno:
                continue
            lines = node.end_lineno - node.lineno + 1
            if lines >= min_lines:
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=node.lineno,
                        check="long-function",
                        message=(
                            f"{node.name}() spans {lines} lines " f"— review for decomposition"
                        ),
                        severity=Severity.LOW,
                    )
                )

    return findings


# --- long-elif-chain helpers ---


def _count_elif_chain(node: ast.If) -> tuple[int, str | None]:
    """Count branches in an if/elif chain and identify the compared variable.

    Returns (branch_count, compared_variable_name_or_None).
    """
    count = 1
    compared_var = _get_comparison_target(node.test)
    current = node
    while current.orelse and len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
        current = current.orelse[0]
        count += 1
        # Check that each branch compares the same variable
        branch_var = _get_comparison_target(current.test)
        if compared_var is not None and branch_var != compared_var:
            compared_var = None  # mixed comparisons, not a dispatch chain
    # Count the else branch if present
    if current.orelse:
        count += 1
    return count, compared_var


def _get_comparison_target(test: ast.expr) -> str | None:
    """Extract the variable name from a comparison like 'x == literal'.

    Returns the variable name, or None if the test isn't a simple comparison.
    """
    if not isinstance(test, ast.Compare):
        return None
    if len(test.ops) != 1:
        return None
    if not isinstance(test.ops[0], (ast.Eq, ast.Is)):
        return None
    left = test.left
    if isinstance(left, ast.Name):
        return left.id
    if isinstance(left, ast.Attribute):
        return _elif_attr_str(left)
    return None


def _elif_attr_str(node: ast.Attribute) -> str:
    """Build a dotted string from an attribute chain."""
    if isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    if isinstance(node.value, ast.Attribute):
        return f"{_elif_attr_str(node.value)}.{node.attr}"
    return node.attr


@check(
    "long-elif-chain",
    severity=Severity.LOW,
    description="Long if/elif chains that may be replaceable with a dispatch dict or enum",
)
def check_long_elif_chain(ctx: AnalysisContext) -> list[Finding]:
    """Find if/elif chains with 8+ branches comparing the same variable.

    These chains grow by accretion — each new status code, format type,
    or category gets another elif. They're often replaceable with a dict
    mapping or enum, which is more maintainable and extensible.

    Investigation pointer: Claude Code can evaluate whether a dispatch
    dict, enum, or match statement would be cleaner.
    """
    findings = []
    min_branches = 8

    for filepath, tree in ctx.all_trees.items():
        if is_test_file(filepath):
            continue

        # Track which If nodes we've already counted (to avoid
        # re-counting elif sub-chains)
        counted: set[int] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            if id(node) in counted:
                continue

            branch_count, compared_var = _count_elif_chain(node)
            if branch_count < min_branches:
                continue

            # Mark all nodes in this chain as counted
            current = node
            while True:
                counted.add(id(current))
                if (
                    current.orelse
                    and len(current.orelse) == 1
                    and isinstance(current.orelse[0], ast.If)
                ):
                    current = current.orelse[0]
                else:
                    break

            # Find enclosing function name
            func_name = None
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.end_lineno and fn.lineno <= node.lineno <= fn.end_lineno:
                    func_name = fn.name

            if compared_var:
                detail = f"comparing {compared_var} to literals " f"— consider a dict or enum"
            else:
                detail = "— review for dispatch table or decomposition"

            location = f"{func_name}()" if func_name else "module level"

            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="long-elif-chain",
                    message=(f"{branch_count}-branch if/elif chain in " f"{location} {detail}"),
                    severity=Severity.LOW,
                )
            )

    return findings
