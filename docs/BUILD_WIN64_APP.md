# 程序包构建（含 FFmpeg）

在 GitHub **Actions → Build win64-app (program + FFmpeg) → Run workflow** 中运行。默认使用 `windows-2022`，需要 VS 2022 和至少 4 GiB 可用空间。

此工作流独立编译 Windows x64 Release 程序、执行原生测试，只下载并校验 FFmpeg，不安装 Python 分析环境或下载模型。构建机上的 Python 仅用于打包，不进入压缩包。

程序包包含：

- `asmrcliper.exe`、引擎脚本、默认配置、环境安装脚本和使用说明。
- `runtime/tools/ffmpeg.exe`、`ffprobe.exe` 及其上游许可和说明。

不包含 Python 运行时、虚拟环境、CUDA / 推理依赖、模型、下载缓存或个人设置。首次使用需通过程序的「运行环境」补齐分析依赖和模型。

编译、GUI 与 FFmpeg 测试成功后，自动发布到 [Releases](https://github.com/0xddy/ASMR-Cliper/releases)：

- 标签：`v<版本>-win64-app-<运行编号>`，与完整集成包区分。
- 附件：`ASMR-Cliper-<版本>-win64-app.zip` 和对应的 `.zip.sha256`；直接解压，无需合并分卷。

上传时先创建草稿，核对附件大小和 SHA-256 后公开；发布失败可使用 **Re-run failed jobs** 重试，复用已打好的 ZIP。Actions 中的 ZIP、校验文件及测试报告保留 7 天。失败的构建不会发布。

完整集成包仍使用 [win64-nv 工作流](BUILD_WIN64_NV.md)。
