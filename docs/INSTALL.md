# macOS 安装与下载

## 一套流程，一个入口

1. 克隆项目得到 `mac-ai-kit/`，在终端进入该文件夹，运行 **`bash start.sh`**。这是 Bash 菜单，首次打开不需要 Python。
2. 选择要做的事：准备基础环境、安装 AI 能力、查看模型、预览或下载模型。只安装自己选中的组件，不默认全装。
3. 安装能力或下载模型时，入口先检查该任务所需依赖。缺少时引导准备，完成后继续原任务；不必复制命令或换一个脚本启动。系统安装、权限、条款仍按屏幕提示由你决定。
4. 在菜单选择共享模型根，默认 `~/AI/models`，也可输入已挂载外置盘的目录。本次会话的下载和能力安装沿用它，不把模型装进程序目录。
5. 下载时选好模型与目录便开始；必要配套自动加入。
6. 在「环境诊断与 API 说明」检查工具安装、配置与进程状态；本包不启动推理。后续由用户自行使用 App、CLI 或 llama-swap。

只看预览不会安装；打开菜单也不会触发安装。基础环境可单独准备，但不是要求你先手动执行的一段命令。底层脚本保留用于维护和审查，不是第二套用户操作流程。

## 运行条件与目录

- 仅原生 Apple Silicon Mac，不使用 Rosetta 或 Linux Docker 跑 Metal/MLX。
- 固定 MLX-Serve 26.10.2 要求 macOS 26.2+。SD.cpp 在安装时静态读取实际发行包的 ARM64 Mach-O 最低系统版本，不根据资产文件名判断。最低加载要求不等于推理实测通过。
- 芯片型号与 Metal 功能仍需逐项核实，256GB 内存不保证任意内核可用，也不等于磁盘容量。
- `~/AI/apps/<组件>-<版本>`：完整原生程序包，含动态库与资源。
- `~/AI/bin`：工具包生成的命令启动脚本，不是软件本体或完整 Python 环境。C++ 工具入口指向 `apps/` 内程序。MLX-Serve App 不创建命令入口。菜单内部无需改 PATH，其他终端可使用完整入口路径。
- Homebrew 工具保持 Homebrew 默认位置；已发现的命令通常优先复用，不搬入 `apps/`。sd.cpp 例外：为提供模型切换，单独安装官方最新受管包，不覆盖 Homebrew 或外部命令。本工具包不向 `/usr/bin` 等系统目录复制程序。
- `~/AI/models` 或你选的外置目录：共享权重。更换目录不自动搬迁、删除或复制旧模型。
- 默认 MLX-Serve App：`~/Applications/MLX-Serve.app`；Hermes 使用官方 `~/.hermes` 与 `~/.local/bin`（若设置 HERMES_HOME，以该目录为准）。

外置盘必须先挂载。工具不为未挂载的盘创建假的 `/Volumes/...` 目录。普通新模型目录会在能力安装或实际下载时创建；能力安装先安全准备目录，再交给 GUI 设置。入口拒绝根目录、含 .. 或符号链接祖先的目录，预览不创建目录。

## WeeToddCLI：完整 App 源码构建

从主菜单「安装运行工具 → 安装 WeeToddCLI」进入，或运行：

```bash
bash start.sh weetodd preview
bash start.sh weetodd install --xcode /Applications/Xcode.app
bash start.sh weetodd doctor
bash start.sh weetodd cli -- models --help
```

需要 Python 3.10+、原生 Apple Silicon、完整 Xcode 26+（包括 Metal Toolchain）和 FFmpeg/FFprobe。
安装前会确认；非交互使用 `--yes`。已有完整 App 使用 `install --app '/实际路径/WeeTodd Studio.app'`。
默认在 `~/AI/apps/weetodd-studio/` 保存固定提交源码与完整包，在 `~/AI/bin/weetodd` 写启动器；不拆出单个二进制、不覆盖已有不同文件、不更改系统 Xcode 选择。
这是独立于 MLX-Serve 的原生 H3 路线；不配置模型、不启动应用或服务、不执行生成测试。
完整参数、模型兼容性及两阶段潜空间放大步骤见 [WeeToddCLI](WEETODD_CLI.md)。

## MLX-Serve 仅安装桌面 App

安装官方完整 App，保留已有应用；不安装、发现或配置 MLX CLI 包装器。用户手动打开 App，添加共享模型目录、加载模型并开启 API。Benchmark 使用 App 显示的地址，默认示例为 `http://127.0.0.1:11234/v1`，不依赖 llama-swap。

### App 界面的共享目录

GUI 使用 Settings → Model Folders → Default folder；Custom folder 只是增加扫描，不能替代默认下载目录。菜单安装会传入所选共享目录；安装器只在明确提供目录时写入已核实的官方偏好 `com.dalcu.mlx-core/defaultModelDownloadPath`，不是另造配置格式。

仅支持核实的 26.10.2 非沙盒版；应用必须完全退出。旧值备份到程序根的 backups 目录，写后读回。商店版、其他版本、签名/沙盒信息无法确认或应用仍运行时停止配置，改由用户在 GUI 选择；不会擅自退出应用。备份标记 PRESENT 表示原设置存在，ABSENT 表示原来未设置，回滚只恢复这一项，不动其他个人设置。

外置目录失联时，上游可能回退 `~/.mlx-serve/models` 且继续扫描内置目录；先挂盘再启动，并在下载前检查设置。GUI 删除模型可能删除共享文件。扫描主要识别 root/name 或 root/author/name，不代表任意嵌套或所有格式均能加载。详见[共享模型目录](SHARED_MODELS.md)。

## 安装完成与遇到问题

- 下载与校验失败、必要文件缺失或依赖准备失败时，停止当前任务并显示问题；修复后仍从同一个入口重试。先前成功项保留，不盲目覆盖或删除。
- 已有命令通常保留并标为“版本未核验”，不等于版本符合要求。sd.cpp 会核验本工具包的发行记录和入口：旧受管版本升级到官方最新发行包，保留旧包目录；外部或修改过的入口不覆盖。固定目标目录缺少匹配记录或唯一可执行文件时停止，不强制修复。
- 安装完成仅表示下载、SHA256、解压及文件检查通过，不是 Mac 启动或推理验收。
- Apple 开发工具安装、管理员密码、首次权限和法律条款需由你在系统/应用界面完成；不要绕过 Gatekeeper、关闭 TLS 或删除隔离属性来解决失败。
- Hermes 最后安装。它仍需官方管理的 Python/uv 等依赖，可能构建 TUI/web、取得独立 Node 并写 shell PATH；“CLI”不代表只有一个文件。不会安装 Hermes desktop、配置 provider 或启用 gateway。

## 下载模型

菜单内部准备固定的 ModelScope 官方 `modelscope-hub==0.4.0` 下载客户端，使用独立 uv tool 环境，不把推理框架塞进系统 Python。若依赖未满足，不会假装已开始下载。详情见[下载器说明](../scripts/README.md)与 [uv tools 官方说明](https://docs.astral.sh/uv/concepts/tools/)。

模型只分“大模型、音频、图像/视频”。默认不选任何模型；技术详情保留仓库、精度和许可。Qwen-Image-2.1-Turbo BF16 完整套装（原生 RGBA）排在 Krea2 Turbo 之前；Music3/H3 为完整 MLX-Serve 8bit 包，YuE2 为 audio.cpp BF16 主模型、F32 VAE 与必需附件，不提供需自行转换的原始权重。

选好模型与目录后显示空间估算并直接下载，不追加 yes 或许可接受问题。平台如另需登录或接受条款，由你自行完成；工具不自动接受、不绕过限制。下载记录在共享根的 Mac-AI-Manifests 中，默认记录请求 revision，不冒称已解析不可变 commit。成功下载不等于运行兼容。

## 运行参考：安装后才需核对

安装与配置步骤不启动推理服务。「环境诊断与 API 说明」可只读查看工具状态；如需运行 llama-swap，请进入独立的「llama-swap（可选）」，明确配置并选择启动。登录启动单独设置，详见 [服务管理](services.md)。

以下其他后端的命令只供部署与手动验收，不是必须逐条输入的安装步骤。跨服务编排、其他后端自动启动和实际模型验收仍需另行配置。

### MLX-Serve

在桌面 App 中手动加载兼容模型并开启 API，端口与运行精度以 App 设置和实际日志为准。本包不配置或启动它的 CLI 后端。

### llama.cpp：独立 GGUF LLM 后端

本包采用官方原生 CLI/server，不增加 Python 推理环境。核实正式版 [v0.7.0](https://github.com/ggml-org/llama.cpp/releases/tag/v0.7.0)（2026-10-15）；该发行页指向 **b11669** 二进制，选择 macOS arm64 包，固定地址与校验值见文末。保留解压后的配套动态库和资源，不只复制单个可执行文件。

其他构建路径仅供维护者参考：[官方安装说明](https://github.com/ggml-org/llama.cpp/blob/master/docs/install.md)、[官方构建说明](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md)。菜单使用固定发行包，不要求用户自行构建。

macOS构建默认启用Metal，Metal可用不代表所有模型/精度均已适配；先核对 `llama-server --version`、`--help` 和 `--list-devices`，启动日志中应确认实际Metal设备与卸载层数。不要用绕过安全警告的方法运行程序。

已有兼容本地 GGUF 时，独立启动示例如下。将程序和模型路径替换为实际路径；这里**不选择、不下载任何模型**：

```bash
/ABSOLUTE/PATH/TO/llama-server \
  --model "/ABSOLUTE/PATH/TO/your-compatible-model.gguf" \
  --alias gguf-llm --host 127.0.0.1 --port 18081 \
  --ctx-size 4096 --parallel 1 --n-gpu-layers 99 --offline
```

`--n-gpu-layers 99` 是最多卸载99层的起始示例，不保证任意模型全卸载或内存充足。上下文/并发逐步增加并测峰值；不要直接把256GB全部分配给模型。`--offline` 避免服务自行联网取模型。API基址为 `http://127.0.0.1:18081/v1`；兼容客户端使用模型名 `gguf-llm`。健康检查 `GET /health` 在加载中返回503、就绪返回200。

交给llama-swap后，不要另开上述独立进程抢端口。改用本包配置中的 `gguf-llm`，客户端统一接 `http://127.0.0.1:8080/v1`，模型名仍为 `gguf-llm`。以下只是安装并确认模型后的验收命令，本包没有执行：

```bash
# 手动独立启动时检查后端；托管模式由llama-swap执行健康检查
curl --fail http://127.0.0.1:18081/health
# 托管模式：该请求会触发模型启动和一次文本推理
curl --fail http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gguf-llm","messages":[{"role":"user","content":"请用一句话介绍自己。"}],"max_tokens":64}'
```

参数与端点依据：[b11669 server文档](https://github.com/ggml-org/llama.cpp/blob/b11669/tools/server/README.md)。同一个GGUF不要同时由MLX-Serve内嵌后端和独立llama-server常驻。此处固定单模型，不开启llama.cpp内部多模型路由。

### llama-swap：统一启停入口

使用[官方release](https://github.com/mostlygeek/llama-swap/releases)的darwin arm64二进制，或按[官方安装说明](https://github.com/mostlygeek/llama-swap)安装。它有浏览器管理界面，不是原生DMG桌面应用，也不加载权重。

复制并修改 `config/llama-swap.example.yaml`，填入真实程序路径、启动参数、端口和健康检查路径。模板含明确占位符，不提供貌似能运行但实际接口不匹配的配置。

```bash
llama-swap --config "$HOME/AI/config/llama-swap.yaml" --listen 127.0.0.1:8080
```

启动前以安装版本的 `--help` 核对参数。外部客户端只调用统一入口；绕过入口调用后端的任务不会被统一编排。可以通过 `/upstream/<service-id>/...` 转发自定义API，但不能把不同服务的JSON格式视为自动一致。

### stable-diffusion.cpp：图片与H3 CPP后端

安装菜单下载 [官方最新发行版](https://github.com/leejet/stable-diffusion.cpp/releases/latest) 的 macOS 安装包，不使用 Homebrew，也不在本机编译。固定安装位置为 `~/AI/apps/sd.cpp/`；版本、来源和文件校验记录在目录内，不写进目录名。

- 一次安装只解析一次最新发行信息，随后下载、SHA256 校验与发布使用同一份计划。
- 静态检查实际 Mach-O 的 ARM64 架构与最低 macOS，不把资产名中的构建系统版本当作最低要求；异常则停止，不悄悄回退旧版。
- 正在运行 SD.cpp 时先停止，再升级。外部命令与旧版本目录不删除；只更新可确认属于工具包的命令入口。
- 服务菜单直接读取受管安装，不依赖 PATH。安装不自动启动或加载权重；切换模型见 [API.md](API.md)。

- Qwen-Image-2.1-Turbo 使用三组件 BF16 套装和专属 2.1 VAE；固定版本已含 Turbo/alpha 支持，PNG/WebP 才保留透明。详见 [模型说明](MODELS.md)。
- Krea2 只保留 Turbo BF16 完整套装；它是低步数效率版，不冒充未蒸馏原版。
- H3需配套专用编码器和音视频VAE；不能用任意普通Qwen3-VL编码器替换。
- API可用同步图片生成或 `/sdcpp/v1/img_gen`、`/sdcpp/v1/vid_gen`异步作业；后者返回任务ID后需轮询完成，详见[固定提交 API](https://github.com/leejet/stable-diffusion.cpp/blob/0886f11b6b8b4d1f6259e0f1337cae554284c37a/examples/server/api.md)。

### audio.cpp：音频CPP后端

只采用确切项目[0xShug0/audio.cpp](https://github.com/0xShug0/audio.cpp)，不是泛指音频C++程序。

选择[官方release](https://github.com/0xShug0/audio.cpp/releases)的macOS arm64 Metal包。有 `audiocpp_server` 和可选WebUI；WebUI不是原生桌面.app。模型应使用项目指定的GGUF/配置与配套资产，原始safetensors通常不能当作GGUF使用。

依[server说明](https://github.com/0xShug0/audio.cpp/blob/main/examples/server/README.md)编写模型配置，显式选择Metal且验证该模型实现支持。不要照抄CUDA示例。

### Hermes Agent：最后安装的第六组件，CLI 优先

固定版 v0.22.2，提交 `d923a5eb487a17c0a68801bba9ef0f56c81040a8`。脚本下载该提交的官方安装器，校验 SHA256 后调用：

固定调用参数为 `--commit <固定提交> --non-interactive --skip-browser --skip-computer-use`，由菜单内部执行，不需用户输入。


- 安装数据通常在 `~/.hermes`，命令在 `~/.local/bin/hermes`，官方安装日志在 `~/.hermes/logs/install.log`。若你显式设置 `HERMES_HOME`，数据与日志以该目录为准。
- 不传 `--include-desktop`；不运行 `hermes setup`、gateway install 或任何模型调用。`--non-interactive` 在该固定版本确实跳过 setup 和 gateway 阶段。
- `--skip-browser` 排除 agent-browser/Chromium，`--skip-computer-use` 排除 cua-driver，官方 PM 会记录这两个 opt-out。**不代表排除所有 Node/TUI/web 构建依赖。**
- 官方管理的 Python 3.14/uv 与必要依赖仍会安装；已有系统 Python 不是保证直接复用的全部运行时。Hermes 产品构建可能需要独立锁定的 Node。为了减少重复安装不篡改依赖，也不使用未经核实的“轻量”参数。
- 官方安装器可能向 `.zshrc` / `.zprofile` 等加入 `~/.local/bin` PATH，计划已披露。包装脚本本身不另行写 shell 配置。已有 Hermes 安装仅保留，不自动更新、重置 checkout 或覆盖数据。

在 Mac 上另行验收：

```bash
"$HOME/.local/bin/hermes" --version
"$HOME/.local/bin/hermes" --help
# 由你主动配置 provider / 工具权限；不在安装中执行
"$HOME/.local/bin/hermes" setup
```

本地 provider 可以指向已经验证的 `http://127.0.0.1:8080/v1` 与 `gguf-llm`；具体配置字段以该版本 setup/help 为准，不凭空生成配置。Agent 客户端就绪不代表底层图像、视频、音频原始权重已经适配。

[官方固定安装器](https://github.com/NousResearch/hermes-agent/blob/d923a5eb487a17c0a68801bba9ef0f56c81040a8/scripts/install.sh) · [依赖声明](https://github.com/NousResearch/hermes-agent/blob/d923a5eb487a17c0a68801bba9ef0f56c81040a8/pyproject.toml)

## 首次启动验收

1. 先分别确认程序版本和 `--help`，然后核对权重格式与模型类型。
2. 后端只绑定127.0.0.1。公网开放、局域网共享、Tailscale、API密钥另行设置，不由本脚本自动修改。
3. 每次只启动一个重模型。模型健康与端口存活是两回事。
4. 后续经用户决定再进行短任务实测；本包没有自动生成/播放音视频的脚本。
5. 测得峰值后再制定matrix允许组合；视频先独占。长作业的生命周期网关未完成前，不启用跨模型自动切换。
6. 需要开机服务时，只让launchd守护llama-swap/网关；不要同时KeepAlive所有被它管理的后端。

## 更新与回滚

- 保留上一已验证二进制与配置，升级单个组件，不一口气更新全部服务。
- 模型、程序和下载器分别锁版本。转换器若必要，只使用对应官方项目并另设隔离环境，不让下载器自行执行。
- 不自动删除旧模型/缓存。模型仓库移动或许可改变时，先复核再调整catalog。
- 本包MIT许可仅覆盖原创脚本/说明。发开源教程时附上各程序与模型的原始许可链接，不打包再分发模型。

## 固定发行资产



以下为官方 release 元数据提供的资产，由安装菜单下载、校验并解压。除 SD.cpp 动态最新发行外，能力安装脚本固定这些地址与校验值；本次仅编写脚本，没有执行安装。

- llama-swap：https://github.com/mostlygeek/llama-swap/releases/download/v265/llama-swap_265_darwin_arm64.tar.gz
  SHA256：01d8d543198973dcfa52fd263e5696230ee1a77fb204f48c5d542761a67623b0
- sd.cpp：从 [官方 latest API](https://api.github.com/repos/leejet/stable-diffusion.cpp/releases/latest) 读取资产 URL 与 SHA256，实际版本写入安装记录。其他组件继续使用下列已核验发行资产。
- audio.cpp：https://github.com/0xShug0/audio.cpp/releases/download/v0.9.2-drama3-base-small/audio-v0.9.2-drama3-base-small-bin-macos-arm64-metal.tar.gz
  SHA256：1281998c1acd170a7d92139853fe6ce6ab920e211d2ad3359775d43ad9ba616d


- llama.cpp：v0.7.0发行说明链接的b11669官方构建（不是声称最新nightly）
  https://github.com/ggml-org/llama.cpp/releases/download/b11669/llama-b11669-bin-macos-arm64.tar.gz
  SHA256：dbed55493d72bc1ba2babc8770d669c2289b5bc4ee9fb2a4dfc158d45b7d13fd
  [官方资产元数据](https://github.com/ggml-org/llama.cpp/releases/expanded_assets/b11669)：约11.6MB；未下载二进制，未在Mac实测安装占用。

  SHA256：7082424c2d4c3821c1b323e2822aec79502cc82475b01fd2bfcd70d6ccd7ec27
- Hermes 官方安装器：https://raw.githubusercontent.com/NousResearch/hermes-agent/d923a5eb487a17c0a68801bba9ef0f56c81040a8/scripts/install.sh
  SHA256：f4cdb0e20f1aca2c3871a76b962fd4db61510f549cf973759c753ac13349beb5
  安装器内容哈希由该固定源码文本计算；运行时的后续依赖校验由上游 PM/锁文件负责，不冒称此哈希覆盖所有下载。

- MLX-Serve App（桌面 App）：https://github.com/ddalcu/mlx-serve/releases/download/v26.10.2/MLX-Serve.dmg
  SHA256：2f49992cb5aca1cd91b59093d1c9f7ecf6166e842312600e1d0cd1c2574ed60a
  官方资产94,249,362字节；应用bundle ID `com.dalcu.mlx-core`。

配置依据：[官方 ModelRoots.swift](https://github.com/ddalcu/mlx-serve/blob/v26.10.2/app/Sources/MLXServe/Models/ModelRoots.swift)、[DownloadManager.swift](https://github.com/ddalcu/mlx-serve/blob/v26.10.2/app/Sources/MLXServe/Services/DownloadManager.swift)、[Info.plist](https://github.com/ddalcu/mlx-serve/blob/v26.10.2/app/Info.plist)。

音乐权重统一保存到共享根的 `music/`：YuE2 为 `music/Yue2-3B-GGUF/`，Music3 为 `music/MiniMax-Music3-MLX-Serve-8bit/`；Qwen3-ASR 语音权重放 `audio/`。菜单仍归“音频”组。旧目录不自动迁移或删除；H3 单选或双选会自动核验当前根内两版清单目录与旧 `video/h3-mlx-8bit`，同 SHA256 文件链接复用，只补缺失或不同文件。外部目录才使用手动复用入口；不全盘扫描、不迁移、不删除，来源须保留。

H3 下载可选 FL2VA、REF2VA 或两版；已有一版时在「管理本地模型 → 复用已有 H3 权重」 选择现有目录和目标版，完整 SHA256 核验后链接相同文件，只下载缺失/不同文件。保留源目录，勿移动或删除；详细版本与音频参考限制见 [H3](H3.md)。

## 按已安装项目补全接口说明

「环境诊断与 API 说明 → 生成或更新 API.md」 可指定安装根或模型根，离线检查后生成/更新该目录的 API.md，保留原内容；安装与下载主入口也会尝试更新。运行状态和版本不由文件存在推断。详见 [API.md 检查说明](API.md)。
