# 开发说明

## 0.6.1 视频音轨预处理与延迟封装

`audio_source.py` 为视频输入调用 FFmpeg 的 `-copyts -map 0:a:0 -c:a copy -vn`，提取与原容器同类的独立音轨；不使用提取后的音频重新编码最终成片。提取后逐包校验编码参数、字节内容、包数量和时间戳，拒绝失去映射关系的结果，再原子发布缓存和校验记录。语音识别与分类仅读取从该音轨生成的 PCM。旧的完整 PCM 分析缓存继续复用。

视频导出先索引完整 GOP，并把声音分析区间向允许范围内的关键帧收紧。此索引在一次任务内跨复核轮次复用。每轮先按最终区间复制音轨、执行完整语音复核；被拒绝的候选不复制画面，也不进行整段视频解码。可发布时，`copy_media_packets` 按相同区间一次性复制并封装原视频、音频包，`verify_reviewed_audio` 要求最终音轨的 SHA-256、包数量和时间映射与复核候选完全一致。

最终文件保留原有的逐包、时间戳、接缝与完整解码校验。完整 FFmpeg 解码移到复核之后，只执行一次，视频/音频解码各限 2 线程、滤镜线程限 1。日志区分提取音轨、音轨复核、视频封装与最终解码；`decode_validation` 和 `audio_review_before_video_export` 记录验证范围。模型推理资源策略保持原有设置。

参考：[FFmpeg streamcopy 与时间戳选项](https://ffmpeg.org/ffmpeg.html)。以下按版本分节的说明保留历史实现背景，以本节的当前视频流水线为准。

`src/MainWindow.cpp` 管理事件、任务调度、文件选择、配置、记录和提示词窗口；`src/MainWindowPages.cpp` 管理五页导航、布局和绘制；`src/UiControls.cpp` 管理下拉选择和开关状态，`src/UiTheme.h` 统一色彩。控件按页面与任务状态显示，底部进度只在工作期间出现。`ProcessRunner` 使用 `CreateProcessW` 启动隐藏的 Python 或 PowerShell 工作进程，并通过管道读取 UTF-8 JSON Lines；Job Object 负责取消整个子进程树。UI 线程通过窗口消息接收事件，不在后台线程直接修改控件。

单行编辑控件以实际 `TEXTMETRIC.tmHeight` 为高度，置于 40 逻辑像素的公共外框中垂直居中，左右各留 12 逻辑像素；框内留白处的点击转发给编辑控件。下拉选择沿用 `CB_*` 读写接口，点击、空格或方向键打开原生菜单；当前选项也写入窗口标题，便于读取控件名称。开关以 `BM_GETCHECK/BM_SETCHECK` 读写，点击或空格切换；禁用时阻止操作。

应用标题和版本资源为 ASMR-Cliper 0.4.0，CMake 的输出名为 `asmrcliper`。`src/app.ico` 含 16–256 像素七种尺寸，由 `src/app.svg` 所描述的耳机与声波图形生成；项目目录和引擎模块名保持兼容。

引擎入口：

```powershell
.\runtime\venv\Scripts\python.exe -X utf8 .\engine\main.py run --config .\runtime\jobs\example.json
.\runtime\venv\Scripts\python.exe -X utf8 .\engine\main.py doctor --config .\config\defaults.json
```

任务配置最少包含 `input`、`output_dir`、`mode`（`strict` / `relaxed` / `extract`）。其余字段继承 `config/defaults.json`。`language` 支持 `auto/ko/ja/zh/en`；`device` 支持 `auto/cuda/cpu`。所有路径使用 Unicode。

「保留声音」为独立设置页，复选框置于文字左侧，`true` 表示保留。五个布尔配置为 `keep_soft_laugh`（默认 true）、`keep_loud_laugh`、`keep_vaping`、`keep_drinking`、`keep_impacts`（后四项默认 false）。没有保留说话声配置；语音识别和情绪话语检查始终生效。页面单独保存和重置，不受其他页未完成输入影响。

AST 分类缓存版本为 `events-v4`，缓存保存类别证据，不包含保留/删除决定。`selected_exclusions` 每次规划都读取当前配置，复查后重新规划也如此。轻笑子类与一般 Laughter、大笑分别记录；有笑声证据时不把一般 Speech 标签直接升级为说话，但具体话语类别、实际转写词和情绪发声仍优先删除。未识别出轻笑子类的一般笑声按大笑类处理，属于需实际样本继续校准的边界。掉落/撞击类需同时出现相对局部背景的孤立短促突变；普通 Tap、Knock、Crackle 或音量本身不能触发这类排除。

处理阶段：

1. PyAV 解码首个音轨；AAC LC 保留逐包映射，其他编码建立等长 PCM 分析网格，一次生成 16 kHz 单声道分析副本、每帧能量与源包序号。分析副本不用于最终导出。
2. 所选的 Whisper large-v3、Qwen3-ASR 或 Turbo 做 VAD 辅助识别与连续覆盖识别。按模型提供的证据过滤非语言音节与常见幻觉；支持中断后的识别窗口恢复。
3. AST AudioSet 分类器识别音乐与边界附近的非语言声学上下文。没有写死某条录音的语言或活动时间段。
4. 两种模式先共用 `exclusions.py`：候选区间内部按 6 秒上下文、2 秒步长检查人声和休息动作，再用短窗定位强人声；带情绪的短句结合转写与声学证据处理。人声分数不会被同时出现的口腔音抵消。电子烟使用重复气流与吸呼气组合；饮水使用杯具与液体/口腔动作组合，或明确饮水话语加液体声音。休息动作跟随相邻停顿与活动外扩，遇到恢复的 ASMR 或缺乏证据时停止。分类标签组合只能判断疑似活动，不能识别具体设备。
5. 严格模式合并话语与密集聊天，并应用固定默认余量；宽松模式逐段检查动作边界，没有固定前后秒数或因密度直接整段删除的规则。找不到近处停顿时扩大搜索，再决定是否舍弃。两者均禁止重新保留已排除的话语或未勾选类别区间。勾选的声音若与说话重叠或无法自然衔接，仍可能随上下文舍弃。
6. 兼容旧版 `audit` 的可选初筛复查（默认关闭），在内存中检查候选计划并映射新证据；独立的完整版模型复核在导出实际成片后执行。
7. 按原 AAC 包序号复制帧并重建时间戳；用必要的隐藏预滚帧初始化首段解码状态。源容器时间戳取整漂移不会累积进分析时间轴。
8. 校验原压缩包、解码与接缝，并对实际成片进行完整版模型复核；按后文规则重剪或标记待复听，写入映射和报告，再将临时结果目录原子改名为正式目录。

边界选择使用能量和声学分类启发式。“完整动作”与“听者预期”不能仅凭波形作绝对判定。声学分类窗口、语音概率与停顿阈值都可能需要针对新的 ASMR 类型校准。应优先增加有代表性的实际录音回归样本，而不是只追求某一个文件的保留分钟数。

## 已验证

- 中文、空格、引号、反斜杠和 Shell 特殊字符参数的 Windows 参数解析往返。
- 取消任务后，Python 进程与其子进程均结束。
- 密集聊天中的可衔接短段在宽松模式下保留；宽松模式没有强制前 5 秒 / 后 7 秒；扩大搜索可以找回远处切点；正常短停顿保留。
- 取消导致的最后一行不完整识别缓存可恢复。
- 44.1 kHz 双声道、48 kHz 单声道 AAC 保持压缩包及解码样本对齐；测试的确定性合成音禁用了 AAC PNS，以避免随机噪声状态干扰对齐断言。
- 真实录音片段经过两种模式导出；C++ GUI 实际启动引擎并接收完成事件。

验证入口：`ctest --test-dir build -C Release --output-on-failure`。GUI 隐藏测试支持 `--self-test 图片路径 --test-report JSON路径`，实际任务测试支持 `--test-job 配置路径 --test-report JSON路径 --snapshot 图片路径`。

## 第三方来源

- [nlohmann/json 3.12.0](https://github.com/nlohmann/json/releases/tag/v3.12.0)：MIT 许可证随头文件保存。
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)：语音识别运行层。
- [large-v3-turbo CTranslate2 模型](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo)：模型来源。
- [AST ONNX 模型](https://huggingface.co/Xenova/ast-finetuned-audioset-10-10-0.4593)：来自 MIT AST AudioSet 模型的 ONNX 转换。
- [PyAV](https://pyav.org/docs/stable/) 与 [FFmpeg](https://ffmpeg.org/)：音频读写和完整解码校验。
- [ONNX Runtime CUDA 要求](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)：此项目固定 GPU Runtime 1.23.2 和 CUDA 12 / cuDNN 9 依赖，避免默认升级到 CUDA 13。
- [Windows 重定向管道](https://learn.microsoft.com/en-us/windows/win32/procthread/creating-a-child-process-with-redirected-input-and-output) 与 [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)：GUI 与引擎之间的进程实现参考。

本机的 `models` 和 `runtime` 用于本地运行，不随源码纳入版本管理。向第三方分发运行包时，应一并核对所用模型、FFmpeg 构建及依赖的许可证文件。


## 0.2 环境管理与 GUI 验证

`scripts/environment.ps1` 负责无 Python 时的引导、代理连通性测试和项目内安装锁；`environment_manager.py` 通过标准库完成环境检查、依赖安装和组件下载；`download_support.py` 实现 Range 断点、重试、校验与 ZIP 路径检查。完整约定见 `ENVIRONMENT.md`。

GUI 测试参数增加：

- `--snapshot 图片路径 --page task|history|environment|settings|logs`：仅渲染指定页面，不执行任务；网络页额外指定 `--settings-tab network`，保留声音页使用 `--settings-tab sounds`。
- `--test-action inspect|install|testproxy --test-report JSON路径`：实际启动 GUI 环境任务并记录最终状态；安装可通过 `--test-component` 选择组件。
- `--test-config JSON路径`：为隐藏测试覆盖配置，不写回用户设置。
- `--test-navigation JSON路径`：在屏幕外的原生窗口中模拟 49 次切换，只重绘脏控件并检查高亮颜色，验证旧导航及选项卡高亮及时清除；不写回用户设置。
- `--test-controls JSON路径`：验证下拉框和开关读写、输入框留白点击、代理启用状态、任务状态下控件显隐，以及空记录页入口；不写回用户设置。
- `--test-job JSON路径 --page history --snapshot 图片路径`：完成实际剪辑并渲染结果记录。

下载回归覆盖：Range 续传、服务器忽略 Range、网络中断重试、哈希损坏不替换原文件、断点身份失效、已有有效文件跳过网络、直接连接不继承环境代理、无效代理和 ZIP 路径越界。


## 0.4 正向提取和成片复核

模式值增加 `extract`。`extraction.py` 从同一套已缓存声学窗口筛选连续正向纹理证据，拒绝未知声音、单次命中和竞争性人声/音乐/气流/饮水/撞击证据。V4 的保留范围限制在重复证据的交集内，再使用上下文边界规划；用户勾选保留的休息声音不成为提取目标。判定标准是保守启发式，不是语义 ASMR 的已校准概率。

`review_enabled` 默认 true，V4 忽略关闭请求。`review_model` 指向独立的 `models/whisper-review`；环境管理器下载并按固定版本和 SHA-256 校验 Systran/faster-whisper-large-v3。原来的 `audit` 仍作为兼容性配置支持初筛模型的计划复查，默认 false，界面开关现在控制实际成片复核。

导出器先在任务临时目录复制 AAC 原帧并验证解码，再由 `Reviewer.inspect` 读取实际候选成片。模型按全时间轴覆盖识别，28 秒窗口边界另做 8 秒上下文复核，批次边界有 2 秒重叠；不会只扫描 VAD 命中的部分。清晰转写通过词概率、非语言音节与常见幻觉过滤后，作为剩余话语证据。`output_to_source` 使用原规划帧时钟映射跨接缝候选到源音频，两侧分别处理。

前几轮检出疑似话语时，导出器抛出 `SpeechRemaining`，临时候选不发布；流水线添加删除证据并重新规划。最多 `review_max_passes` 轮（默认 3，允许 1–5）。最后一轮仍有疑似项时，V2 / V3 发布成片但标为 `needs_review`，GUI 显示数量并提供「待复听位置」入口，打开最终成片对应的 `人声复核.csv`；V4 明确失败并保留 `runtime/cache/<音频>/post-review-latest.json`，不发布音频。每个发布的已复核结果，其 `speech_review.candidate_payload_sha256` 对应最终音频包校验值。

`review-results` 缓存以 `full-review-2` 规则版本、实际 AAC 包 SHA-256、解码时长、采样率、声道、语言和模型路径 / 文件大小 / 修改时间生成键。只写入完整推理结果；重试时必须匹配键才能复用。调整推理参数或话语接纳规则时应递增缓存规则版本。缓存中保留原始 `speech_found` / `passed` 推理结果，发布时的 `needs_review` 不改变缓存判定。

本版成片复核为本地 Whisper large-v3；Qwen3-ASR-1.7B 尚未接入。公开多语言基准不能证明 ASMR 场景的准确率，应使用同一份标注样本比较漏删、误删与耗时。


## 0.5 可选择语音模型与 V4 修正

`model_catalog.py` 集中定义语音模型 ID、存储路径和当前必需组件。Whisper 使用原 CTranslate2 环境；Qwen / CLAP 通过 `NeuralClient` 在独立 Python 子进程中运行，采用 JSON 协议和任务临时 NPY 文件交换数据。没有 Shell 命令拼接。子进程属于 GUI 的 Windows Job Object，取消会终止整个任务树。模型阶段结束即释放进程 / GPU 模型，避免 ASR、声学分析、CLAP 与成片复核同时占用显存。

Qwen 返回真实时间定位结果，不伪造 Whisper 的词概率或对数置信度；其接纳规则使用有效词语、非语言过滤和真实对齐区间。完整成片复核仍扫描实际候选文件；初筛和复核即使选同一个完整模型，也不复用初筛转写。复核缓存版本为 `full-review-3`，包含所选模型、定位模型版本及实际音频内容。

V4 使用 `semantic.confirm` 生成正向区间。CLAP 对同一窗口比较口腔音、表面动作、说话、休息和其他声音描述，再结合 AST 纹理与独立人声否决；连续段必须有多个明确命中，允许中间存在较弱但仍支持 ASMR 的窗口。相邻窗口只承担其中心区间，避免跨过被拒绝的窗口；后续规划仍排除已识别的话语、休息、突兀撞击与长静音。证据记录在 `extraction-evidence.json`，报告保存所用模型与阶段性证据。CLAP 是音频文本匹配模型，不能保证语义活动或主观听感绝对正确。

原 `extraction.py` 保留旧版纹理候选的诊断功能，现行 V4 不以它作为最终提取依据。旧的 0.4 描述仅记录该版本行为。


## 0.5.1 心跳与道具敲击

新增 `keep_heartbeat` 和 `keep_tapping`，默认 true。AST 使用 `Heart sounds, heartbeat`、`Tap`、`Knock` 标签；连续重复证据生成类别区间，取消勾选时由三个模式的规划器排除。普通敲击与物品掉落 / 孤立撞击仍分开判断，保留选项不会覆盖话语或已识别事故排除。

V4 为心跳与道具敲击增加独立 CLAP 描述，仍需连续正向证据；关闭的类别从提取目标转为竞争类别，并屏蔽已检出的区间。声学缓存升级为 `events-v5`，防止旧缓存缺少新字段而漏删；语音识别缓存仍可复用。原 `keep_vaping` 配置键兼容保留，界面名称为“呼气/烟雾”，不扩大为删除一切普通短呼吸。


## 0.6.0 音视频输入与原编码输出

`output_kind` 为 `auto`（默认）、`audio`、`video`，GUI 选择、保存与任务配置一致。`media.inspect_media` 忽略封面图轨道，选择首个音轨与首个视频轨道。非 AAC 音轨通过 `analyze_decoded` 转为本地 PCM 分析网格，分析副本不用于编码输出。

`media.video_groups` 按解码顺序建立 GOP 索引，并验证组内画面的呈现范围。`align_video` 仅保留原计划区间内的完整 GOP，丢弃跨边界的前导/尾部画面。输出同时保留两轨的源相对时间，每段统一平移，保留 PTS/DTS 的重排关系；音频仅复制完整包。MP4 使用微秒级 movie timescale，避免 edit list 默认毫秒精度造成音轨偏移；Matroska 的 DTS 由解码器重建，逐包核对其存储的 PTS 与单调解码顺序。

导出分别计算音视频原包 SHA-256、包长列表、编码参数与实际时间戳，随后完整解码。输出报告包含 `media_kind`、`video_payload_unchanged`、`video_codec`、`keyframe_trim_seconds`、`max_audio_boundary_gap` 与实际 `mapping`。MP4/MOV 的失效 timecode 轨道不自动生成。流模板继续保留方向等流信息，旋转视频已做实际读取检查。

视频复核按真实音轨时间戳解码（为包边界空隙补分析静音，不编码新音频），缓存身份加入实际映射。`mapped_output_to_source` 用导出后的 `analysis_start` 与 `output_start/end` 映射剩余话语，避免拿关键帧调整前的音频计划返算。复核失败仍走原有重剪与原子发布流程。

媒体回归覆盖 H.264 B 帧、H.265、VP9/Opus、可变帧率、偏移时间戳、视频转纯音频、MP3 原编码音频、无关键帧失败、无画面错误，以及以模型桩跑完整 V4 视频复核与重规划。画面测试比较解码后的源/成片图像哈希，确保没有保留已删除区间的画面。
