# 工具包、源码与验证 / Toolkit, source and verification

当前四组工具包为 [v0.4.0-rc.2 预发布版](https://github.com/XinmiaoZhao/NikonFirmwarePatch/releases/tag/v0.4.0-rc.2)。已经发布的版本、附件与对应提交见 [GitHub Releases](https://github.com/XinmiaoZhao/NikonFirmwarePatch/releases)；功能变化见[更新日志](../CHANGELOG.md)。工具包版本与用户选择的固件版本相互独立。

## 工具包内容

完整 toolkit 包含源码、许可证、功能文档、色彩报告及中英文图表、数值数据、后期 F LUT，以及九份由公开 C／汇编源码编译的补丁单元：

`precapture.bin`、`pixelshift.bin`、`pixelshift-af.bin`、`opengate.bin`、`anamorphic.bin`、`h265.bin`、`log.bin`、`quality.bin`、`shared.bin`。

不分发原厂或修改后的完整固件、实拍原片、运行库或编译器。Git 克隆与自动 Source code 归档不带补丁机器码；从源码构建时显式使用 `--code-source source`，或使用与源码一致的完整 toolkit。默认预编译模式不调用编译器，也不自动下载缺失文件。

## 构建身份与独立核验

输入为完整原厂 C 3.11，99,051,117 字节，SHA-256 `d67413c2ede6b9ce8887175f20700bd9fbad2a973a4820968c45503d15d80d96`。

`resources/compiled.json` 绑定代码长度、哈希、符号、编译参数及源文件哈希；`resources/combinations.json` 记录 15 种组合的修改范围。manifest 保存来源提交、功能选择、输出身份与文件哈希。

`verify.py` 独立解封装，检查原始输入、声明窗口、未修改字节、其他模块、身份和校验值。传入 `--features --version --tag` 可同时核对期望的组合与身份。不同组合或不同内容使用不同固件身份。Z8 支持降级与同版本重刷。

## 发布者复现

从最终审阅的干净提交生成源码、文档与附件。下面的 `VERSION` 和 `COMMIT_SHA` 使用本次实际版本和提交：

```sh
python -B tests/test_compiled_paths.py
python tools/build_release.py --tag VERSION --compiler clang --output /path/to/new-assets
python tools/check_release.py /path/to/new-assets/NikonFirmwarePatch-VERSION-toolkit.zip --commit COMMIT_SHA
python tests/validate_distribution.py --archive /path/to/new-assets/NikonFirmwarePatch-VERSION-toolkit.zip --archive-only --report /path/to/archive-report.json
python tests/validate_combinations.py --stock /path/to/Z_8_0311.bin --report /path/to/combinations-report.json
python tests/validate_prebuilt_cli.py --stock /path/to/Z_8_0311.bin --report /path/to/prebuilt-cli-report.json
python tests/validate_f_lut.py
python tools/check_repository.py --all
```

发布工具只纳入明确列出的已提交文件。独立检查器核对成员、源码字节、代码及来源，额外固件、缺失文件和错误身份会被拒绝。附件保持不可变，修订发布新版本。

## 当前源码（未发布）

照片 LUT（`photo-lut`）和共享色彩单元 `z8colour` 尚未发布，也没有对应的工具包。当前源码共十一个单元：在 v0.4.0-rc.2 的九个之外新增 `colour.bin`、`photo-lut.bin`，`shared.bin` 和 `log.bin` 也已改变。`resources/compiled.json` 记录各单元审阅时的编译器：`shared`、`log`、`colour`、`photo-lut` 为 clang 19（Debian 19.1.7；Ubuntu 24.04 的 19.1.1 产出相同字节），其余七个沿用 v0.4.0-rc.2 的 Apple clang 21.0.0。

clang 19 也能复现 `opengate`、`anamorphic`、`h265`、`quality`，但不能复现照片工具组的 `precapture`、`pixelshift`、`pixelshift-af`。`--code-source source` 对所有单元使用同一个编译器，因此不含 `photo-tools` 的组合可直接加 `--code-source source --compiler clang-19` 构建。含 `photo-tools` 时先组装预编译单元，再使用默认预编译模式；构建时仍按 `resources/compiled.json` 核对每个单元的字节：

```sh
python tools/compile_units.py --output build/units --compiler clang-19 --units opengate anamorphic h265 log quality shared colour photo-lut
cp build/units/*.bin resources/
# 这三个单元与 v0.4.0-rc.2 工具包中的字节相同。
for unit in precapture pixelshift pixelshift-af; do cp /path/to/NikonFirmwarePatch-v0.4.0-rc.2/resources/$unit.bin resources/; done
```

`tools/build_release.py` 目前用同一个编译器重编全部单元，并要求编译器记录完全一致，所以还不能为当前源码生成工具包。

## Windows 状态

[Host checks](https://github.com/XinmiaoZhao/NikonFirmwarePatch/actions/workflows/host-checks.yml) 包含跨平台路径检查。手动 Windows 作业按发布 SHA-256 下载最终 ZIP、对照当前提交核对全部成员，再分别用 Python 3.12 和 3.13 从解压目录构建两种组合，并把全选固件与指定回归输入的哈希比对。运行状态见上述 Actions 链接。

当前功能与测试范围见[验证记录](validation.md)，贡献者与许可见 [NOTICE](../NOTICE.md)。

## English

The four groups ship in v0.4.0-rc.2. A complete toolkit packages public source, documentation, bilingual colour figures/data, the optional F LUT and nine matching compiled patch units. Original/full modified firmware, camera media, runtimes and compilers are not distributed.

Source checkouts use explicit source compilation or a matching complete toolkit. Independent verification checks the exact input, declared edits, untouched bytes, other modules and identities. Release assets are built from a clean reviewed commit and retain that identity. Windows firmware jobs verify the published toolkit digest and source commit, then build from its extracted files on Python 3.12 and 3.13.

### Current source (unreleased)

Photo LUT (`photo-lut`) and the shared colour unit `z8colour` are unreleased, and no toolkit contains them. The current source has eleven units: `colour.bin` and `photo-lut.bin` join the nine of v0.4.0-rc.2, and `shared.bin` and `log.bin` have changed. `resources/compiled.json` records each unit's reviewed compiler: clang 19 for `shared`, `log`, `colour` and `photo-lut` (Debian 19.1.7; Ubuntu 24.04's 19.1.1 produces the same bytes) and Apple clang 21.0.0, as in v0.4.0-rc.2, for the other seven.

clang 19 also reproduces `opengate`, `anamorphic`, `h265` and `quality`, but not the photo-tools units `precapture`, `pixelshift` and `pixelshift-af`. `--code-source source` compiles every unit with one compiler, so selections without `photo-tools` build directly with `--code-source source --compiler clang-19`. With `photo-tools`, assemble the prebuilt units first and use the default prebuilt mode, which still checks every unit against `resources/compiled.json`:

```sh
python tools/compile_units.py --output build/units --compiler clang-19 --units opengate anamorphic h265 log quality shared colour photo-lut
cp build/units/*.bin resources/
# These three units are byte-identical to the v0.4.0-rc.2 toolkit.
for unit in precapture pixelshift pixelshift-af; do cp /path/to/NikonFirmwarePatch-v0.4.0-rc.2/resources/$unit.bin resources/; done
```

`tools/build_release.py` recompiles every unit with one compiler and requires identical compiler records, so it cannot yet package the current source.
