# 照片 LUT / Photo LUT

[简体中文](#简体中文) · [English](#english)

**开发版功能，尚未通过实机验收。** Development feature without physical-camera acceptance.

## 简体中文

照片 LUT 用 3D LUT 给 JPEG 加上风格。每个风格声明其 LUT 制作时针对的输入基准：

- **`nikon-sdr`**（默认）：LUT 作用于相机已渲染的照片，即 JPEG 在当前优化校准（Picture Control）之后、所选色彩空间中的 8 位 Y'CbCr 值。实时取景和 JPEG 使用同一风格。
- **Log 基准**（N-Log、S-Log3、Log3G10、F-Log2、LogC3、LogC4 或 V-Log，各带其色域）：照片和照片模式的实时取景由相机自身的 Log 处理链显影，所用参数表在电脑上针对该风格拟合，再由一个小的残差网格完成 JPEG 和实时取景画面。见 [Log 风格](#log-风格)。

**NEF 原始数据不变。**

### 风格库文件

风格库是一个可携带文件 `Z8LUTLIB.BIN`，与固件分开管理，最多 8 个风格：

- 前段是相机读取的运行数据（格式 `Z8LUT004`，总计不超过 2 MiB）：每个风格的输入基准、渲染方式（`cpu-3d` 或 `isp+residual`）、Log 风格的显影参数表（相机原生格式）及其网格（17/33/65 点）；
- 后段保存每个风格原始 `.cube` 的完整字节，Log 风格另含拟合设置与报告，只供电脑端使用。

每个风格有一个**变换标识**：对相机实际应用的全部内容（语义、基准、参数表和网格）计算的 SHA-256。改名、调整顺序都不改变它；内容不同则标识不同。旧格式 `Z8LUT003` 的库按 `nikon-sdr` 风格读入，标识不变，下一次修改或 `library card` 时保存为 `Z8LUT004`。风格名为 1–63 字节 UTF-8（可用中文），不能含控制字符或首尾空格。

```sh
python photo_lut.py library create looks.bin                        # 空库
python photo_lut.py library add looks.bin film.cube --name "胶片 A"   # 可选 --grid 17|33|65、--position N
python photo_lut.py library add looks.bin provia.cube --basis f-log2/f-gamut-c   # Log 风格（见下文）
python photo_lut.py library list looks.bin                          # 顺序、名称、标识、来源
python photo_lut.py library rename looks.bin "胶片 A" "胶片 B"
python photo_lut.py library move looks.bin "胶片 B" 1
python photo_lut.py library remove looks.bin 2                      # 可删到空库
python photo_lut.py library export looks.bin 1 原始.cube --source    # 与导入时逐字节相同
python photo_lut.py library export looks.bin 1 运行.cube --runtime   # nikon-sdr：相机实际应用的重采样网格
python photo_lut.py library verify looks.bin                        # 从原始 .cube 重新推导或重新测量并核对（较慢）
```

每次修改都先完整写入并读回临时文件，再替换原文件；输入错误时原文件保持不变。

**菜单显示。** 相机菜单以“序号. 名称”显示风格，长名称被截断时可按序号区分。尼康菜单字体只含相机自身菜单用到的字，一个标签只能用一种字体；固件先用当前界面语言的菜单字体，缺字时依次改用英文、简体中文、繁体中文、日文、韩文菜单字体中能完整显示该标签的一种。`library add`、`list`、`card` 会提示这些字体都缺少的字（例如“冬”“季”），这类字可能显示为方框；名称本身仍完整保存在库、导出文件和照片元数据中，需要时可改用能显示的名称。

**输入基准。** `--basis` 声明 LUT 期望的输入：

| `--basis` | Log 编码 / 色域 |
|---|---|
| `nikon-sdr`（默认） | 相机渲染后的 SDR JPEG 值 |
| `n-log/bt2020` | N-Log / ITU-R BT.2020 |
| `s-log3/s-gamut3.cine` | S-Log3 / S-Gamut3.Cine |
| `log3g10/redwidegamutrgb` | Log3G10 / REDWideGamutRGB |
| `f-log2/f-gamut-c` | F-Log2 / F-Gamut C |
| `arri-logc3/awg3` | ARRI LogC3 / ARRI Wide Gamut 3 |
| `arri-logc4/awg4` | ARRI LogC4 / ARRI Wide Gamut 4 |
| `v-log/v-gamut` | V-Log / V-Gamut |

也可用简称（`--basis s-log3`）。未指定 `--basis` 时，看起来为其他输入制作的 cube 会被拒绝：文件名、TITLE 或注释中出现 Log 编码或色域名（V-Log、N-Log、S-Log3、LogC、F-Log2、RWG 等）、HLG、PQ、BT.2100/2020，或带 `#LUMIXPHOTOSTYLE`（松下注释，缺省即 V-Log），或 DOMAIN 超出 0..1。提示会说明发现了什么：请声明基准；若 LUT 确为相机渲染 SDR 制作，加 `--accept-input-basis`（原因记录在库中）。声明 Log 基准时，cube 自身文字不得与之矛盾：写明另一种 Log 编码、HLG/PQ、S-Log2/F-Log 或松下照片风格的 cube 会被拒绝；Rec.709 等输出名称不影响。不支持 `.vlt` 文件，也不支持尼康 NP3 或其他相机的照片风格。

### Log 风格

Log LUT 需要场景参考的 Log 输入，渲染后的 8 位 SDR JPEG 无法提供。对 Log 基准的风格，相机改为用自身的 N-Log 处理链（M1 矩阵、RGB 色调曲线、NLG、M2 矩阵和 32 节点 Cpxs 色相环）显影照片，所用的是**在电脑上针对该风格拟合的参数表**，再对 8 位 JPEG 输入应用一个接近恒等的**残差网格**，修正这些环节表达不了的部分。

```sh
python -m pip install -r requirements-photo-lut-log.txt              # numpy、scipy、colour-science：仅编译需要
python photo_lut.py library add looks.bin provia.cube --basis f-log2/f-gamut-c
python photo_lut.py library add looks.bin teal.cube --basis arri-logc3 --grid 65 --grey 400 --smoothness 3
```

编译约需一分钟，并输出**拟合报告**，报告也保存在库中、由 `library list` 显示：

- **dE00 p50/p95/p99，仅 ISP**：只用拟合参数表、不加残差时的 JPEG；
- **dE00 p50/p95/p99，ISP + 残差**：最终 JPEG 路径，经过 8 位 Y'CbCr 和相机的整数运算；
- **残差斜率 p50/p99**：照片实际落入区域中，残差输出变化与 8 位输入变化之比（1 = 恒等；越大越可能出现色带）；
- 可达网格节点比例、Cpxs 节点最小行列式、M1/M2 条件数。

误差在 15,000 个留出的场景颜色上测量（灰点附近 −7…+3.5 EV、自然饱和度、BT.2020 色相），参照为对场景精确 Log 编码后应用该 LUT 的结果。编译器在相机的 229 节点曲线网格上拟合有界单调曲线，保持色相环可逆（行列式下限、节点间平滑 `--smoothness`，默认 3）并控制矩阵条件数；**先把各表量化到固件整数格式**，再在每个网格节点对量化后的模型求逆，得到残差。残差若不能改善 JPEG，就改为恒等网格，相机会跳过它。

两个真实 cube 的实测结果（33 点残差；括号内为 65 点）：

| Cube（基准） | 仅 ISP p50/p95/p99 | ISP + 残差 p50/p95/p99 | 残差斜率 p50/p99 |
|---|---|---|---|
| 富士 F-Log2 C → Provia（`f-log2/f-gamut-c`） | 1.09 / 3.08 / 4.88 | 0.39 / 1.55 / 3.79（0.34 / 1.42 / 3.25） | 1.14 / 1.63 |
| ARRI LogC3 → Film Teal & Orange（`arri-logc3/awg3`） | 1.29 / 5.04 / 8.52 | 0.34 / 2.00 / 4.74（0.31 / 1.87 / 4.63） | 1.18 / 2.65 |

仅 8 位 Y'CbCr 交接本身就带来约 0.2 dE00 的中位误差；其余尾部来自被拟合环节折叠或截断的颜色，任何残差都无法再把它们分开。

**相机如何使用参数表。** M1 基础矩阵由相机与机身自身的矩阵和增益合成，方式与 Log 视频模式完全相同；编译器由本项目 Log 功能已校准的 S-Log3 基础矩阵推导（M1 输入 → S-Gamut3.Cine，再 S-Gamut3.Cine → 风格色域），因此每台机身使用自己的校准。18% 灰放在原厂 N-Log 曲线编码 0.18 的位置（`--grey 400` 曲线单位，也是 S-Log3 校准点）；数值越大画面越亮。三条 RGB 曲线相同。

**哪些改变，哪些不变。**

- 拍摄时的照片显影使用这些参数表。JPEG 再应用残差；**NEF 内嵌预览带有拟合风格但不含残差**（二者来自同一次显影），NEF 原始数据保持原样。
- **照片模式的实时取景**（EVF、液晶屏和 HDMI 输出）也用同一套参数表：相机每次更新取景参数时（约每帧一次），这两次参数构建同样走原厂 N-Log 处理链并使用该风格的参数表。经 CPU 预览路径显示的画面（即 `nikon-sdr` 风格在取景中生效的同一路径，同一 65×65 色度缓存，开销相同）另外应用同一残差网格，使取景接近 JPEG；只有连续两次取景参数构建都已使用该风格的参数表后才开始应用，避免残差落到仍按原参数显影的画面上。残差为恒等时不经过 CPU。不经过该路径的输出只带参数表部分，精度见上表“仅 ISP”一栏。
- 录像模式、回放、菜单以及 `nikon-sdr` 风格或“关闭”时，取景保持原厂行为；录像模式下 Log 功能组的 N-Log、S-Log3、Log3G10 监看不受影响。切换菜单库、风格或关闭照片 LUT 后，下一次取景参数构建即恢复原厂参数。
- 显影选择开关（N-Log 色调）只在这几次构建期间打开，结束后立即恢复，测光、直方图、斑马纹和元数据看到的始终是原厂 SDR 设定。照片显影与实时取景各用一份参数表副本和各自的通道，可在不同 CPU 上同时进行；载入新库不会释放正在使用的参数表。
- 与所有风格一样，所选 Log 风格可用时才允许拍摄。若照片显影没有使用该次拍摄自己的参数表（例如正在发布新库），JPEG 保持原样，元数据也不声明风格。
- Log 风格不能用 `--runtime` 导出：其网格是相机参数表之后的残差，不是独立变换；`--source` 可导出原始 cube。

**尚未实机验证。** QEMU 模拟器运行原厂固件，但其图像管线只做功能模拟、不应用颜色参数表，因此只能证明显影使用了该风格的参数表、JPEG 精确应用了残差，不能证明颜色。仍需实机确认：照片 ISO 下 18% 灰的曝光位置、照片路径和实时取景最终的 Y'CbCr 编码（残差按 JPEG 的 8 位全范围 BT.601 编码制作，与 `nikon-sdr` 预览路径的前提相同）、取景参数从构建到画面的延迟、各机身校准是否与视频相同地合成、平滑渐变中的色带，以及分通道曲线。

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

每次拍摄在分配输出时锁定所选风格：复制其网格、标识、名称和基准，之后改选或载入新库都不影响已开始的拍摄。

### 照片元数据

应用了风格的 JPEG 在其原有 XMP 包中多一个 `photolut` 命名空间（`https://github.com/ChrAlpha/NikonFirmwarePatch/ns/photo-lut/1.0/`，`Schema=1.1`）的描述：`LookName`、`TransformSHA256`（即变换标识，可在 `library list` 中对照）、`Applied`、`Rendering`（nikon-sdr 风格为 `baked-into-jpeg-pixels`，Log 风格为 `isp-fitted+residual`）、`InputBasis`（声明的基准，如 `nikon-sdr` 或 `f-log2/f-gamut-c`）、`FitSHA256`（Log 风格：拟合参数表的标识）、`Strength`/`StrengthScale`、`Grid`、`Algorithm`、`Schema`。只占用原包的空白填充，XMP 包、APP1 段和 Exif 偏移的长度都不变；尼康 MakerNote、优化校准、色彩空间、ICC、用户注释和版权信息不动。

- 只在该 JPEG 的主图确实带风格成功编码时写入（像素已变换；Log 风格则已用其参数表显影）；编码失败、Log 风格未按其参数表显影或风格为“关闭”时不写。
- 无变化（恒等）的 nikon-sdr 风格写入 `Applied=False`、`NoOp=True`。
- **NEF 不写**，其图像与元数据保持原样。
- 这是来源记录，不是给 Lightroom 或 NX Studio 的重新套用指令；这些软件不会因此自动套用风格。

查看：`exiftool -XMP-photolut:all DSC_0001.JPG`（ExifTool 13.59 以该命令读出全部字段）。

### 开发者验证

需要 Python 3.10+、`requirements.txt` 中的依赖和 `unicorn==2.1.4`；编译器测试另需 `requirements-photo-lut-log.txt`，缺少时自动跳过。固件代码是 `z8photolut/src/unit.c` 单一编译单元（含 `hooks.S`），由 `tools/compile_units.py --units photo-lut` 用 clang-19 编译并由仓库自带的链接器链接，审核后的字节记录在 `resources/compiled.json` 与 `resources/photo-lut.bin`；测试执行的就是这份字节。测试用的原厂主程序由官方固件取出：

```sh
python -m pip install -r requirements.txt unicorn==2.1.4
python -c "from z8prores.package import Package; open('z8-main-payload.bi','wb').write(Package(open('Z_8_0311.bin','rb').read()).eg[:-2])"
Z8_PHOTO_LUT_ROM=z8-main-payload.bi python -B -m unittest discover -s tests -p 'test_photo_lut*.py'
```

未设置 `Z8_PHOTO_LUT_ROM` 时只运行宿主测试；`Z8_PHOTO_LUT_SAMPLE_JPEG` 可额外指定一张原厂 JPEG 供 XMP 辅助测试使用；`Z8_PHOTO_LUT_SLOW=1` 在有真实 cube 时以正式设置编译一次。

### 验证范围

宿主测试覆盖库格式、编辑、导出、错误输入、输入基准规则、编译器（固件整数格式、先量化后求逆、相机运算核）和与相机校验代码的一致性；自动恢复、延迟读取时的选择合并、菜单库默认值、拍摄锁定、照片显影与实时取景构建包装、两个参数表通道（含同时显影）、取景模式判断、选择开关恢复、共享颜色钩子、残差 JPEG 与取景路径和 XMP 写入由实际编译的固件代码在 Unicorn 中执行（文件系统和 XMP 合成器为桩）。原厂设置代码测试还在模拟 NOR 上执行第 16 节读写、模式 4 恢复及默认值，覆盖中断写入和原厂前缀保留。QEMU 模拟器另行运行原厂菜单、存储卡，以及带显影和取景构建追踪的 Log 风格拍摄与录像模式。实机菜单显示、存储卡读取、色彩、速度及 Lightroom/NX Studio 的显示均未验证。

## English

Photo LUT gives JPEGs a look from a 3D LUT. Each look declares the input its LUT was made for:

- **`nikon-sdr`** (default): the LUT acts on the photo the camera has already rendered, the JPEG's 8-bit Y'CbCr after the active Picture Control, in the selected colour space. Live view and JPEG use the same look.
- **A Log basis** (N-Log, S-Log3, Log3G10, F-Log2, LogC3, LogC4 or V-Log, each with its gamut): the still and photo-mode live view are developed through the camera's own Log chain with tables fitted to the look on the computer, then a small residual grid finishes the JPEG and the live picture. See [Log looks](#log-looks).

**NEF raw data is never changed.**

### Library file

A library is one portable file, `Z8LUTLIB.BIN`, managed independently of firmware, holding up to eight looks:

- its first part is the runtime bundle the camera reads (format `Z8LUT004`, at most 2 MiB in total): per look its input basis, rendering (`cpu-3d` or `isp+residual`), for a Log look its still-develop tables in the camera's own formats, and its grid (17/33/65 points);
- the rest keeps each look's exact original `.cube` bytes and, for a Log look, its fit settings and report, for the host only.

Each look has a **transform identity**: SHA-256 over everything the camera applies (semantics, basis, tables and grid). Renaming and reordering keep it; different content changes it. Libraries in the earlier `Z8LUT003` format are read as `nikon-sdr` looks with unchanged identities and saved as `Z8LUT004` by the next change or `library card`. Names are 1–63 bytes of UTF-8 (Chinese works) without control characters or outer spaces.

```sh
python photo_lut.py library create looks.bin                       # empty library
python photo_lut.py library add looks.bin film.cube --name "Film A"  # optional --grid 17|33|65, --position N
python photo_lut.py library add looks.bin provia.cube --basis f-log2/f-gamut-c   # a Log look (see below)
python photo_lut.py library list looks.bin                         # order, names, identities, sources
python photo_lut.py library rename looks.bin "Film A" "Film B"
python photo_lut.py library move looks.bin "Film B" 1
python photo_lut.py library remove looks.bin 2                     # down to an empty library
python photo_lut.py library export looks.bin 1 original.cube --source  # byte-identical to the import
python photo_lut.py library export looks.bin 1 runtime.cube --runtime  # nikon-sdr: the resampled grid the camera applies
python photo_lut.py library verify looks.bin                       # re-derive or re-measure from the sources (slow)
```

Every change writes and reads back a complete temporary file before replacing the library; bad input leaves the previous file unchanged.

**Menu labels.** The camera menu shows looks as "N. Name", so clipped long names stay distinguishable. Nikon's menu fonts hold only the characters the camera's own menus use, and a label uses one font: the firmware uses the UI language's menu font, and if that lacks a character, the first of the English, Simplified Chinese, Traditional Chinese, Japanese and Korean menu fonts that has the whole label. `library add`, `list` and `card` point out characters that all of these lack (for example 冬 and 季); they may show as boxes. The name itself is kept in full in the library, exports and photo metadata; rename the look if you want a readable menu label.

**Input basis.** `--basis` declares what the LUT expects:

| `--basis` | Log encoding / gamut |
|---|---|
| `nikon-sdr` (default) | camera-rendered SDR JPEG values |
| `n-log/bt2020` | N-Log / ITU-R BT.2020 |
| `s-log3/s-gamut3.cine` | S-Log3 / S-Gamut3.Cine |
| `log3g10/redwidegamutrgb` | Log3G10 / REDWideGamutRGB |
| `f-log2/f-gamut-c` | F-Log2 / F-Gamut C |
| `arri-logc3/awg3` | ARRI LogC3 / ARRI Wide Gamut 3 |
| `arri-logc4/awg4` | ARRI LogC4 / ARRI Wide Gamut 4 |
| `v-log/v-gamut` | V-Log / V-Gamut |

Short names work too (`--basis s-log3`). Without `--basis`, a cube that looks made for something else is refused: a file name, TITLE or comment naming a Log encoding or gamut (V-Log, N-Log, S-Log3, LogC, F-Log2, RWG, …), HLG, PQ, BT.2100/2020, a `#LUMIXPHOTOSTYLE` comment (Panasonic; missing means V-Log), or a DOMAIN outside 0..1. The message names what it found; declare the basis, or, if the LUT really was made for camera-rendered SDR, add `--accept-input-basis` (the reason is recorded in the library). With a Log basis, the cube's own text must not contradict it: a cube naming another Log encoding, HLG/PQ, S-Log2/F-Log or a Panasonic Photo Style is refused; output names such as Rec.709 are fine. `.vlt` files and Nikon NP3 or other cameras' picture styles are not supported.

### Log looks

A Log LUT expects scene-referred Log input, which the rendered 8-bit SDR JPEG cannot provide. For a look with a Log basis the camera instead develops the still through its native N-Log chain (M1 matrix, RGB tone curves, NLG, M2 matrix and the 32-node Cpxs hue ring) with **tables fitted to the look on the computer**, then applies a near-identity **residual grid** to the 8-bit JPEG input to correct what those stages cannot express.

```sh
python -m pip install -r requirements-photo-lut-log.txt              # numpy, scipy, colour-science: compiling only
python photo_lut.py library add looks.bin provia.cube --basis f-log2/f-gamut-c
python photo_lut.py library add looks.bin teal.cube --basis arri-logc3 --grid 65 --grey 400 --smoothness 3
```

Compiling takes about a minute and prints a **fit report**, also kept in the library and shown by `library list`:

- **dE00 p50/p95/p99, ISP only**: the fitted tables alone, as the JPEG would be without the residual;
- **dE00 p50/p95/p99, ISP + residual**: the final JPEG path, through 8-bit Y'CbCr and the camera's integer kernel;
- **residual slope p50/p99**: residual output steps per 8-bit input step where photos land (1 = identity; higher values risk banding);
- reachable grid nodes, the smallest Cpxs node determinant and the M1/M2 condition numbers.

Errors are measured on 15,000 held-out scene colours (−7…+3.5 EV around grey, natural saturation, BT.2020 hues) against the LUT applied to the exact Log encoding of the scene. The compiler fits on the camera's 229-node curve grid with a bounded monotone curve, keeps the hue ring invertible (determinant floor, inter-node smoothness `--smoothness`, default 3) and the matrices well conditioned, **quantises every table to its firmware integer format first**, then builds the residual by inverting the quantised model at each grid node. If the residual would not improve the JPEG it becomes the identity grid, which the camera skips.

Measured on two real cubes (33-point residual; with 65 points in parentheses):

| Cube (basis) | ISP only p50/p95/p99 | ISP + residual p50/p95/p99 | Residual slope p50/p99 |
|---|---|---|---|
| Fujifilm F-Log2 C → Provia (`f-log2/f-gamut-c`) | 1.09 / 3.08 / 4.88 | 0.39 / 1.55 / 3.79 (0.34 / 1.42 / 3.25) | 1.14 / 1.63 |
| ARRI LogC3 → Film Teal & Orange (`arri-logc3/awg3`) | 1.29 / 5.04 / 8.52 | 0.34 / 2.00 / 4.74 (0.31 / 1.87 / 4.63) | 1.18 / 2.65 |

The 8-bit Y'CbCr hand-off alone costs about 0.2 dE00 at the median; the remaining tail comes from colours the fitted stages fold or clip, which no residual can separate again.

**How the camera uses the tables.** The M1 base is composed by the camera with its own body matrix and gain exactly as the Log video modes compose theirs; the compiler derives it from the calibrated S-Log3 base of this project's Log feature (M1 input → S-Gamut3.Cine, then S-Gamut3.Cine → the look's gamut), so each body applies its own calibration. 18 % grey is placed where the stock N-Log curve encodes 0.18 (`--grey 400` curve units, also the S-Log3 calibration point); a larger value renders brighter. All three RGB curves are identical.

**What changes and what does not.**

- The still develop of a capture uses the tables. The JPEG gets the residual; **NEF embedded previews carry the fitted look without the residual** (one develop feeds both), while the NEF raw data stays original.
- **Photo-mode live view** (EVF, LCD and HDMI output) uses the same tables: whenever the camera updates its live-view parameters (about once per frame), those two builds also run the native N-Log chain with the look's tables. Frames shown through the CPU preview path (the path through which `nikon-sdr` looks reach live view, with the same 65×65 chroma cache and cost) additionally get the same residual grid, so live view approaches the JPEG; it starts only after two consecutive live builds used the look's tables, so the residual never lands on frames still developed with the previous ones. An identity residual skips the CPU. Outputs outside that path carry the tables only, with the "ISP only" accuracy in the table above.
- Movie mode, playback, menus, `nikon-sdr` looks and Off keep native live view; the Log feature group's N-Log, S-Log3 and Log3G10 monitoring in movie mode is unaffected. After a bank or look change, or turning photo LUT off, the next live-view build is native again.
- The develop selector (N-Log tone) is set only for the duration of these builds and restored right after, so metering, histogram, zebras and metadata always see the native SDR setting. The still develop and live view each use their own table copy and lane and may build at the same time on different CPUs; loading a new library never frees tables in use.
- A capture is admitted only when the selected Log look is available, as for every look. If the still develop did not use the capture's own tables (for example a library publication in progress), the JPEG stays native and its metadata claims no look.
- The `--runtime` export is refused for Log looks: their grid is a residual after the camera's tables, not a standalone transform. `--source` exports the original cube.

**Not verified on a camera.** The QEMU emulator runs the original firmware with a functional image pipeline that does not apply colour tables, so it proves the develop uses the look's tables and the JPEG gets exactly the residual, not the colours. The physical-camera checks still open: exposure placement of 18 % grey at still ISOs, the final Y'CbCr encoding of the still path and of live view (the residual is built for the JPEG's 8-bit full-range BT.601 encoding, the same premise as the `nikon-sdr` preview path), the delay from a live build to its frames, whether each body's calibration composes as for video, banding in smooth gradients, and per-channel curves.

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

Each capture latches its look when its output is reserved: it copies the grid, identity, name and basis, so changing the selection or loading a library never affects a capture already under way.

### Photo metadata

A JPEG with a look applied carries one more description in its existing XMP packet, in the `photolut` namespace (`https://github.com/ChrAlpha/NikonFirmwarePatch/ns/photo-lut/1.0/`, `Schema=1.1`): `LookName`, `TransformSHA256` (the transform identity shown by `library list`), `Applied`, `Rendering` (`baked-into-jpeg-pixels` for nikon-sdr looks, `isp-fitted+residual` for Log looks), `InputBasis` (the declared basis, e.g. `nikon-sdr` or `f-log2/f-gamut-c`), `FitSHA256` (Log looks: identity of the fitted tables), `Strength`/`StrengthScale`, `Grid`, `Algorithm` and `Schema`. It uses only the packet's space padding, so the XMP packet, APP1 segment and Exif offsets keep their lengths; Nikon MakerNotes, Picture Control, colour space, ICC, user comment and copyright are untouched.

- It is written only when the JPEG's main image was successfully encoded with the look (transformed pixels; for a Log look, developed through its tables); a failed encode, an undeveloped Log look or Off writes nothing.
- A no-op (identity) nikon-sdr look is recorded as `Applied=False`, `NoOp=True`.
- **NEF files get nothing**; their image and metadata stay original.
- This records provenance; it is not an instruction for Lightroom or NX Studio, which will not re-apply the look from it.

Read it with `exiftool -XMP-photolut:all DSC_0001.JPG` (checked with ExifTool 13.59).

### Developer checks

Python 3.10+, the dependencies in `requirements.txt` and `unicorn==2.1.4` are required; the compiler tests also need `requirements-photo-lut-log.txt` and skip without it. The firmware code is one translation unit, `z8photolut/src/unit.c` (including `hooks.S`), compiled with clang-19 by `tools/compile_units.py --units photo-lut` and linked by the in-house linker; its reviewed bytes are recorded in `resources/compiled.json` and `resources/photo-lut.bin`, and the tests execute those bytes. The tests take the original main payload from the official firmware:

```sh
python -m pip install -r requirements.txt unicorn==2.1.4
python -c "from z8prores.package import Package; open('z8-main-payload.bi','wb').write(Package(open('Z_8_0311.bin','rb').read()).eg[:-2])"
Z8_PHOTO_LUT_ROM=z8-main-payload.bi python -B -m unittest discover -s tests -p 'test_photo_lut*.py'
```

Without `Z8_PHOTO_LUT_ROM` only host tests run; `Z8_PHOTO_LUT_SAMPLE_JPEG` may name an original JPEG for the XMP helper tests, and `Z8_PHOTO_LUT_SLOW=1` also compiles a real cube at production settings when one is available.

### Evidence

Original-ROM settings tests additionally execute section-16 read/write, mode-4 restore and defaults over modelled NOR, including interrupted writes and stock-prefix preservation. Host tests cover the library format, editing, exports, malformed input, input-basis rules, the compiler (firmware integer formats, quantise-then-invert, the camera kernel) and parity with the camera's validator; automatic restore, pending-change merges, bank defaults, capture latching, the still-develop and live-view build wrappers, both provider lanes (including simultaneous builds), the live-view mode gate, selector restore, the shared colour hooks, the residual JPEG and live-view paths and XMP insertion run the actual compiled firmware code in Unicorn with stubbed file system and XMP composer. The QEMU emulator separately runs the original menus, card, a Log-look capture with traced still-develop and live-view builds, and movie mode. Physical menu drawing, card reads, colour, speed and Lightroom/NX Studio display are not verified.
