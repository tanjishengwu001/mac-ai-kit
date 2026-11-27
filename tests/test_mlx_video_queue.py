import base64
from pathlib import Path
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error
import urllib.request
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import mlx_video_queue as queue


def video_event(n=2, sound=True):
    rgb = bytes((1, 90, 240)) * (32 * 32 * n)
    data = dict(type='complete', frames=n, height=32, width=32, fps=24,
                format='rgb8', data=base64.b64encode(rgb).decode())
    if sound:
        data.update(audio_format='pcm_s16le', audio_sample_rate=24000, audio_channels=1,
                    audio_data=base64.b64encode(b'\0\0' * (24000 * n // 24)).decode())
    payload = b'data: {"type":"progress","step":1}\r\n\r\ndata: ' + json.dumps(data).encode() + b'\r\n\r\n'
    return payload, rgb, data


class ByteStream(io.BytesIO):
    def read1(self, n=-1):
        return super().read(7 if n == -1 else min(7, n))


class VideoQueueTests(unittest.TestCase):
    def test_chunked_sse_and_audio(self):
        payload, rgb, info = video_event()
        out = io.BytesIO()
        progress = []
        data, size = queue.receive_rgb(ByteStream(payload), out, on_progress=progress.append)
        self.assertEqual(size, len(rgb))
        self.assertEqual(out.getvalue(), rgb)
        self.assertEqual(data, dict(info, data=''))
        self.assertEqual(progress, [{'type': 'progress', 'step': 1}])

    def test_progress_persists_for_running_job_and_ignores_media(self):
        with tempfile.TemporaryDirectory() as d:
            video_queue = queue.VideoQueue(SimpleNamespace(state=Path(d), max_jobs=2, max_retained=3))
            video_queue.recover()
            job_id = video_queue.submit({'model': 'm1'})['id']
            self.assertIsNone(video_queue.get(job_id)['progress'])
            video_queue.earliest()
            video_queue.update_progress(job_id, {'type': 'progress', 'step': 3, 'total_steps': 20, 'data': 'SECRET'})
            status = video_queue.get(job_id)
            self.assertEqual(status['progress']['step'], 3)
            self.assertEqual(status['progress']['total_steps'], 20)
            self.assertNotIn('data', status['progress'])
            self.assertIsInstance(status['progress']['observed_at'], float)

    def test_progress_migrates_existing_queue_database(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with sqlite3.connect(root / 'jobs.sqlite3') as db:
                db.execute('''CREATE TABLE jobs (seq INTEGER PRIMARY KEY, id TEXT UNIQUE,
                           request TEXT, state TEXT, created REAL, updated REAL, error TEXT,
                           encoder TEXT, output_path TEXT)''')
            video_queue = queue.VideoQueue(SimpleNamespace(state=root, max_jobs=2, max_retained=3))
            video_queue.recover()
            job_id = video_queue.submit({'model': 'm1'})['id']
            self.assertIsNone(video_queue.get(job_id)['progress'])

    def test_truncated_sse_and_typed_error(self):
        payload, _, _ = video_event()
        for data, message in ((payload[:500], '未收到'), ('data: {"type":"error","message":"注意"}\n\n'.encode(), '注意')):
            with self.subTest(message=message):
                with self.assertRaisesRegex(queue.QueueError, message):
                    queue.receive_rgb(ByteStream(data), io.BytesIO())

    def test_mode_rules_and_preview(self):
        for spec in ({'first_frame_image': 'AAAA', 'last_frame_image': 'BBBB'},
                     {'ref_images': ['AAAA'], 'ref_videos': [{'frames': ['BBBB']}], 'ref_audios': ['CCCC']}):
            request = {'model': 'test', **spec, 'steps': 20, 'stream': False, 'preview': True}
            self.assertEqual(queue.pack_request(request), dict(request, stream=True, preview=False))
        for spec in ({'first_frame_image': 'a', 'ref_images': ['b']},
                     {'last_frame_image': 'a'}, {'lora_paths': ['/no']}):
            with self.assertRaises(queue.QueueError):
                queue.pack_request({'model': 'm', **spec})

    def test_restart_and_limits(self):
        with tempfile.TemporaryDirectory() as d:
            video_queue = queue.VideoQueue(SimpleNamespace(state=Path(d), max_jobs=2, max_retained=3))
            video_queue.recover()
            first = video_queue.submit({'model': 'm1'})['id']
            second = video_queue.submit({'model': 'm2'})['id']
            self.assertIsNone(video_queue.submit({'model': 'm3'}))
            self.assertEqual(video_queue.earliest()['id'], first)
            self.assertEqual(video_queue.remove(first), 409)
            video_queue.recover()
            self.assertEqual(video_queue.get(first)['status'], 'failed')
            self.assertEqual(video_queue.earliest()['id'], second)
            self.assertEqual(video_queue.remove(first), 204)

    def test_recovers_published_video_after_archive_root_change(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            args = SimpleNamespace(state=home / 'state', output_dir=home / 'first', max_jobs=3, max_retained=4)
            video_queue = queue.VideoQueue(args)
            video_queue.recover()
            job_id = video_queue.submit({'model': 'm1'})['id']
            self.assertEqual(video_queue.earliest()['id'], job_id)
            source = home / 'sample.mp4'
            source.write_bytes(b'mock mp4')
            video_queue.archive(job_id, source, 'libx264')
            self.assertNotIn('output_path', video_queue.get(job_id))
            args.output_dir = home / 'second'
            video_queue = queue.VideoQueue(args)
            video_queue.recover()
            job = video_queue.get(job_id)
            self.assertEqual(job['status'], 'succeeded')
            self.assertEqual(job['encoder'], 'libx264')
            self.assertEqual(Path(job['output_path']).parents[2], home / 'first')
            self.assertEqual(Path(job['output_path']).read_bytes(), source.read_bytes())
            self.assertEqual(video_queue.remove(job_id), 204)
            self.assertTrue(source.exists())

    def test_prefers_hardware_and_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _, rgb, info = video_event(sound=False)
            (root / 'video.rgb').write_bytes(rgb)
            calls = []

            def fake_ffmpeg(cmd, **opts):
                codec = cmd[cmd.index('-c:v') + 1]
                calls.append(codec)
                self.assertIn('+faststart', cmd)
                if codec == 'libx264':
                    Path(cmd[-1]).write_bytes(b'mock mp4')
                    return SimpleNamespace(returncode=0)
                self.assertIn('-allow_sw', cmd)
                return SimpleNamespace(returncode=1)

            with patch.object(queue.sys, 'platform', 'darwin'), patch.object(queue.subprocess, 'run', fake_ffmpeg):
                codec = queue.encode('ffmpeg', info, root, len(rgb), '8M', 5)
            self.assertEqual(calls, ['h264_videotoolbox', 'libx264'])
            self.assertEqual(codec, 'libx264')
            self.assertEqual((root / 'result.mp4').read_bytes(), b'mock mp4')

    @unittest.skipUnless(os.name == 'posix' and shutil.which('ffmpeg') and shutil.which('ffprobe'),
                         '真实转码回归需要本机 FFmpeg/FFprobe')
    def test_start_entry_fifo_and_actual_mp4(self):
        codecs = subprocess.check_output(['ffmpeg', '-hide_banner', '-encoders'], text=True)
        if 'libx264' not in codecs and sys.platform != 'darwin':
            self.skipTest('FFmpeg 缺少 libx264')
        requests = []
        payload, _, _ = video_event(n=24)

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                item = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, item, self.headers.get('Authorization')))
                if item['prompt'] == 'reject':
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"upstream limit"}}')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for i in range(0, len(payload), 499):
                    self.wfile.write(payload[i:i + 499])
                    self.wfile.flush()

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Upstream) as server, tempfile.TemporaryDirectory() as tmp:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(worker.join, 5)
            home = Path(tmp)
            # 启动时取一个可用端口；服务本身仍拒绝端口 0。
            with ThreadingHTTPServer(('127.0.0.1', 0), Upstream) as reserve:
                port = reserve.server_port
            endpoint = f'http://127.0.0.1:{port}'
            token = home / 'key'
            token.write_text('test-token')
            child = subprocess.Popen(
                ['bash', str(ROOT / 'start.sh'), 'mlx-video-queue', 'run', '--state', str(home / 'state'),
                 '--output-dir', 'dated-exports', '--host', '127.0.0.1', '--port', str(port), '--upstream-port', str(server.server_port), '--upstream-key-file', str(token)],
                cwd=home, env=dict(os.environ, HOME=tmp), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def get(path):
                with client.open(endpoint + path, timeout=5) as res:
                    return res.read()

            try:
                for _ in range(100):
                    if child.poll() is not None:
                        self.fail(f'队列进程异常退出：{child.returncode}')
                    try:
                        if json.loads(get('/healthz'))['worker_alive']:
                            break
                    except OSError:
                        time.sleep(.04)
                else:
                    self.fail('队列未启动')

                specs = [{'model': 'fl2va', 'prompt': 'first', 'first_frame_image': 'AAAA', 'last_frame_image': 'BBBB'},
                         {'model': 'ref2va', 'prompt': 'reject', 'ref_images': ['AAAA']},
                         {'model': 'ref2va', 'prompt': 'third', 'ref_audios': ['AAAA']}]
                jobs = []
                for spec in specs:
                    req = urllib.request.Request(endpoint + queue.ROUTE, json.dumps(spec).encode(),
                                                 {'Content-Type': 'application/json'})
                    with client.open(req, timeout=5) as res:
                        self.assertEqual(res.status, 202)
                        jobs.append(json.load(res))
                for i, job in enumerate(jobs):
                    for _ in range(250):
                        job = json.loads(get(job['status_url']))
                        if job['status'] in ('succeeded', 'failed'):
                            break
                        time.sleep(.04)
                    if i == 1:
                        self.assertEqual(job['status'], 'failed')
                        self.assertIn('upstream limit', job['error'])
                        continue
                    self.assertEqual(job['status'], 'succeeded', job)
                    archived = Path(job['output_path'])
                    self.assertEqual(archived.parents[2].resolve(), (home / 'dated-exports').resolve())
                    self.assertRegex(str(archived.relative_to(archived.parents[2])),
                                     rf'^\d{{4}}-\d{{2}}-\d{{2}}/\d{{2}}/\d{{6}}\.\d{{6}}-{job["id"]}\.mp4$')
                    result = home / f'{i}.mp4'
                    with client.open(endpoint + job['video_url'], timeout=5) as res:
                        self.assertEqual(res.headers.get_content_type(), 'video/mp4')
                        result.write_bytes(res.read())
                    self.assertEqual(result.read_bytes(), archived.read_bytes())
                    info = json.loads(subprocess.check_output(
                        ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(result)]))
                    self.assertEqual([s['codec_name'] for s in info['streams']], ['h264', 'aac'])
                    self.assertEqual(info['streams'][0]['nb_frames'], '24')
                self.assertEqual(requests, [(queue.ROUTE, dict(x, stream=True, preview=False), 'Bearer test-token') for x in specs])
                req = urllib.request.Request(endpoint + '/healthz', headers={'Host': 'untrusted.example'})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    client.open(req, timeout=5)
                self.assertEqual(caught.exception.code, 403)
                req = urllib.request.Request(endpoint + job['status_url'], method='DELETE')
                with client.open(req, timeout=5) as res:
                    self.assertEqual(res.status, 204)
                self.assertFalse(archived.exists())
                self.assertFalse(list((home / 'state/work').glob('.encoding-*')))
            finally:
                child.terminate()
                try:
                    child.wait(5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                server.shutdown()


if __name__ == '__main__':
    unittest.main()
