# WeeToddCLI 安装实机验证

## 范围与结论

2026-11-27，在真实 Apple Silicon Mac（`arm64`，硬件标识 `Mac17,15`）执行本仓库安装器，完成源码下载、完整 App 构建、CLI 配置及重复安装验证。

**安装与 CLI 配置通过；模型兼容、任务预检、视频生成及播放验收均未执行。** 未扫描用户模型盘、未下载/转换模型、未打开 Studio、未启动推理服务。

## 实际环境

- macOS `27.0.1`，build `26A434`。
- Xcode `27.2`，build `27B52`；Swift `6.4.2`；macOS SDK `27.2`。
- Metal Toolchain `27B47`，`metal --version` 输出 `32023.922`。
- FFmpeg/FFprobe 复用现有 `/opt/homebrew/bin/ffmpeg`、`/opt/homebrew/bin/ffprobe`，没有重装。
- 上游源码提交：`b398fcc52a4cb9aaf91ed0739feda9f8620d14f8`，下载后及安装后均用 Git 核对。

初始环境检查确认缺少完整版 Xcode；用户安装 Xcode 后，上游构建探测明确报告缺 Metal。通过所选 Xcode 的官方 `xcodebuild -downloadComponent MetalToolchain` 完成组件安装，随后重跑安装器成功。未执行 `xcode-select --switch`，未绕过许可、系统权限或签名验证。

## 已实际执行且通过

```bash
bash start.sh weetodd install --yes --xcode /Applications/Xcode.app
bash start.sh weetodd doctor
bash start.sh weetodd cli -- models --help
"$HOME/AI/bin/weetodd" --help
bash start.sh weetodd install --yes
```

- 第一次成功安装调用固定版本上游构建脚本，使用独立 `--output`，保留完整 App 和 workers/资源。
- `doctor` 通过 App 深度严格签名检查、H3 MLX worker manifest/SHA256、共享 Metal 库存在性、CLI 帮助及媒体工具可执行检查。
- 两种 CLI 入口都返回真实上游帮助，显示 `--job`、`--output-directory`、`--preflight-only`、`--resume`、worker 覆盖及模型管理子命令。只查看帮助，没有运行这些模型/任务操作。
- 重复安装复用已配置 App，不重新构建，不覆盖模型或外部程序。
- 上游构建提示 `--build-system native` 将来会弃用，以及本机构建采用 ad-hoc 签名。这些是上游警告，不应隐瞒为无警告构建；本次包签名验证已通过。

默认 CLI 是 `~/AI/bin/weetodd`，路径配置为 `~/AI/apps/weetodd-studio/config.json`。真实构建、安装检查和 CLI 帮助日志保留在该安装根的 `logs/build-install.log`、`logs/doctor.log`、`logs/models-help.log`，不将含本机路径的原始构建日志提交到 Git。

## 仓库验证

在独立 worktree 中，使用 Python 3.12 和 Node 24+ 执行 `bash scripts/test.sh --remote`：

- 工具包 Python 回归：691 项，成功，2 项跳过。
- Remote Python 回归：22 项，成功。
- Node 回归：15 项通过，0 项失败。
- 新增 WeeTodd 专项：15 项通过；覆盖只读预览、完整 App 复用、路径带空格、幂等、缺失 Xcode、固定源码提交、外部文件保留、worker 损坏、CLI 参数/失败退出码、符号链接及卷路径大小写保护。
- Shell 语法、Remote 打包一致性、`git diff --check` 通过；文档 shell 示例只做 `bash -n`，JSON 片段只做解析，不执行示例。
- 独立代码审查发现并修复最终 App 符号链接与卷目录大小写保护遗漏；复审通过。

宿主 PATH 暴露已有 Hermes 时，旧安装预览测试的“未安装 Hermes”断言会失败。最终测试使用仅包含 Python、Node、Homebrew 和系统工具的隔离 PATH 通过，未为此修改既有测试。这是测试环境区别，不是模型运行证据。

## 未做事项

没有运行 job `--preflight-only`、`--resume`、任何模型下载/准备/转换、H3 两阶段生成、ffprobe 成片探测或播放验收。没有生成 MP4，因此也没有生成耗时、推理内存峰值或画质结论。潜空间放大使用方法见 [WEETODD_CLI.md](WEETODD_CLI.md)。
