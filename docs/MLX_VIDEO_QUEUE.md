# MLX Serve 视频队列

这是 Mac AI Kit 的独立常驻功能，不修改 MLX Serve，也不替换它的 API。先启动 MLX Serve 并准备对应的 MiniMax H3 模型包，再显式启动队列：

```bash
bash start.sh mlx-video-queue start --output-dir "$PWD/成片"
bash start.sh mlx-video-queue status
bash start.sh mlx-video-queue stop
```

也可从 `bash start.sh` → 第 6 项「服务管理」单独启动、查看状态或日志；“启动全部”“停止全部”“查看状态”均包含此队列。等价命令为 `bash start.sh services start --service mlx-video-queue` 与 `bash start.sh services status --service mlx-video-queue`；单独停止使用 `bash start.sh services stop --service mlx-video-queue`。两个入口复用同一默认状态目录和 launchd 标签，不创建第二个实例；菜单不控制 MLX-Serve 本体。自定义 `--state` 的实例仍用本页原命令管理。

需要 Python 3.10+、已安装的 FFmpeg、macOS 登录会话。也可以用 `run` 在前台运行。后台由当前用户的 launchd 管理，退出终端后继续运行，不设置登录自启。队列默认监听 `0.0.0.0:11235`，将视频任务转发给 `127.0.0.1:11234`。此接口没有客户端认证，仅限可信局域网或 VPN，切勿映射公网；若只需本机访问，启动时显式传入 `--host 127.0.0.1`，远程 Agent 可配合自己的 SSH 隧道。

`status` 显示 PID、实际保存的监听/上游端口、队列 API 健康与日志目录；加 `--json` 可输出只读快照。只查询 launchd 与 `/healthz`，不读取任务数据库、不发推理请求、不修改配置。健康不代表空闲；查询失败保留未知状态并返回非零。

用 `--port`、`--upstream-port`、`--ffmpeg` 可调整端口和程序路径。如果 MLX Serve 要求认证，加 `--upstream-key-file /绝对路径/密钥文件`，在该私有文件内放一行 Bearer token；不要把 token 放在命令行或提交到 Git。修改后台配置先 stop 再 start。默认状态目录为 `~/Library/Application Support/Mac AI Kit/mlx-video-queue`，包含 SQLite、转码临时文件以及日志。

## 按完成时间归档

启动时用 `--output-dir` 指定输出根目录；相对路径从启动命令所在目录解析。省略时保存到状态目录的 `results` 下。按本机时间归档为：

```text
输出根目录/2026-11-25/23/234015.123456-<任务ID>.mp4
```

目录按日期和小时分组，文件名记录时分秒、微秒和任务 ID。成功状态返回 `output_path` 和 `video_url`。已经归档的任务仍使用原来的路径，重启后更换输出根目录只影响新出片。建议将外置盘挂载好再启动；服务以启动时提供的目录为准。

## 提交 → 查询 → 取回

```bash
BASE=http://127.0.0.1:11235
curl -sS --fail-with-body "$BASE/v1/video/generations" \
  -H 'Content-Type: application/json' --data-binary @h3-request.json

# 将 <id> 换成提交返回的 id；成功后按响应的 video_url 取回。
curl -sS --fail-with-body "$BASE/v1/video/jobs/<id>"
curl -sS --fail-with-body "$BASE/v1/video/jobs/<id>/video.mp4" -o shot.mp4
```

提交返回 HTTP 202 和任务 ID。查询状态：`queued`、`running`、`succeeded`、`failed`。未成功时下载返回 409；成功时返回二进制 `video/mp4`，不是 Base64 或 RGB。用 `DELETE /v1/video/jobs/<id>` 移除排队任务或清理结果（正在生成的任务不可删除）。默认最多 32 个活动任务、256 条保留记录；满时返回 429。发布端不应在提交应答丢失后盲目重试。

### 可恢复的客户端监控

`scripts/mlx_video_watch.py` 接受任务 ID 或包含 `id` 的提交响应 JSON，低频查询已提交的任务，成功后自动下载 MP4 和保存状态记录；不提交、不重复生成。示例：

```bash
python3 scripts/mlx_video_watch.py --base http://127.0.0.1:11235 \
  --job-id 任务ID --output-dir ./审片 --interval 60
```

也可用 `--receipt 提交响应.json` 代替 `--job-id`，使用 `--once` 单次查询。连续模式默认每 60 秒查一次，网络异常时退避并增加随机抖动。脚本退出或客户端断线不取消服务端任务，之后重新运行可恢复查询；客户端进程退出后不会自动下载，除非重新启动或由进程管理器保持运行。升级后的队列将上游提供的进度字段保存在 `progress` 中；没有上游数据时为 `null`，不估算百分比。推理进度不表示视频编码、归档已完成，必须以 `succeeded` 为完成标志。此脚本适用于本队列的任务协议，不代表适用于其他厂商的异步 API。

### 批量监控与自动归档

每个镜头的每个版本独立建目录，把该次提交成功后 **HTTP 202 返回的完整 JSON** 保存为该目录的 `提交响应.json`；请求、提示词和参考素材也保存在同一目录。提交仍逐个进行，监控脚本不负责提交，也不会重发。提交响应丢失时不能猜测 ID 或盲目重发。

```text
镜头目录/
  镜头A/v01/提交响应.json
  镜头B/v02/提交响应.json
```

给同一个队列提交的多个任务使用目录发现模式；持续运行并发现后续新建的版本目录：

```bash
python3 scripts/mlx_video_batch_watch.py --watch-dir /镜头目录 \
  --base http://127.0.0.1:11235 --interval 60 --follow
```

也可用 JSON 清单明确绑定地址与位置。清单路径、`receipt` 和 `output_dir` 可使用相对于清单文件所在目录的路径，`id` 与 `receipt` 每个任务只填一种；单条任务也适用：

```json
{
  "version": 1,
  "base": "http://127.0.0.1:11235",
  "jobs": [
    {"receipt": "镜头A/v01/提交响应.json", "output_dir": "镜头A/v01"},
    {"id": "0123456789abcdef0123456789abcdef", "output_dir": "镜头B/v02"}
  ]
}
```

```bash
python3 scripts/mlx_video_batch_watch.py --manifest /路径/jobs.json --interval 60 --follow
```

清单中的任务可各自指定 `base`，覆盖清单的默认 `base`。不加 `--follow` 时，现有任务全部结束或失败后退出；`--once` 只检查一次。不加 `--once` 的连续模式下，网络错误会退避重试；终态失败及文件冲突需人工检查后重新运行。进程停止或客户端断线不取消服务端任务，重新运行即可恢复；要在客户端退出后自动下载，须用客户端的进程管理器保持监控进程运行，不能只依赖服务端队列。

只从任务自身接口下载，结果写入该任务指定目录，文件名为 `<任务ID>.mp4`，旁边保存 `<任务ID>.mp4.job.json`（服务地址、ID、字节数、SHA-256）和状态日志。写入临时文件并校验长度后，无覆盖地发布；重启后先核对来源和哈希，匹配则跳过下载。已有文件缺少记录、校验不符、同一任务被绑定多个目录时拒绝覆盖或混淆，不影响其他有效任务。旧版单任务脚本下载的文件没有本记录，批量模式不会自动认领；可用清单指定一个新的空目录接收。日志只表示观察到的状态，实际视频内容仍需审片。

### 两种 H3 请求

复用 MLX Serve 原生 JSON。用 MLX Serve 的模型 ID 填写 `model`，下方例子的 ID/媒体内容需替换为实际值。视频的宽、高、帧数、种子、步数等可按模型包能力填写。

| 模式 | 原生字段 |
| --- | --- |
| **FL2VA（首／尾帧）** | `{"model":"<FL2VA ID>","prompt":"…","first_frame_image":"<Base64>","last_frame_image":"<Base64>"}`。尾帧可省。 |
| **Ref2VA（多模态参考）** | `{"model":"<Ref2VA ID>","prompt":"…","ref_images":["<Base64>"]}`。可选 `ref_videos`（元素为 `{"frames":[…],"audio":"…"}`，音轨可省）和 `ref_audios`。 |

两组字段不要混用。队列强制内部使用 SSE、关闭过程预览，不接受服务端 LoRA 路径。它不模拟 MiniMax 云端 API，不替用户切换或卸载模型。尺寸、时长及参考数量仍由所选 MLX Serve 版本和模型包校验。

### 编码与恢复

每个任务串行走完生成和压缩。RGB 从 SSE 分块解码至临时文件，不在 Python 内存里拼一整条 Base64；FFmpeg 优先走苹果 VideoToolbox（`h264_videotoolbox`、关闭隐式软件回退，默认目标码率 8 Mb/s）。硬编失败则用 `libx264`，有上游 PCM 音轨就编码为 AAC。结果写入完成后才标记成功；任务状态会标明实际编码器。

**这是下游压缩，不改变 MLX Serve → 队列之间的原始 RGB 协议。** 例如 MLX Serve v26.11.3 的 H3 在生成前执行单次 768 MiB RGB 上限；包装层不能靠 MP4 绕过它。遇到上游拒绝，减少该镜头的帧数、分辨率或链式窗口。请为单个任务的原始 RGB、PCM 和输出预留磁盘空间。

SQLite 保存排队任务；重启后继续未开始的任务。中断的运行中任务标为失败，不自动重复生成；若需要则重新提交。任务结束时清除队列记录中的原请求；已完成 MP4 留存到主动删除。默认不改变 MLX Serve 自身的服务状态。

上游协议实现：[MLX Serve v26.11.3](https://github.com/ddalcu/mlx-serve/tree/v26.11.3)。离线回归通过模拟上游覆盖协议和队列；VideoToolbox 与 launchd 行为须在 macOS 实机确认。
