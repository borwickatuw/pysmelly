"""Shared test helpers for pysmelly tests."""

import ast
from pathlib import Path

import pytest


def parse_code(code: str, filename: str = "test.py") -> dict[Path, ast.Module]:
    """Parse a string of Python code into the all_trees format."""
    return {Path(filename): ast.parse(code)}


def parse_files(files: dict[str, str]) -> dict[Path, ast.Module]:
    """Parse multiple named files into the all_trees format."""
    return {Path(name): ast.parse(code) for name, code in files.items()}


@pytest.fixture
def trees():
    """Factory fixture: call trees.code(...) or trees.files(...) to get all_trees."""
    return type(
        "Trees", (), {"code": staticmethod(parse_code), "files": staticmethod(parse_files)}
    )()
