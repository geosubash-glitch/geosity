# TrekBuddy main PCB: handoff

## Files in this folder
- trekbuddy_main.kicad_pcb: board outline (Edge.Cuts), four 2.2 mm mounting holes, placeholder courtyard boxes for 13 parts. No pads, nets, or traces yet.
- trekbuddy_block_diagram.kicad_sch: block-level schematic. One box per part and the connection notes. No pins or wires.
- trekbuddy_layout_mockup.svg: top-view render of the skeleton layout.
- build_pcb.py and build_sch.py are in /home/claude/trekbuddy_src/ (generators; rerun to regenerate).

## Decisions so far
- Custom PCB is required. The BOM uses bare ICs (BQ25895 QFN-24, BMI270 LGA, MAX17048, eMMC BGA, NEO-F10N) that a dev board can't host in this enclosure.
- Recommended stack-up: 4 layers, 1.6 mm, to keep a ground plane under the GPS and ESP32 antenna area.
- Board outline traced from the enclosure sketch. Scale taken from the 37.50 mm dimension. Accuracy about ±0.5 mm.

## Open items (in order)
1. Confirm mounting hole positions against the enclosure bosses. Current holes are my placement, inset 3-4.5 mm inside the corners.
2. Replace placeholder part sizes with datasheet footprints. ESP32-S3-WROOM-2 and NEO-F10N sizes especially.
3. Pinouts: BQ25895 (24-pin WQFN) partly confirmed; MAX17048 package (TDFN-8 vs WLP-8) not confirmed. Other parts not yet checked. Use official datasheets only.
4. Write the schematic with pin-level wiring, starting with the power section (USB-C, BQ25895, MAX17048, LiPo, regulators).
5. Check the USB-C position: its body overhangs the bottom edge at the current placement. Confirm against the enclosure cutout.
6. Confirm the power and mic buttons stay on the main board (current assumption) or move to a separate button board.
7. Decide whether the display sits on this board (only the FPC connector J1 is here) or on its own board.
8. Route, add copper pours, run ERC and DRC in KiCad. Nothing has been opened in KiCad yet.

## Connection lists (source of truth for the block diagram)
- ESP32 to GPS, display, eMMC, microphone, motion sensor, battery monitor, charging IC, buzzer (output), microphone button (input).
- USB-C to charging IC. USB-C to ESP32 USB data (table only; not in the first list).
- Charging IC to battery and voltage regulators. Battery to battery monitor.
- Power button to power management circuit. Regulators feed ESP32, GPS, display, storage, sensors.

## Discrepancies to resolve
- USB data line to ESP32 appears in the table but not in the first list.
- Microphone button: bidirectional in the first list, input-only in the table.
- Power button has no ESP32 link.
