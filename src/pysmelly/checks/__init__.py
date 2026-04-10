"""Code smell checks.

Import all check modules to trigger registration via @check decorator.
"""

from pysmelly.checks import (  # noqa: F401
    architecture,
    callers,
    dead,
    history_bugs,
    history_coupling,
    history_growth,
    history_team,
    imports,
    patterns_control,
    patterns_data,
    patterns_misc,
    patterns_naming,
    recommendations,
    repetition,
    structure,
)
