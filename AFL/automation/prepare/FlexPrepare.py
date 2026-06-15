from AFL.automation.prepare.FlexHTTPDriver import FlexHTTPDriver
from AFL.automation.prepare.PrepareDriver import PrepareDriver


class FlexPrepare(FlexHTTPDriver, PrepareDriver):
    """Combined Flex robot driver and mass-balance preparation engine.

    Mirrors :class:`~AFL.automation.prepare.OT2Prepare.OT2Prepare` but uses
    :class:`FlexHTTPDriver` as the hardware backend.

    The multiple-inheritance order (FlexHTTPDriver first) means that
    :meth:`transfer` and all hardware methods resolve to FlexHTTPDriver, while
    preparation planning methods (``execute_preparation``, ``set_stocks``, etc.)
    come from PrepareDriver.
    """

    defaults = {
        "prep_targets": [],
        "prepare_volume": "900 ul",
        "catch_volume": "900 ul",
        "deck": {},
        "stocks": [],
        "stock_mix_order": [],
        "fixed_compositions": {},
        "stock_locations": {},
        "stock_transfer_params": {},
        "catch_protocol": {},
    }

    def __init__(self, overrides=None):
        FlexHTTPDriver.__init__(self, overrides=overrides)
        PrepareDriver.__init__(self, driver_name="FlexPrepare", overrides=overrides)
        self.last_target_location = None
        self.useful_links["View Deck"] = "/visualize_deck"

    def status(self):
        return PrepareDriver.status(self) + FlexHTTPDriver.status(self)


_DEFAULT_PORT = 5003

if __name__ == "__main__":
    from AFL.automation.shared.launcher import *
