"""Import-related checks — compatibility shims."""

from __future__ import annotations

import ast

from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check


@check(
    "compat-shims",
    severity=Severity.HIGH,
    description="try/except ImportError patterns from old Python support",
)
def check_compat_shims(ctx: AnalysisContext) -> list[Finding]:
    """Find try/except ImportError patterns (compatibility shims).

    These are often left over from supporting older Python versions
    that the project no longer targets per requires-python.
    """
    findings = []

    for filepath, tree in ctx.all_trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not node.body:
                continue
            if not isinstance(node.body[0], (ast.Import, ast.ImportFrom)):
                continue

            for handler in node.handlers:
                if handler.type is None:
                    continue
                if isinstance(handler.type, ast.Name) and handler.type.id in (
                    "ImportError",
                    "ModuleNotFoundError",
                ):
                    imp = node.body[0]
                    if isinstance(imp, ast.Import):
                        mod_name = imp.names[0].name
                    elif isinstance(imp, ast.ImportFrom):
                        mod_name = imp.module or ""
                    else:
                        mod_name = "?"

                    findings.append(
                        Finding(
                            file=str(filepath),
                            line=node.lineno,
                            check="compat-shims",
                            message=(
                                f"compatibility shim for '{mod_name}' — "
                                f"check if requires-python still needs this"
                            ),
                            severity=Severity.HIGH,
                        )
                    )

    return findings
