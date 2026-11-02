"""AIPerf Benchmark 离线校验：只解析参数与本地产物，不联网、不启动 aiperf。"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('aiperf_bench', ROOT / 'scripts/aiperf_benchmark.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class EndpointTests(unittest.TestCase):
    def test_endpoint_adds_and_keeps_v1(self):
        self.assertEqual(bench.endpoint('http://127.0.0.1:11234'), bench.DEFAULT_URL)
        self.assertEqual(bench.endpoint('http://127.0.0.1:11234/'), bench.DEFAULT_URL)
        self.assertEqual(bench.endpoint('https://host/v1/'), 'https://host/v1')

    def test_endpoint_rejects_unsafe_values(self):
        for value in ('ftp://example.com', 'http://user:pass@x/v1', 'http://x?key=a',
                      'http://x#x', 'http://x?', 'http://x#', 'http://x:99999',
                      'http://x\\evil', 'http://x/\x1b', 'http://x/ a'):
            with self.assertRaises(ValueError):
                bench.endpoint(value)

    def test_aiperf_base_strips_redundant_v1(self):
        self.assertEqual(bench.aiperf_base('http://127.0.0.1:11234/v1'), 'http://127.0.0.1:11234')
        self.assertEqual(bench.aiperf_base('http://127.0.0.1:11234'), 'http://127.0.0.1:11234')
        self.assertEqual(bench.aiperf_base('http://host/v1/chat/completions'), 'http://host/v1/chat/completions')
        self.assertEqual(bench.aiperf_base('http://host/proxy/v1'), 'http://host/proxy')


class ConcurrencyTests(unittest.TestCase):
    def test_parses_and_deduplicates(self):
        self.assertEqual(bench.concurrencies('1,4,8'), (1, 4, 8))
        self.assertEqual(bench.concurrencies(' 8 , 1 , 4 , 8 '), (8, 1, 4))

    def test_rejects_invalid(self):
        for value in ('', '0', '-1', 'a,b', '1,,4', '1,2048', '1,2,3,4,5,6,7,8,9'):
            with self.assertRaises(ValueError):
                bench.concurrencies(value)


class CommandTests(unittest.TestCase):
    def test_default_command_warmup_single_concurrency(self):
        cmd = bench.command('/bin/uvx', 'http://127.0.0.1:11234', 'model-x', '/tmp/tok',
                            (1, 4, 8), 40, None, 1, 600, Path('/tmp/artifacts'), True)
        self.assertEqual(cmd[:6], ['/bin/uvx', '--no-config', '--from', bench.PACKAGE, 'aiperf', 'profile'])
        self.assertIn('aiperf==0.13.0', cmd)
        self.assertEqual(cmd[cmd.index('--url') + 1], 'http://127.0.0.1:11234')
        self.assertEqual(cmd[cmd.index('--model') + 1], 'model-x')
        self.assertEqual(cmd[cmd.index('--concurrency') + 1], '1,4,8')
        self.assertEqual(cmd[cmd.index('--tokenizer') + 1], '/tmp/tok')
        self.assertEqual(cmd[cmd.index('--request-count') + 1], '40')
        self.assertEqual(cmd[cmd.index('--warmup-request-count') + 1], '1')
        self.assertEqual(cmd[cmd.index('--warmup-concurrency') + 1], '1')
        self.assertIn('--auto-plot', cmd)
        self.assertEqual(cmd[cmd.index('--endpoint-type') + 1], 'chat')
        self.assertIn('--streaming', cmd)

    def test_duration_mode_and_no_plot(self):
        cmd = bench.command('/bin/uvx', 'http://h', 'm', 'builtin', (2,), 40, 30.0, 0, 120,
                            Path('/tmp/a'), False)
        self.assertIn('--benchmark-duration', cmd)
        self.assertNotIn('--request-count', cmd)
        self.assertNotIn('--warmup-request-count', cmd)
        self.assertNotIn('--warmup-concurrency', cmd)
        self.assertNotIn('--auto-plot', cmd)
        self.assertEqual(cmd[cmd.index('--request-timeout-seconds') + 1], '120')

    def test_environment_drops_ambient_credentials(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'x', 'HF_TOKEN': 'x', 'HTTPS_PROXY': 'x',
                                     'PYTHONPATH': 'x', 'UV_INDEX_URL': 'x', 'AWS_SECRET_ACCESS_KEY': 'x'}):
            env = bench.clean_environment(Path('/tmp/isolated'), Path('/tmp/cache'))
        self.assertFalse(set(env) & {'OPENAI_API_KEY', 'HF_TOKEN', 'HTTPS_PROXY', 'PYTHONPATH',
                                     'UV_INDEX_URL', 'AWS_SECRET_ACCESS_KEY'})
        self.assertEqual(env['HF_HUB_DISABLE_IMPLICIT_TOKEN'], '1')
        self.assertEqual(env['UV_CACHE_DIR'], '/tmp/cache/uv')
        self.assertEqual(env['MPLBACKEND'], 'Agg')


class SummaryTests(unittest.TestCase):
    def test_sweep_rows_parse_mean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'profile_export_aiperf_sweep.json').write_text(json.dumps({
                'per_combination_metrics': [
                    {'parameters': {'concurrency': 1},
                     'metrics': {'time_to_first_token': {'mean': 30.5}, 'request_latency': {'mean': 120.0}}},
                    {'parameters': {'concurrency': 8},
                     'metrics': {'request_latency': {'mean': 980.25}}},
                ]}), encoding='utf-8')
            rows = bench.sweep_rows(root)
        self.assertEqual([row['level'] for row in rows], [1, 8])
        self.assertEqual(rows[0]['metrics']['time_to_first_token'], 30.5)
        self.assertIsNone(rows[1]['metrics']['time_to_first_token'])
        self.assertEqual(rows[1]['metrics']['request_latency'], 980.25)

    def test_sweep_rows_ignores_unknown_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'profile_export_aiperf_sweep.json').write_text('{"unexpected": []}', encoding='utf-8')
            self.assertEqual(bench.sweep_rows(root), [])

    def test_run_rows_read_concurrency_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for level, avg in ((1, 100.0), (8, 800.0)):
                folder = root / ('concurrency_%d' % level)
                folder.mkdir()
                (folder / 'profile_export_aiperf.json').write_text(json.dumps({
                    'time_to_first_token': {'avg': avg / 4, 'p99': avg},
                    'request_latency': {'avg': avg}}), encoding='utf-8')
            rows = bench.run_rows(root)
        self.assertEqual([row['level'] for row in rows], [1, 8])
        self.assertEqual(rows[1]['metrics']['request_latency'], 800.0)
        self.assertAlmostEqual(rows[1]['metrics']['time_to_first_token'], 200.0)

    def test_minimal_or_broken_artifacts_do_not_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'concurrency_4').mkdir()
            (root / 'concurrency_4' / 'profile_export_aiperf.json').write_text('{broken', encoding='utf-8')
            self.assertEqual(bench.run_rows(root), [])
            # 解析失败不影响原始文件列出：报告仍应让用户看到产物本身。
            self.assertEqual([p.name for p in bench.collect_artifacts(root)], ['profile_export_aiperf.json'])


class ReportTests(unittest.TestCase):
    def test_report_escapes_and_has_no_external_assets(self):
        rows = [{'level': 4, 'label': 'concurrency=4', 'source': 'sweep',
                 'metrics': {'time_to_first_token': 210.456, 'request_latency': 980.0}}]
        meta = {'created': '2026-11-02 10:00:00 UTC', 'levels': '1, 4', 'stop': '每档 40 请求',
                'base': 'http://127.0.0.1:11234', 'model': '<script>x</script>', 'tokenizer': 'builtin',
                'warmup': '每档 1 次（并发 1）', 'exit_code': 0,
                'notes': ['结果不可与长上下文测试直接互比。']}
        with tempfile.TemporaryDirectory() as directory:
            page = bench.render_html(rows, [], meta, Path(directory))
        self.assertIn('210.46', page)
        self.assertIn('980.00', page)
        self.assertNotIn('<script>x</script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('不可与长上下文测试直接互比', page)
        self.assertNotIn('http://cdn', page)
        self.assertNotIn('<script src', page)

    def test_report_without_metrics_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            page = bench.render_html([], [], {'levels': '1', 'notes': []}, Path(directory))
        self.assertIn('未从 aiperf 产物中解析到汇总指标', page)


class ArgumentTests(unittest.TestCase):
    def test_duration_conflicts_with_request_count(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch('shutil.which', return_value='/bin/uvx'):
            with self.assertRaises(SystemExit):
                bench.main(['--duration', '30', '--request-count', '10'])
        self.assertIn('只能选一个', stderr.getvalue())

    def test_invalid_duration_rejected(self):
        for value in ('1', '0', 'abc', '100000'):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    bench.main(['--duration', value])

    def test_start_script_wiring(self):
        source = (ROOT / 'start.sh').read_text(encoding='utf-8')
        self.assertIn('aiperf-benchmark', source)
        self.assertIn('$SCRIPTS/aiperf_benchmark.py', source)
        self.assertIn("'AIPerf 并发压力测试'", source)
        self.assertIn('3) aiperf_benchmark', source)
        self.assertIn('aiperf-benchmark) aiperf_benchmark --help', source)


class RunSmokeTests(unittest.TestCase):
    """用假 uvx 验证完整流程：命令、日志转发、产物收集与报告，不联网。"""

    FAKE = '''#!/bin/bash
while [ $# -gt 0 ]; do
  if [ "$1" = "--artifact-dir" ]; then shift; dir="$1"; fi
  shift
done
mkdir -p "$dir/concurrency_1" "$dir/concurrency_8"
cat > "$dir/profile_export_aiperf_sweep.json" <<'JSON'
{"per_combination_metrics":[
 {"parameters":{"concurrency":1},"metrics":{"request_latency":{"mean":120.0},"time_to_first_token":{"mean":30.0}}},
 {"parameters":{"concurrency":8},"metrics":{"request_latency":{"mean":980.0},"time_to_first_token":{"mean":210.5}}}]}
JSON
echo "fake aiperf finished"
'''

    def test_mock_aiperf_writes_report_and_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'fake-uvx'
            fake.write_text(self.FAKE, encoding='utf-8')
            fake.chmod(0o755)
            output = root / 'reports' / 'run'
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = bench.run(str(fake), 'http://127.0.0.1:11234', 'model-x',
                                 {'kind': 'override', 'value': 'builtin', 'evidence': ['测试'],
                                  'approximate': True},
                                 (1, 8), 5, None, 1, 60, output, root / 'reports', False, '')
            self.assertEqual(code, 0)
            self.assertIn('fake aiperf finished', buffer.getvalue())
            page = (output / 'report.html').read_text(encoding='utf-8')
            self.assertIn('980.00', page)
            self.assertIn('210.50', page)
            record = json.loads((output / 'run.json').read_text(encoding='utf-8'))
            self.assertEqual(record['exit_code'], 0)
            self.assertEqual(record['warmup_concurrency'], 1)
            self.assertIn('artifacts/profile_export_aiperf_sweep.json', record['artifacts'])
            self.assertTrue((output / 'run.log').exists())


if __name__ == '__main__':
    unittest.main()
