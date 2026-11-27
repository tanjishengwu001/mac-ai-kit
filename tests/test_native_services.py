"""原生生命周期离线测试：绝不执行真实二进制、launchctl 或网络请求。"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_services', ROOT / 'scripts/native_services.py')
ns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ns)
REAL_AVAILABLE_MEMORY = ns.available_memory
REAL_NETWORK_ADDRESSES = ns.network_addresses
REAL_PROCESS_IDENTITY = ns.NativeManager.process_identity


class NativeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / 'AI'
        self.models = self.home / 'external-models'
        self.root.mkdir(); self.models.mkdir()
        entries = [
            ('qwen21-turbo', {'diffusion_models/qwen_image_2.1_turbo_bf16.safetensors': 3,
                              'text_encoders/qwen3vl_8b_bf16.safetensors': 4,
                              'vae/qwen_image_2.1_vae_bf16.safetensors': 5}),
            ('asr-qwen3-1.7b-f16', {'qwen3-asr.gguf': 7}),
            ('yue2-audio-cpp-bf16', {'yue2-3b-bf16.gguf': 9, 'yue2-vae-f32.gguf': 11}),
        ]
        self.catalog = {'models': []}
        for mid, files in entries:
            self.catalog['models'].append(dict(id=mid, subdir=mid, expected_files={k: {'bytes': v} for k, v in files.items()}))
            for name, size in files.items():
                path = self.models / mid / name
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'x' * size)
        for spec in ns.SPECS.values():
            binary = self.root / 'apps' / ('sd.cpp' if spec['component'] == 'sd.cpp' else spec['component'] + '-' + spec['version']) / 'bin' / spec['binary']
            binary.parent.mkdir(parents=True); binary.write_text('#!/bin/sh\nexit 99\n'); binary.chmod(0o755)
        self.manager = ns.NativeManager(self.root, self.models, self.catalog)
        self.out = io.StringIO()
        for p in (
            patch.object(ns.sys, 'platform', 'darwin'),
            patch('sd_release.validate_installed', return_value={'binary_relative': 'bin/sd-server', 'tag': 'fixture-latest'}),
            patch.object(ns, 'STOP_TIMEOUT', 0),
            patch.object(self.manager, 'process_identity', return_value=None),
            patch.object(self.manager, 'load_default_sd'),
            patch.object(self.manager, 'wait_ready'),
            patch.object(self.manager, 'refresh_inventory'),
            patch.object(self.manager, 'queue_action', create=True),
            patch.object(ns.subprocess, 'run', side_effect=AssertionError('unexpected process execution')),
            patch.object(ns.http.client, 'HTTPConnection', side_effect=AssertionError('unexpected HTTP')),
            patch.object(ns, 'available_port', return_value=None),
            patch.object(ns, 'network_addresses', return_value=['192.168.1.7', '100.80.1.2']),
            patch.object(ns, 'available_memory', return_value=128 * 1024**3),
            contextlib.redirect_stdout(self.out),
        ):
            p.__enter__(); self.addCleanup(p.__exit__, None, None, None)

    def state(self, loaded=False, owned=False, pid=None):
        return dict(loaded=loaded, owned=owned, pid=pid, detail='test state')

    def configure(self, sid='audio'):
        data = self.manager.recipe(sid)
        self.manager.configure(sid, data)
        return data

    def start_mocks(self):
        for p in (patch.object(self.manager, 'state', return_value=self.state()),
                  patch.object(self.manager, 'ctl', return_value=Mock(returncode=0, stderr='')),
                  patch.object(self.manager, 'status'), patch.object(self.manager, 'write_api')):
            p.__enter__(); self.addCleanup(p.__exit__, None, None, None)

    def test_start_checks_inventory_even_when_services_already_running(self):
        self.configure('audio')
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 4)), patch.object(self.manager, 'status'), patch.object(self.manager, 'write_api') as write:
            self.manager.start('audio')
        self.manager.refresh_inventory.assert_called_once_with('audio')
        write.assert_called_once_with('audio')

    def test_api_update_is_one_line_and_preserves_manual_content(self):
        (self.models / 'API.md').write_text('我的手工说明\n')
        self.configure('audio')
        with patch.object(self.manager, 'state', return_value=self.state()):
            self.manager.write_api()
        self.assertIn('我的手工说明', (self.models / 'API.md').read_text())
        self.assertEqual(self.out.getvalue().strip(), '已更新：' + str(self.models / 'API.md'))

    def test_unconfigured_status_shows_missing_binary_reason(self):
        with patch.object(self.manager, 'state', return_value=self.state()), patch.object(ns, 'installed_binary', return_value=None):
            self.manager.status('audio')
        self.assertIn('audio.cpp 未安装完整', self.out.getvalue())
        self.assertFalse(self.manager.paths('audio')[1].exists())

    def test_unconfigured_status_shows_ready_without_start(self):
        with patch.object(self.manager, 'state', return_value=self.state()):
            self.manager.status('audio')
        self.assertIn('检查通过', self.out.getvalue())
        self.assertFalse(self.manager.paths('audio')[1].exists())

    def test_incomplete_audio_reports_actual_file(self):
        (self.models / 'asr-qwen3-1.7b-f16/qwen3-asr.gguf').unlink()
        (self.models / 'yue2-audio-cpp-bf16/yue2-vae-f32.gguf').unlink()
        with self.assertRaisesRegex(ns.ServiceError, 'qwen3-asr.gguf'):
            self.manager.recipe('audio')

    def test_all_bind_api_lists_actual_addresses(self):
        self.configure()
        with patch.object(self.manager, 'state', return_value=self.state()):
            self.manager.write_api()
        text = (self.models / 'API.md').read_text()
        self.assertIn('http://192.168.1.7:18080', text)
        self.assertIn('http://100.80.1.2:18080', text)
        self.assertNotIn('http://0.0.0.0', text)

    def test_start_migrates_owned_loopback_only_after_explicit_start(self):
        data = self.manager.recipe('audio')
        data['host'] = data['config']['host'] = '127.0.0.1'
        self.manager.configure('audio', data)
        with patch.object(self.manager, 'state', side_effect=[self.state(True, True, 1), self.state(), self.state()]), patch.object(self.manager, 'ctl', return_value=Mock(returncode=0)) as ctl, patch.object(self.manager, 'status'), patch.object(self.manager, 'write_api'):
            self.manager.start('audio')
        self.assertEqual(ctl.call_args_list[0].args[0], 'bootout')
        self.assertEqual(ctl.call_args_list[1].args[0], 'bootstrap')
        self.assertEqual(self.manager.read_settings('audio')['host'], '0.0.0.0')

    def test_readme_not_required_for_audio_runtime(self):
        self.catalog['models'][2]['expected_files']['README.md'] = {'bytes': 9986}
        self.assertIn('yue2', self.manager.recipe('audio')['models'])

    def test_network_addresses_filters_loopback_and_keeps_vpn(self):
        output = 'inet 127.0.0.1 netmask x\n inet 192.168.1.4 netmask x\n inet 100.64.0.8 netmask x\n inet 192.168.1.4 netmask x'
        with patch.object(ns.subprocess, 'run', return_value=Mock(returncode=0, stdout=output)):
            self.assertEqual(REAL_NETWORK_ADDRESSES(), ['192.168.1.4', '100.64.0.8'])

    def test_failed_loopback_stop_does_not_change_saved_configuration(self):
        data = self.manager.recipe('audio')
        data['host'] = data['config']['host'] = '127.0.0.1'
        self.manager.configure('audio', data)
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 1)), patch.object(self.manager, 'ctl', return_value=Mock(returncode=1)), patch.object(self.manager, 'status'), patch.object(self.manager, 'write_api'):
            with self.assertRaises(ns.ServiceError):
                self.manager.start('audio')
        self.assertEqual(self.manager.read_settings('audio')['host'], '127.0.0.1')

    def test_runtime_tokenizer_text_remains_required(self):
        self.catalog['models'][2]['expected_files']['vocab.txt'] = {'bytes': 15}
        self.assertNotIn('yue2', self.manager.recipe('audio')['models'])

    def test_sd_api_example_requests_real_webp(self):
        self.configure('sd-qwen')
        with patch.object(self.manager, 'state', return_value=self.state()):
            self.manager.write_api()
        text = (self.models / 'API.md').read_text()
        self.assertIn('"output_format": "webp"', text)
        self.assertIn('"output_compression": 100', text)
        self.assertIn('省略 output_format 时仍使用 PNG', text)

    def test_start_already_running_still_refreshes_api(self):
        self.configure('audio')
        self.start_mocks()
        self.manager.state.return_value = self.state(True, True, 42)
        self.manager.start('audio')
        self.manager.write_api.assert_called_once()
        self.manager.ctl.assert_not_called()

    def test_status_omits_unrelated_footer(self):
        with patch.object(self.manager, 'state', return_value=self.state()):
            self.manager.status('audio')
        self.assertNotIn('仍由桌面 App', self.out.getvalue())
        self.assertNotIn('Krea 2 Turbo', self.out.getvalue())
        self.assertNotIn('llama-swap 保持', self.out.getvalue())

    def test_model_completeness_uses_all_expected_file_sizes(self):
        self.assertEqual(self.manager.model('qwen21-turbo')[1], 12)
        (self.models / 'qwen21-turbo/vae/qwen_image_2.1_vae_bf16.safetensors').write_bytes(b'bad')
        with self.assertRaises(ns.ServiceError): self.manager.recipe('sd-qwen')

    def test_empty_model_directory_is_not_complete(self):
        for path in (self.models / 'asr-qwen3-1.7b-f16').iterdir(): path.unlink()
        with self.assertRaises(ns.ServiceError): self.manager.model('asr-qwen3-1.7b-f16')

    def test_model_dependency_missing_rejected(self):
        self.catalog['models'][0]['requires'] = ['missing']
        with self.assertRaises(ns.ServiceError): self.manager.model('qwen21-turbo')

    def test_model_dependency_cycle_rejected(self):
        self.catalog['models'][0]['requires'] = ['qwen21-turbo']
        with self.assertRaises(ns.ServiceError): self.manager.model('qwen21-turbo')

    def test_model_file_symlink_not_complete(self):
        p = self.models / 'asr-qwen3-1.7b-f16/qwen3-asr.gguf'
        other = self.home / 'outside.gguf'; other.write_bytes(p.read_bytes()); p.unlink(); p.symlink_to(other)
        with self.assertRaises(ns.ServiceError): self.manager.model('asr-qwen3-1.7b-f16')

    def test_audio_shared_lazy_config_has_both_models(self):
        data = self.manager.recipe('audio')
        self.assertEqual(data['models'], ['qwen3-asr', 'yue2'])
        self.assertIs(data['config']['lazy_load'], True)
        self.assertEqual(data['config']['max_loaded_models'], 1)
        self.assertEqual(data['config']['idle_unload_ms'], 300000)
        self.assertIs(data['config']['ui_management'], False)
        self.assertIs(data['config']['ui'], True)
        self.assertEqual(data['reserve_bytes'], 60)
        self.assertEqual(data['host'], '0.0.0.0')
        self.assertEqual(data['port'], 18080)

    def test_audio_can_use_only_complete_asr(self):
        (self.models / 'yue2-audio-cpp-bf16/yue2-vae-f32.gguf').unlink()
        self.assertEqual(self.manager.recipe('audio')['models'], ['qwen3-asr'])

    def test_audio_without_complete_models_rejected(self):
        with patch.object(self.manager, 'model', side_effect=ns.ServiceError('missing')):
            with self.assertRaises(ns.ServiceError): self.manager.recipe('audio')

    def test_sd_recipe_has_no_lazy_or_shell_launcher(self):
        data = self.manager.recipe('sd-qwen')
        self.assertEqual(data['port'], 12345)
        self.assertNotIn('--diffusion-model', data['args'])
        self.assertIn('--model-dir', data['args'])
        self.assertEqual(data['reserve_bytes'], 24)
        self.assertEqual(data['default_model'], 'qwen-image-21-turbo')
        self.assertEqual(data['mode'], 'native-model-management')
        self.assertNotIn('config', data)
        self.assertNotIn('/bin/sh', data['args'])

    def test_fixed_version_binary_required(self):
        with patch.object(ns, 'installed_binary', return_value=None):
            with self.assertRaises(ns.ServiceError): self.manager.recipe('audio')

    def test_ambiguous_binary_is_rejected(self):
        spec = ns.SPECS['audio']; folder = self.root / 'apps' / (spec['component'] + '-' + spec['version'])
        p = folder / spec['binary']; p.write_text('not executed'); p.chmod(0o755)
        self.assertIsNone(ns.installed_binary(self.root, spec))

    def test_missing_model_root_start_skips_without_creating(self):
        self.start_mocks(); self.manager.models = self.home / 'missing-models'
        self.manager.start()
        self.manager.ctl.assert_not_called()
        self.assertFalse(self.manager.models.exists())

    def test_unmounted_external_volume_recipe_rejected(self):
        self.manager.models = Path('/Volumes/TestDrive/models')
        with patch.object(ns, 'safe_root'), patch.object(ns.os.path, 'ismount', return_value=False):
            with self.assertRaisesRegex(ns.ServiceError, '未挂载'): self.manager.recipe('audio')

    def test_foreign_loaded_agent_is_never_stopped(self):
        with patch.object(self.manager, 'state', return_value=self.state(True, False, 9)), patch.object(self.manager, 'ctl') as ctl, patch.object(self.manager, 'write_api'):
            with self.assertRaises(ns.ServiceError): self.manager.stop()
            ctl.assert_not_called()

    def test_owned_agent_stop_verifies_exit(self):
        with patch.object(self.manager, 'state', side_effect=[self.state(True, True, 9), self.state()]), patch.object(self.manager, 'ctl', return_value=Mock(returncode=0)) as ctl, patch.object(self.manager, 'write_api'):
            self.manager.stop('audio')
            ctl.assert_called_once_with('bootout', self.manager.target('audio'))

    def test_stop_reports_unconfirmed_exit(self):
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 9)), patch.object(self.manager, 'ctl', return_value=Mock(returncode=0)), patch.object(self.manager, 'write_api'):
            with self.assertRaises(ns.ServiceError): self.manager.stop('audio')

    def test_missing_model_root_does_not_prevent_owned_stop(self):
        self.manager.models = self.home / 'missing'
        with patch.object(self.manager, 'state', side_effect=[self.state(True, True, 9), self.state()]), patch.object(self.manager, 'ctl', return_value=Mock(returncode=0)) as ctl:
            self.manager.stop('audio')
            self.assertEqual(ctl.call_count, 1)

    def test_stop_waits_for_delayed_launchd_removal(self):
        with patch.object(ns, 'STOP_TIMEOUT', 35), patch.object(ns.time, 'sleep') as sleep, patch.object(self.manager, 'state', side_effect=[self.state(True, True, 9), self.state(True, True, 9), self.state()]), patch.object(self.manager, 'ctl', return_value=Mock(returncode=0)), patch.object(self.manager, 'write_api'):
            self.manager.stop('audio')
        sleep.assert_called_once()
        self.assertIn('audio.cpp · 转写／YuE2：已停止', self.out.getvalue())

    def test_bootout_absent_but_original_process_alive_is_not_success(self):
        self.configure('sd-qwen')
        self.manager.process_identity.return_value = '501 Sun Oct 25 04:00:00 2026 sd-server'
        with patch.object(self.manager, 'state', side_effect=[self.state(True, True, 9), self.state()]), patch.object(self.manager, 'ctl', return_value=Mock(returncode=0)), patch.object(self.manager, 'write_api'):
            with self.assertRaisesRegex(ns.ServiceError, 'SD.cpp'):
                self.manager.stop('sd-qwen')
        self.assertTrue((self.manager.paths('sd-qwen')[0] / 'stopping.json').exists())
        self.assertIn('等待退出超时', self.out.getvalue())

    def test_pending_stop_survives_manager_restart_and_reused_pid_is_not_touched(self):
        self.configure('audio')
        self.manager.process_identity.return_value = '501 original startup command'
        self.manager.remember_stop('audio', 9)
        with patch.object(self.manager, 'ctl', return_value=Mock(returncode=1)) as ctl:
            st = self.manager.state('audio')
        self.assertTrue(st['stopping'])
        self.assertEqual(st['pid'], 9)
        self.manager.process_identity.return_value = '501 different startup command'
        with patch.object(self.manager, 'ctl', return_value=Mock(returncode=1)) as ctl:
            st = self.manager.state('audio')
        self.assertFalse(st['loaded'])
        self.assertIsNone(st['pid'])
        ctl.assert_called_once_with('print', self.manager.target('audio'))
        self.assertFalse((self.manager.paths('audio')[0] / 'stopping.json').exists())

    def test_retry_pending_stop_only_observes_and_never_sends_signal(self):
        self.configure('audio')
        self.manager.process_identity.return_value = '501 original'
        self.manager.remember_stop('audio', 9)
        with patch.object(ns, 'STOP_TIMEOUT', 35), patch.object(ns.time, 'sleep'), patch.object(self.manager, 'state', return_value=dict(loaded=False, owned=True, pid=9, stopping=True)), patch.object(self.manager, 'ctl') as ctl, patch.object(self.manager, 'write_api'):
            self.manager.process_identity.side_effect = ['501 original', None]
            self.manager.stop('audio')
        ctl.assert_not_called()

    def test_pending_marker_symlink_rejected(self):
        self.configure('audio')
        (self.manager.paths('audio')[0] / 'stopping.json').symlink_to(self.home / 'unknown')
        with self.assertRaises(ns.ServiceError): self.manager.pending_stop('audio')

    def test_process_probe_failure_is_not_exit(self):
        with patch.object(ns.subprocess, 'run', return_value=Mock(returncode=2, stdout='', stderr='bad ps')):
            with self.assertRaises(ns.ServiceError): REAL_PROCESS_IDENTITY(self.manager, 9)

    def test_process_probe_missing_pid(self):
        with patch.object(ns.subprocess, 'run', return_value=Mock(returncode=1, stdout='', stderr='')):
            self.assertIsNone(REAL_PROCESS_IDENTITY(self.manager, 9))

    def test_unstarted_stop_uses_display_names(self):
        with patch.object(self.manager, 'state', return_value=self.state()), patch.object(self.manager, 'write_api'):
            self.manager.stop()
        text = self.out.getvalue()
        self.assertIn('SD.cpp：未启动', text)
        self.assertIn('audio.cpp · 转写／YuE2：未启动', text)
        self.assertNotIn('sd-qwen：', text)

    def test_all_start_includes_existing_video_queue_without_managing_mlx_app(self):
        self.start_mocks(); self.manager.start()
        self.manager.queue_action.assert_called_once_with('start')
        calls = self.manager.ctl.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(ns.SPECS), {'sd-qwen', 'audio'})
        self.assertTrue(all(c.args[0] == 'bootstrap' for c in calls))
        self.assertFalse(any('llama' in str(c) or 'mlx' in str(c).lower() for c in calls))

    def test_queue_single_start_does_not_scan_models_or_start_native_engines(self):
        self.manager.models = self.home / 'missing-models'
        with patch.object(self.manager, '_start') as native:
            self.manager.start('mlx-video-queue')
        self.manager.queue_action.assert_called_once_with('start')
        self.manager.refresh_inventory.assert_not_called()
        native.assert_not_called()
        self.assertFalse(self.manager.models.exists())

    def test_queue_cli_and_menu_have_independent_start_and_status(self):
        for action in ('start', 'status', 'stop'):
            with self.subTest(action=action), patch.object(ns, 'NativeManager', return_value=self.manager):
                self.manager.queue_action.reset_mock()
                self.assertEqual(ns.main([action, '--service', 'mlx-video-queue']), 0)
                self.manager.queue_action.assert_called_once_with(action)
        for choice, action in ((b'8', 'start'), (b'9', 'status')):
            with self.subTest(choice=choice), patch.object(ns.os, 'read', side_effect=[choice, b'0']), patch.object(ns.subprocess, 'run', return_value=Mock(returncode=0)) as run, patch('service_manager.pause_menu'):
                self.manager.queue_action.reset_mock()
                ns.menu(self.manager)
                self.manager.queue_action.assert_called_once_with(action)
                self.assertIn('11235', run.call_args_list[0].args[0][2])

    def test_queue_failure_does_not_prevent_other_services_starting(self):
        self.manager.queue_action.side_effect = ns.ServiceError('队列失败')
        with patch.object(self.manager, '_start') as native, patch.object(self.manager, 'status'), patch.object(self.manager, 'write_api'):
            with self.assertRaisesRegex(ns.ServiceError, '队列失败'):
                self.manager.start()
        native.assert_called_once_with('all')

    def test_stop_all_attempts_queue_and_native_even_if_queue_fails(self):
        self.manager.queue_action.side_effect = ns.ServiceError('队列失败')
        with patch.object(self.manager, 'state', return_value=self.state()) as state:
            with self.assertRaises(ns.ServiceError):
                self.manager.stop()
        self.manager.queue_action.assert_called_once_with('stop')
        self.assertEqual([c.args[0] for c in state.call_args_list], list(ns.SPECS))

    def test_single_native_start_never_touches_queue(self):
        self.start_mocks()
        self.manager.start('audio')
        self.manager.queue_action.assert_not_called()

    def test_queue_logs_reuse_original_directory(self):
        with patch.object(ns.Path, 'home', return_value=self.home):
            folder = self.home / 'Library/Application Support/Mac AI Kit/mlx-video-queue'
            folder.mkdir(parents=True)
            (folder / 'stdout.log').write_text('queue log\n')
            self.manager.logs('mlx-video-queue')
        self.assertIn('queue log', self.out.getvalue())

    def test_queue_adapter_reuses_cli_and_propagates_failure(self):
        with patch.object(ns.subprocess, 'run', return_value=Mock(returncode=1, stdout='', stderr='occupied')) as run:
            with self.assertRaisesRegex(ns.ServiceError, 'occupied'):
                ns.NativeManager.queue_action(self.manager, 'start')
        self.assertEqual(run.call_args.args[0], [ns.sys.executable, str(ROOT / 'scripts/mlx_video_queue.py'), 'start'])

    def test_queue_json_is_in_all_and_individual_status(self):
        row = {'service': 'mlx-video-queue', 'api_healthy': True, 'workload': 'unknown'}
        self.manager.queue_action.return_value = row
        with patch.object(self.manager, 'state', return_value=self.state()):
            for selection, expected in [('all', ['sd-qwen', 'audio', 'mlx-video-queue']), ('mlx-video-queue', ['mlx-video-queue'])]:
                with self.subTest(selection=selection):
                    result = self.manager.status_json(selection)
                    self.assertEqual([r['service'] for r in result['services']], expected)
                    self.assertEqual(result['services'][-1], row)

    def test_queue_json_failure_keeps_native_results_and_returns_nonzero(self):
        self.manager.queue_action.side_effect = subprocess.TimeoutExpired('queue status', 60)
        with patch.object(ns, 'NativeManager', return_value=self.manager), patch.object(self.manager, 'state', return_value=self.state()):
            self.assertEqual(ns.main(['status', '--json']), 2)
        rows = json.loads(self.out.getvalue())['services']
        self.assertEqual(len(rows), 3)
        self.assertIn('error', rows[-1])
        self.assertIsNone(rows[-1]['loaded'])

    def test_all_human_status_keeps_native_results_when_queue_query_fails(self):
        self.manager.queue_action.side_effect = ns.ServiceError('队列状态未知')
        with patch.object(self.manager, 'state', return_value=self.state()):
            with self.assertRaises(ns.ServiceError):
                self.manager.status()
        self.assertIn('audio.cpp', self.out.getvalue())
        self.assertIn('SD.cpp', self.out.getvalue())

    def test_loaded_foreign_agent_start_not_replaced(self):
        self.start_mocks(); self.manager.state.return_value = self.state(True, False, 9)
        self.manager.start(); self.manager.ctl.assert_not_called()

    def test_port_conflicts_skip_all_without_bootstrap(self):
        self.start_mocks()
        with patch.object(ns, 'available_port', side_effect=ns.ServiceError('occupied')):
            self.manager.start()
        self.manager.ctl.assert_not_called()

    def test_duplicate_planned_ports_rejected(self):
        self.start_mocks()
        with patch.object(self.manager, 'options', return_value=('127.0.0.1', 23456)):
            with self.assertRaises(ns.ServiceError): self.manager.start()
        self.manager.ctl.assert_not_called()

    def test_memory_budget_failure_launches_nothing(self):
        self.start_mocks()
        with patch.object(ns, 'available_memory', return_value=1024):
            with self.assertRaises(ns.ServiceError): self.manager.start()
        self.manager.ctl.assert_not_called()

    def test_config_runtime_outside_repository_no_login_autostart(self):
        data = self.configure()
        base, settings, path = self.manager.paths('audio')
        plist = plistlib.loads(path.read_bytes())
        self.assertTrue(path.is_relative_to(self.root))
        self.assertFalse(path.is_relative_to(ROOT))
        self.assertEqual(plist['WorkingDirectory'], str(Path(data['binary']).parent))
        self.assertFalse(plist['KeepAlive'])
        self.assertNotIn('LaunchAgents', str(path))
        self.assertFalse((self.home / 'Library/LaunchAgents').exists())
        self.assertEqual(json.loads(settings.read_text())['models_dir'], str(self.models))
        self.assertEqual(json.loads(settings.read_text())['root'], str(self.root))
        self.assertTrue(self.manager.owned('audio'))

    def test_ownership_rejects_modified_program_arguments(self):
        self.configure(); path = self.manager.paths('audio')[2]
        data = plistlib.loads(path.read_bytes()); data['ProgramArguments'] = ['/bin/evil']
        path.write_bytes(plistlib.dumps(data)); self.assertFalse(self.manager.owned('audio'))

    def test_configure_refuses_foreign_existing_plist(self):
        self.configure(); path = self.manager.paths('audio')[2]; path.write_bytes(plistlib.dumps({'Label': 'foreign'}))
        with self.assertRaises(ns.ServiceError): self.manager.configure('audio', self.manager.recipe('audio'))

    def test_configure_rejects_symlink_log(self):
        self.configure(); base = self.manager.paths('audio')[0]
        (base / 'stdout.log').symlink_to(self.home / 'outside')
        with self.assertRaises(ns.ServiceError): self.manager.configure('audio', self.manager.recipe('audio'))

    def test_state_checks_launchctl_path_and_owned_metadata(self):
        self.configure(); path = self.manager.paths('audio')[2]
        with patch.object(self.manager, 'ctl', return_value=Mock(returncode=0, stdout=f'path = {path}\npid = 123\n')):
            self.assertTrue(self.manager.state('audio')['owned'])
        with patch.object(self.manager, 'ctl', return_value=Mock(returncode=0, stdout='path = /foreign/service.plist\npid = 123\n')):
            self.assertFalse(self.manager.state('audio')['owned'])

    def test_logs_sanitize_terminal_sequences_both_streams(self):
        base = self.manager.paths('audio')[0]; base.mkdir(parents=True)
        (base / 'stdout.log').write_text('hello\x1b[31mRED\x1b[0m\x1b]0;evil-title\x07\n')
        (base / 'stderr.log').write_text('failure\x00\x7f\n')
        self.manager.logs('audio')
        output = self.out.getvalue(); self.assertIn('helloRED', output); self.assertIn('failure', output)
        self.assertNotIn('\x1b', output); self.assertNotIn('evil-title', output); self.assertNotIn('\x00', output)

    def test_logs_follow_ctrl_c_does_not_stop_service(self):
        base = self.manager.paths('audio')[0]; base.mkdir(parents=True); (base / 'stdout.log').write_text('hello\n')
        with patch.object(ns.time, 'sleep', side_effect=KeyboardInterrupt), patch.object(self.manager, 'stop') as stop:
            self.manager.logs('audio', follow=True)
        stop.assert_not_called(); self.assertIn('服务保持运行', self.out.getvalue())

    def test_logs_reject_symlink_file(self):
        base = self.manager.paths('audio')[0]; base.mkdir(parents=True)
        (base / 'stdout.log').symlink_to(self.home / 'outside')
        with self.assertRaises(ns.ServiceError): self.manager.logs('audio')

    def test_logs_reject_symlink_services_parent(self):
        outside = self.home / 'outside'; (outside / 'audio').mkdir(parents=True)
        (outside / 'audio/stdout.log').write_text('private')
        self.manager.base.symlink_to(outside)
        with self.assertRaises((ns.ServiceError, ValueError)): self.manager.logs('audio')
        self.assertNotIn('private', self.out.getvalue())

    def test_api_preserves_all_existing_content_and_appends_missing_service(self):
        target = self.models / 'API.md'; target.write_text('handwritten before\n' + ns.API_BEGIN + '\nstale\n' + ns.API_END + '\nhandwritten after\n')
        self.configure()
        with patch.object(self.manager, 'state', return_value=self.state()): self.manager.write_api()
        text = target.read_text(); self.assertIn('handwritten before', text); self.assertIn('handwritten after', text)
        self.assertIn('stale', text); self.assertEqual(text.count(ns.API_BEGIN), 1)

    def test_api_rejects_inverted_markers_without_modifying(self):
        target = self.models / 'API.md'; original = ns.API_END + '\nkeep me\n' + ns.API_BEGIN; target.write_text(original)
        with self.assertRaises(ns.ServiceError): self.manager.write_api()
        self.assertEqual(target.read_text(), original)

    def test_api_rejects_symlink(self):
        target = self.models / 'API.md'; target.symlink_to(self.home / 'outside.md')
        with self.assertRaises(ns.ServiceError): self.manager.write_api()

    def test_health_is_readonly_local_and_closes_connection(self):
        connection = Mock(); response = Mock(status=200); response.read.return_value = b'{"status":"ok"}'; connection.getresponse.return_value = response
        with patch.object(ns.http.client, 'HTTPConnection', return_value=connection) as factory:
            self.assertTrue(self.manager.health('audio', {'port': 18080}))
        factory.assert_called_once_with('127.0.0.1', 18080, timeout=1)
        connection.request.assert_called_once_with('GET', '/health'); connection.close.assert_called_once()

    def test_health_does_not_accept_redirect(self):
        connection = Mock(); connection.getresponse.return_value = Mock(status=302, read=Mock(return_value=b''))
        with patch.object(ns.http.client, 'HTTPConnection', return_value=connection):
            self.assertFalse(self.manager.health('audio', {'port': 18080}))
        self.assertEqual(connection.request.call_count, 1)

    def test_health_not_queried_for_unowned_service(self):
        self.configure()
        with patch.object(self.manager, 'state', return_value=self.state(True, False, 4)), patch.object(self.manager, 'health') as health:
            self.manager.status()
        health.assert_not_called()

    def test_sd_health_uses_models_endpoint_without_generation(self):
        connection = Mock(); response = Mock(status=200); response.read.return_value = b'{"data":[]}'
        connection.getresponse.return_value = response
        with patch.object(ns.http.client, 'HTTPConnection', return_value=connection):
            self.assertTrue(self.manager.health('sd-qwen', {'port': 12345}))
        connection.request.assert_called_once_with('GET', '/v1/models')

    def test_memory_parser_counts_only_available_categories(self):
        output = 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 10.\nPages inactive: 20.\nPages speculative: 30.\nPages occupied by compressor: 999999.\n'
        with patch.object(ns.subprocess, 'run', return_value=Mock(returncode=0, stdout=output)):
            self.assertEqual(REAL_AVAILABLE_MEMORY(), 60 * 16384)

    def test_memory_parser_fails_closed_on_incomplete_stats(self):
        with patch.object(ns.subprocess, 'run', return_value=Mock(returncode=0, stdout='page size of 16384 bytes\nPages free: 10.')):
            with self.assertRaises(ns.ServiceError): REAL_AVAILABLE_MEMORY()

    def test_bootstrap_partial_failure_reports_without_stopping_success(self):
        self.start_mocks()
        self.manager.ctl.side_effect = [Mock(returncode=1, stderr='failed'), Mock(returncode=0)]
        with self.assertRaises(ns.ServiceError): self.manager.start()
        self.assertEqual(self.manager.ctl.call_count, 2)
        self.assertTrue(all(c.args[0] == 'bootstrap' for c in self.manager.ctl.call_args_list))

    def test_status_selection_only_checks_selected_service(self):
        with patch.object(self.manager, 'state', return_value=self.state()) as state:
            self.manager.status('audio')
        state.assert_called_once_with('audio')

    def test_cli_logs_requires_explicit_service(self):
        with patch.object(ns, 'NativeManager', return_value=self.manager), patch.object(self.manager, 'logs') as logs:
            self.assertEqual(ns.main(['logs']), 2)
            logs.assert_not_called()

    def test_cli_status_passes_selection_without_starting(self):
        with patch.object(ns, 'NativeManager', return_value=self.manager), patch.object(self.manager, 'status') as status, patch.object(self.manager, 'write_api'), patch.object(self.manager, 'start') as start:
            self.assertEqual(ns.main(['status', '--service', 'audio']), 0)
            status.assert_called_once_with('audio'); start.assert_not_called()

    def test_webui_requires_success_html_and_interactive_markup(self):
        cases = [
            (200, 'text/html; charset=utf-8', b'<html><script src="app.js"></script></html>', True),
            (200, 'text/html', b'<html><form></form></html>', True),
            (200, 'text/plain', b'healthy', False),
            (200, 'text/html', b'<html><p>healthy</p></html>', False),
            (200, 'application/json', b'{"status":"ok"}', False),
            (302, 'text/html', b'<html><script></script></html>', False),
            (503, 'text/html', b'<html><form></form></html>', False),
            (200, 'text/html', b'<script>healthy</script>', False),
        ]
        for status, mime, body, expected in cases:
            with self.subTest(status=status, mime=mime, body=body):
                response = Mock(status=status); response.read.return_value = body; response.getheader.return_value = mime
                connection = Mock(); connection.getresponse.return_value = response
                with patch.object(ns.http.client, 'HTTPConnection', return_value=connection) as factory:
                    self.assertEqual(self.manager.webui({'port': 18080}), expected)
                factory.assert_called_once_with('127.0.0.1', 18080, timeout=1)
                connection.request.assert_called_once_with('GET', '/')
                response.read.assert_called_once_with(262144)
                connection.close.assert_called_once()

    def test_webui_network_failure_returns_false_closes_connection(self):
        connection = Mock(); connection.request.side_effect = OSError('offline')
        with patch.object(ns.http.client, 'HTTPConnection', return_value=connection):
            self.assertFalse(self.manager.webui({'port': 18080}))
        connection.close.assert_called_once()

    def test_status_and_api_do_not_probe_webui_without_owned_healthy_pid(self):
        self.configure()
        for state, healthy in [(self.state(True, False, 4), True),
                               (self.state(True, True, None), True),
                               (self.state(True, True, 4), False)]:
            with self.subTest(state=state, healthy=healthy), patch.object(self.manager, 'state', return_value=state), patch.object(self.manager, 'health', return_value=healthy), patch.object(self.manager, 'webui') as webui:
                self.manager.status('audio'); self.manager.write_api()
                webui.assert_not_called()
        text = (self.models / 'API.md').read_text()
        self.assertIn('WebUI：未确认可用', text)
        self.assertNotIn('- WebUI：http://', text)

    def test_confirmed_webui_url_is_shown_only_after_probe(self):
        self.configure()
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 4)), patch.object(self.manager, 'health', return_value=True), patch.object(self.manager, 'webui', return_value=True) as webui:
            self.manager.status('audio'); self.manager.write_api()
        self.assertEqual(webui.call_count, 2)
        self.assertIn('WebUI：http://127.0.0.1:18080/', self.out.getvalue())
        self.assertIn('- WebUI：http://127.0.0.1:18080/', (self.models / 'API.md').read_text())

    def test_healthy_service_without_webui_does_not_claim_frontend(self):
        self.configure()
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 4)), patch.object(self.manager, 'health', return_value=True), patch.object(self.manager, 'webui', return_value=False):
            self.manager.status('audio'); self.manager.write_api()
        self.assertIn('WebUI：未确认可用', self.out.getvalue())
        self.assertNotIn('- WebUI：http://', (self.models / 'API.md').read_text())

    def test_health_metadata_is_backend_self_report_not_gpu_proof(self):
        self.configure()
        response = Mock(status=200); response.read.return_value = b'{"status":"ok","backend":"metal"}'
        connection = Mock(); connection.getresponse.return_value = response
        with patch.object(ns.http.client, 'HTTPConnection', return_value=connection), patch.object(self.manager, 'state', return_value=self.state(True, True, 4)), patch.object(self.manager, 'webui', return_value=False):
            self.manager.status('audio')
        self.assertEqual(self.manager.health_meta['audio']['backend'], 'metal')
        self.assertIn('不是全 GPU 运算证明', self.out.getvalue())

    def test_status_does_not_show_stale_backend_for_offline_service(self):
        self.configure(); self.manager.health_meta['audio'] = {'backend': 'old-metal'}
        with patch.object(self.manager, 'state', return_value=self.state()): self.manager.status('audio')
        self.assertNotIn('old-metal', self.out.getvalue())

    def test_non_macos_start_stop_fail_closed(self):
        with patch.object(ns.sys, 'platform', 'linux'):
            with self.assertRaises(ns.ServiceError): self.manager.start()
            with self.assertRaises(ns.ServiceError): self.manager.stop()

    def test_old_sd_config_owned_but_running_not_restarted(self):
        data = self.configure('sd-qwen')
        data['version'] = 'master-975-95b3090'
        data.pop('mode')
        self.manager.configure('sd-qwen', data)
        self.assertTrue(self.manager.owned('sd-qwen'))
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 12)), patch.object(self.manager, 'ctl') as ctl, patch.object(self.manager, 'status'), patch.object(self.manager, 'write_api'):
            self.manager.start('sd-qwen')
        ctl.assert_not_called()
        self.assertIn('请先停止 SD.cpp', self.out.getvalue())

    def test_stopped_old_sd_config_can_upgrade(self):
        data = self.configure('sd-qwen')
        data['version'] = 'master-975-95b3090'; data.pop('mode')
        self.manager.configure('sd-qwen', data)
        self.start_mocks()
        self.manager.start('sd-qwen')
        self.assertEqual(self.manager.read_settings('sd-qwen')['mode'], 'native-model-management')

    def test_krea_runtime_root_includes_nested_vae_without_copy(self):
        folder = self.models / 'images/Krea-2'
        with patch.object(self.manager, 'model', side_effect=[ns.ServiceError('missing qwen'), ({'path': folder}, 123)]):
            data = self.manager.recipe('sd-qwen')
        self.assertEqual(data['models'], ['krea2-turbo'])
        self.assertIn(str(folder / 'dependencies/Wan_2.1_ComfyUI_repackaged/split_files'), data['args'])
        self.assertFalse(folder.exists())

    def test_license_dependency_is_downloaded_but_not_runtime_required(self):
        real = json.loads((ROOT / 'catalog.json').read_text())
        license = next(m for m in real['models'] if m['id'] == 'qwen21-license')
        self.assertFalse(license['runtime_required'])
        self.assertIn('LICENSE', license['expected_files'])
        self.assertIn('LICENSE', license['include'])
        self.catalog['models'].append(license)
        self.catalog['models'][0]['requires'] = ['qwen21-license']
        data = self.manager.recipe('sd-qwen')
        self.assertEqual(data['models'], ['qwen21-turbo'])
        self.assertEqual(data['default_model'], 'qwen-image-21-turbo')
        self.assertEqual(data['reserve_bytes'], 24)

    def test_license_fix_does_not_ignore_missing_required_encoder(self):
        self.catalog['models'][0]['requires'] = ['qwen21-text']
        self.catalog['models'].append(dict(id='qwen21-text', subdir='encoder', expected_files={'encoder.safetensors': {'bytes':4}}))
        with self.assertRaises(ns.ServiceError): self.manager.model('qwen21-turbo')

    def test_delayed_audio_health_is_waited_without_duplicate_bootstrap(self):
        data = self.configure('audio')
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 42)), patch.object(self.manager, 'health', side_effect=[False, False, True]) as health, patch.object(ns.time, 'sleep'):
            ns.NativeManager.wait_ready(self.manager, 'audio', data)
        self.assertEqual(health.call_count, 3)
        self.assertEqual(self.out.getvalue().count('正在等待 API'), 1)

    def test_wait_timeout_is_failure_not_ready(self):
        data = self.configure('audio')
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 42)), patch.object(self.manager, 'health', return_value=False), patch.object(ns, 'START_TIMEOUT', 0):
            with self.assertRaisesRegex(ns.ServiceError, '超时'):
                ns.NativeManager.wait_ready(self.manager, 'audio', data)

    def test_wait_stops_when_owned_process_exits(self):
        data = self.configure('audio')
        with patch.object(self.manager, 'state', return_value=self.state()), patch.object(self.manager, 'health') as health:
            with self.assertRaisesRegex(ns.ServiceError, '已退出'):
                ns.NativeManager.wait_ready(self.manager, 'audio', data)
        health.assert_not_called()

    def test_start_checks_existing_health_without_restarting(self):
        self.configure('audio'); self.start_mocks()
        self.manager.state.return_value = self.state(True, True, 42)
        self.manager.start('audio')
        self.manager.wait_ready.assert_called_once()
        self.manager.ctl.assert_not_called()

    def test_existing_unhealthy_service_failure_is_reported(self):
        self.configure('audio'); self.start_mocks()
        self.manager.state.return_value = self.state(True, True, 42)
        self.manager.wait_ready.side_effect = ns.ServiceError('等待 API 就绪超时')
        with self.assertRaisesRegex(ns.ServiceError, '尚未就绪'): self.manager.start('audio')
        self.manager.ctl.assert_not_called()
        self.manager.write_api.assert_called_once()

    def test_ctrl_c_during_start_only_exits_wait_and_updates_status(self):
        self.start_mocks()
        self.manager.wait_ready.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt): self.manager.start('audio')
        self.assertEqual([c.args[0] for c in self.manager.ctl.call_args_list], ['bootstrap'])
        self.manager.status.assert_called_once()
        self.manager.write_api.assert_called_once()
        self.assertIn('服务保持运行', self.out.getvalue())

    def test_sd_loading_existing_model_waits_without_load_request(self):
        data = self.manager.recipe('sd-qwen')
        with patch.object(self.manager, 'sd_status', side_effect=[{'state':'loading'}, {'state':'ready'}]) as status, patch.object(ns.time, 'sleep'), patch.object(ns, 'sd_request') as request:
            self.manager.wait_sd_loading(data)
        self.assertEqual(status.call_count, 2)
        request.assert_not_called()

    def test_sd_load_wait_timeout_does_not_report_success(self):
        data = self.manager.recipe('sd-qwen')
        with patch.object(self.manager, 'sd_status', return_value={'state':'loading'}), patch.object(ns, 'SD_LOAD_TIMEOUT', 0):
            with self.assertRaisesRegex(ns.ServiceError, '超时'): self.manager.wait_sd_loading(data)
        self.assertNotIn('已加载', self.out.getvalue())

    def test_sd_api_documents_native_switching_not_generation_model_routing(self):
        self.configure('sd-qwen')
        with patch.object(self.manager, 'state', return_value=self.state()):
            self.manager.write_api()
        text = (self.models / 'API.md').read_text()
        for word in ('models/load', 'models/unload', 'model-files', 'model_busy', 'sd_models.py', '不会自动切换'):
            self.assertIn(word, text)

    def test_default_qwen_load_waits_for_ready(self):
        data = self.manager.recipe('sd-qwen')
        files = [{'relative_path': path, 'category': path.split('/')[0], 'id': path} for path in (
            'diffusion_models/qwen_image_2.1_turbo_bf16.safetensors',
            'text_encoders/qwen3vl_8b_bf16.safetensors', 'vae/qwen_image_2.1_vae_bf16.safetensors')]
        with patch.object(ns, 'sd_request', side_effect=[{'state':'empty'}, {'files':files}, {'state':'ready','recipe':'qwen-image-21-turbo'}]) as request:
            ns.NativeManager.load_default_sd(self.manager, data)
        self.assertEqual([c.args[1] for c in request.call_args_list], ['models/status','model-files','models/load'])
        self.assertIn('已加载', self.out.getvalue())

    def test_default_qwen_does_not_replace_current_or_missing(self):
        data = self.manager.recipe('sd-qwen')
        with patch.object(ns, 'sd_request', return_value={'state':'ready','recipe':'krea2-turbo'}) as request:
            ns.NativeManager.load_default_sd(self.manager, data)
        self.assertEqual(request.call_count, 1)
        data['default_model'] = None
        with patch.object(ns, 'sd_request') as request:
            ns.NativeManager.load_default_sd(self.manager, data)
        request.assert_not_called()

    def test_default_qwen_no_false_success_after_load_failure(self):
        data = self.manager.recipe('sd-qwen')
        with patch.object(ns, 'sd_request', side_effect=[{'state':'empty'}, {'files':[]}]):
            with self.assertRaises(ValueError): ns.NativeManager.load_default_sd(self.manager, data)
        self.assertNotIn('已加载', self.out.getvalue())

    def test_old_sd_api_does_not_advertise_unsupported_switch(self):
        data = self.configure('sd-qwen'); data.pop('mode'); data['version'] = 'master-975-95b3090'
        self.manager.configure('sd-qwen', data)
        with patch.object(self.manager, 'state', return_value=self.state()): self.manager.write_api()
        text = (self.models / 'API.md').read_text()
        self.assertIn('尚未启用原生切换', text)
        self.assertNotIn('models/load', text)


    def test_stop_never_writes_api_or_scans_inventory(self):
        target = self.models / 'API.md'; original = b'manual\r\n'; target.write_bytes(original)
        before = target.stat().st_mtime_ns
        with patch.object(self.manager, 'state', return_value=self.state()), patch.object(self.manager, 'write_api') as write:
            self.manager.stop()
        write.assert_not_called(); self.manager.refresh_inventory.assert_not_called()
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_status_cli_never_writes_api(self):
        with patch.object(ns, 'NativeManager', return_value=self.manager), patch.object(self.manager, 'status') as status, patch.object(self.manager, 'write_api') as write:
            self.assertEqual(ns.main(['status']), 0)
        status.assert_called_once_with('all'); write.assert_not_called()

    def test_start_documents_are_idempotent_when_health_changes(self):
        self.configure()
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 4)), patch.object(self.manager, 'health', return_value=True), patch.object(self.manager, 'webui', return_value=True):
            self.manager.write_api()
        target = self.models / 'API.md'; original = target.read_bytes(); before = target.stat().st_mtime_ns
        self.out.truncate(0); self.out.seek(0)
        with patch.object(self.manager, 'state', return_value=self.state()): self.manager.write_api()
        self.assertEqual(target.read_bytes(), original); self.assertEqual(target.stat().st_mtime_ns, before)
        self.assertNotIn('已更新', self.out.getvalue())
        self.assertIn('不是实时状态', original.decode())

    def test_start_adds_new_service_without_replacing_old_text(self):
        self.configure()
        with patch.object(self.manager, 'state', return_value=self.state()): self.manager.write_api()
        target = self.models / 'API.md'; original = target.read_bytes()
        self.configure('sd-qwen')
        with patch.object(self.manager, 'state', return_value=self.state()): self.manager.write_api()
        updated = target.read_bytes(); self.assertTrue(updated.startswith(original)); self.assertIn(b'12345', updated)
        with patch.object(self.manager, 'state', return_value=self.state()): self.manager.write_api()
        self.assertEqual(target.read_bytes(), updated)

    def test_start_can_add_newly_verified_webui_without_rewriting_old_section(self):
        self.configure()
        with patch.object(self.manager, 'state', return_value=self.state()): self.manager.write_api()
        target = self.models / 'API.md'; original = target.read_bytes()
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 4)), patch.object(self.manager, 'health', return_value=True), patch.object(self.manager, 'webui', return_value=True): self.manager.write_api()
        updated = target.read_bytes(); self.assertTrue(updated.startswith(original)); self.assertIn('- WebUI：http://127.0.0.1:18080/', updated.decode())

    def test_inventory_start_preserves_existing_sections_and_only_adds_missing(self):
        import api_inventory
        target = self.models / 'API.md'; original = 'manual\n#### 原有模型\n\n- 标识：existing\n手改内容\n'; target.write_text(original)
        document = '#### 原有模型\n\n- 标识：existing\n不应替换\n\n#### 新模型\n\n- 标识：new-model\n调用说明\n\n### 本次未发现\nother\n'
        with patch.object(api_inventory, 'inventory', return_value=(self.models, self.root, [], [])), patch.object(api_inventory, 'render', return_value=document):
            ns.NativeManager.refresh_inventory(self.manager)
            updated = target.read_text(); ns.NativeManager.refresh_inventory(self.manager)
        self.assertTrue(updated.startswith(original)); self.assertNotIn('不应替换', updated)
        self.assertIn('调用说明', updated); self.assertNotIn('本次未发现', updated)
        self.assertEqual(target.read_text(), updated)


    def test_combined_budget_can_fail_while_either_single_target_fits(self):
        self.start_mocks()
        recipes = {sid: self.manager.recipe(sid) for sid in ns.SPECS}
        recipes['sd-qwen']['reserve_bytes'] = int(60.4 * 1024**3)
        recipes['audio']['reserve_bytes'] = int(21.8 * 1024**3)
        with patch.object(self.manager, 'recipe', side_effect=lambda sid: recipes[sid]), patch.object(ns, 'available_memory', return_value=90 * 1024**3):
            with self.assertRaisesRegex(ns.ServiceError, '仅启动音频.*仅启动制图'):
                self.manager.start()
            self.manager.ctl.assert_not_called()
            self.assertIn('本次保守预算 90.2 GiB', self.out.getvalue())
            for sid in ('audio', 'sd-qwen'):
                with self.subTest(service=sid):
                    self.manager.ctl.reset_mock()
                    self.manager.start(sid)
                    self.manager.ctl.assert_called_once()
                    self.assertEqual(self.manager.ctl.call_args.args[0], 'bootstrap')
                    self.assertIn(str(self.manager.paths(sid)[2]), self.manager.ctl.call_args.args)

    def test_audio_start_never_inspects_or_waits_for_unselected_sd(self):
        def audio_only(sid, **kwargs):
            self.assertEqual(sid, 'audio')
            return self.state()
        recipe = self.manager.recipe
        with patch.object(self.manager, 'state', side_effect=audio_only), patch.object(self.manager, 'ctl', return_value=Mock(returncode=0)), patch.object(self.manager, 'recipe', wraps=recipe) as checked:
            self.manager.start('audio')
        self.assertEqual([call.args[0] for call in checked.call_args_list], ['audio'])
        self.manager.wait_ready.assert_called_once()
        self.assertEqual(self.manager.wait_ready.call_args.args[0], 'audio')
        self.manager.load_default_sd.assert_not_called()
        self.assertFalse(self.manager.paths('sd-qwen')[0].exists())
        self.assertNotIn('SD.cpp：', self.out.getvalue())
        self.assertNotIn('12345', (self.models / 'API.md').read_text())

    def test_sd_start_ignores_stopped_audio_broken_settings_and_models(self):
        self.configure('audio')
        settings = self.manager.paths('audio')[1]
        settings.write_text('not valid JSON')
        before = {p: p.read_bytes() for p in settings.parent.iterdir() if p.is_file()}
        for folder in ('asr-qwen3-1.7b-f16', 'yue2-audio-cpp-bf16'):
            for path in (self.models / folder).iterdir(): path.unlink()
        recipe = self.manager.recipe
        with patch.object(self.manager, 'state', return_value=self.state()), patch.object(self.manager, 'ctl', return_value=Mock(returncode=0)) as ctl, patch.object(self.manager, 'recipe', wraps=recipe) as checked:
            self.manager.start('sd-qwen')
        self.assertEqual([call.args[0] for call in checked.call_args_list], ['sd-qwen'])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        ctl.assert_called_once()
        self.manager.load_default_sd.assert_called_once()
        self.assertNotIn('audio.cpp：', self.out.getvalue())
        self.assertNotIn('18080', (self.models / 'API.md').read_text())

    def test_single_start_refuses_its_own_insufficient_budget_without_configuration(self):
        self.start_mocks()
        with patch.object(ns, 'available_memory', return_value=26.3 * 1024**3), patch.object(self.manager, 'configure') as configure:
            for sid in ns.SPECS:
                data = self.manager.recipe(sid)
                data['reserve_bytes'] = 24 * 1024**3
                with self.subTest(service=sid), patch.object(self.manager, 'recipe', return_value=data):
                    with self.assertRaisesRegex(ns.ServiceError, '未启动本次待启动服务'):
                        self.manager.start(sid)
        configure.assert_not_called()
        self.manager.ctl.assert_not_called()
        self.assertNotIn('或单独启动音频', self.out.getvalue())

    def test_running_lazy_audio_reserve_is_preserved_without_wait_or_reconfiguration(self):
        data = self.configure('audio')
        data['reserve_bytes'] = 21 * 1024**3
        self.manager.configure('audio', data)
        before = {p: p.read_bytes() for p in self.manager.paths('audio')[0].iterdir() if p.is_file()}
        def states(sid, **kwargs):
            return self.state(True, True, 42) if sid == 'audio' else self.state()
        with patch.object(self.manager, 'state', side_effect=states), patch.object(self.manager, 'ctl') as ctl, patch.object(ns, 'available_memory', return_value=26.3 * 1024**3), patch.object(self.manager, 'status'), patch.object(self.manager, 'write_api'):
            with self.assertRaisesRegex(ns.ServiceError, '预算不足'):
                self.manager.start('sd-qwen')
        self.assertIn('另为尚未停止的音频保留 21.0 GiB', self.out.getvalue())
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.manager.wait_ready.assert_not_called()
        ctl.assert_not_called()

    def test_stopping_audio_is_not_stopped_again_or_reconfigured_by_sd_start(self):
        self.configure('audio')
        before = {p: p.read_bytes() for p in self.manager.paths('audio')[0].iterdir() if p.is_file()}
        def states(sid, **kwargs):
            return dict(self.state(False, True, 42), stopping=True) if sid == 'audio' else self.state()
        with patch.object(self.manager, 'state', side_effect=states), patch.object(self.manager, 'ctl', return_value=Mock(returncode=0)) as ctl, patch.object(self.manager, 'status'), patch.object(self.manager, 'write_api'):
            self.manager.start('sd-qwen')
        self.assertEqual([call.args[0] for call in ctl.call_args_list], ['bootstrap'])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(self.manager.wait_ready.call_args.args[0], 'sd-qwen')
        self.assertIn('音频保留', self.out.getvalue())

    def test_readonly_audio_state_keeps_stale_stop_record(self):
        self.configure('audio')
        self.manager.process_identity.return_value = 'original process'
        self.manager.remember_stop('audio', 42)
        path = self.manager.paths('audio')[0] / 'stopping.json'
        original = path.read_bytes()
        self.manager.process_identity.return_value = None
        with patch.object(self.manager, 'ctl', return_value=Mock(returncode=1)):
            state = self.manager.state('audio', clean_stale_stop=False)
        self.assertFalse(state['loaded'])
        self.assertFalse(state['owned'])
        self.assertEqual(path.read_bytes(), original)

    def test_invalid_active_audio_reserve_fails_closed_without_touching_audio(self):
        data = self.configure('audio')
        for bad in (None, 0, -1, True, '100'):
            with self.subTest(reserve=bad):
                data['reserve_bytes'] = bad
                self.manager.configure('audio', data)
                def states(sid, **kwargs):
                    return self.state(True, True, 42) if sid == 'audio' else self.state()
                with patch.object(self.manager, 'state', side_effect=states), patch.object(self.manager, 'ctl') as ctl, patch.object(self.manager, 'status'), patch.object(self.manager, 'write_api'):
                    with self.assertRaisesRegex(ns.ServiceError, '预算记录无效'):
                        self.manager.start('sd-qwen')
                ctl.assert_not_called()

    def test_start_final_status_and_api_keep_selected_scope_on_interrupt(self):
        self.start_mocks()
        self.manager.wait_ready.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt): self.manager.start('sd-qwen')
        self.manager.status.assert_called_once_with('sd-qwen')
        self.manager.write_api.assert_called_once_with('sd-qwen')
        self.manager.refresh_inventory.assert_called_once_with('sd-qwen')

    def test_single_inventory_does_not_inspect_other_service_or_mlx_models(self):
        import api_inventory
        original = api_inventory.inspect_model
        for sid in ns.SPECS:
            with self.subTest(service=sid):
                allowed = set(ns.SPECS[sid]['model_ids'])
                def inspect(model, base):
                    self.assertIn(model['id'], allowed)
                    return original(model, base)
                with patch.object(api_inventory, 'inspect_model', side_effect=inspect) as checked, patch.object(api_inventory, 'inventory', wraps=api_inventory.inventory) as inventory:
                    ns.NativeManager.refresh_inventory(self.manager, sid)
                self.assertTrue(checked.called)
                templates = inventory.call_args.args[2]
                self.assertEqual([runtime['id'] for runtime in templates['runtimes']], [ns.SPECS[sid]['component']])

    def test_single_inventory_keeps_needed_dependencies_but_not_unrelated_models(self):
        import api_inventory
        self.catalog['models'][0]['requires'] = ['image-dependency']
        self.catalog['models'].append(dict(id='image-dependency', subdir='image-dependency', expected_files={}))
        with patch.object(api_inventory, 'inventory', return_value=(self.models, self.root, [], [])) as inventory, patch.object(api_inventory, 'render', return_value=''):
            ns.NativeManager.refresh_inventory(self.manager, 'sd-qwen')
        ids = {model['id'] for model in inventory.call_args.args[1]['models']}
        self.assertEqual(ids, {'qwen21-turbo', 'image-dependency'})

    def test_selected_api_append_preserves_manual_bytes_and_other_service(self):
        self.configure('audio'); self.configure('sd-qwen')
        target = self.models / 'API.md'
        original = '我的说明\r\n### SD.cpp\r\n手写制图配置 http://127.0.0.1:12345\r\n'.encode()
        target.write_bytes(original)
        def audio_only(sid, **kwargs):
            self.assertEqual(sid, 'audio')
            return self.state()
        with patch.object(self.manager, 'state', side_effect=audio_only):
            self.manager.write_api('audio')
            after = target.read_bytes()
            self.manager.write_api('audio')
        self.assertTrue(after.startswith(original))
        self.assertIn(b'18080', after)
        self.assertNotIn(b'sdcpp/v1', after)
        self.assertEqual(target.read_bytes(), after)

    def test_cli_start_passes_each_selection(self):
        for sid in ('all', 'audio', 'sd-qwen'):
            with self.subTest(service=sid), patch.object(ns, 'NativeManager', return_value=self.manager), patch.object(self.manager, 'start') as start:
                self.assertEqual(ns.main(['start', '--service', sid]), 0)
                start.assert_called_once_with(sid)

    def test_menu_routes_start_all_audio_and_image_without_extra_actions(self):
        for choice, expected in (('1', ()), ('2', ('audio',)), ('3', ('sd-qwen',))):
            with self.subTest(choice=choice), patch.object(ns.os, 'read', side_effect=[choice.encode(), b'0']), patch.object(ns.subprocess, 'run', return_value=Mock(returncode=0)), patch('service_manager.pause_menu'), patch.object(self.manager, 'start') as start, patch.object(self.manager, 'stop') as stop:
                ns.menu(self.manager)
                start.assert_called_once_with(*expected)
                stop.assert_not_called()

    def test_menu_cancel_does_not_start_stop_or_write_api(self):
        with patch.object(ns.os, 'read', return_value=b'0'), patch.object(ns.subprocess, 'run', return_value=Mock(returncode=0)), patch.object(self.manager, 'start') as start, patch.object(self.manager, 'stop') as stop, patch.object(self.manager, 'write_api') as write:
            ns.menu(self.manager)
        start.assert_not_called(); stop.assert_not_called(); write.assert_not_called()

    def test_logs_leave_api_bytes_and_mtime_unchanged(self):
        target = self.models / 'API.md'; target.write_bytes(b'manual\r\n')
        before = target.stat().st_mtime_ns
        self.manager.logs('audio')
        self.assertEqual(target.read_bytes(), b'manual\r\n')
        self.assertEqual(target.stat().st_mtime_ns, before)
        self.manager.refresh_inventory.assert_not_called()


    def test_json_healthy_audio_never_claims_idle_or_stop_safe(self):
        self.configure('audio')
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 42)), patch.object(self.manager, 'health', return_value=True):
            result = self.manager.status_json('audio')
        self.assertEqual(len(result['services']), 1)
        row = result['services'][0]
        self.assertTrue(row['api_healthy'])
        self.assertEqual(row['workload'], 'unknown')
        self.assertEqual(row['stop_safety'], 'unknown')
        self.assertEqual(row['model_residency'], 'unknown')
        self.assertFalse(row['atomic_shutdown_barrier'])

    def test_json_sd_empty_is_not_global_idle(self):
        self.configure('sd-qwen')
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 42)), patch.object(self.manager, 'health', return_value=True), patch.object(self.manager, 'sd_status', return_value={'state':'empty'}):
            row = self.manager.status_json('sd-qwen')['services'][0]
        self.assertEqual(row['model_residency'], 'empty')
        self.assertEqual(row['workload'], 'unknown')
        self.assertEqual(row['stop_safety'], 'unknown')

    def test_json_unsupported_platform_does_not_claim_service_stopped(self):
        with patch.object(ns.sys, 'platform', 'linux'), patch.object(self.manager, 'state') as state:
            result = self.manager.status_json('audio')
        self.assertFalse(result['supported'])
        self.assertIsNone(result['services'][0]['loaded'])
        self.assertIn('error', result['services'][0])
        state.assert_not_called()

    def test_json_launchctl_query_failure_is_unknown_not_stopped(self):
        with patch.object(self.manager, 'ctl', return_value=Mock(returncode=1, stdout='', stderr='permission denied')):
            row = self.manager.status_json('audio')['services'][0]
        self.assertIn('error', row)
        self.assertIsNone(row['loaded'])
        self.assertEqual(row['workload'], 'unknown')

    def test_json_only_exact_missing_own_launchd_service_is_not_running(self):
        missing = 'Could not find service "' + self.manager.label('audio') + '" in domain for user gui: 501'
        with patch.object(self.manager, 'ctl', return_value=Mock(returncode=113, stdout='', stderr=missing)):
            row = self.manager.status_json('audio')['services'][0]
        self.assertFalse(row['loaded'])
        self.assertEqual(row['workload'], 'not_running')
        for code, message in ((1, missing), (113, missing.replace(self.manager.label('audio'), 'other.service')), (113, 'unknown domain')):
            with self.subTest(code=code, message=message), patch.object(self.manager, 'ctl', return_value=Mock(returncode=code, stdout='', stderr=message)):
                row = self.manager.status_json('audio')['services'][0]
                self.assertIn('error', row)
                self.assertEqual(row['workload'], 'unknown')

    def test_json_cli_is_pure_json_and_preserves_api_bytes_and_mtime(self):
        target = self.models / 'API.md'; target.write_bytes(b'manual\r\n')
        before = target.stat().st_mtime_ns
        with patch.object(ns, 'NativeManager', return_value=self.manager), patch.object(self.manager, 'state', return_value=self.state()), patch.object(self.manager, 'recipe') as recipe:
            self.assertEqual(ns.main(['status', '--service', 'audio', '--json']), 0)
        document = json.loads(self.out.getvalue())
        self.assertEqual([row['service'] for row in document['services']], ['audio'])
        self.assertEqual(target.read_bytes(), b'manual\r\n')
        self.assertEqual(target.stat().st_mtime_ns, before)
        recipe.assert_not_called()
        self.manager.refresh_inventory.assert_not_called()

    def test_json_cli_error_is_parseable_and_nonzero_without_start(self):
        with patch.object(ns, 'NativeManager', return_value=self.manager), patch.object(self.manager, 'start') as start:
            self.assertEqual(ns.main(['start', '--json']), 2)
        self.assertIn('error', json.loads(self.out.getvalue()))
        start.assert_not_called()

    def test_json_query_failure_returns_nonzero_and_no_stop(self):
        with patch.object(ns, 'NativeManager', return_value=self.manager), patch.object(self.manager, 'ctl', return_value=Mock(returncode=1, stdout='', stderr='denied')), patch.object(self.manager, 'stop') as stop:
            self.assertEqual(ns.main(['status', '--service', 'audio', '--json']), 2)
        self.assertIn('error', json.loads(self.out.getvalue())['services'][0])
        stop.assert_not_called()

    def test_json_pending_stop_is_readonly_and_not_safe_for_video(self):
        self.configure('audio')
        with patch.object(self.manager, 'state', return_value=dict(self.state(False, True, 42), stopping=True)) as state, patch.object(self.manager, 'health') as health:
            row = self.manager.status_json('audio')['services'][0]
        state.assert_called_once_with('audio', clean_stale_stop=False, strict=True)
        self.assertTrue(row['stopping'])
        self.assertEqual(row['stop_safety'], 'unknown')
        health.assert_not_called()

    def test_start_appends_agent_policy_even_without_new_model_sections(self):
        import api_inventory
        target = self.models / 'API.md'; original = b'manual\r\n'; target.write_bytes(original)
        with patch.object(api_inventory, 'inventory', return_value=(self.models, self.root, [], [])), patch.object(api_inventory, 'render', return_value=''):
            ns.NativeManager.refresh_inventory(self.manager, 'audio')
            first = target.read_bytes(); before = target.stat().st_mtime_ns
            ns.NativeManager.refresh_inventory(self.manager, 'audio')
        self.assertTrue(first.startswith(original))
        self.assertIn(b'MAC-AI-KIT:AGENT-WORKFLOW:', first)
        self.assertEqual(target.read_bytes(), first)
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_existing_service_start_adds_policy_when_inventory_refresh_is_mocked(self):
        self.configure('audio')
        target = self.models / 'API.md'; original = b'manual\r\n'; target.write_bytes(original)
        with patch.object(self.manager, 'state', return_value=self.state(True, True, 42)), patch.object(self.manager, 'health', return_value=False), patch.object(self.manager, 'status'):
            self.manager.start('audio')
        self.assertTrue(target.read_bytes().startswith(original))
        self.assertIn('Agent 资源协调', target.read_text())


if __name__ == '__main__':
    unittest.main()
