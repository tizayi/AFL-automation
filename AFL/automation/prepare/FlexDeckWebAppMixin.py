"""FlexDeckWebAppMixin — deck visualiser for the Opentrons Flex (OT-3).

Replaces :class:`OT2DeckWebAppMixin` for any class that inherits from
:class:`FlexHTTPDriver`.  Key differences from the OT2 UI:

* 4-row × 3-column deck layout (rows A–D, columns 1–3).
* Optional staging column (A4–D4) shown when the deck config includes a
  staging-area fixture.
* Gripper status panel below the pipettes section.
* Flex pipette names in the load-instrument widget.
* Flex labware names in the load-labware chooser.
"""
import json
import pathlib
import re

from jinja2 import Template
from AFL.automation.APIServer.Driver import Driver
from AFL.automation.prepare.OT2DeckWebAppMixin import OT2DeckWebAppMixin

# Flex-specific labware options shown in the load-labware dialog.
FLEX_LABWARE_OPTIONS = {
    "opentrons/opentrons_flex_96_tiprack_50ul":   "Flex 96 Tiprack 50 µL",
    "opentrons/opentrons_flex_96_tiprack_200ul":  "Flex 96 Tiprack 200 µL",
    "opentrons/opentrons_flex_96_tiprack_1000ul": "Flex 96 Tiprack 1000 µL",
    "opentrons/corning_96_wellplate_360ul_flat":  "Corning 96 Well Plate",
    "opentrons/nest_96_wellplate_2ml_deep":       "NEST 2 mL 96 Deep Well",
    "opentrons/nest_1_reservoir_290ml":           "NEST 290 mL Reservoir",
    "custom_beta/nest_96_wellplate_1p6ml_deep_afl": "NEST 1.6 mL Deep Well (AFL)",
    "custom_beta/nist_6_20ml_vials":              "NIST 6 × 20 mL vial carrier",
    "custom_beta/nist_2_100ml_bottles":           "NIST 2 × 100 mL bottle carrier",
    "heaterShakerModuleV1":                       "Heater-Shaker (needs labware on top)",
    "magneticBlockV1":                            "Magnetic Block",
    "temperatureModuleV2":                        "Temperature Module GEN2",
    "thermocyclerModuleV2":                       "Thermocycler (occupies slots 7+10)",
    "absorbanceReaderV1":                         "Absorbance Reader",
}

# Staging slots — enabled when the deck config includes a staging-area fixture.
_STAGING_SLOTS = {
    "A4": "A4", "B4": "B4", "C4": "C4", "D4": "D4",
}

# Cutout IDs that enable the staging column.
# Includes the combined staging+waste-chute fixture variants.
_STAGING_CUTOUT_IDS = {
    "cutoutA4", "cutoutB4", "cutoutC4", "cutoutD4",
    "stagingAreaRightSlot",
    "stagingAreaRightSlotAndWasteChute",
    "stagingAreaLeftSlot",
}

# Static mapping from right-column cutout IDs to Flex alphanumeric slot names.
# Used to detect which slot holds a trash or waste-chute fixture.
_TRASH_CUTOUTS = {
    "cutoutA3": "A3", "cutoutB3": "B3", "cutoutC3": "C3", "cutoutD3": "D3",
}


class FlexDeckWebAppMixin(OT2DeckWebAppMixin):
    """Deck visualiser mixin for the Opentrons Flex.

    Inherits :meth:`_generate_ot2_well_svg` from :class:`OT2DeckWebAppMixin`
    (well rendering is robot-agnostic) and overrides :meth:`visualize_deck`.
    """

    def _has_staging_area(self):
        """Return True if any deck-config entry enables the staging column."""
        deck_config = self.config.get("deck_configuration", [])
        for entry in deck_config:
            cutout_id = entry.get("cutoutId", "")
            fixture_id = entry.get("cutoutFixtureId", "")
            if cutout_id in _STAGING_CUTOUT_IDS or fixture_id in _STAGING_CUTOUT_IDS:
                return True
        return False

    def _get_flex_slot_info(self, slot_key, compact):
        """Build the slot info dict for *slot_key* (OT2 numeric string or staging label).

        Returns the same structure expected by the Jinja template.
        """
        # slot_key is already a Flex alphanumeric (e.g. "D1") or staging ("A4").
        slot_str = str(slot_key).upper()
        # slot_label == slot_str for Flex keys; kept for template compatibility.
        display_label = slot_str

        info = {
            "name": "Empty",
            "type": "empty",
            "color": "#f5f5f5",
            "svg": "",
            "slot_label": display_label,
            "click_attr": "",
            "buttons": "",
        }

        # Check for trash fixture in this slot
        deck_config = self.config.get("deck_configuration", [])
        for entry in deck_config:
            fixture = entry.get("cutoutFixtureId", "")
            cutout = entry.get("cutoutId", "")
            if "trash" in fixture.lower() or "wasteChute" in fixture.lower():
                mapped_flex = _TRASH_CUTOUTS.get(cutout)
                if mapped_flex == slot_str:
                    info.update({
                        "name": "Trash / Waste",
                        "type": "trash",
                        "color": "#ffcdd2",
                    })
                    return info

        has_labware = slot_str in self.config["loaded_labware"]
        has_module = slot_str in self.config["loaded_modules"]

        if has_labware:
            labware_id, labware_type, labware_data = self.config["loaded_labware"][slot_str]
            definition = labware_data.get("definition", {})
            display_name = definition.get("metadata", {}).get("displayName", labware_type)
            is_tiprack = (
                "tiprack" in labware_type.lower()
                or definition.get("metadata", {}).get("displayCategory") == "tipRack"
            )

            def _well_key(w):
                m = re.match(r"([A-Za-z]+)(\d+)", w)
                return (m.group(1), int(m.group(2))) if m else (w, 0)

            wells = sorted(definition.get("wells", {}).keys(), key=_well_key)

            mounts = []
            if is_tiprack:
                for m, d in self.config["loaded_instruments"].items():
                    if labware_id in d.get("tip_racks", []):
                        mounts.append(m)

            info.update({
                "name": display_name[:20] + ("..." if len(display_name) > 20 else ""),
                "type": "labware",
                "color": "#bbdefb",
                "svg": self._generate_ot2_well_svg(
                    labware_data,
                    available_tips=self.config.get("available_tips", {}),
                    size=50 if compact else 90,
                    labware_uuid=labware_id,
                    compact=compact,
                ),
            })

            if is_tiprack:
                info["tiprack"] = True
                info["mounts"] = mounts
                info["color"] = "#fff3e0"
                info["buttons"] = "".join(
                    f"<button style='margin-top:4px;font-size:10px;' "
                    f"onclick=\"resetTipracks('{m}')\">Reset {m}</button>"
                    for m in mounts
                )

            if len(wells) > 10:
                target_str = ",".join(f"{slot_str}{w}" for w in wells)
                info["buttons"] += (
                    f"<button style='margin-top:4px;font-size:10px;' "
                    f"onclick=\"openPrepTargetDialog('{slot_str}','{target_str}')\">"
                    "Targets</button>"
                )

        if has_module:
            _, module_type = self.config["loaded_modules"][slot_str]
            module_name = module_type.replace("ModuleV", " v").replace("Module", " Mod")
            if has_labware:
                info["name"] = f"{module_name}<br><small>{info['name']}</small>"
                info["color"] = "#c8e6c9"
            else:
                info.update({
                    "name": module_name,
                    "type": "module_only",
                    "color": "#e1bee7",
                })

        if info["type"] in ("empty", "module_only"):
            info["click_attr"] = (
                f"onclick=\"showLabwareOptions('{slot_str}')\" style=\"cursor:pointer;\""
            )
        elif info["type"] == "labware":
            safe_name = display_name.replace("'", "\\'")
            info["click_attr"] = (
                f"onclick=\"openMoveLabwareDialog('{slot_str}', '{safe_name}')\" "
                f"style=\"cursor:pointer;\""
            )

        return info

    @Driver.unqueued(render_hint="html")
    def visualize_deck(self, mode="full", **kwargs):
        """Render the Flex deck as an HTML page.

        The layout is 4 rows (A=back → D=front) × 3 columns (1–3), with an
        optional staging column (A4–D4) when the deck config includes a
        staging-area fixture.
        """
        compact = mode == "simple"

        # Base layout: rows top→bottom are A→D (back to front of robot).
        # All keys are Flex alphanumeric end-to-end.
        base_layout = [
            ["A1", "A2", "A3"],  # Row A (back)
            ["B1", "B2", "B3"],  # Row B
            ["C1", "C2", "C3"],  # Row C
            ["D1", "D2", "D3"],  # Row D (front)
        ]
        staging_slots = []
        has_staging = self._has_staging_area()
        if has_staging:
            staging_slots = ["A4", "B4", "C4", "D4"]

        slot_infos = {}
        for row in base_layout:
            for slot in row:
                slot_infos[slot] = self._get_flex_slot_info(slot, compact)

        for s in staging_slots:
            slot_infos[s] = self._get_flex_slot_info(s, compact)

        # Gripper info
        gripper = self.config.get("loaded_gripper")
        gripper_info = None
        if gripper:
            gripper_info = {
                "serial": gripper.get("serial", "unknown"),
                "gripper_id": gripper.get("gripper_id", "unknown"),
            }

        base = pathlib.Path(__file__).parent.parent / "apps" / "flex_deck"
        html_template = (base / "flex_deck.html").read_text()
        css = (base / "css" / "style.css").read_text()
        js = (base / "js" / "main.js").read_text()

        all_slots = [s for row in base_layout for s in row] + staging_slots

        from jinja2 import Template
        template = Template(html_template)
        return template.render(
            slot_layout=base_layout,
            staging_slots=staging_slots,
            slot_infos=slot_infos,
            loaded_instruments=self.config.get("loaded_instruments", {}),
            gripper_info=gripper_info,
            mode=mode,
            deck_data_json=json.dumps({
                "labwareChoices": FLEX_LABWARE_OPTIONS,
                "gripperLoaded": gripper_info is not None,
                "allSlots": all_slots,
            }),
            inline_css=css,
            inline_js=js,
        )
