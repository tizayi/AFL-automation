"""
FlexHTTPDriver — Opentrons Flex (OT-3) support for AFL.

The Flex uses the same HTTP API base as the OT2 but with key differences:

* API version header: ``Opentrons-Version: 3``
* Deck slots: alphanumeric ``A1–D3`` (and staging ``A4–D4``) instead of
  numeric ``1–12``.
* Pipette names: ``flex_1channel_50``, ``flex_1channel_1000``, etc.
* Deck configuration must be declared before labware is loaded on each run.
* Trash is a configurable fixture (trash bin or waste chute), not a fixed
  location at slot 12.

Users interact with the driver using the same OT2 numeric slot convention
(``1``–``12``).  :meth:`_normalize_slot` translates these internally before
any command reaches the HTTP API so that no user-facing config needs to change
when switching from an OT2 to a Flex.
"""

import requests

from AFL.automation.APIServer.Driver import Driver
from AFL.automation.prepare.OT2HTTPDriver import OT2HTTPDriver
from AFL.automation.prepare.FlexDeckWebAppMixin import FlexDeckWebAppMixin

# ---------------------------------------------------------------------------
# Slot translation table: OT2 numeric → Flex alphanumeric
# The Flex deck is a 4×3 grid.  Row D is at the front (≈ OT2 rows 1–3),
# row A is at the back (≈ OT2 rows 10–12).
# ---------------------------------------------------------------------------
_OT2_TO_FLEX_SLOT = {
    "1":  "D1", "2":  "D2", "3":  "D3",
    "4":  "C1", "5":  "C2", "6":  "C3",
    "7":  "B1", "8":  "B2", "9":  "B3",
    "10": "A1", "11": "A2", "12": "A3",
}

# Canonical config key used for the 96-channel pipette.  The Opentrons HTTP API
# addresses it as "left" mount, but storing it under a distinct key prevents
# collision with an independent left-mount single-channel pipette.
_96CH_MOUNT_KEY = "96channel"


class FlexHTTPDriver(FlexDeckWebAppMixin, OT2HTTPDriver):
    """Driver for the Opentrons Flex (OT-3) robot.

    Subclasses :class:`OT2HTTPDriver` and overrides only the parts that differ
    between the OT2 and the Flex HTTP API.

    Parameters
    ----------
    overrides : dict, optional
        Configuration overrides passed through to :class:`Driver`.

    Configuration keys (in addition to those inherited from OT2HTTPDriver)
    -----------------------------------------------------------------------
    deck_configuration : list of dict
        Opentrons deck-configuration payload.  Each entry is a dict with keys
        ``cutoutId`` and ``cutoutFixtureId``.  Sent to the robot once per run
        before any labware is loaded.

        Default: a single trash bin in cutout A3 (the most common Flex setup)::

            [{"cutoutId": "cutoutA3", "cutoutFixtureId": "trashBinAdapter"}]

        Common fixture IDs:
        - ``"trashBinAdapter"``  — trash bin
        - ``"wasteChuteOnlyAdapter"`` — waste chute (no staging)
        - ``"stagingAreaRightSlot"`` — staging area (enables D4/C4/B4/A4)
        - ``"magneticBlockV1"``  — magnetic block
    """

    ROBOT_TYPE = "OT-3"
    API_VERSION = "3"

    # When dropping tips the Flex uses a movable trash bin, not the OT2's
    # fixed-position trash at slot 12.  The addressable area name depends on
    # which cutout the trash bin is placed in; cutoutA3 → movableTrashA3.
    # Override ``TRASH_ADDRESSABLE_AREA`` or set ``trash_addressable_area`` in
    # config if your deck uses a different cutout or a waste chute.
    TRASH_ADDRESSABLE_AREA = "movableTrashA3"

    PIPETTE_NAME_ALIASES = {
        # Full names pass through unchanged.
        "flex_1channel_50":    "flex_1channel_50",
        "flex_1channel_1000":  "flex_1channel_1000",
        "flex_8channel_50":    "flex_8channel_50",
        "flex_8channel_1000":  "flex_8channel_1000",
        "flex_96channel_1000": "flex_96channel_1000",
        # Convenient shorthand aliases.
        "flex_50":    "flex_1channel_50",
        "flex_1000":  "flex_1channel_1000",
        "flex_8_50":  "flex_8channel_50",
        "flex_8_1000": "flex_8channel_1000",
        "flex_96":    "flex_96channel_1000",
    }

    EXPECTED_TIPRACK_TOKEN = {
        "flex_1channel_50":    "50ul",
        "flex_1channel_1000":  "1000ul",
        "flex_8channel_50":    "50ul",
        "flex_8channel_1000":  "1000ul",
        "flex_96channel_1000": "1000ul",
    }

    # Only declare defaults that are NEW or DIFFERENT from OT2HTTPDriver.
    # gather_defaults() walks the MRO and merges all class-level defaults dicts
    # automatically, so inherited keys do not need to be repeated here.
    defaults = {
        "deck_configuration": [
            {"cutoutId": "cutoutA3", "cutoutFixtureId": "trashBinAdapter"},
        ],
        # Persists across runs: None when no gripper loaded, otherwise
        # {"gripper_id": <run-scoped-id>, "serial": <serial-number>}.
        "loaded_gripper": None,
    }

    def __init__(self, overrides=None):
        # Set Flex API version header BEFORE OT2HTTPDriver.__init__ so that
        # _initialize_robot() (called inside OT2HTTPDriver.__init__) uses
        # Opentrons-Version: 3 from the very first request.
        self.headers = {"Opentrons-Version": self.API_VERSION}
        OT2HTTPDriver.__init__(self, overrides=overrides)
        self.name = "FlexHTTPDriver"
        # Override the API version header set by OT2HTTPDriver.__init__.
        self.headers = {"Opentrons-Version": self.API_VERSION}

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

        Parameters
        ----------
        slot : str or int

        Returns
        -------
        str
            Flex alphanumeric slot name.
        """
        s = str(slot).strip()
        return _OT2_TO_FLEX_SLOT.get(s, s)

    def _api_slot_name(self, slot):
        """Return the Flex slot name to use in HTTP API commands."""
        return self._normalize_slot(slot)

    # ------------------------------------------------------------------
    # Deck configuration
    # ------------------------------------------------------------------

    def _after_run_created(self, run_id):
        """Apply deck config and optionally reload the gripper after a new run is created.

        Called by :meth:`OT2HTTPDriver._create_run` before any labware or
        instruments are loaded onto the new run.
        """
        self._apply_deck_configuration(run_id)
        # Re-register the gripper with the new run if it was loaded previously.
        if self.config.get("loaded_gripper"):
            self.load_gripper()

    def _apply_deck_configuration(self, run_id):
        """POST the deck configuration for *run_id* to the robot.

        The deck configuration tells the Flex which fixtures (trash bin, waste
        chute, staging areas, modules) occupy each cutout.  It must be set
        before labware loading commands are sent.

        Parameters
        ----------
        run_id : str
            The run ID returned by the ``POST /runs`` call.
        """
        deck_config = self.config.get("deck_configuration", [])
        if not deck_config:
            self.log_info("No deck configuration defined; skipping.")
            return

        self.log_info(
            f"Applying deck configuration for run {run_id}: {deck_config}"
        )

        response = requests.patch(
            url=f"{self.base_url}/runs/{run_id}/deckConfiguration",
            headers=self.headers,
            json={"data": deck_config},
        )

        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to apply Flex deck configuration "
                f"(HTTP {response.status_code}): {response.text}"
            )

        self.log_info("Flex deck configuration applied successfully.")

    # ------------------------------------------------------------------
    # Gripper
    # ------------------------------------------------------------------

    def load_gripper(self):
        """Find the attached Flex gripper and register it with the current run.

        Queries ``GET /instruments`` to locate the gripper on the ``extension``
        mount, then issues a ``loadGripper`` setup command.  The resulting
        run-scoped gripper ID is stored in ``config['loaded_gripper']`` and
        persists across runs so the gripper is automatically re-registered
        when a new run is created.

        Returns
        -------
        str
            The run-scoped gripper ID assigned by the robot.

        Raises
        ------
        RuntimeError
            If no gripper is physically attached to the extension mount, or if
            the HTTP API call fails.
        """
        run_id = self._ensure_run_exists()

        instr_response = requests.get(
            url=f"{self.base_url}/instruments",
            headers=self.headers,
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

        cmd_response = requests.post(
            url=f"{self.base_url}/runs/{run_id}/commands",
            headers=self.headers,
            params={"waitUntilComplete": True},
            json={
                "data": {
                    "commandType": "loadGripper",
                    "params": {"gripperId": gripper_serial},
                    "intent": "setup",
                }
            },
        )
        self._check_cmd_success(cmd_response)

        result = cmd_response.json()["data"]["result"]
        gripper_run_id = result.get("gripperId", gripper_serial)

        self.config["loaded_gripper"] = {
            "gripper_id": gripper_run_id,
            "serial": gripper_serial,
        }
        self.config._update_history()
        self.log_info(f"Gripper loaded with run-scoped ID {gripper_run_id}")
        return gripper_run_id

    @Driver.quickbar(
        qb={
            "button_text": "Move Labware",
            "params": {
                "source_slot": {"label": "Source Slot", "type": "text", "default": "1"},
                "dest_slot": {"label": "Dest Slot (or offDeck)", "type": "text", "default": "2"},
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
        dest_slot : str or int or ``"offDeck"``
            Destination slot, or ``"offDeck"`` to remove the labware from the
            deck entirely.
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
            If *use_gripper* is ``True`` but the gripper has not been loaded.
        """
        source_slot = str(source_slot)

        if source_slot not in self.config["loaded_labware"]:
            raise ValueError(
                f"No labware loaded in slot {source_slot!r}. "
                f"Loaded slots: {list(self.config['loaded_labware'].keys())}"
            )

        labware_id, labware_name, labware_data = self.config["loaded_labware"][source_slot]

        if use_gripper and not self.config.get("loaded_gripper"):
            raise RuntimeError(
                "Gripper is not loaded. Call load_gripper() before move_labware()."
            )

        strategy = "usingGripper" if use_gripper else "manualMoveWithoutPause"

        dest_str = str(dest_slot).strip().lower()
        if dest_str == "offdeck":
            new_location = "offDeck"
        else:
            new_location = {"slotName": self._api_slot_name(dest_slot)}

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

        # Update labware tracking to reflect the new position.
        del self.config["loaded_labware"][source_slot]
        if dest_str != "offdeck":
            self.config["loaded_labware"][str(dest_slot)] = (
                labware_id, labware_name, labware_data
            )
        self.config._update_history()

        self.log_info(
            f"Moved '{labware_name}' from slot {source_slot} to {dest_slot} "
            f"(strategy: {strategy!r})"
        )
        return {
            "source_slot": source_slot,
            "dest_slot": str(dest_slot),
            "strategy": strategy,
            "labware_id": labware_id,
        }

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
            if info and "96channel" in info.get("name", ""):
                self.pipette_info[_96CH_MOUNT_KEY] = self.pipette_info.pop("left")

        # Recover the run-scoped pipette ID that OT2HTTPDriver._update_pipettes
        # cannot find because it looks under 'left' but we store under '96channel'.
        if _96CH_MOUNT_KEY in self.pipette_info:
            stored = self.config.get("loaded_instruments", {}).get(_96CH_MOUNT_KEY, {})
            stored_id = stored.get("pipette_id")
            if stored_id and not self.pipette_info[_96CH_MOUNT_KEY].get("id"):
                self.pipette_info[_96CH_MOUNT_KEY]["id"] = stored_id

    def reset_deck(self):
        """Reset the deck configuration, including gripper registration."""
        super().reset_deck()
        # OT2HTTPDriver.reset_deck() does not know about loaded_gripper.
        # Clear it here so _after_run_created does not attempt to re-register a
        # stale gripper serial against the freshly-created run.
        self.config["loaded_gripper"] = None

    def load_instrument(self, name, mount, tip_rack_slots, reload=False, **kwargs):
        """Load a pipette, routing the 96-channel to its own config key.

        The 96-channel must be declared to the HTTP API under ``'left'`` mount,
        but AFL tracks it under the ``'96channel'`` key so that a separately
        loaded left-mount single-channel is never confused with it.

        For all other pipettes the call is delegated directly to
        :meth:`OT2HTTPDriver.load_instrument`.
        """
        pipette_name = self._normalize_pipette_name(name)
        if "96channel" not in pipette_name:
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
            "full96":  {"primaryNozzle": "A1", "frontRightNozzle": "H12", "style": "ALL"},
            "column":  {"primaryNozzle": "A1", "frontRightNozzle": "H1",  "style": "COLUMN"},
            "single":  {"primaryNozzle": "A1", "frontRightNozzle": "A1",  "style": "SINGLE"},
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
