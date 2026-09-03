"""Robot-specific behavior for the Opentrons HTTP driver."""

from abc import ABC
from collections.abc import Mapping

import requests


_OT2_TO_FLEX_SLOT = {
    "1": "D1", "2": "D2", "3": "D3",
    "4": "C1", "5": "C2", "6": "C3",
    "7": "B1", "8": "B2", "9": "B3",
    "10": "A1", "11": "A2", "12": "A3",
}

_FLEX_TRASH_CUTOUT_TO_AREA = {
    "cutoutA1": "movableTrashA1",
    "cutoutB1": "movableTrashB1",
    "cutoutC1": "movableTrashC1",
    "cutoutD1": "movableTrashD1",
    "cutoutA3": "movableTrashA3",
    "cutoutB3": "movableTrashB3",
    "cutoutC3": "movableTrashC3",
    "cutoutD3": "movableTrashD3",
}

_FLEX_MODULE_FIXTURE_IDS = {
    "heaterShakerModuleV1",
    "magneticBlockV1",
    "thermocyclerModuleV2",
    "absorbanceReaderV1",
}


class RobotProfile(ABC):
    """Encapsulate robot-family behavior used by ``OpentronsHTTPDriver``."""

    api_version: str
    robot_type: str
    pipette_name_aliases: Mapping[str, str]
    expected_tiprack_token: Mapping[str, str]

    def normalize_slot(self, slot: str | int) -> str:
        """Return the API slot name for a user-supplied deck location."""
        return str(slot).strip().upper()

    def normalize_pipette_name(self, pipette_name: str) -> str:
        """Return the API pipette name for an alias or canonical name."""
        key = str(pipette_name).strip().lower()
        return self.pipette_name_aliases.get(key, key)

    def expected_tiprack_name(self, pipette_name: str) -> str | None:
        """Return the expected tiprack token for a pipette, if known."""
        return self.expected_tiprack_token.get(self.normalize_pipette_name(pipette_name))

    def trash_addressable_area(self, deck_configuration: list[dict] | None = None) -> str:
        """Return the trash addressable area for the current deck."""
        raise NotImplementedError

    def configure_startup(self, driver) -> None:
        """Apply robot-specific startup behavior after connecting."""


class OT2Profile(RobotProfile):
    api_version = "2"
    robot_type = "OT-2 Standard"
    pipette_name_aliases = {
        "p10": "p10_single",
        "p10_single": "p10_single",
        "p10_single_gen1": "p10_single",
        "p300": "p300_single",
        "p300_single": "p300_single",
        "p1000": "p1000_single",
        "p1000_single": "p1000_single",
    }
    expected_tiprack_token = {
        "p10_single": "10ul",
        "p300_single": "300ul",
        "p1000_single": "1000ul",
    }

    def trash_addressable_area(self, deck_configuration: list[dict] | None = None) -> str:
        return "fixedTrash"


class FlexProfile(RobotProfile):
    api_version = "4"
    robot_type = "OT-3"
    pipette_name_aliases = {
        "flex_1channel_50": "flex_1channel_50",
        "flex_1channel_1000": "flex_1channel_1000",
        "flex_8channel_50": "flex_8channel_50",
        "flex_8channel_1000": "flex_8channel_1000",
        "flex_96channel_1000": "flex_96channel_1000",
        "flex_50": "flex_1channel_50",
        "flex_1000": "flex_1channel_1000",
        "flex_8_50": "flex_8channel_50",
        "flex_8_1000": "flex_8channel_1000",
        "flex_96": "flex_96channel_1000",
    }
    expected_tiprack_token = {
        "flex_1channel_50": "50ul",
        "flex_1channel_1000": "1000ul",
        "flex_8channel_50": "50ul",
        "flex_8channel_1000": "1000ul",
        "flex_96channel_1000": "1000ul",
    }

    def normalize_slot(self, slot: str | int) -> str:
        value = super().normalize_slot(slot)
        return _OT2_TO_FLEX_SLOT.get(value, value)

    def trash_addressable_area(self, deck_configuration: list[dict] | None = None) -> str:
        for fixture in deck_configuration or []:
            if fixture.get("cutoutFixtureId") != "trashBinAdapter":
                continue
            area = _FLEX_TRASH_CUTOUT_TO_AREA.get(fixture.get("cutoutId"))
            if area is not None:
                return area
        return "movableTrashA3"

    def configure_startup(self, driver) -> None:
        self._home_if_needed(driver)
        module_serials = self._get_module_serials(driver)
        self._sync_deck_configuration(driver, module_serials)

    def _home_if_needed(self, driver) -> None:
        try:
            response = requests.get(
                url=f"{driver.base_url}/motors/engaged", headers=driver.headers
            )
            if response.status_code != 200:
                driver.log_warning(
                    f"Could not check motor engagement (HTTP {response.status_code}); "
                    "homing to be safe."
                )
                driver.home()
                return

            engaged = response.json()
            if not (
                engaged.get("x", {}).get("enabled", False)
                and engaged.get("y", {}).get("enabled", False)
            ):
                driver.log_info("Gantry axes not engaged; homing robot before use.")
                driver.home()
        except Exception as error:
            driver.log_warning(f"Motor engagement check failed ({error}); homing to be safe.")
            driver.home()

    def _get_module_serials(self, driver) -> dict[str, str]:
        try:
            response = requests.get(
                url=f"{driver.base_url}/modules", headers=driver.headers, timeout=5
            )
            if response.status_code != 200:
                driver.log_warning(
                    f"Could not fetch module list (HTTP {response.status_code}); "
                    "serial numbers will be omitted from deck configuration."
                )
                return {}

            return {
                f"cutout{module['moduleOffset']['slot']}": module["serialNumber"]
                for module in response.json().get("data", [])
                if module.get("serialNumber") and module.get("moduleOffset", {}).get("slot")
            }
        except Exception as error:
            driver.log_warning(
                f"Could not fetch Flex module serial numbers ({error}); "
                "serial numbers will be omitted from deck configuration."
            )
            return {}

    def _sync_deck_configuration(self, driver, module_serials: Mapping[str, str]) -> None:
        try:
            response = requests.get(
                url=f"{driver.base_url}/deck_configuration",
                headers=driver.headers,
                timeout=5,
            )
            fixtures = response.json().get("data", {}).get("cutoutFixtures", [])
            if response.status_code == 200 and fixtures:
                driver.config["deck_configuration"] = [
                    {
                        key: value
                        for key, value in fixture.items()
                        if key != "opentronsModuleSerialNumber"
                    }
                    for fixture in fixtures
                ]
                driver.config._update_history()
        except Exception as error:
            driver.log_warning(
                f"Could not read robot deck configuration ({error}); using configured defaults."
            )

        self._apply_deck_configuration(driver, module_serials)

    def _apply_deck_configuration(self, driver, module_serials: Mapping[str, str]) -> None:
        deck_configuration = driver.config.get("deck_configuration", [])
        if not deck_configuration:
            return

        payload = []
        for fixture in deck_configuration:
            enriched_fixture = dict(fixture)
            if fixture.get("cutoutFixtureId") in _FLEX_MODULE_FIXTURE_IDS:
                serial = module_serials.get(fixture.get("cutoutId"))
                if serial is not None:
                    enriched_fixture["opentronsModuleSerialNumber"] = serial
            payload.append(enriched_fixture)

        response = requests.put(
            url=f"{driver.base_url}/deck_configuration",
            headers=driver.headers,
            json={"data": {"cutoutFixtures": payload}},
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(
                "Failed to apply Flex deck configuration "
                f"(HTTP {response.status_code}): {response.text}"
            )
