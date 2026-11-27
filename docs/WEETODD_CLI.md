# WeeTodd CLI：安装配置与 H3 两阶段潜空间放大

本文对应上游 [WeeTodd-Studio](https://github.com/wee-todd/WeeTodd-Studio) 固定提交 `b398fcc52a4cb9aaf91ed0739feda9f8620d14f8`。参数与模型契约已按该提交的 README、Swift CLI、模型配置和两阶段源码核对；不是按其他版本或 ComfyUI 节点名称猜测。

> **本次范围只有工具安装配置。** 不自动下载、转换、扫描或预检模型，不生成视频，不访问用户模型盘。本文后半部分的模型配置、预检与生成命令均为**将来由用户主动执行的示例**；写出示例不表示执行过，更不表示生成验收通过。Xcode、App 构建、worker 文件检查通过也不能证明模型兼容、显存足够或画质合格。

## 1. 工具入口与安装边界

在 mac-ai-kit 仓库根目录使用：

```bash
# 默认只读预览：不联网、不写配置、不安装、不启动模型
bash start.sh weetodd
bash start.sh weetodd preview

# 完整 Xcode 安装并完成首次初始化后，再主动安装
bash start.sh weetodd install --xcode /Applications/Xcode.app

# 如已有完整兼容 App，可复用它而不是重新构建
bash start.sh weetodd install --app "/path/to/WeeTodd Studio.app"

# 检查工具包状态，不等于原生模型 preflight
bash start.sh weetodd doctor

# 原生 CLI 参数透传
bash start.sh weetodd cli -- models --help
```

也可通过 `python3 scripts/weetodd_setup.py` 调用对应子命令；`install --yes` 用于明确同意安装步骤，不授权额外模型操作。安装可用 `--root`、`--bin-dir` 指定工具根目录和 wrapper 目录。

统一入口的 `cli` 后**必须用 `--` 分隔原生参数**，例如 `bash start.sh weetodd cli -- models --help`。安装后的 wrapper 则直接透传，不加这个分隔符，例如 `~/AI/bin/weetodd models --help`。

默认布局：

```text
~/AI/apps/weetodd-studio/
  config.json
  source/b398fcc52a4cb9aaf91ed0739feda9f8620d14f8/
    mac-ai-kit/WeeTodd Studio.app
~/AI/bin/weetodd
```

这里 App 是通过上游 `--output` 创建的独立包，不覆盖上游默认构建包。`--app PATH` 复用模式记录用户提供的完整 bundle，不能假设它也位于默认源码目录。

`config.json` 只记录 App、FFmpeg/FFprobe、worker 等工具路径，**不是 Studio settings，也不是模型 recipe**。安装器不写 Studio settings、不配置模型，更不会替用户登记模型位置。不要把模型路径追加为未经实现的 `--model`、`--config` 参数。

安装完整性验证包括 `codesign` 校验、H3 manifest 中记录的 SHA-256、`LTXNative/mlx.metallib` 存在性及原生 CLI `models --help`。`doctor` 只做安装完整性和媒体工具检查；这些不是读取模型后的 worker preflight，更不是渲染测试。本文没有自行执行上述安装验证，实际安装是否成功以工具输出为准。

构建需要 Apple Silicon 和**完整 Xcode 26+**；只有 Command Line Tools 不足。上游要求 macOS 26+ SDK，并在构建时探测 Metal 与 SwiftUI 宏。先打开 Xcode 完成许可及组件安装；缺少 Metal 时按 Xcode Settings → Components 安装 Metal Toolchain。`--xcode` 接受 `.app` 或 `Contents/Developer`，上游不会因此修改系统全局 `xcode-select`。复用 App 不等于验证其模型运行能力。

完整 bundle 应保留：

```text
WeeTodd Studio.app/
  Contents/MacOS/WeeToddCLI
  Contents/MacOS/WeeToddH3MLXWorker
  Contents/MacOS/WeeToddLTXWorker
  Contents/Resources/LTXNative/mlx.metallib
  Contents/Resources/H3MLXNative/manifest.json
  Contents/Resources/RendererSource/
```

**不要仅拷贝 CLI 或 worker 到 `~/AI/bin`。** H3 Swift MLX worker 与 LTX worker 共享 `Contents/Resources/LTXNative/mlx.metallib`，不是猜测的 `H3Native/mlx.metallib`。源码的解析器先解析可执行文件的符号链接；位于 `Contents/MacOS` 的 worker 只选择上述 bundle 资源，不回退到旁边的影子文件。非打包 worker 则要求其自身目录下的 `mlx.metallib`。旧的 `WeeToddH3Worker` 也不是本文使用的 `WeeToddH3MLXWorker`。

## 2. 先分清三种配置与入口

| 对象 | 内容与用途 |
| --- | --- |
| mac-ai-kit 工具配置 | App、CLI、FFmpeg/FFprobe、worker 的路径；安装器管理，不含模型任务 |
| 模型 recipe | `format: "weetodd-headless-v2"`；含 `engine`、`components`、`config`、`prompt`、`conditioning` 等 |
| Studio 原生 job | `format: "weetodd-studio-native-job-v1"`；由 Studio 导出，冻结项目、准备后的 recipe、输入观察及运行时 |

**`WeeToddCLI` 的普通原生生成入口是 `--job`，不是 `--recipe`。** 它接受：

```text
--job FILE                 必需：Studio 导出的 job
--output-directory DIR     必需：本次输出目录
--preflight-only           只做 job/worker 预检，不生成；仍会读取模型/媒体并可能写日志
--resume                   校验后复用已完成产物，未完成项仍可能生成
--h3-swift-worker PATH      覆盖移动后的 H3 worker 路径
--ltx25-swift-worker PATH   覆盖移动后的 LTX worker 路径
```

原生 job 不接受任意采样覆盖 flag；分辨率、seed、Turbo、放大与细化设置应先在 Studio/recipe 中确定，再导出。`--resume` **不是只读检查或 dry-run**。`--preflight-only` 也不是本仓库的 `preview`。

上游另有 `upscale`（FlashVSR）、`regenerate`、`process`，不能把它们等同于 H3 新生成片段的两阶段潜空间放大。`models list/download/convert/sharp-table/prepare-h3` 是特定原生模型/修复组件管理命令，不是通用 H3 profile 创建入口；尤其 `models prepare-h3` 默认走 restoration，可能下载和准备组件，不适合作为安装后自动诊断。

上游 Python 的 `scripts/setup_models.py` / `scripts/render_headless.py --recipe ...` 是另一套入口；工具包安装不承诺已建立其完整 Python 推理依赖，也不能把它们的 flags 直接传给 `WeeToddCLI`。

## 3. 将来配置模型：原地链接，不自动复制

以下步骤本次**未执行**。先确认模型许可、完整组件及可用内存，再由用户选择自己的稳定本地路径；所有 `/REPLACE/...` 都是占位符，不是现有机器上的模型位置。

### 推荐：Studio 原生模型设置，再由 CLI 执行 job

在 Studio 的 **Models & LoRAs / Model setup** 中选择原生 H3 对应任务，链接现有兼容组件，保留 Swift MLX 后端。不要误选 Draw Things 或 legacy Python 路径。模型库浏览不会自动生成；创建 profile、准备 clip 和导出 job 涉及结构检查或预检，应留到用户授权模型阶段。

H3 recipe 的真实组件键如下：

| `components` 键 | 应选择的内容 |
| --- | --- |
| `checkpoint` | 对应任务的 support / H3 task manifest **目录**，不是 transformer 文件 |
| `transformer` | 对应任务的兼容 transformer 文件或受支持完整分页包；Swift 引导设置优先识别其支持的原生文件，不能任取内部 page |
| `text_encoder` | Qwen3-VL 编码器分页包根目录，保留 manifest 和 pages |
| `processor` | 对应 Qwen3-VL processor 目录 |
| `tokenizer` | tokenizer 目录或原生支持的 `tokenizer.json`；Swift 设置会把目录解析为其中的文件 |
| `video_vae` | 兼容 H3 video VAE 文件；例如已准备包中的 `video_vae_affine_q8.safetensors` |
| `audio_vae` | 兼容 folded-weight audio VAE；Swift 设置选择文件，Python 设置也支持受支持的包目录 |
| `vision_encoder` | 原生图像/参考任务在需要时链接的 Qwen vision tower 文件或目录；不要丢弃包内 vision 组件 |
| `task` | 配方内部的 `t2va` / `fl2va` / `ref2va`，由任务设置解析 |

`conditioning.task` 使用另一层任务名：纯文本 `t2v`，首帧/首尾帧 `fflf`，外观参考 `ref2va`。纯文本/关键帧使用相匹配的 FL2VA 组件与 FL Turbo；参考任务需要真正 Ref2VA 组件与 Ref Turbo，不能只改 task 字符串就混用权重。两阶段还需**单独链接 H3 learned latent upscaler**；普通基础模型包不包含所有 Turbo/upscaler。

### 可选：已有兼容 Python 环境时，通过 CLI 创建基础模型 profile

这是上游真实的命令行配置接口，**会读取组件并运行配置/组件预检，本次没有执行**。它创建基础 recipe，不自动补齐两阶段设置，也不等于 `WeeToddCLI --job` 所需的 job。以下从固定提交的上游源码根目录运行；`python` 必须是用户另行准备的兼容环境：

```bash
# 将来才执行：替换所有路径；不要照抄到未知模型目录
python scripts/setup_models.py create h3-text \
  --component "checkpoint=/REPLACE/MODELS/fl2va-support" \
  --component "transformer=/REPLACE/MODELS/fl2va-transformer" \
  --component "text_encoder=/REPLACE/MODELS/qwen-vision-pack" \
  --component "processor=/REPLACE/MODELS/fl2va-support/processor" \
  --component "tokenizer=/REPLACE/MODELS/fl2va-support/tokenizer" \
  --component "video_vae=/REPLACE/MODELS/video_vae_affine_q8.safetensors" \
  --component "audio_vae=/REPLACE/MODELS/fl2va-support/audio_vae" \
  --profiles-directory /REPLACE/MODEL-RECIPES \
  --memory-mode lower_memory
```

返回的 `recipePath` 是新建且唯一命名的基础 recipe。可导入 Studio，复核原生 Swift 兼容性，再配置下节两阶段参数。首尾帧用 `h3-image`，参考任务用 `h3-reference`，并替换成其匹配组件、补齐媒体。`lower_memory` 不是内存一定够用的保证。该脚本的 `scan PRESET ROOT...` 会扫描指定目录，`download ID --destination DIR` 会下载模型；都不属于本次安装范围。

## 4. 将来使用：896×512 → 1344×768，1.5×，Turbo 4 + 标准 3

这是 **H3 自动两阶段生成**：先生成小分辨率草稿的 AV 潜变量，放大视频潜变量，再进行标准细化，最后解码。不是先生成 MP4 再做像素放大，也不是 LTX 的 8+3 recipe。

在 Studio 原生 H3 clip 中配置：

| 设置 | 本例值 |
| --- | --- |
| 最终画布 Width / Height | **1344 / 768** |
| Two-stage upscale | 开启 |
| Draft scale | **1.5** |
| 计算后的草稿画布 | **896×512** |
| Draft sampling | 默认 Turbo 策略，Euler，**4 次模型计算** |
| Turbo adapter | 任务匹配，强度 **1**，从第 0 次开始 |
| Latent upscaler | learned H3 模型，非 bilinear 插值 |
| Refinement evaluations | **3 次实际计算**，标准 adapter 策略 |
| Second-pass strength | **0.35** |
| Seed | **42** |
| 帧率 | **24 fps**；H3 原生时间网格按 24 fps 处理 |
| 时长 | 本例请求约 **5 秒**，记录实际对齐后的时长；不等于已生成 5 秒 |

上游先把每个最终轴除以 `draft_scale`，再向上取到 32 的倍数；本例计算结果正好为 896×512。**最终尺寸填 1344×768，不填 896×512**；最终画布不自动替用户取整。

项目输出选择 **24 fps、MP4，保留音轨**。1344×768 是 7:4，不是精确 16:9，不为凑比例非等比拉伸。
首个示例用纯文本 T2VA（配方 conditioning 为 `t2v`），无需参考素材；建议提示词：

> A locked-off close-up of an adult singer holding one sustained open vowel, then closing their lips naturally as the note ends. Soft side lighting, slight head turn, visible eyes and mouth, no cut, no camera zoom.

这只是未来测试素材，不应把其声音或口型视为用户 MV 的既定素材。学习型放大器可对照 [H3 社区 3D 权重](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler)，以当前导入校验为准；缺失时停止，不改用 2D、Interpolation、RelayVSR 或 MLX-Serve。

1.5× 路线验证有效后，才另导出相同模型、提示词与种子的 **2×：672×384 → 1344×768** 对照，并使用不同输出目录。可选的 `draft_scale=1.34` 得到 1024×576 草稿；Studio 常规界面只提供 1.5×/2×，只有能在冻结前可靠重新准备配方时才尝试，禁止直接篡改带校验的导出 job。

下面仅为查看导出 recipe 时使用的**字段片段**，不是独立可执行 job，也不应覆盖整个已有 recipe：

```json
{
  "config": {
    "width": 1344,
    "height": 768,
    "steps": 5,
    "sampling_method": "euler",
    "seed": 42,
    "projection_backend": "mlx",
    "transformer_backend": "mlx",
    "attention_policy": "dense",
    "transformer_weight_cache_gb": 0
  },
  "loras": {
    "version": 1,
    "adapters": [
      {
        "path": "/REPLACE/MODELS/task-matched-turbo.safetensors",
        "strength": 1.0,
        "profile": "turbo",
        "qkv_layout": "contiguous_qkv",
        "start_after_evaluations": 0
      }
    ]
  },
  "two_stage": {
    "version": 1,
    "draft_scale": 1.5,
    "strength": 0.35,
    "evaluations": 3,
    "refinement_adapter_policy": "standard",
    "learned_upscaler_path": "/REPLACE/MODELS/h3-learned-upscaler.safetensors",
    "learned_upscaler_header_sha256": "REPLACE_WITH_STUDIO_RECORDED_HEADER_SHA256"
  }
}
```

关键解释：

- `config.steps=5` 是 **5 个调度网格点、4 次 Turbo 计算**。不能写成 4，也不要把两阶段写成 `steps=7`。`two_stage.evaluations=3` 是另一个独立的实际计算预算。
- 片段省略了 `duration_seconds`、基础组件、prompt、conditioning 和其他已准备设置；应保留 Studio 生成的完整内容。24 fps 不是一个假想的 CLI `--fps` 参数。
- `loras` 是 recipe **顶层**的版本化 descriptor。它不能与非空 `components.loras` pair 列表重复定义。匹配 Turbo 的元数据是重要条件，不是文件名含有 “turbo” 就足够。
- Turbo 只用于第一阶段；第二阶段排除已识别的 Turbo，普通创意 LoRA 保持原强度参与两阶段。不需要额外臆造一个“标准三步 LoRA”路径。
- learned H3 upscaler 要求兼容的 **24 通道 BF16** 权重结构。上游已知文件名为 `minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors`，但兼容性取决于头部/结构，不仅是文件名；不能用 LTX upscaler 替代。
- `learned_upscaler_header_sha256` 必须是 Studio 检查后记录的真实 64 位小写十六进制值。其哈希范围是 **SafeTensors 的 8 字节长度前缀 + 原始 JSON header**，不是整个模型文件。上例故意使用无效占位符，不能直接执行或伪造。
- `resize_method` 与 learned 模型路径/头部哈希**互斥**。选择 learned 时不要再填 `resize_method: "bilinear"`。上游历史 bilinear 示例有明显重影，不应宣称质量等同。
- `strength=0.35` 是共同采样时钟的比例，**不是直接指定 video sigma 为 0.35**。实际 video/audio 起始 sigma 由实现计算并记录。
- 草稿音频潜变量原样保留，最后只做一次音频解码；这不保证声学质量、对白可懂度，也不表示复制了参考音频。
- 最终轴需为 32 的倍数且至少 64，长边不超过 1920，短边不超过 1088；草稿还要满足普通 H3 预算，参考输入也占用行数。不能只看分辨率合法就断言能跑。
- 不混用 continuation、保存来源的 refinement、Fun 控制、Motion repair、FastH3、VDN、实验注意力或 transformer weight cache。关闭不兼容项后重新准备/导出，不静默降级。

## 5. 将来导出、预检与生成：必须显式执行

确认第 3、4 节模型和设置后，在 Studio 选择 **Movie → Export Clip Headless Job**（或 Export Movie Headless Job）。保留 job 旁边的 prepared-input 文件夹、模型、worker 和 FFmpeg 安装。不要把单独的 `weetodd-headless-v2` recipe 当成 job 传入。

以下命令是**尚未执行的后续示例**，替换路径后由用户分别决定是否执行：

```bash
# 将来预检：会读取模型/媒体、检查 FFmpeg 和 worker；不是工具 preview
bash start.sh weetodd cli -- \
  --job /REPLACE/JOBS/H3.weetodd-job.json \
  --output-directory /REPLACE/OUTPUTS/h3-preflight \
  --preflight-only

# 将来真正生成：显式去掉 --preflight-only，并选新输出目录
bash start.sh weetodd cli -- \
  --job /REPLACE/JOBS/H3.weetodd-job.json \
  --output-directory /REPLACE/OUTPUTS/h3-render

# 以后恢复同一 job；未完成部分可能执行新生成
bash start.sh weetodd cli -- \
  --job /REPLACE/JOBS/H3.weetodd-job.json \
  --output-directory /REPLACE/OUTPUTS/h3-render \
  --resume
```

若 App 移动，可显式添加：

```text
--h3-swift-worker "/NEW/PATH/WeeTodd Studio.app/Contents/MacOS/WeeToddH3MLXWorker"
```

worker 路径覆盖不能修复所有移动的模型、FFmpeg 或 prepared-input 路径；必要时重新导出。原生 job 校验被冻结的任务、输入和 worker 身份，改过 job/输入后不应强行复用原输出目录。

原生 movie job 的 finishing 有单独限制：支持的 cut/音量/fit/fill 不等于支持全部 GUI 特效；标题、转场、额外音轨、finishing 放大/插帧等组合会拒绝。**H3 生成过程中的 `two_stage` 与 movie finishing 的 upscale 是两件事。**

### 将来的验收清单（不是本次结果）

- 工具层：完整 bundle、共享 Metal 库及 FFmpeg/FFprobe 可用。
- 模型层：真实 job 的 worker preflight 通过，任务、组件、Turbo、upscaler 身份一致。
- 执行层：receipt、worker JSONL 日志、实际输出媒体完整；不是只看进程退出码或有一个 MP4。
- 参数层：核对草稿/最终画布、4+3 实际评估次数、0.35、42 和 24 fps；H3 会对齐帧数，生成原片可能长于请求时长。
- 质量层：播放检查运动、细节、重影、参考一致性及声音；本参数组合没有在本机获得任何生成或质量验收。

将来生成后，可用下列命令核对尺寸、实际帧率、帧数/时长及音轨（本次不执行）：

```bash
ffprobe -v error \
  -show_entries 'stream=codec_type,codec_name,width,height,avg_frame_rate,nb_frames,duration:format=duration' \
  -of json /REPLACE/OUTPUTS/final.mp4
```

仅看到 768 高度不能证明走了潜空间两阶段；日志还须证明 Swift/MLX H3、草稿尺寸、learned upscaler、标准细化次数/强度和最后解码。正常速度播放重点看眼睛/发际线闪跳、持续元音嘴形及是否提前收口。计时从提交到可播放 MP4，分列草稿、放大、细化、解码/编码，内存峰值只报告实际测到的数值。预检失败若改约 3 秒排查，须重新导出并使用新目录，不能写成 5 秒通过。FFmpeg 只做编码、封装或探测，不承担本例的放大。

## 6. 源码核对位置

以下路径均相对于上述固定提交的上游仓库：

- `studio/README.md`：Build and open、Guided model setup、H3 Two-stage upscale、Headless movie and clip jobs。
- `examples/headless/README.md`：H3 组件链接、Native H3 two-stage exports 与 Python/native 入口差异。
- `studio/Sources/WeeToddCLI/main.swift`、`StudioCore/NativeHeadlessCLIArguments.swift`（后者位于同一 `studio/Sources/` 下）：真实子命令与原生 flags。
- `scripts/setup_models.py`、`src/wee_todd_mlx/model_setup.py`、`studio/Sources/StudioCore/NativeModelSetup.swift`：组件键、profile 创建及预检行为。
- `studio/Sources/StudioCore/H3TwoStageSettings.swift`、`NativeH3TwoStagePreparation.swift`、`NativeH3LoRAComposition.swift`、`NativeH3LearnedUpscalerMetadata.swift`（后三者同目录）：默认值、几何、LoRA 与头部哈希。
- `integrations/weetodd-mlx/Sources/H3MLX/H3TwoStageRecipe.swift`、`H3TwoStagePlan.swift`（后者同目录）：worker 两阶段契约与强度语义。
- `scripts/build_studio_app.py`、`scripts/build_h3_mlx_worker.py`、`integrations/weetodd-inference/Sources/InferenceContracts/NativeMLXMetalLibrary.swift`：打包与共享 Metal 资源查找。

本文只核对源码和文档；未执行任何模型下载、组件创建/预检或生成，也未触碰外置模型卷。
