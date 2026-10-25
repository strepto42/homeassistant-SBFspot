"""Cross-file consistency guards (no Home Assistant needed).

The entity descriptions in ``sensor.py`` / ``binary_sensor.py`` and the flows in
``config_flow.py`` are read through ``ast``; they are checked against
``strings.json`` / ``translations/en.json`` and against the ``smabt`` models:

* every entity ``key`` has a ``translation_key`` with a name in the strings, and
  the strings have no orphans;
* ``strings.json`` and ``translations/en.json`` are identical;
* every error / abort / step / field used by a flow exists in the strings;
* every ``value_fn`` reads a real ``SpotData`` field;
* every cumulative (TOTAL_INCREASING) sensor is covered by the coordinator's
  "never report 0 / never go down" handling.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PKG = _ROOT / "custom_components" / "sbfspot"

#: Everything the config flow must be able to report (see the integration spec).
EXPECTED_FLOW_ERRORS = {
    "bridge_unreachable",
    "bridge_busy",
    "inverter_unreachable",
    "cannot_connect",
    "invalid_auth",
    "protocol_error",
    "invalid_bt_address",
    "unknown",
}
EXPECTED_FLOW_ABORTS = {
    "already_configured",
    "reconfigure_successful",
    "reauth_successful",
    "wrong_device",
}
EXPECTED_STEPS = {"user", "reconfigure", "reauth_confirm", "init"}


# --------------------------------------------------------------------- helpers


def _parse(name: str) -> ast.Module:
    path = _PKG / name
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _strings(filename: str) -> dict:
    return json.loads((_PKG / filename).read_text(encoding="utf-8"))


def _const_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_constants(name: str) -> dict[str, str]:
    """Module-level ``NAME = "text"`` / ``NAME: Final = "text"`` assignments."""
    found: dict[str, str] = {}
    for node in _parse(name).body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        text = _const_str(value)
        if text is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = text
    return found


@dataclass
class Description:
    """One ``...EntityDescription(...)`` call, read from the source."""

    key: str
    translation_key: str | None
    state_class: str | None
    value_attrs: set[str] = field(default_factory=set)
    spot_attrs: set[str] = field(default_factory=set)


def _descriptions(module: str) -> list[Description]:
    found: list[Description] = []
    for node in ast.walk(_parse(module)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else ""
        if not name.endswith("Description") or name.startswith("Sensor"):
            continue
        if not any(kw.arg == "key" for kw in node.keywords):
            continue  # the dataclass definition itself, not an instance
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        key = _const_str(kwargs["key"])
        assert key is not None, f"{module}: key must be a string literal"
        state_class = kwargs.get("state_class")
        value_fn = kwargs.get("value_fn")
        attrs: set[str] = set()
        spot_attrs: set[str] = set()
        if value_fn is not None:
            for sub in ast.walk(value_fn):
                if isinstance(sub, ast.Attribute):
                    attrs.add(sub.attr)
                    if isinstance(sub.value, ast.Attribute) and (
                        sub.value.attr == "spot"
                    ):
                        spot_attrs.add(sub.attr)
        found.append(
            Description(
                key=key,
                translation_key=_const_str(kwargs.get("translation_key")),
                state_class=(
                    state_class.attr if isinstance(state_class, ast.Attribute) else None
                ),
                value_attrs=attrs,
                spot_attrs=spot_attrs,
            )
        )
    return found


def _sensor_descriptions() -> list[Description]:
    return _descriptions("sensor.py")


def _binary_sensor_descriptions() -> list[Description]:
    return _descriptions("binary_sensor.py")


def _spot_fields() -> set[str]:
    """Field names of ``smabt.models.SpotData``."""
    path = _PKG / "smabt" / "models.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SpotData":
            return {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    raise AssertionError("SpotData not found in smabt/models.py")


def _tuple_constant(name: str, module: str = "const.py") -> tuple[str, ...]:
    for node in _parse(module).body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id == name:
            assert isinstance(value, ast.Tuple), f"{name} must be a tuple literal"
            items = [_const_str(elt) for elt in value.elts]
            assert all(item is not None for item in items)
            return tuple(item for item in items if item is not None)
    raise AssertionError(f"{name} not found in {module}")


# ------------------------------------------------------------ flow collection


@dataclass
class FlowUsage:
    """What one group of flow classes (config or options) uses."""

    steps: set[str] = field(default_factory=set)
    errors: set[str] = field(default_factory=set)
    aborts: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)


def _flow_usage() -> dict[str, FlowUsage]:
    consts = _module_constants("const.py")
    usage = {"config": FlowUsage(), "options": FlowUsage()}

    def visit(node: ast.AST, group: str) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                _visit_call(sub, usage[group], consts)
            elif isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "errors"
                    ):
                        # ``errors["base"] = err.error`` carries a ValidationError
                        # code (collected from the raise sites); literals are
                        # codes of their own.
                        if (code := _const_str(sub.value)) is not None:
                            usage[group].errors.add(code)

    tree = _parse("config_flow.py")
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            group = "options" if "Options" in node.name else "config"
        else:
            group = "config"
        visit(node, group)
    return usage


def _visit_call(call: ast.Call, usage: FlowUsage, consts: dict[str, str]) -> None:
    func = call.func
    attr = func.attr if isinstance(func, ast.Attribute) else ""
    name = func.id if isinstance(func, ast.Name) else ""
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}

    if (step := _const_str(kwargs.get("step_id"))) is not None:
        usage.steps.add(step)
    if (reason := _const_str(kwargs.get("reason"))) is not None:
        usage.aborts.add(reason)
    if attr == "_abort_if_unique_id_configured":
        usage.aborts.add("already_configured")
    if attr == "_abort_if_unique_id_mismatch" and "reason" not in kwargs:
        usage.aborts.add("unique_id_mismatch")
    # raise ValidationError("<code>") in the shared validation helper
    if name == "ValidationError" and call.args:
        code = _const_str(call.args[0])
        assert code is not None, "ValidationError codes must be literals"
        usage.errors.add(code)
    # vol.Required(CONF_X, ...) / vol.Optional(CONF_X, ...)
    if (
        attr in {"Required", "Optional"}
        and isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "vol"
        and call.args
    ):
        first = call.args[0]
        if isinstance(first, ast.Name):
            assert first.id in consts, f"{first.id} is not defined in const.py"
            usage.fields.add(consts[first.id])
        else:
            literal = _const_str(first)
            assert literal is not None
            usage.fields.add(literal)


# ------------------------------------------------------------------ translations


def test_strings_json_equals_en_json() -> None:
    assert _strings("strings.json") == _strings("translations/en.json")


def test_translation_files_are_formatted_identically() -> None:
    assert (_PKG / "strings.json").read_text(encoding="utf-8") == (
        _PKG / "translations" / "en.json"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("platform", "descriptions"),
    [
        ("sensor", _sensor_descriptions),
        ("binary_sensor", _binary_sensor_descriptions),
    ],
)
def test_every_entity_key_has_a_translation(platform: str, descriptions) -> None:
    found = descriptions()
    assert found, f"no entity descriptions found in {platform}.py"
    keys = [d.key for d in found]
    assert len(keys) == len(set(keys)), f"duplicate {platform} keys"
    entities = _strings("strings.json")["entity"][platform]
    for description in found:
        assert description.translation_key == description.key, description.key
        assert "name" in entities[description.translation_key], description.key
    assert set(entities) == {d.translation_key for d in found}, (
        "strings.json has entity names without a description (or vice versa)"
    )


def test_the_expected_entities_exist() -> None:
    sensors = {d.key for d in _sensor_descriptions()}
    assert sensors == {
        "power",
        "ac_power_l1",
        "ac_power_l2",
        "ac_power_l3",
        "ac_voltage_l1",
        "ac_voltage_l2",
        "ac_voltage_l3",
        "ac_current_l1",
        "ac_current_l2",
        "ac_current_l3",
        "dc_power_a",
        "dc_power_b",
        "dc_voltage_a",
        "dc_voltage_b",
        "dc_current_a",
        "dc_current_b",
        "grid_frequency",
        "energy_today",
        "energy_total",
        "operating_time",
        "feed_in_time",
        "temperature",
        "status",
        "grid_relay",
        "bluetooth_signal",
    }
    assert {d.key for d in _binary_sensor_descriptions()} == {"inverter_online"}


def test_every_value_fn_reads_a_real_spot_field() -> None:
    fields = _spot_fields()
    for description in _sensor_descriptions():
        assert description.spot_attrs, f"{description.key} has no d.spot.<field>"
        assert description.spot_attrs <= fields, (
            f"{description.key} reads unknown SpotData field(s): "
            f"{description.spot_attrs - fields}"
        )


def test_every_cumulative_sensor_is_preserved_by_the_coordinator() -> None:
    """A TOTAL_INCREASING sensor must read a field the coordinator protects.

    ``e_today`` has its own rule (reset at local midnight); the others are
    listed in ``const.CUMULATIVE_FIELDS`` and never report 0 or go down.
    """
    protected = set(_tuple_constant("CUMULATIVE_FIELDS")) | {"e_today"}
    cumulative = [
        d for d in _sensor_descriptions() if d.state_class == "TOTAL_INCREASING"
    ]
    assert {d.key for d in cumulative} == {
        "energy_today",
        "energy_total",
        "operating_time",
        "feed_in_time",
    }
    for description in cumulative:
        assert description.spot_attrs & protected, description.key
    assert set(_tuple_constant("CUMULATIVE_FIELDS")) == {
        "e_total",
        "operating_time",
        "feed_in_time",
    }


# ------------------------------------------------------------------ config flow


def test_config_flow_covers_every_required_error_and_abort() -> None:
    config = _flow_usage()["config"]
    assert EXPECTED_FLOW_ERRORS <= config.errors
    assert EXPECTED_FLOW_ABORTS <= config.aborts
    assert EXPECTED_STEPS - {"init"} <= config.steps
    assert "init" in _flow_usage()["options"].steps


@pytest.mark.parametrize("group", ["config", "options"])
def test_flow_errors_and_aborts_exist_in_strings(group: str) -> None:
    usage = _flow_usage()[group]
    strings = _strings("strings.json")[group]
    missing_errors = usage.errors - set(strings.get("error", {}))
    missing_aborts = usage.aborts - set(strings.get("abort", {}))
    assert not missing_errors, f"{group}: errors without a string: {missing_errors}"
    assert not missing_aborts, f"{group}: aborts without a string: {missing_aborts}"


@pytest.mark.parametrize("group", ["config", "options"])
def test_strings_have_no_unused_errors_aborts_or_steps(group: str) -> None:
    usage = _flow_usage()[group]
    strings = _strings("strings.json")[group]
    assert set(strings.get("error", {})) <= usage.errors
    assert set(strings.get("abort", {})) <= usage.aborts
    assert set(strings["step"]) == usage.steps


def test_every_flow_field_is_described() -> None:
    """Each form field has a label (``data``) and a ``data_description``."""
    usage = _flow_usage()
    strings = _strings("strings.json")
    labels: set[str] = set()
    descriptions: set[str] = set()
    for group in ("config", "options"):
        for step_id, step in strings[group]["step"].items():
            data = set(step.get("data", {}))
            described = set(step.get("data_description", {}))
            assert described == data, f"{group}.{step_id}: data_description != data"
            assert step.get("title"), f"{group}.{step_id}: no title"
            labels |= data
            descriptions |= described
    used = usage["config"].fields | usage["options"].fields
    assert used <= labels, f"fields without a label: {used - labels}"
    assert used <= descriptions


def test_the_inverter_sleeps_hint_is_in_the_user_step() -> None:
    """The form must tell the user to set up in daylight."""
    strings = _strings("strings.json")
    text = json.dumps(strings["config"]["step"]["user"]).lower()
    assert "sun" in text or "daylight" in text
    assert "inverter_unreachable" in strings["config"]["error"]
