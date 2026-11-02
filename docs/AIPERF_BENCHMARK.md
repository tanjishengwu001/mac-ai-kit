# AIPerf 并发压力测试

主菜单第 5 项 **Benchmark → 第 3 项 AIPerf 并发压力测试**，也可运行 `bash start.sh aiperf-benchmark`。

```bash
bash start.sh aiperf-benchmark
bash start.sh aiperf-benchmark --base-url http://127.0.0.1:11234/v1 --concurrency 1,4,8
bash start.sh aiperf-benchmark --duration 60 --warmup 0
bash start.sh script-help aiperf-benchmark
```

只测试已启动的无认证 OpenAI 兼容接口，不安装、启动或停止推理服务，不下载模型权重。先准备 Python 3.10+；运行时用 `uvx` 在隔离环境取固定版 `aiperf==0.13.0`，不写进系统 Python。首次运行会联网下载该包及其依赖，之后走 uv 缓存。

## 与另外两项的区别

| | 本项 | Llama benchmark | MLX benchmark |
|---|---|---|---|
| 测量对象 | 多并发下的延迟、吞吐、错误率 | 单请求上下文吞吐 | 单请求长上下文吞吐 |
| 输入预算 | aiperf 合成语料 | pp 2048 + 背景档 | 2048 + 背景档 |
| 停止条件 | 每档请求数或时长 | 固定档位各一次 | 固定四档各一次 |
| 主要指标 | TTFT、请求时延、ITL、tok/s、req/s | PP / TG / 端到端首 token | 服务端原生 PP / TG |

三者语料、采样与计时口径不同，**结果不可直接互比**。本项回答的是“并发升高时服务如何退化”，不是“这个模型多快”。

## 自动选择模型与 tokenizer

1. 默认 `http://127.0.0.1:11234/v1`；只填主机与端口会补 `/v1`。不接受 URL 中的账号密码、查询参数或片段。只读 GET `/models`，8 秒超时，不带 Key、不继承网络代理、不跟随重定向。
2. 一个模型自动选；多个模型显示编号供选择，绝不默认取首个。aiperf 本身不会提示选择模型，缺 `--model` 会直接配置报错，所以模型必须由这一步确定，也可用 `--model` 覆盖。
3. tokenizer 优先本地同源 `tokenizer.json` 所在目录（绝对路径），规则与 Llama benchmark 一致：精确 repo ID、目录名、相对路径、服务 `root`、本地 `config.json` 元数据；不跟随符号链接、不读权重、不猜量化后缀。
4. 匹配不到时退 `builtin`（tiktoken 近似词表），报告与终端都会标注“token 数为估算”。词表只影响 token 类指标（ISL/OSL、tok/s、ITL），不影响 TTFT、请求时延与并发结论。可用 `--tokenizer` 指定本地目录、HF 仓库或 `builtin`。

## 默认参数

- 并发档位 `1,4,8`（`--concurrency`，最多 8 档，1–1024）。按并发档展开，每档独立预热、独立测量。
- 每档 40 请求（`--request-count`，1–100000）；或用 `--duration 秒`（5–86400）按时长停止，两者只能选一个。
- 预热：每档 1 次、并发固定为 1（`--warmup`）。`--warmup 0` 关闭。预热只消冷启动，正式阶段才施加所选并发。
- 输入输出长度、端点为 chat + streaming、单请求超时 600 秒（`--timeout`）。不改服务端配置。

执行的命令形状：

```bash
uvx --from aiperf==0.13.0 aiperf profile \
  --url <去 /v1 的地址> --model <选中的模型> \
  --endpoint-type chat --streaming --tokenizer <词表> \
  --concurrency 1,4,8 --request-count 40 \
  --warmup-request-count 1 --warmup-concurrency 1 \
  --ui-type simple --artifact-dir <报告目录>/artifacts \
  --request-timeout-seconds 600 --auto-plot
```

子进程使用独立 `HOME`、独立 uv 缓存与 `MPLBACKEND=Agg`，不继承密钥、令牌、代理变量或 `PYTHONPATH`。

## 报告

默认 `output/reports/aiperf-benchmark-<UTC时间戳>-<随机后缀>/`（已被 `.gitignore` 忽略），可用 `--output` 指定父目录：

- `report.html`：离线自包含索引页，按并发档汇总 TTFT、请求时延、ITL、输出吞吐、请求吞吐，并列出全部原始产物与 PNG 图表。
- `artifacts/`：aiperf 原生产物，含 `profile_export_aiperf.csv`、`profile_export_aiperf.json`、`profile_export_aiperf_sweep.csv/json`、各并发档目录、`logs/` 与 PNG。
- `run.json`：接口、模型、词表来源与依据、并发档、预热设置、实际命令、退出码、产物清单。
- `run.log`：aiperf 原始输出。

汇总表优先取 sweep 汇总的均值；单档只有一次测量时，均值即该次结果。解析不到指标时报告明确写出“未解析到”，不补零、不推断。

## 失败与边界

- aiperf 失败或中断不自动重试，保留已生成产物，报告标注退出码，退出码原样返回给菜单。
- 某一档请求失败会体现在 aiperf 自己的错误统计里；本工具不改写这些数字。
- 预热与正式阶段共用同一合成语料生成器，可能命中服务端前缀缓存，使 TTFT 偏低。要测冷启动请重启服务后加 `--warmup 0`。
- 每档都会预热一次，模型在第一档之后已热，后续档的预热属于重复开销（约一次完整生成的时间），换来每档起点一致。
- PNG 图表依赖 matplotlib；无显示环境已通过 `MPLBACKEND=Agg` 规避，仍失败时报告只缺图，CSV/JSON 不受影响。
- 远程接口可能计费；不要把不可信地址填进来。本项不发送 Key，也不支持需要认证的接口。
