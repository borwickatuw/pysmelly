"""pysmelly - AST-based Python code smell detector."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pysmelly")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
