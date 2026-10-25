"""Guards for the ``smabt`` package: standard library only, relative imports,
no Home Assistant, no blocking I/O."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from .conftest import SMABT_DIR

MODULES = sorted(SMABT_DIR.glob("*.py"))
BLOCKING_MODULES = {
    "threading",
    "concurrent",
    "subprocess",
    "socket",
    "requests",
    "urllib",
}


def imports_of(path: Path) -> list[ast.Import | ast.ImportFrom]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]


def test_the_package_is_not_empty() -> None:
    names = {path.name for path in MODULES}
    assert {"__init__.py", "client.py", "frame.py", "parse.py", "transport.py"} <= names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_homeassistant_imports(path: Path) -> None:
    for node in imports_of(path):
        names = (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
        )
        for name in names:
            assert name.split(".")[0] != "homeassistant", f"{path.name}:{node.lineno}"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_intra_package_imports_are_relative(path: Path) -> None:
    for node in imports_of(path):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            continue
        names = (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
        )
        for name in names:
            top = name.split(".")[0]
            assert top not in {"smabt", "custom_components", "sbfspot"}, (
                f"{path.name}:{node.lineno} imports {name!r} absolutely"
            )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_only_the_standard_library_is_imported(path: Path) -> None:
    for node in imports_of(path):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue
            names = [node.module or ""]
        else:
            names = [a.name for a in node.names]
        for name in names:
            top = name.split(".")[0]
            assert top in sys.stdlib_module_names, (
                f"{path.name}:{node.lineno} imports {name!r}"
            )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_threads_or_blocking_io(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in imports_of(path):
        names = (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
        )
        for name in names:
            assert name.split(".")[0] not in BLOCKING_MODULES, (
                f"{path.name}:{node.lineno}"
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                assert (func.value.id, func.attr) != ("time", "sleep"), (
                    f"{path.name}:{node.lineno}"
                )
            assert not (isinstance(func, ast.Name) and func.id == "open"), (
                f"{path.name}:{node.lineno}"
            )


def test_modules_use_from_future_annotations() -> None:
    for path in MODULES:
        text = path.read_text(encoding="utf-8")
        assert "from __future__ import annotations" in text, path.name
