# 一键构建 win64-nv 完整集成包

将本项目源码提交到 GitHub 仓库，保留 `.github/workflows/build-win64-nv.yml`。工作流进入默认分支后，在 **Actions → Build win64-nv (full portable package) → Run workflow** 点击运行。默认使用 `windows-2022`，无需配置 Token、Python、CUDA Toolkit 或模型下载密钥。

运行成功后，会自动发布到仓库的 **Releases**。每次运行使用独立标签 `v版本号-win64-nv-运行ID`，绑定本次构建提交；同一运行重试会继续处理同一个 Release。

GitHub 要求[每个 Release 附件小于 2 GiB](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)，完整包会拆成每卷最多 1900 MiB 的文件。下载全部 `.zip.001`、`.zip.002` 等分卷、同名 `.zip.parts.json` 和 `merge-win64-nv.ps1` 到同一目录，然后在该目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\merge-win64-nv.ps1
```

脚本检查每卷和合并后 ZIP 的大小、SHA-256，生成完整 ZIP。合并需额外预留一个 ZIP 的空间。完整解压后运行 `ASMR-Cliper\asmrcliper.exe`，无需安装其他解压或合并工具。

该次任务的 Artifacts 中也保留：

- `ASMR-Cliper-版本号-win64-nv.zip`：完整 ZIP64 集成包，直接解压后运行 `ASMR-Cliper\asmrcliper.exe`。
- `win64-nv-final-reports-…`：ZIP 的 SHA-256 校验文件、编译与打包日志、CTest 报告和移动目录后的环境验证报告。

Actions 产物默认保留 7 天；Release 附件不会随 Actions 产物到期而删除。工作流仍仅手动启动，发布任务使用仓库自带的 `GITHUB_TOKEN`，无需额外配置 Token。GitHub 的构建时长、制品存储和下载额度仍按仓库所属账号计算。

## 独立任务与失败重试

下载与编译任务独立运行；打包任务等两者成功后才开始：

1. **Download all environments and models**：安装并保存完整环境快照为中间制品。
2. **Compile and test Win64 GUI**：仅编译 Release 程序并执行原生测试，不下载模型。
3. **Assemble and validate portable package**：下载前两项已完成的制品，执行 Python 回归、目录迁移验证与最终打包。
4. **Publish verified package to GitHub Release**：校验完整 ZIP 和构建提交，创建草稿并逐卷上传；全部附件的远端大小与 SHA-256 验证通过后公开发布。
5. **Delete intermediate artifacts after success**：Release 发布成功后，删除本次运行的环境快照、编译中间制品和相应中间日志。

如果编译或打包失败，在**该次运行**右上角选择 **Re-run failed jobs**，已成功的下载任务不会重新运行，打包会复用其环境快照。不要点新的 Run workflow 或 Re-run all jobs，这两种操作会重新执行下载。重试应在中间制品的 7 天有效期内完成。若下载任务本身失败，需要重跑下载任务；中断的任务没有完整可复用制品。

如果仅发布失败，同样选择 **Re-run failed jobs**：已成功的下载、编译和打包不会重跑。发布任务会重新获取最终 ZIP，复用草稿中已经校验通过的附件，仅补传缺失或损坏的附件。已公开发布的 Release 不会被覆盖。若需要使用新提交中的代码修复，必须从该提交启动新的 Run workflow；重跑旧任务仍然使用旧提交。

失败时保留已上传的中间制品，成功后立即清理。每个任务结束还会清理其专用工作目录，清理前校验目录所有权；不删除仓库、预装工具链或其他任务目录。未创建 Actions cache。最终 ZIP 与验证报告保留 7 天后过期；GitHub 已消耗的运行分钟数不能退回，存储用量统计也可能延迟更新。

## 集成内容

| 内容 | 版本 / 范围 |
|---|---|
| GUI | C++20 / Visual Studio 2022，Release x64，`asmrcliper.exe` |
| 核心 Python | 3.12.10 嵌入式，位于 `runtime/python` |
| 神经模型 Python | 独立的 3.12.10 嵌入式，位于 `runtime/neural` |
| NVIDIA 依赖 | 核心环境的 CTranslate2、ONNX Runtime GPU、cuBLAS、cuDNN、NVRTC；神经环境的 PyTorch 2.9.1 CUDA 12.8 |
| 模型 | Whisper large-v3、Whisper large-v3-turbo、Qwen3-ASR-1.7B、Qwen 时间定位、AST、CLAP |
| 媒体工具 | FFmpeg Windows essentials，校验发布者提供的 SHA-256 |
| Windows 运行库 | VS 2022 提供的 x64 CRT DLL，随两套 Python 放置 |

Python 与模型下载地址、模型提交版本和 SHA-256 来自 `config/environment.json`。核心与神经依赖分别使用 `engine/requirements.txt`、`engine/neural-requirements.txt`，PyTorch 固定从 `cu128` 索引安装，Qwen ASR 固定为 0.0.6。GitHub 无显卡的构建机也会安装 CUDA 版本。

FFmpeg 使用项目环境清单中的发布链接，会随上游发布更新；pip 的间接依赖也可能更新。因此这不是逐字节可复现构建。压缩包内 `build-info.json` 记录源码提交、构建时间、全部 pip 包版本、模型哈希、FFmpeg 压缩包哈希和运行库哈希，便于追溯。

## 空间与运行器

当前六组模型本身约 11.38 GiB，另有 CUDA、PyTorch、Python 和压缩输出。构建开始要求工作盘至少 **50 GiB 可用空间**，打包前还会根据实际文件总量检查输出盘。pip 禁用下载缓存，临时文件与构建文件放在同一工作盘。

独立发布任务要求至少 **25 GiB 可用空间**，只下载最终 ZIP 和报告；分卷逐个生成、上传后移除，不再复制整套模型或同时保存全部分卷。

GitHub 托管构建机会选择可用空间最多的本地磁盘，不删除预装工具。标准托管运行器的保证空间不足以覆盖本包的峰值，实际可用空间随镜像变化；如预检提示空间不足，在 Run workflow 的 `runner` 输入填写已配置的、带 VS 2022 C++ 工具链的较大 Windows x64 运行器标签。自托管机器使用 `RUNNER_TEMP` 所在磁盘，不自动写入其他磁盘。

参考：[GitHub 托管运行器规格](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[手动运行工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)。

## 验证与打包规则

编译任务执行 CTest 的 GUI 和参数传递测试；打包任务执行子进程取消和 Python 音视频剪辑回归，随后把整个目录从含空格与中文的构建路径移动到新路径。验证时仅保留 Windows 系统 PATH，检查两套 Python 的导入、核心依赖版本、CUDA 版 PyTorch、全部模型 SHA-256、FFmpeg 启动以及移动后的 GUI 控件测试。最后写入 ZIP64，完整读取压缩包校验 CRC，再生成整包 SHA-256。

GitHub 普通运行器没有 NVIDIA GPU，报告中的 GPU 检测可以为 false；它不会使 CUDA 版检查失败。CI 不执行 GPU 实际推理，也不以此声称验证了识别准确率。目标电脑需要可用的 NVIDIA 驱动，软件仍按用户设置选择推理设备。

不会打包开发机的 `runtime/venv`、用户设置、历史记录、日志、任务缓存、下载缓存或本机快捷方式。成品默认代理关闭，所有模型路径均为相对路径；用户仍可在软件中设置代理。

## 本地构建

使用 PowerShell 7，先安装 Visual Studio 2022 C++ Build Tools 和 CMake。下载与编译脚本分别使用新目录：

```powershell
pwsh -File .\scripts\download-win64-nv.ps1 `
  -WorkDirectory D:\asmrclip-download-001 `
  -Proxy http://127.0.0.1:10886

pwsh -File .\scripts\build-win64-nv.ps1 -WorkDirectory D:\asmrclip-native-001
```

直连下载时省略 `-Proxy`。构建代理只在此构建进程中生效，不会写入成品设置或全局 Git 配置。失败后日志保留在工作目录的 `reports` 中。

本地打包时，将下载目录的 `dependencies` 内容复制到一个独立打包目录的 `payload/ASMR Cliper 构建`，将编译目录的 `native` 复制到该打包目录下，再运行 `pwsh -File .\scripts\package-win64-nv.ps1 -WorkDirectory 该打包目录`。需要同时保留临时源文件和最终 ZIP 的磁盘空间。GitHub 工作流已自动完成这些传递步骤。
