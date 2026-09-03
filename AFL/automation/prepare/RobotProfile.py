"""Robot-specific behavior for the Opentrons HTTP driver."""

from abc import ABC, abstractmethod
from typing import Dict, List, Mapping, Optional, Tuple, Union

import requests


_OT2_TO_FLEX_SLOT = {
    "1": "D1", "2": "D2", "3": "D3",
    "4": "C1", "5": "C2", "6": "C3",
    "7": "B1", "8": "B2", "9": "B3",
    "10": "A1", "11": "A2", "12": "A3",
}

_FLEX_STAGING_SLOTS = {"A4", "B4", "C4", "D4"}

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

_FLEX_THERMOCYCLER_CUTOUTS = {"cutoutA1", "cutoutB1"}


class RobotProfile(ABC):
    """Encapsulate robot-family behavior used by ``OpentronsHTTPDriver``."""

    api_version: str
    robot_type: str
    driver_name: str
    defaults: Dict[str, object]
    pipette_name_aliases: Mapping[str, str]
    expected_tiprack_token: Mapping[str, str]

    @abstractmethod
    def normalize_slot(self, slot: Union[str, int]) -> str:
        """Return the API slot name for a user-supplied deck location."""

    @abstractmethod
    def slot_location(self, slot: Union[str, int]) -> Dict[str, str]:
        """Return the Opentrons API location payload for a deck position."""

    @abstractmethod
    def parse_well(self, location: str) -> Tuple[str, str]:
        """Split a deck location into an API slot name and well name."""

    @abstractmethod
    def normalize_pipette_name(self, pipette_name: str) -> str:
        """Return the API pipette name for an alias or canonical name."""

    @abstractmethod
    def expected_tiprack_name(self, pipette_name: str) -> Optional[str]:
        """Return the expected tiprack token for a pipette, if known."""

    @abstractmethod
    def trash_addressable_area(
        self, deck_configuration: Optional[List[dict]] = None
    ) -> str:
        """Return the trash addressable area for the current deck."""

    @abstractmethod
    def validate_slot(self, slot: str, config: Mapping[str, object]) -> None:
        """Raise an error when a slot cannot be used by this robot."""

    @abstractmethod
    def labware_move_strategy(
        self, use_gripper: bool, config: Mapping[str, object]
    ) -> str:
        """Return the Opentrons strategy for a labware move."""

    @abstractmethod
    def load_gripper(self, driver) -> str:
        """Detect and persist the robot gripper, if supported."""

    @abstractmethod
    def prepare_module_load(self, driver, module_name: str, slot: str) -> None:
        """Apply robot-specific deck preparation before loading a module."""

    @abstractmethod
    def instrument_mounts(
        self, pipette_name: str, requested_mount: str
    ) -> Tuple[str, str]:
        """Return the API mount and persistent state mount for a pipette."""

    @abstractmethod
    def normalize_pipette_info(self, driver) -> None:
        """Normalize API-reported pipette metadata for persistent state."""

    @abstractmethod
    def get_tip(self, driver, mount: str) -> Tuple[str, str]:
        """Reserve and return the next tip for a profile-specific mount."""

    @abstractmethod
    def configure_nozzle_layout(self, driver, config_type: str) -> str:
        """Configure an optional multi-channel pipette nozzle layout."""

    @abstractmethod
    def reset_deck(self, driver) -> None:
        """Clear robot-specific persistent state after a generic deck reset."""

    @abstractmethod
    def configure_startup(self, driver) -> None:
        """Apply robot-specific startup behavior after connecting."""


class OT2Profile(RobotProfile):
    api_version = "2"
    robot_type = "OT-2 Standard"
    driver_name = "OT2_HTTP_Driver"
    defaults = {}
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

    def normalize_slot(self, slot: Union[str, int]) -> str:
        return str(slot).strip().upper()

    def slot_location(self, slot: Union[str, int]) -> Dict[str, str]:
        return {"slotName": self.normalize_slot(slot)}

    def parse_well(self, location: str) -> Tuple[str, str]:
        for index, character in enumerate(str(location)):
            if character.isalpha():
                return str(location)[:index], str(location)[index:]
        return str(location), ""

    def normalize_pipette_name(self, pipette_name: str) -> str:
        key = str(pipette_name).strip().lower()
        return self.pipette_name_aliases.get(key, key)

    def expected_tiprack_name(self, pipette_name: str) -> Optional[str]:
        return self.expected_tiprack_token.get(self.normalize_pipette_name(pipette_name))

    def trash_addressable_area(
        self, deck_configuration: Optional[List[dict]] = None
    ) -> str:
        return "fixedTrash"

    def validate_slot(self, slot: str, config: Mapping[str, object]) -> None:
        return None

    def labware_move_strategy(
        self, use_gripper: bool, config: Mapping[str, object]
    ) -> str:
        return "manualMoveWithoutPause"

    def load_gripper(self, driver) -> str:
        raise NotImplementedError("OT2 does not support a gripper.")

    def prepare_module_load(self, driver, module_name: str, slot: str) -> None:
        return None

    def instrument_mounts(
        self, pipette_name: str, requested_mount: str
    ) -> Tuple[str, str]:
        mount = str(requested_mount).strip().lower()
        return mount, mount

    def normalize_pipette_info(self, driver) -> None:
        return None

    def get_tip(self, driver, mount: str) -> Tuple[str, str]:
        available = list(driver.config.get("available_tips", {}).get(mount, []))
        if not available:
            raise ValueError(f"No tips available for mount {mount}")

        reserved_locations = {
            str(location).strip().upper()
            for location in driver.config.get("reserved_stock_tips", [])
        }
        for index, (tiprack_id, well_name) in enumerate(available):
            slot = driver._slot_by_labware_uuid(tiprack_id)
            location = None if slot is None else f"{slot}{well_name}".upper()
            if location not in reserved_locations:
                driver.config.setdefault("available_tips", {})[mount] = (
                    available[:index] + available[index + 1:]
                )
                return tiprack_id, well_name
        raise RuntimeError(f"No unreserved tips available for {mount} mount")

    def configure_nozzle_layout(self, driver, config_type: str) -> str:
        raise NotImplementedError("OT2 does not support configurable nozzle layouts.")

    def reset_deck(self, driver) -> None:
        return None

    def configure_startup(self, driver) -> None:
        return None


class FlexProfile(RobotProfile):
    api_version = "4"
    robot_type = "OT-3"
    driver_name = "FlexHTTPDriver"
    defaults = {
        "deck_configuration": [
            {"cutoutFixtureId": "singleLeftSlot", "cutoutId": "cutoutA1"},
            {"cutoutFixtureId": "singleLeftSlot", "cutoutId": "cutoutB1"},
            {"cutoutFixtureId": "singleLeftSlot", "cutoutId": "cutoutC1"},
            {"cutoutFixtureId": "singleLeftSlot", "cutoutId": "cutoutD1"},
            {"cutoutFixtureId": "singleCenterSlot", "cutoutId": "cutoutA2"},
            {"cutoutFixtureId": "singleCenterSlot", "cutoutId": "cutoutB2"},
            {"cutoutFixtureId": "singleCenterSlot", "cutoutId": "cutoutC2"},
            {"cutoutFixtureId": "singleCenterSlot", "cutoutId": "cutoutD2"},
            {"cutoutFixtureId": "trashBinAdapter", "cutoutId": "cutoutA3"},
            {"cutoutFixtureId": "stagingAreaRightSlot", "cutoutId": "cutoutB3"},
            {"cutoutFixtureId": "stagingAreaRightSlot", "cutoutId": "cutoutC3"},
            {"cutoutFixtureId": "stagingAreaRightSlot", "cutoutId": "cutoutD3"},
        ],
        "loaded_gripper": None,
        "blocked_slots": [],
    }
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

    _96_CHANNEL_MOUNT = "96channel"
    _NOZZLE_LAYOUT_PARAMS = {
        "full96": {"primaryNozzle": "A1", "frontRightNozzle": "H12", "style": "ALL"},
        "column": {"primaryNozzle": "A1", "frontRightNozzle": "H1", "style": "COLUMN"},
        "single": {"primaryNozzle": "A1", "frontRightNozzle": "A1", "style": "SINGLE"},
    }

    def normalize_slot(self, slot: Union[str, int]) -> str:
        value = str(slot).strip().upper()
        return _OT2_TO_FLEX_SLOT.get(value, value)

    def slot_location(self, slot: Union[str, int]) -> Dict[str, str]:
        flex_slot = self.normalize_slot(slot)
        if flex_slot in _FLEX_STAGING_SLOTS:
            return {"addressableAreaName": flex_slot}
        return {"slotName": flex_slot}

    def parse_well(self, location: str) -> Tuple[str, str]:
        value = str(location).strip().upper()
        if len(value) >= 3 and value[0].isalpha() and value[1].isdigit():
            return value[:2], value[2:]
        for index, character in enumerate(value):
            if character.isalpha():
                return self.normalize_slot(value[:index]), value[index:]
        return self.normalize_slot(value), ""

    def normalize_pipette_name(self, pipette_name: str) -> str:
        key = str(pipette_name).strip().lower()
        return self.pipette_name_aliases.get(key, key)

    def expected_tiprack_name(self, pipette_name: str) -> Optional[str]:
        return self.expected_tiprack_token.get(self.normalize_pipette_name(pipette_name))

    def trash_addressable_area(
        self, deck_configuration: Optional[List[dict]] = None
    ) -> str:
        for fixture in deck_configuration or []:
            if fixture.get("cutoutFixtureId") != "trashBinAdapter":
                continue
            area = _FLEX_TRASH_CUTOUT_TO_AREA.get(fixture.get("cutoutId"))
            if area is not None:
                return area
        return "movableTrashA3"

    def validate_slot(self, slot: str, config: Mapping[str, object]) -> None:
        blocked_slots = [
            str(blocked_slot).strip().upper()
            for blocked_slot in config.get("blocked_slots", [])
        ]
        if slot in blocked_slots:
            raise ValueError(
                f"Slot {slot!r} is physically inaccessible (listed in blocked_slots)."
            )

    def labware_move_strategy(
        self, use_gripper: bool, config: Mapping[str, object]
    ) -> str:
        if not use_gripper:
            return "manualMoveWithoutPause"
        if not config.get("loaded_gripper"):
            raise RuntimeError("Gripper is not loaded. Call load_gripper() before move_labware().")
        return "usingGripper"

    def load_gripper(self, driver) -> str:
        response = requests.get(
            url=f"{driver.base_url}/instruments", headers=driver.headers
        )
        if response.status_code != 200:
            raise RuntimeError(f"Failed to get instruments: {response.text}")

        gripper = next(
            (
                instrument
                for instrument in response.json().get("data", [])
                if instrument.get("mount") == "extension"
            ),
            None,
        )
        if gripper is None:
            raise RuntimeError(
                "No gripper found on the extension mount. "
                "Ensure a gripper is physically attached to the Flex."
            )

        serial = gripper.get("serialNumber")
        if not serial:
            raise RuntimeError(
                "Gripper found on the extension mount but its serialNumber is missing."
            )

        driver.config["loaded_gripper"] = {"gripper_id": serial, "serial": serial}
        driver.config._update_history()
        driver.log_info(f"Gripper detected with serial {serial}")
        return serial

    def prepare_module_load(self, driver, module_name: str, slot: str) -> None:
        flex_slot = self.normalize_slot(slot)
        cutout_id = f"cutout{flex_slot}"
        module_fixture = module_name if module_name in _FLEX_MODULE_FIXTURE_IDS else None
        module_serials = getattr(driver, "_module_serials", {})

        if module_fixture and module_serials and cutout_id not in module_serials:
            raise ValueError(
                f"No {module_name!r} detected at Flex slot {flex_slot!r}."
            )

        if module_name == "thermocyclerModuleV2":
            deck_configuration = [
                fixture
                for fixture in driver.config.get("deck_configuration", [])
                if fixture.get("cutoutId") not in _FLEX_THERMOCYCLER_CUTOUTS
            ]
            deck_configuration.extend(
                {
                    "cutoutId": thermocycler_cutout,
                    "cutoutFixtureId": "thermocyclerModuleV2",
                }
                for thermocycler_cutout in sorted(_FLEX_THERMOCYCLER_CUTOUTS)
            )
        elif module_fixture:
            deck_configuration = []
            replaced = False
            for fixture in driver.config.get("deck_configuration", []):
                if fixture.get("cutoutId") == cutout_id:
                    deck_configuration.append(
                        {"cutoutId": cutout_id, "cutoutFixtureId": module_fixture}
                    )
                    replaced = True
                else:
                    deck_configuration.append(fixture)
            if not replaced:
                deck_configuration.append(
                    {"cutoutId": cutout_id, "cutoutFixtureId": module_fixture}
                )
        else:
            return

        driver.config["deck_configuration"] = deck_configuration
        driver.config._update_history()
        self._apply_deck_configuration(driver, module_serials)

    def instrument_mounts(
        self, pipette_name: str, requested_mount: str
    ) -> Tuple[str, str]:
        if "96channel" in pipette_name:
            return "left", self._96_CHANNEL_MOUNT
        mount = str(requested_mount).strip().lower()
        return mount, mount

    def normalize_pipette_info(self, driver) -> None:
        left_pipette = driver.pipette_info.get("left")
        if left_pipette and "96channel" in left_pipette.get("name", ""):
            driver.pipette_info[self._96_CHANNEL_MOUNT] = driver.pipette_info.pop("left")
            stored = driver.config.get("loaded_instruments", {}).get(
                self._96_CHANNEL_MOUNT, {}
            )
            if stored.get("pipette_id"):
                driver.pipette_info[self._96_CHANNEL_MOUNT]["id"] = stored["pipette_id"]
            driver.pipette_info[self._96_CHANNEL_MOUNT]["mount"] = self._96_CHANNEL_MOUNT

    def get_tip(self, driver, mount: str) -> Tuple[str, str]:
        if mount != self._96_CHANNEL_MOUNT:
            return OT2Profile().get_tip(driver, mount)

        available = list(driver.config.get("available_tips", {}).get(mount, []))
        if not available:
            raise RuntimeError("No tip racks available for the 96-channel pipette.")
        tiprack_id = available[0][0]
        driver.config.setdefault("available_tips", {})[mount] = [
            tip for tip in available if tip[0] != tiprack_id
        ]
        driver.config._update_history()
        return tiprack_id, "A1"

    def configure_nozzle_layout(self, driver, config_type: str) -> str:
        if config_type not in self._NOZZLE_LAYOUT_PARAMS:
            raise ValueError(
                f"config_type must be one of {list(self._NOZZLE_LAYOUT_PARAMS)!r}."
            )
        instrument = driver.config.get("loaded_instruments", {}).get(
            self._96_CHANNEL_MOUNT
        )
        if instrument is None:
            raise RuntimeError("No 96-channel pipette loaded. Call load_instrument() first.")

        run_id = driver._ensure_run_exists()
        response = requests.post(
            url=f"{driver.base_url}/runs/{run_id}/commands",
            headers=driver.headers,
            params={"waitUntilComplete": True},
            json={
                "data": {
                    "commandType": "configureNozzleLayout",
                    "params": {
                        "pipetteId": instrument["pipette_id"],
                        "configurationParams": self._NOZZLE_LAYOUT_PARAMS[config_type],
                    },
                    "intent": "setup",
                }
            },
        )
        driver._check_cmd_success(response)
        instrument["nozzle_layout"] = config_type
        driver.config._update_history()
        return config_type

    def reset_deck(self, driver) -> None:
        default_fixture_by_column = {
            "1": "singleLeftSlot",
            "2": "singleCenterSlot",
            "3": "stagingAreaRightSlot",
        }
        driver.config["loaded_gripper"] = None
        driver.config["deck_configuration"] = [
            {
                "cutoutId": fixture["cutoutId"],
                "cutoutFixtureId": default_fixture_by_column.get(
                    fixture["cutoutId"][-1], "singleLeftSlot"
                ),
            }
            if fixture.get("cutoutFixtureId") in _FLEX_MODULE_FIXTURE_IDS
            else fixture
            for fixture in driver.config.get("deck_configuration", [])
        ]
        driver.config._update_history()

    def configure_startup(self, driver) -> None:
        self._home_if_needed(driver)
        module_serials = self._get_module_serials(driver)
        driver._module_serials = module_serials
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

    def _get_module_serials(self, driver) -> Dict[str, str]:
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
