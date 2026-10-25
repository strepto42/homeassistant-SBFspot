"""Manifest / packaging guards.

HACS reads ``version`` to detect updates, hassfest wants the manifest keys in a
fixed order, and the version in ``pyproject.toml`` must not drift away from it.
These tests need no Home Assistant, so they run on native Windows too.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PKG = _ROOT / "custom_components" / "sbfspot"
_MANIFEST = _PKG / "manifest.json"
_PYPROJECT = _ROOT / "pyproject.toml"

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_REQUIRED_KEYS = {
    "domain",
    "name",
    "codeowners",
    "config_flow",
    "dependencies",
    "documentation",
    "integration_type",
    "iot_class",
    "issue_tracker",
    "loggers",
    "requirements",
    "version",
}
_MODULES = (
    "__init__",
    "binary_sensor",
    "config_flow",
    "const",
    "coordinator",
    "diagnostics",
    "entity",
    "sensor",
)


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def test_manifest_domain_matches_folder() -> None:
    assert _manifest()["domain"] == _PKG.name == "sbfspot"


def test_manifest_has_the_required_keys() -> None:
    assert _REQUIRED_KEYS <= set(_manifest())


def test_manifest_keys_are_in_hassfest_order() -> None:
    """hassfest: ``domain`` first, ``name`` second, the rest alphabetical."""
    keys = list(_manifest())
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


def test_manifest_core_fields() -> None:
    m = _manifest()
    assert m["name"] == "SBFspot (SMA Bluetooth)"
    assert m["codeowners"] == ["@strepto42"]
    assert m["config_flow"] is True
    # One inverter behind one bridge: a "device", polled over the LAN.
    assert m["integration_type"] == "device"
    assert m["iot_class"] == "local_polling"
    assert m["dependencies"] == []
    # The SMA Bluetooth client (smabt) is bundled and uses only the stdlib.
    assert m["requirements"] == []
    assert m["loggers"] == ["custom_components.sbfspot"]


def test_manifest_links() -> None:
    m = _manifest()
    assert m["documentation"].startswith(
        "https://github.com/strepto42/homeassistant-SBFspot"
    )
    assert m["issue_tracker"] == (
        "https://github.com/strepto42/homeassistant-SBFspot/issues"
    )


def test_manifest_version_is_semver() -> None:
    assert _SEMVER.match(_manifest()["version"]), "version must be X.Y.Z for HACS"


def test_manifest_version_matches_pyproject() -> None:
    manifest_version = _manifest()["version"]
    text = _PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml must declare a version"
    assert match.group(1) == manifest_version, (
        f"pyproject version {match.group(1)} != manifest version {manifest_version}"
    )


@pytest.mark.parametrize("module", _MODULES)
def test_integration_module_exists(module: str) -> None:
    assert (_PKG / f"{module}.py").is_file()


def test_translations_files_exist() -> None:
    assert (_PKG / "strings.json").is_file()
    assert (_PKG / "translations" / "en.json").is_file()


def _integration_modules() -> list[Path]:
    # smabt is a self-contained library with its own guard tests (test_guard.py).
    return sorted(_PKG.glob("*.py"))


@pytest.mark.parametrize("path", _integration_modules(), ids=lambda p: p.name)
def test_modules_use_relative_imports_for_siblings(path: Path) -> None:
    """``from custom_components.sbfspot.X import Y`` couples the integration to
    its path on disk; Home Assistant convention is ``from .X import Y``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            assert top != "custom_components", f"{path.name}:{node.lineno}"
            assert top != "smabt", f"{path.name}:{node.lineno}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top not in {"custom_components", "smabt"}, (
                    f"{path.name}:{node.lineno}"
                )
