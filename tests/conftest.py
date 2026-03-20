"""Shared test helpers for pysmelly tests."""

import ast
from pathlib import Path

import pytest

from pysmelly.context import AnalysisContext


def parse_code(code: str, filename: str = "test.py") -> AnalysisContext:
    """Parse a string of Python code into an AnalysisContext."""
    return AnalysisContext({Path(filename): ast.parse(code)}, verbose=False)


def parse_files(files: dict[str, str]) -> AnalysisContext:
    """Parse multiple named files into an AnalysisContext."""
    return AnalysisContext(
        {Path(name): ast.parse(code) for name, code in files.items()}, verbose=False
    )


@pytest.fixture
def trees():
    """Factory fixture: call trees.code(...) or trees.files(...) to get AnalysisContext."""
    return type(
        "Trees", (), {"code": staticmethod(parse_code), "files": staticmethod(parse_files)}
    )()
