"""Code smell checks.

Import all check modules to trigger registration via @check decorator.
"""

from pysmelly.checks import callers, imports, patterns, structure  # noqa: F401
