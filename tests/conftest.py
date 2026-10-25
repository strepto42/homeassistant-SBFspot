"""Shared test setup.

``custom_components/sbfspot/__init__.py`` imports Home Assistant, which is not
installed on every machine (notably native Windows).  The SMA Bluetooth client
(``smabt``) is pure standard library, so it is loaded here *standalone*: as a
top-level package named ``smabt`` straight from its directory, bypassing the
parent package.  Tests then simply ``import smabt``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SMABT_DIR = REPO_ROOT / "custom_components" / "sbfspot" / "smabt"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
MODULE_NAME = "smabt"


def load_smabt(name: str = MODULE_NAME) -> ModuleType:
    """Import ``custom_components/sbfspot/smabt`` as top-level package *name*."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        SMABT_DIR / "__init__.py",
        submodule_search_locations=[str(SMABT_DIR)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


load_smabt()


@pytest.fixture(scope="session")
def smabt() -> ModuleType:
    """The standalone-loaded ``smabt`` package."""
    return load_smabt()
