> 当前四组预发布版的机身测试范围见[验证记录](validation.md)。本文按功能列出实际限制。

# 已知问题与功能限制 / Known issues and feature limits

适用于基于官方 Nikon Z8 C 3.11 的当前四组预发布版；“照片 LUT”一节适用于尚未发布的当前源码。历史公开版本限制保留具体版本号。以下列出当前实现中已确认的问题和明确的功能限制；操作方法见各功能指南，验证记录见[当前版本验证](validation.md)。

## Windows 工具包修订

原 v0.3.0 在 Windows 上构建预拍摄、像素位移或开放门时，可能报 `Compiled patch source does not match the reviewed recipe`，而 `--check` 仍通过。v0.3.0-r1 修复了路径分隔符比较。请重新解压完整修订工具包到新目录，使用原厂 C 3.11 输入和一个尚不存在的输出目录；无需修改配方或关闭哈希检查。`--check` 检查输入兼容性，不代表完整构建已验证。详见 [issue #12](https://github.com/XinmiaoZhao/NikonFirmwarePatch/issues/12)。

## RAW 连拍与预拍摄

- **预拍支持 FX/DX、L、C15/C30、HE／HE★。** 选择“无损压缩”回退 HE★，不是无损。C60/C120 RAW、HLG/HEIF 和副卡独立 JPEG 尺寸计划仍不支持。
- 组合包实拍确认 DX 与 FX 选择无损后写出 HE★；DX C30、仅 RAW、直接选择 HE★ 的组合仍待补测。
- **近满卡处理仍按 JPEG 计划。** 剩余张数已计入 NEF 及普通 JPEG 品质，但拍摄准入、提交预留、自动溢出换卡和 r／缓冲显示仍沿用原厂高速 JPEG 计划；排队等待写入的 NEF 没有全部预留空间。显示张数不能作为可写 NEF 数量的保证。数据库拒绝登记时会尝试 JPEG 回退，包括 RAW-only 设置；每张目标卡都应留足空间。
- **RAW 写卡积压会缩短连拍和预拍窗口。** RAW 帧缓冲保留到 NEF 写完，比原厂 JPEG 更早积压；连续短间隔拍摄时，可用帧数会减少。HE★ 比 HE 占用更多缓冲与卡空间。
- **停止文件会持续阻止额外 NEF 写入。** NEF 写入失败、写后找不到文件或超出默认 1.5 秒处理预算时，会尝试创建 `Z8PRESTP.BIN`。该文件存在期间，后续帧尝试回退 JPEG；重启不会清除它，主卡的停止状态还会影响副卡共享的 RAW 缓冲。处理方法见[停止文件指南](stop-file.md)。
- **部分可选元数据缺失。** 生成的 NEF 缺少部分原厂镜头校正块、灰度子图及 XMP 校正数据。

详细设置与双卡规则见 [RAW／预拍摄指南](raw-precapture.md#简体中文)。

## Log 与反挤压

- Log 回放名称与辅助按片段选择。新录和旧录 N-RAW 已上机确认；跨菜单库和断电重开后的交互仍值得补测。
- 当前默认 S-Log3 保留非负截断；高饱和青蓝、暗部及不同曝光的表现继续按[色彩报告](log-colour-report.md)更新。
- 反挤压只改变显示几何，录像保持原始像素比例；影片回放使用当前倍率。
- 机身试用未报告反挤压绿点异常；全高画幅边缘覆盖、小尺寸取景器和主体检测框仍缺逐项实测。

## ProRes

- **HQ 星标不能区分其余三档。** 本版用星标区分 HQ 与 422／LT／Proxy，部分帮助文字仍写 HQ。所选档位以画质菜单为准，实际录像可用 MOV 编码标识确认。
- **重设视频拍摄菜单不会重设新增档位。** 重设后如需改变档位，应在画质菜单中重新选择。
- 新增档位沿用原 ProRes 422 路径的画幅和帧率范围；不增加 ProRes RAW 或 3:2。

档位、菜单库和文件编码标识见 [ProRes 指南](prores.md)。

## 像素位移合成

- **v0.3.0／v0.3.0-r1 只识别起始文件编号为 `0001`、`2049`、`4097`、`6145`、`8193` 的组**，其他组列表为空（[issue #17](https://github.com/XinmiaoZhao/NikonFirmwarePatch/issues/17)）。本版已修复；机身上从 4660 和 4664 起拍的 4 张与 16 张组均合成成功。
- **列表不显示被拒帧的数量或原因。** 已识别为像素位移、但因格式或完整性检查被拒的帧不会出现在列表中；空列表不一定表示卡上没有像素位移照片。若相机拍出的完整组仍不出现，请保留原片并报告。
- **输入限完整的 4／8／16／32 张 FX/L、8256×5504、14-bit 无损压缩 NEF 组。** HE／HE★、DX、残缺或重复组不能合成，RAW 预拍生成的 HE／HE★ NEF 也不适用。
- **机内缩略图只显示首帧。** 合成输出为 NEFX，需在电脑上查看；机内回放不能用来查看最终合成结果。
- 4／8 张组输出 8256×5504，16／32 张组输出 16512×11008。重复合成另起文件名，不覆盖原片或旧输出。

组选择、输出与取消操作见[像素位移指南](pixelshift.md)。

## 开放门与 N-RAW 四档

- **开放门限 FX 3:2 N-RAW 8.3K 30／25／24p**，有效图像 8256×5504。不提供 50／60p 开放门，也不把 ProRes／H.265 改成 3:2。开放门与四档 N-RAW 是同一个构建选项。
- **两档画质的软件会合并档位名称。** 为兼容这类读取器，Fine 映射为 High、Basic 映射为 Normal；软件显示的两档名称不能用于区分实际的四档编码。
- **低码率档位有细节损失。** Basic 使用 1.0 bit/像素预算，实拍已观察到高频纹理减弱。开放门 High 为 2.5 bit/像素，低于普通 8.3K 30／25／24p High 的 3.0 bit/像素；不能把两者视为相同的单位像素压缩预算。

模式选择与各档码率预算见[开放门／N-RAW 指南](opengate-nraw.md)。

## 照片 LUT（未发布）

照片 LUT（`photo-lut`）只在当前源码中提供，尚未发布，也**没有任何实机验证**。宿主测试、在 Unicorn 中执行的固件代码和 QEMU 模拟器证明照片显影使用了风格的参数表、JPEG 精确应用了残差；模拟器的图像管线不应用颜色参数表，因此不能证明颜色。详见[照片 LUT](photo-lut.md#简体中文)。

- **Log 风格的颜色仍待实机确认：** 照片 ISO 下 18% 灰的曝光位置；照片路径和实时取景最终的 Y'CbCr 编码（残差按 JPEG 的 8 位全范围 BT.601 编码制作，与 `nikon-sdr` 预览路径的前提相同）；取景参数从构建到画面的延迟；各机身校准是否与视频相同地合成；平滑渐变中的色带；分通道曲线。
- **实机菜单显示、存储卡读取、色彩、速度以及 Lightroom／NX Studio 的显示均未验证。**
- **JPEG 残差按最近一次照片显影判断。** JPEG 是否应用 Log 风格的残差并写入 XMP，取决于最近一次照片显影是否使用了该风格的参数表，而不是该帧自己的那次显影。两者只在连拍期间重新发布风格库时才可能不一致。
- **实时取景的残差只用于固件取景模式 6。** Log 风格的参数表用于照片取景模式 6、9、11；残差只加在经 CPU 预览路径显示的模式 6 画面上。模式 9、11 只有参数表，精度对应拟合报告的“仅 ISP”。
- **NEF 内嵌预览不含残差。** Log 风格的 NEF 内嵌预览与 JPEG 来自同一次显影，带有拟合参数表但没有残差。NEF 原始数据保持原样，也不写照片 LUT 元数据。
- **没有发布工具包。** 从当前源码构建时，按[发布包内容和验证流程](releases.md#当前源码未发布)准备补丁单元。

风格库、Log 风格和存储卡载入规则见[照片 LUT 指南](photo-lut.md)。

## English

### Windows toolkit revision

The original v0.3.0 can reject pre-capture, pixel-shift or open-gate builds on Windows with `Compiled patch source does not match the reviewed recipe`, even when `--check` passes. v0.3.0-r1 fixes path-separator comparison. Extract the complete revised toolkit into a fresh directory and build from official C 3.11 into a new output directory. Keep the recipes and hash checks intact. `--check` validates input compatibility; it is not a complete build check. See [issue #12](https://github.com/XinmiaoZhao/NikonFirmwarePatch/issues/12).

Applies to the current four-group pre-release, built from official Nikon Z8 C 3.11; the Photo LUT section applies to the unreleased current source. Historical limitations name their affected releases.

### RAW capture and pre-capture

- Pre-capture supports FX/DX, L, C15/C30 and HE/HE★. Lossless selection falls back to HE★, not lossless. C60/C120 RAW, HLG/HEIF and separately sized secondary JPEG plans remain unsupported.
- Camera files confirm lossless-to-HE★ fallback in DX and FX. Direct HE★ selection in DX, C30, RAW-only remains a separate test.
- Admission, reservations, overflow switching and r/buffer display retain the stock JPEG plan, although the remaining-shot estimate includes NEF and ordinary JPEG quality. Queued NEFs are not all reserved. Database refusal attempts JPEG fallback even for RAW-only. Keep space on each destination card; the displayed count is not a guaranteed NEF capacity.
- RAW buffers remain occupied until NEF writing finishes. Write backlog reduces available burst/history frames; HE★ uses more memory and card space than HE.
- Failed, missing or over-budget NEF writes attempt to create `Z8PRESTP.BIN`. Its presence blocks further extra NEFs and prompts JPEG fallback; restarting does not clear it. A primary-card stop also affects the shared secondary RAW buffer. See the [stop-file guide](stop-file.md#english-summary).
- Some optional lens-correction blocks, a grayscale subimage and XMP correction metadata are absent.

### Log and desqueeze

The camera tester confirmed per-clip labels and view assist on previously recorded and new N-RAW clips. Cross-bank and power-cycle transitions remain separate tests. Desqueeze uses the current magnification on playback.

### ProRes

- HQ has a star; 422/LT/Proxy share the plain icon. Some help text still says HQ. Check the quality menu and the recorded MOV codec.
- Resetting the movie menu does not reset the added grade; select the desired grade again.
- Dimensions and frame rates remain those of stock ProRes 422. ProRes RAW and 3:2 are not added.

### Pixel shift

- **v0.3.0/v0.3.0-r1 recognize only groups whose first file number is `0001`, `2049`, `4097`, `6145` or `8193`**; other groups give an empty list ([issue #17](https://github.com/XinmiaoZhao/NikonFirmwarePatch/issues/17)). This release fixes group recognition. On camera, 4- and 16-frame groups starting at 4660 and 4664 merged successfully.
- The list does not show how many frames were refused or why. Frames recognized as pixel shift but refused by the format or completeness checks are omitted, so an empty list does not prove that the card has no pixel-shift shots. If a complete camera group is still missing, keep the originals and report it.
- Requires complete 4/8/16/32-frame FX/L 8256×5504 14-bit lossless NEF groups. HE/HE★, DX, incomplete and duplicate groups are unsupported.
- Thumbnails show the first frame. View the merged NEFX on a computer; camera playback does not display the merged result.
- Output is 8256×5504 for 4/8 frames and 16512×11008 for 16/32. Repeated merges use new filenames without overwriting inputs or earlier outputs.

### Open gate and N-RAW tiers

- Open gate is limited to FX 3:2 N-RAW 8.3K 30/25/24p, with 8256×5504 active pixels. It adds neither 50/60p open gate nor ProRes/H.265 3:2. Open gate and the four N-RAW tiers form one build option.
- Compatibility metadata maps Fine to High and Basic to Normal for two-tier readers. Those labels cannot distinguish all four actual encoding grades.
- Basic uses a 1.0-bit/pixel budget, with reduced fine texture observed in camera samples. Open-gate High uses 2.5 bits/pixel versus 3.0 for ordinary 8.3K 30/25/24p High.

### Photo LUT (unreleased)

Photo LUT (`photo-lut`) exists only in the current source. It is unreleased and **has not been verified on any camera**. Host tests, firmware code executed in Unicorn and the QEMU emulator show that the still develop uses the look's tables and that the JPEG gets exactly the residual; the emulator's image pipeline does not apply colour tables, so they do not prove colour. See [Photo LUT](photo-lut.md#english).

- Log-look colour still needs camera checks: exposure placement of 18 % grey at still ISOs, the final Y'CbCr encoding of the still path and of live view (the residual is built for the JPEG's 8-bit full-range BT.601 encoding, the same premise as the `nikon-sdr` preview path), the delay from a live build to its frames, whether each body's calibration composes as for video, banding in smooth gradients, and per-channel curves.
- Physical menu drawing, card reads, colour, speed and Lightroom/NX Studio display are not verified.
- The JPEG residual, and the XMP record with it, depends on whether the most recent still develop used the look's tables, not on the frame's own develop. The two can differ only when a library is republished during a burst.
- Log-look tables serve photo live-view modes 6, 9 and 11, but the residual reaches only mode 6 frames shown through the CPU preview path. Modes 9 and 11 get the tables only, with the "ISP only" accuracy of the fit report.
- NEF embedded previews of Log looks come from the same develop as the JPEG and carry the fitted tables without the residual. NEF raw data stays original and gets no Photo LUT metadata.
- No toolkit is published. To build from the current source, prepare the patch units as described in [release contents and verification](releases.md#current-source-unreleased).
