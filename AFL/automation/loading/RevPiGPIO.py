"""Revolution Pi digital-input driver for AFL loaders.

Input names are configured in piCtory and read through ``revpimodio2``'s
process image.  The public interface mirrors :class:`PiGPIO`: ``state`` maps
logical input names to their current boolean values.
"""

import lazy_loader as lazy


class RevPiGPIO:
    """Read Revolution Pi digital inputs via ``revpimodio2``.

    Parameters
    ----------
    channels : dict
        Mapping of piCtory input variable names to logical AFL names, such as
        ``{'I_1': 'DOOR', 'I_2': 'ARM_UP'}``.
    autorefresh : bool, optional
        Passed to ``revpimodio2.RevPiModIO``. Defaults to ``True`` so reads
        reflect the current process image.
    """

    def __init__(self, channels: dict, autorefresh: bool = True) -> None:
        self.revpimodio = lazy.load(
            "revpimodio2", require="AFL-automation[revpi]"
        )
        self.channels = dict(channels)
        self.ids = {name: pictory_name for pictory_name, name in self.channels.items()}
        self._rpi = self.revpimodio.RevPiModIO(autorefresh=autorefresh)

        for pictory_name in self.channels:
            try:
                _ = self._rpi.io[pictory_name]
            except KeyError as error:
                raise RuntimeError(
                    f"RevPiGPIO: input '{pictory_name}' not found in the RevPi "
                    "process image. Check your piCtory configuration."
                ) from error

    @property
    def state(self) -> dict[str, bool]:
        """Return current input values keyed by their logical AFL names."""
        return {
            name: bool(self._rpi.io[pictory_name].value)
            for pictory_name, name in self.channels.items()
        }

    def read(self, name: str) -> bool:
        """Return the current value of a logical input name."""
        if name not in self.ids:
            raise KeyError(
                f"RevPiGPIO: unknown channel '{name}'. Known channels: {list(self.ids)}"
            )
        return self.state[name]