# 环境管理与下载

ASMR-Cliper 的「运行环境」页使用与命令行相同的环境管理器，初次运行不要求系统已安装 Python。

## 界面操作

1. 在「偏好设置 → 网络下载」填写 HTTP / HTTPS 代理地址，保存设置。当前默认地址为 `http://127.0.0.1:10886`。关闭「通过代理下载」后直接连接，不继承系统环境变量中的代理。
2. 点击「测试连接」，分别查看 Python、PyPI、Hugging Face 的成功状态和耗时。下载大文件时仍可能受连接稳定性影响。
3. 打开「运行环境」，点击「检测环境」。程序启动后也会执行一次检测，不会自动下载。
4. 环境不完整时点击「补齐环境」，只下载缺失或损坏的必要组件。也可以使用缺失组件旁的「安装 / 修复」。全部就绪后隐藏安装按钮，有效文件不会重新下载。
5. 底部显示任务状态、文件下载量和速度。「取消任务」终止整个后台进程组。重试时保留有效文件，模型及 FFmpeg 下载支持断点续传。Python 的小型安装包中断后会重新下载；依赖安装由 pip 管理缓存和重试。

安装时可以切换页面，查看日志或处理记录。音频剪辑与安装不会同时运行，以免安装过程改变正在使用的依赖。

## 检测内容

| 组件 | 检测方式 | 来源 |
|---|---|---|
| Python | 项目 Python 实际启动，确认 3.12 | python.org，3.12.10 Windows x64 embedded ZIP |
| 分析依赖 | 子进程导入主要模块，核对固定版本 | pypi.org；pip 25.1.1 先通过已验证 wheel 引导 |
| Whisper 模型 | 文件大小和 SHA-256 | Systran/faster-whisper-large-v3、mobiuslabsgmbh/faster-whisper-large-v3-turbo |
| Qwen 及时间定位 | 文件大小和 SHA-256 | Qwen/Qwen3-ASR-1.7B、Qwen/Qwen3-ForcedAligner-0.6B |
| ASMR 声音识别 | 文件大小和 SHA-256 | laion/clap-htsat-unfused |
| 独立音频模型依赖 | 实际导入与固定版本检查 | PyTorch 官方源与 PyPI |
| 声音分类模型 | 文件大小和 SHA-256 | Hugging Face Xenova/ast-finetuned-audioset-10-10-0.4593 |
| FFmpeg | 实际执行 `ffmpeg -version` | gyan.dev 的 Essentials ZIP 及其发布校验值 |
| GPU（可选） | CTranslate2 检测可见的 NVIDIA GPU | 已有驱动；程序不安装系统显卡驱动 |

检查通过表示文件、版本、加载或设备可见性通过相应检测；实际 GPU 推理仍在剪辑中执行。

`config/environment.json` 固定 Python、pip 和模型的来源、模型 revision、大小与 SHA-256。FFmpeg 下载时取得发布者的当前 SHA-256，再验证安装包。现有可启动的 FFmpeg 会复用，不做自动版本升级。

默认 Whisper large-v3 与基础声音分类模型约 3.20 GiB；其他模型大小见后面的组件表。GPU 分析依赖另需数 GiB，下载缓存会额外占用空间。已有的健康 `runtime/venv` 优先复用；没有可用虚拟环境时，在 `runtime/python` 安装项目专用 Python。无需管理员权限，也不修改全局 Python、PATH、Git 或系统代理设置。

代理配置保存在本地 `config/user.json`。包含认证信息的代理地址也会随配置保存在本机。

## 命令行

```powershell
# 与 GUI 相同的安装流程，默认使用本机 10886 代理
.\scripts\setup-runtime.ps1

# 指定代理，或直接连接
.\scripts\setup-runtime.ps1 -Proxy 'http://127.0.0.1:7890'
.\scripts\setup-runtime.ps1 -Direct

# 只补齐某个组件
.\scripts\setup-runtime.ps1 -Component ffmpeg

# 检测；不需要联网
.\scripts\environment.ps1 -Action inspect -Config .\config\defaults.json
```

## 实现约定

- C++ 通过结构化参数调用 Windows PowerShell 5.1 脚本，不拼接可执行的命令字符串。
- PowerShell 负责无 Python 时的检测和引导安装；Python 标准库负责组件检测、下载和安装。UTF-8 JSON Lines 统一报告进度。
- 安装有跨进程文件锁；取消由 Windows Job Object 结束所有子进程。下载以 `.part` 暂存，文件验证通过才替换目标。
- 模型/FFmpeg 的断点记录绑定 URL、预期 SHA-256 和大小；服务器不支持 Range 时安全地重新下载。
- ZIP 安装禁止路径越界。模型的完整性缓存与文件大小、修改时间及预期哈希绑定。
- 音频分析不使用下载代理，也不上传录音。


## 0.4 成片复核模型（历史版本，0.5 以所选模型为准）

新增「成片复核模型」组件，使用固定版本的 Systran/faster-whisper-large-v3，约 2.88 GB，下载至 `models/whisper-review`。与 Turbo 初筛共用 faster-whisper / CTranslate2 运行依赖，不覆盖初筛模型；支持代理、SHA-256 校验和模型断点续传。成片复核开启或使用 V4 时该组件必需。

可以在界面单独下载，或运行 `scripts/environment.ps1 -Action install -Component review`。初筛和复核顺序运行，加载完整版前释放初筛模型；自动设备模式可在 GPU 初始化失败时回退 CPU。模型运行期间的真实显存需求还取决于批次和音频长度。


## 0.5 模型选择与独立依赖

0.6.8 起，「运行环境」分为「模型文件」和「基础环境」，负责检测、下载与修复；选择模型移至「偏好设置 → 识别与成片」，点击「保存当前分类」后用于环境检查。两处可通过「识别设置」和「管理模型」互相跳转。`speech_model` 支持 `whisper-large-v3`（默认）、`qwen3-asr`、`whisper-turbo`；`review_model_id` 支持前两项。未被当前配置使用的模型是可选组件，不阻止开始任务。选 Qwen 时还需要 `aligner` 和 `neural`；V4 需要 `clap` 和 `neural`。关闭复核仅对 V2 / V3 生效。

| 组件 | 内容 | 模型下载量 |
|---|---|---|
| review | Whisper large-v3 | 约 2.88 GiB |
| whisper | Whisper large-v3-turbo | 约 1.51 GiB |
| qwen | Qwen3-ASR-1.7B | 约 4.38 GiB |
| aligner | Qwen3-ForcedAligner-0.6B | 约 1.71 GiB |
| clap | CLAP HTSAT unfused | 约 0.58 GiB |

勾选「轻语 / 耳语」，或 V2 / V3 未勾选「喝水休息」时，需要 `clap` 和 `neural`；V4 始终需要。组件列表及「补齐环境」按当前选项计算必需组件，缺少时先补齐再开始分析。

`neural` 安装到 `runtime/neural/python.exe`，使用独立嵌入式 Python 3.12、PyTorch 2.9.1 和最小推理依赖；GPU 版本从 PyTorch 官方 CUDA 12.8 源安装，CPU 电脑使用 CPU 包。CUDA PyTorch 下载约 2.86 GB，另有其他依赖。不会升级核心引擎的 NumPy、Transformers 或 ONNX Runtime。Qwen 的网页演示、服务和 vLLM 依赖不参与本程序推理。

可单独运行 `scripts/environment.ps1 -Action install -Component qwen`，会同时补齐定位模型与依赖；`-Component clap` 会补齐 CLAP 和独立依赖。程序检测已安装文件的大小、SHA-256、依赖版本及实际导入。音频推理只使用本地文件，关闭 Hugging Face 联网加载；环境检测不触发模型推理。

较大模型文件按有界 HTTP Range 分块传输，完整下载后再核对总大小与 SHA-256。中断可从有效断点续传；不会把部分下载当成已安装模型。

来源：[Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-1.7B)、[Qwen 时间定位](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B)、[CLAP](https://huggingface.co/laion/clap-htsat-unfused)。固定版本和文件校验值见 `config/environment.json`。
