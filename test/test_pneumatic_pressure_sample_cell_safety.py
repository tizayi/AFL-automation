import pytest

from AFL.automation.loading import PneumaticPressureSampleCell as cell_module
from AFL.automation.loading.PneumaticPressureSampleCell import (
    PneumaticPressureSampleCell,
)


class FakeRelay:
    def __init__(self):
        self.labels = {
            n: n for n in ("enable", "piston-vent", "arm-up", "arm-down", "postsample")
        }
        self.state = {}
        self.calls = []

    def setChannels(self, channels):
        self.calls.append(dict(channels))
        self.state.update(channels)


class FakeDigitalIn:
    def __init__(self, state):
        self.state = state


@pytest.fixture(autouse=True)
def afl_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AFL_HOME", str(tmp_path))


def make_cell(relay=None, digitalin=None, **kwargs):
    return PneumaticPressureSampleCell(
        pctrl=object(),
        relayboard=relay or FakeRelay(),
        digitalin=digitalin,
        overrides={"arm_move_delay": 0},
        **kwargs,
    )


def test_placeholder_robot_host_fails_before_any_output_is_set():
    relay = FakeRelay()

    with pytest.raises(ValueError, match="robot_interlock_host"):
        make_cell(relay=relay, robot_interlock_host="<robot-host-or-IP>")

    assert relay.calls == []


def test_robot_door_status_request_has_timeout(monkeypatch):
    seen = {}

    class Response:
        def json(self):
            return {"data": {"status": "closed"}}

    def fake_get(url, headers=None, timeout=None):
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(cell_module.requests, "get", fake_get)

    make_cell(robot_interlock_host="10.0.0.5")

    assert seen["timeout"] is not None


def test_arm_up_times_out_and_turns_arm_valve_off():
    relay = FakeRelay()
    # ARM_UP stays True: the limit switch never reports the arm as up.
    digitalin = FakeDigitalIn({"ARM_UP": True, "ARM_DOWN": True})

    with pytest.raises(RuntimeError, match="ARM_UP"):
        PneumaticPressureSampleCell(
            pctrl=object(),
            relayboard=relay,
            digitalin=digitalin,
            overrides={"arm_move_delay": 0, "arm_limit_timeout": 0.05},
        )

    assert relay.state["arm-up"] is False


def test_arm_down_times_out_and_turns_arm_valve_off():
    relay = FakeRelay()
    digitalin = FakeDigitalIn({"ARM_UP": False, "ARM_DOWN": True})
    cell = PneumaticPressureSampleCell(
        pctrl=object(),
        relayboard=relay,
        digitalin=digitalin,
        overrides={"arm_move_delay": 0, "arm_limit_timeout": 0.05},
    )

    with pytest.raises(RuntimeError, match="ARM_DOWN"):
        cell._arm_down()

    assert relay.state["arm-down"] is False
    assert cell.arm_state == "UNKNOWN"


def test_arm_up_succeeds_when_limit_reached():
    digitalin = FakeDigitalIn({"ARM_UP": False, "ARM_DOWN": True})

    cell = make_cell(digitalin=digitalin)

    assert cell.arm_state == "UP"


class FakeLoadStopper:
    def __init__(self):
        self.config = {}
        self.resets = 0
        self.app = None
        self.data = None

    def reset(self):
        self.resets += 1


def test_sensor_reset_without_index_resets_every_sensor():
    stoppers = [FakeLoadStopper(), FakeLoadStopper()]
    cell = make_cell(load_stopper=stoppers)
    for ls in stoppers:
        ls.resets = 0

    cell.sensor_reset()

    assert [ls.resets for ls in stoppers] == [1, 1]


def test_sensor_reset_with_index_resets_only_that_sensor():
    stoppers = [FakeLoadStopper(), FakeLoadStopper()]
    cell = make_cell(load_stopper=stoppers)
    for ls in stoppers:
        ls.resets = 0

    cell.sensor_reset(sensor_n="1")  # HTTP query values arrive as strings

    assert [ls.resets for ls in stoppers] == [0, 1]


def test_set_sensor_config_for_one_sensor():
    stoppers = [FakeLoadStopper(), FakeLoadStopper()]
    cell = make_cell(load_stopper=stoppers)
    for ls in stoppers:
        ls.resets = 0

    cell.set_sensor_config(sensor_n=1, stopper_timeout=30)

    assert stoppers[0].config == {}
    assert stoppers[1].config == {"stopper_timeout": 30}
    assert [ls.resets for ls in stoppers] == [0, 1]
