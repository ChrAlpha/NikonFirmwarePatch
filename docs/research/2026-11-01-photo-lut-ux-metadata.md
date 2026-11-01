# Photo LUT UX and metadata: Panasonic REAL TIME LUT, Nikon Picture Controls, and Z8 gaps

Research date: 2026-11-01. Code baseline: `9bb6071`.
Evidence copies, SHA-256 hashes, and local check outputs are in
`nxc-data/scratch/z8-photo-lut-ux-evidence-20261101/research/` (`web/`, `upstream/exiftool/`, `checks/`, `evidence-sha256.txt`).

This report separates three things: **facts** (sections 1–3, each tiered and cited), **current gaps** in the baseline (section 4), and **proposals** that still need owner and behavior review (section 5). No proposal here is an approved product decision.

## Evidence tiers

| Tier | Meaning |
|---|---|
| **A** | Vendor primary document (operating manual, help, firmware notes, press release, support page), fetched and read |
| **A-code** | Upstream or project source code at a pinned commit, read directly |
| **B** | Maintainer-accepted issue with a user sample, local tool behavior, or this project's QEMU artifact (not a physical camera) |
| **C** | Third-party user report with no published sample; describes one user's file, not general camera behavior |
| **—** | Not established. No claim made |

## 1. Panasonic REAL TIME LUT and LUMIX Lab (facts)

Reference body: the LUMIX S9 HTML operating manual (DVQP3138). Page IDs link to the sources at the end.

| Topic | Finding (paraphrased) | Tier | Source |
|---|---|---|---|
| Library capacity | The S9 registers up to 39 LUTs. Out of the box it holds `Vlog_709` plus three sample LUTs, and the menu lists `[Set4]`–`[Set39]`. | A | [S9-0074], [S9-0113] |
| Capacity varies by model | The S5M2/S5M2X support page gives 10 registration destinations. | A | [EU-FAQ] |
| Card import | LUT files are placed in the card root and loaded through `[LUT Library]` › destination › `[Load]`. Loading onto an occupied destination replaces it. | A | [S9-0074] |
| Filename limits | Limits depend on the card file system: FAT32 allows 8 characters before the extension, and exFAT allows 255 including the extension. Alphanumeric names are advised. | A | [S9-0074] |
| Formats and grid | Accepts `.vlt` (VariCam 3D LUT spec) and `.cube`; `.cube` must be a 3D LUT of 2 to 33 points. | A | [S9-0074] |
| Library management | Offers `[Sorting]` (reorder), `[Edit Title]` (rename), and `[Delete]`. `Vlog_709` is fixed. Sample LUTs can be sorted or deleted but return after a reset. | A | [S9-0074] |
| Reset | Resetting setup/custom settings restores the library defaults and removes user LUTs. | A | [S9-0023] |
| Phone path | LUMIX Lab's `[LUT Transfer]` works over Wi-Fi. It shows separate phone and camera lists and supports transfer, rename, delete, and reorder. | A | [S9-0125] |
| Extended looks | Firmware Ver.2.0, with LUMIX Lab 3.0.0 or later, lets REAL TIME LUT use LUTs carrying grain, color-noise, sharpness, and NR parameters. Grain and color noise apply to photos only. | A | [S9-FW] |
| **Input basis** | A `.cube` may declare its base Photo Style with a `#LUMIXPHOTOSTYLE <TAG>` line below the title. There are 18 tags, including STD, NAT, FLAT, CNED2, CNEV2, 709L, 2100HLG, 2100HLGF, and VLOG. A missing or invalid tag is treated as V-Log. The camera then switches to that base style automatically. | A | [S9-0074], [S9-0071] |
| Basis for editor-made LUTs | Panasonic tells users to shoot with the same Photo Style that the LUT was built for, and warns that a mismatch can display or record incorrectly. | A | [S9-0071] |
| Range | Panasonic recommends full-range LUTs for stills. | A | [S9-0071], [S9-0074] |
| Fidelity | Panasonic says in-camera results will not exactly match the same LUT applied in editing software. | A | [S9-0071] |
| **Strength** | `[LUT Opacity]` is set per LUT. My Photo Style can stack two LUTs, with LUT1's output feeding LUT2, and the base style follows LUT1. The manual text gives no numeric range. | A | [S9-0071] |
| Style names | My Photo Style names allow 22 characters, and double-byte characters count as two. | A | [S9-0071] |
| **RAW** | LUTs are not applied to RAW data. In RAW-only shooting, the LUT affects only live view and playback thumbnails. | A | [S9-0071] |
| RAW re-application | The in-camera `[RAW Processing]` menu (RW2 → JPEG) offers `[LUT]` LUT1/LUT2 and `[LUT Opacity]`, picked from the camera's own library. `[REAL TIME LUT]` itself cannot be selected there. | A | [S9-0105] |
| View assist | LUT View Assist accepts only V-Log-based LUTs. | A | [S9-0089] |

### Panasonic still metadata

| Field | Container | Finding | Tier | Source |
|---|---|---|---|---|
| `LUT1Name` 0x00f1 (string), `LUT1Opacity` 0x00f3 (int8u, percent), `LUT2Name` 0x00f4, `LUT2Opacity` 0x00f5 | Panasonic **MakerNote** IFD (`Panasonic::Main`) | Defined in upstream `Panasonic.pm` lines 1502–1517 with the comment `#github365`. They shipped in 13.44 (2025-12-15). Names use uppercase `LUT`, and no 0x00f2 tag is defined. The local ExifTool 13.59 lists all four. | A-code | [ET-PANA], [ET-CHANGES] |
| Meaning of those tags | The issue #365 reporter inferred the meanings from one JPEG (look `Fuji_CLASSIC-Neg`, opacity 0–100) and called them a guess. The camera model is not given. The maintainer added the tags as proposed. Panasonic documents none of these fields. | B | [ET-365] |
| Presence in RW2 | One S5M2 user's RW2 shows `LUT1Name` = `xjd to fujicn.vlt`, `LUT1Opacity` = 100, and `PhotoStyle` = `Unknown (21)`. The same user says the embedded preview carries the look. No sample is published. This describes one file, not a guarantee for other models or firmware. | C | [LC-2107] |
| Name source | The single C-tier value includes a file extension, which hints that the loaded filename is stored rather than the `[Edit Title]` text. Not established. | C / — | [LC-2107] |
| `PhotoStyle` 0x0089 | Upstream has no value mapping for REAL TIME LUT or My Photo Style. | A-code | [ET-PANA] line 1137 |
| General EXIF/XMP | No primary source shows Panasonic writing the LUT into standard EXIF or XMP. | — | — |
| Writability | ExifTool marks all four tags writable. Finding them in a file does not prove the camera applied a look, and writing them changes no pixels. | A-code | [ET-PANA] |
| Lightroom/ACR | No direct Adobe evidence that Lightroom/ACR shows or re-applies these tags. No claim. | — | — |

## 2. Nikon Z8 Picture Controls, Flexible Color, NX Studio (facts)

| Topic | Finding (paraphrased) | Tier | Source |
|---|---|---|---|
| Firmware | Z8 firmware 3.00 added Flexible Color Picture Control (announced 2025-06-11). The project's input is C 3.11. | A | [NK-PR]; README pin |
| Custom PC slots | Slots are C-1 to C-9, with menu items `[Save/edit]`, `[Rename]`, `[Delete]`, and `[Load/save]`. There is no reorder operation. | A | [Z8-PC] |
| Custom PC name | Names allow up to 19 characters. By default the camera appends a two-digit number to the base name. | A | [Z8-PC] |
| Card I/O | `[Copy to camera]` imports into C-1–C-9, with an optional rename. `[Delete from card]` removes card copies. `[Copy to card]` writes to card positions 1–99. Only the primary slot is read. | A | [Z8-PC] |
| **Flexible Color vs ordinary** | A Flexible Color custom PC can be imported, but the camera can neither edit nor rename it. Ordinary custom PCs can be both edited and renamed. | A | [Z8-PC], [NX-FC] |
| Flexible Color authoring | Flexible Color is created in NX Studio only: Advanced Settings, Color Blender (8 hues × hue/chroma/brightness), and Color Grading (three wheels plus blending and balance). It requires SDR tone mode and the latest Picture Control process, and imports only into cameras that support it. | A | [NX-FC] |
| NX Studio library | NX Studio stores up to 150 custom PCs, each with a name and comment, and supports edit and delete. Deleting one leaves past edits intact. | A | [NX-CPC] |
| NP3 import/export | Import comes from a card (root or `NIKON/CUSTOMPC`; the card must be camera-formatted with `DCIM` and `NIKON001.DSC`) or from a file. Export goes to a file or to `NIKON/CUSTOMPC`. A card holds 99 files per format (NCP/NP2/NP3), 297 in total. Comments can be edited only on file export. | A | [NX-IO] |
| `.cube` relation | Nothing found in NX Studio or Z8 docs relates `.cube` to still Picture Controls. NP3 and `.cube` are not interchangeable. | — | — |
| NEF re-application | The Picture Control tool works only on RAW and merged pixel-shift files. Reverting restores the capture-time settings. RAWs made with Picture Controls that NX Studio lacks fall back to their embedded preview and cannot be edited. | A | [NX-PCT], [NX-ADJ], [NX-FMT] |
| Sidecars | Edits are stored either in `NKSC_PARAM/*.nksc` (linked by filename) or inside the NEF. Edits saved into a NEF do not show on the camera. | A | [NX-SAVE] |
| Playback info | Shooting data lists Picture Control and color space. The user `[Image comment]` (up to 36 characters) appears in playback and in the NX Studio Info tab. | A | [Z8-INFO], [Z8-CMT] |

### Nikon still metadata (ExifTool upstream `Nikon.pm` @ `980409314f`)

| Field | Container | Finding | Tier |
|---|---|---|---|
| `PictureControlData` 0x0023 | Nikon **MakerNote** | Decoded by version prefix `01`/`02`/`03`, with an unknown-version fallback. Flags: writable `undef`, `Protected`, `Permanent => 0` (lines 2043–2071). | A-code |
| `PictureControl3` | inside 0x0023 | Holds `PictureControlName` and `PictureControlBase` as `string[20]` (offsets 8 and 28), followed by the classic adjustments (offsets 54–73). Color Blender and Color Grading are not decoded (lines 3778–3915). | A-code |
| NKSC sidecar | XMP `nine` / `NineEdits` | Edit parameters are opaque base64 (lines 13326+). | A-code |
| `XMP-crd:*` in NEF | XMP | ExifTool labels these Camera Raw Defaults (`XMP2.pl` line 84). How Adobe applications use them is not established. | A-code + B |
| Baseline readback (QEMU) | JPEG + NEF | The Off and `cycle` captures have identical `PictureControlName`/`Base` (`Auto`), `ColorSpace` (`sRGB`), empty `UserComment`, `CustomRendered` (`Custom`), and `Software` (`Ver.03.91`). | B (`baseline-metadata-readback.json`) |
| Custom XMP readability | ExifTool 13.59 read an unregistered `z8lut` namespace probe as `[XMP-z8lut]` tags, with no config. | B (`checks/z8lut-probe.txt`) |

## 3. Project facts relevant to design

| Fact | Tier | Source |
|---|---|---|
| Patch code already uses pinned C3.11 file I/O entry points: `FW_OPEN`, `FW_CLOSE`, `FW_READ`, `FW_WRITE`, `FW_SEEK`, `FW_CREATE`, `FW_RENAME`, `FW_EXISTS`, and `FW_REMOVE`. These show that patched code can read and write card files. They do **not** show when a LUT bundle could be read (boot, menu, capture), who would own its memory, or what happens if the card is removed. | A-code | `z8pixelshift/src/firmware.c:6-17` |
| The camera validates a binary bundle natively: magic, version, CRC, bounds, and titles. Host-precompiled bundles therefore need no `.cube` parser on the camera. | A-code | `z8photolut/src/lut.c` `z8_lut_validate` |
| The kernel converts full-range BT.601 Y'CbCr to R'G'B' from the encoder input, so the LUT sees Nikon-rendered, gamma-encoded values after the active Picture Control and in the JPEG color space. This integer BT.601 conversion is the implemented model, not proven camera colorimetry: the patcher records `color_contract.camera_colorimetry_verified=False`. | A-code | `lut.c` `transform`; `z8photolut/patcher.py:243-244` |
| Strength: the kernel blends in Y'CbCr with `strength` 0–256. The firmware always passes 256 (`firmware.c:611/976/1021`), and the preview-table builder rejects any other value. | A-code | `lut.c`, `firmware.c` |
| The parser discards `.cube` comments, so a file carrying `#LUMIXPHOTOSTYLE VLOG` is accepted without warning. | B (local run on `cube.py` identical to `9bb6071`) | `z8photolut/cube.py` |
| NEF images and previews are deliberately kept separate from the LUT path. | A-code | commit `ec97a35` |

## 4. Current gaps at 9bb6071

| Area | Vendor reference | Current behavior | Gap |
|---|---|---|---|
| Library delivery | Panasonic: card root or app, 39 (S9) or 10 (S5M2) slots. Nikon: NP3 from card into C-1–C-9. | The look set is fixed when the firmware is built: at most 8 `.cube` files, compiled into the image. | No import, export, rename, reorder, or delete without rebuilding and reflashing. |
| Names | Panasonic `[Edit Title]`; Nikon PC names of 19 characters | `TITLE` or file stem, 1–63 UTF-8 bytes, unique ignoring case, shown as a UTF-16 menu string | Camera glyph coverage and row width are unverified. The 19-character Nikon limit belongs to `PictureControlName`, a field this feature does not write, so it does not apply here. |
| Intensity | Per-LUT `[LUT Opacity]` | Fixed at full strength | The kernel supports blending; the UI and preview path do not. |
| Input basis | Declared in the file (`#LUMIXPHOTOSTYLE`), with V-Log as the default | Implicit: the Nikon-rendered encoder input. Source annotations are dropped. | The basis is neither declared nor recorded. Log/HLG-targeted LUTs are not detected. The effect of `Auto` PC and AdobeRGB on authoring expectations is undocumented. |
| RAW | Panasonic does not apply LUTs to RAW. One C-tier RW2 shows a LUT-styled preview and the LUT name. | NEF and its previews are deliberately left untouched. | This is an intentional difference from that C-tier report, not a gap. |
| Metadata | Panasonic MakerNote LUT fields (JPEG: B; RW2: C) | Nothing records the look. | An applied look cannot be identified from the file. |

## 5. Proposals (need owner and behavior review)

**P1. Host library plus card workflow, replacing the fixed build-time set.**
- Target: a standalone host tool manages the library. It handles import, `.cube` validation, resampling, display names, order, delete, and export. It writes a precompiled, CRC-checked bundle that the camera loads from the card and checks with its existing native validator, so the camera never parses `.cube`.
- Three storage models need separate decisions:
  - **Session load into RAM (implementation direction):** an explicit load validates the bundle and copies it from the card into camera RAM. The looks stay usable for that power session after the card is removed or swapped. Persistence across power-off is not implied.
  - **Lazy card-backed reads:** looks are read from a known card path when needed. These depend on that card being present, so the policy for removal or swap must be defined.
  - **Persistent internal storage:** looks are copied into camera memory that survives power-off. No suitable store of the bundle's size has been identified. This is an open investigation, not a known impossibility.
- Proposed fallback policy:
  - If an explicit load fails because the bundle is missing, corrupt, or oversized, the last validated active library stays in use and the failure is shown.
  - If no library is active, or a persisted selection cannot be resolved against the loaded library, the camera behaves as Off and shows that state.
- Investigation items:
  - selection lifetime per photo bank and across reset
  - when loading happens relative to capture
  - memory ownership
- Feasibility is not claimed either way.

**P2. Name handling.**
- Keep the current host-side limits unless a camera gate requires otherwise.
- Gate: measure glyph coverage and row width on the physical camera (QEMU screenshots are supporting evidence only).
- Truncation and disambiguation are this project's design, decided after the gate; for example, a deterministic ellipsis plus the slot index.
- Do not adopt the 19-character `PictureControlName` limit; that Nikon field is not written.

**P3. Color basis: explicit declaration with a documented default.**
- Each look carries a basis that the host tool and manifest record.
- If a look declares nothing, the host tool applies one documented default, the current behavior: Nikon-rendered SDR encoder input. It labels the result as assumed.
- The host tool reads `#LUMIXPHOTOSTYLE`. Explicit log/HLG tags (`VLOG`, `2100HLG`, `2100HLGF`), and declarations it can recognize as malformed or missing a value, are flagged as incompatible with the default and accepted only with an explicit user declaration. It rejects `.vlt` with a clear message.
- When an arbitrary `.cube` carries no annotation, the vendor target cannot be reliably inferred. Such files are treated as Nikon-rendered input, and the tool labels that basis as an explicit assumption.
- Docs must not present Panasonic `.cube`, N-Log LUTs, or NP3 as interchangeable.
- A runtime gate keyed on Picture Control or ColorSpace is **only a candidate**. No input contract shows that `Auto` or AdobeRGB breaks a given LUT. Recording the actual PC and color space alongside the look (P5) exposes mismatches without disabling anything.

**P4. Strength (candidate).**
- The kernel can already blend in Y'CbCr, so exposing stepped strength is a possible menu feature. It needs:
  - a behavior decision: whether Y'CbCr blending is acceptable compared with RGB-domain opacity, and which steps to offer
  - preview-path support for values other than full strength
  - a per-bank persistence design

**P5. Provenance metadata (informs the separate XMP investigation).**
- Constraints from the evidence:
  - Leave `PictureControlData`/`PictureControlName`, `ColorSpace`, `CustomRendered`, and other Nikon MakerNote content unchanged. Spoofing them recreates no look and can make NX Studio fall back to preview-only.
  - `UserComment` holds the user's Image Comment and is shared by NEF and JPEG.
  - ExifTool reads a custom XMP namespace without configuration (B).
- Candidate JPEG fields: look name, slot index, strength, sample hash from the manifest, declared basis, firmware identity, and the PC and color space at capture.
- Whether the NEF records the selection is a design choice. Panasonic precedent here is C-tier only, and writing to the NEF must not compromise its isolation.

**Optional future direction (not in scope).** A host tool could re-apply a look to an exported render using the recorded sample hash, labelled as approximate. Management and metadata do not depend on it.

## 6. Acceptance matrix

"Fact" rows check behavior this report has established. "Proposal" rows apply only if the matching proposal is adopted.

| # | Kind | Check | Pass criterion | Evidence needed |
|---|---|---|---|---|
| 1 | Proposal P1 | Host library operations | Import, rename, reorder, delete, and export produce a bundle that `validate_bundle` and `z8_lut_validate` accept. Order and names match the library. | Host unit tests |
| 2 | Proposal P1 | Card load | A valid bundle is loaded into the session and stays selectable after the card is removed. A failed explicit load (missing, corrupt, or oversized bundle) keeps the last validated active library and shows the failure. With no active library, or a persisted selection that cannot be resolved, the camera behaves as Off and shows it. Capture never stalls. | QEMU first, then physical camera |
| 3 | Proposal P1 | Lifetime | The defined behavior holds across power cycles, card removal/swap, bank switching, and bank/camera reset. | QEMU NVRAM readback plus physical camera |
| 4 | Fact + P2 | Names | Every bundled name renders legibly within the row on the camera. Truncated names stay distinguishable. | Physical camera LCD/EVF, human-observed; QEMU screenshot supporting |
| 5 | Proposal P3 | Basis | Unannotated looks get the documented Nikon-rendered default, labelled as assumed. Explicit log/HLG `#LUMIXPHOTOSTYLE` tags, and recognizably malformed or missing-value declarations, are refused without an explicit declaration. `.vlt` is rejected. The basis appears in the manifest. | Host unit tests |
| 6 | Proposal P4 | Strength | 0% is byte-identical to Off, and 100% matches the current output. Intermediate steps are monotone. Preview and JPEG agree within the stated tolerance. | Kernel tests, QEMU capture, physical camera |
| 7 | Proposal P5 | JPEG provenance | The fields read back with ExifTool and match the manifest. The Nikon MakerNote is byte-identical to an Off capture apart from per-shot fields. | QEMU readback (B), then physical camera |
| 8 | Fact | NEF isolation | NEF image data and previews are identical between Off and LUT captures apart from per-shot fields. `rawpy` decodes the file, and NX Studio opens and edits it. | `tools/check_photo_lut_capture.py`; NX Studio on a physical-camera file |
| 9 | Observation | Third-party display | Record what Lightroom/ACR and NX Studio actually show, with application versions. Assume nothing. | Human observation |

QEMU results are tier B and do not replace physical-camera or human acceptance.

## 7. Open questions

- Panasonic `[LUT Opacity]` range and step size (the manual is silent; issue #365 reports 0–100).
- Whether `LUT1Name` stores the filename or the `[Edit Title]` text, and which Panasonic models and firmware write 0x00f1–0x00f5.
- Whether Adobe applications display or use Panasonic LUT tags or Nikon `XMP-crd`.
- Z8 C3.11: when card bundles could be loaded, a persistent store of the required size, and how selection lifetime interacts with banks and resets (P1).
- Z8 C3.11 JPEG XMP/IFD0 writer hook points (separate investigation, P5).
- Menu font glyph coverage and row width (P2).

## Sources

All of the following were fetched and read on 2026-11-01; local copies and hashes are in `evidence-sha256.txt`.

- [S9-0023] https://eww.pavc.panasonic.co.jp/dscoi/DC-S9/html/DC-S9_DVQP3138_eng/0023.html
- [S9-0071] https://eww.pavc.panasonic.co.jp/dscoi/DC-S9/html/DC-S9_DVQP3138_eng/0071.html
- [S9-0074] https://eww.pavc.panasonic.co.jp/dscoi/DC-S9/html/DC-S9_DVQP3138_eng/0074.html
- [S9-0089] https://eww.pavc.panasonic.co.jp/dscoi/DC-S9/html/DC-S9_DVQP3138_eng/0089.html
- [S9-0105] https://eww.pavc.panasonic.co.jp/dscoi/DC-S9/html/DC-S9_DVQP3138_eng/0105.html
- [S9-0113] https://eww.pavc.panasonic.co.jp/dscoi/DC-S9/html/DC-S9_DVQP3138_eng/0113.html
- [S9-0125] https://eww.pavc.panasonic.co.jp/dscoi/DC-S9/html/DC-S9_DVQP3138_eng/0125.html
- [S9-FW] https://av.jpn.support.panasonic.com/support/global/cs/dsc/download/ff/dl/s9.html
- [EU-FAQ] https://support-uk.panasonic.eu/app/answers/detail/a_id/10529/~/how-can-i-add-custom-lut-files-on-my-lumix-s5m2-&-s5m2x-series-camera
- [Z8-PC] https://onlinemanual.nikonimglib.com/z8/en/picture_controls_37.html
- [Z8-INFO] https://onlinemanual.nikonimglib.com/z8/en/photo_information_50.html
- [Z8-CMT] https://onlinemanual.nikonimglib.com/z8/en/sum_image_comment_335.html
- [NK-PR] https://www.nikonusa.com/press-room/z-8-firmware-3-point-0
- [NX-IO] https://www.nikonimglib.com/nxstdo/onlinehelp/en/copy_custom_picture_controls_50.html
- [NX-CPC] https://www.nikonimglib.com/nxstdo/onlinehelp/en/about_custom_picture_controls_49.html
- [NX-FC] https://www.nikonimglib.com/nxstdo/onlinehelp/en/the_picture_controls_flexible_color_23.html
- [NX-PCT] https://www.nikonimglib.com/nxstdo/onlinehelp/en/the_picture_controls_tool_22.html
- [NX-ADJ] https://www.nikonimglib.com/nxstdo/onlinehelp/en/the_adjustments_tab_20.html
- [NX-FMT] https://www.nikonimglib.com/nxstdo/onlinehelp/en/supported_formats_4.html
- [NX-SAVE] https://www.nikonimglib.com/nxstdo/onlinehelp/en/save_76.html
- [ET-PANA] https://github.com/exiftool/exiftool/blob/980409314fddbd83d9092b5c160fafab7f67fd52/lib/Image/ExifTool/Panasonic.pm
- [ET-NIKON] https://github.com/exiftool/exiftool/blob/980409314fddbd83d9092b5c160fafab7f67fd52/lib/Image/ExifTool/Nikon.pm
- [ET-XMP2] https://github.com/exiftool/exiftool/blob/980409314fddbd83d9092b5c160fafab7f67fd52/lib/Image/ExifTool/XMP2.pl
- [ET-CHANGES] https://github.com/exiftool/exiftool/blob/980409314fddbd83d9092b5c160fafab7f67fd52/Changes
- [ET-365] https://github.com/exiftool/exiftool/issues/365 (read with `gh api`)
- [LC-2107] https://github.com/storytold/lightcraft/issues/2107 (read with `gh api`; C tier)
- Project: `baseline-metadata-readback.json` (supervisor-provided QEMU readback), `z8pixelshift/src/firmware.c`, and `z8photolut/` at `9bb6071`

Not retrieved, so not used as evidence: the Panasonic LUMIX Lab and REAL TIME LUT marketing pages (HTTP 403), `nikon-asia.com` (HTTP 403), the Nikon support FAQ (TLS error), and the Nikon download center page 606 (HTTP 404 to curl).
