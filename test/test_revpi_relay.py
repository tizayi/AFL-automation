import signal
from types import SimpleNamespace

import pytest

from AFL.automation.loading.RevPiRelay import RevPiRelay


class FakeRevPiModIO:
    def __init__(self, autorefresh):
        self.autorefresh = autorefresh
        self.io = {name: SimpleNamespace(value=0) for name in ("O_1", "O_2")}
        self.written = None  # output values as of exit(), i.e. sent to hardware
        self.exited = False

    def exit(self, full=True):
        # revpimodio2 stops autorefresh and writes the output buffer on exit.
        self.exited = True
        self.written = {name: io.value for name, io in self.io.items()}


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setattr(
        "AFL.automation.loading.RevPiRelay.lazy.load",
        lambda *args, **kwargs: SimpleNamespace(RevPiModIO=FakeRevPiModIO),
    )
    registered = []
    monkeypatch.setattr(
        "AFL.automation.loading.RevPiRelay.atexit.register", registered.append
    )
    # Python's defaults: SIGINT raises KeyboardInterrupt, SIGTERM is SIG_DFL.
    handlers = {signal.SIGINT: signal.default_int_handler}
    monkeypatch.setattr(
        "AFL.automation.loading.RevPiRelay.signal.getsignal",
        lambda signum: handlers.get(signum, signal.SIG_DFL),
    )
    monkeypatch.setattr(
        "AFL.automation.loading.RevPiRelay.signal.signal",
        lambda signum, handler: handlers.__setitem__(signum, handler),
    )
    relay = RevPiRelay({"O_1": "enable", "O_2": "piston-vent"})
    relay._registered_atexit = registered
    relay._signal_handlers = handlers
    return relay


def test_shutdown_turns_outputs_off_and_writes_them_to_hardware(relay):
    relay.setChannels({"enable": True, "piston-vent": True})

    relay.shutdown()

    assert relay._rpi.exited
    assert relay._rpi.written == {"O_1": False, "O_2": False}


def test_shutdown_is_registered_at_exit_and_idempotent(relay):
    assert relay.shutdown in relay._registered_atexit

    relay.shutdown()
    relay.shutdown()


def test_set_channels_after_shutdown_raises(relay):
    relay.shutdown()

    with pytest.raises(RuntimeError, match="shut down"):
        relay.setChannels({"enable": True})


def test_sigterm_turns_outputs_off_then_calls_previous_handler(relay, monkeypatch):
    killed = []
    monkeypatch.setattr(
        "AFL.automation.loading.RevPiRelay.os.kill",
        lambda pid, signum: killed.append(signum),
    )
    relay.setChannels({"enable": True})

    relay._signal_handlers[signal.SIGTERM](signal.SIGTERM, None)

    assert relay._rpi.written == {"O_1": False, "O_2": False}
    # Previous SIGTERM handler was the default, so the signal is re-raised to
    # terminate the process as it would have without our handler.
    assert killed == [signal.SIGTERM]


def test_sigint_chains_to_keyboard_interrupt(relay):
    relay.setChannels({"enable": True})

    with pytest.raises(KeyboardInterrupt):
        relay._signal_handlers[signal.SIGINT](signal.SIGINT, None)

    assert relay._rpi.written == {"O_1": False, "O_2": False}
