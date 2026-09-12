# ASMR-Cliper 程序包

适用于 Windows 10/11 x64。包含程序、引擎脚本、默认配置、FFmpeg 和 ffprobe；不包含 Python、推理依赖或模型。

1. 完整解压 ZIP，保持目录结构，运行 `ASMR-Cliper\asmrcliper.exe`。
2. 首次使用时打开「运行环境」，点击「补齐环境」安装 Python、分析依赖和所选模型。下载代理在「偏好设置 → 网络下载」配置。
3. 环境就绪后即可剪辑，正常使用无需联网。安装说明见 `docs/ENVIRONMENT.md`。

FFmpeg 已放在 `runtime/tools`，无需另行下载。其来源、文件校验值与构建验证结果记录在 `build-info.json`；上游许可和说明位于 `runtime/tools/FFmpeg-LICENSE.txt` 与 `FFmpeg-README.txt`。程序使用的 nlohmann/json 许可位于 `third_party/nlohmann/LICENSE.MIT`。

本包的构建验证覆盖 GUI、FFmpeg 和 ffprobe，不执行需要模型的剪辑推理。
