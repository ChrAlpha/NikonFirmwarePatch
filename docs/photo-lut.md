# 照片 LUT / Photo LUT

[简体中文](#简体中文) · [English](#english)

**开发版功能，尚未通过实机验收。** Development feature without physical-camera acceptance.

## 简体中文

照片 LUT 把一个 3D LUT 应用到相机已渲染的照片：即 JPEG 在当前优化校准（Picture Control）之后、所选色彩空间中的 8 位 Y'CbCr 值。实时取景和 JPEG 使用同一风格；**NEF 原始数据不变**。

### 风格库文件

风格库是一个可携带文件 `Z8LUTLIB.BIN`，与固件分开管理，最多 8 个风格：

- 前段是相机实际应用的运行网格（17/33/65 点，总计不超过 2 MiB）；
- 后段保存每个风格原始 `.cube` 的完整字节，只供电脑端使用。

每个风格有一个**变换标识**：对相机实际应用的网格及其语义计算的 SHA-256。改名、调整顺序都不改变它；内容不同则标识不同。风格名为 1–63 字节 UTF-8（可用中文），不能含控制字符或首尾空格。

```sh
python photo_lut.py library create looks.bin                        # 空库
python photo_lut.py library add looks.bin film.cube --name "胶片 A"   # 可选 --grid 17|33|65、--position N
python photo_lut.py library list looks.bin                          # 顺序、名称、标识、来源
python photo_lut.py library rename looks.bin "胶片 A" "胶片 B"
python photo_lut.py library move looks.bin "胶片 B" 1
python photo_lut.py library remove looks.bin 2                      # 可删到空库
python photo_lut.py library export looks.bin 1 原始.cube --source    # 与导入时逐字节相同
python photo_lut.py library export looks.bin 1 运行.cube --runtime   # 相机实际应用的重采样网格
python photo_lut.py library verify looks.bin                        # 从原始 .cube 重新推导并核对（较慢）
```

每次修改都先完整写入并读回临时文件，再替换原文件；输入错误时原文件保持不变。

**菜单显示。** 相机菜单以“序号. 名称”显示风格，长名称被截断时可按序号区分。尼康菜单字体只含相机自身菜单用到的字，一个标签只能用一种字体；固件先用当前界面语言的菜单字体，缺字时依次改用英文、简体中文、繁体中文、日文、韩文菜单字体中能完整显示该标签的一种。`library add`、`list`、`card` 会提示这些字体都缺少的字（例如“冬”“季”），这类字可能显示为方框；名称本身仍完整保存在库、导出文件和照片元数据中，需要时可改用能显示的名称。

**输入基准。** 为 Log、HLG/PQ 或其他厂商基准制作的 LUT 不适用于相机渲染后的 SDR 照片。文件名、TITLE 或注释中出现 V-Log、N-Log、S-Log、LogC、F-Log、HLG、PQ、BT.2100/2020 等标记，或带 `#LUMIXPHOTOSTYLE`（松下注释，缺省即 V-Log）、`.vlt` 文件、超出 0..1 的 DOMAIN，都会被拒绝；确认 LUT 确为相机渲染 SDR 制作时，加 `--accept-input-basis`，原因会记录在库中。松下、尼康 NP3 或其他相机的 LUT 与本功能不能互换。

### 固件与存储卡风格

1. **固件只需构建一次**：`python patch.py Z_8_0311.bin --features photo-lut --version NN.NN --tag p0 --output 目录`（可与其他功能组同时选择）。固件不含任何风格，以后增删改风格都不必重新构建。
2. **写入存储卡**：`python photo_lut.py library card looks.bin /存储卡根目录`，在卡根目录生成 `Z8LUTLIB.BIN`。
3. **在相机中载入**：照片拍摄菜单 > 照片 LUT > **从存储卡载入 LUT**。

A–D 拍摄菜单库分别保存完整变换标识，改名或调整顺序后仍能对应原风格；移除或改变网格的风格会显示不可用。单个菜单库重置把该库设为“关闭”，仍保留当前库来源。全局重置把所有菜单库设为“关闭”，并卸载已载入的库。

存储卡载入规则：

| 情况 | 结果 |
|---|---|
| 只有一张卡有有效文件 | 载入（卡槽 1 = CFexpress `A:`，卡槽 2 = SD `B:`） |
| 两张卡文件的运行部分（相机读取的前段）完全相同 | 载入；只含原始 .cube 的后段不同不影响 |
| 两张卡文件的运行部分不同 | 拒绝：“两张卡的 Z8LUTLIB.BIN 不同” |
| 任一卡的文件无效、截断、过大或读取失败 | 拒绝并指出卡槽，即使另一张卡有效 |
| 没有文件 | 拒绝：“两张卡上都没有 Z8LUTLIB.BIN” |

选择“从存储卡载入 LUT”后，菜单页不关闭而是立即重建：该行改为显示结果（如“存储卡 LUT：已载入 8 个（卡槽 1）”），下方列出新风格。拒绝时当前风格和各菜单库选择都不变。

**导入一次，正常关机后自动恢复。** 相机内部保存当前风格库来源和 A–D 各菜单库的完整变换标识；风格网格仍保存在卡上的 `Z8LUTLIB.BIN`。下次开机无需进入菜单、载入或重选，固件会自动读取存储卡并恢复选择。选择“关闭”只关闭效果，保留已导入库的来源；即使四个菜单库都关闭，下次开机仍自动载入该库。原厂菜单的“复制”同时复制该菜单库的 LUT 选择，之后修改来源菜单库不会影响副本。

菜单只列出当前库中的风格：显式载入存储卡库会替换已载入的库；重启继续使用同一来源。原来选择的风格若不在当前库中，会保留原标识并显示不可用，不会误用同位置或同名风格。卡缺失、延迟就绪、文件损坏或两卡冲突时，固件保留选择并自动重试；启动工作任务暂时缺少内存或系统资源时，也由原厂设置任务自动重试。只在当前菜单库需要恢复或尚未载入导入库时重试，未使用菜单库缺少风格不会反复读卡。风格恢复前阻止拍摄，选择“关闭”即可拍摄。已载入的库留在 RAM 中，取出卡不会改变其风格。

首次从旧版升级时，非关闭选择无法还原完整标识，会要求重新选择；带旧版会话标记的关闭选择仍保持关闭。全零的旧配置按从未设置处理，采用固件默认值“关闭”。返回原厂固件后，原厂设置仍可读取，原厂保存会清除本功能的扩展记录。

每次拍摄在分配输出时锁定所选风格：复制其网格、标识和名称，之后改选或载入新库都不影响已开始的拍摄。

### 照片元数据

应用了风格的 JPEG 在其原有 XMP 包中多一个 `photolut` 命名空间（`https://github.com/ChrAlpha/NikonFirmwarePatch/ns/photo-lut/1.0/`）的描述：`LookName`、`TransformSHA256`（即变换标识，可在 `library list` 中对照）、`Applied`、`Rendering=baked-into-jpeg-pixels`、`InputBasis`、`Strength`/`StrengthScale`、`Grid`、`Algorithm`、`Schema`。只占用原包的空白填充，XMP 包、APP1 段和 Exif 偏移的长度都不变；尼康 MakerNote、优化校准、色彩空间、ICC、用户注释和版权信息不动。

- 只在该 JPEG 的主图确实以变换后的像素成功编码时写入；编码失败或风格为“关闭”时不写。
- 无变化（恒等）风格写入 `Applied=False`、`NoOp=True`。
- **NEF 不写**，其图像与元数据保持原样。
- 这是来源记录，不是给 Lightroom 或 NX Studio 的重新套用指令；这些软件不会因此自动套用风格。

查看：`exiftool -XMP-photolut:all DSC_0001.JPG`（ExifTool 13.59 以该命令读出全部字段）。

### 开发者验证

需要 Python 3.10+、`requirements.txt` 中的依赖和 `unicorn==2.1.4`。固件代码是 `z8photolut/src/unit.c` 单一编译单元（含 `hooks.S`），由 `tools/compile_units.py --units photo-lut` 用 clang-19 编译并由仓库自带的链接器链接，审核后的字节记录在 `resources/compiled.json` 与 `resources/photo-lut.bin`；测试执行的就是这份字节。测试用的原厂主程序由官方固件取出：

```sh
python -m pip install -r requirements.txt unicorn==2.1.4
python -c "from z8prores.package import Package; open('z8-main-payload.bi','wb').write(Package(open('Z_8_0311.bin','rb').read()).eg[:-2])"
Z8_PHOTO_LUT_ROM=z8-main-payload.bi python -B -m unittest discover -s tests -p 'test_photo_lut*.py'
```

未设置 `Z8_PHOTO_LUT_ROM` 时只运行宿主测试；`Z8_PHOTO_LUT_SAMPLE_JPEG` 可额外指定一张原厂 JPEG 供 XMP 辅助测试使用。

### 验证范围

宿主测试覆盖库格式、编辑、导出、错误输入和与相机校验代码的一致性；自动恢复、延迟读取时的选择合并、菜单库默认值、拍摄锁定和 XMP 写入由实际编译的固件代码在 Unicorn 中执行（文件系统和 XMP 合成器为桩）。原厂设置代码测试还在模拟 NOR 上执行第 16 节读写、模式 4 恢复及默认值，覆盖中断写入和原厂前缀保留。QEMU 模拟器另行运行原厂菜单与存储卡。实机菜单显示、存储卡读取、色彩、速度及 Lightroom/NX Studio 的显示均未验证。

## English

Photo LUT applies a 3D LUT to the photo the camera has already rendered: the JPEG's 8-bit Y'CbCr after the active Picture Control, in the selected colour space. Live view and JPEG use the same look; **NEF raw data is never changed**.

### Library file

A library is one portable file, `Z8LUTLIB.BIN`, managed independently of firmware, holding up to eight looks:

- its first part is the runtime grid the camera applies (17/33/65 points, at most 2 MiB in total);
- the rest keeps each look's exact original `.cube` bytes for the host.

Each look has a **transform identity**: SHA-256 over the grid the camera applies and its semantics. Renaming and reordering keep it; different content changes it. Names are 1–63 bytes of UTF-8 (Chinese works) without control characters or outer spaces.

```sh
python photo_lut.py library create looks.bin                       # empty library
python photo_lut.py library add looks.bin film.cube --name "Film A"  # optional --grid 17|33|65, --position N
python photo_lut.py library list looks.bin                         # order, names, identities, sources
python photo_lut.py library rename looks.bin "Film A" "Film B"
python photo_lut.py library move looks.bin "Film B" 1
python photo_lut.py library remove looks.bin 2                     # down to an empty library
python photo_lut.py library export looks.bin 1 original.cube --source  # byte-identical to the import
python photo_lut.py library export looks.bin 1 runtime.cube --runtime  # the resampled grid the camera applies
python photo_lut.py library verify looks.bin                       # re-derive from the sources (slow)
```

Every change writes and reads back a complete temporary file before replacing the library; bad input leaves the previous file unchanged.

**Menu labels.** The camera menu shows looks as "N. Name", so clipped long names stay distinguishable. Nikon's menu fonts hold only the characters the camera's own menus use, and a label uses one font: the firmware uses the UI language's menu font, and if that lacks a character, the first of the English, Simplified Chinese, Traditional Chinese, Japanese and Korean menu fonts that has the whole label. `library add`, `list` and `card` point out characters that all of these lack (for example 冬 and 季); they may show as boxes. The name itself is kept in full in the library, exports and photo metadata; rename the look if you want a readable menu label.

**Input basis.** LUTs made for log, HLG/PQ or another vendor's base do not fit the camera's rendered SDR photo. A file name, TITLE or comment mentioning V-Log, N-Log, S-Log, LogC, F-Log, HLG, PQ, BT.2100/2020 and similar, a `#LUMIXPHOTOSTYLE` comment (Panasonic; missing means V-Log), a `.vlt` file, or a DOMAIN outside 0..1 is refused. If the LUT really was made for camera-rendered SDR, add `--accept-input-basis`; the reason is recorded in the library. Panasonic, Nikon NP3 and other cameras' LUTs are not interchangeable with this feature.

### Firmware and card looks

1. **Build the firmware once**: `python patch.py Z_8_0311.bin --features photo-lut --version NN.NN --tag p0 --output DIR` (other feature groups may be selected too). The firmware carries no looks, so changing looks never needs a rebuild.
2. **Copy to a card**: `python photo_lut.py library card looks.bin /card/root` writes `Z8LUTLIB.BIN` to the card root.
3. **Load in the camera**: Photo shooting menu > Photo LUT > **Load LUTs from card**.

Shooting banks A–D save full transform identities: renaming or reordering preserves the choice; a removed or changed transform remains unavailable. Individual bank reset sets that bank to Off and retains the current library source. Whole-settings reset sets every bank to Off and unloads the library.

Card load rules:

| Situation | Result |
|---|---|
| One card has a valid file | Loaded (slot 1 = CFexpress `A:`, slot 2 = SD `B:`) |
| Both files have an identical runtime part (the part the camera reads) | Loaded; differences only in the host source part are ignored |
| The runtime parts differ | Refused: "Slots 1 and 2 differ" |
| Either file is invalid, truncated, oversized or unreadable | Refused naming the slot, even if the other card is valid |
| No file | Refused: "No Z8LUTLIB.BIN on either card" |

After Load LUTs from card the page does not close; it is rebuilt at once, that row showing the result (for example "Card LUTs: 8 loaded (slot 1)") with the new looks below it. A refusal keeps the current looks and every bank's selection.

**Import once; normal power cycles restore automatically.** Internal settings retain the active library source and each A–D bank's full transform identity; the grids stay in `Z8LUTLIB.BIN` on the card. Startup restores the library and choices without a menu visit, Load or reselection. Off disables the effect and retains the imported library source: even with all four banks Off, startup still reloads that library. The native bank Copy command copies its LUT choice too; later source-bank edits do not change the copy.

The menu lists the active library only. Explicitly importing a card library replaces the loaded one, and restart restores that same source. If a bank's chosen transform is absent, its identity stays saved and the look is unavailable; a same-name or same-position look never replaces it. Missing, delayed, corrupt or conflicting cards retain choices and trigger automatic retries. The native settings task also retries startup when the restore worker temporarily lacks memory or task resources. Retries stop once the active bank is resolved and the imported library has loaded; an unused bank's missing look causes no repeated card reads. Capture is blocked until the selected look restores or Off is chosen. A published library remains in RAM when its card is removed.

On the first upgrade, legacy non-Off choices need a new selection because their full identities cannot be reconstructed; tagged Off choices stay Off. An all-zero legacy configuration is treated as never set and takes the firmware default, Off. Stock firmware can still read its original settings; saving with stock firmware clears this feature's extension record.

Each capture latches its look when its output is reserved: it copies the grid, identity and name, so changing the selection or loading a library never affects a capture already under way.

### Photo metadata

A JPEG with a look applied carries one more description in its existing XMP packet, in the `photolut` namespace (`https://github.com/ChrAlpha/NikonFirmwarePatch/ns/photo-lut/1.0/`): `LookName`, `TransformSHA256` (the transform identity shown by `library list`), `Applied`, `Rendering=baked-into-jpeg-pixels`, `InputBasis`, `Strength`/`StrengthScale`, `Grid`, `Algorithm` and `Schema`. It uses only the packet's space padding, so the XMP packet, APP1 segment and Exif offsets keep their lengths; Nikon MakerNotes, Picture Control, colour space, ICC, user comment and copyright are untouched.

- It is written only when the JPEG's main image was successfully encoded from the transformed pixels; a failed encode or Off writes nothing.
- A no-op (identity) look is recorded as `Applied=False`, `NoOp=True`.
- **NEF files get nothing**; their image and metadata stay original.
- This records provenance; it is not an instruction for Lightroom or NX Studio, which will not re-apply the look from it.

Read it with `exiftool -XMP-photolut:all DSC_0001.JPG` (checked with ExifTool 13.59).

### Developer checks

Python 3.10+, the dependencies in `requirements.txt` and `unicorn==2.1.4` are required. The firmware code is one translation unit, `z8photolut/src/unit.c` (including `hooks.S`), compiled with clang-19 by `tools/compile_units.py --units photo-lut` and linked by the in-house linker; its reviewed bytes are recorded in `resources/compiled.json` and `resources/photo-lut.bin`, and the tests execute those bytes. The tests take the original main payload from the official firmware:

```sh
python -m pip install -r requirements.txt unicorn==2.1.4
python -c "from z8prores.package import Package; open('z8-main-payload.bi','wb').write(Package(open('Z_8_0311.bin','rb').read()).eg[:-2])"
Z8_PHOTO_LUT_ROM=z8-main-payload.bi python -B -m unittest discover -s tests -p 'test_photo_lut*.py'
```

Without `Z8_PHOTO_LUT_ROM` only host tests run; `Z8_PHOTO_LUT_SAMPLE_JPEG` may name an original JPEG for the XMP helper tests.

### Evidence

Original-ROM settings tests additionally execute section-16 read/write, mode-4 restore and defaults over modelled NOR, including interrupted writes and stock-prefix preservation. Host tests cover the library format, editing, exports, malformed input and parity with the camera's validator; automatic restore, pending-change merges, bank defaults, capture latching and XMP insertion run the actual compiled firmware code in Unicorn with stubbed file system and XMP composer. The QEMU emulator separately runs the original menus and card. Physical menu drawing, card reads, colour, speed and Lightroom/NX Studio display are not verified.
