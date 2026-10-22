"""Pattern checks for naming and stringly-typed access.

Hungarian notation, getattr, passwords, closures.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from functools import partial

from pysmelly.checks.helpers import bare_target, is_bare_name, is_secret_name, source_trees
from pysmelly.context import AnalysisContext
from pysmelly.registry import Advice, Finding, Severity, Site, check, multi_site_finding

# --- hungarian-notation ---

# Apps Hungarian (type-as-prefix): strName, intCount, lstItems
# Systems Hungarian (storage-as-prefix): szName, lpBuffer, dwFlags, fnCallback
_HUNGARIAN_RE = re.compile(
    r"^(str|int|bool|lst|dict|arr|obj|flt|tpl|set"  # Apps Hungarian
    r"|sz|lp|dw|fn|cb|rg|pi|pf|pp|lpsz|pfn)[A-Z]"  # Systems Hungarian
)


def _to_snake_case(name: str) -> str:
    """Convert camelCase hungarian name to snake_case suggestion."""
    # Split on uppercase letter boundaries
    parts: list[str] = []
    current: list[str] = []
    for ch in name:
        if ch.isupper() and current:
            parts.append("".join(current).lower())
            current = [ch]
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).lower())
    return "_".join(parts)


@check(
    "hungarian-notation",
    severity=Severity.LOW,
    description="Variables using Hungarian notation (strName, intCount, lstItems)",
    advice=Advice(
        fix="Rename the variable to the snake_case name given, without the type prefix.",
        dismiss=(
            "Dismiss only if the name mirrors an external schema field (an API key, a"
            " database column) that must keep its spelling."
        ),
    ),
)
def check_hungarian_notation(ctx: AnalysisContext) -> list[Finding]:
    """Find variables using Hungarian notation prefixes."""
    findings = []
    seen: set[tuple[str, int]] = set()  # (file, line) dedup

    for filepath, tree in source_trees(ctx):
        for node in ast.walk(tree):
            names_to_check: list[tuple[str, int]] = []

            # Assignment targets
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names_to_check.append((target.id, node.lineno))

            # Annotated assignment targets
            if isinstance(node, ast.AnnAssign) and (bound := bare_target(node)) is not None:
                names_to_check.append((bound.id, node.lineno))

            # Function parameters
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                    names_to_check.append((arg.arg, node.lineno))

            # For-loop targets
            if isinstance(node, ast.For) and (bound := bare_target(node)) is not None:
                names_to_check.append((bound.id, node.lineno))

            for name, line in names_to_check:
                # Skip UPPER_CASE names
                if name.isupper() or name.upper() == name:
                    continue
                m = _HUNGARIAN_RE.match(name)
                if m:
                    key = (str(filepath), line)
                    if key not in seen:
                        seen.add(key)
                        snake = _to_snake_case(name)
                        findings.append(
                            Finding(
                                file=str(filepath),
                                line=line,
                                check="hungarian-notation",
                                message=(f"{name} uses Hungarian notation (snake_case: {snake})"),
                                severity=Severity.LOW,
                            )
                        )

    return findings


# --- plaintext-passwords ---


def _secret_operand_name(node: ast.expr) -> str | None:
    """Return the secret-shaped name an operand refers to, else ``None``.

    Only a bare name or an attribute access carries a name this check can
    judge; a call, a literal or a subscript does not.

    The name test is ``helpers.is_secret_name`` — the same vocabulary
    ``secrets-in-logs`` uses, deliberately shared. The metadata-suffix
    exemption is right for equality too: comparing a secret's id, name or
    scope (``token_id == x``, ``api_key_name == y``) is a comparison of
    metadata, not of a secret, and there is nothing timing-attackable about
    it. The wider vocabulary is the other half of the trade — ``pwd``,
    ``passphrase``, ``cvv``, ``ssn``, ``credit_card`` and the rest were real
    gaps in a HIGH-severity check that only knew six names.
    """
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    else:
        return None
    return name if is_secret_name(name) else None


def _plaintext_comparison(node: ast.Compare) -> tuple[str, str] | None:
    """Return ``(name, operator)`` for a ==/!= against a secret-shaped operand.

    At most one result per ``Compare`` node, so a chained comparison
    reports once. Every operand of the comparison is considered, not only
    the two flanking the operator.
    """
    operands = [node.left, *node.comparators]
    for op in node.ops:
        if not isinstance(op, (ast.Eq, ast.NotEq)):
            continue
        for operand in operands:
            name = _secret_operand_name(operand)
            if name is not None:
                return name, "==" if isinstance(op, ast.Eq) else "!="
    return None


@check(
    "plaintext-passwords",
    severity=Severity.HIGH,
    description="Equality comparison on password/secret/token variables",
    advice=Advice(
        fix=(
            "Compare with hmac.compare_digest(), or compare hashes, so the comparison takes"
            " constant time."
        ),
        dismiss=(
            "Dismiss only if the value is not a secret, just a label sharing the name; then"
            " rename it so the name says what it is."
        ),
    ),
)
def check_plaintext_passwords(ctx: AnalysisContext) -> list[Finding]:
    """Find plaintext password comparisons using == or !=."""
    findings = []

    for filepath, tree in source_trees(ctx):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            match = _plaintext_comparison(node)
            if match is None:
                continue
            var_name, op_str = match
            findings.append(
                Finding(
                    file=str(filepath),
                    line=node.lineno,
                    check="plaintext-passwords",
                    message=(f"{var_name} compared with {op_str} — possible plaintext comparison"),
                    severity=Severity.HIGH,
                )
            )

    return findings


# --- getattr-strings ---

# One string name is reported across files once it has this many getattr/hasattr
# uses, in this many files.
GETATTR_SPREAD_MIN = 3


@check(
    "getattr-strings",
    severity=Severity.MEDIUM,
    description="getattr(obj, 'literal') without default or hasattr(obj, 'literal')",
    advice=Advice(
        fix=(
            "Write getattr(obj, 'x') without a default as obj.x, and replace hasattr checks"
            " with an isinstance() or Protocol check on the type that has the attribute."
        ),
        dismiss=(
            "Dismiss only for hasattr on objects from outside the project whose classes"
            " cannot be imported to check against."
        ),
    ),
)
def check_getattr_strings(ctx: AnalysisContext) -> list[Finding]:
    """Find stringly-typed getattr/hasattr calls with literal strings."""
    findings = []
    # For cross-file aggregation
    cross_file: dict[str, list[tuple[str, int]]] = {}

    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue

            func_name = node.func.id

            if func_name == "getattr" and len(node.args) >= 2:
                attr_arg = node.args[1]
                if not isinstance(attr_arg, ast.Constant) or not isinstance(attr_arg.value, str):
                    continue
                # Skip if 3rd arg or default= keyword provided
                has_default = len(node.args) >= 3 or any(
                    kw.arg == "default" for kw in node.keywords
                )
                attr_name = attr_arg.value
                # Track for cross-file
                cross_file.setdefault(attr_name, []).append((str(filepath), node.lineno))
                if has_default:
                    continue
                if not ctx.roles.is_test(filepath):
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="getattr-strings",
                            message=(f"getattr(obj, '{attr_name}') without default"),
                            severity=Severity.MEDIUM,
                        )
                    )

            elif func_name == "hasattr" and len(node.args) >= 2:
                attr_arg = node.args[1]
                if not isinstance(attr_arg, ast.Constant) or not isinstance(attr_arg.value, str):
                    continue
                # hasattr(self, ...) is legitimate introspection (e.g. Django
                # reverse OneToOneField checks), not stringly-typed access
                first_arg = node.args[0]
                if is_bare_name(first_arg, "self"):
                    continue
                attr_name = attr_arg.value
                cross_file.setdefault(attr_name, []).append((str(filepath), node.lineno))
                if not ctx.roles.is_test(filepath):
                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="getattr-strings",
                            message=(
                                f"hasattr(obj, '{attr_name}') — stringly-typed attribute check"
                            ),
                            severity=Severity.MEDIUM,
                        )
                    )

    # Cross-file shotgun surgery check
    for attr_name, locs in cross_file.items():
        files = {loc[0] for loc in locs}
        if len(locs) >= GETATTR_SPREAD_MIN and len(files) >= GETATTR_SPREAD_MIN:
            sites = [Site(f, line) for f, line in sorted(locs)]
            findings.append(
                multi_site_finding(
                    "getattr-strings",
                    Severity.MEDIUM,
                    sites,
                    partial(_getattr_spread_message, attr_name, sites),
                    anchor=Site(*locs[0]),
                )
            )

    return findings


def _getattr_spread_message(
    attr_name: str, sites: list[Site], indices: tuple[int, ...]
) -> str | None:
    """getattr-strings' cross-file message over the sites at ``indices``;
    None below the location or file floor."""
    shown = [sites[index] for index in indices]
    files = {site.file for site in shown}
    if len(shown) < GETATTR_SPREAD_MIN or len(files) < GETATTR_SPREAD_MIN:
        return None
    loc_strs = [f"{site.file}:{site.line}" for site in shown[:5]]
    return (
        f"'{attr_name}' used in getattr/hasattr across"
        f" {len(shown)} locations in {len(files)} files"
        f" ({', '.join(loc_strs)}) — shotgun surgery risk"
    )


# --- late-binding-closures ---

_LOOP_STMTS = (ast.For, ast.AsyncFor)
_CLOSURE_STMTS = (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)
_CLOSURE_NODE = ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef


def _get_loop_var_names(loop: ast.For | ast.AsyncFor) -> set[str]:
    """Extract variable names from a for-loop target."""
    names: set[str] = set()
    target = loop.target
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, ast.Tuple):
        for elt in target.elts:
            if isinstance(elt, ast.Name):
                names.add(elt.id)
    return names


def _bound_param_names(args: ast.arguments) -> set[str]:
    """Names a signature binds: positional, keyword-only, ``*args``, ``**kwargs``."""
    names = {a.arg for a in args.args + args.posonlyargs + args.kwonlyargs}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _default_captured_names(args: ast.arguments) -> set[str]:
    """Outer names captured *by value* through a default — the ``x=x`` idiom."""
    return {d.id for d in args.defaults + args.kw_defaults if isinstance(d, ast.Name)}


def _assigned_names_in(nodes: Iterable[ast.AST]) -> set[str]:
    """Simple names assigned anywhere under ``nodes`` (``Assign`` and ``AnnAssign``)."""
    names: set[str] = set()
    for start in nodes:
        for node in ast.walk(start):
            if isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and (target := bare_target(node)) is not None:
                names.add(target.id)
    return names


def _loaded_names_in(nodes: Iterable[ast.AST]) -> set[str]:
    """Names read (``Load`` context) anywhere under ``nodes``."""
    return {
        node.id
        for start in nodes
        for node in ast.walk(start)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _find_free_names_in_func(
    func: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> set[str]:
    """Find names read (Load) inside a function/lambda that aren't its own params or locals."""
    body: list[ast.AST] = [func.body] if isinstance(func, ast.Lambda) else list(func.body)
    return (
        _loaded_names_in(body)
        - _bound_param_names(func.args)
        - _assigned_names_in(body)
        - _default_captured_names(func.args)
    )


def _capture_message(closure: _CLOSURE_NODE, var_str: str) -> str:
    """Phrase the finding for a lambda or a def."""
    subject = "lambda" if isinstance(closure, ast.Lambda) else f"{closure.name}()"
    return (
        f"{subject} captures loop variable {var_str} by reference — all closures"
        f" will see the final value"
    )


def _loop_capture_findings(filepath: str, loop: ast.For | ast.AsyncFor) -> list[Finding]:
    """Findings for every closure inside ``loop`` that reads the loop variable."""
    loop_vars = _get_loop_var_names(loop)
    if not loop_vars:
        return []

    findings: list[Finding] = []
    for child in ast.walk(loop):
        if child is loop or not isinstance(child, _CLOSURE_STMTS):
            continue
        captured = loop_vars & _find_free_names_in_func(child)
        if not captured:
            continue
        findings.append(
            Finding(
                file=filepath,
                line=child.lineno,
                check="late-binding-closures",
                message=_capture_message(child, ", ".join(sorted(captured))),
                severity=Severity.HIGH,
            )
        )
    return findings


@check(
    "late-binding-closures",
    severity=Severity.HIGH,
    description="Lambda/closure in loop captures loop variable by reference, not value",
    advice=Advice(
        fix=(
            "Bind the loop variable when the closure is made: a default argument"
            " (name=name) or functools.partial."
        ),
        dismiss=(
            "Dismiss only if every closure is called before the loop advances, inside the"
            " same iteration."
        ),
    ),
)
def check_late_binding_closures(ctx: AnalysisContext) -> list[Finding]:
    """Find closures in loops that capture the loop variable by late binding."""
    findings = []

    for filepath, tree in source_trees(ctx):
        for node in ast.walk(tree):
            if isinstance(node, _LOOP_STMTS):
                findings.extend(_loop_capture_findings(str(filepath), node))

    return findings


# --- numbered-variables ---

_NUMBERED_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z_]*?)(\d+)$")


def _collect_numbered_series(
    assignments: list[tuple[str, int]],
) -> dict[str, list[int]]:
    """Group assigned names of the form <stem><int> by stem.

    Returns {stem: [numbers...]} for stems with 4+ distinct numbers where
    the numbers form a run starting at 0 or 1 (item1..item10, not
    utf8/sha256/x509 one-offs).
    """
    by_stem: dict[str, set[int]] = {}
    for name, _lineno in assignments:
        m = _NUMBERED_NAME_RE.match(name)
        if not m:
            continue
        stem, num = m.group(1), int(m.group(2))
        # Require the stem to end in a letter (skip "v1"/"h2" style) and be
        # more than one char, so single-letter + digit names don't trip it.
        if len(stem) < 2 or stem.endswith("_"):
            continue
        by_stem.setdefault(stem, set()).add(num)

    series: dict[str, list[int]] = {}
    for stem, nums in by_stem.items():
        if len(nums) < 4:
            continue
        low = min(nums)
        if low not in {0, 1}:
            continue
        # The numbers should be mostly contiguous (a real series), not
        # scattered like account_2024, account_2025.
        span = max(nums) - low + 1
        if len(nums) >= span * 0.7:
            series[stem] = sorted(nums)
    return series


def _numbered_scopes(tree: ast.Module) -> list[tuple[str, list[tuple[str, int]]]]:
    """Name assignments per scope: module top level, then each function body."""
    scopes = [("module scope", _name_assignments(ast.iter_child_nodes(tree)))]
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((f"{func.name}()", _name_assignments(ast.walk(func))))
    return scopes


def _name_assignments(nodes: Iterable[ast.AST]) -> list[tuple[str, int]]:
    """``(name, lineno)`` for every simple-name assignment target among ``nodes``."""
    assignments: list[tuple[str, int]] = []
    for node in nodes:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                assignments.append((target.id, node.lineno))
    return assignments


@check(
    "numbered-variables",
    severity=Severity.LOW,
    description="Series of variables like item1, item2, ... that should be a list",
    advice=Advice(
        fix="Replace the numbered names with one list or dict, and loop over it.",
        dismiss=(
            "Dismiss only if the numbers are part of domain names that do not form a"
            " sequence, such as coordinates x1 and x2 or protocol fields."
        ),
    ),
)
def check_numbered_variables(ctx: AnalysisContext) -> list[Finding]:
    """Find runs of numbered variables (``item1``…``item10``).

    A series of same-stem, sequentially-numbered names is a list or dict
    spelled out by hand: every operation over them is copy-pasted, and
    adding an element means editing every site. Checks module scope and
    each function body separately.
    """
    findings = []

    for filepath, tree in source_trees(ctx):
        for scope_name, assignments in _numbered_scopes(tree):
            if not assignments:
                continue
            first_line = {name: line for name, line in reversed(assignments)}
            for stem, nums in _collect_numbered_series(assignments).items():
                anchor = min(first_line[f"{stem}{n}"] for n in nums if f"{stem}{n}" in first_line)
                findings.append(
                    Finding(
                        file=str(filepath),
                        line=anchor,
                        check="numbered-variables",
                        message=(
                            f"{stem}1..{stem}{max(nums)} ({len(nums)} numbered"
                            f" variables in {scope_name})"
                        ),
                        severity=Severity.LOW,
                    )
                )

    return findings
