#!/bin/bash
# mac-ai-kit: bash-entry
# 主菜单入口（Remote 亦可独立运行）；兼容 macOS 自带 Bash 3.2，无需预装 Python。
# 必须先判断 source，再切换解释器；不能替换用户当前的交互式 shell。
# 这里仅使用各 shell 都能解析的语法，且不修改变量、选项或信号处理。
# Bash 的 BASH_SOURCE 是当前脚本身份；仅有继承的版本变量不代表解释器。
if [ -n "${BASH_VERSION:-}" ] && [ -n "${BASH_SOURCE:-}" ]; then
  if [ "${BASH_SOURCE:-}" != "$0" ]; then
    printf '%s\n' '请不要 source 此脚本；请运行 bash start.sh。' >&2
    return 2
  fi
elif [ -n "${ZSH_VERSION:-}" ]; then
  case "${ZSH_EVAL_CONTEXT:-}" in
    *:file) printf '%s\n' '请不要 source 此脚本；请运行 bash start.sh。' >&2; return 2 ;;
  esac
else
  # POSIX sh 没有来源变量：仅当 $0 确认是本入口时才重新执行。
  # 从 shell 或其他脚本用点命令加载时，礼貌返回，不替换父进程。
  if [ "$(sed -n '2p' "$0" 2>/dev/null)" != '# mac-ai-kit: bash-entry' ]; then
    printf '%s\n' '请不要 source 此脚本；请运行 bash start.sh。' >&2
    return 2
  fi
fi
if [ -z "${BASH_VERSION:-}" ] || [ -z "${BASH_SOURCE:-}" ]; then
  # 与用户输入 bash start.sh 使用同一 PATH 解释器，避免强制切到另一版本。
  # 清理 zsh 导出的身份变量，防止 Bash 继承后反复转交自己。
  if command -v bash >/dev/null 2>&1; then
    exec /usr/bin/env -u ZSH_VERSION -u ZSH_EVAL_CONTEXT bash "$0" "$@"
  else
    exec /usr/bin/env -u ZSH_VERSION -u ZSH_EVAL_CONTEXT /bin/bash "$0" "$@"
  fi
fi
set -o pipefail
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)" || exit 1
SCRIPTS="$ROOT/scripts"
. "$SCRIPTS/path_validation.sh"
. "$SCRIPTS/model_directory.sh"
PYTHON=''
load_model_directory || exit $?
trap 'printf "\n已中断。\n"; exit 130' INT
# 更新解压目录可能使旧终端仍停在已删除的 inode；每轮检查并恢复失效目录。
ensure_project_directory() {
  if [ ! -d "$SCRIPTS" ]; then
    printf '%s\n' '工具包目录已被移动或替换，请从新目录重新运行 bash start.sh。' >&2
    return 2
  fi
  # 有效目录保持不变，保留用户相对路径的原始含义；失效时才恢复。
  if ! pwd -P >/dev/null 2>&1; then
    cd -- "$ROOT" 2>/dev/null || return 2
  fi
}
say() { printf '%s\n' "$*"; }
refresh_paths() {
  # 仅刷新本进程，不 source 用户配置，也不执行 brew shellenv / eval。
  local folder
  for folder in "$HOME/.cargo/bin" "$HOME/.local/bin" /opt/homebrew/sbin /opt/homebrew/bin "$HOME/AI/bin"; do
    if [ -d "$folder" ]; then
      case ":$PATH:" in *":$folder:"*) ;; *) PATH="$folder:$PATH" ;; esac
    fi
  done
  export PATH
  hash -r
}
find_python() {
  local candidate
  PYTHON=''
  candidate="$(command -v python3 2>/dev/null)"
  if [ -n "$candidate" ] && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON="$candidate"; return 0
  fi
  if command -v uv >/dev/null 2>&1; then
    candidate="$(uv python find --no-python-downloads 3.12 2>/dev/null)"
    if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      PYTHON="$candidate"; return 0
    fi
  fi
  return 1
}
install_env() {
  /bin/bash "$SCRIPTS/setup_mac_environment.sh" --install "$@"
  local status=$?
  refresh_paths
  return "$status"
}
ensure_runtime() {
  refresh_paths
  if find_python && command -v uv >/dev/null 2>&1; then return 0; fi
  say '需要先准备 Python / uv，接下来进入基础环境安装，完成后自动继续。'
  install_env || return $?
  if ! find_python || ! command -v uv >/dev/null 2>&1; then
    say '基础环境尚未就绪（可能已取消）。本次停止，可从菜单重试。' >&2; return 1
  fi
}
update_api() {
  local directory="$1"
  shift
  trim_path_input "$directory"; directory="$TRIMMED_PATH"
  case "$directory" in "~") directory="$HOME" ;; "~/"*) directory="$HOME/${directory#\~/}" ;; esac
  validate_model_root_shell "$directory" || return $?
  model_volume_ready "$directory" || return $?
  find_python || { say '未找到 Python 3.10+，无法生成 API.md；不会自动安装。' >&2; return 1; }
  "$PYTHON" "$SCRIPTS/api_inventory.py" --directory "$directory" "$@"
}
api_menu() {
  say "API 说明文档：$MODEL_DIR/API.md（不启动服务）"
  update_api "$MODEL_DIR"
}
find_client() {
  CLIENT="$(command -v ms-hub 2>/dev/null)"
  [ -z "$CLIENT" ] || return 0
  local folder
  if command -v uv >/dev/null 2>&1; then
    folder="$(uv tool dir --bin 2>/dev/null)"
    if [ -n "$folder" ] && [ -x "$folder/ms-hub" ]; then CLIENT="$folder/ms-hub"; return 0; fi
  fi
  if [ -x "$ROOT/.venv-download/bin/ms-hub" ]; then CLIENT="$ROOT/.venv-download/bin/ms-hub"; return 0; fi
  return 1
}
ensure_client() {
  find_client && return 0
  say '尚未找到下载客户端，接下来按需安装。'
  /bin/bash "$SCRIPTS/bootstrap_download_env.sh" || return $?
  refresh_paths
  find_client || { say '下载客户端未就绪（可能已取消），未开始下载模型。' >&2; return 1; }
}
usage() {
  cat <<'HELP'
Mac AI Kit · 本地 AI 创作工坊
在终端进入工具包目录，运行 bash start.sh 打开菜单。
高级用法：bash start.sh [任务] [选项]
  env                 基础环境预览；--install 安装
  capabilities        推理与生成工具预览；--install 选择安装
  download-client     下载客户端预览；--install 安装
  models              按大模型 / 音频 / 图像视频列出模型
  installed-models    查看已下载模型、文件体积与合并建议（只读）
  download            选择模型后下载；缺依赖自动进入安装
  check               检查基础环境，不安装
  check-tools         查看工具安装、配置及进程状态（只读）
  api [--directory 目录] 更新 API.md 文档，默认使用已保存的模型目录
  services            全部 / 仅音频 / 仅制图启动、状态与日志；llama-swap 可选
                      start / stop / status [--service all|sd-qwen|audio]
                      status --json（只读快照，unknown 不是空闲）
                      logs --service audio --follow（Ctrl+C 仅退出查看）
  benchmark           Llama benchmark（通用接口测试）
  mlx-benchmark       MLX-Serve 长上下文 benchmark（0 / 50K / 100K / 200K）
  aiperf-benchmark    AIPerf 多并发压力测试（延迟、吞吐与错误率）
  remote              配置 Remotion Remote（仅写配置，不启动服务）
                      可选：--project 已有工程目录
  merge-h3            自动校验并合并当前模型目录的 H3 共享权重
  script-help 任务     查看内部任务帮助
选项：--preview / --dry-run，--details，--select ID列表，
      --models-dir 目录（仅覆盖本次任务；菜单「设置模型目录」会持久保存）
      download --reuse-mtp-source 旧mtp目录（显式只读复用，允许旧路径空白）
无参数显示菜单。不会默认下载全部模型或自动启动 Docker / API。
HELP
}
choose_directory() {
  local value
  printf '模型目录 [%s]（输入 q 取消）：' "$MODEL_DIR"
  IFS= read -r value || return 1
  trim_path_input "$value"
  value="$TRIMMED_PATH"
  [ "$value" != q ] || return 1
  [ -n "$value" ] || value="$MODEL_DIR"
  case "$value" in '~') value="$HOME" ;; '~/'*) value="$HOME/${value#\~/}" ;; esac
  case "$value" in /*) ;; *) value="$PWD/$value" ;; esac
  case "$value" in /|*$'\n'*|*$'\r'*) say '目录无效。'; return 1 ;; esac
  validate_model_root_shell "$value" || return $?
  model_volume_ready "$value" || return $?
  save_model_directory "$value" || return $?
  MODEL_DIR="$value"
  DIRECTORY_CHOSEN=1
  say "已保存模型目录：$MODEL_DIR"
}
prepare_model_directory() {
  validate_model_root_shell "$1" || return $?
  "$PYTHON" - "$1" "$SCRIPTS" <<'PYDIR'
import os
from pathlib import Path
import sys
try:
    sys.path.insert(0, sys.argv[2])
    from path_validation import validate_model_root
    path = validate_model_root(sys.argv[1])
    if path.resolve() == Path('/'):
        raise ValueError('不能使用文件系统根目录')
    if '..' in path.parts:
        raise ValueError('目录不能包含 ..')
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('目录或上级是符号链接，请选择真实目录')
    if path.parts[:2] == ('/', 'Volumes'):
        if len(path.parts) < 3 or not os.path.ismount(str(Path('/Volumes') / path.parts[2])):
            raise ValueError('外置磁盘未挂载，不会创建同名假挂载目录')
    path.mkdir(parents=True, exist_ok=True)
except (OSError, ValueError) as exc:
    print('模型目录未就绪：' + str(exc), file=sys.stderr)
    sys.exit(1)
PYDIR
}
configure_remote() {
  local launcher="$ROOT/remote/start-remotion-remote.sh" args=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --project) [ "$#" -ge 2 ] && [ -n "$2" ] || return 2; args+=(--project "$2"); shift 2 ;;
      -h|--help) say '用法：bash start.sh remote [--project 已有工程目录]（仅保存配置，不启动服务）'; return 0 ;;
      *) say "配置入口不支持此参数：$1" >&2; return 2 ;;
    esac
  done
  [ -f "$launcher" ] || { say 'Remote 模块文件缺失，请重新获取完整工具包。' >&2; return 1; }
  /bin/bash "$launcher" --configure "${args[@]}"
}
merge_h3() {
  model_directory_for_args "$@" || return $?
  find_python || { say '合并检查需要 Python 3.10+，请先准备基础环境。' >&2; return 1; }
  "$PYTHON" "$SCRIPTS/merge_h3_models.py" --models-dir "$MODEL_DIR" "$@"
}
benchmark() {
  model_directory_for_args "$@" || return $?
  find_python || { say 'Benchmark 需要 Python 3.10+，请先准备基础环境；不会自动安装。' >&2; return 1; }
  "$PYTHON" "$SCRIPTS/llama_benchmark.py" --models-dir "$MODEL_DIR" "$@"
}
mlx_benchmark() {
  model_directory_for_args "$@" || return $?
  find_python || { say 'Benchmark 需要 Python 3.10+，请先准备基础环境；不会自动安装。' >&2; return 1; }
  "$PYTHON" "$SCRIPTS/mlx_benchmark.py" --models-dir "$MODEL_DIR" "$@"
}
aiperf_benchmark() {
  model_directory_for_args "$@" || return $?
  find_python || { say 'Benchmark 需要 Python 3.10+，请先准备基础环境；不会自动安装。' >&2; return 1; }
  "$PYTHON" "$SCRIPTS/aiperf_benchmark.py" --models-dir "$MODEL_DIR" "$@"
}
services() {
  find_python || { say '服务管理需要 Python 3.10+，请先准备基础环境；不会自动安装。' >&2; return 1; }
  "$PYTHON" "$SCRIPTS/native_services.py" --models-dir "$MODEL_DIR" "$@"
}
check_tools() {
  find_python || { say '工具状态检查需要 Python 3.10+，请先准备基础环境。' >&2; return 1; }
  "$PYTHON" "$SCRIPTS/check_tools.py" --models-dir "$MODEL_DIR" "$@"
}
run_task() {
  ensure_project_directory || return $?
  local task="${1:---help}" install=0 dry=0 details=0 select='' directory='' mode_seen=0 reuse='' reuse_mtp=''
  shift
  case "$task" in api)
    local api_directory="$MODEL_DIR"
    if [ "$#" -eq 1 ] && { [ "$1" = --help ] || [ "$1" = -h ]; }; then
      say 'bash start.sh api [--directory 安装根目录或模型根目录]（离线检查，不安装、不启动服务）'; return 0
    fi
    if [ "$#" -gt 0 ]; then
      [ "$#" -eq 2 ] && [ "$1" = --directory ] && [ -n "$2" ] || { say '用法：bash start.sh api [--directory 目录]' >&2; return 2; }
      api_directory="$2"
    fi
    update_api "$api_directory"; return $? ;;
    installed-models)
      model_directory_for_args "$@" || return $?
      find_python || { say '未找到 Python 3.10+，无法检查模型；不会自动安装。' >&2; return 1; }
      "$PYTHON" "$SCRIPTS/model_inventory.py" --models-dir "$MODEL_DIR" "$@"; return $? ;;
    services) services "$@"; return $? ;;
    benchmark) benchmark "$@"; return $? ;;
    mlx-benchmark) mlx_benchmark "$@"; return $? ;;
    aiperf-benchmark) aiperf_benchmark "$@"; return $? ;;
    remote) configure_remote "$@"; return $? ;;
    merge-h3) merge_h3 "$@"; return $? ;;
    check-tools) check_tools "$@"; return $? ;; -h|--help) usage; return 0 ;; script-help)
    [ "$#" -eq 1 ] || { usage >&2; return 2; }
    case "$1" in
      benchmark) benchmark --help ;;
      mlx-benchmark) mlx_benchmark --help ;;
      aiperf-benchmark) aiperf_benchmark --help ;;
      remote) configure_remote --help ;;
      env) /bin/bash "$SCRIPTS/setup_mac_environment.sh" --help ;;
      capabilities) /bin/bash "$SCRIPTS/setup_ai_capabilities.sh" --help ;;
      download-client) /bin/bash "$SCRIPTS/bootstrap_download_env.sh" --help ;;
      download) find_python || { say '查看下载器高级帮助需先在菜单准备基础环境。' >&2; return 1; }; "$PYTHON" "$SCRIPTS/download_models.py" --help ;;
      *) return 2 ;;
    esac
    return $? ;;
    env|capabilities|download-client|models|download|check) ;; *) say "未知任务：$task" >&2; return 2 ;;
  esac
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --install) [ "$mode_seen" -eq 0 ] || return 2; mode_seen=1; install=1 ;;
      --preview|--dry-run) [ "$mode_seen" -eq 0 ] || return 2; mode_seen=1; dry=1 ;;
      --details) details=1 ;;
      --select|--models-dir|--reuse-from|--reuse-mtp-source)
        [ "$#" -ge 2 ] && [ -n "$2" ] || { say "缺少参数值：$1" >&2; return 2; }
        case "$1" in --select) select="$2" ;; --models-dir) trim_path_input "$2"; directory="$TRIMMED_PATH"; [ -n "$directory" ] || { say '模型目录不能为空。' >&2; return 2; } ;; --reuse-from) reuse="$2" ;; --reuse-mtp-source) reuse_mtp="$2" ;; esac
        shift ;;
      -h|--help) usage; return 0 ;;
      *) say "未知参数：$1" >&2; return 2 ;;
    esac
    shift
  done
  [ -z "$reuse_mtp" ] || [ "$task" = download ] || { say '--reuse-mtp-source 仅用于下载。' >&2; return 2; }
  [ -z "$reuse" ] || [ "$task" = download ] || { say '--reuse-from 仅用于下载。' >&2; return 2; }
  case "$task" in models|download|check) [ "$install" -eq 0 ] || return 2 ;; esac
  case "$task" in env|download-client|check) [ -z "$select$directory" ] || return 2 ;; models) [ -z "$select" ] || return 2 ;; esac
  case "$task" in capabilities|models|download) [ -n "$directory" ] || directory="$MODEL_DIR" ;; esac
  if [ -n "$directory" ]; then
    case "$directory" in '~') directory="$HOME" ;; '~/'*) directory="$HOME/${directory#\~/}" ;; esac
    case "$directory" in /*) ;; *) directory="$PWD/$directory" ;; esac
    case "$directory" in /|*$'\n'*|*$'\r'*) say '模型目录无效。' >&2; return 2 ;; esac
  fi
  [ -z "$directory" ] || validate_model_root_shell "$directory" || return $?
  [ -z "$directory" ] || model_volume_ready "$directory" || return $?
  if [ -n "$reuse" ]; then
    case "$reuse" in '~') reuse="$HOME" ;; '~/'*) reuse="$HOME/${reuse#\~/}" ;; esac
    case "$reuse" in /*) ;; *) reuse="$PWD/$reuse" ;; esac
    case "$reuse" in /|*$'\n'*|*$'\r'*) say '复用来源目录无效。' >&2; return 2 ;; esac
    [ -d "$reuse" ] || { say '复用来源目录不存在。' >&2; return 2; }
  fi
  local args=()
  [ "$details" -eq 0 ] || args+=(--details)
  case "$task" in
    env|check)
      if [ "$install" -eq 1 ]; then install_env "${args[@]}"; else /bin/bash "$SCRIPTS/setup_mac_environment.sh" --dry-run "${args[@]}"; fi ;;
    capabilities)
      if [ "$install" -eq 1 ]; then
        # 先选组件；取消 / 无效输入不准备环境、不创建模型目录。
        local choose_args=(--confirm-only)
        [ "$details" -eq 0 ] || choose_args+=(--details)
        [ -z "$directory" ] || choose_args+=(--models-dir "$directory")
        [ -z "$select" ] || choose_args+=(--select "$select")
        select="$(/bin/bash "$SCRIPTS/setup_ai_capabilities.sh" "${choose_args[@]}")" || return $?
        [ -n "$select" ] || return 0
        args+=(--select "$select")
        ensure_runtime || return $?
        if [ -n "$directory" ]; then prepare_model_directory "$directory" || return $?; fi
        args+=(--apply --yes --quiet-plan)
      else
        args+=(--dry-run)
        [ -z "$select" ] || args+=(--select "$select")
      fi
      [ -z "$directory" ] || args+=(--models-dir "$directory")
      /bin/bash "$SCRIPTS/setup_ai_capabilities.sh" "${args[@]}"
      local result=$?
      if [ "$install" -eq 1 ] && [ "$result" -eq 0 ] && [ -d "$directory" ]; then
        update_api "$directory" --if-present || say '安装步骤已结束，但 API.md 未更新；可在「环境诊断与 API 说明」中重试。' >&2
      fi
      return "$result" ;;
    download-client)
      if [ "$install" -eq 1 ]; then
        find_client && return 0
        ensure_runtime || return $?; ensure_client; else /bin/bash "$SCRIPTS/bootstrap_download_env.sh" --dry-run "${args[@]}"; fi ;;
    models|download)
      if [ "$task" = download ] && [ "$dry" -eq 0 ]; then
        # 有 Python 时先选择并校验，取消不安装下载客户端或其他环境。
        if ! find_python; then
          say '列出并校验模型需要 Python 3.10+；将先进入基础环境准备，未选择模型前不会安装下载客户端。'
          ensure_runtime || return $?
        fi
        local choose_args=(--choose-only)
        [ -z "$reuse_mtp" ] || choose_args+=(--reuse-mtp-source "$reuse_mtp")
        [ -z "$select" ] || choose_args+=(--select "$select")
        [ -z "$directory" ] || choose_args+=(--models-dir "$directory")
        select="$("$PYTHON" "$SCRIPTS/download_models.py" "${choose_args[@]}")" || return $?
        [ -n "$select" ] || return 0
        if ! find_client; then ensure_runtime || return $?; ensure_client || return $?; fi
        args+=(--cli "$CLIENT")
      else
        find_python || { say '未找到 Python 3.10+；请在本菜单选择「准备基础环境」后继续。' >&2; return 1; }
      fi
      [ -z "$reuse" ] || args+=(--reuse-from "$reuse")
      [ -z "$reuse_mtp" ] || args+=(--reuse-mtp-source "$reuse_mtp")
      [ "$task" != models ] || args+=(--list)
      [ "$dry" -eq 0 ] || args+=(--dry-run)
      [ -z "$select" ] || args+=(--select "$select")
      [ -z "$directory" ] || args+=(--models-dir "$directory")
      "$PYTHON" "$SCRIPTS/download_models.py" "${args[@]}"
      local result=$? api_directory="${directory:-$MODEL_DIR}"
      if [ "$task" = download ] && [ "$dry" -eq 0 ] && [ -d "$api_directory" ]; then
        update_api "$api_directory" --if-present || say 'API.md 未更新；可在「环境诊断与 API 说明」中重试。' >&2
      fi
      return "$result" ;;
  esac
}
reuse_h3_menu() {
  say "检测 H3 模型：$MODEL_DIR"
  merge_h3
}
# 菜单只负责导航；安装、校验与下载仍由原有任务入口执行。
menu_result() {
  local code="$1"
  if [ "$code" -ne 0 ]; then printf '本次未完成（退出码 %s），可从菜单重试。\n' "$code"; fi
  ui_pause
}
install_menu() {
  local code=0
  while :; do
    ensure_project_directory || return $?
    UI_BACK_LABEL=返回
    ui_select '安装运行工具' \
      '准备基础工具' '检查并安装 Python、uv 等运行环境' \
      '安装推理与生成引擎' '选择需要的工具，确认后才开始安装'
    case "$UI_CHOICE" in
      0|q) return "$code" ;;
      h) usage; ui_pause; continue ;;
      1) run_task env --install ;;
      2) run_task capabilities --install --models-dir "$MODEL_DIR" ;;
      *) say '请选择菜单中的编号。'; ui_pause; continue ;;
    esac
    code=$?; menu_result "$code"
  done
}
models_menu() {
  local code=0
  while :; do
    ensure_project_directory || return $?
    UI_BACK_LABEL=返回
    ui_select '模型管理' \
      '下载模型' '按用途选择模型，下载到指定目录' \
      '浏览可下载模型' '查看模型清单，不下载、不安装' \
      '设置模型目录' '选择本机磁盘或已挂载的外置磁盘' \
      '合并 H3 共享权重' '自动检测两版，相同文件校验后共享' \
      '查看已下载模型' '查看本地文件、体积与合并建议'
    case "$UI_CHOICE" in
      0|q) return "$code" ;;
      h) usage; ui_pause; continue ;;
      1) if [ "$DIRECTORY_CHOSEN" -eq 1 ] || choose_directory; then run_task download --models-dir "$MODEL_DIR"; else say '已取消。'; fi ;;
      2) run_task models --models-dir "$MODEL_DIR" ;;
      3|d) printf '当前目录：%s\n' "$MODEL_DIR"; choose_directory ;;
      4|r) reuse_h3_menu ;;
      5) run_task installed-models ;;
      t) run_task models --details --models-dir "$MODEL_DIR" ;;
      *) say '请选择菜单中的编号。'; ui_pause; continue ;;
    esac
    code=$?; menu_result "$code"
  done
}
diagnostics_menu() {
  local code=0
  while :; do
    ensure_project_directory || return $?
    UI_BACK_LABEL=返回
    ui_select '环境诊断与 API 说明' \
      '检查运行环境' '只读检查依赖与版本，不安装工具' \
      '检查工具与运行状态' '查看安装、配置及正在运行的工具，不启停服务' \
      '查看与更新 API 说明' '更新当前模型目录的 API.md 文档，不启动服务'
    case "$UI_CHOICE" in
      0|q) return "$code" ;;
      h) usage; ui_pause; continue ;;
      1) run_task check ;;
      2) check_tools ;;
      3|a) api_menu ;;
      *) say '请选择菜单中的编号。'; ui_pause; continue ;;
    esac
    code=$?; menu_result "$code"
  done
}
benchmark_menu() {
  local code=0
  while :; do
    ensure_project_directory || return $?
    UI_BACK_LABEL=返回
    ui_select 'Benchmark' \
      'Llama benchmark' '0、50K、100K、200K 上下文吞吐测试；PP 计时不兼容 MLX-Serve、oMLX 等框架的空 role 消息。' \
      'MLX-Serve 长上下文 benchmark' '0、50K、100K、200K 四档测试' \
      'AIPerf 并发压力测试' '多并发下的首 token 延迟、吞吐与错误率；与长上下文测试口径不同，不可直接互比。'
    case "$UI_CHOICE" in
      0|q) return "$code" ;;
      h) usage; ui_pause; continue ;;
      1) benchmark ;;
      2) mlx_benchmark ;;
      3) aiperf_benchmark ;;
      *) say '请选择菜单中的编号。'; ui_pause; continue ;;
    esac
    code=$?; menu_result "$code"
  done
}
menu() {
  local code=0
  while :; do
    ensure_project_directory || return $?
    UI_BACK_LABEL=退出
    ui_select '从这里开始' \
      '安装运行工具' '准备基础环境，选择推理与生成引擎' \
      '管理本地模型' '浏览、下载模型，设置存储目录' \
      '配置 Remotion 远程渲染' '在你指定的仓库目录中配置脚本和服务' \
      '环境诊断与 API 说明' '检查安装状态，整理本机接口说明' \
      'Benchmark' '测试已有接口，保存测速报告' \
      '服务管理' '独立服务一键启停、状态与日志；llama-swap 可选'
    case "$UI_CHOICE" in
      0|q) return "$code" ;;
      h) usage; ui_pause; continue ;;
      1) install_menu; code=$?; continue ;;
      2) models_menu; code=$?; continue ;;
      3|R) configure_remote ;;
      4) diagnostics_menu; code=$?; continue ;;
      5) benchmark_menu; code=$?; continue ;;
      # 保留不冲突的旧快捷键，不在主屏堆叠高级操作。
      d) choose_directory ;;
      t) run_task models --details --models-dir "$MODEL_DIR" ;;
      6) services; code=$?; [ "$code" -eq 0 ] || menu_result "$code"; continue ;;
      8) run_task download --dry-run --models-dir "$MODEL_DIR" ;;
      r) reuse_h3_menu ;;
      a) api_menu ;;
      *) say '请选择菜单中的编号。'; ui_pause; continue ;;
    esac
    code=$?; menu_result "$code"
  done
}
refresh_paths
if [ "$#" -gt 0 ]; then
  run_task "$@"
else
  source "$SCRIPTS/terminal_ui.sh" || { say '菜单模块缺失，请重新获取完整工具包。' >&2; exit 2; }
  menu
fi
exit $?
