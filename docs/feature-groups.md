# 五组安装选项 / Five feature groups

| 构建选项 | 安装内容 | 相机中的主要入口 |
|---|---|---|
| `framing` | 3:2 开放门、N-RAW 四档画质、反挤压显示 | 画面尺寸／帧频、N-RAW 画质、反挤压 |
| `video-quality` | ProRes 四档与 HQ 星标、H.265 五档码率及对应星标 | ProRes 画质、H.265 画质 |
| `log-colour` | N-Log、S-Log3、Log3G10 选择及相关显示／标签 | Log 设定 |
| `photo-tools` | C15/C30 RAW 预拍、像素偏移合成 | 普通画质、RAW 记录、像素变化合成 |
| `photo-lut` | 存储卡风格库 `Z8LUTLIB.BIN` 的 3D LUT 风格，作用于 JPEG 与照片模式实时取景；NEF 原始数据不变 | 照片拍摄菜单 > 照片 LUT |

每组可以独立从原厂 C 3.11 构建，也可一次选择多组，共 31 种非空组合。N-RAW 画质与开放门共同使用码率及剩余时间逻辑，保留在同一组。反挤压可在菜单关闭；RAW 预拍与像素偏移使用各自入口。

**`photo-lut` 尚未发布，也未经实机验证。** v0.4.0-rc.2 只含前四组（15 种组合）。风格库与固件分开管理，增删风格不必重新构建固件，见[照片 LUT](photo-lut.md)。

构建选项决定安装哪些功能，机内菜单决定每项功能如何使用。先选择所需组，再从原厂包一次构建完整固件；不要向已经修改的固件继续叠加。

旧版选项 `prores / precapture / pixelshift / opengate-nraw` 仅用于对应的旧版源码。当前 CLI 会明确拒绝旧名称。复现旧包时使用该版本的源码或完整工具包。

功能指南：[开放门](opengate-nraw.md)、[反挤压](anamorphic.md)、[ProRes](prores.md)、[H.265](h265.md)、[Log](log-colour.md)、[RAW 预拍](raw-precapture.md)、[像素偏移](pixelshift.md)、[照片 LUT](photo-lut.md)。

### 地址区域与共享单元

`photo-lut` 占用 MAIN 文本段末尾的 `0x40B50000..0x40C00000`，其他组不使用该区域：

| 地址 | 内容 |
|---|---|
| `0x40B50000..0x40B68000` | 代码：`z8photolut/src/unit.c` 单一编译单元，钩子位于开头 |
| `0x40B68000` | 照片 LUT 选项页 |
| `0x40B69000` | 三个运行时改写的菜单文字块，各 `0x800` 字节 |
| `0x40B6B000` | 私有状态页，`0x1000` 字节，载入后位于 RAM `0xA1016B000` |
| `0x40B6C000..0x40B6E000` | 显影页：两条参数表通道、通道记录（`+0x80`）和参数表副本（`+0x200`／`+0xB00`） |
| `0x40B6E000..0x40C00000` | 未使用 |

多个组共用的位置由单一共享单元写入，各组只提供内容：

- **字符串（`Strings`）。** 各组的固定字符串使用共享编号 3137–3224；编号 3225 用于结束共享文字，不会显示。`photo-lut` 另有私有编号 3226–3237，其 UTF-16 文字位于自己的三个文字块，菜单状态或已载入的风格库变化后由固件改写。只有选择 `photo-lut` 时，36 种语言的偏移表才增加这些编号。
- **照片拍摄菜单（`PhotoMenu`）。** 只在选择 `photo-lut` 时启用。第 2 行（项目 `0x2CAA`）打开状态 `0xF8` 的选项页；隐藏列表只隐藏未选组的自定义行（`0x2CAA` 属于 `photo-lut`，`0x2C13` 属于 `photo-tools`）。它接管像素偏移原来对隐藏数和刷新回调的修改；`shared_photo_refresh` 先调用基础刷新（选择 `photo-tools` 时为像素偏移的刷新，否则为原厂 `0x405D9808`），再调用照片 LUT 行刷新。未选 `photo-lut` 时，像素偏移保留自己的修改，共享代码的视频部分与 v0.4.0-rc.2 相同。
- **色彩链（`z8colour`）。** 代码位于 `0x40981000`，配置位于 `0x40981F00..0x40981F60`。它独占录像色彩链的五个位置（M1、RGB 曲线、NLG、M2、Cpxs），选择 `log-colour` 或 `photo-lut` 时安装。配置包含 `log-colour` 的 Log 类型和十张表，以及 `photo-lut` 的参数表提供块。提供块有两条 `0x38` 字节的通道：通道 0 用于照片显影，通道 1 用于照片模式实时取景；每条通道只服务其记录的那次构建，其余构建依次使用 Log 录像表或原厂表。未选 `photo-lut` 时只安装基础部分（通道 0 和 Log 类型，616 字节）。
- **文本段载入边界。** 启动时复制的文本段终点取所选组下限和所有修改末端中的较大者，按页取整，不超过 `0x40C00000`。`photo-lut` 的下限为 `0x40B6E000`，因此全零的私有状态页和显影页也会载入 RAM。

### 从 v0.4.0-rc.2 迁移

- 原有四组名称和含义不变；`patch.py --interactive` 增加第 5 项，第 6 项为全选。
- 不含 `log-colour` 的 7 种组合，补丁内容与 v0.4.0-rc.2 逐字节相同。
- 含 `log-colour` 的 8 种组合改由 `z8colour` 处理色彩链，固件字节因此与 v0.4.0-rc.2 不同。Log 录像行为不变：在 Unicorn 中逐一比较 v0.4.0-rc.2 的 Log 钩子与 `z8colour` 钩子，覆盖各 Log 类型值和原厂选择，95 种情况结果一致。v0.4.0-rc.2 发布的四组全选 SHA-256 只适用于该版工具包。

## English

The five installation choices are `framing`, `video-quality`, `log-colour`, `photo-tools` and `photo-lut`. Each builds independently from the original C 3.11 image; multiple groups are composed in one build. There are 31 nonempty combinations. Open gate stays paired with N-RAW quality, while desqueeze can be disabled in the camera. Pre-capture and pixel-shift keep separate controls. `photo-lut` adds Photo LUT to the photo shooting menu: 3D LUT looks from a card library, `Z8LUTLIB.BIN`, applied to JPEGs and photo-mode live view, with NEF raw data unchanged. See [Photo LUT](photo-lut.md).

**`photo-lut` is unreleased and has not been verified on a camera.** v0.4.0-rc.2 ships the first four groups (15 combinations). Looks are managed separately from firmware, so changing them needs no rebuild.

Build-time choices install features; camera settings select their behavior. Legacy option names belong to older source/toolkit versions and are rejected by the current CLI.

### Address regions and shared owners

`photo-lut` owns `0x40B50000..0x40C00000` at the end of the MAIN text section, which no other group uses: code `0x40B50000..0x40B68000` (the single translation unit `z8photolut/src/unit.c`, hooks first), the option page at `0x40B68000`, three runtime-rewritten menu text blocks of `0x800` bytes at `0x40B69000`, the `0x1000`-byte private state page at `0x40B6B000` (RAM `0xA1016B000` once loaded) and the develop pages `0x40B6C000..0x40B6E000` (two table lanes, lane records at `+0x80`, table copies at `+0x200`/`+0xB00`). `0x40B6E000..0x40C00000` is unused.

Locations used by several groups are written by one shared owner; each group supplies content only.

- **Strings.** Fixed group strings use shared IDs 3137–3224; ID 3225 ends the shared text and is never displayed. `photo-lut` adds private IDs 3226–3237, whose UTF-16 text lives in its own three text blocks and is rewritten by the firmware when the menu status or the loaded library changes. The 36 language offset tables carry these IDs only when `photo-lut` is selected.
- **Photo shooting menu (`PhotoMenu`).** Active only with `photo-lut`. Row 2 (item `0x2CAA`) opens the option page in state `0xF8`, and the hide list hides only the custom rows of unselected groups (`0x2CAA` for `photo-lut`, `0x2C13` for `photo-tools`). It takes over pixel-shift's hide-count and refresh-callback edits; `shared_photo_refresh` calls the base refresh (pixel-shift's with `photo-tools`, otherwise stock `0x405D9808`) and then the Photo LUT row refresh. Without `photo-lut`, pixel-shift keeps its own edits and the video part of the shared code matches v0.4.0-rc.2.
- **Colour chain (`z8colour`).** Code at `0x40981000`, configuration at `0x40981F00..0x40981F60`. It owns the five recording colour-chain sites (M1, RGB curves, NLG, M2, Cpxs) and is installed with `log-colour` or `photo-lut`. The configuration holds the `log-colour` Log type and its ten tables, and the `photo-lut` table provider block. That block has two `0x38`-byte lanes: lane 0 for the still develop and lane 1 for photo-mode live view. Each lane serves only the build it records; every other build takes the movie Log tables or the stock tables. Without `photo-lut` only the 616-byte base part (lane 0 and the Log types) is installed.
- **Text loader boundary.** The text copied at startup ends at the larger of the selected groups' floors and the end of every edit, rounded up to a page and at most `0x40C00000`. The `photo-lut` floor is `0x40B6E000`, so its all-zero state and develop pages are loaded into RAM.

### Migrating from v0.4.0-rc.2

- The four existing group names keep their meaning; `patch.py --interactive` adds choice 5, and 6 selects all.
- The 7 combinations without `log-colour` produce byte-identical patches to v0.4.0-rc.2.
- The 8 combinations with `log-colour` route the colour chain through `z8colour`, so their firmware bytes differ from v0.4.0-rc.2. Log recording behaves the same: Unicorn runs of the v0.4.0-rc.2 Log hooks and the `z8colour` hooks agree in all 95 cases covering the Log type values and native selectors. The published v0.4.0-rc.2 full-selection SHA-256 applies to that toolkit only.
