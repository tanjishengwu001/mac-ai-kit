#!/usr/bin/env python3
"""独立管理已核验配方的原生服务；不安装、不下载、不代替 MLX 桌面 App。"""
import argparse
from collections import deque
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import ipaddress
import os
from pathlib import Path
import plistlib
import re
import shlex
import socket
import subprocess
import sys
import time
import queue
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_inventory import inspect_model, safe_root
from sd_models import request as sd_request, selection as sd_selection
from service_manager import ServiceError, atomic_write, checked_path, port_number, available_port, menu_working_directory
from agent_workflow import append_workflow

ROOT = Path(__file__).resolve().parents[1]
QUEUE_SERVICE = 'mlx-video-queue'
SPECS = {
    'sd-qwen': dict(title='SD.cpp', component='sd.cpp', version='managed', binary='sd-server', port=12345,
                    model_ids=('qwen21-turbo', 'krea2-turbo')),
    'audio': dict(title='audio.cpp · 转写／YuE2', component='audio.cpp', version='v0.9.2-drama3-base-small', binary='audiocpp_server', port=18080,
                  model_ids=('asr-qwen3-1.7b-f16', 'yue2-audio-cpp-bf16')),
}
STOP_TIMEOUT = 35.0
STOP_POLL = 0.5
START_TIMEOUT = 120.0
START_POLL = 0.5
START_PROGRESS = 15.0
SD_LOAD_TIMEOUT = 1800.0

API_BEGIN = '<!-- MAC-AI-KIT:NATIVE:BEGIN -->'
API_END = '<!-- MAC-AI-KIT:NATIVE:END -->'


def clean(value):
    """日志与外部错误不允许改变终端标题、颜色或光标。"""
    value = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', str(value))
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    return ''.join(c for c in value if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) < 160))


def print_safe(value):
    print(clean(value))


def installed_binary(root, spec):
    """SD.cpp 读取受管发行记录；其他工具保持各自的已核验安装方式。"""
    if spec['component'] == 'sd.cpp':
        from sd_release import validate_installed
        folder = root / 'apps/sd.cpp'
        try:
            meta = validate_installed(folder)
            return str(folder / meta['binary_relative'])
        except (ValueError, OSError):
            return None
    folder = root / 'apps' / (spec['component'] + '-' + spec['version'])
    if not folder.is_dir() or folder.is_symlink():
        return None
    files = [p for p in folder.rglob(spec['binary']) if p.is_file() and os.access(p, os.X_OK)
             and p.resolve().is_relative_to(folder.resolve())]
    return str(files[0].resolve()) if len(files) == 1 else None


def network_addresses():
    """只读读取实际 IPv4 网卡地址，包含 VPN；地址存在不保证防火墙放行。"""
    if sys.platform != 'darwin':
        return []
    try:
        result = subprocess.run(['/sbin/ifconfig'], capture_output=True, text=True, timeout=5)
        if result.returncode:
            return []
        addresses = []
        for value in re.findall(r'\binet (\d+\.\d+\.\d+\.\d+)\b', result.stdout):
            address = ipaddress.ip_address(value)
            if not address.is_loopback and not address.is_unspecified and value not in addresses:
                addresses.append(value)
        return addresses
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def available_memory():
    """保守读取可回收页面；不计压缩内存，不执行 purge，不保证峰值。"""
    if sys.platform != 'darwin':
        raise ServiceError('内存预检仅支持 macOS；未启动服务')
    r = subprocess.run(['/usr/bin/vm_stat'], capture_output=True, text=True, timeout=5)
    size = re.search(r'page size of (\d+) bytes', r.stdout)
    if r.returncode or not size:
        raise ServiceError('无法读取当前可用内存；未启动服务')
    values = {}
    for key in ('Pages free', 'Pages inactive', 'Pages speculative'):
        m = re.search(r'^' + key + r':\s+(\d+)\.', r.stdout, re.M)
        if not m:
            raise ServiceError('内存统计不完整；未启动服务')
        values[key] = int(m[1])
    return sum(values.values()) * int(size[1])


class NativeManager:
    def __init__(self, root, models, catalog=None):
        self.root = checked_path(root)
        self.models = checked_path(models)
        self.catalog = catalog or json.loads((ROOT / 'catalog.json').read_text())
        self.base = self.root / 'services'
        self.health_meta = {}
        self.suffix = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]

    def paths(self, sid):
        if sid not in SPECS:
            raise ServiceError('未知服务')
        base = self.base / sid
        return base, base / 'settings.json', base / 'service.plist'

    def label(self, sid):
        return 'org.mac-ai-kit.native.' + sid + '.' + self.suffix

    def target(self, sid):
        return f'gui/{os.getuid()}/{self.label(sid)}'

    def ctl(self, *args):
        return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, timeout=15)

    @contextmanager
    def lock(self):
        safe_root(self.root)
        for path in (self.base, self.base / '.native.lock'):
            if path.is_symlink():
                raise ServiceError('服务目录或锁是符号链接，未操作')
        self.base.mkdir(exist_ok=True)
        with (self.base / '.native.lock').open('a') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ServiceError('另一个服务管理操作正在执行，请稍后重试')
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def model(self, mid):
        by_id = {m['id']: m for m in self.catalog['models']}
        def visit(key, seen):
            if key in seen or key not in by_id:
                raise ServiceError('模型依赖清单无效')
            definition = dict(by_id[key])
            definition['expected_files'] = {name: spec for name, spec in definition.get('expected_files', {}).items()
                                            if Path(name).suffix.lower() != '.md' and Path(name).name.lower() not in ('license', 'license.txt', 'notice', 'notice.txt', '.gitattributes', '.gitignore')}
            item = inspect_model(definition, self.models)
            if item['state'] != 'complete':
                details = list(item.get('notes', [])[:3])
                for name, expected in definition.get('expected_files', {}).items():
                    file = item['path'] / name
                    if file.is_file() and not file.is_symlink() and file.stat().st_size != expected.get('bytes'):
                        details.append(f"{name}：实际 {file.stat().st_size} 字节，清单 {expected.get('bytes')} 字节")
                        break
                raise ServiceError(key + '：' + str(item['path']) + '；' + '；'.join(details))
            total = sum(s['bytes'] for s in by_id[key].get('expected_files', {}).values())
            for dep in by_id[key].get('requires', []):
                # 许可资料仍随下载保存，但不属于推理所需组件。未知依赖仍报错。
                if by_id.get(dep, {}).get('runtime_required', True):
                    total += visit(dep, seen | {key})[1]
            return item, total
        return visit(mid, set())

    def options(self, sid):
        _, settings, _ = self.paths(sid)
        if not settings.exists():
            return '0.0.0.0', SPECS[sid]['port']
        data = self.read_settings(sid)
        return '0.0.0.0', port_number(data['port'])

    def recipe(self, sid):
        safe_root(self.models)
        if len(self.models.parts) > 2 and self.models.parts[1] == 'Volumes' and not os.path.ismount(Path('/Volumes') / self.models.parts[2]):
            raise ServiceError('模型外置盘未挂载')
        spec = SPECS[sid]
        binary = installed_binary(self.root, spec)
        if not binary:
            raise ServiceError(f"{spec['component']} 未安装完整；请在安装运行工具中重新安装该工具")
        host, port = self.options(sid)
        base, _, _ = self.paths(sid)
        data = dict(owner='mac-ai-kit-native-v1', root=str(self.root), models_dir=str(self.models),
                    service=sid, host=host, port=port, binary=binary, version=spec['version'])
        if sid == 'sd-qwen':
            from sd_release import validate_installed
            data['version'] = validate_installed(self.root / 'apps/sd.cpp')['tag']
        if sid == 'sd-qwen':
            roots, available, skipped = [], [], []
            default_budget = 0
            for mid in spec['model_ids']:
                try:
                    item, size = self.model(mid)
                except ServiceError as error:
                    skipped.append(str(error))
                    continue
                roots.append(str(item['path']))
                if mid == 'qwen21-turbo':
                    default_budget = int(size * 2.0)
                if mid == 'krea2-turbo':
                    roots.append(str(item['path'] / 'dependencies/Wan_2.1_ComfyUI_repackaged/split_files'))
                available.append(mid)
            if not available:
                raise ServiceError('未发现完整图像模型；' + '；'.join(skipped))
            # 原生目录注册只扫描已有文件；服务就绪后通过原生 API 加载默认 Qwen，不复制或链接模型。
            args = [binary, '--listen-ip', host, '--listen-port', str(port)]
            for folder in dict.fromkeys(roots):
                args += ['--model-dir', folder]
            data.update(args=args, models=available, skipped_models=skipped,
                        mode='native-model-management', reserve_bytes=default_budget, default_model='qwen-image-21-turbo' if 'qwen21-turbo' in available else None)

        else:
            models, sizes, skipped = [], [], []
            for mid in spec['model_ids']:
                try:
                    item, size = self.model(mid)
                except ServiceError as error:
                    skipped.append(str(error))
                    continue
                if mid.startswith('asr-'):
                    first = next(iter(item['model']['expected_files']))
                    models.append(dict(id='qwen3-asr', family='qwen3_asr', path=str(item['path'] / first), task='asr', mode='offline'))
                else:
                    models.append(dict(id='yue2', family='yue2', path=str(item['path']), task='gen', mode='offline',
                        session_options={'yue2.model_gguf': 'yue2-3b-bf16.gguf', 'yue2.vae_gguf': 'yue2-vae-f32.gguf'}))
                sizes.append(size)
            if not models:
                raise ServiceError('未发现完整音频模型；' + '；'.join(skipped))
            config = dict(host=host, port=port, backend='metal', lazy_load=True, max_loaded_models=1, idle_unload_ms=300000, ui=True, ui_management=False, models=models)
            data.update(args=[binary, '--config', str(base / 'server.json'), '--log'], config=config,
                        models=[m['id'] for m in models], skipped_models=skipped, reserve_bytes=int(max(sizes) * 3.0))
        return data

    def read_settings(self, sid):
        base, path, _ = self.paths(sid)
        if self.base.is_symlink() or base.is_symlink() or path.is_symlink():
            raise ServiceError('服务设置不可为符号链接')
        data = json.loads(path.read_text())
        if (data.get('owner') != 'mac-ai-kit-native-v1' or data.get('root') != str(self.root)
            or data.get('service') != sid or data.get('host') not in ('127.0.0.1', '0.0.0.0')
            or (sid != 'sd-qwen' and data.get('version') != SPECS[sid]['version'])
            or not isinstance(data.get('version'), str) or not data['version']):
            raise ServiceError('服务设置归属或结构不符')
        port_number(data['port'])
        return data

    def owned(self, sid):
        base, _, path = self.paths(sid)
        try:
            if self.base.is_symlink() or base.is_symlink() or path.is_symlink():
                return False
            data = self.read_settings(sid)
            plist = plistlib.loads(path.read_bytes())
            return (plist.get('Label') == self.label(sid) and plist.get('MacAIKitRoot') == str(self.root)
                    and plist.get('ProgramArguments') == data['args']
                    and plist.get('StandardOutPath') == str(base / 'stdout.log')
                    and plist.get('StandardErrorPath') == str(base / 'stderr.log'))
        except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
            return False

    def process_identity(self, pid):
        """只读进程指纹；不以 PID 单独判断归属，也绝不向 PID 发信号。"""
        r = subprocess.run(['/bin/ps', '-p', str(int(pid)), '-o', 'uid=', '-o', 'lstart=', '-o', 'command='],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 1 and not r.stdout.strip() and not r.stderr.strip():
            return None
        if r.returncode or not r.stdout.strip():
            raise ServiceError('无法核查原进程是否退出；请查看日志后重试停止')
        return r.stdout.strip()

    def pending_stop(self, sid, clean_stale=True):
        path = self.paths(sid)[0] / 'stopping.json'
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not self.owned(sid):
            raise ServiceError('停止记录归属不符，未控制进程')
        record = json.loads(path.read_text())
        if (record.get('root') != str(self.root) or record.get('service') != sid
                or type(record.get('pid')) is not int or record['pid'] <= 0
                or not isinstance(record.get('identity'), str) or not record['identity']):
            raise ServiceError('停止记录无效，未控制进程')
        if self.process_identity(record['pid']) == record['identity']:
            return record
        # 进程已退出或 PID 已复用；不会操作新进程。
        if clean_stale:
            path.unlink()
        return None

    def remember_stop(self, sid, pid):
        if not pid:
            return
        identity = self.process_identity(pid)
        if identity is None:
            return
        path = self.paths(sid)[0] / 'stopping.json'
        if path.is_symlink():
            raise ServiceError('停止记录为符号链接，未操作')
        atomic_write(path, json.dumps(dict(root=str(self.root), service=sid,
                                         pid=pid, identity=identity)).encode())

    def state(self, sid, clean_stale_stop=True, strict=False):
        if sys.platform != 'darwin':
            return dict(loaded=False, owned=False, pid=None, detail='非 macOS，未查询进程')
        r = self.ctl('print', self.target(sid))
        if r.returncode:
            if strict:
                missing = re.search(r'Could not find service\s+"' + re.escape(self.label(sid)) + r'"\s+in domain',
                                    (r.stdout or '') + '\n' + (r.stderr or ''))
                if r.returncode != 113 or not missing:
                    raise ServiceError('launchd 查询失败，不能确认服务已停止；请核查当前用户会话与服务状态')
            pending = self.pending_stop(sid, clean_stale=clean_stale_stop)
            if pending:
                return dict(loaded=False, owned=True, pid=pending['pid'], stopping=True,
                            detail='停止中，原进程尚未退出')
            return dict(loaded=False, owned=False, pid=None, detail='未启动')
        path = re.search(r'^\s*path = (.+)$', r.stdout, re.M)
        owned = bool(path and Path(path[1].strip()) == self.paths(sid)[2] and self.owned(sid))
        pid = re.search(r'^\s*pid = (\d+)$', r.stdout, re.M)
        exit_match = re.search(r'^\s*last exit code = (\d+)$', r.stdout, re.M)
        detail = '进程运行' if pid else ('已退出，代码 ' + exit_match[1] if exit_match else '已注册，启动中或已退出（查看日志）')
        return dict(loaded=True, owned=owned, pid=int(pid[1]) if pid else None,
                    detail=detail if owned else '同名服务归属不符，不控制')

    def health(self, sid, data):
        self.health_meta.pop(sid, None)
        # 不用环境 HTTP_PROXY，不跟随重定向；只读取本机健康接口，绝不发推理。
        conn = http.client.HTTPConnection('127.0.0.1', data['port'], timeout=1)
        try:
            conn.request('GET', '/v1/models' if sid == 'sd-qwen' else '/health')
            r = conn.getresponse()
            body = r.read(65536)
            if r.status != 200:
                return False
            result = json.loads(body)
            if isinstance(result, dict):
                self.health_meta[sid] = result
            return (isinstance(result, dict) and isinstance(result.get('data'), list)) if sid == 'sd-qwen' else (isinstance(result, dict) and result.get('status') in ('ok', 'ready', 'healthy'))
        except (OSError, ValueError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    def sd_status(self, data):
        """只读查询实际已加载模型，不以磁盘目录推断运行状态。"""
        conn = http.client.HTTPConnection('127.0.0.1', data['port'], timeout=2)
        try:
            conn.request('GET', '/sdcpp/v1/models/status')
            response = conn.getresponse()
            result = json.loads(response.read(262144))
            return result if response.status == 200 and isinstance(result, dict) else {}
        except (OSError, ValueError, http.client.HTTPException):
            return {}
        finally:
            conn.close()

    def webui(self, data):
        """仅探测固定版本的根页面；没有真实前端时不把健康页冒充 WebUI。"""
        conn = http.client.HTTPConnection('127.0.0.1', data['port'], timeout=1)
        try:
            conn.request('GET', '/')
            response = conn.getresponse()
            content = response.read(262144).decode('utf-8', errors='replace').lower()
            return (response.status == 200 and 'text/html' in response.getheader('Content-Type', '').lower()
                    and '<html' in content and ('<script' in content or '<form' in content))
        except (OSError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    def configure(self, sid, data):
        base, path, plist = self.paths(sid)
        for candidate in (self.base, base, path, plist, base / 'server.json', base / 'stdout.log', base / 'stderr.log'):
            if candidate.is_symlink():
                raise ServiceError('服务路径存在符号链接，拒绝覆盖：' + str(candidate))
        if plist.exists() and not self.owned(sid):
            raise ServiceError('已有启动文件归属不符，不覆盖')
        base.mkdir(parents=True, exist_ok=True)
        # 只保存固定配方，不接受 shell 命令或用户提供的任意 ProgramArguments。
        if 'config' in data:
            atomic_write(base / 'server.json', json.dumps(data['config'], ensure_ascii=False, indent=2).encode())
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode())
        content = dict(Label=self.label(sid), ProgramArguments=data['args'], MacAIKitRoot=str(self.root),
                       RunAtLoad=True, KeepAlive=False, AbandonProcessGroup=False, ExitTimeOut=30,
                       WorkingDirectory=str(Path(data['binary']).parent),
                       StandardOutPath=str(base / 'stdout.log'), StandardErrorPath=str(base / 'stderr.log'),
                       EnvironmentVariables={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})
        atomic_write(plist, plistlib.dumps(content))
        return plist

    def wait_ready(self, sid, data):
        """等待本工具管理的进程和只读健康接口；不发推理、不重启、不杀进程。"""
        deadline = time.monotonic() + START_TIMEOUT
        next_progress = time.monotonic()
        while True:
            st = self.state(sid)
            if not st['loaded'] or not st['owned'] or st.get('stopping'):
                raise ServiceError('服务已退出或归属变化，未确认就绪')
            if st['pid'] and self.health(sid, data):
                return
            if not st['pid'] and st.get('detail', '').startswith('已退出'):
                raise ServiceError(st['detail'] + '；请查看日志')
            now = time.monotonic()
            if now >= deadline:
                raise ServiceError(f'等待 API 就绪超时（{START_TIMEOUT:g} 秒）；进程未被停止')
            if now >= next_progress:
                print_safe(SPECS[sid]['title'] + '：正在等待 API 就绪…')
                next_progress = now + START_PROGRESS
            time.sleep(min(START_POLL, deadline - now))

    def load_default_sd(self, data):
        """只在本次新启动后默认加载 Qwen；等待期间 Ctrl+C 不关闭服务。"""
        if not data.get('default_model'):
            return
        url = 'http://127.0.0.1:' + str(data['port'])
        runtime = sd_request(url, 'models/status', timeout=2)
        if runtime.get('state') == 'ready':
            return  # 保留其他客户端已加载的模型。
        if runtime.get('state') not in ('empty', 'loading'):
            raise ServiceError('SD.cpp 模型状态异常，未自动加载；请查看日志')
        if runtime['state'] == 'loading':
            self.wait_sd_loading(data)
            return
        files = sd_request(url, 'model-files').get('files', [])
        payload = sd_selection(data['default_model'], files)
        print_safe('SD.cpp：正在加载 Qwen Image 2.1 Turbo…')
        # load 是同步 HTTP；后台仅等待这一次请求，主线程提供进度并允许退出等待。
        result = queue.Queue()
        def load():
            try:
                result.put((True, sd_request(url, 'models/load', payload, timeout=SD_LOAD_TIMEOUT)))
            except Exception as error:
                result.put((False, error))
        threading.Thread(target=load, daemon=True).start()
        deadline = time.monotonic() + SD_LOAD_TIMEOUT
        next_progress = time.monotonic() + START_PROGRESS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServiceError('SD.cpp 默认模型加载等待超时；服务和加载请求未被停止，请查看状态与日志')
            try:
                ok, runtime = result.get(timeout=min(START_POLL, remaining))
                break
            except queue.Empty:
                if time.monotonic() >= next_progress:
                    print_safe('SD.cpp：模型仍在加载…')
                    next_progress = time.monotonic() + START_PROGRESS
        if not ok:
            raise ServiceError('默认加载请求未完成：' + str(runtime))
        if runtime.get('state') != 'ready' or runtime.get('recipe') != data['default_model']:
            raise ServiceError('SD.cpp 默认模型未确认就绪；请查看状态与日志')
        print_safe('SD.cpp：Qwen Image 2.1 Turbo 已加载。')

    def wait_sd_loading(self, data):
        """只观察正在加载的模型，不重复提交、不切换他人选择。"""
        deadline = time.monotonic() + SD_LOAD_TIMEOUT
        next_progress = time.monotonic()
        while True:
            state = self.sd_status(data).get('state')
            if state == 'ready':
                return
            if state in ('error', 'empty'):
                raise ServiceError('SD.cpp 模型未加载成功；请查看日志')
            now = time.monotonic()
            if now >= deadline:
                raise ServiceError('SD.cpp 模型加载等待超时；服务未被停止')
            if now >= next_progress:
                print_safe('SD.cpp：模型仍在加载…')
                next_progress = now + START_PROGRESS
            time.sleep(min(START_POLL, deadline - now))

    def refresh_inventory(self, selection='all'):
        """启动前只读扫描所选范围，安静补全说明；保留用户手工段落。"""
        from api_inventory import inventory, render
        target = self.models / 'API.md'
        if target.is_symlink():
            raise ServiceError('API.md 为符号链接，未启动服务')
        old = target.read_bytes().decode('utf-8') if target.exists() else ''
        if (old.count(API_BEGIN) != old.count(API_END) or old.count(API_BEGIN) > 1
                or (API_BEGIN in old and old.index(API_BEGIN) > old.index(API_END))):
            raise ServiceError('API.md 原生服务标记不完整，未覆盖手工内容')
        templates = json.loads((ROOT / 'config/api_templates.json').read_text())
        catalog = self.catalog
        if selection != 'all':
            # 单项启动不检查另一服务或 MLX App；只保留本项模型及其清单依赖。
            by_id = {model['id']: model for model in catalog['models']}
            needed = set()
            pending = list(SPECS[selection]['model_ids'])
            while pending:
                mid = pending.pop()
                if mid in needed:
                    continue
                needed.add(mid)
                pending.extend(by_id.get(mid, {}).get('requires', []))
            catalog = dict(catalog, models=[model for model in catalog['models'] if model['id'] in needed])
            templates = dict(templates, runtimes=[runtime for runtime in templates.get('runtimes', [])
                                                  if runtime['id'] == SPECS[selection]['component']])
        directory, install, models, runtimes = inventory(self.models, catalog, templates)
        # 启动仅补缺失模型/工具说明；显式“检查并补全 API.md”仍可重建检查区。
        document = render(directory, install, models, runtimes, templates)
        additions = []
        for section in re.findall(r'^#### .*?(?=^#{1,4} |^<!--|\Z)', document, re.M | re.S):
            model_id = re.search(r'^- 标识：(.*)$', section, re.M)
            key = ('- 标识：' + model_id.group(1)) if model_id else section.splitlines()[0]
            if not re.search(r'^' + re.escape(key) + r'\r?$', old, re.M):
                additions.append(section.rstrip())
        content = old
        if additions:
            content += ('\n\n' if old else '') + '## 启动时补充的调用说明\n\n这些是调用参考，不表示服务当前正在运行。\n\n' + '\n\n'.join(additions) + '\n'
        content = append_workflow(content, self.root, self.models, ROOT)
        if content != old:
            atomic_write(target, content.encode())
            self._inventory_added = True

    def start(self, selection='all'):
        if selection == QUEUE_SERVICE:
            return self.queue_action('start')
        if selection != 'all' and selection not in SPECS:
            raise ServiceError('未知服务')
        try:
            queue_error = None
            if selection == 'all':
                try:
                    self.queue_action('start')
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    queue_error = error
                    print_safe(str(error))
            self._start(selection)
            if queue_error:
                raise ServiceError(str(queue_error))
        except KeyboardInterrupt:
            print_safe('已退出等待，已提交的服务保持运行；可在服务状态中查看。')
            raise
        finally:
            if sys.platform == 'darwin':
                self.status(selection)
                self.write_api(selection)

    def queue_action(self, action, json_output=False):
        """复用原队列入口、固定 launchd 标签及默认状态目录，不另建实例。"""
        command = [sys.executable, str(ROOT / 'scripts/mlx_video_queue.py'), action]
        if json_output:
            command.append('--json')
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if json_output:
            row = json.loads(result.stdout)
            if not isinstance(row, dict) or row.get('service') != QUEUE_SERVICE:
                raise ServiceError('队列状态响应结构无效')
            if result.returncode and 'error' not in row:
                row['error'] = '队列状态查询失败'
            return row
        if result.stdout:
            print_safe(result.stdout.rstrip())
        if result.returncode:
            raise ServiceError('MLX-Serve 视频队列：' + (clean(result.stderr).strip() or '操作未完成'))

    def _start(self, selection='all'):
        if sys.platform != 'darwin':
            raise ServiceError('启停仅支持 macOS；没有启动进程')
        selected = list(SPECS) if selection == 'all' else [selection]
        self.refresh_inventory(selection)
        with self.lock():
            plan = []
            restart = set()
            failures = []
            for sid in selected:
                st = self.state(sid)
                if st.get('stopping'):
                    print_safe(SPECS[sid]['title'] + '：原进程仍在退出，请稍后重试')
                    continue
                if st['loaded']:
                    if st['owned'] and sid == 'sd-qwen' and self.read_settings(sid).get('mode') != 'native-model-management':
                        print_safe('SD.cpp：旧版服务仍在运行；请先停止 SD.cpp，再启动图像服务以启用模型切换。')
                        continue
                    if not st['owned']:
                        print_safe(SPECS[sid]['title'] + '：归属不符，未操作')
                        continue
                    if self.read_settings(sid)['host'] == '0.0.0.0':
                        try:
                            data = self.read_settings(sid)
                            self.wait_ready(sid, data)
                            if sid == 'sd-qwen' and self.sd_status(data).get('state') == 'loading':
                                self.wait_sd_loading(data)
                            print_safe(SPECS[sid]['title'] + '：已就绪，不重复启动')
                        except (OSError, ValueError) as error:
                            failures.append(sid)
                            print_safe(SPECS[sid]['title'] + '：' + str(error) + '；日志 ' + str(self.paths(sid)[0] / 'stderr.log'))
                        continue
                    restart.add(sid)
                try:
                    data = self.recipe(sid)
                    if sid not in restart:
                        available_port(data['port'], data['host'])
                    plan.append((sid, data))
                except (OSError, ValueError) as e:
                    print_safe(SPECS[sid]['title'] + '：未就绪，' + str(e))
            if not plan:
                if failures:
                    raise ServiceError('部分服务尚未就绪；请查看日志')
                return
            ports = [d['port'] for _, d in plan]
            if len(set(ports)) != len(ports):
                raise ServiceError('服务端口重复，未启动；请检查各服务 settings.json 的 port')
            # 即使 audio 懒加载，也为最大音频模型保留估算空间。已有进程实际占用已反映在可用页中。
            reserve = sum(d['reserve_bytes'] for _, d in plan) + 8 * 1024**3
            # 已运行的音频可能尚未加载权重，可用页不包含其后续请求的占用。
            # 仅只读核查归属和已保存预算；不扫描音频模型、不等待/重启/修改另一服务。
            if not any(sid == 'audio' for sid, _ in plan):
                audio_state = self.state('audio', clean_stale_stop=False)
                if audio_state['owned'] and (audio_state['loaded'] or audio_state.get('stopping') or audio_state['pid']):
                    audio_reserve = self.read_settings('audio').get('reserve_bytes')
                    if type(audio_reserve) is not int or audio_reserve <= 0:
                        raise ServiceError('已运行音频的内存预算记录无效；请先手动停止音频，再重试；未改动音频服务')
                    reserve += audio_reserve
                    print_safe(f'另为尚未停止的音频保留 {audio_reserve / 1024**3:.1f} GiB（懒加载）；不会停止或重配音频。')
            free = available_memory()
            print_safe('本次选择：' + '、'.join(SPECS[sid]['title'] for sid in selected))
            print(f'可回收内存估算 {free / 1024**3:.1f} GiB；本次保守预算 {reserve / 1024**3:.1f} GiB')
            if reserve > free * .85:
                hint = '可改选“仅启动音频”或“仅启动制图”，或先手动释放不用的模型' if selection == 'all' else '请先手动释放不用的模型后重试'
                raise ServiceError('当前内存预算不足，未启动本次待启动服务；' + hint + '。未停止其他服务或卸载桌面 App 模型；预算不是峰值保证')
            print('服务监听 0.0.0.0；无认证，仅限可信局域网或 VPN，勿映射公网。')
            for sid, data in plan:
                phase = '启动'
                try:
                    if sid in restart:
                        print_safe(SPECS[sid]['title'] + '：旧配置仅限本机，正在重启为所有网卡监听；当前任务将中断。')
                        result = self.ctl('bootout', self.target(sid))
                        if result.returncode or self.state(sid)['loaded']:
                            raise ServiceError('旧服务尚未确认停止，未改写配置')
                        available_port(data['port'], data['host'])
                    plist = self.configure(sid, data)
                    r = self.ctl('bootstrap', f'gui/{os.getuid()}', str(plist))
                    if r.returncode:
                        raise ServiceError(clean(r.stderr).strip() or 'launchd 拒绝启动')
                    phase = '就绪检查'
                    self.wait_ready(sid, data)
                    if sid == 'sd-qwen':
                        phase = '默认模型加载（服务保留）'
                        self.load_default_sd(data)
                except (OSError, ValueError, subprocess.SubprocessError) as e:
                    failures.append(sid)
                    print_safe(f"{SPECS[sid]['title']} {phase}失败：{e}；日志 {self.paths(sid)[0] / 'stderr.log'}")
            if failures:
                raise ServiceError('部分服务尚未就绪：' + '、'.join(SPECS[sid]['title'] for sid in failures) + '；其他服务保持当前状态')

    def stop(self, selection='all'):
        if selection == QUEUE_SERVICE:
            return self.queue_action('stop')
        if sys.platform != 'darwin':
            raise ServiceError('启停仅支持 macOS；没有停止进程')
        failures = []
        if selection == 'all':
            try:
                self.queue_action('stop')
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                failures.append(QUEUE_SERVICE)
                print_safe(str(error))
        with self.lock():
            for sid in list(SPECS) if selection == 'all' else [selection]:
                title = SPECS[sid]['title']
                try:
                    st = self.state(sid)
                    if not st['loaded'] and not st.get('stopping'):
                        print_safe(title + '：未启动')
                        continue
                    if not st['owned']:
                        raise ServiceError('归属不符，未停止')
                    if st['loaded']:
                        self.remember_stop(sid, st['pid'])
                        r = self.ctl('bootout', self.target(sid))
                        # 即使 bootout 返回错误，也核查是否已经退出，避免竞态误报。
                        error = clean(r.stderr).strip() if r.returncode else ''
                    else:
                        error = ''
                    print_safe(title + '：正在停止…')
                    deadline = time.monotonic() + STOP_TIMEOUT
                    while True:
                        current = self.state(sid)
                        pending = self.pending_stop(sid)
                        if not current['loaded'] and not pending:
                            print_safe(title + '：已停止；不会自动重启')
                            break
                        if current['loaded'] and not current['owned']:
                            raise ServiceError('服务归属发生变化，未继续操作')
                        if time.monotonic() >= deadline:
                            detail = ('；' + error) if error else ''
                            raise ServiceError('等待退出超时，尚未确认停止' + detail
                                               + '；日志 ' + str(self.paths(sid)[0] / 'stderr.log'))
                        time.sleep(STOP_POLL)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    failures.append(sid)
                    print_safe(title + '：' + str(error))
        if failures:
            raise ServiceError('未完全停止：' + '、'.join(SPECS[sid]['title'] if sid in SPECS else 'MLX-Serve 视频队列' for sid in failures))

    def status_json(self, selection='all'):
        """机器可读的只读快照；健康或权重驻留从不推断为空闲。"""
        result = dict(schema_version=1, toolkit_root=str(ROOT), root=str(self.root),
                      models_dir=str(self.models), supported=sys.platform == 'darwin', services=[])
        selected = list(SPECS) if selection == 'all' else ([] if selection == QUEUE_SERVICE else [selection])
        for sid in selected:
            row = dict(service=sid, title=SPECS[sid]['title'], configured=False,
                       loaded=None, owned=None, pid=None, stopping=None,
                       api_healthy=None, model_residency='unknown', workload='unknown',
                       stop_safety='unknown', atomic_shutdown_barrier=False)
            result['services'].append(row)
            if sys.platform != 'darwin':
                row['error'] = '非 macOS，未查询服务；不能据此判断已停止'
                continue
            try:
                state = self.state(sid, clean_stale_stop=False, strict=True)
                row.update(loaded=state['loaded'], owned=state['owned'], pid=state['pid'],
                           stopping=bool(state.get('stopping')), detail=state['detail'])
                if not state['loaded'] and not state['pid'] and not state.get('stopping'):
                    row.update(workload='not_running', stop_safety='not_needed')
                try:
                    data = self.read_settings(sid)
                except (OSError, ValueError, KeyError) as error:
                    row['configuration_note'] = clean(error)
                    continue
                row.update(configured=True, host=data['host'], port=data['port'],
                           configured_models_dir=data['models_dir'])
                if state['owned'] and state['pid'] and not state.get('stopping'):
                    healthy = self.health(sid, data)
                    row['api_healthy'] = healthy
                    if healthy and sid == 'sd-qwen' and data.get('mode') == 'native-model-management':
                        runtime = self.sd_status(data)
                        row['model_residency'] = runtime.get('state', 'unknown')
                        row['model_recipe'] = runtime.get('recipe')
                # 上游当前配置无统一、原子的“关闭入口并排空再停机”协议。
                # empty / ready / healthy 都不是任务空闲证明，也不授权自动 stop。
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                row['error'] = clean(error)
        if selection in ('all', QUEUE_SERVICE):
            try:
                row = self.queue_action('status', json_output=True)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                row = dict(service=QUEUE_SERVICE, title='MLX-Serve 视频队列', loaded=None,
                           owned=None, pid=None, api_healthy=None, model_residency='unknown',
                           workload='unknown', stop_safety='unknown', atomic_shutdown_barrier=False,
                           error=clean(error))
            result['services'].append(row)
        return result

    def status(self, selection='all'):
        if selection == QUEUE_SERVICE:
            return self.queue_action('status')
        queue_error = None
        if selection == 'all':
            try:
                self.queue_action('status')
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                queue_error = error
                print_safe(str(error))
        print_safe('模型目录：' + str(self.models))
        addresses = network_addresses()
        for sid in list(SPECS) if selection == 'all' else [selection]:
            spec = SPECS[sid]
            st = self.state(sid)
            try:
                data = self.read_settings(sid)
                healthy = bool(st['owned'] and st['pid'] and not st.get('stopping') and self.health(sid, data))
                desc = st['detail'] if not st['owned'] or st.get('stopping') else ('API 健康' if healthy else ('进程存在，API 尚未就绪' if st['pid'] else st['detail']))
                url = f" | http://127.0.0.1:{data['port']}" if healthy else ''
                print_safe(f"{spec['title']}：{desc}{url} | PID {st['pid'] or '-'}")
                print_safe('  监听：' + data['host'] + ':' + str(data['port']))
                if data['host'] == '0.0.0.0':
                    for address in addresses if healthy else []:
                        print_safe(f"  API：http://{address}:{data['port']}")
                has_ui = bool(healthy and self.webui(data))
                if has_ui:
                    print_safe(f"  WebUI：http://127.0.0.1:{data['port']}/")
                    if data['host'] == '0.0.0.0':
                        for address in addresses:
                            print_safe(f"  WebUI：http://{address}:{data['port']}/")
                else:
                    print_safe('  WebUI：未确认可用')
                if sid == 'sd-qwen' and healthy and data.get('mode') == 'native-model-management':
                    runtime = self.sd_status(data)
                    current = runtime.get('recipe') or {'empty':'未加载', 'loading':'加载中', 'error':'加载失败', 'ready':'命令行预加载'}.get(runtime.get('state'), '未确认')
                    print_safe('  当前模型：' + str(current))
                    print_safe('  切换模型：在 WebUI 选择配方及组件，点击 Load models。')
                backend = self.health_meta.get(sid, {}).get('backend')
                if healthy and backend:
                    print_safe('  后端自报：' + str(backend) + '（不是全 GPU 运算证明）')
                if data['models_dir'] != str(self.models):
                    print_safe('  当前进程/配置使用旧模型目录：' + data['models_dir'])
            except (OSError, ValueError, KeyError):
                print_safe(spec['title'] + '：未配置；' + st['detail'])
                try:
                    candidate = self.recipe(sid)
                    action = '仅启动制图' if sid == 'sd-qwen' else '仅启动音频'
                    print_safe('  检查通过：选择“' + action + '”即可单独配置并启动，也可选择“启动全部”。')
                    for reason in candidate.get('skipped_models', []):
                        print_safe('  跳过模型：' + reason)
                except (OSError, ValueError, KeyError) as error:
                    print_safe('  原因：' + str(error))
            print_safe('  日志：' + str(self.paths(sid)[0]))
        if queue_error:
            raise ServiceError(str(queue_error))

    def write_api(self, selection='all'):
        if not self.models.is_dir():
            return
        safe_root(self.models)
        target = self.models / 'API.md'
        if target.is_symlink():
            raise ServiceError('API.md 为符号链接，未写入')
        lines = [API_BEGIN, '## 原生独立服务（无需 llama-swap）', '', '以下为启动时记录的配置和探测结果，不是实时状态；停止服务不会改写此文档，当前状态请在服务管理中查看。', '']
        addresses = network_addresses()
        for sid in list(SPECS) if selection == 'all' else [selection]:
            try:
                d = self.read_settings(sid)
            except (OSError, ValueError, KeyError):
                continue
            st = self.state(sid)
            health = bool(st['owned'] and st['pid'] and not st.get('stopping') and self.health(sid, d))
            url = f"http://127.0.0.1:{d['port']}"
            has_ui = bool(health and self.webui(d))
            ui_text = url + '/' if has_ui else '未确认可用；稍后在服务管理查看状态刷新'
            if sid == 'sd-qwen' and d.get('mode') != 'native-model-management':
                lines += ['### SD.cpp（旧配置）', '', '- API：' + url, '- WebUI：' + ui_text, '- 日志：' + str(self.paths(sid)[0]), '尚未启用原生切换：先安装新版 SD.cpp，待任务结束后停止并重新启动服务。', '']
                continue
            api_label = 'API' if health else '配置地址（未确认就绪）'
            lines += [f"### {SPECS[sid]['title']}", '', f"- {api_label}：{url}", f"- WebUI：{ui_text}", f"- 状态：{'健康' if health else '未确认就绪'}", f"- 模型目录：{d['models_dir']}", f"- 日志：{self.paths(sid)[0]}", '', '```bash', f"curl --fail {shlex.quote(url + ('/v1/models' if sid == 'sd-qwen' else '/health'))}"]
            if sid == 'sd-qwen':
                lines += [f"curl --fail {shlex.quote(url + '/sdcpp/v1/recipes')}",
                          f"curl --fail {shlex.quote(url + '/sdcpp/v1/model-files')}",
                          f"curl --fail {shlex.quote(url + '/sdcpp/v1/models/status')}", '```', '',
                          '首次启动默认加载完整的 Qwen Image 2.1 Turbo；没有完整 Qwen 时保留空服务，不自动改选 Krea。重复启动保留当前模型。在 WebUI 可选择 Qwen Image 2.1 Turbo 或 Krea 2 Turbo、对应组件，再点击 Load models。切换沿用同一端口和进程，一次驻留一个图像模型。',
                          'API：先 GET /sdcpp/v1/model-files 取得实际文件 id，再 POST /sdcpp/v1/models/load。下面 helper 自动匹配返回的 id，不猜根目录前缀：', '', '```bash']
                helper = ROOT / 'scripts/sd_models.py'
                for mid in d['models']:
                    recipe = 'qwen-image-21-turbo' if mid == 'qwen21-turbo' else 'krea2-turbo'
                    lines += [f"python3 {shlex.quote(str(helper))} --url {shlex.quote(url)} load {recipe}"]
                lines += [f"curl --fail {shlex.quote(url + '/sdcpp/v1/models/unload')} -X POST -H 'Content-Type: application/json' -d '{{}}'", '```', '',
                          '原生 load JSON 格式：{"recipe":"qwen-image-21-turbo","models":{"diffusion_model":"返回的文件 id","llm":"返回的文件 id","vae":"返回的文件 id"}}。Krea 使用 recipe=krea2-turbo。绝对路径不能作为文件 id。',
                          'load 同步等待加载完成；另一个客户端可轮询 status（empty/loading/ready/error）。正在生成或排队时切换返回 409 model_busy，请等待，不强停。加载中推理返回 503 model_loading；加载失败可能留下空上下文。generation 的 model 字段不会自动切换模型。多客户端请串行协调“加载→生成”，避免其他客户端在两次请求间切换。', '', '```bash']
                payload = json.dumps({'prompt':'一只玻璃小鸟','n':1,'size':'1024x1024','output_format':'webp','output_compression':100}, ensure_ascii=False)
                lines += [f"curl --fail {shlex.quote(url + '/v1/images/generations')} -H 'Content-Type: application/json' -d {shlex.quote(payload)}", '```', '', '返回 JSON 的 data[0].b64_json 是 WebP 图片的 Base64，解码后保存为 .webp，不是把 JSON 改扩展名。质量 100 仍为有损编码。当前接口没有全局格式启动参数；其他客户端省略 output_format 时仍使用 PNG。GET /sdcpp/v1/capabilities 的 output_formats_by_mode.img_gen 应包含 webp；若不包含，当前二进制未启用 WebP，不能仅靠改扩展名解决。']
            else:
                lines += [f"curl --fail {shlex.quote(url + '/v1/models')}"]
                if 'qwen3-asr' in d['models']:
                    lines += [f"curl --fail {shlex.quote(url + '/v1/audio/transcriptions')} -F 'model=qwen3-asr' -F 'file=@/替换为录音.wav;type=audio/wav'"]
                if 'yue2' in d['models']:
                    payload = json.dumps({'model':'yue2','request':{'text':'[Verse]\nMorning light upon the sea','options':{'style':'English, acoustic pop','cot':'off'}}})
                    lines += [f"curl --fail {shlex.quote(url + '/v1/tasks/run')} -H 'Content-Type: application/json' -d {shlex.quote(payload)}"]
                lines += ['```', '', 'ASR 返回 text；YuE2 返回顶层 audio（base64 WAV）。两个模型共用一个服务，按需最多驻留一个，空闲五分钟卸载；全忙时切换可返回 503，请稍后重试。']
            if d['host'] == '0.0.0.0':
                lines += ['', '- 监听：0.0.0.0（所有网卡）；无认证，仅限可信局域网或 VPN，勿映射公网。']
                for address in addresses:
                    remote = f"http://{address}:{d['port']}"
                    lines += [('- 网卡 API：' if health else '- 网卡配置地址（未确认就绪）：') + remote, '- 网卡 WebUI：' + (remote + '/' if has_ui else '未确认可用')]
                if not addresses:
                    lines += ['- 未读取到非回环 IPv4 地址；请检查网络连接。']
                lines += ['- 网卡地址来自本机，其他设备能否连接仍取决于防火墙与网络权限。']
            else:
                lines += ['', '- 监听：127.0.0.1（仅本机）；主动启动本服务将迁移为所有网卡监听。']
            lines += ['']
        lines += [API_END, '']
        block = '\n'.join(lines)
        old = target.read_bytes().decode('utf-8') if target.exists() else ''
        if (old.count(API_BEGIN) != old.count(API_END) or old.count(API_BEGIN) > 1
            or (API_BEGIN in old and old.index(API_BEGIN) > old.index(API_END))):
            raise ServiceError('API.md 原生服务标记不完整，未覆盖手工内容')
        # 保留所有现有字节，按服务和实际端点补充缺失项，不重写旧状态或手工说明。
        additions = []
        for section in re.findall(r'^### .*?(?=^### |^<!--|\Z)', block, re.M | re.S):
            title = section.splitlines()[0]
            base = re.search(r'http://127\.0\.0\.1:\d+', section)
            existing = re.findall(r'^### .*?(?=^#{1,3} |^<!--|\Z)', old, re.M | re.S)
            found = [part for part in existing if part.splitlines()[0] == title and (not base or base.group(0) in part)]
            if not found:
                additions.append(section.rstrip())
                continue
            # 新增网卡/WebUI、模型加载命令或接口时只补该条，不重复整份服务说明。
            missing = []
            for line in section.splitlines():
                if line.startswith(('curl ', 'python3 ')) and line not in old:
                    missing.append('```bash\n' + line + '\n```')
                elif re.match(r'^- (?:网卡 )?(?:API|WebUI|配置地址[^：]*|网卡配置地址[^：]*)：http', line):
                    if line not in old and ('WebUI：' in line or line.split('：', 1)[1] not in old):
                        missing.append(line)
            if missing:
                additions.append(title + '\n\n' + '\n\n'.join(missing))
        content = old
        if additions:
            notice = '以下为启动时记录的调用地址与探测结果，不是实时状态；停止服务不改写本文档。当前状态请在服务管理中查看。'
            if not old:
                content = API_BEGIN + '\n## 原生独立服务（无需 llama-swap）\n\n' + notice + '\n\n' + '\n\n'.join(additions) + '\n' + API_END + '\n'
            else:
                content = old + ('\n' if old.endswith('\n') else '\n\n') + '## 新增服务调用说明\n\n' + notice + '\n\n' + '\n\n'.join(additions) + '\n'
        content = append_workflow(content, self.root, self.models, ROOT)
        if content != old:
            atomic_write(target, content.encode())
        if content != old or getattr(self, '_inventory_added', False):
            print_safe('已更新：' + str(target))
        self._inventory_added = False

    def logs(self, sid, follow=False, lines=60):
        base = (Path.home() / 'Library/Application Support/Mac AI Kit/mlx-video-queue'
                if sid == QUEUE_SERVICE else self.paths(sid)[0])
        handles = {}
        if base.is_symlink() or (base.parent if sid == QUEUE_SERVICE else self.base).is_symlink():
            raise ServiceError('日志目录为符号链接，未读取')
        try:
            for name in ('stdout.log', 'stderr.log'):
                path = base / name
                if path.is_symlink():
                    raise ServiceError('日志为符号链接，未读取')
                if path.is_file():
                    f = path.open(errors='replace')
                    # 最多读末尾 2 MiB，避免大日志占满内存。
                    f.seek(max(0, path.stat().st_size - 2 * 1024**2))
                    print_safe(f'--- {path} ---')
                    print_safe(''.join(deque(f, maxlen=lines)).rstrip())
                    handles[name] = f
            if not handles:
                print('尚无日志：服务可能尚未启动。')
                return
            if follow:
                print('实时日志；Ctrl+C 返回，不会停止服务。')
                while True:
                    for name, f in handles.items():
                        chunk = f.read(65536)
                        if chunk:
                            print_safe('[' + name + '] ' + chunk.rstrip())
                    time.sleep(.3)
        except KeyboardInterrupt:
            print('\n已退出日志查看，服务保持运行。')
        finally:
            for f in handles.values():
                f.close()


def menu(manager):
    from service_manager import menu as swap_menu, Manager as SwapManager, pause_menu
    script = Path(__file__).with_name('terminal_ui.sh')
    options = [('启动全部', '启动 SD.cpp / audio.cpp / MLX 视频队列（11235）；不启动 MLX App'),
               ('仅启动音频', '只启动 audio.cpp（转写／YuE2），只预检音频预算'),
               ('仅启动制图', '只启动 SD.cpp；默认加载完整 Qwen，不关闭音频'),
               ('停止全部', '关闭受管原生服务及视频队列，正在进行的任务会中断'),
               ('查看状态', '查看地址、进程、API 健康及日志位置'),
               ('查看日志', '选择服务，Ctrl+C 仅退出日志查看'),
               ('可选 llama-swap', '原有独立配置与启停；不参与上面的一键操作'),
               ('仅启动 MLX 视频队列', '11235 包装层；不加载模型，不启停 11234 MLX-Serve'),
               ('查看 MLX 视频队列状态', '单独查看队列进程、API 健康及日志位置')]
    def select(title, items):
        rfd, wfd = os.pipe()
        try:
            command = '. "$1"; UI_BACK_LABEL=返回; ui_select ' + shlex.quote(title) + ' ' + ' '.join(shlex.quote(x) for pair in items for x in pair)
            command += '; printf "%s" "$UI_CHOICE" >&"$2"'
            subprocess.run(['/bin/bash', '-c', command, 'native-menu', str(script), str(wfd)], pass_fds=(wfd,), check=True, cwd=menu_working_directory())
            os.close(wfd); wfd = -1
            choice = os.read(rfd, 32).decode().strip()
        finally:
            os.close(rfd)
            if wfd >= 0: os.close(wfd)
        return choice

    while True:
        choice = select('服务管理', options)
        if choice in ('', '0', 'q'): return
        try:
            if choice == '1': manager.start()
            elif choice == '2': manager.start('audio')
            elif choice == '3': manager.start('sd-qwen')
            elif choice == '4': manager.stop()
            elif choice in ('5', 'h'): manager.status()
            elif choice == '6':
                key = select('查看日志', [(SPECS['sd-qwen']['title'], '查看图像服务日志'), (SPECS['audio']['title'], '查看音频服务日志'), ('MLX 视频队列', '查看 11235 队列日志')])
                if key in ('', '0', 'q'): continue
                if key in ('1', '2'): manager.logs('sd-qwen' if key == '1' else 'audio', follow=True)
                elif key == '3': manager.logs(QUEUE_SERVICE, follow=True)
            elif choice == '7':
                swap_menu(SwapManager(manager.root, manager.models))
                continue
            elif choice == '8': manager.start(QUEUE_SERVICE)
            elif choice == '9': manager.status(QUEUE_SERVICE)

        except (OSError, ValueError, subprocess.SubprocessError) as e:
            print_safe('未完成：' + str(e))
        pause_menu()


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立原生服务一键启停与日志；MLX-Serve 仍由桌面 App 管理')
    parser.add_argument('--root', default=str(Path.home() / 'AI'), help='程序与服务根目录（不是模型目录）')
    parser.add_argument('--models-dir', default=str(Path.home() / 'AI/models'), help='已有模型根目录')
    parser.add_argument('action', nargs='?', choices=['menu','status','start','stop','logs'])
    parser.add_argument('--service', choices=['all', *SPECS, QUEUE_SERVICE], default='all', help='选择原生服务或 MLX 视频队列')
    parser.add_argument('--follow', action='store_true', help='持续显示新增日志，Ctrl+C 仅退出查看')
    parser.add_argument('--json', action='store_true', help='仅 status：输出只读 JSON；健康或模型驻留不等于任务空闲')
    args = parser.parse_args(argv)
    try:
        manager = NativeManager(args.root, args.models_dir)
        action = args.action or ('menu' if sys.stdin.isatty() else 'status')
        if args.json and action != 'status':
            raise ServiceError('--json 仅适用于 status，不执行启停')
        if action == 'menu': menu(manager)
        elif action == 'status':
            if args.json:
                result = manager.status_json(args.service)
                print(json.dumps(result, ensure_ascii=False))
                return 0 if result['supported'] and not any('error' in row for row in result['services']) else 2
            manager.status(args.service)
        elif action == 'logs':
            if args.service == 'all':
                raise ServiceError('日志请指定 --service sd-qwen、audio 或 mlx-video-queue')
            manager.logs(args.service, args.follow)
        else: getattr(manager, action)(args.service)
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        if args.json:
            print(json.dumps(dict(schema_version=1, error=clean(e)), ensure_ascii=False))
        else:
            print_safe('服务操作未完成：' + str(e))
        return 2
    except (KeyboardInterrupt, EOFError):
        print('\n已返回。'); return 130


if __name__ == '__main__':
    sys.exit(main())
