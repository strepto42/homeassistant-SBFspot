"""HACS metadata guard: hacs.json and the brand images must stay valid."""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_HACS = _ROOT / "hacs.json"
_BRAND = _ROOT / "custom_components" / "sbfspot" / "brand"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _hacs() -> dict:
    return json.loads(_HACS.read_text(encoding="utf-8"))


def test_hacs_json_valid() -> None:
    assert _hacs().get("name"), "hacs.json must declare a name"


def test_hacs_integration_lives_in_custom_components() -> None:
    assert _hacs().get("content_in_root") is False
    assert (_ROOT / "custom_components" / "sbfspot" / "manifest.json").is_file()


def test_hacs_min_homeassistant_is_versioned() -> None:
    min_ha = _hacs().get("homeassistant")
    assert min_ha, "hacs.json should pin a minimum homeassistant version"
    assert re.match(r"^\d+\.\d+", str(min_ha)), f"unparseable HA version: {min_ha}"


def _png_size(path: Path) -> tuple[int, int, int]:
    """Return (width, height, colour type) from the IHDR chunk."""
    data = path.read_bytes()
    assert data[:8] == _PNG_SIGNATURE, f"{path.name} is not a PNG"
    assert data[12:16] == b"IHDR"
    width, height, _depth, colour_type = struct.unpack(">IIBB", data[16:26])
    return width, height, colour_type


@pytest.mark.parametrize(
    ("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)], ids=str
)
def test_brand_icons_are_square_png_with_alpha(name: str, size: int) -> None:
    """Home Assistant serves ``brand/icon.png`` and ``icon@2x.png`` directly."""
    width, height, colour_type = _png_size(_BRAND / name)
    assert (width, height) == (size, size)
    assert colour_type == 6, "icon must be RGBA (transparent background)"
