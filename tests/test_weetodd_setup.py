"""WeeTodd 安装入口的隔离回归；不下载、不构建、不运行模型。"""
import importlib.util
import io
import hashlib
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/weetodd_setup.py"


class WeeToddSetupTests(unittest.TestCase):
    def setUp(self):
        for stream in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            stream.__enter__()
            self.addCleanup(stream.__exit__, None, None, None)

    def load_module(self):
        self.assertTrue(SCRIPT.is_file(), "缺少 WeeTodd 安装器")
        spec = importlib.util.spec_from_file_location("weetodd_setup", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_default_preview_does_not_create_directories(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "尚未创建的安装目录"
            with redirect_stdout(io.StringIO()) as output:
                code = module.main(["--root", str(root)])
            self.assertEqual(code, 0)
            self.assertIn("Xcode", output.getvalue())
            self.assertIn(module.REVISION, output.getvalue())
            self.assertFalse(root.exists())

    def make_app(self, folder):
        app = folder / "有空格的 WeeTodd Studio.app"
        macos = app / "Contents/MacOS"
        resources = app / "Contents/Resources"
        macos.mkdir(parents=True)
        (resources / "LTXNative").mkdir(parents=True)
        (resources / "LTXNative/mlx.metallib").write_bytes(b"fixture only")
        for name in ("WeeToddCLI", "WeeToddH3MLXWorker"):
            target = macos / name
            target.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            target.chmod(0o755)
        manifest = resources / "H3MLXNative/manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({
            "format": "weetodd-h3-mlx-worker-v1", "protocol": 1,
            "sharedMetalLibrary": "LTXNative/mlx.metallib",
            "files": {"WeeToddH3MLXWorker": hashlib.sha256(
                (macos / "WeeToddH3MLXWorker").read_bytes()).hexdigest()},
        }))
        return app

    def test_reuse_installs_wrapper_and_is_idempotent(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            app = self.make_app(folder)
            root, bins = folder / "install", folder / "bin"
            tools = {"ffmpeg": "/usr/bin/true", "ffprobe": "/usr/bin/true"}
            argv = ["install", "--app", str(app), "--root", str(root),
                    "--bin-dir", str(bins), "--yes"]
            with patch.object(module, "check_host"), patch.object(module, "verify_signature"), \
                    patch.object(module, "media_tools", return_value=tools):
                self.assertEqual(module.main(argv), 0)
                before = (root / "config.json").read_bytes()
                self.assertEqual(module.main(argv), 0)
                self.assertEqual(before, (root / "config.json").read_bytes())
            wrapper = bins / "weetodd"
            result = subprocess.run([str(wrapper), "models", "--help", "含 空格"],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), ["models", "--help", "含 空格"])
            self.assertEqual(json.loads(before)["app"], str(app))
            self.assertFalse((root / "source").exists())

    def test_build_checks_xcode_before_download(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "install"
            with patch.object(module, "check_host"), \
                    patch.object(module, "media_tools", return_value={}), \
                    patch.object(module, "check_xcode", side_effect=ValueError("缺少完整 Xcode")) as check, \
                    patch.object(module, "prepare_source") as source:
                self.assertEqual(module.main(["install", "--root", str(root), "--yes"]), 1)
                check.assert_called_once()
                source.assert_not_called()
            self.assertFalse(root.exists())

    def test_source_build_uses_pinned_revision_and_separate_output(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "install"
            source = root / "source" / module.REVISION
            with patch.object(module, "run") as run:
                run.return_value.stdout = module.REVISION + "\n"
                self.assertEqual(module.prepare_source(root), source)
                commands = [list(map(str, call.args[0])) for call in run.call_args_list]
                self.assertIn(["git", "-C", str(source), "fetch", "--depth", "1", "origin", module.REVISION], commands)
                self.assertIn(["git", "-C", str(source), "checkout", "--detach", module.REVISION], commands)
            with patch.object(module, "run") as run:
                app = module.build_app(source, Path("/Applications/Xcode.app"))
                command = list(map(str, run.call_args.args[0]))
                self.assertIn("--output", command)
                self.assertIn(str(app), command)
                self.assertIn("--xcode", command)
                self.assertEqual(app, source / "mac-ai-kit/WeeTodd Studio.app")

    def test_build_rejects_output_symlinks_before_reuse_or_build(self):
        module = self.load_module()
        for location in ("app", "output_parent", "source_ancestor"):
            for dangling in (False, True):
                with self.subTest(location=location, dangling=dangling), \
                        tempfile.TemporaryDirectory() as tmp:
                    folder = Path(tmp).resolve()
                    target = folder / "target"
                    if not dangling:
                        target.mkdir()
                    source = folder / "source"
                    app = source / "mac-ai-kit/WeeTodd Studio.app"
                    link = {"app": app, "output_parent": app.parent,
                            "source_ancestor": source}[location]
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(target, target_is_directory=True)
                    with patch.object(module, "run") as run:
                        with self.assertRaisesRegex(ValueError, "符号链接"):
                            module.build_app(source, Path("/unused/Xcode.app"))
                        run.assert_not_called()
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(target.exists(), not dangling)
                    if not dangling:
                        self.assertEqual(list(target.iterdir()), [])

    def test_invalid_app_and_foreign_wrapper_are_preserved(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            app = self.make_app(folder)
            metal = app / "Contents/Resources/LTXNative/mlx.metallib"
            metal.unlink()
            with self.assertRaisesRegex(ValueError, "Metal"):
                module.validate_app(app)
            metal.write_bytes(b"fixture")
            bins = folder / "bin"
            bins.mkdir()
            wrapper = bins / "weetodd"
            wrapper.write_text("用户已有入口")
            with patch.object(module, "verify_signature"), self.assertRaisesRegex(ValueError, "保留不覆盖"):
                module.configure(app, folder / "install", bins,
                                 {"ffmpeg": "/usr/bin/true", "ffprobe": "/usr/bin/true"})
            self.assertEqual(wrapper.read_text(), "用户已有入口")
            self.assertFalse((folder / "install").exists())

    def test_volume_guard_is_case_insensitive_without_filesystem_access(self):
        module = self.load_module()
        for name in ("Volumes", "volumes", "VOLUMES", "vOlUmEs"):
            for suffix in ("", "/Unavailable/AI"):
                with self.subTest(name=name, suffix=suffix), \
                        patch.object(Path, "is_symlink", return_value=False), \
                        patch.object(module.os.path, "ismount", return_value=False), \
                        patch.object(Path, "mkdir") as mkdir, \
                        patch.object(module, "run") as run:
                    with self.assertRaisesRegex(ValueError, "未挂载"):
                        module.safe_directory(Path(f"/{name}{suffix}"))
                    mkdir.assert_not_called()
                    run.assert_not_called()

    def test_paths_reject_unmounted_volume_and_symlinks(self):
        module = self.load_module()
        with patch.object(Path, "is_symlink", return_value=False), \
                patch.object(module.os.path, "ismount", return_value=False):
            with self.assertRaisesRegex(ValueError, "未挂载"):
                module.safe_directory(Path("/Volumes/Unavailable/AI"))
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link"
            link.symlink_to(tmp, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "符号链接"):
                module.safe_directory(link / "install")

    def test_doctor_and_cli_require_configuration_and_preserve_arguments(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "install"
            with patch.object(module, "check_host"):
                self.assertEqual(module.main(["doctor", "--root", str(root)]), 1)
                self.assertFalse(root.exists())
                app = self.make_app(Path(tmp))
                with patch.object(module, "verify_signature"):
                    module.configure(app, root, Path(tmp) / "bin",
                                     {"ffmpeg": "/usr/bin/true", "ffprobe": "/usr/bin/true"})
                    self.assertEqual(module.main(["doctor", "--root", str(root)]), 0)
                with patch.object(module, "run") as run:
                    run.return_value.returncode = 0
                    self.assertEqual(module.main(["cli", "--root", str(root), "--",
                                                  "models", "--help"]), 0)
                    self.assertEqual(list(map(str, run.call_args.args[0])),
                                     [str(app / "Contents/MacOS/WeeToddCLI"), "models", "--help"])

    def test_entry_preview_and_help(self):
        result = subprocess.run(["bash", str(SCRIPT.parents[1] / "start.sh"), "weetodd", "preview"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Xcode", result.stdout)
        help_result = subprocess.run(["bash", str(SCRIPT.parents[1] / "start.sh"),
                                      "script-help", "weetodd"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--xcode", help_result.stdout)

    def test_cli_preserves_failure_exit_code(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            app = self.make_app(folder)
            root = folder / "install"
            with patch.object(module, "verify_signature"):
                module.configure(app, root, folder / "bin",
                                 {"ffmpeg": "/usr/bin/true", "ffprobe": "/usr/bin/true"})
            with patch.object(module, "check_host"), patch.object(module, "run",
                    side_effect=subprocess.CalledProcessError(7, ["fixture"])):
                self.assertEqual(module.main(["cli", "--root", str(root), "--", "--help"]), 7)

    def test_invalid_configuration_is_reported_without_traceback(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for value in ("[]", "null", '{"format": "mac-ai-kit-weetodd-v1"}', "broken"):
                (root / "config.json").write_text(value)
                with patch.object(module, "check_host"):
                    self.assertEqual(module.main(["doctor", "--root", str(root)]), 1)

    def test_host_and_xcode_guards(self):
        module = self.load_module()
        with patch.object(module.platform, "system", return_value="Linux"):
            with self.assertRaisesRegex(ValueError, "Apple Silicon"):
                module.check_host()
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Xcode.app"
            with self.assertRaisesRegex(ValueError, "完整 Xcode"):
                module.check_xcode(app)
            executable = app / "Contents/Developer/usr/bin/xcodebuild"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.object(module, "run") as run:
                run.return_value.stdout = "Xcode 25.0\n"
                with self.assertRaisesRegex(ValueError, "Xcode 26"):
                    module.check_xcode(app)
                run.return_value.stdout = "Xcode 27.2\n"
                self.assertEqual(module.check_xcode(app), app / "Contents/Developer")
                self.assertEqual(run.call_args.kwargs["env"]["DEVELOPER_DIR"], str(app / "Contents/Developer"))

    def test_dirty_or_wrong_source_is_not_reset(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source" / module.REVISION
            (source / ".git").mkdir(parents=True)
            for origin, head, status in ((module.REPOSITORY, module.REVISION, " M user.txt"),
                                         ("https://example.invalid/other", module.REVISION, ""),
                                         (module.REPOSITORY, "0" * 40, "")):
                outputs = [subprocess.CompletedProcess([], 0, stdout=value) for value in (origin, head, status)]
                with patch.object(module, "run", side_effect=outputs) as run:
                    with self.assertRaisesRegex(ValueError, "保留不 reset"):
                        module.prepare_source(root)
                    self.assertEqual(run.call_count, 3)

    def test_corrupt_worker_is_rejected_before_launch(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            (app / "Contents/MacOS/WeeToddH3MLXWorker").write_text("modified")
            with patch.object(module, "run") as run:
                with self.assertRaisesRegex(ValueError, "SHA256"):
                    module.validate_app(app)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
