"""Check registry and finding dataclass."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from pysmelly.context import AnalysisContext


class Severity(Enum):
    """How likely this finding represents a real problem."""

    HIGH = "high"  # Act on this or explicitly justify keeping it
    MEDIUM = "medium"  # Review each finding, fix what makes sense
    LOW = "low"  # Informational — skim for surprises


@dataclass
class Finding:
    """A single code smell finding."""

    file: str
    line: int
    check: str
    message: str
    severity: Severity


# Type for check functions
CheckFn = Callable[["AnalysisContext"], list[Finding]]

# Global registry
CHECKS: dict[str, CheckFn] = {}
CHECK_SEVERITY: dict[str, Severity] = {}
CHECK_DESCRIPTIONS: dict[str, str] = {}


def check(name: str, severity: Severity = Severity.MEDIUM, description: str = ""):
    """Register a lint check function by name, severity, and description."""

    def decorator(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        CHECK_SEVERITY[name] = severity
        CHECK_DESCRIPTIONS[name] = description
        return fn

    return decorator
