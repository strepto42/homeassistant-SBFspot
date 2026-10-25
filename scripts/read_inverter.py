#!/usr/bin/env python3
"""Read an SMA Bluetooth inverter through the ESP32 bridge, from the command line.

Use this to test the whole chain (bridge firmware, Bluetooth link, protocol,
password) on real hardware before installing the Home Assistant integration::

    python scripts/read_inverter.py --bridge 192.168.1.50 --bt 00:80:25:AA:BB:CC
    python scripts/read_inverter.py --bridge esp32.local:9900 --bt 00:80:25:AA:BB:CC -v

``-v`` turns on DEBUG logging, which hex-dumps every Bluetooth frame sent and
received (the logon password is masked).  The protocol client is imported straight
from ``custom_components/sbfspot/smabt`` without importing Home Assistant.

Exit status: 0 on success, 1 if the inverter could not be read, 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

PACKAGE_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "sbfspot" / "smabt"
)
DEFAULT_PORT = 9900
DEFAULT_PASSWORD = "0000"


def load_smabt() -> ModuleType:
    """Import the protocol package standalone (no parent package, no Home Assistant)."""
    spec = importlib.util.spec_from_file_location(
        "smabt",
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot find the smabt package in {PACKAGE_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["smabt"] = module
    spec.loader.exec_module(module)
    return module


def parse_bridge(value: str) -> tuple[str, int]:
    """Split ``HOST``, ``HOST:PORT`` or ``[IPV6]:PORT`` into host and port."""
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else DEFAULT_PORT
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        return host, int(port)
    return value, DEFAULT_PORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read an SMA Bluetooth inverter via the ESP32 bridge."
    )
    parser.add_argument(
        "--bridge",
        required=True,
        metavar="HOST[:PORT]",
        help=f"address of the ESP32 bridge (default port {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--bt",
        required=True,
        metavar="AA:BB:CC:DD:EE:FF",
        help="Bluetooth address of the inverter",
    )
    parser.add_argument(
        "--password",
        help="inverter user password (prompted for if omitted; SMA default 0000)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="seconds to wait for each response from the inverter (default 10)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG logging: hex-dump every frame",
    )
    return parser


# (label, SpotData attribute, unit, decimals)
SPOT_ROWS = (
    ("AC power, total", "pac_total", "W", 0),
    ("AC power L1", "pac1", "W", 0),
    ("AC power L2", "pac2", "W", 0),
    ("AC power L3", "pac3", "W", 0),
    ("AC voltage L1", "uac1", "V", 2),
    ("AC voltage L2", "uac2", "V", 2),
    ("AC voltage L3", "uac3", "V", 2),
    ("AC current L1", "iac1", "A", 3),
    ("AC current L2", "iac2", "A", 3),
    ("AC current L3", "iac3", "A", 3),
    ("DC power MPPT 1", "pdc1", "W", 0),
    ("DC power MPPT 2", "pdc2", "W", 0),
    ("DC voltage MPPT 1", "udc1", "V", 2),
    ("DC voltage MPPT 2", "udc2", "V", 2),
    ("DC current MPPT 1", "idc1", "A", 3),
    ("DC current MPPT 2", "idc2", "A", 3),
    ("Grid frequency", "grid_frequency", "Hz", 2),
    ("Energy today", "e_today", "kWh", 3),
    ("Energy total", "e_total", "kWh", 3),
    ("Operating time", "operating_time", "h", 2),
    ("Feed-in time", "feed_in_time", "h", 2),
    ("Temperature", "temperature", "°C", 1),
    ("Bluetooth signal", "bt_signal", "%", 1),
    ("Status", "status", "", 0),
    ("Grid relay", "grid_relay", "", 0),
)


def format_value(value: object, unit: str, decimals: int) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{decimals}f} {unit}".rstrip()
    return str(value)


def render(data: object) -> str:
    """Human-readable report of an ``InverterData``."""
    info = data.info  # type: ignore[attr-defined]
    spot = data.spot  # type: ignore[attr-defined]
    lines = ["Device"]
    for label, value in (
        ("Name", info.name),
        ("Type", info.device_type),
        ("Class", info.device_class),
        ("Software", info.sw_version),
        ("Serial", info.serial),
        ("SUSyID", info.susy_id),
        ("Bluetooth", info.bt_address),
    ):
        lines.append(f"  {label:<18} {'-' if value is None else value}")
    lines.append("Spot values  (- = not reported by the inverter)")
    for label, attr, unit, decimals in SPOT_ROWS:
        lines.append(
            f"  {label:<18} {format_value(getattr(spot, attr), unit, decimals)}"
        )
    return "\n".join(lines)


async def run(
    smabt: ModuleType, transport: object, password: str, per_response: float
) -> int:
    """Read the inverter and print the report; returns the process exit status."""
    try:
        data = await smabt.async_read_inverter(
            transport, password, timeout=per_response
        )
    except smabt.SmaError as err:
        print(f"{type(err).__name__}: {err}", file=sys.stderr)
        return 1
    print(render(data))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Never crash on a console that cannot show the degree sign.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    smabt = load_smabt()
    try:
        host, port = parse_bridge(args.bridge)
        transport = smabt.BridgeTransport(
            host, port, args.bt, connect_timeout=args.timeout, bt_timeout=30.0
        )
    except ValueError as err:  # malformed port or Bluetooth address
        print(f"error: {err}", file=sys.stderr)
        return 2
    password = args.password
    if password is None:
        password = getpass.getpass(f"Inverter password [{DEFAULT_PASSWORD}]: ")
        password = password or DEFAULT_PASSWORD
    try:
        return asyncio.run(run(smabt, transport, password, args.timeout))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
