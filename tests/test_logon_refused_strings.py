"""The ``logon_refused`` config flow error exists, is used, and is explained.

Needs no Home Assistant: the flow and the strings are read from the files.
"""

from __future__ import annotations

import json

import pytest

from .test_sensor_consistency import _PKG, _flow_usage


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json"])
def test_logon_refused_has_a_text_that_says_what_to_do(name: str) -> None:
    text = json.loads((_PKG / name).read_text(encoding="utf-8"))["config"]["error"][
        "logon_refused"
    ]
    assert "refused the logon" in text
    assert "open sessions" in text
    assert "15 minutes" in text
    # It is not the wrong-password text.
    assert (
        text
        != json.loads((_PKG / name).read_text(encoding="utf-8"))["config"]["error"][
            "invalid_auth"
        ]
    )


def test_the_config_flow_can_report_a_refused_logon() -> None:
    assert "logon_refused" in _flow_usage()["config"].errors
