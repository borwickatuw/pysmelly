"""Inheritance checks — cross-file class-hierarchy smells.

Refused bequest, deep inheritance chains, multiple-inheritance method
collisions. All build on a shared project-class hierarchy index.
"""

from __future__ import annotations

import ast
from pathlib import Path

from pysmelly.checks.helpers import is_test_file
from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check


def _build_class_index(
    all_trees: dict[Path, ast.Module],
) -> tuple[dict[str, ast.ClassDef], dict[str, str], set[str]]:
    """Index project classes by name.

    Returns (index, file_of, ambiguous) where index maps name -> ClassDef,
    file_of maps name -> file path, and ambiguous is the set of names
    defined by more than one class (skipped — can't resolve reliably).
    """
    index: dict[str, ast.ClassDef] = {}
    file_of: dict[str, str] = {}
    ambiguous: set[str] = set()
    for filepath, tree in all_trees.items():
        if is_test_file(filepath):
            continue
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                if node.name in index:
                    ambiguous.add(node.name)
                index[node.name] = node
                file_of[node.name] = str(filepath)
    return index, file_of, ambiguous


def _base_names(node: ast.ClassDef) -> list[str]:
    names = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _project_base_defs(
    node: ast.ClassDef, index: dict[str, ast.ClassDef], ambiguous: set[str]
) -> list[ast.ClassDef]:
    result = []
    for name in _base_names(node):
        if name in ambiguous:
            continue
        base = index.get(name)
        if base is not None and base is not node:
            result.append(base)
    return result


def _method_names(node: ast.ClassDef) -> set[str]:
    return {
        item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _ancestors(
    node: ast.ClassDef,
    index: dict[str, ast.ClassDef],
    ambiguous: set[str],
    seen: set[int] | None = None,
) -> list[ast.ClassDef]:
    """Project-class ancestors of node, depth-first, cycle-safe."""
    if seen is None:
        seen = set()
    result = []
    for base in _project_base_defs(node, index, ambiguous):
        if id(base) in seen:
            continue
        seen.add(id(base))
        result.append(base)
        result.extend(_ancestors(base, index, ambiguous, seen))
    return result


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_trivial_override_body(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    """If the method body is a pure stub, return a label ('pass' or
    'raise NotImplementedError'); else None. A leading docstring is
    ignored."""
    body = list(method.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    if len(body) != 1:
        return None
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return "pass"
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        if stmt.value.value is Ellipsis:
            return "pass"
    if isinstance(stmt, ast.Raise):
        exc = stmt.exc
        name = None
        if isinstance(exc, ast.Name):
            name = exc.id
        elif isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
            name = exc.func.id
        if name == "NotImplementedError":
            return "raise NotImplementedError"
    return None


@check(
    "refused-bequest",
    severity=Severity.MEDIUM,
    description="Subclass that stubs out most of the methods it inherits",
)
def check_refused_bequest(ctx: AnalysisContext) -> list[Finding]:
    """Find subclasses that reject most of what they inherit.

    A subclass whose overrides are mostly ``pass`` or
    ``raise NotImplementedError`` isn't specializing its base — it's
    declaring the base is the wrong parent (a skateboard is not a
    vehicle). The inheritance buys nothing and misleads callers who
    expect the base's contract.
    """
    findings = []
    index, file_of, ambiguous = _build_class_index(ctx.all_trees)

    for name, node in index.items():
        ancestors = _ancestors(node, index, ambiguous)
        if not ancestors:
            continue
        ancestor_methods: set[str] = set()
        for anc in ancestors:
            ancestor_methods |= _method_names(anc)

        overrides = [
            item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name in ancestor_methods
            and not _is_dunder(item.name)
        ]
        if len(overrides) < 3:
            continue
        trivial = [(m.name, _is_trivial_override_body(m)) for m in overrides]
        stubbed = [(n, label) for n, label in trivial if label is not None]
        if len(stubbed) < 3 or len(stubbed) / len(overrides) < 0.5:
            continue

        base_name = _base_names(node)[0] if _base_names(node) else "base"
        stub_names = ", ".join(n for n, _ in stubbed[:5])
        if len(stubbed) > 5:
            stub_names += ", ..."
        findings.append(
            Finding(
                file=file_of[name],
                line=node.lineno,
                check="refused-bequest",
                message=(
                    f"{name} stubs out {len(stubbed)} of {len(overrides)}"
                    f" methods it inherits from {base_name}"
                    f" ({stub_names}) — {base_name} is likely the wrong"
                    f" base; prefer composition"
                ),
                severity=Severity.MEDIUM,
            )
        )

    return findings


def _is_subclassed_by_project(
    name: str, index: dict[str, ast.ClassDef], ambiguous: set[str]
) -> bool:
    for other in index.values():
        if other.name == name:
            continue
        if name in _base_names(other) and name not in ambiguous:
            return True
    return False


@check(
    "deep-inheritance",
    severity=Severity.LOW,
    description="Project inheritance chain 5+ classes deep",
)
def check_deep_inheritance(ctx: AnalysisContext) -> list[Finding]:
    """Find deep in-codebase inheritance chains.

    Each level adds behavior scattered across a class the reader must
    hold in their head at once (the yo-yo problem: understanding one
    method means bouncing up and down five classes). Reported once per
    chain, at the deepest (leaf) class. Framework base classes don't
    count — only links between classes defined in this codebase.
    """
    findings = []
    index, file_of, ambiguous = _build_class_index(ctx.all_trees)
    min_depth = 5

    def longest_path(node: ast.ClassDef, seen: frozenset[int] = frozenset()) -> int:
        if id(node) in seen:
            return 0
        bases = _project_base_defs(node, index, ambiguous)
        if not bases:
            return 1
        return 1 + max(longest_path(b, seen | {id(node)}) for b in bases)

    for name, node in index.items():
        if _is_subclassed_by_project(name, index, ambiguous):
            continue  # not a leaf — its subclass will report the chain
        chain_len = longest_path(node)
        if chain_len < min_depth:
            continue
        # Build the longest root-to-leaf chain for the message.
        chain = [name]
        current = node
        while True:
            bases = _project_base_defs(current, index, ambiguous)
            if not bases:
                break
            deepest = max(bases, key=longest_path)
            chain.append(deepest.name)
            current = deepest
        chain_str = " -> ".join(reversed(chain))
        findings.append(
            Finding(
                file=file_of[name],
                line=node.lineno,
                check="deep-inheritance",
                message=(
                    f"{name} sits at the bottom of a {chain_len}-class"
                    f" inheritance chain ({chain_str}) — behavior is"
                    f" scattered across levels; consider composition"
                ),
                severity=Severity.LOW,
            )
        )

    return findings


def _has_incomparable_pair(classes: list[ast.ClassDef], anc_ids: dict[int, set[int]]) -> bool:
    """True if any two classes are incomparable — neither an ancestor of
    the other — meaning they are genuinely competing definitions rather
    than an override down a single linear chain."""
    for i, c1 in enumerate(classes):
        for c2 in classes[i + 1 :]:
            if id(c2) not in anc_ids.get(id(c1), set()) and id(c1) not in anc_ids.get(
                id(c2), set()
            ):
                return True
    return False


@check(
    "mi-method-collision",
    severity=Severity.LOW,
    description="Multiple bases define the same method; subclass picks via MRO",
)
def check_mi_method_collision(ctx: AnalysisContext) -> list[Finding]:
    """Find multiple-inheritance classes where 2+ bases define the same
    method and the subclass doesn't override it.

    Which implementation wins is decided silently by the MRO, not by the
    author. When the base implementations differ, one branch's behavior
    is quietly dropped — an easy source of "why isn't my override
    running" bugs. Reported at LOW: cooperative super() chains make this
    correct often enough to warrant a look, not an alarm.

    A method defined only by a shared diamond ancestor is NOT a collision
    — both branches inherit the same definition, so the MRO is
    unambiguous. Only genuinely competing definitions (from classes where
    neither is an ancestor of the other) count.
    """
    findings = []
    index, file_of, ambiguous = _build_class_index(ctx.all_trees)

    for name, node in index.items():
        bases = _project_base_defs(node, index, ambiguous)
        if len(bases) < 2:
            continue
        own_methods = _method_names(node)

        # The ancestor closure: every project class this one inherits from.
        closure = _ancestors(node, index, ambiguous)
        anc_ids: dict[int, set[int]] = {
            id(c): {id(a) for a in _ancestors(c, index, ambiguous)} for c in closure
        }

        # method name -> classes in the closure that define it
        definers: dict[str, list[ast.ClassDef]] = {}
        for cls in closure:
            for m in _method_names(cls):
                if not _is_dunder(m):
                    definers.setdefault(m, []).append(cls)

        collisions = sorted(
            m
            for m, classes in definers.items()
            if m not in own_methods
            and len(classes) >= 2
            and _has_incomparable_pair(classes, anc_ids)
        )
        if not collisions:
            continue

        base_list = ", ".join(_base_names(node))
        shown = ", ".join(collisions[:5])
        if len(collisions) > 5:
            shown += ", ..."
        findings.append(
            Finding(
                file=file_of[name],
                line=node.lineno,
                check="mi-method-collision",
                message=(
                    f"{name}({base_list}) inherits {len(collisions)}"
                    f" method(s) defined in multiple bases ({shown}) and"
                    f" overrides none — the MRO silently picks one;"
                    f" make the choice explicit"
                ),
                severity=Severity.LOW,
            )
        )

    return findings
