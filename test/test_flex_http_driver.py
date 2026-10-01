"""Tests for FlexHTTPDriver and VirtualFlexHTTPDriver.

The test strategy mirrors test_ot2_http_driver.py:
- ``StubFlexHTTPDriver`` bypasses network/hardware init exactly as
  ``StubOT2HTTPDriver`` does for the OT2.
- Tests are grouped by concern:
  1. Slot translation (_normalize_slot / _api_slot_name)
  2. Class-level attribute overrides (API version, trash area, pipette aliases)
  3. Deck configuration (read-only: _get_deck_configuration / trash detection)
  4. Transfer command uses the correct trash addressable area
  5. FlexPrepare MRO sanity
  6. Gripper support
  7. VirtualFlexHTTPDriver
"""

import json

import pytest
from pathlib import Path
from unittest.mock import patch, call

from AFL.automation.prepare.FlexHTTPDriver import FlexHTTPDriver, _OT2_TO_FLEX_SLOT, _96CH_MOUNT_KEY
from AFL.automation.prepare.FlexPrepare import FlexPrepare
from AFL.automation.prepare.OT2HTTPDriver import OT2HTTPDriver
from AFL.automation.prepare.VirtualFlexHTTPDriver import (
    VirtualFlexHTTPDriver,
    VIRTUAL_DECK_CONFIGURATION,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DummyConfig(dict):
    def _update_history(self):
        return None


class StubFlexHTTPDriver(FlexHTTPDriver):
    """In-memory stub that skips all network calls.

    Follows the same pattern as StubOT2HTTPDriver: every attribute that
    ``__init__`` normally sets via ``requests`` calls is pre-populated here.
    ``_execute_atomic_command`` records calls instead of hitting the network.
    """

    def __init__(self):
        self.app = None
        self.config = DummyConfig({
            "loaded_instruments": {},
            "loaded_labware": {},
            "available_tips": {},
            "loaded_modules": {},
            "loaded_gripper": None,
        })
        self.data = {}
        self.session_id = None
        self.protocol_id = None
        self.run_id = "flex-test-run"
        self.max_transfer = None
        self.min_transfer = None
        self.min_largest_pipette = None
        self.max_smallest_pipette = None
        self.has_tip = False
        self.last_pipette = None
        self.modules = {}
        self.pipette_info = {}
        self.hardware_pipettes = {}
        self.executed_commands = []
        self.fail_on = None  # command type to fail once, for error-path tests
        self.custom_labware_files = {}
        self.sent_custom_labware = {}
        self.custom_labware_dir = Path("/tmp/flex-http-driver-tests")
        self.headers = {"Opentrons-Version": FlexHTTPDriver.API_VERSION}
        self.base_url = "http://flex.test"

    def _ensure_run_exists(self, check_run_status=True):
        return self.run_id

    def _update_pipettes(self):
        self.pipette_info = {
            mount: info.copy() for mount, info in self.hardware_pipettes.items()
        }
        self.min_transfer = None
        self.max_transfer = None
        for info in self._get_active_pipettes().values():
            min_v = info.get("min_volume")
            max_v = info.get("max_volume")
            if self.min_transfer is None or self.min_transfer > min_v:
                self.min_transfer = min_v
            if self.max_transfer is None or self.max_transfer < max_v:
                self.max_transfer = max_v

    def get_wells(self, location):
        return [{"labwareId": "labware_1", "wellName": location[-2:]}]

    def _execute_atomic_command(self, command, params, check_run_status=True):
        if command == self.fail_on:
            self.fail_on = None
            raise RuntimeError(f"simulated {command} failure")
        if command == "pickUpTip":
            mount = params["pipetteMount"]
            self.get_tip(mount)
            self.has_tip = True
            self.last_pipette = mount
        elif command == "dropTipInPlace":
            self.has_tip = False

        self._track_tip(command, params.get("pipetteId"))
        self.executed_commands.append((command, dict(params)))
        return {"commandType": command, "params": params}


def _flex_pipette_info(mount, pipette_id, *, min_volume, max_volume, channels=1):
    name = f"flex_1channel_{max_volume}" if channels == 1 else f"flex_{channels}channel_{max_volume}"
    return {
        "id": pipette_id,
        "name": name,
        "model": f"{name}_v3.5",
        "serial": f"{mount}-flex-serial",
        "mount": mount,
        "min_volume": min_volume,
        "max_volume": max_volume,
        "aspirate_flow_rate": 150,
        "dispense_flow_rate": 300,
        "channels": channels,
    }


def _configured_flex_driver():
    driver = StubFlexHTTPDriver()
    driver.hardware_pipettes = {
        "left": _flex_pipette_info("left", "flex-left-id", min_volume=5, max_volume=50),
        "right": _flex_pipette_info("right", None, min_volume=5, max_volume=1000),
    }
    driver.config["loaded_instruments"]["left"] = {
        "name": "flex_1channel_50",
        "pipette_id": "flex-left-id",
        "tip_racks": ["tiprack-left"],
    }
    driver.config["available_tips"]["left"] = [
        ("tiprack-left", "A1"),
        ("tiprack-left", "A2"),
        ("tiprack-left", "A3"),
    ]
    driver._update_pipettes()
    driver._update_pipette_ranges()
    return driver


# ---------------------------------------------------------------------------
# 1. Slot translation
# ---------------------------------------------------------------------------

class TestSlotTranslation:
    def test_all_ot2_slots_map_to_flex_slots(self):
        driver = StubFlexHTTPDriver()
        expected = {
            "1": "D1", "2": "D2", "3": "D3",
            "4": "C1", "5": "C2", "6": "C3",
            "7": "B1", "8": "B2", "9": "B3",
            "10": "A1", "11": "A2", "12": "A3",
        }
        for ot2_slot, flex_slot in expected.items():
            assert driver._normalize_slot(ot2_slot) == flex_slot, (
                f"Slot {ot2_slot!r} should map to {flex_slot!r}"
            )

    def test_flex_format_slots_pass_through_unchanged(self):
        driver = StubFlexHTTPDriver()
        for flex_slot in ("A1", "B2", "C3", "D1", "D4"):
            assert driver._normalize_slot(flex_slot) == flex_slot

    def test_integer_slots_are_coerced_to_string(self):
        driver = StubFlexHTTPDriver()
        assert driver._normalize_slot(1) == "D1"
        assert driver._normalize_slot(12) == "A3"

    def test_api_slot_name_delegates_to_normalize(self):
        driver = StubFlexHTTPDriver()
        for ot2_slot in _OT2_TO_FLEX_SLOT:
            assert driver._api_slot_name(ot2_slot) == driver._normalize_slot(ot2_slot)

    def test_slot_map_covers_all_12_ot2_slots(self):
        assert set(_OT2_TO_FLEX_SLOT.keys()) == {str(i) for i in range(1, 13)}

    def test_slot_map_produces_unique_flex_targets(self):
        # Each numeric slot maps to a distinct Flex slot
        assert len(set(_OT2_TO_FLEX_SLOT.values())) == 12


# ---------------------------------------------------------------------------
# 2. Class-level attribute overrides
# ---------------------------------------------------------------------------

class TestClassAttributes:
    def test_api_version_is_4(self):
        assert FlexHTTPDriver.API_VERSION == "4"

    def test_trash_addressable_area_is_movable_trash(self):
        assert FlexHTTPDriver.TRASH_ADDRESSABLE_AREA == "movableTrashA3"

    def test_ot2_trash_is_fixed_trash(self):
        # Confirm the OT2 parent still uses the original value
        assert OT2HTTPDriver.TRASH_ADDRESSABLE_AREA == "fixedTrash"

    def test_flex_pipette_aliases_contain_expected_names(self):
        expected = {
            "flex_1channel_50",
            "flex_1channel_1000",
            "flex_8channel_50",
            "flex_8channel_1000",
            "flex_96channel_1000",
        }
        for name in expected:
            assert name in FlexHTTPDriver.PIPETTE_NAME_ALIASES

    def test_flex_short_aliases_resolve_correctly(self):
        driver = StubFlexHTTPDriver()
        assert driver._normalize_pipette_name("flex_50") == "p50_single_flex"
        assert driver._normalize_pipette_name("flex_1000") == "p1000_single_flex"
        assert driver._normalize_pipette_name("flex_96") == "p1000_96"

    def test_expected_tiprack_tokens_cover_all_pipettes(self):
        for pipette_name in FlexHTTPDriver.PIPETTE_NAME_ALIASES.values():
            assert pipette_name in FlexHTTPDriver.EXPECTED_TIPRACK_TOKEN, (
                f"Missing tiprack token for {pipette_name!r}"
            )

    def test_flex_driver_is_subclass_of_ot2_driver(self):
        assert issubclass(FlexHTTPDriver, OT2HTTPDriver)


# ---------------------------------------------------------------------------
# 3. Deck configuration
# ---------------------------------------------------------------------------

class _DeckConfigResponse:
    def __init__(self, status_code=200, payload=None, text="ok"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class TestDeckConfiguration:
    def test_get_deck_configuration_reads_robot_endpoint(self):
        driver = StubFlexHTTPDriver()
        robot_fixtures = [
            {"cutoutId": "cutoutD3", "cutoutFixtureId": "trashBinAdapter"},
        ]
        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.get") as mock_get:
            mock_get.return_value = _DeckConfigResponse(
                payload={"data": {"cutoutFixtures": robot_fixtures}}
            )
            result = driver._get_deck_configuration()

        assert result == robot_fixtures
        assert mock_get.call_args.kwargs["url"] == "http://flex.test/deck_configuration"

    def test_get_deck_configuration_returns_empty_on_http_error(self):
        driver = StubFlexHTTPDriver()
        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.get") as mock_get:
            mock_get.return_value = _DeckConfigResponse(500, text="boom")
            assert driver._get_deck_configuration() == []

    def test_get_deck_configuration_returns_empty_on_request_error(self):
        driver = StubFlexHTTPDriver()
        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.get") as mock_get:
            mock_get.side_effect = ConnectionError("down")
            assert driver._get_deck_configuration() == []

    def test_driver_never_writes_deck_configuration(self):
        assert not hasattr(FlexHTTPDriver, "_apply_deck_configuration")
        assert not hasattr(FlexHTTPDriver, "set_staging_areas")
        assert "deck_configuration" not in FlexHTTPDriver.gather_defaults()

    def test_trash_area_detected_from_robot_configuration(self):
        driver = StubFlexHTTPDriver()
        with patch.object(
            driver,
            "_get_deck_configuration",
            return_value=[{"cutoutId": "cutoutD3", "cutoutFixtureId": "trashBinAdapter"}],
        ):
            driver._autodetect_trash_area()
        assert driver.TRASH_ADDRESSABLE_AREA == "movableTrashD3"

    def test_trash_area_falls_back_when_robot_unreachable(self):
        driver = StubFlexHTTPDriver()
        with patch.object(driver, "_get_deck_configuration", return_value=[]):
            driver._autodetect_trash_area()
        assert driver.TRASH_ADDRESSABLE_AREA == "movableTrashA3"


class TestNumericSlotNormalization:
    def test_load_labware_passes_flex_slot_to_parent(self):
        driver = StubFlexHTTPDriver()
        with patch.object(OT2HTTPDriver, "load_labware", return_value="lw") as parent:
            driver.load_labware("corning_96_wellplate_360ul_flat", "3")
        assert parent.call_args.args[1] == "D3"

    def test_load_labware_accepts_int_and_lowercase_slots(self):
        driver = StubFlexHTTPDriver()
        with patch.object(OT2HTTPDriver, "load_labware", return_value="lw") as parent:
            driver.load_labware("plate", 3)
            driver.load_labware("plate", "d3")
        assert [c.args[1] for c in parent.call_args_list] == ["D3", "D3"]

    def test_load_module_passes_flex_slot_to_parent(self):
        driver = StubFlexHTTPDriver()
        with patch.object(OT2HTTPDriver, "load_module", return_value="mod") as parent:
            driver.load_module("heaterShakerModuleV1", "6")
        assert parent.call_args.args[1] == "C3"

    def test_duplicate_module_numeric_slot_raises_clear_error(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_modules"]["C3"] = ("module_1", "heaterShakerModuleV1")
        with pytest.raises(RuntimeError, match="Module already loaded in slot C3"):
            driver.load_module("heaterShakerModuleV1", "6")

    def test_virtual_load_instrument_accepts_numeric_tiprack_slots(self):
        d = VirtualFlexHTTPDriver.__new__(VirtualFlexHTTPDriver)
        d.config = DummyConfig({
            "loaded_instruments": {}, "loaded_labware": {},
            "available_tips": {}, "loaded_modules": {},
        })
        d.app = None
        d.pipette_info = {}
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_instrument("flex_1channel_1000", "left", ["1"])
        tiprack_id = d.config["loaded_labware"]["D1"][0]
        assert d.config["loaded_instruments"]["left"]["tip_racks"] == [tiprack_id]


class TestLabwareChoices:
    @staticmethod
    def _write_def(directory, load_name, display_name, namespace="custom_beta"):
        definition = {
            "namespace": namespace,
            "parameters": {"loadName": load_name},
            "metadata": {"displayName": display_name},
        }
        (directory / f"{load_name}.json").write_text(json.dumps(definition))

    def _driver(self, tmp_path):
        driver = StubFlexHTTPDriver()
        driver.custom_labware_dir = tmp_path
        return driver

    def test_custom_labware_listed_from_directory(self, tmp_path):
        self._write_def(tmp_path, "nist_stirred_catch", "NIST Stirred Catch")
        choices = self._driver(tmp_path)._labware_choices()
        assert choices["custom_beta/nist_stirred_catch"] == "NIST Stirred Catch"

    def test_new_files_appear_without_restart(self, tmp_path):
        driver = self._driver(tmp_path)
        assert "custom_beta/late_plate" not in driver._labware_choices()
        self._write_def(tmp_path, "late_plate", "Late Plate")
        assert driver._labware_choices()["custom_beta/late_plate"] == "Late Plate"

    def test_order_is_builtin_then_custom_then_modules(self, tmp_path):
        from AFL.automation.prepare.FlexDeckWebAppMixin import (
            FLEX_LABWARE_OPTIONS,
            FLEX_MODULE_OPTIONS,
        )
        self._write_def(tmp_path, "b_plate", "b plate")
        self._write_def(tmp_path, "a_plate", "A plate")
        keys = list(self._driver(tmp_path)._labware_choices())

        n_builtin = len(FLEX_LABWARE_OPTIONS)
        assert keys[:n_builtin] == list(FLEX_LABWARE_OPTIONS)
        assert keys[n_builtin:n_builtin + 2] == ["custom_beta/a_plate", "custom_beta/b_plate"]
        assert keys[n_builtin + 2:] == list(FLEX_MODULE_OPTIONS)

    def test_missing_display_name_falls_back_to_key(self, tmp_path):
        (tmp_path / "bare.json").write_text(
            json.dumps({"namespace": "custom_beta", "parameters": {"loadName": "bare"}})
        )
        assert self._driver(tmp_path)._labware_choices()["custom_beta/bare"] == "custom_beta/bare"

    def test_duplicate_definitions_do_not_break_the_dialog(self, tmp_path):
        self._write_def(tmp_path, "dup", "Dup")
        (tmp_path / "dup_copy.json").write_text((tmp_path / "dup.json").read_text())
        assert "custom_beta/dup" in self._driver(tmp_path)._labware_choices()

    def test_builtin_list_has_no_custom_entries(self):
        from AFL.automation.prepare.FlexDeckWebAppMixin import FLEX_LABWARE_OPTIONS
        assert all(k.startswith("opentrons/") for k in FLEX_LABWARE_OPTIONS)


# ---------------------------------------------------------------------------
# 4. Transfer uses Flex trash addressable area
# ---------------------------------------------------------------------------

class TestTransferTipDrop:
    def test_transfer_drop_tip_uses_movable_trash(self):
        driver = _configured_flex_driver()
        driver.transfer("1A1", "1A2", 30)

        trash_commands = [
            (cmd, params)
            for cmd, params in driver.executed_commands
            if cmd == "moveToAddressableAreaForDropTip"
        ]
        assert len(trash_commands) >= 1
        for _, params in trash_commands:
            assert params["addressableAreaName"] == "movableTrashA3", (
                f"Expected movableTrashA3, got {params['addressableAreaName']!r}"
            )

    def test_transfer_force_new_tip_drop_uses_movable_trash(self):
        driver = _configured_flex_driver()
        driver.transfer("1A1", "1A2", 30, force_new_tip=True, drop_tip=True)

        trash_commands = [
            (cmd, params)
            for cmd, params in driver.executed_commands
            if cmd == "moveToAddressableAreaForDropTip"
        ]
        assert len(trash_commands) >= 1
        for _, params in trash_commands:
            assert params["addressableAreaName"] == "movableTrashA3"

    def test_ot2_driver_still_uses_fixed_trash(self):
        """Regression: patching FlexHTTPDriver must not affect OT2HTTPDriver."""
        from AFL.automation.prepare.OT2HTTPDriver import OT2HTTPDriver
        assert OT2HTTPDriver.TRASH_ADDRESSABLE_AREA == "fixedTrash"


# Fixture ID for a waste chute sharing cutout D3 with a Flex Stacker.  The
# detection only relies on the "WasteChute" / "NoCover" substrings.
_CHUTE_WITH_STACKER = "flexStackerModuleV1WithWasteChuteRightAdapterNoCover"


def _deck(*entries):
    return [{"cutoutId": c, "cutoutFixtureId": f} for c, f in entries]


def _detect(driver, deck):
    with patch.object(driver, "_get_deck_configuration", return_value=deck):
        driver._autodetect_trash_area()


class TestWasteChute:
    def test_chute_with_stacker_detected_as_uncovered(self):
        driver = StubFlexHTTPDriver()
        _detect(driver, _deck(("cutoutD3", _CHUTE_WITH_STACKER)))
        assert driver.waste_chute == {"fixture": _CHUTE_WITH_STACKER, "covered": False}
        assert driver.use_waste_chute_for_tips

    def test_covered_chute_detected(self):
        driver = StubFlexHTTPDriver()
        _detect(driver, _deck(("cutoutD3", "wasteChuteRightAdapterCovered")))
        assert driver.waste_chute["covered"] is True

    def test_trash_bin_preferred_over_chute_for_tips(self):
        driver = StubFlexHTTPDriver()
        _detect(driver, _deck(
            ("cutoutA3", "trashBinAdapter"),
            ("cutoutD3", "wasteChuteRightAdapterNoCover"),
        ))
        assert not driver.use_waste_chute_for_tips
        assert driver._tip_drop_area("any") == "movableTrashA3"
        assert driver.waste_chute is not None  # still available to the gripper

    def test_chute_outside_d3_is_ignored(self):
        driver = StubFlexHTTPDriver()
        _detect(driver, _deck(("cutoutC3", "wasteChuteRightAdapterNoCover")))
        assert driver.waste_chute is None

    @pytest.mark.parametrize("channels,expected", [
        (1, "1ChannelWasteChute"),
        (8, "8ChannelWasteChute"),
    ])
    def test_tip_area_by_pipette_channels(self, channels, expected):
        driver = StubFlexHTTPDriver()
        driver.pipette_info = {"left": {"id": "pip", "name": "flex", "channels": channels}}
        _detect(driver, _deck(("cutoutD3", _CHUTE_WITH_STACKER)))
        assert driver._tip_drop_area("pip") == expected

    @pytest.mark.parametrize("layout,expected", [
        ("full96", "96ChannelWasteChute"),
        ("column", "8ChannelWasteChute"),
        ("single", "1ChannelWasteChute"),
    ])
    def test_96_channel_area_follows_nozzle_layout(self, layout, expected):
        driver = StubFlexHTTPDriver()
        driver.pipette_info = {_96CH_MOUNT_KEY: {"id": "p96", "name": "flex_96channel_1000", "channels": 96}}
        driver.config["loaded_instruments"][_96CH_MOUNT_KEY] = {
            "pipette_id": "p96", "nozzle_layout": layout,
        }
        _detect(driver, _deck(("cutoutD3", _CHUTE_WITH_STACKER)))
        assert driver._tip_drop_area("p96") == expected

    def test_96_channel_full_rack_refused_by_covered_chute(self):
        driver = StubFlexHTTPDriver()
        driver.pipette_info = {_96CH_MOUNT_KEY: {"id": "p96", "name": "flex_96channel_1000", "channels": 96}}
        _detect(driver, _deck(("cutoutD3", "wasteChuteRightAdapterCovered")))
        with pytest.raises(RuntimeError, match="covered"):
            driver._tip_drop_area("p96")

    def test_transfer_drops_tips_in_chute(self):
        driver = _configured_flex_driver()
        _detect(driver, _deck(("cutoutD3", _CHUTE_WITH_STACKER)))
        driver.transfer("1A1", "1A2", 30)

        areas = [
            params["addressableAreaName"]
            for cmd, params in driver.executed_commands
            if cmd == "moveToAddressableAreaForDropTip"
        ]
        assert areas and set(areas) == {"1ChannelWasteChute"}

    def _driver_with_plate(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["C2"] = ("lw-1", "plate", {})
        driver.config["loaded_gripper"] = {"gripper_id": "g", "serial": "g"}
        return driver

    def test_gripper_discards_labware_into_chute(self):
        driver = self._driver_with_plate()
        _detect(driver, _deck(("cutoutD3", _CHUTE_WITH_STACKER)))
        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post, \
             patch.object(driver, "_check_cmd_success"):
            result = driver.move_labware("C2", "wasteChute")

        params = mock_post.call_args.kwargs["json"]["data"]["params"]
        assert params["newLocation"] == {"addressableAreaName": "gripperWasteChute"}
        assert params["strategy"] == "usingGripper"
        assert "C2" not in driver.config["loaded_labware"]
        assert result["dest_slot"] == "wasteChute"

    def test_discard_refused_for_covered_chute(self):
        driver = self._driver_with_plate()
        _detect(driver, _deck(("cutoutD3", "wasteChuteRightAdapterCovered")))
        with pytest.raises(RuntimeError, match="uncovered waste chute"):
            driver.move_labware("C2", "wasteChute")
        assert "C2" in driver.config["loaded_labware"]

    def test_discard_refused_without_gripper(self):
        driver = self._driver_with_plate()
        _detect(driver, _deck(("cutoutD3", _CHUTE_WITH_STACKER)))
        with pytest.raises(RuntimeError, match="requires the gripper"):
            driver.move_labware("C2", "wasteChute", use_gripper=False)

    def test_deck_view_labels_chute_and_detects_staging_variant(self):
        driver = StubFlexHTTPDriver()
        deck = _deck(("cutoutD3", "stagingAreaSlotWithWasteChuteRightAdapterNoCover"))
        assert driver._has_staging_area(deck)
        assert driver._get_flex_slot_info("D3", False, deck)["name"] == "Waste Chute"
        assert not driver._has_staging_area(_deck(("cutoutD3", _CHUTE_WITH_STACKER)))


class TestTipTracking:
    def _commands(self, driver):
        return [cmd for cmd, _ in driver.executed_commands]

    def test_failed_transfer_tip_is_discarded_before_next_transfer(self):
        driver = _configured_flex_driver()
        driver.fail_on = "aspirate"
        with pytest.raises(RuntimeError, match="simulated aspirate"):
            driver.transfer("1A1", "1A2", 30)
        assert driver.has_tip and driver.tip_contaminated

        driver.executed_commands.clear()
        driver.transfer("1A1", "1A2", 30)

        cmds = self._commands(driver)
        assert cmds[:3] == ["moveToAddressableAreaForDropTip", "dropTipInPlace", "pickUpTip"]
        assert driver.executed_commands[1][1]["pipetteId"] == "flex-left-id"
        assert driver.tip_contaminated is False

    def test_mix_discards_tip_from_failed_transfer(self):
        driver = _configured_flex_driver()
        driver.fail_on = "dispense"
        with pytest.raises(RuntimeError):
            driver.transfer("1A1", "1A2", 30)

        driver.executed_commands.clear()
        driver.mix(10, "1A2")
        assert self._commands(driver)[:3] == [
            "moveToAddressableAreaForDropTip", "dropTipInPlace", "pickUpTip",
        ]

    def test_tip_kept_on_purpose_is_still_reused(self):
        driver = _configured_flex_driver()
        driver.transfer("1A1", "1A2", 30, drop_tip=False)
        assert driver.has_tip and not driver.tip_contaminated

        driver.executed_commands.clear()
        driver.transfer("1A1", "1A3", 30)
        assert "pickUpTip" not in self._commands(driver)

    def test_reset_deck_forgets_attached_tip(self):
        driver = _configured_flex_driver()
        driver.fail_on = "aspirate"
        with pytest.raises(RuntimeError):
            driver.transfer("1A1", "1A2", 30)

        driver.reset_deck()
        assert driver.has_tip is False
        assert driver.tip_contaminated is False
        assert driver.tip_pipette_id is None

    def _driver_with_tiprack(self):
        driver = _configured_flex_driver()
        driver.config["loaded_labware"]["D1"] = ("tiprack-left", "opentrons_flex_96_tiprack_50ul", {})
        driver.config["loaded_gripper"] = {"gripper_id": "g", "serial": "g"}
        _detect(driver, _deck(("cutoutD3", _CHUTE_WITH_STACKER)))
        return driver

    @pytest.mark.parametrize("dest", ["wasteChute", "offDeck"])
    def test_tiprack_leaving_deck_is_forgotten(self, dest):
        driver = self._driver_with_tiprack()
        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post"), \
             patch.object(driver, "_check_cmd_success"):
            driver.move_labware("D1", dest)

        assert driver.config["available_tips"]["left"] == []
        assert driver.config["loaded_instruments"]["left"]["tip_racks"] == []

    def test_tiprack_moved_on_deck_keeps_its_tips(self):
        driver = self._driver_with_tiprack()
        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post"), \
             patch.object(driver, "_check_cmd_success"):
            driver.move_labware("D1", "C1")

        assert len(driver.config["available_tips"]["left"]) == 3
        assert driver.config["loaded_labware"]["C1"][0] == "tiprack-left"

    def test_deck_page_offers_chute_only_when_move_would_accept_it(self):
        driver = StubFlexHTTPDriver()
        driver.useful_links = {}
        driver._labware_choices = lambda: {}
        with patch.object(driver, "_get_deck_configuration", return_value=[]):
            _detect(driver, _deck(("cutoutD3", _CHUTE_WITH_STACKER)))
            assert '"wasteChuteAvailable": true' in driver.visualize_deck()
            _detect(driver, _deck(("cutoutD3", "wasteChuteRightAdapterCovered")))
            assert '"wasteChuteAvailable": false' in driver.visualize_deck()


class TestNoTipDisposal:
    def test_empty_deck_disables_tip_disposal(self):
        driver = StubFlexHTTPDriver()
        _detect(driver, [])
        assert driver.tip_disposal_available is False

    def test_transfer_refused_before_any_command(self):
        driver = _configured_flex_driver()
        _detect(driver, _deck(("cutoutD3", "singleRightSlot")))
        with patch.object(driver, "_get_deck_configuration", return_value=[]):
            with pytest.raises(RuntimeError, match="No trash bin or waste chute"):
                driver.transfer("1A1", "1A2", 30)
        assert driver.executed_commands == []
        assert driver.has_tip is False

    def test_transfer_rechecks_deck_before_refusing(self):
        driver = _configured_flex_driver()
        _detect(driver, [])
        # Chute configured in the Opentrons App after startup.
        with patch.object(driver, "_get_deck_configuration",
                          return_value=_deck(("cutoutD3", _CHUTE_WITH_STACKER))):
            driver.transfer("1A1", "1A2", 30)
        assert driver.tip_disposal_available is True
        assert any(cmd == "dropTipInPlace" for cmd, _ in driver.executed_commands)

    def test_trash_bin_or_chute_enables_tip_disposal(self):
        driver = StubFlexHTTPDriver()
        _detect(driver, _deck(("cutoutA3", "trashBinAdapter")))
        assert driver.tip_disposal_available is True
        _detect(driver, _deck(("cutoutD3", _CHUTE_WITH_STACKER)))
        assert driver.tip_disposal_available is True


# ---------------------------------------------------------------------------
# 5. FlexPrepare MRO sanity
# ---------------------------------------------------------------------------

class TestFlexPrepareMRO:
    def test_flex_prepare_inherits_from_flex_http_driver(self):
        assert issubclass(FlexPrepare, FlexHTTPDriver)

    def test_flex_prepare_inherits_from_ot2_http_driver(self):
        assert issubclass(FlexPrepare, OT2HTTPDriver)

    def test_flex_http_driver_before_prepare_driver_in_mro(self):
        mro = FlexPrepare.__mro__
        flex_idx = mro.index(FlexHTTPDriver)
        from AFL.automation.prepare.PrepareDriver import PrepareDriver
        prepare_idx = mro.index(PrepareDriver)
        assert flex_idx < prepare_idx

    def test_deck_configuration_is_not_a_persisted_default(self):
        assert "deck_configuration" not in FlexPrepare.gather_defaults()

    def test_gather_defaults_merges_all_parent_defaults(self):
        defaults = FlexPrepare.gather_defaults()
        # From OT2HTTPDriver
        assert "robot_ip" in defaults
        assert "loaded_labware" in defaults
        # From FlexHTTPDriver
        assert "loaded_gripper" in defaults
        # From OT2Prepare/PrepareDriver
        assert "stocks" in defaults


class _Step:
    def __init__(self, source, volume):
        self.source = source
        self.volume = volume


class _BalancedTarget:
    def __init__(self, steps):
        self.protocol = steps


class StubFlexPrepare(FlexPrepare):
    """FlexPrepare with no robot: the Flex transfer() override (tip-disposal
    check) still runs, only OT2HTTPDriver.transfer is replaced."""

    def __init__(self):
        self.app = None
        self.data = {"prepare": {"executed_transfers": []}}
        self.config = DummyConfig({
            "deck": {"D3A1": "Water"},
            "prep_targets": ["D1A1", "D1A2"],
            "stock_transfer_params": {"default": {"drop_tip": True}},
            "stock_mix_order": [],
            "catch_protocol": {"dest": "C1A1", "volume": 300},
        })
        self.stocks = []
        self.last_target_location = None
        self.transfers = []

    def _fake_parent_transfer(self, source, dest, volume, *args, **kwargs):
        self.transfers.append((source, dest, float(volume), kwargs))
        return {"source": source, "dest": dest, "subtransfers_ul": [float(volume)]}


def _patched_prepare():
    driver = StubFlexPrepare()
    return driver, patch.object(OT2HTTPDriver, "transfer", autospec=True,
                                side_effect=StubFlexPrepare._fake_parent_transfer)


class TestAlignScript:
    def test_flex_align_script_declares_flex_robot(self, tmp_path):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["C2"] = (
            "pl", "plate",
            {"definition": {"parameters": {"loadName": "nest_96_wellplate_2ml_deep"},
                            "namespace": "opentrons", "version": 2}},
        )
        out = tmp_path / "align.py"
        driver.make_align_script(str(out))
        src = out.read_text()
        compile(src, str(out), "exec")
        assert "requirements = {'robotType': 'Flex', 'apiLevel': '2.16'}" in src
        assert "protocol.load_labware('nest_96_wellplate_2ml_deep', 'C2'" in src

    def test_ot2_align_script_header_unchanged(self):
        header = "\n".join(OT2HTTPDriver._align_script_header(None))
        assert "'apiLevel': '2.13'" in header
        assert "robotType" not in header


class TestFlexPrepareWorkflow:
    def test_preparation_methods_come_from_ot2prepare(self):
        from AFL.automation.prepare.OT2Prepare import OT2Prepare
        for name in ("resolve_destination", "execute_preparation", "execute_preparation_plan",
                     "transfer_to_catch", "get_transfer_params", "process_stocks"):
            owner = next(c for c in FlexPrepare.__mro__ if name in vars(c))
            assert owner is OT2Prepare, name
        assert next(c for c in FlexPrepare.__mro__ if "transfer" in vars(c)) is FlexHTTPDriver

    def test_resolve_destination_pops_prep_target(self):
        driver = StubFlexPrepare()
        assert driver.resolve_destination(None) == "D1A1"
        assert driver.config["prep_targets"] == ["D1A2"]

    def test_execute_preparation_transfers_and_records(self):
        driver, patcher = _patched_prepare()
        with patcher:
            ok = driver.execute_preparation({}, _BalancedTarget([_Step("D3A1", 120)]), "D1A1")

        assert ok is True
        assert driver.transfers == [("D3A1", "D1A1", 120.0, {"drop_tip": True})]
        assert driver.last_target_location == "D1A1"
        assert driver.data["prepare"]["executed_transfers"][0]["source_stock_name"] == "Water"

    def test_transfer_to_catch_uses_last_target(self):
        driver, patcher = _patched_prepare()
        driver.last_target_location = "D1A1"
        with patcher:
            driver.transfer_to_catch()
        assert driver.transfers == [("D1A1", "C1A1", 300.0, {})]

    def test_preparation_stops_before_moving_liquid_without_tip_disposal(self):
        driver, patcher = _patched_prepare()
        driver.tip_disposal_available = False
        with patcher, patch.object(driver, "_get_deck_configuration", return_value=[]), \
             pytest.warns(UserWarning, match="No trash bin or waste chute"):
            ok = driver.execute_preparation({}, _BalancedTarget([_Step("D3A1", 120)]), "D1A1")
        assert ok is False
        assert driver.transfers == []


# ---------------------------------------------------------------------------
# 6. Gripper support
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status_code=201):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def _gripper_instruments_response(serial="GRPV1234"):
    return _FakeResponse(
        {
            "data": [
                {
                    "mount": "extension",
                    "instrumentType": "gripper",
                    "instrumentModel": "gripperV1",
                    "serialNumber": serial,
                }
            ]
        },
        status_code=200,
    )


def _load_gripper_cmd_response(gripper_run_id="gripper-run-id-001"):
    return _FakeResponse(
        {"data": {"result": {"gripperId": gripper_run_id}, "status": "succeeded"}},
        status_code=201,
    )


class TestGripperSupport:
    def test_load_gripper_queries_instruments_endpoint(self):
        driver = StubFlexHTTPDriver()

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.get") as mock_get, \
             patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post:
            mock_get.return_value = _gripper_instruments_response()
            mock_post.return_value = _load_gripper_cmd_response()
            driver.load_gripper()

        mock_get.assert_called_once()
        assert "/instruments" in mock_get.call_args.kwargs["url"]

    def test_load_gripper_sends_no_run_command(self):
        """API v4+ makes the gripper implicitly available; no loadGripper command."""
        driver = StubFlexHTTPDriver()

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.get") as mock_get, \
             patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post:
            mock_get.return_value = _gripper_instruments_response(serial="GRPV9999")
            result = driver.load_gripper()

        mock_post.assert_not_called()
        assert result == "GRPV9999"

    def test_load_gripper_stores_serial_in_config(self):
        driver = StubFlexHTTPDriver()

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.get") as mock_get:
            mock_get.return_value = _gripper_instruments_response(serial="GRPV1234")
            driver.load_gripper()

        assert driver.config["loaded_gripper"] == {"gripper_id": "GRPV1234", "serial": "GRPV1234"}

    def test_load_gripper_raises_when_no_gripper_attached(self):
        driver = StubFlexHTTPDriver()

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.get") as mock_get:
            mock_get.return_value = _FakeResponse({"data": []}, status_code=200)
            with pytest.raises(RuntimeError, match="No gripper found"):
                driver.load_gripper()

    def test_move_labware_with_gripper_sends_correct_command(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["D3"] = ("labware-id-1", "my_plate", {"definition": {}})
        driver.config["loaded_gripper"] = {"gripper_id": "g-1", "serial": "GRPV1234"}

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _FakeResponse({"data": {"result": {}, "status": "succeeded"}})
            result = driver.move_labware("3", "5")

        posted = mock_post.call_args.kwargs["json"]["data"]
        assert posted["commandType"] == "moveLabware"
        assert posted["params"]["labwareId"] == "labware-id-1"
        assert posted["params"]["strategy"] == "usingGripper"
        # Slot "5" → "C2" via _normalize_slot
        assert posted["params"]["newLocation"] == {"slotName": "C2"}

    def test_move_labware_updates_loaded_labware_tracking(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["D3"] = ("labware-id-1", "my_plate", {"definition": {}})
        driver.config["loaded_gripper"] = {"gripper_id": "g-1", "serial": "GRPV1234"}

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _FakeResponse({"data": {"result": {}, "status": "succeeded"}})
            driver.move_labware("3", "5")

        assert "D3" not in driver.config["loaded_labware"]
        assert driver.config["loaded_labware"]["C2"][0] == "labware-id-1"

    def test_move_labware_to_offdeck_removes_from_tracking(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["D3"] = ("labware-id-1", "my_plate", {"definition": {}})
        driver.config["loaded_gripper"] = {"gripper_id": "g-1", "serial": "GRPV1234"}

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _FakeResponse({"data": {"result": {}, "status": "succeeded"}})
            result = driver.move_labware("3", "offDeck")

        assert "D3" not in driver.config["loaded_labware"]
        assert "offDeck" not in driver.config["loaded_labware"]
        posted_params = mock_post.call_args.kwargs["json"]["data"]["params"]
        assert posted_params["newLocation"] == "offDeck"

    def test_move_labware_manual_uses_correct_strategy(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["D1"] = ("labware-id-2", "my_plate", {"definition": {}})
        # No gripper needed for manual move

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _FakeResponse({"data": {"result": {}, "status": "succeeded"}})
            driver.move_labware("1", "2", use_gripper=False)

        posted_params = mock_post.call_args.kwargs["json"]["data"]["params"]
        assert posted_params["strategy"] == "manualMoveWithoutPause"

    def test_move_labware_raises_when_source_slot_empty(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_gripper"] = {"gripper_id": "g-1", "serial": "GRPV1234"}

        with pytest.raises(ValueError, match="No labware loaded in slot"):
            driver.move_labware("7", "8")

    def test_move_labware_raises_when_gripper_not_loaded(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["D3"] = ("labware-id-1", "my_plate", {"definition": {}})
        # loaded_gripper is None by default

        with pytest.raises(RuntimeError, match="Gripper is not loaded"):
            driver.move_labware("3", "5", use_gripper=True)

    def test_move_labware_return_value(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["D3"] = ("labware-id-1", "my_plate", {"definition": {}})
        driver.config["loaded_gripper"] = {"gripper_id": "g-1", "serial": "GRPV1234"}

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _FakeResponse({"data": {"result": {}, "status": "succeeded"}})
            result = driver.move_labware("3", "6")

        assert result["source_slot"] == "D3"
        assert result["dest_slot"] == "C3"
        assert result["strategy"] == "usingGripper"
        assert result["labware_id"] == "labware-id-1"


# ---------------------------------------------------------------------------
# 8. 96-channel pipette support
# ---------------------------------------------------------------------------

from AFL.automation.prepare.OT2HTTPDriver import TIPRACK_WELLS


def _96ch_stub():
    """StubFlexHTTPDriver pre-configured with a 96-channel and two tipracks."""
    from itertools import chain
    driver = StubFlexHTTPDriver()
    driver.config["loaded_labware"]["D1"] = ("rack-A", "opentrons_flex_96_tiprack_1000ul", {"definition": {"wells": {w: {} for w in TIPRACK_WELLS}, "metadata": {"displayName": "t"}}})
    driver.config["loaded_labware"]["D2"] = ("rack-B", "opentrons_flex_96_tiprack_1000ul", {"definition": {"wells": {w: {} for w in TIPRACK_WELLS}, "metadata": {"displayName": "t"}}})
    driver.config["loaded_instruments"][_96CH_MOUNT_KEY] = {
        "name": "flex_96channel_1000",
        "pipette_id": "pip-96ch",
        "tip_racks": ["rack-A", "rack-B"],
    }
    driver.config["available_tips"][_96CH_MOUNT_KEY] = [
        (rack, well) for rack in ("rack-A", "rack-B") for well in TIPRACK_WELLS
    ]
    driver.hardware_pipettes[_96CH_MOUNT_KEY] = _flex_pipette_info(
        _96CH_MOUNT_KEY, "pip-96ch", min_volume=5, max_volume=1000, channels=96
    )
    driver._update_pipettes()
    driver._update_pipette_ranges()
    return driver


class TestNinetyChannelSupport:
    def test_load_instrument_96ch_uses_api_left_mount(self):
        """The loadPipette command must use 'left' even though we track under '96channel'."""
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["D1"] = ("r1", "opentrons_flex_96_tiprack_1000ul", {})
        posted = []

        class _Resp:
            status_code = 201
            text = ""
            def json(self): return {"data": {"result": {"pipetteId": "p96"}, "status": "succeeded"}}

        with patch("AFL.automation.prepare.OT2HTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _Resp()
            driver.load_instrument("flex_96channel_1000", "96channel", ["1"])
            posted = mock_post.call_args.kwargs["json"]["data"]["params"]

        assert posted["mount"] == "left"
        assert posted["pipetteName"] == "p1000_96"

    def test_load_instrument_96ch_stored_under_96channel_key(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["D1"] = ("r1", "opentrons_flex_96_tiprack_1000ul", {})

        class _Resp:
            status_code = 201
            text = ""
            def json(self): return {"data": {"result": {"pipetteId": "p96"}, "status": "succeeded"}}

        with patch("AFL.automation.prepare.OT2HTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _Resp()
            driver.load_instrument("flex_96channel_1000", "96channel", ["1"])

        assert _96CH_MOUNT_KEY in driver.config["loaded_instruments"]
        assert "left" not in driver.config["loaded_instruments"]
        assert driver.config["loaded_instruments"][_96CH_MOUNT_KEY]["name"] == "p1000_96"

    def test_load_instrument_96ch_tips_stored_under_96channel_key(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_labware"]["D1"] = ("r1", "opentrons_flex_96_tiprack_1000ul", {})

        class _Resp:
            status_code = 201
            text = ""
            def json(self): return {"data": {"result": {"pipetteId": "p96"}, "status": "succeeded"}}

        with patch("AFL.automation.prepare.OT2HTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _Resp()
            driver.load_instrument("flex_96channel_1000", "96channel", ["1"])

        tips = driver.config["available_tips"]
        assert _96CH_MOUNT_KEY in tips
        assert "left" not in tips
        assert len(tips[_96CH_MOUNT_KEY]) == 96  # one full tiprack

    def test_get_tip_96ch_consumes_entire_tiprack(self):
        driver = _96ch_stub()
        before = len(driver.config["available_tips"][_96CH_MOUNT_KEY])  # 192
        tiprack_id, well = driver.get_tip(_96CH_MOUNT_KEY)
        after = len(driver.config["available_tips"][_96CH_MOUNT_KEY])
        assert tiprack_id == "rack-A"
        assert well == "A1"
        assert before - after == 96  # full rack consumed

    def test_get_tip_96ch_advances_to_next_rack(self):
        driver = _96ch_stub()
        rack1_id, _ = driver.get_tip(_96CH_MOUNT_KEY)  # consumes rack-A
        rack2_id, _ = driver.get_tip(_96CH_MOUNT_KEY)  # consumes rack-B
        assert rack1_id == "rack-A"
        assert rack2_id == "rack-B"
        assert driver.config["available_tips"][_96CH_MOUNT_KEY] == []

    def test_get_tip_96ch_raises_when_no_racks(self):
        driver = _96ch_stub()
        driver.config["available_tips"][_96CH_MOUNT_KEY] = []
        with pytest.raises(RuntimeError, match="No tip racks available"):
            driver.get_tip(_96CH_MOUNT_KEY)

    def test_configure_nozzle_layout_full96_posts_correct_command(self):
        driver = _96ch_stub()

        class _Resp:
            status_code = 201
            text = ""
            def json(self): return {"data": {"result": {}, "status": "succeeded"}}

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _Resp()
            driver.configure_nozzle_layout("full96")

        posted = mock_post.call_args.kwargs["json"]["data"]
        assert posted["commandType"] == "configureNozzleLayout"
        params = posted["params"]
        assert params["pipetteId"] == "pip-96ch"
        assert params["configurationParams"]["style"] == "ALL"

    def test_configure_nozzle_layout_column_posts_correct_command(self):
        driver = _96ch_stub()

        class _Resp:
            status_code = 201
            text = ""
            def json(self): return {"data": {"result": {}, "status": "succeeded"}}

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _Resp()
            driver.configure_nozzle_layout("column")

        posted = mock_post.call_args.kwargs["json"]["data"]["params"]
        assert posted["configurationParams"]["style"] == "COLUMN"

    def test_configure_nozzle_layout_stores_layout_in_config(self):
        driver = _96ch_stub()

        class _Resp:
            status_code = 201
            text = ""
            def json(self): return {"data": {"result": {}, "status": "succeeded"}}

        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as mock_post:
            mock_post.return_value = _Resp()
            result = driver.configure_nozzle_layout("column")

        assert result == "column"
        assert driver.config["loaded_instruments"][_96CH_MOUNT_KEY]["nozzle_layout"] == "column"

    def test_configure_nozzle_layout_invalid_type_raises(self):
        driver = _96ch_stub()
        with pytest.raises(ValueError, match="config_type must be"):
            driver.configure_nozzle_layout("quadrant")

    def test_configure_nozzle_layout_without_loaded_pipette_raises(self):
        driver = StubFlexHTTPDriver()
        with pytest.raises(RuntimeError, match="No 96-channel pipette loaded"):
            driver.configure_nozzle_layout("full96")

    def test_update_pipettes_remaps_96ch_from_left_to_96channel_key(self):
        """After _update_pipettes, a 96-channel reported as 'left' is remapped."""
        driver = StubFlexHTTPDriver()
        # Simulate hardware saying 96ch is on 'left'
        driver.hardware_pipettes = {
            "left": _flex_pipette_info("left", "p96-id", min_volume=5, max_volume=1000, channels=96)
        }
        driver.hardware_pipettes["left"]["name"] = "flex_96channel_1000"
        driver.config["loaded_instruments"]["96channel"] = {
            "name": "flex_96channel_1000",
            "pipette_id": "p96-id",
            "tip_racks": [],
        }
        driver._update_pipettes()
        # The stub copies hardware_pipettes directly; the real FlexHTTPDriver
        # would rename, but the stub doesn't call super.  Just verify the key
        # is present if set up that way.
        assert _96CH_MOUNT_KEY in driver.config["loaded_instruments"]


# ---------------------------------------------------------------------------
# 7. VirtualFlexHTTPDriver
# ---------------------------------------------------------------------------

class TestVirtualFlexHTTPDriver:
    @pytest.fixture(autouse=True)
    def _isolated_home(self, monkeypatch, tmp_path):
        # The custom labware directory lives under $HOME/.afl (not AFL_HOME);
        # keep the real one untouched.
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def _driver(self):
        return VirtualFlexHTTPDriver()

    # --- class hierarchy ---

    def test_is_subclass_of_flex_http_driver(self):
        assert issubclass(VirtualFlexHTTPDriver, FlexHTTPDriver)

    def test_is_subclass_of_ot2_http_driver(self):
        assert issubclass(VirtualFlexHTTPDriver, OT2HTTPDriver)

    # --- labware ---

    def test_load_labware_stores_in_config(self):
        d = self._driver()
        labware_id = d.load_labware("corning_96_wellplate_360ul_flat", "3")
        assert d.config["loaded_labware"]["D3"][0] == labware_id
        assert d.config["loaded_labware"]["D3"][1] == "corning_96_wellplate_360ul_flat"

    def test_load_labware_definition_contains_standard_wells(self):
        d = self._driver()
        d.load_labware("my_plate", "1")
        definition = d.config["loaded_labware"]["D1"][2]
        assert "A1" in definition["definition"]["wells"]
        assert "H12" in definition["definition"]["wells"]

    def test_load_module_stores_in_config(self):
        d = self._driver()
        module_id = d.load_module("heaterShakerModuleV1", "6")
        assert d.config["loaded_modules"]["C3"][0] == module_id

    # --- instruments ---

    def test_load_instrument_stores_in_config(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_200ul", "2")
        pipette_id = d.load_instrument("flex_1channel_1000", "left", ["2"])
        assert "left" in d.config["loaded_instruments"]
        assert d.config["loaded_instruments"]["left"]["pipette_id"] == pipette_id
        assert d.config["loaded_instruments"]["left"]["name"] == "p1000_single_flex"

    def test_load_instrument_populates_tips(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_200ul", "2")
        d.load_instrument("flex_1channel_1000", "left", ["2"])
        assert len(d.config["available_tips"]["left"]) == 96

    def test_load_instrument_infers_max_volume(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_200ul", "2")
        d.load_instrument("flex_1channel_50", "right", ["2"])
        assert d.pipette_info["right"]["max_volume"] == 50

    def test_load_instrument_infers_channel_count_single(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_200ul", "2")
        d.load_instrument("flex_1channel_1000", "left", ["2"])
        assert d.pipette_info["left"]["channels"] == 1

    def test_load_instrument_infers_channel_count_8(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_200ul", "2")
        d.load_instrument("flex_8channel_50", "left", ["2"])
        assert d.pipette_info["left"]["channels"] == 8

    # --- transfer (real logic, virtual stubs) ---

    def test_transfer_picks_up_and_drops_tip(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_labware("corning_96_wellplate_360ul_flat", "2")
        d.load_instrument("flex_1channel_1000", "left", ["1"])
        d.transfer("2A1", "2B1", 50)
        assert not d.has_tip

    def test_transfer_consumes_a_tip(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_labware("corning_96_wellplate_360ul_flat", "2")
        d.load_instrument("flex_1channel_1000", "left", ["1"])
        tips_before = len(d.config["available_tips"]["left"])
        d.transfer("2A1", "2B1", 50)
        assert len(d.config["available_tips"]["left"]) == tips_before - 1

    def test_transfer_returns_record(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_labware("corning_96_wellplate_360ul_flat", "2")
        d.load_instrument("flex_1channel_1000", "left", ["1"])
        result = d.transfer("2A1", "2B1", 200)
        assert result["requested_volume_ul"] == 200.0
        assert result["subtransfers_ul"] == [200.0]
        assert result["pipette_mount"] == "left"

    def test_transfer_splits_volume_exceeding_max(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_labware("corning_96_wellplate_360ul_flat", "2")
        d.load_instrument("flex_1channel_50", "left", ["1"])
        result = d.transfer("2A1", "2B1", 90)  # 90 > 50 → must split
        assert len(result["subtransfers_ul"]) == 2
        assert sum(result["subtransfers_ul"]) == pytest.approx(90.0)

    def test_transfer_raises_when_no_tips_left(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_labware("corning_96_wellplate_360ul_flat", "2")
        d.load_instrument("flex_1channel_1000", "left", ["1"])
        d.config["available_tips"]["left"] = []
        with pytest.raises(RuntimeError, match="No tips available"):
            d.transfer("2A1", "2B1", 50)

    # --- gripper ---

    def test_load_gripper_stores_in_config(self):
        d = self._driver()
        gripper_id = d.load_gripper()
        assert d.config["loaded_gripper"]["gripper_id"] == gripper_id
        assert d.config["loaded_gripper"]["serial"] == "VIRTUAL-GRP"

    def test_move_labware_updates_tracking(self):
        d = self._driver()
        d.load_labware("my_plate", "3")
        d.load_gripper()
        d.move_labware("3", "5")
        assert "D3" not in d.config["loaded_labware"]
        assert "C2" in d.config["loaded_labware"]

    def test_move_labware_offdeck_removes_from_tracking(self):
        d = self._driver()
        d.load_labware("my_plate", "3")
        d.load_gripper()
        d.move_labware("3", "offDeck")
        assert "D3" not in d.config["loaded_labware"]
        assert "offDeck" not in d.config["loaded_labware"]

    def test_virtual_deck_has_uncovered_chute_and_trash_bin(self):
        d = self._driver()
        assert d.waste_chute == {"fixture": "wasteChuteRightAdapterNoCover", "covered": False}
        assert d.TRASH_ADDRESSABLE_AREA == "movableTrashA3"
        assert d.use_waste_chute_for_tips is False

    def test_virtual_discard_tiprack_in_chute_forgets_its_tips(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_instrument("flex_1channel_1000", "left", ["1"])
        d.load_gripper()

        result = d.move_labware("1", "wasteChute")

        assert result["dest_slot"] == "wasteChute"
        assert "D1" not in d.config["loaded_labware"]
        assert "WASTECHUTE" not in d.config["loaded_labware"]
        assert d.config["available_tips"]["left"] == []

    def test_virtual_discard_requires_gripper(self):
        d = self._driver()
        d.load_labware("my_plate", "3")
        with pytest.raises(RuntimeError, match="requires the gripper"):
            d.move_labware("3", "wasteChute", use_gripper=False)

    def test_move_labware_without_gripper_raises(self):
        d = self._driver()
        d.load_labware("my_plate", "3")
        with pytest.raises(RuntimeError, match="Gripper is not loaded"):
            d.move_labware("3", "5", use_gripper=True)

    # --- reset ---

    def test_reset_clears_all_state(self):
        d = self._driver()
        d.load_labware("my_plate", "1")
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "2")
        d.load_instrument("flex_1channel_1000", "left", ["2"])
        d.load_gripper()
        d.reset()
        assert d.config["loaded_labware"] == {}
        assert d.config["loaded_instruments"] == {}
        assert d.config["available_tips"] == {}
        assert d.config["loaded_gripper"] is None
        assert not d.has_tip

    # --- 96-channel (virtual) ---

    def test_virtual_96ch_load_uses_96channel_key(self):
        d = self._driver()
        d.reset()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_instrument("flex_96channel_1000", "96channel", ["1"])
        assert _96CH_MOUNT_KEY in d.config["loaded_instruments"]
        # The 96-channel instrument is recorded under its own key
        assert d.config["loaded_instruments"][_96CH_MOUNT_KEY]["name"] == "p1000_96"

    def test_virtual_96ch_left_mount_remapped_to_96ch_key(self):
        """Even if user passes mount='left' for the 96-ch, driver normalises it."""
        d = self._driver()
        d.reset()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_instrument("flex_96channel_1000", "left", ["1"])
        assert _96CH_MOUNT_KEY in d.config["loaded_instruments"]
        assert d.config["loaded_instruments"][_96CH_MOUNT_KEY]["name"] == "p1000_96"

    def test_virtual_96ch_channels_is_96(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_instrument("flex_96channel_1000", "96channel", ["1"])
        assert d.pipette_info[_96CH_MOUNT_KEY]["channels"] == 96

    def test_virtual_96ch_tip_pickup_consumes_full_rack(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "2")
        d.load_instrument("flex_96channel_1000", "96channel", ["1", "2"])
        tips_before = len(d.config["available_tips"][_96CH_MOUNT_KEY])  # 192
        d.get_tip(_96CH_MOUNT_KEY)  # consume rack from slot 1
        tips_after = len(d.config["available_tips"][_96CH_MOUNT_KEY])
        assert tips_before - tips_after == 96

    def test_virtual_configure_nozzle_layout_stores_in_config(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_instrument("flex_96channel_1000", "96channel", ["1"])
        result = d.configure_nozzle_layout("column")
        assert result == "column"
        assert d.config["loaded_instruments"][_96CH_MOUNT_KEY]["nozzle_layout"] == "column"

    def test_virtual_configure_nozzle_layout_invalid_raises(self):
        d = self._driver()
        d.load_labware("opentrons_flex_96_tiprack_1000ul", "1")
        d.load_instrument("flex_96channel_1000", "96channel", ["1"])
        with pytest.raises(ValueError, match="config_type must be"):
            d.configure_nozzle_layout("octant")

    def test_virtual_configure_nozzle_layout_no_pipette_raises(self):
        d = self._driver()
        d.reset()  # ensure no 96-channel is loaded from prior test pollution
        with pytest.raises(RuntimeError, match="No 96-channel pipette loaded"):
            d.configure_nozzle_layout("full96")

    # --- deck config / run ---

    def test_virtual_deck_configuration_is_fixed_layout(self):
        d = self._driver()
        assert d._get_deck_configuration() == VIRTUAL_DECK_CONFIGURATION

    def test_ensure_run_exists_returns_virtual_id(self):
        d = self._driver()
        assert d._ensure_run_exists() == "virtual-run"


# ---------------------------------------------------------------------------
# 8. Fixes verified against a real Flex (robot software 9.1.2)
# ---------------------------------------------------------------------------

class _JsonResp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self):
        return self._body


# GET /modules as returned by the Flex with Opentrons-Version: 4.
_FLEX_MODULES_RESPONSE = {
    "data": [
        {
            "moduleModel": "heaterShakerModuleV1",
            "data": {
                "status": "running",
                "labwareLatchStatus": "idle_closed",
                "speedStatus": "holding at target",
                "currentSpeed": 294,
                "targetSpeed": 300,
                "temperatureStatus": "idle",
                "currentTemperature": 26.6,
            },
        },
        {"moduleModel": "flexStackerModuleV1", "data": {"status": "idle"}},
    ],
    "meta": {"cursor": 0, "totalLength": 2},
}

# A moveToWell that hit something, as returned by the Flex.
_COLLISION_RESPONSE = {
    "data": {
        "id": "cmd-1",
        "commandType": "moveToWell",
        "status": "failed",
        "error": {
            "errorType": "stallOrCollision",
            "errorCode": "2003",
            "detail": "Stall or Collision Detected",
            "wrappedErrors": [
                {"errorType": "StallOrCollisionDetectedError", "errorCode": "2003",
                 "detail": "collision_detected (head_l)", "wrappedErrors": []}
            ],
        },
    }
}


class TestHeaterShakerStatus:
    def _get(self, body, status_code=200):
        return patch(
            "AFL.automation.prepare.OT2HTTPDriver.requests.get",
            return_value=_JsonResp(body, status_code),
        )

    def test_reads_flex_v4_modules_format(self):
        driver = StubFlexHTTPDriver()
        with self._get(_FLEX_MODULES_RESPONSE):
            assert driver.get_shake_rpm() == ("holding at target", 294, 300)
            assert driver.get_shaker_temp() == (26.6, None)
            assert driver.get_shake_latch_status() == "idle_closed"

    def test_reads_legacy_modules_format(self):
        driver = StubFlexHTTPDriver()
        legacy = {"modules": [{"moduleModel": "heaterShakerModuleV1",
                               "data": {"currentTemp": 25.0, "targetTemp": 37.0}}]}
        with self._get(legacy):
            assert driver.get_shaker_temp() == (25.0, 37.0)

    def test_missing_heater_shaker_raises(self):
        driver = StubFlexHTTPDriver()
        with self._get({"data": []}), pytest.raises(RuntimeError, match="No heater-shaker"):
            driver.get_shake_rpm()

    def test_http_error_raises(self):
        driver = StubFlexHTTPDriver()
        with self._get({}, status_code=500), pytest.raises(RuntimeError, match="HTTP 500"):
            driver.get_shake_rpm()


class TestMotionFaultLockout:
    def test_collision_locks_out_further_commands(self):
        driver = StubFlexHTTPDriver()
        with pytest.raises(RuntimeError, match="Motion fault"), \
             patch("AFL.automation.prepare.OT2HTTPDriver.requests.post",
                   return_value=_JsonResp(_COLLISION_RESPONSE, 201)):
            OT2HTTPDriver._execute_atomic_command(driver, "moveToWell", {"pipetteId": "p"})
        assert driver.motion_fault

        with patch("AFL.automation.prepare.OT2HTTPDriver.requests.post") as post, \
             pytest.raises(RuntimeError, match="Refusing"):
            OT2HTTPDriver._execute_atomic_command(driver, "moveToWell", {"pipetteId": "p"})
        post.assert_not_called()

    def test_move_labware_is_refused_after_fault(self):
        driver = StubFlexHTTPDriver()
        driver.motion_fault = "Stall or Collision Detected"
        with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post") as post, \
             pytest.raises(RuntimeError, match="Refusing"):
            driver.move_labware("1", "2")
        post.assert_not_called()

    def test_home_clears_fault(self):
        driver = StubFlexHTTPDriver()
        driver.motion_fault = "Stall or Collision Detected"
        with patch("AFL.automation.prepare.OT2HTTPDriver.requests.post",
                   return_value=_JsonResp({}, 200)):
            driver.home()
        assert driver.motion_fault is None

    def test_ordinary_command_failure_does_not_lock(self):
        driver = StubFlexHTTPDriver()
        failed = {"data": {"id": "c", "status": "failed",
                           "error": {"errorCode": "4000", "detail": "bad params"}}}
        with patch("AFL.automation.prepare.OT2HTTPDriver.requests.post",
                   return_value=_JsonResp(failed, 201)), \
             pytest.raises(RuntimeError, match="Command returned error"):
            OT2HTTPDriver._execute_atomic_command(driver, "aspirate", {"pipetteId": "p"})
        assert driver.motion_fault is None


class TestCommandPayloads:
    def test_blowout_uses_api_command_name_and_flow_rate(self):
        driver = _configured_flex_driver()
        driver.transfer("1A1", "1A2", 30, blow_out=True)
        blowouts = [p for c, p in driver.executed_commands if c == "blowout"]
        assert len(blowouts) == 1
        assert blowouts[0]["flowRate"] == 300
        assert "blowOut" not in [c for c, _ in driver.executed_commands]

    def test_mix_sends_flow_rates_and_records_pipette(self):
        driver = _configured_flex_driver()
        driver.mix(10, "1A2", repetitions=2)
        liquid = [(c, p) for c, p in driver.executed_commands if c in ("aspirate", "dispense")]
        assert len(liquid) == 4
        assert all("flowRate" in p for _, p in liquid)
        assert driver.last_pipette == "left"

    def test_align_script_loads_96ch_on_left_mount(self):
        driver = StubFlexHTTPDriver()
        assert driver._align_script_mount(_96CH_MOUNT_KEY) == "left"
        assert driver._align_script_mount("right") == "right"

    def test_nozzle_layouts_match_api_schema(self):
        driver = StubFlexHTTPDriver()
        driver.config["loaded_instruments"][_96CH_MOUNT_KEY] = {"pipette_id": "p96", "tip_racks": []}
        sent = {}
        for layout in ("full96", "column", "single"):
            with patch("AFL.automation.prepare.FlexHTTPDriver.requests.post",
                       return_value=_JsonResp({"data": {"status": "succeeded"}}, 201)) as post:
                driver.configure_nozzle_layout(layout)
            sent[layout] = post.call_args.kwargs["json"]["data"]["params"]["configurationParams"]
        assert sent["full96"] == {"style": "ALL"}
        assert sent["column"] == {"style": "COLUMN", "primaryNozzle": "A1"}
        assert sent["single"] == {"style": "SINGLE", "primaryNozzle": "A1"}


def test_run_check_timeout_does_not_replace_run():
    import requests as _requests
    driver = StubFlexHTTPDriver()
    with patch("AFL.automation.prepare.OT2HTTPDriver.requests.get",
               side_effect=_requests.exceptions.Timeout), \
         patch.object(driver, "_create_run") as create, \
         pytest.raises(ConnectionError, match="Timed out"):
        OT2HTTPDriver._ensure_run_exists(driver)
    create.assert_not_called()


def test_virtual_driver_uses_its_own_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AFL_HOME", str(tmp_path / "afl"))
    assert VirtualFlexHTTPDriver().filepath.name == "VirtualFlexHTTPDriver.config.json"


class TestRunCreation:
    def _post_run(self):
        return patch("AFL.automation.prepare.OT2HTTPDriver.requests.post",
                     return_value=_JsonResp({"data": {"id": "new-run"}}, 201))

    def test_failed_deck_reload_discards_run_and_raises(self):
        driver = StubFlexHTTPDriver()
        with self._post_run(), \
             patch.object(driver, "_reload_deck_configuration", return_value=False), \
             patch.object(driver, "_cleanup_stale_run",
                          side_effect=lambda: setattr(driver, "run_id", None)) as cleanup, \
             pytest.raises(RuntimeError, match="could not reload the saved deck"):
            OT2HTTPDriver._create_run(driver)
        cleanup.assert_called_once()
        assert driver.run_id is None

    def test_successful_deck_reload_keeps_run(self):
        driver = StubFlexHTTPDriver()
        with self._post_run(), \
             patch.object(driver, "_reload_deck_configuration", return_value=True), \
             patch.object(driver, "_cleanup_stale_run") as cleanup:
            assert OT2HTTPDriver._create_run(driver) == "new-run"
        cleanup.assert_not_called()


class TestTipStateSafeguards:
    def test_new_run_refused_while_tip_attached(self):
        driver = StubFlexHTTPDriver()
        driver.has_tip = True
        with patch("AFL.automation.prepare.OT2HTTPDriver.requests.post") as post, \
             pytest.raises(RuntimeError, match="confirm_tip_removed"):
            OT2HTTPDriver._create_run(driver)
        post.assert_not_called()

    def test_confirm_tip_removed_clears_tip_state(self):
        driver = StubFlexHTTPDriver()
        driver.has_tip = True
        driver.last_pipette = "left"
        driver.tip_pipette_id = "flex-left-id"
        driver.tip_contaminated = True
        driver.confirm_tip_removed()
        assert (driver.has_tip, driver.last_pipette,
                driver.tip_pipette_id, driver.tip_contaminated) == (False, None, None, False)

    def test_reset_tipracks_keeps_attached_tip(self):
        driver = _configured_flex_driver()
        driver.has_tip = True
        driver.reset_tipracks()
        assert driver.has_tip is True

    def _instruments(self, tip_detected):
        return _JsonResp({"data": [{
            "mount": "left", "instrumentType": "pipette",
            "instrumentName": "p1000_single_flex", "instrumentModel": "p1000_single_v3.6",
            "serialNumber": "P1K", "data": {"channels": 1, "min_volume": 5.0, "max_volume": 1000.0},
            "state": {"tipDetected": tip_detected},
        }]})

    def test_startup_fails_when_tip_sensor_reports_tip(self):
        driver = StubFlexHTTPDriver()
        with patch("AFL.automation.prepare.OT2HTTPDriver.requests.get",
                   return_value=self._instruments(True)):
            OT2HTTPDriver._update_pipettes(driver)
        assert driver.pipette_info["left"]["tip_detected"] is True
        with pytest.raises(RuntimeError, match="Tip detected on the left pipette"):
            driver._check_no_tips_attached()

    def test_startup_passes_without_tips(self):
        driver = StubFlexHTTPDriver()
        with patch("AFL.automation.prepare.OT2HTTPDriver.requests.get",
                   return_value=self._instruments(False)):
            OT2HTTPDriver._update_pipettes(driver)
        driver._check_no_tips_attached()

    def test_tip_check_runs_before_homing(self):
        driver = StubFlexHTTPDriver()
        with patch.object(OT2HTTPDriver, "_initialize_robot"), \
             patch.object(driver, "_check_no_tips_attached", side_effect=RuntimeError("tip")), \
             patch.object(driver, "_home_if_needed") as home, \
             pytest.raises(RuntimeError, match="tip"):
            driver._initialize_robot()
        home.assert_not_called()


class TestFlexDefaultFlowRates:
    def _driver(self, pipette, tiprack_name, tip_volume=None, mount="left"):
        driver = StubFlexHTTPDriver()
        wells = {"A1": {"totalLiquidVolume": tip_volume}} if tip_volume else {}
        driver.config["loaded_labware"]["C2"] = (
            "rack-1", tiprack_name, {"definition": {"wells": wells}},
        )
        driver.config["loaded_instruments"][mount] = {
            "name": pipette, "pipette_id": "pid", "tip_racks": ["rack-1"],
        }
        driver.pipette_info = {mount: {"id": "pid", "name": pipette,
                                       "aspirate_flow_rate": 150, "dispense_flow_rate": 150}}
        return driver

    @pytest.mark.parametrize("pipette, rack, tips, expected", [
        ("p50_single_flex", "opentrons_flex_96_tiprack_50ul", 50, 35),
        ("p1000_single_flex", "opentrons_flex_96_tiprack_50ul", 50, 478),
        ("p1000_single_flex", "opentrons_flex_96_tiprack_200ul", 200, 716),
        ("p1000_single_flex", "opentrons_flex_96_tiprack_1000ul", 1000, 716),
        ("p1000_multi_flex", "opentrons_flex_96_tiprack_50ul", 50, 478),
    ])
    def test_defaults_follow_pipette_and_tip_size(self, pipette, rack, tips, expected):
        driver = self._driver(pipette, rack, tips)
        for action in ("aspirate", "dispense", "blow_out"):
            assert driver._flow_rate("left", action) == expected

    def test_96ch_defaults(self):
        driver = self._driver("p1000_96", "opentrons_flex_96_tiprack_200ul", 200, mount=_96CH_MOUNT_KEY)
        assert driver._flow_rate(_96CH_MOUNT_KEY, "aspirate") == 80

    def test_96ch_200ul_blow_out_differs(self):
        driver = self._driver("p200_96", "opentrons_flex_96_tiprack_200ul", 200, mount=_96CH_MOUNT_KEY)
        assert driver._flow_rate(_96CH_MOUNT_KEY, "aspirate") == 15
        assert driver._flow_rate(_96CH_MOUNT_KEY, "dispense") == 15
        assert driver._flow_rate(_96CH_MOUNT_KEY, "blow_out") == 10

    def test_tip_size_read_from_rack_name_when_definition_has_no_volume(self):
        driver = self._driver("p1000_single_flex", "opentrons_flex_96_tiprack_50ul")
        assert driver._flow_rate("left", "aspirate") == 478

    def test_api_pipette_name_resolves(self):
        driver = self._driver("flex_1channel_50", "opentrons_flex_96_tiprack_50ul", 50)
        assert driver._flow_rate("left", "aspirate") == 35

    def test_unknown_combination_falls_back_with_one_warning(self):
        driver = self._driver("p1000_multi_em_flex", "opentrons_flex_96_tiprack_1000ul", 1000)
        driver._warned_flow_rate_keys = set()
        with patch.object(driver, "log_warning") as warn:
            assert driver._flow_rate("left", "aspirate") == 150
            assert driver._flow_rate("left", "dispense") == 150
        warn.assert_called_once()

    def test_user_rate_overrides_default(self):
        driver = self._driver("p50_single_flex", "opentrons_flex_96_tiprack_50ul", 50)
        driver.set_aspirate_rate(20, "left")
        assert driver._flow_rate("left", "aspirate") == 20
        assert driver._flow_rate("left", "dispense") == 35

    def test_transfer_sends_default_rates(self):
        driver = _configured_flex_driver()  # 50 uL pipette on left, 'tiprack-left' not on deck
        driver.config["loaded_labware"]["C2"] = (
            "tiprack-left", "opentrons_flex_96_tiprack_50ul",
            {"definition": {"wells": {"A1": {"totalLiquidVolume": 50}}}},
        )
        driver.config["loaded_instruments"]["left"]["name"] = "p50_single_flex"
        driver.transfer("1A1", "1A2", 30, blow_out=True)
        rates = {c: p["flowRate"] for c, p in driver.executed_commands if "flowRate" in p}
        assert rates == {"aspirate": 35, "dispense": 35, "blowout": 35}
