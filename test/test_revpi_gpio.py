from types import SimpleNamespace

import pytest

from AFL.automation.loading.RevPiGPIO import RevPiGPIO


class FakeRevPiModIO:
    def __init__(self, autorefresh, monitoring=False):
        self.autorefresh = autorefresh
        self.monitoring = monitoring
        self.io = {
            "I_1": SimpleNamespace(value=1),
            "I_2": SimpleNamespace(value=0),
        }


def make_gpio(monkeypatch):
    monkeypatch.setattr(
        "AFL.automation.loading.RevPiGPIO.lazy.load",
        lambda *args, **kwargs: SimpleNamespace(RevPiModIO=FakeRevPiModIO),
    )
    return RevPiGPIO({"I_1": "DOOR", "I_2": "ARM_UP"})


def test_revpi_gpio_reads_current_process_image(monkeypatch):
    gpio = make_gpio(monkeypatch)

    assert gpio.ids == {"DOOR": "I_1", "ARM_UP": "I_2"}
    assert gpio.state == {"DOOR": True, "ARM_UP": False}

    gpio._rpi.io["I_2"].value = 1
    assert gpio.read("ARM_UP") is True


def test_revpi_gpio_opens_process_image_read_only(monkeypatch):
    # A second autorefreshing RevPiModIO that is not in monitoring mode writes
    # its (all False) output buffer every cycle, fighting RevPiRelay.
    gpio = make_gpio(monkeypatch)

    assert gpio._rpi.monitoring is True


def test_revpi_gpio_validates_input_names(monkeypatch):
    monkeypatch.setattr(
        "AFL.automation.loading.RevPiGPIO.lazy.load",
        lambda *args, **kwargs: SimpleNamespace(RevPiModIO=FakeRevPiModIO),
    )

    with pytest.raises(RuntimeError, match="I_99"):
        RevPiGPIO({"I_99": "DOOR"})


def test_revpi_gpio_rejects_unknown_logical_input(monkeypatch):
    gpio = make_gpio(monkeypatch)

    with pytest.raises(KeyError, match="UNKNOWN"):
        gpio.read("UNKNOWN")