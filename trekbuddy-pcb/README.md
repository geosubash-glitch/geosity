# TrekBuddy main PCB: rough placement v0.1

Open `trekbuddy_main.kicad_pcb` in KiCad 7, 8 or 9. To change anything, edit `build_pcb.py` and run `python3 build_pcb.py`.

## What's in it
- Board outline traced from `enclosure_sketch.jpg` and scaled from the 37.503 mm dimension. Extents are about 56 x 61 mm. `outline_check.png` shows the outline (red) over the sketch.
- Notches clear the four enclosure screw bosses.
- Two 2.2 mm NPTH holes (H1, H2) at the two small round holes drawn inside the board on the sketch, which measure about 2.0 mm there.
- 4 copper layers, 1.6 mm.
- 17 placeholder parts sized to the body, with no pads. Courtyards are on F.CrtYd.
- A keepout on all copper layers under the ESP32 antenna. Its size is approximate.
- DRC was run with KiCad 7.0.11 (`drc.rpt`). The only warnings say the placeholder footprint libraries aren't installed, which is expected.

## Placement
| Ref | Part | Where |
|---|---|---|
| U1 | ESP32-S3-WROOM-2 | Top left, antenna at the top edge |
| U2, J3 | NEO-F10N + U.FL for an external GNSS antenna | Top right |
| U3 | eMMC BGA-153 | Middle left |
| BZ1 | Piezo buzzer | Middle |
| J1 | Display FPC | Middle |
| SW1, SW2 | Power and mic buttons | Right edge |
| U4, L1 | BQ25895 + inductor | Lower middle, close to USB-C |
| U5, J2 | MAX17048 + battery connector | Right lobe |
| U6, L2 | 3.3 V buck | Lower left |
| U7, MK1 | BMI270, MEMS mic | Lower left |
| J4 | USB-C | Bottom edge. It overhangs by about 0.9 mm, so check it against the cutout. |

## Assumptions to check
- The NEO-F10N has no built-in antenna, so J3 is a U.FL for an external patch or chip antenna.
- The keepout size, the USB-C overhang and the button positions are guesses until checked against the datasheets and the enclosure.
- The mic needs a sound port in the enclosure near MK1.
- Open items 3-8 in `HANDOFF_previous.md` still apply (pinouts, schematic, routing).
