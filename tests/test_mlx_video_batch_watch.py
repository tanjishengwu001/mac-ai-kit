import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/mlx_video_batch_watch.py'
spec = importlib.util.spec_from_file_location('batch_watch', SCRIPT)
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)
A = 'a' * 32
B = 'b' * 32
C = 'c' * 32
VIDEO = b'\x00\x00\x00\x18ftypisom-test'


class BatchWatchTests(unittest.TestCase):
    def setUp(self):
        self.count = []
        parent = self

        class Server(BaseHTTPRequestHandler):
            def do_GET(self):
                parent.count.append(self.path)
                job = self.path.split('/')[4]
                if self.path.endswith('/video.mp4'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'video/mp4')
                    self.send_header('Content-Length', str(len(VIDEO) + (10 if job == C else 0)))
                    self.end_headers()
                    self.wfile.write(VIDEO)
                    return
                body = {'id': job, 'status': 'failed' if job == B else 'succeeded', 'error': 'example'}
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_watch(self, *args):
        return subprocess.run(['python3', str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=15)

    def receipt(self, folder, job_id):
        folder.mkdir(parents=True)
        (folder / '提交响应.json').write_text(json.dumps({'id': job_id}))

    def test_discovery_one_failure_does_not_block_download(self):
        self.receipt(self.root / 'shot-a', A)
        self.receipt(self.root / 'shot-b', B)
        result = self.run_watch('--watch-dir', self.root, '--base', self.base, '--once')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((self.root / 'shot-a' / f'{A}.mp4').read_bytes(), VIDEO)
        self.assertFalse((self.root / 'shot-b' / f'{B}.mp4').exists())
        meta = json.loads((self.root / 'shot-a' / f'{A}.mp4.job.json').read_text())
        self.assertEqual((meta['id'], meta['base'], meta['bytes']), (A, self.base, len(VIDEO)))
        self.assertIn('failed', result.stdout)
        calls = self.count.count(f'/v1/video/jobs/{A}/video.mp4')
        self.run_watch('--watch-dir', self.root, '--base', self.base, '--once')
        self.assertEqual(self.count.count(f'/v1/video/jobs/{A}/video.mp4'), calls)

    def test_conflict_never_overwrites_existing_file(self):
        folder = self.root / 'shot'
        self.receipt(folder, A)
        target = folder / f'{A}.mp4'
        target.write_bytes(b'user file')
        result = self.run_watch('--watch-dir', self.root, '--base', self.base, '--once')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(target.read_bytes(), b'user file')
        self.assertIn('refusing overwrite', result.stdout)
        self.assertNotIn(f'/v1/video/jobs/{A}/video.mp4', self.count)

    def test_manifest_uses_receipt_and_relative_output_dir(self):
        (self.root / 'r.json').write_text(json.dumps({'id': A}))
        (self.root / 'jobs.json').write_text(json.dumps({'version': 1, 'base': self.base,
                                                        'jobs': [{'receipt': 'r.json', 'output_dir': 'shot'}]}))
        result = self.run_watch('--manifest', self.root / 'jobs.json', '--once')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'shot' / f'{A}.mp4').read_bytes(), VIDEO)

    def test_duplicate_receipts_are_not_downloaded(self):
        self.receipt(self.root / 'one', A)
        self.receipt(self.root / 'two', A)
        result = self.run_watch('--watch-dir', self.root, '--base', self.base, '--once')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.count, [])
        self.assertIn('duplicate', result.stdout)

    def test_bad_receipt_does_not_block_valid(self):
        self.receipt(self.root / 'good', A)
        folder = self.root / 'bad'
        folder.mkdir()
        (folder / '提交响应.json').write_text('{')
        result = self.run_watch('--watch-dir', self.root, '--base', self.base, '--once')
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.root / 'good' / f'{A}.mp4').read_bytes(), VIDEO)

    def test_checksum_mismatch_is_reported(self):
        self.receipt(self.root / 'shot', A)
        self.assertEqual(self.run_watch('--watch-dir', self.root, '--base', self.base, '--once').returncode, 0)
        target = self.root / 'shot' / f'{A}.mp4'
        target.write_bytes(b'altered')
        result = self.run_watch('--watch-dir', self.root, '--base', self.base, '--once')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(target.read_bytes(), b'altered')
        self.assertIn('checksum mismatch', result.stdout)

    def test_truncated_download_is_not_published(self):
        self.receipt(self.root / 'shot', C)
        result = self.run_watch('--watch-dir', self.root, '--base', self.base, '--once')
        self.assertEqual(result.returncode, 2)
        folder = self.root / 'shot'
        self.assertFalse((folder / f'{C}.mp4').exists())
        self.assertEqual(list(folder.glob('*.part')), [])

    def test_follow_discovers_new_receipt(self):
        child = subprocess.Popen(['python3', str(SCRIPT), '--watch-dir', str(self.root),
                                  '--base', self.base, '--interval', '5', '--follow'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.receipt(self.root / 'new', A)
            target = self.root / 'new' / f'{A}.mp4.job.json'
            deadline = time.monotonic() + 12
            while not target.exists() and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertTrue(target.exists())
            self.assertEqual((self.root / 'new' / f'{A}.mp4').read_bytes(), VIDEO)
        finally:
            child.terminate()
            child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
