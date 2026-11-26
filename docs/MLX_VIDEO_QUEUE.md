# MLX Serve 视频队列

这是 Mac AI Kit 的独立常驻功能，不修改 MLX Serve，也不替换它的 API。先启动 MLX Serve 并准备对应的 MiniMax H3 模型包，再显式启动队列：

```bash
bash start.sh mlx-video-queue start --output-dir "$PWD/成片"
bash start.sh mlx-video-queue status
bash start.sh mlx-video-queue stop
```

需要 Python 3.10+、已安装的 FFmpeg、macOS 登录会话。也可以用 `run` 在前台运行。后台由当前用户的 launchd 管理，退出终端后继续运行，不设置登录自启。队列只监听 `127.0.0.1:11235`，默认将视频任务转发给 `127.0.0.1:11234`。使用远程 Agent 时用自己的 SSH 隧道，不要将这个没有客户端 API 密钥的服务直接暴露给局域网或公网。

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
