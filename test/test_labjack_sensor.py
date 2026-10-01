from types import SimpleNamespace

from AFL.automation.loading.LabJackSensor import LabJackSensor
from AFL.automation.loading.LabJackDigitalOut import LabJackDigitalOut


class FakeLJM:
    def __init__(self):
        self.started = []
        self.waited = []

    def openS(self, *args):
        return 1

    def startInterval(self, handle, rate):
        self.started.append(handle)

    def waitForNextInterval(self, handle):
        self.waited.append(handle)
        return 0

    def eWriteName(self, *args):
        pass

    def eReadName(self, *args):
        return 0.5


def patch_ljm(monkeypatch, module):
    ljm = FakeLJM()
    monkeypatch.setattr(f"AFL.automation.loading.{module}.lazy.load", lambda *a, **k: ljm)
    return ljm


def test_each_sensor_uses_its_own_interval_timer(monkeypatch):
    ljm = patch_ljm(monkeypatch, "LabJackSensor")

    a = LabJackSensor(port_to_read="AIN0", reset_port="DIO6")
    b = LabJackSensor(port_to_read="AIN1", reset_port="DIO7")
    a.read()
    b.read()

    assert a.intervalHandle != b.intervalHandle
    assert ljm.waited == [a.intervalHandle, b.intervalHandle]


def test_digital_out_write_does_not_wait_on_a_sensor_timer(monkeypatch):
    ljm = patch_ljm(monkeypatch, "LabJackDigitalOut")

    LabJackDigitalOut(port_to_write="TDAC4").write(1.0)

    assert ljm.waited == []
