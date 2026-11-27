"""只读队列状态与服务菜单的回归；不操作用户的 launchd。"""
import contextlib
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mlx_video_queue as queue


class QueueStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'queue'
        self.path = self.root / f'{queue.LABEL}.plist'
        self.target = f'gui/{queue.os.getuid()}/{queue.LABEL}'
        self.out = io.StringIO()

    def status(self, result, json_output=True):
        with patch.object(queue.sys, 'platform', 'darwin'), patch.object(queue.subprocess, 'run', return_value=result) as run, contextlib.redirect_stdout(self.out):
            code = queue.main(['status', '--state', str(self.root)] + (['--json'] if json_output else []))
        run.assert_called_once_with(['/bin/launchctl', 'print', self.target], capture_output=True, text=True, timeout=15)
        return code, json.loads(self.out.getvalue()) if json_output else self.out.getvalue()

    def configured(self, host='0.0.0.0', port=12398):
        self.root.mkdir(mode=0o700)
        self.path.write_bytes(plistlib.dumps(dict(Label=queue.LABEL, ProgramArguments=[sys.executable,
            str(self.root / 'mlx_video_queue.py'), 'run', '--state', str(self.root),
            '--host', host, '--port', str(port), '--upstream-port', '12397'])))
        return Mock(returncode=0, stdout=f'path = {self.path}\npid = 42\n', stderr='')

    def test_missing_service_is_readonly_and_names_default_port(self):
        result = Mock(returncode=113, stdout='', stderr=f'Could not find service "{queue.LABEL}" in domain for user gui: 501')
        code, row = self.status(result)
        self.assertEqual(code, 0)
        self.assertFalse(row['loaded'])
        self.assertEqual(row['port'], 11235)
        self.assertFalse(self.root.exists())
        self.assertEqual(row['workload'], 'unknown')

    def test_query_error_is_unknown_and_nonzero_not_stopped(self):
        code, row = self.status(Mock(returncode=1, stdout='', stderr='permission denied'))
        self.assertEqual(code, 2)
        self.assertIsNone(row['loaded'])
        self.assertIn('error', row)
        self.assertFalse(self.root.exists())

    def test_health_checks_saved_port_and_service_identity(self):
        result = self.configured()
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.return_value = queue.LABEL
        response.read.return_value = b'{"worker_alive":true}'
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        with patch('http.client.HTTPConnection', return_value=connection) as connect:
            code, row = self.status(result)
        self.assertEqual(code, 0)
        self.assertTrue(row['api_healthy'])
        self.assertTrue(row['owned'])
        self.assertEqual(row['pid'], 42)
        self.assertEqual(row['upstream_port'], 12397)
        self.assertEqual(row['workload'], 'unknown')
        self.assertFalse(row['atomic_shutdown_barrier'])
        connect.assert_called_once_with('127.0.0.1', 12398, timeout=1)
        connection.request.assert_called_once_with('GET', '/healthz')
        connection.close.assert_called_once()
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)

    def test_saved_localhost_is_accepted_and_probed(self):
        result = self.configured(host='localhost')
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.return_value = queue.LABEL
        response.read.return_value = b'{"worker_alive":true}'
        with patch('http.client.HTTPConnection', return_value=connection) as connect:
            code, row = self.status(result)
        assert isinstance(row, dict)
        self.assertEqual(code, 0, row)
        self.assertTrue(row['configured'])
        self.assertTrue(row['owned'])
        self.assertTrue(row['api_healthy'])
        self.assertEqual(row['host'], 'localhost')
        connect.assert_called_once_with('localhost', 12398, timeout=1)
        connection.request.assert_called_once_with('GET', '/healthz')
        connection.close.assert_called_once()

    def test_saved_empty_host_is_probed_via_loopback(self):
        result = self.configured(host='')
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.return_value = queue.LABEL
        response.read.return_value = b'{"worker_alive":true}'
        with patch('http.client.HTTPConnection', return_value=connection) as connect:
            code, row = self.status(result)
        assert isinstance(row, dict)
        self.assertEqual(code, 0, row)
        self.assertTrue(row['configured'])
        self.assertTrue(row['owned'])
        self.assertTrue(row['api_healthy'])
        self.assertEqual(row['host'], '')
        connect.assert_called_once_with('127.0.0.1', 12398, timeout=1)
        connection.request.assert_called_once_with('GET', '/healthz')
        connection.close.assert_called_once()

    def test_foreign_state_directory_is_not_probed_or_adopted(self):
        with patch('http.client.HTTPConnection') as connect:
            code, row = self.status(Mock(returncode=0, stdout='path = /other/queue.plist\npid = 42\n', stderr=''))
        self.assertEqual(code, 2)
        self.assertFalse(row['owned'])
        connect.assert_not_called()

    def test_human_status_empty_bind_host_has_usable_url(self):
        result = self.configured(host='')
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.return_value = queue.LABEL
        response.read.return_value = b'{"worker_alive":true}'
        with patch('http.client.HTTPConnection', return_value=connection):
            code, text = self.status(result, json_output=False)
        self.assertEqual(code, 0)
        self.assertIn('http://127.0.0.1:12398', text)


    def test_wrong_health_service_or_dead_worker_is_not_healthy(self):
        result = self.configured()
        for label, body in [('other-service', b'{"worker_alive":true}'), (queue.LABEL, b'{"worker_alive":false}')]:
            self.out = io.StringIO()
            connection = Mock()
            response = connection.getresponse.return_value
            response.status = 200
            response.getheader.return_value = label
            response.read.return_value = body
            with self.subTest(label=label, body=body), patch('http.client.HTTPConnection', return_value=connection):
                _, row = self.status(result)
            self.assertFalse(row['api_healthy'])

    def test_human_status_shows_queue_port_pid_and_log_path(self):
        result = self.configured()
        with patch('http.client.HTTPConnection', side_effect=OSError('offline')):
            code, text = self.status(result, json_output=False)
        self.assertEqual(code, 0)
        for value in ('MLX', '12398', '42', str(self.root), '未就绪'):
            self.assertIn(value, text)

    def test_json_cannot_start_service(self):
        with patch.object(queue, 'launch') as launch, contextlib.redirect_stdout(self.out):
            self.assertEqual(queue.main(['start', '--json']), 2)
        launch.assert_not_called()
        self.assertIn('error', json.loads(self.out.getvalue()))


if __name__ == '__main__':
    unittest.main()
