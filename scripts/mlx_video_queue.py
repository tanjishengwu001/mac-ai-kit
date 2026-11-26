"""MLX Serve 视频排队服务；监听地址可配置，标准库 + FFmpeg。"""
import argparse
import base64
import binascii
from contextlib import closing
from datetime import datetime
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
LABEL = 'org.mac-ai-kit.mlx-video-queue'
MAX_REQUEST = 64 * 1024**2
MAX_RGB = 768 * 1024**2
MAX_META = 32 * 1024**2
ROUTE = '/v1/video/generations'
DATA_FIELD = re.compile(rb'"data"\s*:\s*"')
JOB_ROUTE = re.compile(r'/v1/video/jobs/([0-9a-f]{32})(/video\.mp4)?\Z')
JSON_HEADERS = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
class QueueError(Exception):
    pass
def pack_request(body):
    """保持 MLX 接口字段原样，仅关闭预览、强制 SSE，并阻止读取服务器文件。"""
    if not isinstance(body, dict):
        raise QueueError('请求必须是 JSON 对象')
    if not isinstance(body.get('model'), str) or not body['model'].strip():
        raise QueueError('请指定已在 MLX Serve 准备好的 model ID')
    # H3 的 LoRA 参数是服务端本地文件路径，不允许从这个 HTTP 包装层传入。
    if any(key in body for key in ('lora_path', 'lora_paths')):
        raise QueueError('队列接口不接受服务端 LoRA 文件路径')
    refs = any(body.get(key) for key in ('ref_images', 'ref_videos', 'ref_audios'))
    ends = any(body.get(key) for key in ('first_frame_image', 'last_frame_image'))
    if refs and ends:
        raise QueueError('FL2VA 首尾帧与 Ref2VA 通用参考不可混发')
    if body.get('last_frame_image') and not body.get('first_frame_image'):
        raise QueueError('提供尾帧时也要提供首帧')
    return dict(body, stream=True, preview=False)
def receive_rgb(stream, destination):
    """按块解开 SSE complete 行中的 RGB；元数据及音轨单独限长。"""
    buf = bytearray()
    head = b''
    phase = 'event'
    carry = b''
    written = 0
    padded = False
    def emit(segment):
        nonlocal carry, written, padded
        # Zig 标准 base64 不转义；兼容 JSON 的正斜杠转义。
        segment = carry + segment
        segment = segment.replace(rb'\/', b'/')
        trailing = int(segment.endswith(b'\\'))
        usable = len(segment) - trailing
        n = usable - usable % 4
        carry = segment[n:]
        if n:
            if padded:
                raise QueueError('MLX RGB base64 填充之后仍有数据')
            try:
                raw = base64.b64decode(segment[:n], validate=True)
            except binascii.Error as e:
                raise QueueError('MLX 返回了无效的 RGB base64') from e
            padded = segment[n - 1:n] == b'='
            written += len(raw)
            if written > MAX_RGB:
                raise QueueError('RGB 超过接收上限')
            destination.write(raw)
    def decode(event):
        if not event.startswith(b'data:'):
            return None
        data = event[5:].strip()
        if not data or data == b'[DONE]':
            return None
        value = json.loads(data)
        if not isinstance(value, dict):
            raise QueueError('MLX SSE 事件不是 JSON 对象')
        if value.get('type') == 'error':
            raise QueueError(str(value.get('message', 'MLX 生成失败'))[:500])
        return value
    while True:
        chunk = stream.read1(65536)
        if not chunk:
            break
        while chunk:
            if phase == 'rgb':
                quote = chunk.find(b'"')
                if quote < 0:
                    emit(chunk)
                    break
                emit(chunk[:quote])
                if carry:
                    raise QueueError('MLX 返回了截断的 RGB base64')
                phase = 'tail'
                chunk = chunk[quote + 1:]
                continue
            line, sep, rest = chunk.partition(b'\n')
            buf.extend(line)
            if len(buf) > MAX_META:
                raise QueueError('MLX SSE 元数据超过上限')
            if phase == 'event':
                match = DATA_FIELD.search(buf)
                if match and buf.startswith(b'data:'):
                    prefix = bytes(buf[:match.start()])
                    # 上游媒体协议的 data 位于 complete 事件；进度不带此字段。
                    if re.search(rb'"type"\s*:\s*"complete"', prefix):
                        head = prefix + b'"data":""'
                        payload = bytes(buf[match.end():])
                        buf.clear()
                        phase = 'rgb'
                        chunk = payload + (sep + rest if sep else b'')
                        continue
            if sep:
                event = decode(head + bytes(buf)) if phase == 'tail' else decode(bytes(buf))
                if event and event.get('type') == 'complete':
                    if phase != 'tail':
                        raise QueueError('MLX complete 未携带 RGB 字段')
                    return event, written
                buf.clear()
            chunk = rest
    if phase == 'tail' and buf:
        event = decode(head + bytes(buf))
        if event and event.get('type') == 'complete':
            return event, written
    raise QueueError('MLX 流结束，未收到完整视频')
def positive_int(value, maximum):
    if type(value) is not int or not 0 < value <= maximum:
        raise QueueError('MLX 返回了无效视频/音轨元数据')
    return value
def encode(ffmpeg, meta, folder, bytes_written, bitrate, timeout):
    width = positive_int(meta.get('width'), 8192)
    height = positive_int(meta.get('height'), 8192)
    fps = positive_int(meta.get('fps'), 240)
    frames = positive_int(meta.get('frames'), MAX_RGB)
    if meta.get('format') != 'rgb8' or bytes_written != width * height * frames * 3:
        raise QueueError('RGB 大小与 MLX 返回的帧数或尺寸不符')
    pending = folder / 'result.tmp.mp4'
    cmd = [ffmpeg, '-nostdin', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
           '-video_size', f'{width}x{height}', '-framerate', str(fps), '-i', str(folder / 'video.rgb'),
           ]
    audio = meta.get('audio_data')
    if audio:
        if meta.get('audio_format') != 'pcm_s16le':
            raise QueueError('不支持的 MLX 音轨格式')
        rate = positive_int(meta.get('audio_sample_rate'), 192000)
        channels = positive_int(meta.get('audio_channels'), 8)
        pcm = base64.b64decode(audio, validate=True)
        if not pcm or len(pcm) % (channels * 2):
            raise QueueError('MLX 返回了截断的音轨')
        (folder / 'audio.pcm').write_bytes(pcm)
        cmd += ['-f', 's16le', '-ar', str(rate), '-ac', str(channels), '-i', str(folder / 'audio.pcm')]
    cmd += ['-map', '0:v:0']
    if audio:
        cmd += ['-map', '1:a:0', '-c:a', 'aac', '-b:a', '192k']
    cmd += ['-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
    encoders = []
    if sys.platform == 'darwin':
        encoders.append(('h264_videotoolbox', ['-allow_sw', '0', '-b:v', bitrate]))
    encoders.append(('libx264', ['-preset', 'fast', '-crf', '18']))
    for name, args in encoders:
        pending.unlink(missing_ok=True)
        # stderr 不回传原始本地路径；错误也不会触发第二次生成。
        try:
            result = subprocess.run(cmd + ['-c:v', name] + args + [str(pending)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            if name == 'h264_videotoolbox':
                continue
            raise
        if result.returncode == 0 and pending.is_file() and pending.stat().st_size:
            pending.replace(folder / 'result.mp4')
            return name
    raise QueueError('FFmpeg 编码失败：检查 VideoToolbox、libx264 和 AAC 支持')
class VideoQueue:
    def __init__(self, args):
        self.args = args
        self.root = args.state
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
            raise QueueError('状态目录必须归当前用户所有且权限为 700')
        self.work_dir = self.root / 'work'
        self.work_dir.mkdir(mode=0o700, exist_ok=True)
        self.output = getattr(args, 'output_dir', None) or self.root / 'results'
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def db(self):
        conn = sqlite3.connect(self.root / 'jobs.sqlite3', timeout=10, isolation_level=None)
        conn.execute('PRAGMA secure_delete=ON')
        conn.row_factory = sqlite3.Row
        return conn
    def recover(self):
        with closing(self.db()) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                seq INTEGER PRIMARY KEY, id TEXT UNIQUE, request TEXT, state TEXT,
                created REAL, updated REAL, error TEXT, encoder TEXT, output_path TEXT)''')
            # 原子发布后的 MP4 是完整的，可恢复，不重复消耗推理。
            for job in db.execute("SELECT id, output_path FROM jobs WHERE state='running'").fetchall():
                output = Path(job['output_path']) if job['output_path'] else None
                if output and output.is_file():
                    db.execute("UPDATE jobs SET state='succeeded', request=NULL, updated=? WHERE id=?",
                               (time.time(), job['id']))
                else:
                    if output:
                        output.with_suffix('.partial').unlink(missing_ok=True)
                    db.execute("""UPDATE jobs SET state='failed', request=NULL, updated=?,
                               output_path=NULL, error='服务中断；为避免重复生成未自动重试，请重新提交。'
                               WHERE id=?""", (time.time(), job['id']))
        for folder in self.work_dir.glob('.encoding-*'):
            if folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder)
    def submit(self, body):
        job_id = uuid.uuid4().hex
        now = time.time()
        with closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= self.args.max_jobs:
                return None
            if db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] >= self.args.max_retained:
                return None
            db.execute("INSERT INTO jobs (id,request,state,created,updated) VALUES (?,?,'queued',?,?)",
                       (job_id, json.dumps(body, ensure_ascii=False, allow_nan=False), now, now))
            db.commit()
        self.wake.set()
        return self.get(job_id)
    def get(self, job_id):
        with closing(self.db()) as db:
            row = db.execute('SELECT id,state AS status,created,updated,error,encoder,output_path FROM jobs WHERE id=?',
                             (job_id,)).fetchone()
        if not row:
            return None
        info = dict(row)
        path = f'/v1/video/jobs/{job_id}'
        info['status_url'] = path
        if info['status'] == 'succeeded':
            info['video_url'] = path + '/video.mp4'
        else:
            info.pop('output_path', None)
        return info
    def remove(self, job_id):
        with closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT state,output_path FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job:
                return 404
            if job['state'] == 'running':
                return 409
            if job['output_path']:
                Path(job['output_path']).unlink(missing_ok=True)
            db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
            db.commit()
        return 204
    def earliest(self):
        with closing(self.db()) as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT id,request FROM jobs WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
            if job:
                db.execute("UPDATE jobs SET state='running', updated=? WHERE id=?", (time.time(), job['id']))
            db.commit()
            return job
    def work(self):
        while not self.stop.is_set():
            self.wake.clear()
            job = self.earliest()
            if not job:
                self.wake.wait(1)
                continue
            encoder = error = None
            try:
                encoder = self.generate(job)
            except QueueError as exc:
                error = str(exc)
            except (OSError, ValueError, subprocess.SubprocessError):
                error = '生成或编码中断：请检查上游连接、超时、FFmpeg 和可用磁盘空间'
            with closing(self.db()) as db:
                db.execute('''UPDATE jobs SET state=?,updated=?,error=?,encoder=?,request=NULL,
                           output_path=CASE WHEN ? THEN NULL ELSE output_path END WHERE id=?''',
                           ('failed' if error else 'succeeded', time.time(), error, encoder, bool(error), job['id']))
    def archive(self, job_id, source, encoder):
        # 以压缩完成时的本地时间分目录；ID 避免同秒、时钟回拨和夏令时重名。
        finished = datetime.now().astimezone()
        folder = self.output / finished.strftime('%Y-%m-%d') / finished.strftime('%H')
        if not folder.resolve().is_relative_to(self.output.resolve()):
            raise QueueError('成片归档目录指向输出根目录之外')
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = finished.strftime('%H%M%S.%f') + f'-{job_id}.mp4'
        output = folder / name
        temporary = output.with_suffix('.partial')
        with closing(self.db()) as db:
            db.execute('UPDATE jobs SET output_path=?,encoder=? WHERE id=?', (str(output), encoder, job_id))
        try:
            # 暂存与目标在同一目录：即使输出根目录在外置盘，也可原子发布。
            with source.open('rb') as src, temporary.open('xb') as dest:
                shutil.copyfileobj(src, dest, 1024 * 1024)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    def generate(self, job):
        headers = dict(JSON_HEADERS)
        if self.args.upstream_key_file:
            key = self.args.upstream_key_file.read_text().strip()
            if not key or not key.isascii() or not key.isprintable():
                raise QueueError('上游密钥文件必须包含一行非空的 ASCII 密钥')
            headers['Authorization'] = 'Bearer ' + key
        req = urllib.request.Request(f'http://127.0.0.1:{self.args.upstream_port}{ROUTE}',
                                     job['request'].encode(), headers=headers, method='POST')
        with tempfile.TemporaryDirectory(prefix='.encoding-', dir=self.work_dir) as tmp:
            folder = Path(tmp)
            try:
                with self.opener.open(req, timeout=self.args.timeout) as response, (folder / 'video.rgb').open('wb') as raw:
                    if response.headers.get_content_type() != 'text/event-stream':
                        raise QueueError('MLX Serve 未返回预期的 SSE 视频流')
                    meta, size = receive_rgb(response, raw)
            except urllib.error.HTTPError as e:
                with e:
                    detail = ''
                    try:
                        detail = str(json.loads(e.read(16384)).get('error', {}).get('message', ''))[:500]
                    except (ValueError, TypeError, AttributeError):
                        pass
                raise QueueError(f'MLX Serve HTTP {e.code}' + (f'：{detail}' if detail else '')) from e
            encoder = encode(self.args.ffmpeg, meta, folder, size, self.args.bitrate, self.args.timeout)
            self.archive(job['id'], folder / 'result.mp4', encoder)
            return encoder
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)
    @property
    def queue(self):
        return self.server.queue
    def log_message(self, *_):
        pass
    def json_response(self, code, obj=None):
        data = json.dumps(obj, ensure_ascii=False).encode() if obj is not None else b''
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Mac-AI-Kit-Service', LABEL)
        self.end_headers()
        if data:
            self.wfile.write(data)
    def check_local(self):
        # 回环监听时限制 Host；非回环监听允许客户端使用服务器 LAN IP 访问。仍拒绝带 Origin/Sec-Fetch-Site 的网页请求。
        port = self.server.server_port
        if self.server.server_address[0] in ('127.0.0.1', '::1') and self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}', f'[::1]:{port}'):
            self.json_response(403, {'error': '只接受本地 API 请求'})
            return False
        if self.headers.get('Origin') is not None or self.headers.get('Sec-Fetch-Site') is not None:
            self.json_response(403, {'error': '不接受网页发起的请求'})
            return False
        return True
    def do_POST(self):
        if not self.check_local():
            return
        if self.path != ROUTE:
            return self.json_response(404, {'error': '接口不存在'})
        if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
            return self.json_response(415, {'error': '仅支持有 Content-Length 的 JSON 请求'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_REQUEST:
            return self.json_response(413, {'error': '请求大小超限或没有请求体'})
        if not self.server.ingress.acquire(blocking=False):
            return self.json_response(503, {'error': '正在接收其他任务，请稍后重试'})
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise QueueError('请求体未完整传输')
            body = pack_request(json.loads(raw))
            job = self.queue.submit(body)
        except (ValueError, QueueError) as exc:
            detail = str(exc) if isinstance(exc, QueueError) else '无效 JSON 请求'
            return self.json_response(400, {'error': detail})
        except (OSError, sqlite3.Error):
            return self.json_response(503, {'error': '无法接收任务，请检查连接和状态目录'})
        finally:
            self.server.ingress.release()
        if not job:
            return self.json_response(429, {'error': '队列或保留结果已满；请稍后重试或清理旧任务'})
        self.json_response(202, job)
    def do_GET(self):
        if not self.check_local():
            return
        if self.path == '/healthz':
            return self.json_response(200 if self.server.worker.is_alive() else 503,
                                      {'worker_alive': self.server.worker.is_alive()})
        m = JOB_ROUTE.fullmatch(self.path)
        job = self.queue.get(m[1]) if m else None
        if not job:
            return self.json_response(404, {'error': '任务不存在'})
        if not m[2]:
            return self.json_response(200, job)
        if job['status'] != 'succeeded':
            return self.json_response(409, {'error': '没有可下载的成片', 'status': job['status']})
        try:
            video = Path(job['output_path']).open('rb')
        except FileNotFoundError:
            return self.json_response(404, {'error': '成片已清理'})
        with video:
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(os.fstat(video.fileno()).st_size))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                shutil.copyfileobj(video, self.wfile, 65536)
            except (BrokenPipeError, ConnectionResetError):
                pass
    def do_DELETE(self):
        if not self.check_local():
            return
        m = JOB_ROUTE.fullmatch(self.path)
        try:
            status = self.queue.remove(m[1]) if m and not m[2] else 404
        except (OSError, sqlite3.Error):
            return self.json_response(503, {'error': '无法删除归档文件或记录'})
        self.json_response(status, {404: {'error': '任务不存在'}, 409: {'error': '任务正在运行'}}.get(status))
def access_urls(host, port):
    """返回用户可实际访问的 IPv4 URL；0.0.0.0 只用于绑定，不作为访问地址。"""
    if host not in ('0.0.0.0', '::', ''):
        return [f'http://{host}:{port}']
    addresses = set()
    try:
        result = subprocess.run(['ifconfig'], capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            addresses.update(re.findall(r'\binet (\d+(?:\.\d+){3})\b', result.stdout))
    except (OSError, subprocess.SubprocessError):
        pass
    addresses = sorted(ip for ip in addresses if not ip.startswith(('127.', '169.254.')))
    if not addresses:
        # 兜底：根据默认出站路由获取当前主机的主要局域网地址，不发送数据包。
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(('192.0.2.1', 9))
                ip = probe.getsockname()[0]
                if ip and not ip.startswith('127.'):
                    addresses.append(ip)
        except OSError:
            pass
    if not addresses:
        addresses = ['127.0.0.1']
    return [f'http://{ip}:{port}' for ip in addresses]
def print_access_urls(prefix, host, port):
    print(f'{prefix}（监听：{host}:{port}）', flush=True)
    for url in access_urls(host, port):
        print(f'  {url}', flush=True)
def run(args):
    if args.port == args.upstream_port:
        raise QueueError('队列和 MLX Serve 必须使用不同端口')
    args.ffmpeg = shutil.which(args.ffmpeg)
    if not args.ffmpeg:
        raise QueueError('未找到 FFmpeg；请先安装，或用 --ffmpeg 指定')
    queue = VideoQueue(args)
    with (queue.root / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise QueueError('该状态目录已有一个队列进程') from e
        queue.recover()
        with ThreadingHTTPServer((args.host, args.port), Handler) as server:
            server.queue = queue
            server.ingress = threading.BoundedSemaphore(2)
            server.worker = threading.Thread(target=queue.work, daemon=True)
            server.worker.start()
            signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
            print_access_urls('MLX 视频队列已启动', args.host, server.server_port)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                queue.stop.set()
                queue.wake.set()
                # 保留锁直到进程退出；未完成任务由下次启动标为失败。
def atomic_write(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        target = Path(temp.name)
        try:
            temp.write(content)
            temp.close()
            target.replace(path)
        finally:
            target.unlink(missing_ok=True)
def launch(args):
    if sys.platform != 'darwin':
        raise QueueError('后台启停使用 macOS 用户级 launchd；可用 run 调试')
    domain = f'gui/{os.getuid()}'
    target = f'{domain}/{LABEL}'
    root = args.state
    path = root / f'{LABEL}.plist'
    def ctl(*argv):
        return subprocess.run(['/bin/launchctl', *argv], capture_output=True, text=True, timeout=15)
    state = ctl('print', target)
    if args.action == 'status':
        print(state.stdout if state.returncode == 0 else '队列未加载')
        return
    if state.returncode == 0:
        match = re.search(r'^\s*path = (.+)$', state.stdout, re.M)
        if not match or Path(match[1].strip()).resolve() != path.resolve():
            raise QueueError('同名 launchd 服务来自其他状态目录，未修改')
        if args.action == 'start':
            print('队列已加载；修改配置请先 stop 再 start。')
            return
        if ctl('bootout', target).returncode:
            raise QueueError('launchd 未能停止服务')
        print('队列已停止；下次启动保留等待中的任务。')
        return
    if args.action == 'stop':
        print('队列未加载。')
        return
    ffmpeg = shutil.which(args.ffmpeg)
    if not ffmpeg:
        raise QueueError('未找到 FFmpeg；请先安装，或用 --ffmpeg 指定')
    if args.port == args.upstream_port:
        raise QueueError('队列和 MLX Serve 必须使用不同端口')
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((args.host, args.port))
        except OSError as exc:
            raise QueueError(f'队列监听地址 {args.host}:{args.port} 无法绑定；请换地址/端口或关闭占用进程') from exc
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise QueueError('状态目录必须归当前用户所有且权限为 700')
    script = root / 'mlx_video_queue.py'
    atomic_write(script, Path(__file__).read_bytes())
    cmd = [sys.executable, str(script), 'run', '--state', str(root), '--ffmpeg', str(Path(ffmpeg).absolute())]
    for name in ('host', 'port', 'upstream_port', 'timeout', 'bitrate', 'max_jobs', 'max_retained', 'upstream_key_file', 'output_dir'):
        value = getattr(args, name)
        if value is not None:
            cmd += ['--' + name.replace('_', '-'), str(value)]
    atomic_write(path, plistlib.dumps(dict(Label=LABEL, ProgramArguments=cmd, RunAtLoad=True,
        KeepAlive=True, ThrottleInterval=10, StandardOutPath=str(root / 'stdout.log'),
        StandardErrorPath=str(root / 'stderr.log'))))
    if ctl('bootstrap', domain, str(path)).returncode:
        raise QueueError('launchd 未能加载服务；请确认当前用户已登录')
    # 已加载不等于已监听；确认端口确实由本服务响应。
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for _ in range(30):
        try:
            with client.open(f'http://127.0.0.1:{args.port}/healthz', timeout=.5) as res:
                if res.headers.get('X-Mac-AI-Kit-Service') == LABEL:
                    print_access_urls('队列已启动', args.host, args.port)
                    return
        except OSError:
            pass
        time.sleep(.2)
    raise QueueError('已加载但未确认健康；请查看状态目录的 stderr.log')
def main(argv=None):
    os.umask(0o077)
    p = argparse.ArgumentParser(description='MLX Serve 常驻视频队列及 MP4 回传')
    p.add_argument('action', choices=('start', 'stop', 'status', 'run'),
                   help='启动、停止、查看 launchd 状态，或在前台运行')
    p.add_argument('--state', type=Path, default=Path.home() / 'Library/Application Support/Mac AI Kit/mlx-video-queue')
    p.add_argument('--output-dir', type=Path, help='成片输出根目录（默认状态目录下的 results）')
    p.add_argument('--host', default='0.0.0.0', help='队列监听地址（默认 0.0.0.0，监听所有 IPv4 网卡）')
    p.add_argument('--port', type=int, default=11235, help='队列监听端口')
    p.add_argument('--upstream-port', type=int, default=11234, help='MLX Serve 本机端口')
    p.add_argument('--upstream-key-file', type=Path, help='可选的上游 Bearer 密钥文件路径')
    p.add_argument('--ffmpeg', default='ffmpeg', help='FFmpeg 命令或绝对路径')
    p.add_argument('--timeout', type=int, default=3600, help='上游 I/O 与单次编码超时（秒）')
    p.add_argument('--bitrate', default='8M', help='VideoToolbox 目标码率')
    p.add_argument('--max-jobs', type=int, default=32, help='最大活动任务数')
    p.add_argument('--max-retained', type=int, default=256, help='所有任务记录数量上限')
    args = p.parse_args(argv)
    if not (1 <= args.port <= 65535 and 1 <= args.upstream_port <= 65535 and args.timeout > 0
            and 0 < args.max_jobs <= args.max_retained and re.fullmatch(r'[1-9][0-9]*[kKmM]?', args.bitrate)):
        p.error('端口、超时、队列上限或码率无效')
    args.state = args.state.expanduser().resolve()
    if args.output_dir:
        args.output_dir = args.output_dir.expanduser().resolve()
    if args.upstream_key_file:
        args.upstream_key_file = args.upstream_key_file.expanduser().resolve()
    try:
        (run if args.action == 'run' else launch)(args)
    except (QueueError, OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
        p.exit(1, f'{exc}\n')
if __name__ == '__main__':
    main()
