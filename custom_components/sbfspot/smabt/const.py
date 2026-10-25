"""Protocol constants for the SMA Bluetooth client.

Ported from SBFspot (https://github.com/SBFspot/SBFspot, CC BY-NC-SA 3.0):
``SBFNet.cpp`` / ``SBFspot.cpp`` (packet layout, application ids), ``Types.h``
(``LriDef``, data types, error codes), ``nan.h`` (NaN sentinels) and the
per-``getInverterDataType`` query ranges in ``getInverterData()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

# ------------------------------ framing

L1_START = 0x7E
"""Start-of-frame marker of every Bluetooth (L1) frame; also delimits L2 packets."""

L1_HEADER_LEN = 18
"""``pkHeader``: SOP(1) length(2) checksum(1) source(6) destination(6) command(2)."""

L2_SIGNATURE = bytes.fromhex("ff036065")
"""``BTH_L2SIGNATURE`` (0x656003FF little endian) right after the L2 start byte."""

L2_HEADER_LEN = 24
"""Bytes from the 'longwords' byte up to and including the packet id."""

ESCAPE_BYTE = 0x7D
ESCAPED_BYTES = frozenset({0x7D, 0x7E, 0x11, 0x12, 0x13})
"""Bytes that ``writeByte()`` escapes as ``0x7D, byte ^ 0x20`` (SBFNet.cpp)."""
ESCAPE_XOR = 0x20

MAX_RESYNC_BYTES = 256
"""Junk bytes tolerated in front of a frame before giving up."""

ADDR_UNKNOWN = b"\xff" * 6
"""``addr_unknown``: broadcast / 'any sender' address."""
ADDR_ZERO = bytes(6)
"""``LocalBTAddress`` before the topology packet has told SBFspot its own address."""

# L1 command codes (``pkHeader.command``)
CMD_L2_LAST = 0x0001
"""Carries an L2 packet; for multi-part packets this is the final fragment."""
CMD_HELLO = 0x0002
CMD_SIGNAL_REQUEST = 0x0003
CMD_SIGNAL_RESPONSE = 0x0004
CMD_TOPOLOGY = 0x0005
CMD_L2_FRAGMENT = 0x0008
"""Non-final fragment of a multi-part L2 packet."""

# ------------------------------ application ids

APP_SUSY_ID = 125
"""``AppSUSyID``: the SUSyID SBFspot announces itself with."""
ANY_SUSY_ID = 0xFFFF
ANY_SERIAL = 0xFFFFFFFF
CTRL_REQUEST = 0xA0
CTRL_REQUEST_SB240 = 0xE0

SUSY_ID_SB240 = 244
"""``SID_SB240``: this device wants control byte 0xE0 instead of 0xA0 in queries."""
DEVICE_CLASS_SOLAR_INVERTER = 8001
"""``DEVICECLASS::SolarInverter`` tag id; the grid relay is only queried for it."""

MIN_PROTOCOL_VERSION = 4
"""``pcktBuf[19] < 4`` -> E_FWVERSION (firmware older than 1.71)."""

# Logon (logonSMAInverter)
USER_GROUP_USER = 0x07
USER_GROUP_INSTALLER = 0x0A
PASSWORD_FIELD_LEN = 12
ENCODE_CHAR_USER = 0x88
ENCODE_CHAR_INSTALLER = 0xBB
LOGON_TIMEOUT = 0x00000384
"""Session timeout field of the logon request ('900 sec?' in SBFspot)."""
LOGON_RESULT_INVALID_PASSWORD = 0x0100

# Command words of the non-query packets
CMD_WORD_IDENTIFY = 0x00000200
CMD_WORD_LOGON = 0xFFFD040C
CMD_WORD_LOGOFF = 0xFFFD010E
LOGOFF_ARGUMENT = 0xFFFFFFFF

# ------------------------------ SMA NaN (nan.h)

NAN_S32 = -0x80000000
NAN_U32 = 0xFFFFFFFF
NAN_S64 = -0x8000000000000000
NAN_U64 = 0xFFFFFFFFFFFFFFFF

# ------------------------------ record types

DT_ULONG = 0
DT_STATUS = 8
DT_STRING = 16
DT_FLOAT = 32
DT_SLONG = 64

TAG_END_OF_LIST = 0x00FFFFFE
"""Attribute tag that terminates a status record's attribute list."""

STATUS_OK = 0
STATUS_LRI_NOT_AVAILABLE = 21
"""``E_LRINOTAVAIL``: requested logical record is not available on this device."""


class Lri(IntEnum):
    """Logical record indexes (``LriDef`` in Types.h) that this client decodes."""

    OPERATION_HEALTH = 0x00214800  # device status (INV_STATUS)
    COOLSYS_TMP_NOM = 0x00237700  # inverter temperature
    DC_MS_WATT = 0x00251E00  # SPOT_PDC1 / SPOT_PDC2
    METERING_TOT_WH_OUT = 0x00260100  # SPOT_ETOTAL
    METERING_DY_WH_OUT = 0x00262200  # SPOT_ETODAY
    GRID_MS_TOT_W = 0x00263F00  # SPOT_PACTOT
    OPERATION_GRI_SW_STT = 0x00416400  # INV_GRIDRELAY
    DC_MS_VOL = 0x00451F00  # SPOT_UDC1 / SPOT_UDC2
    DC_MS_AMP = 0x00452100  # SPOT_IDC1 / SPOT_IDC2
    METERING_TOT_OP_TMS = 0x00462E00  # SPOT_OPERTM
    METERING_TOT_FEED_TMS = 0x00462F00  # SPOT_FEEDTM
    METERING_GRID_MS_TOT_W_OUT = 0x00463600
    METERING_GRID_MS_TOT_W_IN = 0x00463700
    GRID_MS_W_PHS_A = 0x00464000  # SPOT_PAC1
    GRID_MS_W_PHS_B = 0x00464100  # SPOT_PAC2
    GRID_MS_W_PHS_C = 0x00464200  # SPOT_PAC3
    GRID_MS_PH_V_PHS_A = 0x00464800  # SPOT_UAC1
    GRID_MS_PH_V_PHS_B = 0x00464900  # SPOT_UAC2
    GRID_MS_PH_V_PHS_C = 0x00464A00  # SPOT_UAC3
    GRID_MS_A_PHS_A_1 = 0x00465000  # SPOT_IAC1
    GRID_MS_A_PHS_B_1 = 0x00465100  # SPOT_IAC2
    GRID_MS_A_PHS_C_1 = 0x00465200  # SPOT_IAC3
    GRID_MS_A_PHS_A = 0x00465300  # SPOT_IAC1 (alternate LRI)
    GRID_MS_A_PHS_B = 0x00465400  # SPOT_IAC2 (alternate LRI)
    GRID_MS_A_PHS_C = 0x00465500  # SPOT_IAC3 (alternate LRI)
    GRID_MS_HZ = 0x00465700  # SPOT_FREQ
    NAMEPLATE_LOCATION = 0x00821E00  # INV_NAME
    NAMEPLATE_MAIN_MODEL = 0x00821F00  # INV_CLASS
    NAMEPLATE_MODEL = 0x00822000  # INV_TYPE
    NAMEPLATE_PKG_REV = 0x00823400  # INV_SWVER


@dataclass(frozen=True, slots=True)
class Query:
    """One ``getInverterData(command, first, last)`` request."""

    name: str
    command: int
    first: int
    last: int


# The queries SBFspot issues for one poll, in the order of Inverter.cpp
# (``getInverterData(m_inverters, <type>)``), taken from the switch in
# ``getInverterData(InverterData *devList[], enum getInverterDataType type)``.
Q_SOFTWARE_VERSION = Query("software version", 0x58000200, 0x00823400, 0x008234FF)
Q_TYPE_LABEL = Query("type label", 0x58000200, 0x00821E00, 0x008220FF)
Q_METERING_GRID = Query("metering grid power", 0x51000200, 0x00463600, 0x004637FF)
Q_DEVICE_STATUS = Query("device status", 0x51800200, 0x00214800, 0x002148FF)
Q_TEMPERATURE = Query("temperature", 0x52000200, 0x00237700, 0x002377FF)
Q_GRID_RELAY = Query("grid relay status", 0x51800200, 0x00416400, 0x004164FF)
Q_ENERGY = Query("energy production", 0x54000200, 0x00260100, 0x002622FF)
Q_OPERATION_TIME = Query("operation time", 0x54000200, 0x00462E00, 0x00462FFF)
Q_DC_POWER = Query("DC power", 0x53800200, 0x00251E00, 0x00251EFF)
Q_DC_VOLTAGE = Query("DC voltage/current", 0x53800200, 0x00451F00, 0x004521FF)
Q_AC_POWER = Query("AC power", 0x51000200, 0x00464000, 0x004642FF)
Q_AC_VOLTAGE = Query("AC voltage/current", 0x51000200, 0x00464800, 0x004655FF)
Q_AC_TOTAL_POWER = Query("AC total power", 0x51000200, 0x00263F00, 0x00263FFF)
Q_GRID_FREQUENCY = Query("grid frequency", 0x51000200, 0x00465700, 0x004657FF)

DEVICE_INFO_QUERIES = (Q_SOFTWARE_VERSION, Q_TYPE_LABEL)
"""Run first (Inverter.cpp: SoftwareVersion, TypeLabel)."""

SPOT_QUERIES = (
    Q_METERING_GRID,
    Q_DEVICE_STATUS,
    Q_TEMPERATURE,
    Q_GRID_RELAY,
    Q_ENERGY,
    Q_OPERATION_TIME,
    Q_DC_POWER,
    Q_DC_VOLTAGE,
    Q_AC_POWER,
    Q_AC_VOLTAGE,
    Q_AC_TOTAL_POWER,
    Q_GRID_FREQUENCY,
)
"""Run after the device info, in SBFspot's order.

``Q_METERING_GRID`` has no counterpart in :class:`SpotData` (a Sunny Boy answers
status 21, 'LRI not available').  It is kept so the session on the wire is
identical to the one verified against real hardware.  ``Q_GRID_RELAY`` is only
sent for solar inverters (Inverter.cpp: ``DevClass == SolarInverter``).
"""

# ------------------------------ units (SBFspot.h)

SIGNAL_FULL_SCALE = 255.0
"""BT signal: ``pcktBuf[22] * 100.0 / 255.0`` (getBT_SignalStrength)."""
