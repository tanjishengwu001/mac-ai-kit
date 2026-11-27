# 第三方软件与模型说明

## 许可范围

根目录及 `scripts/` 中的 MIT 许可证覆盖本项目自行编写的代码与文档。保留原有 Lean AI Kit contributors 署名，并记录 Mac AI Kit contributors 的后续贡献；改名不改变第三方权利。

本仓库提供下载和配置入口，不因此拥有上游软件、模型权重、数据、商标或附件。上游内容无论通过安装器获取、单独下载还是由用户自行提供，都继续受各自许可证约束。这里是来源索引和使用边界说明，不是第三方完整法律文件合集，也不是商用合规保证。

## 软件来源

下列来源与仓库中的安装器或文档一致；实际使用版本和校验值以对应脚本为准。使用或再分发时，请阅读该版本随附的 LICENSE、NOTICE 与依赖声明。

- [MLX-Serve](https://github.com/ddalcu/mlx-serve)：App 及内置/独立 CLI。
- [WeeTodd Studio](https://github.com/wee-todd/WeeTodd-Studio)：从固定提交构建完整 App、WeeToddCLI 及原生 workers，保留上游许可、依赖锁与包内 Notices；本仓库不捆绑其二进制或模型。H3 社区潜变量放大器等权重另受各自许可约束。
- [llama.cpp](https://github.com/ggml-org/llama.cpp)：GGUF 推理后端。
- [llama-swap](https://github.com/mostlygeek/llama-swap)：后端启停及代理。
- [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp)：图像与视频后端。
- [audio.cpp](https://github.com/0xShug0/audio.cpp)：音频后端。
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)：可选 CLI 安装组件。
- [ModelScope Hub](https://github.com/modelscope/modelscope_hub)：模型下载客户端。
- [uv](https://github.com/astral-sh/uv)、[Homebrew](https://github.com/Homebrew/brew)、[Python](https://www.python.org/)：按任务需要准备的基础工具。
- [Remotion](https://github.com/remotion-dev/remotion)、[Node.js](https://nodejs.org/)、[pnpm](https://pnpm.io/)、[Git](https://git-scm.com/)：可选 Remote 所依赖的现有工程及工具。Remotion 的适用许可和商业使用条件需单独检查，不能套用本工具包的 MIT 许可。

上述项目及其依赖并不因被列出而成为本项目 MIT 授权的一部分。安装包可能含更多第三方库；实际再分发二进制时须另行核对完整依赖及声明，本索引不能代替该工作。

## Benchmark 依赖

[llama-benchy 0.4.0](https://pypi.org/project/llama-benchy/0.4.0/)（MIT）由 uvx 在用户确认后按需取得，本包不捆绑其源码或 wheel。自有严格入口使用该固定版内部接口，并依赖 tokenizers / huggingface_hub 读取数据 JSON，不执行模型仓库代码。其依赖与 tokenizer 数据各自遵守上游许可。

## 模型、转换包与必要附件

具体模型和组件记录在 [catalog.json](catalog.json) 的 `license`、`license_url`、`license_notice` 等字段中；来源及格式说明见 [模型文档](docs/MODELS.md)。这些是仓库维护时的记录，不代替你下载或使用时取得的上游许可正文。

当前清单包含 Apache 类许可、非商业许可及自定义研究/社区许可，不能整体称为“自由商用开源模型”。尤其应核对：

- Qwen-Image 的研究/商用条件。
- Krea、MiniMax H3、MiniMax Music3 等自定义许可中的地域、用途、收入、署名或托管条件。
- YuE2 权重的非商业要求。
- 量化与转换包的原模型许可、转换方声明，以及独立编码器、VAE 和其他附件的许可。

镜像可下载、格式转换、量化或修改文件名均不会解除上游限制。模型输出的权利和使用条件也不由本项目 MIT 许可证统一授权。工具不会替用户接受平台受限条款、办理许可或判断所在地合规。
