"""
FlexHTTPDriver — Opentrons Flex (OT-3) support for AFL.

The Flex uses the same HTTP API base as the OT2 but with key differences:

* API version header: ``Opentrons-Version: 4``
* Deck slots: alphanumeric ``A1–D3`` (and staging ``A4–D4``) instead of
  numeric ``1–12``.
* Pipette names: ``flex_1channel_50``, ``flex_1channel_1000``, etc.
* Deck configuration is a robot-level setting (not per-run), set up in the
  Opentrons App; this driver only reads it.
* Trash is a configurable fixture (trash bin or waste chute), not a fixed
  location at slot 12.
"""

import requests

from AFL.automation.APIServer.Driver import Driver
from AFL.automation.prepare.OT2HTTPDriver import OT2HTTPDriver
from AFL.automation.prepare.FlexDeckWebAppMixin import FlexDeckWebAppMixin

# ---------------------------------------------------------------------------
# Slot translation table: OT2 numeric → Flex alphanumeric
# The Flex deck is a 4×3 grid.  Row D is at the front (≈ OT2 rows 1–3),
# row A is at the back (≈ OT2 rows 10–12).
# Staging slots A4–D4 are Flex-native and pass through unchanged.
# ---------------------------------------------------------------------------
_OT2_TO_FLEX_SLOT = {
    "1":  "D1", "2":  "D2", "3":  "D3",
    "4":  "C1", "5":  "C2", "6":  "C3",
    "7":  "B1", "8":  "B2", "9":  "B3",
    "10": "A1", "11": "A2", "12": "A3",
}

# Staging-area slots are column-4 slots reachable only by the gripper.
_STAGING_SLOTS = {"A4", "B4", "C4", "D4"}

# The waste chute can only be installed in cutout D3.  Its fixture ID varies
# (cover or no cover, combined with a staging area or a Flex Stacker), so it is
# detected by substring rather than by exact ID.
_WASTE_CHUTE_CUTOUT = "cutoutD3"

# Waste-chute addressable area for tip drops, by number of active nozzles.
_WASTE_CHUTE_TIP_AREAS = {
    1: "1ChannelWasteChute",
    8: "8ChannelWasteChute",
    96: "96ChannelWasteChute",
}

# Active nozzle count for each 96-channel nozzle layout (see configure_nozzle_layout).
_NOZZLE_LAYOUT_CHANNELS = {"full96": 96, "column": 8, "single": 1}

# Canonical config key used for the 96-channel pipette.  The Opentrons HTTP API
# addresses it as "left" mount, but storing it under a distinct key prevents
# collision with an independent left-mount single-channel pipette.
_96CH_MOUNT_KEY = "96channel"


def _is_96_channel(pipette_name):
    """Return True for a 96-channel pipette name.

    Matches hardware names (``p1000_96``, ``p200_96``) as reported by
    ``GET /instruments`` and as produced by ``PIPETTE_NAME_ALIASES``, and the
    Python Protocol API names (``flex_96channel_1000``).
    """
    name = str(pipette_name or "")
    return name.endswith("_96") or "96channel" in name


class FlexHTTPDriver(FlexDeckWebAppMixin, OT2HTTPDriver):
    """Driver for the Opentrons Flex (OT-3) robot.

    Subclasses :class:`OT2HTTPDriver` and overrides only the parts that differ
    between the OT2 and the Flex HTTP API.

    Parameters
    ----------
    overrides : dict, optional
        Configuration overrides passed through to :class:`Driver`.

    Deck configuration
    ------------------
    The deck configuration (trash bin, staging areas, module fixtures) is
    owned by the robot and set up in the Opentrons App.  The driver never
    writes it; it only reads ``GET /deck_configuration`` via
    :meth:`_get_deck_configuration` to locate the trash bin and to render
    the deck view.  Modules that need a fixture (heater-shaker, magnetic
    block, thermocycler, absorbance reader) must already be configured on
    the robot before :meth:`load_module` is called.
    """

    ROBOT_TYPE = "OT-3"
    API_VERSION = "4"

    # When dropping tips the Flex uses a movable trash bin, not the OT2's
    # fixed-position trash at slot 12.  The addressable area name depends on
    # which cutout the trash bin is placed in; cutoutA3 → movableTrashA3.
    # It is detected from the robot's deck configuration at startup.
    TRASH_ADDRESSABLE_AREA = "movableTrashA3"

    # Waste chute detected at startup: None, or {"fixture": <id>, "covered": bool}.
    # Tips go to the chute only when no trash bin is configured.
    waste_chute = None
    use_waste_chute_for_tips = False
    # False when the deck has neither a trash bin nor a waste chute; transfer()
    # then refuses to start rather than failing at the tip drop.
    tip_disposal_available = True

    # Pipette currently holding a tip (set on pickUpTip, cleared on dropTipInPlace),
    # and whether that tip must be discarded because the transfer using it failed.
    tip_pipette_id = None
    tip_contaminated = False

    PIPETTE_NAME_ALIASES = {
        # --- Hardware-Level Names (Pass through cleanly) ---
        "p50_single_flex":      "p50_single_flex",
        "p1000_single_flex":    "p1000_single_flex",
        "p50_multi_flex":       "p50_multi_flex",
        "p1000_multi_flex":     "p1000_multi_flex",
        "p1000_96":             "p1000_96",
        "p200_96":              "p200_96",
        "p1000_multi_em_flex":  "p1000_multi_em_flex",

        # --- Python Protocol API Names (Translated to Hardware Names) ---
        "flex_1channel_50":    "p50_single_flex",
        "flex_1channel_1000":  "p1000_single_flex",
        "flex_8channel_50":    "p50_multi_flex",
        "flex_8channel_1000":  "p1000_multi_flex",
        "flex_96channel_1000": "p1000_96",
        "flex_96channel_200":  "p200_96",

        # --- Convenient Shorthand Aliases ---
        "flex_50":              "p50_single_flex",
        "flex_1000":            "p1000_single_flex",
        "flex_8_50":            "p50_multi_flex",
        "flex_8_1000":          "p1000_multi_flex",
        "flex_96":              "p1000_96",
        "flex_96_200":          "p200_96",
    }

    EXPECTED_TIPRACK_TOKEN = {
        # Keyed to the resolved hardware names
        "p50_single_flex":      "50ul",
        "p1000_single_flex":    "1000ul",
        "p50_multi_flex":       "50ul",
        "p1000_multi_flex":     "1000ul",
        "p1000_96":             "1000ul",
        "p200_96":              "200ul",
        "p1000_multi_em_flex":  "1000ul",
    }
    # Maps a trashBinAdapter cutout ID to the addressable area name used when
    # dropping tips.  Covers all 8 non-center column positions where the trash
    # bin can legally be placed on the Flex deck.
    _TRASH_CUTOUT_TO_AREA = {
        "cutoutA1": "movableTrashA1",
        "cutoutB1": "movableTrashB1",
        "cutoutC1": "movableTrashC1",
        "cutoutD1": "movableTrashD1",
        "cutoutA3": "movableTrashA3",
        "cutoutB3": "movableTrashB3",
        "cutoutC3": "movableTrashC3",
        "cutoutD3": "movableTrashD3",
    }

    # Only declare defaults that are NEW or DIFFERENT from OT2HTTPDriver.
    # gather_defaults() walks the MRO and merges all class-level defaults dicts
    # automatically, so inherited keys do not need to be repeated here.
    defaults = {
        # None when no gripper detected, otherwise {"gripper_id": <serial>, "serial": <serial>}.
        "loaded_gripper": None,
    }

    def __init__(self, name="FlexHTTPDriver", overrides=None):
        # _initialize_robot() (called inside OT2HTTPDriver.__init__) switches
        # the headers to the Flex API version before its first request.
        OT2HTTPDriver.__init__(self, name=name, overrides=overrides)
        self.headers = {"Opentrons-Version": self.API_VERSION}

    def load_labware(self, name, slot, module=None, check_run_status=True, **kwargs):
        """Normalize *slot* to its Flex name, then delegate to OT2HTTPDriver.

        The parent compares *slot* directly against the (Flex-keyed)
        ``loaded_labware`` and ``loaded_modules`` dicts.
        """
        slot = self._normalize_slot(slot)
        return super().load_labware(name, slot, module=module,
                                    check_run_status=check_run_status, **kwargs)

    def load_module(self, name, slot, check_run_status=True, **kwargs):
        """Normalize *slot* to its Flex name, then delegate to OT2HTTPDriver."""
        return super().load_module(name, self._normalize_slot(slot),
                                   check_run_status=check_run_status, **kwargs)

    def _initialize_robot(self):
        """Initialize connection, then detect the trash bin location."""
        # Ensure the correct API version header is used for ALL startup calls.
        # OT2HTTPDriver.__init__ sets self.headers = {"Opentrons-Version": "2"}
        # before calling this method via polymorphism, so we must override it
        # here before any requests go out.
        self.headers = {"Opentrons-Version": self.API_VERSION}
        super()._initialize_robot()
        self._check_no_tips_attached()
        self._home_if_needed()
        self._autodetect_trash_area()

    def _check_no_tips_attached(self):
        """Refuse to start while a pipette's tip sensor reports a tip.

        The driver starts believing no tip is attached, so a tip left on (e.g.
        after a crash) would be driven into the tip rack at the next pickup.
        """
        mounts = [
            mount for mount, info in self.pipette_info.items()
            if info and info.get("tip_detected")
        ]
        if mounts:
            raise RuntimeError(
                f"Tip detected on the {', '.join(mounts)} pipette. Remove it by hand, "
                "then restart the driver."
            )

    def _home_if_needed(self):
        """Home the robot if any gantry axis is not engaged (i.e. after power-on or E-stop).

        Queries ``GET /motors/engaged`` and homes all axes if X or Y are not
        holding position.  Skips homing (~30 s) when the robot is already ready.
        """
        try:
            response = requests.get(
                url=f"{self.base_url}/motors/engaged",
                headers=self.headers,
                timeout=5,
            )
            if response.status_code != 200:
                self.log_warning(
                    f"Could not check motor engagement (HTTP {response.status_code}); "
                    "homing to be safe."
                )
                self.home()
                return

            engaged = response.json()
            # X and Y are the key gantry axes; if either isn't engaged the robot
            # hasn't been homed since boot.
            x_ok = engaged.get("x", {}).get("enabled", False)
            y_ok = engaged.get("y", {}).get("enabled", False)
            if not (x_ok and y_ok):
                self.log_info("Gantry axes not engaged — homing robot before use.")
                self.home()
            else:
                self.log_info("Gantry axes already engaged; skipping startup home.")
        except Exception as e:
            self.log_warning(f"Motor engagement check failed ({e}); homing to be safe.")
            self.home()

    def _get_deck_configuration(self):
        """Return the robot's current deck configuration.

        Reads ``GET /deck_configuration`` and returns its ``cutoutFixtures``
        list (dicts with ``cutoutId`` and ``cutoutFixtureId``).  Returns an
        empty list if the robot cannot be reached, which callers treat as a
        deck with no fixtures.
        """
        try:
            response = requests.get(
                url=f"{self.base_url}/deck_configuration",
                headers=self.headers,
                timeout=5,
            )
            if response.status_code != 200:
                self.log_warning(
                    f"GET /deck_configuration returned HTTP {response.status_code}."
                )
                return []
            return response.json().get("data", {}).get("cutoutFixtures", [])
        except Exception as e:  # noqa: BLE001 — best-effort, callers fall back
            self.log_warning(f"Could not read robot deck configuration ({e}).")
            return []

    def _autodetect_trash_area(self):
        """Detect the trash bin and waste chute from the robot's deck configuration.

        Sets ``TRASH_ADDRESSABLE_AREA`` from a ``trashBinAdapter`` entry
        (e.g. ``cutoutD3`` → ``movableTrashD3``) and :attr:`waste_chute` from
        any cutout-D3 fixture whose ID contains ``WasteChute``.  Tips are
        dropped in the trash bin when one exists, otherwise in the waste chute.
        If neither exists (or the deck configuration cannot be read),
        :attr:`tip_disposal_available` is set to ``False``.
        """
        trash_area = None
        self.waste_chute = None
        for entry in self._get_deck_configuration():
            fixture = entry.get("cutoutFixtureId", "")
            cutout = entry.get("cutoutId", "")
            if fixture == "trashBinAdapter" and trash_area is None:
                trash_area = self._TRASH_CUTOUT_TO_AREA.get(cutout)
            elif cutout == _WASTE_CHUTE_CUTOUT and "wastechute" in fixture.lower():
                self.waste_chute = {
                    "fixture": fixture,
                    "covered": "nocover" not in fixture.lower(),
                }

        self.TRASH_ADDRESSABLE_AREA = trash_area or "movableTrashA3"
        self.use_waste_chute_for_tips = trash_area is None and self.waste_chute is not None
        self.tip_disposal_available = trash_area is not None or self.waste_chute is not None

        if not self.tip_disposal_available:
            self.log_error(
                "No trash bin or waste chute found in the robot's deck configuration; "
                "transfers are disabled until one is configured in the Opentrons App."
            )
            return
        if self.waste_chute:
            self.log_info(f"Waste chute detected ({self.waste_chute['fixture']!r})")
        if self.use_waste_chute_for_tips:
            self.log_info("No trash bin configured; dropping tips in the waste chute")
        else:
            self.log_info(f"Trash area set to {self.TRASH_ADDRESSABLE_AREA!r}")

    def _require_tip_disposal(self):
        """Raise unless there is somewhere to drop tips.

        Re-reads the deck configuration first, so a trash bin or chute added in
        the Opentrons App after startup is picked up without a restart.
        """
        if self.tip_disposal_available:
            return
        self._autodetect_trash_area()
        if not self.tip_disposal_available:
            raise RuntimeError(
                "No trash bin or waste chute in the robot's deck configuration, so "
                "used tips could not be dropped. Configure one in the Opentrons App "
                "before transferring."
            )

    def transfer(self, source, dest, volume, *args, **kwargs):
        """Check that tips can be dropped, then delegate to OT2HTTPDriver.transfer.

        The check runs before any tip is picked up or liquid moved, so a missing
        trash bin or chute cannot leave a used tip on the pipette.  A tip left
        on the pipette by a failed transfer is discarded first, never reused.
        """
        self._require_tip_disposal()
        self._discard_contaminated_tip()
        try:
            return super().transfer(source, dest, volume, *args, **kwargs)
        except Exception:
            self._mark_tip_contaminated()
            raise

    def mix(self, volume, location, repetitions=1, **kwargs):
        """Discard a tip left by a failed transfer, then delegate to OT2HTTPDriver.mix."""
        self._discard_contaminated_tip()
        try:
            return super().mix(volume, location, repetitions=repetitions, **kwargs)
        except Exception:
            self._mark_tip_contaminated()
            raise

    def _execute_atomic_command(self, command_type, params=None, *args, **kwargs):
        """Run a command via OT2HTTPDriver, tracking which pipette holds a tip."""
        pipette_id = (params or {}).get("pipetteId")
        result = super()._execute_atomic_command(command_type, params, *args, **kwargs)
        self._track_tip(command_type, pipette_id)
        return result

    def _track_tip(self, command_type, pipette_id):
        if command_type == "pickUpTip":
            self.tip_pipette_id = pipette_id
        elif command_type == "dropTipInPlace":
            self.tip_pipette_id = None
            self.tip_contaminated = False

    def _mark_tip_contaminated(self):
        if self.has_tip:
            self.tip_contaminated = True
            self.log_warning(
                "Transfer failed with a tip attached; it will be discarded before "
                "the next transfer instead of being reused."
            )

    def _discard_contaminated_tip(self):
        """Drop a tip left on the pipette by a failed transfer or mix."""
        if not (self.tip_contaminated and self.has_tip):
            self.tip_contaminated = False
            return
        pipette_id = self.tip_pipette_id
        if pipette_id is None:
            mount = self.last_pipette
            pipette_id = self.pipette_info.get(mount, {}).get("id") if mount else None
        if pipette_id is None:
            raise RuntimeError(
                "A used tip from a failed transfer is still attached, but the pipette "
                "holding it is unknown. Remove it manually, then call reset_deck()."
            )
        self.log_info(f"Discarding tip left on pipette {pipette_id} by a failed transfer")
        self._execute_atomic_command(
            "moveToAddressableAreaForDropTip",
            {
                "pipetteId": pipette_id,
                "addressableAreaName": self._tip_drop_area(pipette_id),
                "offset": {"x": 0, "y": 0, "z": 10},
                "alternateDropLocation": False,
            },
            check_run_status=False,
        )
        self._execute_atomic_command("dropTipInPlace", {"pipetteId": pipette_id}, check_run_status=False)
        self.has_tip = False

    def _active_channels(self, pipette_id):
        """Return the number of active nozzles on the pipette with *pipette_id*."""
        for mount, info in self.pipette_info.items():
            if info and info.get("id") == pipette_id:
                if mount == _96CH_MOUNT_KEY or _is_96_channel(info.get("name")):
                    layout = (
                        self.config.get("loaded_instruments", {})
                        .get(_96CH_MOUNT_KEY, {})
                        .get("nozzle_layout", "full96")
                    )
                    return _NOZZLE_LAYOUT_CHANNELS.get(layout, 96)
                return int(info.get("channels") or 1)
        return 1

    def _tip_drop_area(self, pipette_id):
        """Return the trash bin area, or the waste-chute area for this pipette."""
        if not self.use_waste_chute_for_tips:
            return self.TRASH_ADDRESSABLE_AREA

        channels = self._active_channels(pipette_id)
        if channels == 96 and self.waste_chute["covered"]:
            raise RuntimeError(
                "The 96-channel pipette cannot drop all 96 tips into a covered "
                "waste chute. Remove the chute cover and update the deck "
                "configuration in the Opentrons App."
            )
        return _WASTE_CHUTE_TIP_AREAS.get(channels, "1ChannelWasteChute")

    # ------------------------------------------------------------------
    # Slot translation
    # ------------------------------------------------------------------

    def _normalize_slot(self, slot):
        """Translate an OT2-convention numeric slot to a Flex alphanumeric slot.

        Users always interact with numeric slots (``"1"``–``"12"``).  This
        method converts them to the Flex representation (``"D1"``–``"A3"``)
        before any value is sent to the HTTP API.

        Slots already in Flex format (e.g. ``"A1"``, ``"D3"``) are returned
        unchanged, so direct Flex-format input also works.

        Staging slots ``"A4"``–``"D4"`` are Flex-native and pass through
        unchanged.  They are only reachable by the gripper; pipettes cannot
        access them, and they only exist if the robot's deck configuration
        has a staging-area fixture.

        Parameters
        ----------
        slot : str or int

        Returns
        -------
        str
            Flex alphanumeric slot name.
        """
        s = str(slot).strip().upper()
        return _OT2_TO_FLEX_SLOT.get(s, s)

    def _api_slot_name(self, slot):
        """Return the Flex slot name to use in HTTP API commands."""
        return self._normalize_slot(slot)

    def parse_well(self, loc):
        """Parse a Flex well location string, e.g. ``"D1A1"`` → ``("D1", "A1")``.

        Flex slot names are two characters (letter + digit, e.g. ``"D1"``)
        followed by the well name (letter(s) + digit(s), e.g. ``"A1"``).  The
        base implementation stops at the *first* alpha character, which splits
        ``"D1A1"`` as slot=``""`` + well=``"D1A1"`` — completely wrong for Flex.

        This override reads exactly two characters as the slot prefix when the
        string starts with a letter (Flex format), otherwise falls back to the
        OT2 base behaviour (numeric prefix).
        """
        loc = str(loc)
        if loc and loc[0].isalpha():
            # Flex format: "<Letter><Digit><WellName>", e.g. "D1A1" or "A3H12"
            slot = loc[:2]
            well = loc[2:]
        else:
            # OT2 / numeric format: delegate to base implementation
            slot, well = super().parse_well(loc)
        return slot, well

    def _slot_location(self, slot):
        """Return the location dict for loadLabware/moveLabware API commands.

        Staging slots (A4\u2013D4) are addressable areas, not deck slots, so they
        require ``{"addressableAreaName": ...}`` instead of ``{"slotName": ...}``.
        """
        flex_slot = self._normalize_slot(slot)
        if flex_slot in _STAGING_SLOTS:
            return {"addressableAreaName": flex_slot}
        return {"slotName": flex_slot}

    # ------------------------------------------------------------------
    # Gripper
    # ------------------------------------------------------------------

    def load_gripper(self):
        """Detect the attached Flex gripper and record it in config.

        Queries ``GET /instruments`` to locate the gripper on the ``extension``
        mount.  In API v4+ the gripper is implicitly available to ``moveLabware``
        without any run command — this method simply confirms it is attached and
        stores its serial so the frontend can show gripper status.

        Returns
        -------
        str
            The gripper serial number.

        Raises
        ------
        RuntimeError
            If no gripper is physically attached to the extension mount, or if
            the HTTP API call fails.
        """
        instr_response = requests.get(
            url=f"{self.base_url}/instruments",
            headers=self.headers,
            timeout=5,
        )
        if instr_response.status_code != 200:
            raise RuntimeError(
                f"Failed to get instruments: {instr_response.text}"
            )

        gripper_instrument = next(
            (
                inst
                for inst in instr_response.json().get("data", [])
                if inst.get("mount") == "extension"
            ),
            None,
        )
        if gripper_instrument is None:
            raise RuntimeError(
                "No gripper found on the extension mount. "
                "Ensure a gripper is physically attached to the Flex."
            )

        gripper_serial = gripper_instrument.get("serialNumber")
        if not gripper_serial:
            raise RuntimeError(
                "Gripper found on the extension mount but its serialNumber is missing "
                "or empty in the /instruments response."
            )

        self.config["loaded_gripper"] = {
            "gripper_id": gripper_serial,
            "serial": gripper_serial,
        }
        self.config._update_history()
        self.log_info(f"Gripper detected with serial {gripper_serial}")
        return gripper_serial

    @Driver.quickbar(
        qb={
            "button_text": "Move Labware",
            "params": {
                "source_slot": {"label": "Source Slot", "type": "text", "default": "1"},
                "dest_slot": {"label": "Dest Slot (or offDeck / wasteChute)", "type": "text", "default": "2"},
                "use_gripper": {"label": "Use Gripper", "type": "bool", "default": True},
            },
        }
    )
    def move_labware(self, source_slot, dest_slot, use_gripper=True):
        """Move a labware from one deck slot to another.

        Uses the Flex gripper by default (``strategy='usingGripper'``).  Set
        ``use_gripper=False`` for a manual move where the robot pauses and
        waits for the operator to reposition the plate.

        Parameters
        ----------
        source_slot : str or int
            OT2-convention slot (``"1"``–``"12"'') containing the labware to move.
        dest_slot : str or int or ``"offDeck"`` or ``"wasteChute"``
            Destination slot, ``"offDeck"`` to remove the labware from the
            deck entirely, or ``"wasteChute"`` to discard it down an uncovered
            waste chute with the gripper.
        use_gripper : bool
            If ``True`` (default), use the gripper.  The gripper must already
            be loaded via :meth:`load_gripper`.

        Returns
        -------
        dict
            ``{source_slot, dest_slot, strategy, labware_id}``

        Raises
        ------
        ValueError
            If *source_slot* contains no loaded labware.
        RuntimeError
            If *use_gripper* is ``True`` but the gripper has not been loaded,
            or *dest_slot* is ``"wasteChute"`` without the gripper or without
            an uncovered waste chute.
        """
        self._require_no_motion_fault()
        source_slot, new_location, dest_label, strategy = self._plan_labware_move(
            source_slot, dest_slot, use_gripper
        )
        labware_id, labware_name, labware_data = self.config["loaded_labware"][source_slot]

        run_id = self._ensure_run_exists()

        move_response = requests.post(
            url=f"{self.base_url}/runs/{run_id}/commands",
            headers=self.headers,
            params={"waitUntilComplete": True},
            json={
                "data": {
                    "commandType": "moveLabware",
                    "params": {
                        "labwareId": labware_id,
                        "newLocation": new_location,
                        "strategy": strategy,
                    },
                    "intent": "setup",
                }
            },
        )
        self._check_cmd_success(move_response)
        return self._record_labware_move(source_slot, dest_label, strategy)

    def _plan_labware_move(self, source_slot, dest_slot, use_gripper):
        """Validate a move and return ``(source_slot, new_location, dest_label, strategy)``.

        *new_location* is the ``moveLabware`` payload value; *dest_label* is
        the Flex slot name, ``"offDeck"`` or ``"wasteChute"``.
        """
        source_slot = self._normalize_slot(source_slot)
        if source_slot not in self.config["loaded_labware"]:
            raise ValueError(
                f"No labware loaded in slot {source_slot!r}. "
                f"Loaded slots: {list(self.config['loaded_labware'].keys())}"
            )

        if use_gripper and not self.config.get("loaded_gripper"):
            raise RuntimeError(
                "Gripper is not loaded. Call load_gripper() before move_labware()."
            )
        strategy = "usingGripper" if use_gripper else "manualMoveWithoutPause"

        dest_str = str(dest_slot).strip().lower()
        if dest_str == "wastechute":
            if not use_gripper:
                raise RuntimeError("Discarding labware in the waste chute requires the gripper.")
            if not self.waste_chute or self.waste_chute["covered"]:
                raise RuntimeError(
                    "No uncovered waste chute in the robot's deck configuration; "
                    "cannot discard labware with the gripper."
                )
            return source_slot, {"addressableAreaName": "gripperWasteChute"}, "wasteChute", strategy
        if dest_str == "offdeck":
            return source_slot, "offDeck", "offDeck", strategy
        return source_slot, self._slot_location(dest_slot), self._normalize_slot(dest_slot), strategy

    def _record_labware_move(self, source_slot, dest_label, strategy):
        """Update labware and tip tracking after a successful move."""
        labware_id, labware_name, labware_data = self.config["loaded_labware"].pop(source_slot)
        if dest_label in ("offDeck", "wasteChute"):
            self._forget_tiprack(labware_id)
        else:
            self.config["loaded_labware"][dest_label] = (labware_id, labware_name, labware_data)
        self.config._update_history()

        self.log_info(
            f"Moved '{labware_name}' from slot {source_slot} to {dest_label} "
            f"(strategy: {strategy!r})"
        )
        return {
            "source_slot": source_slot,
            "dest_slot": dest_label,
            "strategy": strategy,
            "labware_id": labware_id,
        }

    def _forget_tiprack(self, labware_id):
        """Stop drawing tips from *labware_id* once it has left the deck."""
        for mount, tips in self.config.get("available_tips", {}).items():
            remaining = [(rack, well) for rack, well in tips if rack != labware_id]
            if len(remaining) != len(tips):
                self.config["available_tips"][mount] = remaining
                self.log_info(
                    f"Removed {len(tips) - len(remaining)} tips from {mount} mount: "
                    f"tip rack {labware_id} left the deck"
                )
        for instrument in self.config.get("loaded_instruments", {}).values():
            racks = instrument.get("tip_racks", [])
            if labware_id in racks:
                instrument["tip_racks"] = [r for r in racks if r != labware_id]

    def _align_script_mount(self, mount):
        """The Protocol API loads the 96-channel on the ``'left'`` mount."""
        return "left" if mount == _96CH_MOUNT_KEY else mount

    def _align_script_header(self):
        """Alignment-script header declaring a Flex protocol.

        Flex protocols must declare ``robotType: "Flex"`` in ``requirements``
        (apiLevel 2.15+); otherwise the Opentrons App analyses them as OT-2
        protocols and rejects Flex slot names such as ``"D1"``.
        """
        return [
            "from opentrons import protocol_api",
            "",
            "metadata = {",
            "    'protocolName': 'Alignment Check',",
            "    'author': 'AFL Auto-Generated',",
            "    'description': 'Script for aligning and testing deck configuration',",
            "}",
            "",
            "requirements = {'robotType': 'Flex', 'apiLevel': '2.16'}",
            "",
        ]

    # ------------------------------------------------------------------
    # 96-channel pipette support
    # ------------------------------------------------------------------

    def _update_pipettes(self):
        """Update pipette info then remap 96-channel from 'left' → '96channel'."""
        super()._update_pipettes()
        # The Flex HTTP API reports the 96-channel under the 'left' mount.  Rename
        # it so we can distinguish it from an independent left-mount 1-channel.
        if "left" in self.pipette_info:
            info = self.pipette_info["left"]
            if info and _is_96_channel(info.get("name")):
                self.pipette_info[_96CH_MOUNT_KEY] = self.pipette_info.pop("left")

        # Recover the run-scoped pipette ID that OT2HTTPDriver._update_pipettes
        # cannot find because it looks under 'left' but we store under '96channel'.
        if _96CH_MOUNT_KEY in self.pipette_info:
            stored = self.config.get("loaded_instruments", {}).get(_96CH_MOUNT_KEY, {})
            stored_id = stored.get("pipette_id")
            if stored_id and not self.pipette_info[_96CH_MOUNT_KEY].get("id"):
                self.pipette_info[_96CH_MOUNT_KEY]["id"] = stored_id

    def confirm_tip_removed(self):
        """Record that any tip was removed by hand, including one marked for discard."""
        super().confirm_tip_removed()
        self.tip_pipette_id = None
        self.tip_contaminated = False

    def reset_deck(self):
        """Reset deck state, clear the gripper, and forget any attached tip.

        The run is discarded, so the robot starts the next one with no tip;
        remove any physical tip by hand before calling this.
        """
        super().reset_deck()
        self.config["loaded_gripper"] = None
        self.has_tip = False
        self.tip_pipette_id = None
        self.tip_contaminated = False
        self.config._update_history()

    def load_instrument(self, name, mount, tip_rack_slots, reload=False, **kwargs):
        """Load a pipette, routing the 96-channel to its own config key.

        The 96-channel must be declared to the HTTP API under ``'left'`` mount,
        but AFL tracks it under the ``'96channel'`` key so that a separately
        loaded left-mount single-channel is never confused with it.

        For all other pipettes the call is delegated directly to
        :meth:`OT2HTTPDriver.load_instrument`.
        """
        pipette_name = self._normalize_pipette_name(name)
        if not _is_96_channel(pipette_name):
            return super().load_instrument(name, mount, tip_rack_slots, reload=reload, **kwargs)

        # Suppress the OT2 'left'/'right' validation by passing mount='left'.
        result = super().load_instrument(name, "left", tip_rack_slots, reload=reload, **kwargs)

        # Remap all state dicts: 'left' → '96channel'
        for d in (self.config["loaded_instruments"], self.config["available_tips"], self.pipette_info):
            if "left" in d:
                d[_96CH_MOUNT_KEY] = d.pop("left")
        # Patch the 'mount' field inside pipette_info so get_pipette() returns it correctly.
        if _96CH_MOUNT_KEY in self.pipette_info and self.pipette_info[_96CH_MOUNT_KEY]:
            self.pipette_info[_96CH_MOUNT_KEY]["mount"] = _96CH_MOUNT_KEY

        self.config._update_history()
        return result

    def get_tip(self, mount):
        """Pop the next tip from *mount*'s tiprack list.

        For the 96-channel (``mount == '96channel'``) in full-rack mode, a
        single ``pickUpTip`` consumes **all 96 wells** of one tiprack.  This
        override removes the entire first tiprack from the available list and
        returns ``(tiprack_id, 'A1')`` — the Opentrons API only needs the
        tiprack ID and a single anchor well for a 96-channel pickup.

        For all other mounts the parent implementation is used (advances one
        well at a time).
        """
        if mount != _96CH_MOUNT_KEY:
            return super().get_tip(mount)

        tips = self.config["available_tips"].get(mount, [])
        if not tips:
            raise RuntimeError(
                "No tip racks available for the 96-channel pipette. "
                "Load additional tipracks or call reset_tipracks()."
            )
        first_rack_id = tips[0][0]
        # Consume every entry that belongs to this tiprack in one sweep.
        self.config["available_tips"][mount] = [
            (tid, w) for tid, w in tips if tid != first_rack_id
        ]
        self.config._update_history()
        return (first_rack_id, "A1")

    @Driver.queued()
    def configure_nozzle_layout(self, config_type="full96", **kwargs):
        """Configure the active nozzle layout for the 96-channel pipette.

        Must be called after :meth:`load_instrument` when using the
        96-channel.  Has no effect on 1- or 8-channel pipettes.

        Parameters
        ----------
        config_type : {"full96", "column", "single"}
            ``"full96"``  — all 96 nozzles active (default).
            ``"column"``  — 8 nozzles in a single column (behaves like 8-channel).
            ``"single"``  — 1 nozzle only (behaves like 1-channel).
        """
        _layout_params = {
            "full96":  {"style": "ALL"},
            "column":  {"style": "COLUMN", "primaryNozzle": "A1"},
            "single":  {"style": "SINGLE", "primaryNozzle": "A1"},
        }
        if config_type not in _layout_params:
            raise ValueError(
                f"config_type must be one of {list(_layout_params.keys())!r}. "
                f"Received: {config_type!r}"
            )

        instrument = self.config.get("loaded_instruments", {}).get(_96CH_MOUNT_KEY)
        if instrument is None:
            raise RuntimeError(
                "No 96-channel pipette loaded. Call load_instrument() first."
            )

        pipette_id = instrument["pipette_id"]
        run_id = self._ensure_run_exists()

        response = requests.post(
            url=f"{self.base_url}/runs/{run_id}/commands",
            headers=self.headers,
            params={"waitUntilComplete": True},
            json={
                "data": {
                    "commandType": "configureNozzleLayout",
                    "params": {
                        "pipetteId": pipette_id,
                        "configurationParams": _layout_params[config_type],
                    },
                    "intent": "setup",
                }
            },
        )
        self._check_cmd_success(response)

        instrument["nozzle_layout"] = config_type
        self.config._update_history()
        self.log_info(f"96-channel nozzle layout set to {config_type!r}")
        return config_type
