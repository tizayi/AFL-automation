"""VirtualFlexHTTPDriver — in-memory simulation of the Opentrons Flex.

Unlike the real driver, this class never opens a network connection.  All
protocol-level logic (transfer volume splitting, pipette selection, tip
tracking, deck configuration) is preserved by running the real parent code
paths; only the HTTP boundary methods are replaced with lightweight stubs
that update ``config`` and log.

This makes it suitable for:

* Unit and integration tests without hardware present (including full
  ``FlexPrepare`` + ``MassBalance`` workflows).
* Local development on workstations without a Flex on the network.
* Docker-compose-based CI pipelines.
"""

import uuid

from AFL.automation.APIServer.Driver import Driver
from AFL.automation.prepare.OT2HTTPDriver import TIPRACK_WELLS
from AFL.automation.prepare.FlexHTTPDriver import FlexHTTPDriver, _96CH_MOUNT_KEY
from AFL.automation.shared.utilities import listify


class VirtualFlexHTTPDriver(FlexHTTPDriver):
    """In-memory simulation of :class:`FlexHTTPDriver`.

    All state is stored in ``self.config`` (the same dict used by the real
    driver), so methods that read config — ``get_wells()``, tip tracking,
    ``status()`` — work without modification.

    Parameters
    ----------
    overrides : dict, optional
        Passed through to :class:`Driver` for persistent config initialisation.
    """

    def __init__(self, overrides=None):
        super().__init__(overrides=overrides)
        self.name = "VirtualFlexHTTPDriver"

    # ------------------------------------------------------------------
    # Bootstrap / robot connection — skip all network calls
    # ------------------------------------------------------------------

    def _initialize_robot(self):
        self.pipette_info = {}
        self.min_transfer = None
        self.max_transfer = None
        self.log_info("Virtual Flex initialised (no hardware)")

    def _update_pipettes(self):
        """Derive min/max transfer limits from the pipettes registered in config."""
        self.min_transfer = None
        self.max_transfer = None
        for mount, info in self.pipette_info.items():
            if not info:
                continue
            min_v = info.get("min_volume", 1)
            max_v = info.get("max_volume", 1000)
            if self.min_transfer is None or self.min_transfer > min_v:
                self.min_transfer = min_v
            if self.max_transfer is None or self.max_transfer < max_v:
                self.max_transfer = max_v

    def _ensure_run_exists(self, check_run_status=True):
        return "virtual-run"

    # ------------------------------------------------------------------
    # Deck configuration — no HTTP, just log
    # ------------------------------------------------------------------

    def _apply_deck_configuration(self):
        deck_config = self.config.get("deck_configuration", [])
        self.log_info(f"Virtual: deck configuration applied ({len(deck_config)} fixture(s))")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:8]}"

    def _pipette_max_volume(self, pipette_name: str) -> int:
        """Extract the maximum volume from a Flex pipette name.

        ``"flex_1channel_1000"`` → ``1000``, ``"flex_8channel_50"`` → ``50``.
        Falls back to ``1000`` if the name cannot be parsed.
        """
        parts = pipette_name.rsplit("_", 1)
        try:
            return int(parts[-1])
        except (ValueError, IndexError):
            return 1000

    def _channels_from_name(self, pipette_name: str) -> int:
        if "96channel" in pipette_name:
            return 96
        if "8channel" in pipette_name:
            return 8
        return 1

    # ------------------------------------------------------------------
    # Deck management — store state in config, no HTTP
    # ------------------------------------------------------------------

    @Driver.quickbar(qb={"button_text": "Home"})
    def home(self, **kwargs):
        self.log_info("Virtual: robot homed")

    def load_labware(self, name, slot, module=None, **kwargs):
        """Register labware in a slot without contacting the robot.

        Builds a minimal definition with all 96 standard wells so that
        :meth:`get_wells` assertions pass for any common labware type.
        """
        labware_id = self._generate_id("labware")
        definition = {
            "definition": {
                "wells": {w: {} for w in TIPRACK_WELLS},
                "metadata": {"displayName": name},
            }
        }
        flex_slot = self._normalize_slot(slot)
        self.config["loaded_labware"][flex_slot] = (labware_id, name, definition)
        self.config._update_history()
        self.log_info(f"Virtual: loaded labware '{name}' in slot {flex_slot} → {labware_id}")
        return labware_id

    def load_module(self, name, slot, **kwargs):
        """Register a module in a slot without contacting the robot."""
        module_id = self._generate_id("module")
        flex_slot = self._normalize_slot(slot)
        self.config["loaded_modules"][flex_slot] = (module_id, name)
        self.config._update_history()
        self.log_info(f"Virtual: loaded module '{name}' in slot {flex_slot} → {module_id}")
        return module_id

    def load_instrument(self, name, mount, tip_rack_slots, reload=False, **kwargs):
        """Register a pipette and initialise its tip supply without contacting the robot.

        Volume limits and channel count are inferred from *name* using the
        Flex naming convention (``flex_<channels>channel_<volume>``).
        The 96-channel is always stored under the ``'96channel'`` key regardless
        of what *mount* value is passed in.
        """
        pipette_name = self._normalize_pipette_name(name)
        # 96-channel is always stored under its canonical key.
        if "96channel" in pipette_name:
            mount = _96CH_MOUNT_KEY
        else:
            mount = str(mount).strip().lower()
        tip_rack_slots = [str(s) for s in listify(tip_rack_slots)]

        pipette_id = self._generate_id("pipette")
        max_vol = self._pipette_max_volume(pipette_name)
        channels = self._channels_from_name(pipette_name)

        tip_racks = [self.config["loaded_labware"][s][0] for s in tip_rack_slots]

        self.config["loaded_instruments"][mount] = {
            "name": pipette_name,
            "pipette_id": pipette_id,
            "tip_racks": tip_racks,
        }
        self.pipette_info[mount] = {
            "id": pipette_id,
            "name": pipette_name,
            "min_volume": 1,
            "max_volume": max_vol,
            "aspirate_flow_rate": 150,
            "dispense_flow_rate": 300,
            "channels": channels,
        }

        if not reload:
            self.config["available_tips"][mount] = [
                (rack_id, well)
                for rack_id in tip_racks
                for well in TIPRACK_WELLS
            ]

        self._update_pipettes()
        self._update_pipette_ranges()
        self.config._update_history()
        self.log_info(
            f"Virtual: loaded '{pipette_name}' on {mount} mount → {pipette_id} "
            f"({len(self.config['available_tips'].get(mount, []))} tips)"
        )
        return pipette_id

    def configure_nozzle_layout(self, config_type="full96", **kwargs):
        """Configure the 96-channel nozzle layout in-memory (no HTTP call)."""
        valid = ("full96", "column", "single")
        if config_type not in valid:
            raise ValueError(
                f"config_type must be one of {list(valid)!r}. Received: {config_type!r}"
            )
        instrument = self.config.get("loaded_instruments", {}).get(_96CH_MOUNT_KEY)
        if instrument is None:
            raise RuntimeError("No 96-channel pipette loaded.")
        instrument["nozzle_layout"] = config_type
        self.config._update_history()
        self.log_info(f"Virtual: 96-channel nozzle layout set to {config_type!r}")
        return config_type

    def load_gripper(self):
        gripper_id = self._generate_id("gripper")
        self.config["loaded_gripper"] = {
            "gripper_id": gripper_id,
            "serial": "VIRTUAL-GRP",
        }
        self.config._update_history()
        self.log_info(f"Virtual: gripper loaded → {gripper_id}")
        return gripper_id

    def move_labware(self, source_slot, dest_slot, use_gripper=True):
        """Simulate a labware move by updating config tracking."""
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

        labware_id, labware_name, labware_data = self.config["loaded_labware"][source_slot]
        strategy = "usingGripper" if use_gripper else "manualMoveWithoutPause"
        dest_str = str(dest_slot).strip().lower()

        del self.config["loaded_labware"][source_slot]
        if dest_str != "offdeck":
            flex_dest = self._normalize_slot(dest_slot)
            self.config["loaded_labware"][flex_dest] = (labware_id, labware_name, labware_data)
        else:
            flex_dest = "offDeck"
        self.config._update_history()

        self.log_info(
            f"Virtual: moved '{labware_name}' from slot {source_slot} to "
            f"{flex_dest} ({strategy})"
        )
        return {
            "source_slot": source_slot,
            "dest_slot": flex_dest,
            "strategy": strategy,
            "labware_id": labware_id,
        }

    # ------------------------------------------------------------------
    # Atomic command execution — real tip-state tracking, no HTTP
    # ------------------------------------------------------------------

    def _execute_atomic_command(
        self, command_type, params=None, wait_until_complete=True, timeout=None,
        check_run_status=True
    ):
        """Execute a single Opentrons command in-memory.

        All liquid-handling commands (aspirate, dispense, mix, touch tip, …)
        are logged.  Tip pickup and drop update ``has_tip`` and consume from
        ``config["available_tips"]`` so that the real tip-tracking logic in
        :meth:`transfer` works correctly.
        """
        if params is None:
            params = {}

        if command_type == "pickUpTip":
            mount = params.get("pipetteMount")
            tips = self.config["available_tips"].get(mount, [])
            if not tips:
                raise RuntimeError(f"No tips available for {mount} mount")
            tiprack_id, well = self.get_tip(mount)
            self.has_tip = True
            self.last_pipette = mount
            self.log_info(f"Virtual: picked up tip from {tiprack_id} {well} ({mount})")

        elif command_type == "dropTipInPlace":
            self.has_tip = False
            self.log_info("Virtual: dropped tip")

        elif command_type == "moveToAddressableAreaForDropTip":
            # Pre-drop positioning — nothing to simulate physically
            self.log_info(
                f"Virtual: move to trash area "
                f"'{params.get('addressableAreaName', 'unknown')}'"
            )

        else:
            self.log_info(f"Virtual: {command_type}")

        return True

    # ------------------------------------------------------------------
    # Convenience reset
    # ------------------------------------------------------------------

    def reset(self):
        """Reset all virtual deck and instrument state."""
        self.config["loaded_labware"] = {}
        self.config["loaded_instruments"] = {}
        self.config["loaded_modules"] = {}
        self.config["available_tips"] = {}
        self.config["prep_targets"] = []
        self.config["loaded_gripper"] = None
        self.pipette_info = {}
        self.has_tip = False
        self.last_pipette = None
        self.run_id = None
        self.modules = {}
        self.sent_custom_labware = {}
        self.min_transfer = None
        self.max_transfer = None
        self.log_info("Virtual Flex reset")

    @Driver.quickbar(
        qb={
            "button_text": "Transfer",
            "params": {
                "source": {"label": "Source Well", "type": "text", "default": "1A1"},
                "dest": {"label": "Dest Well", "type": "text", "default": "1A1"},
                "volume": {"label": "Volume (uL)", "type": "float", "default": 300},
            },
        }
    )
    def transfer(self, source, dest, volume, **kwargs):
        """Transfer fluid — runs the full real logic through virtual stubs."""
        return super().transfer(source, dest, volume, **kwargs)


if __name__ == "__main__":
    from AFL.automation.shared.launcher import *
