#!/usr/bin/env python3
"""安装完整 WeeTodd App 并配置 CLI；不下载模型、不执行生成任务。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY = "https://github.com/wee-todd/WeeTodd-Studio.git"
REVISION = "b398fcc52a4cb9aaf91ed0739feda9f8620d14f8"


def run(command, **kwargs):
    return subprocess.run([str(part) for part in command], check=True, text=True, **kwargs)


def check_host():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("需要原生 Apple Silicon Mac；不支持 Linux 或 Rosetta。")


def media_tools():
    result = {}
    for name in ("ffmpeg", "ffprobe"):
        executable = shutil.which(name)
        if not executable:
            raise ValueError(f"缺少 {name}；已有 Homebrew 时请运行 brew install ffmpeg。")
        run([executable, "-version"], capture_output=True, timeout=30)
        result[name] = executable
    return result


def check_xcode(selected):
    value = selected or os.environ.get("DEVELOPER_DIR")
    if not value:
        value = run(["/usr/bin/xcode-select", "-p"], capture_output=True, timeout=30).stdout.strip()
    developer = Path(value).expanduser().resolve()
    if developer.suffix == ".app":
        developer /= "Contents/Developer"
    if not (developer / "usr/bin/xcodebuild").is_file():
        raise ValueError("需要完整 Xcode 26+，仅 Command Line Tools 不够。安装并首次打开 Xcode，"
                         "完成初始化和 Metal Toolchain 后，用 --xcode 指定路径；不更改系统选择。")
    env = os.environ.copy()
    env["DEVELOPER_DIR"] = str(developer)
    version = run([developer / "usr/bin/xcodebuild", "-version"], env=env,
                  capture_output=True, timeout=30).stdout
    match = re.search(r"^Xcode (\d+)", version)
    if not match or int(match[1]) < 26:
        raise ValueError(f"需要 Xcode 26+：{version.strip()}")
    # 真正的 Metal 编译/链接与 SwiftUI 宏探测由固定版本上游构建器执行。
    print(version.strip())
    return developer


def prepare_source(root):
    source = safe_directory(root / "source" / REVISION)
    if source.exists():
        if not (source / ".git").is_dir():
            raise ValueError(f"已有非受管目录，不覆盖：{source}")
        origin = run(["git", "-C", source, "remote", "get-url", "origin"], capture_output=True).stdout.strip()
        head = run(["git", "-C", source, "rev-parse", "HEAD"], capture_output=True).stdout.strip()
        status = run(["git", "-C", source, "status", "--porcelain", "--", ".", ":(exclude)mac-ai-kit"],
                     capture_output=True).stdout.strip()
        if origin != REPOSITORY or head != REVISION or status:
            raise ValueError(f"已有源码来源、提交或工作区不匹配，保留不 reset/pull：{source}")
    else:
        source.mkdir(parents=True)
        run(["git", "init", source])
        run(["git", "-C", source, "remote", "add", "origin", REPOSITORY])
        run(["git", "-C", source, "fetch", "--depth", "1", "origin", REVISION])
        run(["git", "-C", source, "checkout", "--detach", REVISION])
        if run(["git", "-C", source, "rev-parse", "HEAD"], capture_output=True).stdout.strip() != REVISION:
            raise ValueError("上游提交核对失败。")
    return source


def build_app(source, xcode):
    # 上游会 resolve --output；必须先检查最终包路径（包括悬空链接）。
    app = safe_directory(source / "mac-ai-kit" / "WeeTodd Studio.app")
    if app.exists():
        # 已有包即使不完整也不覆盖，后面的 validate_app 会说明缺项。
        return app
    run([sys.executable, source / "scripts/build_studio_app.py", "--configuration", "release",
         "--xcode", xcode, "--output", app], cwd=source)
    return app


def verify_signature(app):
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app],
        capture_output=True, timeout=60)


def validate_app(app):
    macos = app / "Contents/MacOS"
    for name in ("WeeToddCLI", "WeeToddH3MLXWorker"):
        target = macos / name
        if not target.is_file() or not os.access(target, os.X_OK):
            raise ValueError(f"完整 App 缺少可执行文件：{target}")
    resources = app / "Contents/Resources"
    metal = resources / "LTXNative/mlx.metallib"
    if not metal.is_file() or metal.stat().st_size == 0:
        raise ValueError(f"缺少 H3 共享 Metal 资源：{metal}")
    manifest = json.loads((resources / "H3MLXNative/manifest.json").read_text())
    worker = macos / "WeeToddH3MLXWorker"
    if (not isinstance(manifest, dict) or manifest.get("format") != "weetodd-h3-mlx-worker-v1"
            or manifest.get("protocol") != 1
            or manifest.get("sharedMetalLibrary") != "LTXNative/mlx.metallib"
            or not isinstance(manifest.get("files"), dict)
            or manifest.get("files", {}).get(worker.name) != hashlib.sha256(worker.read_bytes()).hexdigest()):
        raise ValueError("H3 MLX worker manifest 或 SHA256 不匹配；不会绕过校验。")
    verify_signature(app)
    # 仅帮助，不运行模型检查、下载、worker 预检或渲染。
    run([macos / "WeeToddCLI", "models", "--help"], capture_output=True, timeout=60)


def safe_directory(path):
    path = path.expanduser().absolute()
    if path == Path("/") or ".." in path.parts or any(c in str(path) for c in "\n\r\0"):
        raise ValueError(f"目录不安全：{path}")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f"安装目录不能经过符号链接：{path}")
    # macOS 通常大小写不敏感；其他平台也保守拦截这些别名，避免假卷目录。
    if len(path.parts) >= 2 and path.parts[1].casefold() == "volumes":
        if len(path.parts) < 3 or not os.path.ismount(Path(*path.parts[:3])):
            raise ValueError("外置盘未挂载；不会创建同名目录。")
    return path


def wrapper_text(app, tools):
    parents = list(dict.fromkeys(str(Path(tools[name]).parent) for name in ("ffmpeg", "ffprobe")))
    return ("#!/bin/bash\n# mac-ai-kit: weetodd-wrapper-v1\n"
            f"export PATH={shlex.quote(':'.join(parents))}:\"$PATH\"\n"
            f"exec {shlex.quote(str(app / 'Contents/MacOS/WeeToddCLI'))} \"$@\"\n")


def require_same_or_absent(path, content):
    if path.is_symlink() or (path.exists() and path.read_text() != content):
        raise ValueError(f"已有文件不匹配，保留不覆盖：{path}；请使用独立 --root / --bin-dir。")


def configure(app, root, bins, tools):
    validate_app(app)
    wrapper = wrapper_text(app, tools)
    metadata = json.dumps({"format": "mac-ai-kit-weetodd-v1", "app": str(app),
                           "cli": str(app / "Contents/MacOS/WeeToddCLI"),
                           "h3_swift_worker": str(app / "Contents/MacOS/WeeToddH3MLXWorker"),
                           **tools}, ensure_ascii=False, indent=2) + "\n"
    for target, text in ((root / "config.json", metadata), (bins / "weetodd", wrapper)):
        require_same_or_absent(target, text)
    root.mkdir(parents=True, exist_ok=True)
    bins.mkdir(parents=True, exist_ok=True)
    for target, text in ((root / "config.json", metadata), (bins / "weetodd", wrapper)):
        if not target.exists():
            with target.open("x") as stream:
                stream.write(text)
    (bins / "weetodd").chmod(0o755)
    print(f"已配置 CLI：{bins / 'weetodd'}\nApp：{app}\n配置记录：{root / 'config.json'}")
    print("未修改 shell 启动文件或 Studio 设置；未下载模型、未进行任务预检或生成。")


def load_config(root):
    target = root / "config.json"
    if not target.is_file() or target.is_symlink():
        raise ValueError(f"尚未配置 WeeTodd：{target}；先运行 install。")
    config = json.loads(target.read_text())
    if not isinstance(config, dict) or config.get("format") != "mac-ai-kit-weetodd-v1":
        raise ValueError("不支持的 WeeTodd 配置格式。")
    for key in ("app", "cli", "h3_swift_worker", "ffmpeg", "ffprobe"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise ValueError(f"配置缺少有效绝对路径：{key}")
    app = Path(config["app"])
    if (Path(config["cli"]) != app / "Contents/MacOS/WeeToddCLI"
            or Path(config["h3_swift_worker"]) != app / "Contents/MacOS/WeeToddH3MLXWorker"):
        raise ValueError("CLI / worker 必须属于同一个完整 App。")
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog="透传示例：bash start.sh weetodd cli -- models --help")
    parser.add_argument("command", nargs="?", default="preview", choices=("preview", "install", "doctor", "cli"))
    parser.add_argument("--root", type=Path, default=Path.home() / "AI/apps/weetodd-studio")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / "AI/bin")
    parser.add_argument("--app", type=Path, help="复用已有完整 App，不构建、不覆盖")
    parser.add_argument("--xcode", type=Path, help="完整 Xcode .app 或 Contents/Developer；不修改 xcode-select")
    parser.add_argument("--yes", action="store_true", help="确认安装计划（非交互必须提供）")
    argv = list(sys.argv[1:] if argv is None else argv)
    forwarded = []
    if "--" in argv:
        index = argv.index("--")
        forwarded, argv = argv[index + 1:], argv[:index]
    args = parser.parse_args(argv)
    if forwarded and args.command != "cli":
        parser.error("只有 cli 接受 -- 后的原生命令参数。")
    if args.command != "cli":
        print(f"WeeTodd 源码：{REPOSITORY}\n固定构建提交：{REVISION}\n安装目录：{args.root}")
        print("源码构建需 Apple Silicon、完整 Xcode 26+ / Metal Toolchain、FFmpeg/FFprobe。")
        print("预览不写文件、不联网、不安装；安装也不下载模型、不打开 App、不运行推理。")
    if args.command == "preview":
        return 0
    try:
        root, bins = safe_directory(args.root), safe_directory(args.bin_dir)
        if args.command in ("doctor", "cli"):
            check_host()
            config = load_config(root)
            if args.command == "doctor":
                validate_app(Path(config["app"]))
                for name in ("ffmpeg", "ffprobe"):
                    run([config[name], "-version"], capture_output=True, timeout=30)
                print(json.dumps(config, ensure_ascii=False, indent=2))
                print("安装检查通过（不代表模型兼容、任务预检或生成通过）。")
                return 0
            env = os.environ.copy()
            env["PATH"] = os.pathsep.join([str(Path(config[name]).parent) for name in ("ffmpeg", "ffprobe")]
                                          + [env.get("PATH", "")])
            return run([config["cli"], *(forwarded or ["--help"])], env=env).returncode
        if not args.yes and (not sys.stdin.isatty() or input("确认安装？[y/N] ").lower() != "y"):
            raise ValueError("未确认安装；非交互请显式添加 --yes。")
        check_host()
        tools = media_tools()
        if args.app is not None:
            app = args.app.expanduser().resolve()
        elif (root / "config.json").is_file():
            app = Path(load_config(root)["app"])
        else:
            existing = [p for p in (Path.home() / "Applications/WeeTodd Studio.app",
                                    Path("/Applications/WeeTodd Studio.app")) if p.exists()]
            if existing:
                raise ValueError(f"发现已有 App：{existing}；请用 --app 明确选择并复用，不另装。")
            xcode = check_xcode(args.xcode)
            app = build_app(prepare_source(root), xcode)
        configure(app, root, bins, tools)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"WeeTodd 未完成：{exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        if args.command == "cli" and isinstance(exc, subprocess.CalledProcessError):
            return exc.returncode if exc.returncode > 0 else 128 - exc.returncode
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
