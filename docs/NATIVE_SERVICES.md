# 独立服务管理

主菜单第 6 项「服务管理」提供“启动全部”“仅启动音频”“仅启动制图”，以及独立的“仅启动 MLX 视频队列”“查看 MLX 视频队列状态”。全部启动、停止、状态和日志选择均包含 11235 视频队列，单独停止队列可用下方命令。不需要 llama-swap，也不会为了启动服务安装程序或下载权重。底层使用 macOS 当前用户的 LaunchAgent，操作方式类似 `systemctl`，但不是 Linux systemd。

## 常用命令

在 `mac-ai-kit/` 目录执行：

```bash
# 打开菜单；菜单本身不启动服务
bash start.sh services

# 启动全部当前可用且受支持的服务
bash start.sh services start

# 查看状态，不加载模型
bash start.sh services status

# 关闭本工具包管理的独立服务
bash start.sh services stop

# 只启动音频（转写／YuE2）
bash start.sh services start --service audio

# 只启动制图（SD.cpp）
bash start.sh services start --service sd-qwen

# 也可以单独停止或查看状态
bash start.sh services stop --service audio
bash start.sh services status --service audio

# 独立管理 11235 视频队列，不启停 11234 MLX-Serve
bash start.sh services start --service mlx-video-queue
bash start.sh services status --service mlx-video-queue
bash start.sh services status --service mlx-video-queue --json
bash start.sh services stop --service mlx-video-queue
bash start.sh services logs --service mlx-video-queue --follow
```

启停和状态的 `--service` 可选 `all`（默认）、`sd-qwen`、`audio` 或 `mlx-video-queue`；日志必须明确选择单个服务。直接脚本入口支持 `--root` 指定原生工具根（默认 `~/AI`），例如 `python3 scripts/native_services.py --root ~/AI --models-dir /你的模型目录 status`。交互菜单需要终端；自动化调用请明确指定 `status`、`start`、`stop` 或 `logs`。

## MLX 视频队列（11235）

队列复用原有 `mlx-video-queue` 命令、`org.mac-ai-kit.mlx-video-queue` launchd 标签及 `~/Library/Application Support/Mac AI Kit/mlx-video-queue` 状态目录，不在 `~/AI/services` 下创建第二套实例。菜单中的单独启动只启动包装层，不扫描权重、不预留 H3 模型内存、不写模型目录的 API.md，也不启停或加载 MLX-Serve 本体。需要已安装的 FFmpeg；不会自动安装。

“启动全部”先尝试队列，再按原有内存预检启动 SD.cpp / audio.cpp；队列失败仍尝试其他服务，并返回失败状态。图像/音频预算不足时，已启动的队列不会被回滚或停止。队列启动会继续已有等待任务，因此启动全部不是媒体任务的资源协调器；有待处理视频时，请遵守后文的资源协调规则，不同时提交图像/音频任务。

已有队列被重复启动时保留现有配置，不重启。菜单使用默认状态目录和启动参数；自定义输出目录、端口、FFmpeg 或上游认证文件仍通过 `bash start.sh mlx-video-queue start ...` 设置，停止后再次从菜单启动会使用默认参数。使用自定义 `--state` 的实例请继续用原命令和同一 `--state` 管理，菜单不会接管其他状态目录。

状态区分 launchd 注册、PID 和 `/healthz` 的工作线程健康，读取保存配置中的实际端口；不把队列健康当作上游模型就绪或任务空闲。JSON 的队列 `workload`、`stop_safety` 保持 `unknown`，没有原子停机屏障。“停止全部”现在也会停止队列，运行中任务可能中断；等待任务仍按原队列恢复规则保留。队列的停止沿用原入口，不承诺原生 SD/audio 的 35 秒退出追踪。详情见 [视频队列](MLX_VIDEO_QUEUE.md)。

## 单独启动与内存预检

菜单“仅启动音频”等同于 `start --service audio`；“仅启动制图”等同于 `start --service sd-qwen`。只检查所选服务的程序、模型及依赖和端口，只写它的配置、启动并等待它就绪；不要求另一服务已经安装或模型完整。启动后的状态与 API.md 追加也仅针对本次选择，另一服务忙碌或未就绪时不会等待或抢占它。启动全部仍保留原有批量行为。

预算为本次待启动模型的保守估算加 8 GiB 余量，必须不超过当前可回收内存估算的 85%。不启动的服务不计新增加载预算；例外是**已经运行、已注册或尚未退出的 audio.cpp**：它可能尚未加载权重，后续音频请求仍会占用内存，因此制图启动继续保留已保存的音频懒加载预算，并单独显示这一项。仅只读核查该预算，不检查其模型文件、不等待健康、不重配、不停止、不清理它的退出追踪记录。预算记录无效时安全拒绝；需要释放时请自行执行 `bash start.sh services stop --service audio` 并确认停止。

单项启动不是跳过内存保护。按当前完整清单，保守预算约为音频 29.8 GiB、默认 Qwen 制图 68.4 GiB、两项合计 90.2 GiB（包括余量；实际以本次输出为准）。例如可回收内存 26.3 GiB 时，允许预算约 22.4 GiB，即使只启动音频也可能被拒绝。请手动释放不使用的模型后重试；本工具不会卸载 MLX App 或自动停另一服务。现有音频自己的五分钟闲置卸载配置不变，本版没有新增全局闲置调度。

## 启动等待

API 健康检查最多等待 120 秒，每 15 秒一条简短进度。已运行但未就绪的受管服务也等待，不重复启动。SD 默认 Qwen 加载最多等待 30 分钟；正在加载其他模型时只等待，不抢占或替换。超时报告日志位置，保留服务，可稍后查看状态；Ctrl+C 仅退出等待，后台请求可能继续完成。

运行时校验只要求推理组件。Qwen 的许可文本仍保留在下载清单与许可说明中，不因文档目录缺失或没有运行时文件尺寸记录而阻止推理。缺少 Krea 不会阻止完整 Qwen，真正缺少权重、VAE、编码器或必要配置仍会拒绝对应模型。

## 日志在哪里看

菜单中可查看日志；命令行既可查看最近输出，也可持续跟踪：

```bash
# 查看图像服务的最近输出
bash start.sh services logs --service sd-qwen

# 查看音频服务
bash start.sh services logs --service audio

# 持续查看 stdout 和 stderr 新输出
bash start.sh services logs --service audio --follow
bash start.sh services logs --service sd-qwen --follow
```

日志、服务配置和 LaunchAgent 文件放在工具根的 `services/<服务ID>/`，默认分别为 `~/AI/services/sd-qwen/` 与 `~/AI/services/audio/`，不放在模型目录。日志视图区分标准输出与错误输出；排查问题时两者都要看。`--follow` 中按 Ctrl+C 仅结束日志查看，不停止后台服务。要停止请使用 `stop`。日志可能包含请求内容或本地路径，分享前请检查。

## API.md 何时修改

启动服务时检查文档，单项启动只补本项缺失的模型、服务或调用端点；已有内容全部保留。停止、状态、日志均不写 API.md。没有新增内容时不显示“已更新”。文档中的探测结果是历史记录，不表示服务现在仍在线；当前状态看服务管理。需要重新核查文件状态时，使用“环境诊断与 API 说明 → 查看与更新 API 说明”，显式重建受管检查区。

更新期间不要删除正在使用的工具包目录。若终端报告 getcwd，表示它所在的工作目录已经失效；重新进入新的目录运行 bash start.sh 即可。这不代表停止服务删除了模型目录。

## 当前管理范围

| 服务 ID | 后端 / 模型 | 默认本机接口 | 加载方式 |
| --- | --- | --- | --- |
| `sd-qwen` | SD.cpp / Qwen Image 2.1 Turbo 与 Krea 2 Turbo | `127.0.0.1:12345` | 首次启动默认加载 Qwen；WebUI / API 切换 |
| `audio` | audio.cpp / ASR 与 Yue2 | `127.0.0.1:18080` | 共用一个服务，`lazy_load=true`，`max_loaded_models=1`，闲置 5 分钟卸载 |
| `mlx-video-queue` | MLX-Serve 视频排队包装层 | `127.0.0.1:11235` | 不加载权重；转发到已有的 `127.0.0.1:11234` |

- 「全部启动」是启动全部符合条件的受管服务，不是把目录里所有模型都装进内存。程序版本、所需文件和模型组合必须通过检查；未安装、不完整、不支持或未验证的项目会跳过并报告原因，不自动补装或下载。
- audio 的健康接口可在权重尚未加载时就绪。ASR 与 Yue2 由首次实际请求按需加载；最大驻留模型数为 1 不等于所有请求都能安全并行。
- SD.cpp 服务就绪后默认加载 Qwen，按 Qwen 完整组件做保守内存预检；没有完整 Qwen 时只启动空服务。重复启动保留当前模型。本工具不是全局调度器，加载大模型前请释放其他 App 不用的权重。
- 状态页与 `API.md` 只在确认服务归属、运行 PID、API 健康且根页面确实含交互前端后列出 WebUI；纯健康文字页不算 WebUI。audio 开启前端但禁用前端模型管理（`ui=true`、`ui_management=false`）。
- audio 配置请求 Metal 后端；健康检查中的后端名称只是程序自报，不能证明所有步骤均在 GPU 上运行。
- 状态应分别理解进程是否运行、HTTP 接口是否就绪、权重是否已加载。进程存在或健康检查成功不等于模型已经加载，也不等于实际生成已通过验证。
- 图像服务只注册完整的本地 Qwen / Krea 组件目录，不复制权重、不建链接、不自动下载。
- MLX-Serve 仍是手动使用桌面 App：打开 App、加载模型、开启 API；这里不调用 MLX CLI，也不托管 App。
- H3 / Music3 模型加载仍为外部流程；这里只管理视频队列包装层，不管理 MLX App 或模型生命周期。
- llama-swap 是独立的可选入口，其启动、停止与日志不包含在本页的 `all` 中，见 [llama-swap（可选）](services.md)。

## 模型目录、退出与安全边界

服务沿用主菜单「设置模型目录」保存的选择，也可用 `--models-dir` 临时覆盖。工具根与模型根独立：权重可以放在外置盘，配置和日志仍留在工具根。更改模型目录不会搬移、删除权重，也不会自动改造已经运行的服务；切换前先停止对应服务，再用新目录启动。外置盘未挂载时不会悄悄改用默认模型目录。

默认不配置登录自启。关闭终端或退出菜单不等于停止已启动的服务，显式运行 `stop` 才关闭。停止操作仅针对本工具包拥有的 LaunchAgent，不通过进程名批量杀死用户自行运行的其他程序。不接管已占用的端口，也不修改外部服务。

独立服务监听 `0.0.0.0`，供可信局域网或 VPN 中的设备访问；接口无认证，切勿映射公网。API.md 同时列出本机地址与实际网卡地址（包括可读到的 Tailscale 地址），是否可达取决于防火墙与网络。旧版仅监听本机的受管服务，在主动选择启动该服务（单项或全部）时先停止再按新配置启动，当前任务会中断；仅查看状态不会重启或改配置。安装、查看状态、查看日志均不应被视为授权启动。离线测试不能代替 Apple Silicon Mac 上的真实模型加载、请求与停止验收。

## 已核对的上游版本

- [sd.cpp master-1006-0886f11 API](https://github.com/leejet/stable-diffusion.cpp/blob/master-1006-0886f11/examples/server/api.md)：`GET /v1/models` 检查就绪；`POST /v1/images/generations` 同步生成。Qwen 配方见[固定版文档](https://github.com/leejet/stable-diffusion.cpp/blob/master-1006-0886f11/docs/qwen_image_2.1.md)。
- [audio.cpp v0.9.2 服务说明](https://github.com/0xShug0/audio.cpp/blob/v0.9.2-drama3-base-small/examples/server/README.md)：`GET /health`、`GET /v1/models`；ASR `/v1/audio/transcriptions`，YuE2 `/v1/tasks/run`。服务返回 JSON 中的 base64 音频需要解码。
- 两个后端不统一请求格式；Agent 按 API.md 调对应端口即可，无需中间转发层。macOS launchd 只负责进程生命周期，不会转换 API 或安排整个机器的推理资源。

## 制图返回 WebP

生成的 API.md 制图请求显式发送 `output_format: "webp"`、`output_compression: 100`，响应图片 Base64 解码后保存为 `.webp`。质量 100 仍是有损 WebP。固定版服务器没有全局输出格式启动参数；WebUI 或其他客户端省略该字段仍用 PNG，请在客户端选择 WebP。`/v1/images/edits` 当前固定版仅支持 PNG/JPEG，不沿用这个设置。

## 停止确认

界面显示 `SD.cpp` 和 `audio.cpp · 转写／YuE2`；`sd-qwen`、`audio` 仅是内部目录与命令标识，不改动已有配置路径。停止后最多等待 35 秒，同时核查 launchd 注册与原进程退出；超时保留追踪记录，查看状态或再次停止可继续确认，不会向其他进程发送信号。

## 在同一个 SD.cpp 服务切换模型

打开状态显示的 WebUI，选 Qwen Image 2.1 Turbo 或 Krea 2 Turbo 配方，再选对应主干、文本编码器、VAE，点击 Load models。同一进程、同一 12345 端口，切换会释放旧图像上下文；不是多模型同时驻留。

API.md 自动记录这些端点和当前可用模型的命令：

- GET `/sdcpp/v1/recipes`：配方。
- GET `/sdcpp/v1/model-files`：实际组件文件 ID。
- GET `/sdcpp/v1/models/status`：当前模型、empty/loading/ready/error、加载秒数。
- POST `/sdcpp/v1/models/load`：提交 `recipe` 和 `models`（diffusion_model、llm、vae 对应实际文件 ID）。同步等待加载结束。
- POST `/sdcpp/v1/models/unload`：卸载权重，服务继续运行。

也可在项目根执行以下命令，客户端自动从文件列表匹配精确组件，不猜测 ID：

```bash
python3 scripts/sd_models.py load qwen-image-21-turbo
python3 scripts/sd_models.py load krea2-turbo
python3 scripts/sd_models.py status
python3 scripts/sd_models.py unload
```

这些命令直接调用已有 SD.cpp，不经过 llama-swap，不启动新服务。自定义端口或远程服务使用 `--url http://主机:端口`。遇到同名多份组件请在 WebUI 手选，脚本不猜。客户端超时不代表服务端已停止加载，先查询 status，不要立刻重复加载。

忙时返回 409 model_busy，不强行切换；加载中推理返回 503 model_loading。错误组件返回 400 且保留旧模型，真正加载失败可能返回 422 并留下空状态。生成请求的 model 字段不负责切换；多个客户端必须协调加载与生成顺序，原生接口不提供跨这两个请求的模型锁。

旧版升级：先停止 SD.cpp，再通过安装菜单更新官方最新发行包，最后启动。安装目录固定为 `~/AI/apps/sd.cpp`；保留原 sd-qwen 服务标识和日志目录，不创建第二个图像服务。仅查看状态不升级、不重启。真实 Mac 推理尚未在本包制作环境验证。

启动前检查当前模型目录，只追加 API.md 中缺失的模型说明；启动后只补缺少的服务与端点。没有新增不写文件，停止与查看状态也不写文件。确有新增时仅提示一行“已更新”，终端 API/WebUI 地址仍以当次健康探测为准。


## Agent 资源协调与 JSON 快照

API.md 会追加当前路径版本的 Agent 策略：LLM 必须先加载并验证能响应，视频执行时不运行 SD.cpp 或 audio.cpp；从视频切回前仅卸载视频模型，保持 LLM / MLX App。此为用户要求的资源策略，不是框架无法并行的结论。已核查的 MLX 调度让行不保证支持冷加载 LLM 或所有安装版本；合计内存不足时应请求调整方案，不悄悄卸载 LLM。视频任务 / 接口未确认时不擅自控制 App。

```bash
bash start.sh services status --json
bash start.sh services status --service audio --json
bash start.sh services status --service sd-qwen --json
```

返回 `schema_version=1`，包含实际工具包 / 服务 / 模型路径及所选 `services`。每项区分 `loaded`（launchd 注册）、`owned`、`pid`、`stopping`、`api_healthy`、`model_residency`、`workload` 与 `stop_safety`。SD 的 `model_residency=empty/ready/loading` 只是权重状态；音频当前没有核验过的全局任务计数，保守为 `unknown`。所有已注册 / 运行 / 正在退出的服务，`workload` 和 `stop_safety` 不会凭健康推为 idle，`atomic_shutdown_barrier=false`。只有精确确认本工具的 launchd 标签不存在且无受管待退出进程时才标 `workload=not_running` / `stop_safety=not_needed`；这不代表其他手动实例不存在。

JSON 查询不扫描模型、不写设置 / API.md、不清理退出标记。非 macOS 为 `supported=false`；查询失败保留未知值和 `error`。平台不支持、观察失败或参数错误返回非零（2），输出仍是 JSON；Agent 不得把失败当作已停止。快照不是锁，也不能保证返回之后没有新请求。JSON unknown 本身不证明空闲，但不否定 Agent 已通过独占入口和完整任务账本获得的独立终态证据。

**停机规则：** `stop` 本身仍可能中断任务，不能仅靠 `status --json` 的健康 / 模型状态决定无人值守停止。Agent 只有在独占全部媒体提交入口（包括 WebUI 与其他客户端）、确认所有同步 / 异步任务已终结且阻止新请求的协调约定有效，或得到用户对当前停止的明确确认后，才能用已有 `stop --service sd-qwen` / `stop --service audio`；已知忙碌 / 排队时默认等待，不将资源策略视为取消任务许可。独占协调与任务终态可独立证明时，Agent 可按策略主动停止，无需逐次重复询问；否则存在外部提交方、请求超时、任务丢失或未知时，等待并询问，不强杀。随后再次确认所选服务停止，才转入视频。不会新增代理调度器、闲置定时器、凭据或网络配置；没有声称实现 `--if-idle` 原子停机。

依据：audio.cpp 固定版默认模式的 [模型状态输出](https://github.com/0xShug0/audio.cpp/blob/v0.9.2-drama3-base-small/app/server/runtime.cpp#L2235-L2264) 不提供权威 busy / queue 观测；不能为此顺便开启尚未在本配置核验的实验并行模式。SD.cpp 的 [模型卸载保护](https://github.com/leejet/stable-diffusion.cpp/blob/master-1006-0886f11/examples/server/routes_models.cpp#L56-L71) 会原子拒绝 busy，但其他客户端仍可重新加载，且有 [独立放大任务](https://github.com/leejet/stable-diffusion.cpp/blob/master-1006-0886f11/examples/server/api.md#L989-L1033)，不能把卸载成功当作全局停机屏障。
