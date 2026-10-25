"""The ``scripts/read_inverter.py`` command line tool."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import smabt

from .capture import Capture
from .fake_bridge import FakeBridge, simulate_inverter_session

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_inverter.py"


@pytest.fixture(scope="module")
def cli() -> ModuleType:
    """The script as a module (without letting it re-import ``smabt``)."""
    spec = importlib.util.spec_from_file_location("read_inverter_cli", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_bridge(cli: ModuleType) -> None:
    assert cli.parse_bridge("192.168.1.50") == ("192.168.1.50", 9900)
    assert cli.parse_bridge("esp32.local:1234") == ("esp32.local", 1234)
    assert cli.parse_bridge("[fe80::1]:9901") == ("fe80::1", 9901)
    assert cli.parse_bridge("[fe80::1]") == ("fe80::1", 9900)
    assert cli.parse_bridge("fe80::1") == ("fe80::1", 9900)


async def test_run_prints_device_and_spot_values(
    cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    capture = Capture.load()
    async with FakeBridge(simulate_inverter_session(capture)) as bridge:
        transport = smabt.BridgeTransport(
            "127.0.0.1", bridge.port, capture.identifiers["inverter_bt_address"]
        )
        status = await cli.run(smabt, transport, "0000", 2.0)

    out = capsys.readouterr().out
    assert status == 0
    for expected in (
        "SB 3000TL-20",
        "Solar Inverters",
        "03.20.01.R",
        "2100000042",
        "AC power, total    622 W",
        "AC voltage L1      228.94 V",
        "Grid frequency     49.97 Hz",
        "Energy total       60854.831 kWh",
        "Status             Ok",
        "Grid relay         Closed",
    ):
        assert expected in out, expected
    assert "AC voltage L2      -" in out  # NaN shows as a dash


async def test_run_reports_errors_with_status_1(
    cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = smabt.BridgeTransport(
        "127.0.0.1", 1, "00:80:25:00:00:01", connect_timeout=1
    )
    assert await cli.run(smabt, transport, "0000", 1.0) == 1
    assert "BridgeUnreachableError" in capsys.readouterr().err


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_script_rejects_a_bad_bluetooth_address() -> None:
    result = run_script(
        "--bridge", "127.0.0.1", "--bt", "nonsense", "--password", "0000"
    )
    assert result.returncode == 2
    assert "Bluetooth address" in result.stderr


def test_script_exits_1_when_the_bridge_is_down() -> None:
    result = run_script(
        "--bridge", "127.0.0.1:1", "--bt", "00:80:25:00:00:01", "--password", "0000"
    )
    assert result.returncode == 1
    assert "BridgeUnreachableError" in result.stderr


async def run_cli_async(*args: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    out, err = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode is not None
    return process.returncode, out.decode(), err.decode()


async def test_script_end_to_end_with_verbose_frame_dump() -> None:
    capture = Capture.load()
    async with FakeBridge(simulate_inverter_session(capture)) as bridge:
        code, out, err = await run_cli_async(
            "-v",
            "--bridge",
            f"127.0.0.1:{bridge.port}",
            "--bt",
            capture.identifiers["inverter_bt_address"],
            "--password",
            "0000",
        )

    assert code == 0, err
    assert "AC power, total    622 W" in out
    assert " -> " in err or "->" in err
    assert "DEBUG" in err
    assert "<password>" in err
    assert "B8 B8 B8 B8 88 88 88 88 88 88 88 88" not in err


async def test_script_is_quiet_without_verbose() -> None:
    capture = Capture.load()
    async with FakeBridge(simulate_inverter_session(capture)) as bridge:
        code, out, err = await run_cli_async(
            "--bridge",
            f"127.0.0.1:{bridge.port}",
            "--bt",
            capture.identifiers["inverter_bt_address"],
            "--password",
            "0000",
        )

    assert code == 0, err
    assert "Spot values" in out
    assert "DEBUG" not in err
