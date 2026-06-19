"""
RevPiRelay — Revolution Pi digital-output relay driver for AFL.

Replaces :class:`PiPlatesRelay` / :class:`LabJackRelay` when the relay board
is driven by a Revolution Pi's onboard DIO module via ``revpimodio2``.

The Revolution Pi process image uses named output bits that are configured in
piCtory (the web-based hardware configurator).  Each output is addressed by
the variable name assigned in piCtory, e.g. ``"O_1"``, ``"O_2"``, etc.

Usage example (direct)::

    relay = RevPiRelay(
        relaylabels={
            'O_1': 'arm-up',
            'O_2': 'arm-down',
            'O_3': 'rinse1',
            'O_4': 'rinse2',
            'O_5': 'blow',
            'O_6': 'piston-vent',
            'O_7': 'postsample',
        }
    )

Usage example (launcher / config JSON)::

    {
        "_classname": "AFL.automation.loading.RevPiRelay.RevPiRelay",
        "_args": [{
            "O_1": "arm-up",
            "O_2": "arm-down",
            "O_3": "rinse1",
            "O_4": "rinse2",
            "O_5": "blow",
            "O_6": "piston-vent",
            "O_7": "postsample"
        }]
    }

Note on output naming:
    The exact piCtory output variable names depend on your hardware configuration.
    Open ``http://<revpi-ip>/pictory`` and look at the output names for your
    DIO or RO (relay output) module.  Common patterns are ``"O_1"``–``"O_14"``
    for DIO modules and ``"RelayOutput_1"``–``"RelayOutput_6"`` for RO modules.
"""

import atexit
import threading
import time
import warnings

import lazy_loader as lazy

from AFL.automation.loading.MultiChannelRelay import MultiChannelRelay


class RevPiRelay(MultiChannelRelay):
    """Relay driver for Revolution Pi digital / relay outputs via ``revpimodio2``.

    Parameters
    ----------
    relaylabels : dict
        Mapping of piCtory output variable name (str) to logical channel name
        (str), e.g. ``{'O_1': 'arm-up', 'O_2': 'arm-down'}``.
        Unlike PiPlatesRelay the keys are **strings** (piCtory names), not
        integers.  Every key must exist in the RevPi process image or
        ``revpimodio2`` will raise a ``RuntimeError`` at startup.
    autorefresh : bool, optional
        Passed to ``revpimodio2.RevPiModIO``.  Defaults to ``True`` so that the
        process image is continuously synchronised in the background.
    """

    def __init__(self, relaylabels, autorefresh=True):
        self.revpimodio = lazy.load("revpimodio2", require="AFL-automation[revpi]")

        # labels  : {piCtory_name: logical_name}
        # ids     : {logical_name: piCtory_name}
        self.labels = dict(relaylabels)
        self.ids = {v: k for k, v in self.labels.items()}

        # Runtime attributes assigned by PneumaticSampleCell / PneumaticPressureSampleCell
        self.app = None
        self.data = None

        # Software-side state mirror: {logical_name: bool}
        self.state = {name: False for name in self.ids}

        self._lock = threading.Lock()
        self._rpi = self.revpimodio.RevPiModIO(autorefresh=autorefresh)

        # Validate that every piCtory name actually exists in the process image.
        for pictory_name in self.labels:
            try:
                _ = self._rpi.io[pictory_name]
            except KeyError:
                raise RuntimeError(
                    f"RevPiRelay: output '{pictory_name}' not found in the RevPi "
                    f"process image.  Check your piCtory configuration."
                )

        atexit.register(self.setAllChannelsOff)

    # ------------------------------------------------------------------
    # Public interface (MultiChannelRelay)
    # ------------------------------------------------------------------

    def setAllChannelsOff(self):
        """Set every configured output to False (relay open / de-energised)."""
        self.setChannels({name: False for name in self.ids})

    def setChannels(self, channels):
        """Set one or more relay outputs.

        Parameters
        ----------
        channels : dict
            ``{logical_name: bool}`` pairs, e.g. ``{'arm-up': True}``.
            Keys are logical names (values of ``relaylabels``), not piCtory
            variable names.
        """
        print(f'RevPiRelay state change, CHANNELS = {channels}')

        with self._lock:
            for name, val in channels.items():
                if name not in self.ids:
                    raise KeyError(
                        f"RevPiRelay: unknown channel '{name}'. "
                        f"Known channels: {list(self.ids)}"
                    )
                self.state[name] = bool(val)

            self._refresh_board_state()

    def getChannels(self, asid=False):
        """Read the current state of all channels.

        Parameters
        ----------
        asid : bool, optional
            If True, return piCtory variable names as keys instead of logical
            names.

        Returns
        -------
        dict
            ``{channel_name: bool}`` for every configured output.
        """
        with self._lock:
            if asid:
                return {self.ids[name]: val for name, val in self.state.items()}
            return dict(self.state)

    def toggleChannels(self, channels):
        """Toggle the state of the listed channels.

        Parameters
        ----------
        channels : list of str
            Logical names to toggle.
        """
        updates = {name: not self.state.get(name, False) for name in channels}
        self.setChannels(updates)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_board_state(self):
        """Write the current software state to the RevPi process image and verify readback.

        Called with ``self._lock`` already held.
        """
        # Write all outputs atomically (one by one — revpimodio2 batches writes
        # when autorefresh=True and the cycle runs).
        for name, val in self.state.items():
            pictory_name = self.ids[name]
            self._rpi.io[pictory_name].value = val

        # Give the process image one cycle to propagate (typically ≤ 5 ms).
        time.sleep(0.01)

        # Readback and retry loop — mirrors PiPlatesRelay / LabJackRelay behaviour.
        mismatches = self._get_mismatches()
        if mismatches:
            retries = 0
            warnings.warn(
                f"RevPiRelay: readback mismatch on channels {list(mismatches)}; retrying."
            )
            while retries < 60:
                for name in mismatches:
                    self._rpi.io[self.ids[name]].value = self.state[name]
                time.sleep(0.01)
                mismatches = self._get_mismatches()
                if not mismatches:
                    print(f"RevPiRelay: success after {retries + 1} retries.")
                    break
                retries += 1

            if mismatches:
                raise Exception(
                    f"RevPiRelay: failed to set channels {list(mismatches)} "
                    f"after 60 attempts."
                )

    def _get_mismatches(self):
        """Return the set of logical names whose readback does not match the desired state."""
        mismatches = set()
        for name, desired in self.state.items():
            actual = bool(self._rpi.io[self.ids[name]].value)
            if actual != desired:
                mismatches.add(name)
        return mismatches
