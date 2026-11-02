# Benchmark

菜单第二项 [MLX benchmark](MLX_BENCHMARK.md) 补足 50K、100K、200K 高上下文测试，第三项 [AIPerf 并发压测](AIPERF_BENCHMARK.md) 测量多并发下的延迟、吞吐与错误率。以下说明针对第一项 Llama benchmark；MLX-Serve 的 PP 计时不准，勿将其结果视为纯预填充性能。三项的语料、采样与计时口径不同，结果不可直接互比。

主菜单第 5 项 **Benchmark → Llama benchmark**，也可运行 `bash start.sh benchmark`。测试所选无认证 OpenAI 兼容接口，不安装、启动或停止推理服务。先准备 Python 3.10+ 和 uvx；缺失时提示并返回，不自动安装基础环境。

## 自动选择模型与 tokenizer

1. 默认连接 `http://127.0.0.1:11234/v1`，与 MLX benchmark 一致；不继承 llama-swap 代理端口。请先开启该端口上的服务，可在提示中改写实际地址，或用 `--base-url` 显式覆盖。只填主机与端口会补 `/v1`。先只读 GET `/models`，8 秒超时，不带 Key、不继承网络代理、不跟随重定向；不自动启动服务。
2. 一个模型自动选；多个模型显示编号供选择，绝不默认取首个。`/models` 是服务声明的列表，不证明任何模型已加载。
3. 从模型菜单保存的 models 根目录查找同源 tokenizer；独立 Python 入口也沿用此设置，未设置时才用 `~/AI/models`，`--models-dir` 优先。支持精确 repo ID、目录 basename、相对目录（如 `llm/Qwen3.8-27B-bf16`）、绝对目录、服务 `root`、本地 `config.json` / `tokenizer_config.json` 的原仓库元数据。扫描最多 4 层、5000 个目录，不跟随目录或 tokenizer 符号链接，不读模型权重。
4. 优先本地 `tokenizer.json`；`llm/…` 等本地分类路径绝不当成 Hugging Face 仓库。清单明确对应原仓库时才可由该路径回退到真实 repo ID。Qwen3.8-27B BF16 的原下载清单包含约 20 MB 的 `tokenizer.json`，无需为此重下权重。无本地文件时，使用目录来源元数据或服务返回的完整仓库形式 ID。后者只是服务声明的推定，不能独立证明模型身份；应使用服务对应的同源 tokenizer。不会删除量化后缀猜仓库，也不使用无关的 GPT2。已知转换映射保存在 `config/benchmark_tokenizers.json`，含核查来源及 tokenizer SHA256；跨仓库本地复用还须匹配该 hash。
5. 服务自定义 alias 无法自动证明对应关系时，只要求从本地候选中选一次同一模型；即使只有一个候选也不擅自认定。重名目录要求选择；空列表、连接失败、无可靠来源或无效选择给出原因并返回菜单，不进入模型/tokenizer 必填循环。

不再要求手填模型或 tokenizer。未知别名且本地没有可识别模型时，请让服务 `/models` 返回准确原仓库 `root`，或在模型菜单所选目录补齐同一模型的 tokenizer 后重试。私有、需授权或没有 `tokenizer.json` 的公共仓库会明确失败，不改用近似分词器。

## 默认参数与运行

- 最大**背景深度** 200000：测试 0、最大值的 1/4、1/2、最大值四档，取整数。可改为 4–2000000。
- `pp 2048`、`tg 480`、`runs 1`、`latency-mode generation`、`skip-coherence`，并发 1、失败停止。
- 每一档一次正式测量，不代表整套只发一次请求；仍有准备、预热和延迟测量。
- 200000 是名义背景深度，不是模型总上下文上限。加 pp/tg 至少约 202528 token，还须预留后端模板等开销。工具不调整模型或服务配置。

选好接口、模型、档位和报告目录后开始测试，不再额外输入 `yes`。按需联网准备固定版本 `llama-benchy==0.4.0`、依赖及公开测试语料。若需要公共 tokenizer，会明确展示仓库，只下载 `tokenizer.json`，不下载模型权重、不执行模型仓库的 Python/remote code。远程接口可能计费；不应填写不可信接口。

## 分词准确性与隔离

已核查 [PyPI 0.4.0 发布包](https://pypi.org/project/llama-benchy/0.4.0/) 的 `corpus.py` / `config.py`：支持本地 tokenizer JSON，但不支持服务端 `/tokenize`。其默认加载失败会回退 GPT2，且保留 JSON 中的截断/填充配置，因此本入口通过自有 `benchmark_runner.py` 调用固定版工具：

- 严格加载已选 `tokenizer.json`，关闭 truncation / padding，禁用整个 GPT2/transformers 回退分支；失败发生在推理开始前。
- 本地 tokenizer 在测试前复制到本次报告的私有目录，使用绝对路径传入 uvx；uvx 的 Python 隔离不阻止同用户读取这个文件。目录权限 0700、快照文件 0600，不改原文件权限、不请求提权。复制失败直接停止。
- 服务端返回的长输入 token 数与本地分词严重不一致时停止，不沿用上游的名义 token 数掩盖明显截断。服务未返回 usage 时仍无法证明后端实际接收完整深度，报告是名义深度测速，不作服务端完整性保证。

不询问、保存或传递用户 API Key。子进程使用受限环境及独立 HOME / HF 配置，不继承终端的密钥、代理凭据或登录配置。上游自身可能发送字面量 `Bearer EMPTY` 占位头，它不是用户密钥。不支持带账号密码、查询参数或片段的 URL。

## 报告与失败

默认保存在项目内 `output/reports/llama-benchmark-<UTC时间戳>-<随机后缀>/`，不受启动时所在目录影响。可在提示处或用 `--output` 更改报告父目录（绝对目录，支持 `~/`）。每次新建子目录，不覆盖旧报告；`.gitignore` 已忽略 `/output/`：

- `run.json`：接口、模型、参数、tokenizer 原来源与匹配依据。
- `tokenizer.json.audit`：实际 tokenizer 文件、SHA256 和严格分词设置。
- `tokenizer/tokenizer.json`：仅本地来源的私有快照。
- `.csv`：上游结构化结果；`stdout.log` / `stderr.log`：原始日志。
- `runtime-home/`：独立配置；输出根目录 `.llama-benchy-cache/` 复用公开依赖及 tokenizer 缓存。

运行时实时显示 stdout/stderr，Python 测试执行器使用无缓冲输出。开始会打印本次报告及两份日志的完整路径；默认位于 项目内 `output/reports/llama-benchmark-<时间戳>-<随机后缀>/`。原始日志不修改，终端转显会过滤危险控制序列，并保留普通百分比。uvx 依赖准备通常出现在 stderr；看到“Python 测试执行器已启动”才说明已进入执行器，之前 stdout 为空不能证明模型正在加载。子进程没有新输出时每 10 秒显示耗时，不伪造进度，也不自动重试或启动另一份测试。

失败/中断保留已有文件，不自动重跑。四档 pp/tg 不完整、指标无效或检测到计数近似时，不展示为成功。终端显示四行 PP/TG tok/s，宽屏另显示端到端首 token 毫秒。Ctrl-C 停止本次进程组，不管理目标服务。日志与来源路径可能包含私人信息，分享前检查。测速不是回答质量评测。

## 验证范围

只进行自有代码的离线测试、模拟 HTTP 响应和模拟 uvx 进程；未调用用户服务、未下载权重、未运行真实模型、未完成 Mac 或 200k 深度性能验收。

## 统一 HTML 报告

每次测试额外生成 `report.html`，与 MLX benchmark 使用同一离线版式。原 CSV 与日志不修改。报告按 test_name 配对 PP/TG，读取 t_s_mean 与 e2e_ttft_mean；客户端 PP 推导值保留在原始表格，不进入服务端 PP 摘要和曲线，以免将空 role 首包造成的虚高当作性能。TG 与端到端 TTFT 明确采用客户端口径。CSV 未提供的实际 token 数、缓存、结束原因、Ceiling 和 Drift 保持未知，不从 `tg480` 猜成实际生成480。

`--runs 1` 是正式每档一次；本包没有传 `--no-warmup`，llama-benchy 0.4.0 仍默认每档额外完整预热一次，另有模板校准和延迟探针。MLX 单次脚本的生成预热为0；只对齐输入预算与输出目标，不宣称热状态、语料、计时口径完全一致。单次 CSV 中的标准差0不证明稳定。失败时能解析的部分 CSV 仍生成标注失败的报告，不纳入摘要；无法解析则保留原文件并报原因。

计时问题已有上游讨论：[Llama Benchy PR #33](https://github.com/eugr/llama-benchy/pull/33)（2026-10-03，核查时未合并）。本包仅适配报告，没有应用该补丁或更改上游 PP 计算；不使用任意固定速度阈值宣判成绩真假。
