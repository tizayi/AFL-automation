import pytest

from AFL.automation.loading.PneumaticPressureSampleCell import (
    PneumaticPressureSampleCell,
)
from AFL.automation.loading.PneumaticPressureSampleCellRevPi import (
    PneumaticPressureSampleCellRevPi,
    _DEFAULT_CUSTOM_CONFIG,
)


@pytest.fixture(autouse=True)
def afl_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AFL_HOME", str(tmp_path))


class FakeRelay:
    labels = {n: n for n in ("enable", "piston-vent", "arm-up", "arm-down", "postsample")}

    def setChannels(self, channels):
        pass


class FakePressure:
    def timed_dispense(self, *args, **kwargs):
        pass

    def dispenseRunning(self):
        return False


def make(cls):
    return cls(pctrl=FakePressure(), relayboard=FakeRelay(), overrides={"arm_move_delay": 0})


def record_states(cell, monkeypatch):
    seen = []
    monkeypatch.setattr(cell.relayboard, "setChannels", lambda ch: seen.append(cell.state), raising=False)
    return seen


def test_revpi_defaults_route_loads_to_configured_sensors(monkeypatch):
    cell = make(PneumaticPressureSampleCellRevPi)
    monkeypatch.setattr("time.sleep", lambda s: None)
    states = record_states(cell, monkeypatch)

    cell.loadSample()
    assert "LOAD IN PROGRESS to beforeTarget" in states

    states.clear()
    cell.advanceSample()
    assert "LOAD IN PROGRESS to afterTarget" in states


def test_explicit_label_overrides_default(monkeypatch):
    cell = make(PneumaticPressureSampleCellRevPi)
    monkeypatch.setattr("time.sleep", lambda s: None)
    states = record_states(cell, monkeypatch)

    cell.loadSample(load_dest_label="afterTarget")

    assert "LOAD IN PROGRESS to afterTarget" in states


def test_base_cell_keeps_unlabelled_default(monkeypatch):
    cell = make(PneumaticPressureSampleCell)
    monkeypatch.setattr("time.sleep", lambda s: None)
    states = record_states(cell, monkeypatch)

    cell.loadSample()

    assert "LOAD IN PROGRESS" in states


def test_default_labels_match_configured_sensors():
    labels = {ls["sensorlabel"] for ls in _DEFAULT_CUSTOM_CONFIG["load_stopper"]}
    defaults = PneumaticPressureSampleCellRevPi.gather_defaults()

    assert {defaults["load_dest_label"], defaults["advance_dest_label"]} == labels
