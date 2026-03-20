"""Code smell checks.

Import all check modules to trigger registration via @check decorator.
"""

from pysmelly.checks import (  # noqa: F401
    architecture,
    callers,
    dead,
    history,
    imports,
    patterns,
    recommendations,
    repetition,
    structure,
)
