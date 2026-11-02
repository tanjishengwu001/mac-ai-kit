# 测试说明

从仓库根目录运行：

```bash
bash scripts/test.sh
```

需要已安装的 Bash 与 Python 3.10+（CI 使用 Python 3.12）。无需 pip、npm 或 pnpm 安装项目依赖。脚本检查所有 Shell 文件语法、Remote 单文件发行版与源码的一致性，并运行 `tests/` 内的 Python 回归测试。

## 完整 Remote 回归

```bash
bash scripts/test.sh --remote
```

另需已安装的 Node.js 24+ 与 Git。脚本先验证依赖，缺少依赖时退出码为 2，不会自动安装，也不会把跳过测试误报为完整通过。

Remote 测试包含模拟 macOS 启动器、临时 Git 仓库、本地回环 HTTP/TCP 服务及自制 Studio 夹具。不访问外部 Git，不执行真实 Remotion，不调用真实 launchctl，不下载模型，不执行实际应用安装或视频渲染。测试进程使用隔离 HOME 和临时目录，禁用全局 Git 配置，结束后清理自己创建的临时目录。测试失败返回非零状态。

在 macOS 上，系统临时目录可能以 `/var/folders` 表示，而其真实路径为 `/private/var/folders`。测试入口会先将自己的临时根目录转换为真实路径，避免夹具把这两个表示当成不同目录。终端交互测试在等待子进程退出时持续读取伪终端输出；macOS 在子进程退出后若拒绝读取伪终端属性，该项属性核对无法执行，其余按键、输出和退出码仍照常检查。

## 手工验收边界

离线回归不等于以下实机验收：

- macOS 原生 Bash 3.2 的真实安装与权限、签名、DMG 流程。
- 上游下载可用性、模型大文件断点续传、磁盘空间与文件系统行为。
- 模型首次加载、实际 API 推理、音视频质量与性能。
- AIPerf 并发压测的真实指标：离线回归只验证参数校验、命令拼装、产物解析与报告生成，不联网安装 aiperf，也不连接真实服务。
- 固定版本 Remotion 工程与真实渲染器的兼容性。

`.github/workflows/checks.yml` 为 Ubuntu/macOS 执行同一完整测试命令。GitHub 准备运行环境时可能下载 Python/Node；测试本身不安装应用或下载模型。该工作流需将仓库推送至 GitHub 并启用 Actions 后才会运行，随包提供配置不代表托管 CI 已通过。

离线核查记录见 [TEST_RESULTS.md](TEST_RESULTS.md)。
