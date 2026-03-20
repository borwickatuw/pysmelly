"""Shared test helpers for pysmelly tests."""

import ast
import subprocess
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


@pytest.fixture
def git_repo(tmp_path):
    """Temp git repo for history check tests."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    return tmp_path
