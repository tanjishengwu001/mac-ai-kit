# Mac AI Kit · 本地 AI 创作工坊

面向 Apple Silicon Mac：安装本地 AI 工具、管理模型、诊断环境，并按需管理服务。

## 开始使用

克隆项目后进入 `mac-ai-kit/`，运行：

```bash
bash start.sh
```

使用 ↑↓ 选择、Enter/→ 确认、Esc/← 直接返回（无需回车），`h` 查看帮助，`0/q` 退出。误用 `zsh start.sh` 也会转交 Bash。打开菜单不会安装软件或启动服务。

## 六个入口

1. **安装运行工具**：准备 Homebrew、FFmpeg、uv、fnm/Node 等基础工具，或选择 MLX-Serve、llama.cpp、llama-swap、sd.cpp、audio.cpp、Hermes。先展示计划，再确认安装；系统授权与可选组件另行提示。
2. **管理本地模型**：选择、下载、查看模型，设置公共模型目录，检查 H3 共享文件。已有完整文件不重复下载。
3. **配置 Remotion 远程渲染**：在指定的已有工程中保存配置；安装不会顺带启动渲染。
4. **环境诊断与 API 说明**：检查工具状态，查看或更新模型目录下的 `API.md`。这是调用说明，不是新建 API 服务。
5. **Benchmark**：Llama benchmark 用于通用接口测试，MLX benchmark 使用原生计时，两者生成同版 HTML 报告；AIPerf 并发压力测试测量多并发下的延迟、吞吐与错误率。
   三项都只测试已启动的无认证接口，不安装、不启停服务、不下载权重。报告默认保存到项目内 `output/reports/测试名称-时间戳-随机后缀/`，已由 `.gitignore` 忽略。
6. **服务管理**：可选择“启动全部”“仅启动音频”或“仅启动制图”，以及关闭、查看状态与日志，不经过 llama-swap。SD.cpp 可在同一 WebUI / API 切换 Qwen / Krea；audio.cpp 提供转写／YuE2；llama-swap 保留为可选入口。安装工具不等于服务已启动。

## 文档

[安装](docs/INSTALL.md) · [模型](docs/MODELS.md) · [共享目录](docs/SHARED_MODELS.md) · [API 说明](docs/API.md) · [独立服务管理](docs/NATIVE_SERVICES.md) · [llama-swap（可选）](docs/services.md) · [Benchmark](docs/BENCHMARK.md) · [MLX 高上下文测试](docs/MLX_BENCHMARK.md) · [AIPerf 并发压测](docs/AIPERF_BENCHMARK.md) · [Remote](docs/REMOTE.md) · [外置盘恢复](docs/DOWNLOAD_RECOVERY.md)

## 测试与贡献

```bash
bash scripts/test.sh
bash scripts/test.sh --remote
```

测试使用隔离模拟，不安装软件、不下载权重、不启动真实推理服务。完整测试另需 Node.js 24+ 与 Git。离线测试不能代替 Mac 实机验收。

## 安全与许可

服务仅在明确操作后启动；不要把无认证接口暴露公网。模型配置中的启动命令必须可信。工具包不会自动统一各后端 API，也不承诺未知模型或异步任务的内存调度安全。

本项目代码为 [MIT](LICENSE)，第三方程序、模型和资产各自遵循上游许可，见 [第三方说明](THIRD_PARTY_NOTICES.md)。不要关闭 Gatekeeper、TLS 或系统安全设置来绕过错误。[安全说明](SECURITY.md) · [变更记录](CHANGELOG.md)

