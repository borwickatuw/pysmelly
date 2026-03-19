"""Code smell checks.

Import all check modules to trigger registration via @check decorator.
"""

from pysmelly.checks import callers, imports, patterns, recommendations, structure  # noqa: F401
