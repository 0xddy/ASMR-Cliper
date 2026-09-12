# 集成组件来源

本包保留 Python 压缩包中的许可文件、各 wheel 的 `.dist-info` 元数据与许可文件，以及 FFmpeg 的 `runtime/tools/FFmpeg-LICENSE.txt`。组件许可证各自适用。`build-info.json` 记录所使用的版本与下载校验值。

| 组件 | 上游 / 许可信息 |
|---|---|
| Python | https://www.python.org/ / https://docs.python.org/3/license.html |
| PyTorch CUDA | https://pytorch.org/ / `runtime/neural/Lib/site-packages/torch` 与相关 dist-info |
| Qwen ASR | https://github.com/QwenLM/Qwen3-ASR |
| Whisper / CTranslate2 | https://github.com/SYSTRAN/faster-whisper / https://github.com/OpenNMT/CTranslate2 |
| ONNX Runtime | https://github.com/microsoft/onnxruntime |
| NVIDIA CUDA / cuDNN wheel | https://docs.nvidia.com/cuda/eula/ / 随 wheel 附带的 NVIDIA 许可 |
| Microsoft VC143 CRT | VS 2022 的 `VC/Redist/MSVC/*/x64/Microsoft.VC143.CRT` 可再发行文件；https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution |
| FFmpeg | https://ffmpeg.org/ / Windows 构建及构建配置：https://www.gyan.dev/ffmpeg/builds/ |

模型来自 `config/environment.json` 中固定提交的 Hugging Face 仓库；具体地址和版本均随包记录。上游模型页面包含模型说明与许可：

- https://huggingface.co/Systran/faster-whisper-large-v3
- https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo
- https://huggingface.co/Qwen/Qwen3-ASR-1.7B
- https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B
- https://huggingface.co/Xenova/ast-finetuned-audioset-10-10-0.4593
- https://huggingface.co/laion/clap-htsat-unfused
