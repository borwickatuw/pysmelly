"""Code smell checks.

Import all check modules to trigger registration via @check decorator.
"""

from pysmelly.checks import (  # noqa: F401
    callers,
    dead,
    imports,
    patterns,
    recommendations,
    structure,
)
