"""
FlexHTTPDriver — Opentrons Flex (OT-3) support for AFL.

The Flex uses the same HTTP API base as the OT2 but with key differences:

* API version header: ``Opentrons-Version: 4``
* Deck slots: alphanumeric ``A1–D3`` (and staging ``A4–D4``) instead of
  numeric ``1–12``.
* Pipette names: ``flex_1channel_50``, ``flex_1channel_1000``, etc.
* Deck configuration is a robot-level setting (not per-run) that must be
  applied before any labware or modules are loaded.
* Trash is a configurable fixture (trash bin or waste chute), not a fixed
  location at slot 12.
"""

import requests

from AFL.automation.APIServer.Driver import Driver
from AFL.automation.prepare.OT2HTTPDriver import OT2HTTPDriver
from AFL.automation.prepare.FlexDeckWebAppMixin import FlexDeckWebAppMixin

# Translate OT2 slot names to Flex
_OT2_TO_FLEX_SLOT = {
    "1":  "D1", "2":  "D2", "3":  "D3",
    "4":  "C1", "5":  "C2", "6":  "C3",
    "7":  "B1", "8":  "B2", "9":  "B3",
    "10": "A1", "11": "A2", "12": "A3",
}

# The slot names for the staging area
_STAGING_SLOTS = {"A4", "B4", "C4", "D4"}

# Only stacker module and heater shaker are supported other fixtures are not
_MODULE_FIXTURE_IDS = set(
    "heaterShakerModuleV1",
    "magneticBlockV1",
    "thermocyclerModuleV2",
    "absorbanceReaderV1",
    "flexStackerModuleV1"
    )

# TODO i think i can handle the trash bin as a fixture

# TODO decide if 96channel pipette support not implemented for now

class FlexHTTPDriver(FlexDeckWebAppMixin, OT2HTTPDriver):
    """Driver for the Opentrons Flex (OT-3) robot.

    Subclasses :class:`OT2HTTPDriver` and overrides only the parts that differ
    between the OT2 and the Flex HTTP API.

    Parameters
    ----------
    overrides : dict, optional
        Configuration overrides passed through to :class:`Driver`.
    
    """

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

    DECK_STREAM_REQUEST_TIMEOUT_SECONDS = 15
    defaults = {}
    defaults["robot_ip"] = "127.0.0.1"  # Default to localhost, should be overridden
    defaults["robot_port"] = "31950"  # Default Opentrons HTTP API port
    defaults["loaded_labware"] = {}  # Persistent storage for loaded labware
    defaults["loaded_instruments"] = {}  # Persistent storage for loaded instruments
    defaults["loaded_modules"] = {}  # Persistent storage for loaded modules
    defaults["available_tips"] = {}  # Persistent storage for available tips, Format: {mount: [(tiprack_id, well_name), ...]}
    defaults["stock_tip_locations"] = {}  # Configured stock tip candidates, Format: {stock_name: ["6A4", "9A4"]}
    defaults["stock_tip_reservations"] = {}  # Activated stock tip reservations, Format: {stock_name: ["6A4"]}
    defaults["reserved_stock_tips"] = []  # Tip locations reserved for stock pipetting, e.g. ["6A4"]
    defaults["occupied_sample_locations"] = []  # Sample destinations already populated on deck
    defaults["prep_targets"] = []  # Persistent storage for prep target well locations
    defaults["tip_rack_offset"] = {"x": 0, "y": 0, "z": 0}  # Default offset for tip pickup/return at tiprack wells
    defaults["enable_deck_stream"] = True
    defaults["deck_stream_video_fps"] = 1

    def __init__(self, overrides:dict=None):
        """Initialize the Flex HTTP driver.

        Parameters
        ----------
        overrides : dict, optional
            Configuration values that override the class defaults.

        Examples
        --------
        >>> driver = FlexHTTPDriver({"robot_ip": "127.0.0.1", "robot_port": "31950"})
        >>> driver.base_url
        'http://127.0.0.1:31950'
        """
        self.app = None
        Driver.__init__(
            self,
            name="Flex_HTTP_Driver",
            defaults=self.gather_defaults(),
            overrides=overrides,
        )
        self.name = "Flex_HTTP_Driver"

        # Initialize state variables
        self.session_id = None
        self.protocol_id = None
        self.max_transfer = None
        self.min_transfer = None
        self.has_tip = False
        self.last_pipette = None
        self.current_tip = None
        self.modules = {}
        self._deck_stream_thread = None
        self._deck_stream_stop_event = None
        self._deck_stream_lock = threading.Lock()
        self._deck_stream_state = {
            "running": False,
            "current_window_started_at": None,
            "last_completed_at": None,
            "last_video_path": None,
            "last_frame_count": 0,
            "last_error": None,
            "stopped_for_run_status": None,
            "task_name": None,
        }
            
        self.pipette_info = {}

        # Custom labware handling
        self.custom_labware_files = {}
        self.sent_custom_labware = {}
        self.custom_labware_dir = self._get_custom_labware_dir()
        self._load_custom_labware_defs()

        # Base URL for HTTP requests
        self.base_url = f"http://{self.config['robot_ip']}:{self.config['robot_port']}"
        self.headers = {"Opentrons-Version": "2"}

        # Initialize the robot connection
        self._initialize_robot()
        self.useful_links['View Deck'] = '/visualize_deck'

        self._home_if_needed()


    def _home_if_needed(self):
        """Home the robot if any gantry axis is not engaged (i.e. after power-on or E-stop).

        Queries ``GET /motors/engaged`` and homes all axes if X or Y are not
        holding position.  Skips homing (~30 s) when the robot is already ready.
        """
        try:
            response = requests.get(
                url=f"{self.base_url}/motors/engaged",
                headers=self.headers,
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

    def _initialize_robot(self):
        """Probe robot connectivity and refresh attached pipette metadata.

        Raises
        ------
        RuntimeError
            If the robot cannot be reached or pipette metadata cannot be read.
        """
        self.log_info("Initializing Flex HTTP Driver")
        try:
            # Check if the robot is reachable
            response = requests.get(url=f"{self.base_url}/health", headers=self.headers)
            if response.status_code != 200:
                raise ConnectionError(f"Failed to connect to robot at {self.base_url}")

            # Get attached pipettes
            self._update_pipettes()
        except requests.exceptions.RequestException as e:
            self.log_error(f"Error connecting to robot: {str(e)}")
            raise ConnectionError(
                f"Error connecting to robot at {self.base_url}: {str(e)}"
            )

        self._home_if_needed()

    def _update_modules(self):
        """ Refresh cached metadata for pipettes attached to the robot.

        The main purpose is to track module serial numbers
        """
        self._module_info = {}  # cutoutId -> serialNumber
        try:
            response = requests.get(
                url=f"{self.base_url}/modules",
                headers=self.headers,
                timeout=5,
            )
            if response.status_code != 200:
                self.log_warning(
                    f"Could not fetch module list (HTTP {response.status_code}); "
                    "serial numbers will be omitted from deck configuration."
                )
                return

            for module in response.json().get("data", []):
                serial = module.get("serialNumber")
                module_id = module.get("id")
                if not serial:
                    continue
                # physicalPort.slot is the Flex slot string, e.g. "D1"
                slot = module.get("moduleOffset", {}).get("slot")
                if slot:
                    cutout_id = f"cutout{slot}"
                    self._module_serials[module_id] = {
                        "cutout_id": cutout_id,
                        "serial": serial
                        }
                    self.log_info(
                        f"Module serial cached: {cutout_id} → {serial} "
                        f"({module.get('moduleModel', module.get('moduleType', '?'))})"
                    )
            print(self._module_serials)
        except Exception as e:  # noqa: BLE001 — best-effort, don't block startup
            self.log_warning(f"_update_modules: {e}; serial numbers will be omitted.")

    def _normalize_slot(self, slot):
        """Translate an OT2-convention numeric slot to a Flex alphanumeric slot.

        Users always interact with numeric slots (``"1"``–``"12"``).  This
        method converts them to the Flex representation (``"D1"``–``"A3"``)
        before any value is sent to the HTTP API.

        Slots already in Flex format (e.g. ``"A1"``, ``"D3"``) are returned
        unchanged, so direct Flex-format input also works.

        Staging slots ``"A4"``–``"D4"`` are Flex-native and pass through
        unchanged.  They are only reachable by the gripper; pipettes cannot
        access them.  They are enabled by default in the deck configuration.

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

    # TODO Look at this as it may not work and create a deck configuration with a
    # waste chute
    def _autodetect_trash_area(self):
        """Set TRASH_ADDRESSABLE_AREA from the deck configuration.

        Scans ``deck_configuration`` for a ``trashBinAdapter`` entry and
        derives the matching addressable area name (e.g. ``cutoutD3`` →
        ``movableTrashD3``).  Falls back to ``"movableTrashA3"`` if none found.
        """
        for entry in self.config.get("deck_configuration", []):
            if entry.get("cutoutFixtureId") == "trashBinAdapter":
                cutout = entry.get("cutoutId", "")
                area = self._TRASH_CUTOUT_TO_AREA.get(cutout)
                if area:
                    self.TRASH_ADDRESSABLE_AREA = area
                    self.log_info(f"Trash area set to {area!r} (from {cutout!r})")
                    return
        self.TRASH_ADDRESSABLE_AREA = "movableTrashA3"

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
        source_slot = self._normalize_slot(source_slot)

        if source_slot not in self.config["loaded_labware"]:
            raise ValueError(
                f"No labware loaded in slot {source_slot!r}. "
                f"Loaded slots: {list(self.config['loaded_labware'].keys())}"
            )

        dest_str_check = str(dest_slot).strip().lower()
        if dest_str_check != "offdeck":
            self._check_slot_not_blocked(dest_slot)

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
            new_location = self._slot_location(dest_slot)

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
            self.config["loaded_labware"][self._normalize_slot(dest_slot)] = (
                labware_id, labware_name, labware_data
            )
        self.config._update_history()

        self.log_info(
            f"Moved '{labware_name}' from slot {source_slot} to {self._normalize_slot(dest_slot)} "
            f"(strategy: {strategy!r})"
        )
        return {
            "source_slot": source_slot,
            "dest_slot": self._normalize_slot(dest_slot),
            "strategy": strategy,
            "labware_id": labware_id,
        }

    