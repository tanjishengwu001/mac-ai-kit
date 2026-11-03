#!/usr/bin/env python3
"""仅标准库：用 uvx 隔离运行 aiperf，对已有无认证 OpenAI 兼容接口做多并发压力测试。"""
from __future__ import annotations

import argparse
import codecs
import datetime as dt
import html
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import string
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from path_validation import validate_model_root
from benchmark_discovery import discover, choose, resolve_tokenizer, default_model_root

PACKAGE = 'aiperf==0.13.0'
DEFAULT_URL = 'http://127.0.0.1:11234/v1'
DEFAULT_CONCURRENCY = (1, 4, 8)
DEFAULT_REQUEST_COUNT = 16
MAX_REPORT_BYTES = 20_000_000
# 摘要列：metric tag -> (标题, 单位)
SUMMARY_METRICS = (
    ('time_to_first_token', '首 token 延迟', 'ms'),
    ('request_latency', '请求时延', 'ms'),
    ('inter_token_latency', '逐 token 间隔', 'ms'),
    ('output_token_throughput', '输出吞吐', 'tok/s'),
    ('request_throughput', '请求吞吐', 'req/s'),
)
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def endpoint(value: str) -> str:
    """校验接口地址并补齐 /v1；不接受凭据、查询参数或片段。"""
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError('接口地址不能含空白或控制字符')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('接口地址或端口无效') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or '?' in value or '#' in value
            or '\\' in value or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('仅支持无账号密码、查询参数或片段的 HTTP(S) 接口地址')
    return value.rstrip('/') + ('/v1' if parsed.path in ('', '/') else '')


def aiperf_base(url: str) -> str:
    """aiperf 按 --endpoint-type 自动拼 /v1/chat/completions，这里去掉多余的 /v1。"""
    parsed = urlsplit(url)
    path = parsed.path
    if path.endswith('/chat/completions'):
        return url
    if path.endswith('/v1'):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip('/'), '', ''))


def concurrencies(value: str) -> tuple[int, ...]:
    items = [part.strip() for part in value.split(',')]
    if not items or any(not item.isdecimal() for item in items):
        raise ValueError('并发档位须为逗号分隔的整数，例如 1,4,8')
    numbers = []
    for item in items:
        number = int(item)
        if not 1 <= number <= 1024:
            raise ValueError('并发档位范围：1–1024')
        if number not in numbers:
            numbers.append(number)
    if len(numbers) > 8:
        raise ValueError('最多 8 个并发档位；档位过多会让单次压测时间成倍增加')
    return tuple(numbers)


def default_report_directory() -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    return ROOT / 'output' / 'reports' / ('aiperf-benchmark-' + stamp + '-' + '%08x' % random.getrandbits(32))


def output_directory(value: str) -> Path:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('输出目录不能含控制字符')
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts or path == Path('/'):
        raise ValueError('请使用绝对目录（可用 ~/），不能是 / 或含 ..')
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError('请选择真实目录，不使用符号链接目录')
    if path.parts[:2] == ('/', 'Volumes'):
        if len(path.parts) < 3 or not os.path.ismount(Path('/Volumes') / path.parts[2]):
            raise ValueError('外置磁盘尚未挂载')
    return path


def clean_environment(home: Path, cache: Path) -> dict[str, str]:
    """白名单环境：不继承代理、密钥、令牌或 Python 注入变量。"""
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT')
           if key in os.environ}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / 'config'), XDG_CACHE_HOME=str(home / 'cache'),
               HF_HOME=str(home / 'huggingface'), HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
               UV_NO_CONFIG='1', UV_KEYRING_PROVIDER='disabled', UV_CACHE_DIR=str(cache / 'uv'),
               HF_HUB_CACHE=str(cache / 'hub'), NO_COLOR='1', TERM='dumb',
               PYTHONUNBUFFERED='1', MPLBACKEND='Agg')
    return env


def tokenizer_choice(model_row, models_dir: Path) -> dict:
    """同源本地 tokenizer 优先；不确定时退 builtin，并记录原因。"""
    try:
        resolution = resolve_tokenizer(model_row, models_dir)
    except EOFError:
        raise
    except (ValueError, OSError) as exc:
        return {'kind': 'builtin', 'value': 'builtin',
                'evidence': ['同源 tokenizer 未确定，改用内置近似词表：' + str(exc)],
                'approximate': True}
    if resolution.get('kind') == 'local':
        return dict(resolution, approximate=False)
    if resolution.get('kind') == 'public':
        return dict(resolution, approximate=False, needs_network=True)
    return {'kind': 'builtin', 'value': 'builtin', 'evidence': ['未知来源'], 'approximate': True}


def command(uvx: str, base: str, model: str, tokenizer: str, levels: tuple[int, ...],
            request_count: int, duration: float | None, warmup: int, timeout: float,
            artifacts: Path, plot: bool) -> list[str]:
    cmd = [uvx, '--no-config', '--from', PACKAGE, 'aiperf', 'profile',
           '--url', base, '--model', model,
           '--endpoint-type', 'chat', '--streaming',
           '--tokenizer', tokenizer,
           '--concurrency', ','.join(str(level) for level in levels),
           '--ui-type', 'simple',
           '--artifact-dir', str(artifacts),
           '--request-timeout-seconds', '%g' % timeout]
    if duration is not None:
        cmd += ['--benchmark-duration', '%g' % duration]
    else:
        cmd += ['--request-count', str(request_count)]
    if warmup > 0:
        # 预热固定单并发，正式阶段才用真实并发。
        cmd += ['--warmup-request-count', str(warmup), '--warmup-concurrency', '1']
    if plot:
        cmd.append('--auto-plot')
    return cmd


def finite(value, key=None) -> float | None:
    if isinstance(value, dict):
        value = value.get(key) if key else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def stats_of(entry) -> dict:
    return entry if isinstance(entry, dict) else {}


def sweep_rows(artifacts: Path) -> list[dict]:
    """从 sweep 汇总读取每档指标；结构不符时返回空列表，不臆造数据。"""
    for name in ('profile_export_aiperf_sweep.json', 'aggregate/profile_export_aiperf_aggregate.json'):
        data = read_json(artifacts / name)
        combos = data.get('per_combination_metrics')
        if isinstance(combos, dict):
            combos = list(combos.values())
        if not isinstance(combos, list) or not combos:
            continue
        rows = []
        for combo in combos:
            if not isinstance(combo, dict):
                continue
            params = combo.get('parameters') if isinstance(combo.get('parameters'), dict) else {}
            metrics = combo.get('metrics') if isinstance(combo.get('metrics'), dict) else {}
            level = None
            for key in ('concurrency', 'concurrency_level', 'concurrent_requests'):
                if isinstance(params.get(key), int):
                    level = params[key]
                    break
            label = ', '.join('%s=%s' % (k, v) for k, v in sorted(params.items())) or '—'
            values = {}
            for tag, _title, _unit in SUMMARY_METRICS:
                stats = stats_of(metrics.get(tag))
                values[tag] = finite(stats.get('mean')) if stats else finite(metrics.get(tag))
            rows.append({'level': level, 'label': label, 'metrics': values, 'source': name})
        return rows
    return []


def run_rows(artifacts: Path) -> list[dict]:
    """扫描每个并发档的 run 级 JSON，作为 sweep 汇总缺失时的依据。"""
    rows = []
    if not artifacts.is_dir():
        return rows
    for folder in sorted(artifacts.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        match = re.fullmatch(r'concurrency_(\d+)', folder.name)
        data = {}
        for name in ('profile_export_aiperf.json', 'aggregate/profile_export_aiperf_aggregate.json'):
            data = read_json(folder / name)
            if data:
                break
        if not data:
            continue
        metrics = data.get('metrics') if isinstance(data.get('metrics'), dict) else data
        values = {}
        for tag, _title, _unit in SUMMARY_METRICS:
            entry = stats_of(metrics.get(tag))
            values[tag] = finite(entry.get('avg')) or finite(entry.get('mean'))
        rows.append({'level': int(match.group(1)) if match else None,
                     'label': 'concurrency=%s' % (match.group(1) if match else '—'),
                     'metrics': values, 'source': folder.name})
    return rows


def collect_artifacts(artifacts: Path) -> list[Path]:
    if not artifacts.is_dir():
        return []
    found = []
    for path in sorted(artifacts.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in ('.json', '.csv', '.png', '.log', '.txt', '.jsonl') and path.stat().st_size:
            found.append(path)
    return found


def write_json(path: Path, payload: dict) -> None:
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def render_html(rows: list[dict], artifacts: list[Path], meta: dict, artifacts_dir: Path) -> str:
    escaped = html.escape
    body = []
    if rows:
        head = ''.join('<th>%s%s</th>' % (escaped(title), ' (%s)' % escaped(unit) if unit else '')
                       for _tag, title, unit in [('l', '并发', '')] + list(SUMMARY_METRICS))
        lines = []
        for row in rows:
            cells = ['<td>%s</td>' % escaped(str(row['level'] if row['level'] is not None else row['label']))]
            for tag, _title, _unit in SUMMARY_METRICS:
                value = row['metrics'].get(tag)
                cells.append('<td>%s</td>' % ('—' if value is None else '%.2f' % value))
            lines.append('<tr>' + ''.join(cells) + '</tr>')
        body.append('<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (head, ''.join(lines)))
        body.append('<p class="note">并发档指标来自 %s；单档只有一次测量时，均值即该次结果。</p>'
                    % escaped(', '.join(sorted({r['source'] for r in rows}))))
    else:
        body.append('<p class="warn">未从 aiperf 产物中解析到汇总指标（可能压测失败或导出格式不同）。'
                    '请直接查看下方原始 CSV / JSON，本页不填写推测值。</p>')
    images = [p for p in artifacts if p.suffix.lower() == '.png']
    if images:
        body.append('<h2>图表</h2>')
        for image in images:
            rel = image.relative_to(artifacts_dir).as_posix()
            body.append('<figure><img src="%s" alt="%s"><figcaption>%s</figcaption></figure>'
                        % (escaped(rel), escaped(image.name), escaped(rel)))
    if artifacts:
        items = ''.join('<li><a href="%s">%s</a> <span class="size">%s</span></li>'
                        % (escaped(p.relative_to(artifacts_dir).as_posix()),
                           escaped(p.relative_to(artifacts_dir).as_posix()), human_size(p.stat().st_size))
                        for p in artifacts)
        body.append('<h2>原始产物</h2><ul class="files">%s</ul>' % items)
    notes = ''.join('<li>%s</li>' % escaped(item) for item in meta.get('notes', []))
    template = string.Template("""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIPerf 并发压力测试报告</title>
<style>
:root{color-scheme:light dark}
body{margin:0;padding:32px;font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
color:#1c1c1e;background:#f7f7f9}
main{max-width:960px;margin:0 auto;background:#fff;border-radius:14px;padding:32px 36px;
box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:28px 0 10px}
.sub{color:#6b6b70;font-size:13px;margin:0 0 22px}
dl{display:grid;grid-template-columns:120px 1fr;gap:6px 14px;font-size:14px;margin:0}
dt{color:#6b6b70}dd{margin:0;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:9px 10px;text-align:right;border-bottom:1px solid #e6e6ea}
th:first-child,td:first-child{text-align:left}
thead th{color:#6b6b70;font-weight:600;font-size:13px;border-bottom:1px solid #d7d7dc}
tbody tr:hover{background:#fafafc}
.note{color:#6b6b70;font-size:13px}
.warn{background:#fff6e5;border:1px solid #f0d9a8;border-radius:8px;padding:12px 14px;font-size:14px}
ul.files{padding-left:18px;font-size:14px}ul.files li{margin:3px 0}
.size{color:#9a9aa0;font-size:12px}
figure{margin:16px 0}img{max-width:100%;border:1px solid #e6e6ea;border-radius:8px}
figcaption{color:#6b6b70;font-size:12px;margin-top:6px}
ul.notes{font-size:13px;color:#4a4a4f;padding-left:18px}
@media (prefers-color-scheme:dark){
body{background:#141416;color:#f2f2f5}main{background:#1c1c1f;box-shadow:none}
dt,th,.note,figcaption,.sub{color:#9b9ba2}th,td{border-color:#2c2c31}thead th{border-color:#3a3a40}
tbody tr:hover{background:#232328}.warn{background:#2e2718;border-color:#5a4a24}
img{border-color:#2c2c31}a{color:#6cb6ff}.size{color:#77777e}ul.notes{color:#c4c4cb}}
</style></head><body><main>
<h1>AIPerf 并发压力测试报告</h1>
<p class="sub">${created} · 并发 ${levels} · ${stop}</p>
<dl>
<dt>接口</dt><dd>${base}</dd>
<dt>模型</dt><dd>${model}</dd>
<dt>分词器</dt><dd>${tokenizer}</dd>
<dt>预热</dt><dd>${warmup}</dd>
<dt>退出码</dt><dd>${exit_code}</dd>
</dl>
${body}
<h2>口径说明</h2>
<ul class="notes">${notes}</ul>
</main></body></html>
""")
    return template.substitute({
        'created': escaped(meta.get('created', '')),
        'levels': escaped(meta.get('levels', '')),
        'stop': escaped(meta.get('stop', '')),
        'base': escaped(meta.get('base', '')),
        'model': escaped(meta.get('model', '')),
        'tokenizer': escaped(meta.get('tokenizer', '')),
        'warmup': escaped(meta.get('warmup', '')),
        'exit_code': escaped(str(meta.get('exit_code', ''))),
        'body': '\n'.join(body),
        'notes': notes,
    })


def human_size(size: int) -> str:
    for unit in ('B', 'KB', 'MB'):
        if size < 1024 or unit == 'MB':
            return ('%d %s' % (size, unit)) if unit == 'B' else ('%.1f %s' % (size, unit))
        size /= 1024.0
    return '%d B' % size


def pump(handle, log, decoder) -> bool:
    """旁路读取日志并过滤控制序列；返回是否读到新内容。"""
    raw = log.read(8192)
    if not raw:
        return False
    text = decoder.decode(raw)
    if text:
        handle.write(ANSI.sub('', text))
        handle.flush()
    return True


def ensure_dir(path: Path, mode: int | None = None) -> None:
    """补目录；已存在时不再调用 mkdir，避免某些文件系统代理重复创建报错。"""
    if path.exists():
        return
    if mode is None:
        path.mkdir(parents=True)
    else:
        path.mkdir(parents=True, mode=mode)


def run(uvx: str, base: str, model: str, tokenizer: dict, levels: tuple[int, ...],
        request_count: int, duration: float | None, warmup: int, timeout: float,
        directory: Path, parent: Path, plot: bool, note: str) -> int:
    ensure_dir(directory)
    artifacts = directory / 'artifacts'
    ensure_dir(artifacts, 0o700)
    cache = parent / '.aiperf-cache'
    ensure_dir(cache, 0o700)
    runtime = directory / 'runtime-home'
    ensure_dir(runtime, 0o700)
    cmd = command(uvx, base, model, str(tokenizer['value']), levels, request_count,
                  duration, warmup, timeout, artifacts, plot)
    stop_desc = ('每档 %g 秒' % duration) if duration is not None else ('每档 %d 请求' % request_count)
    record = {'schema_version': 1, 'created': dt.datetime.now(dt.timezone.utc).isoformat(),
              'tool': PACKAGE, 'base_url': base, 'model': model, 'tokenizer': tokenizer,
              'concurrency': list(levels), 'request_count': request_count, 'duration_s': duration,
              'warmup_requests': warmup, 'warmup_concurrency': 1 if warmup else 0,
              'streaming': True, 'endpoint_type': 'chat', 'auto_plot': plot,
              'request_timeout_s': timeout, 'note': note, 'command': cmd, 'exit_code': None}
    write_json(directory / 'run.json', record)
    print('报告目录：%s' % directory.resolve())
    print('准备隔离环境并启动 aiperf；首次运行会联网安装 %s（只下载 Python 包，不下载模型权重）。' % PACKAGE)
    print('下方为 aiperf 原始输出（Ctrl-C 中断）。', flush=True)
    log_path = directory / 'run.log'
    process = None
    code = 1
    try:
        with log_path.open('xb') as log:
            process = subprocess.Popen(cmd, cwd=str(directory), env=clean_environment(runtime, cache),
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            started = last = time.monotonic()
            with log_path.open('rb') as reader:
                while process.poll() is None:
                    pump(sys.stdout, reader, decoder)
                    time.sleep(0.2)
                    now = time.monotonic()
                    if now - last >= 30:
                        print('[已运行 %d 秒 · 等待压测进展（Ctrl-C 中断）]' % int(now - started), flush=True)
                        last = now
                while pump(sys.stdout, reader, decoder):
                    pass
        code = process.returncode
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, 15)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, 9)
                process.wait()
        record['interrupted'] = True
        print('\n已中断；保留已生成的产物，不重试。')
        code = 130
    finally:
        if process is not None and process.returncode is None:
            code = 130
    artifacts_found = collect_artifacts(artifacts)
    rows = sweep_rows(artifacts) or run_rows(artifacts)
    usable = any(any(v is not None for v in row['metrics'].values()) for row in rows)
    if not usable:
        rows = []
    tokenizer_desc = str(tokenizer['value'])
    if tokenizer.get('approximate'):
        tokenizer_desc += '（近似词表，token 数为估算）'
    elif tokenizer.get('needs_network'):
        tokenizer_desc += '（从 Hugging Face 下载词表文件，不含权重）'
    notes = [
        '这是并发压力测试：关注同一服务在不同并发下的延迟与吞吐变化。',
        '与另外两项 benchmark 的“长上下文单请求吞吐”语料、采样和计时口径不同，结果不可直接互比。',
        '每次预热使用单并发 1 次请求，仅消除冷启动；正式阶段才施加所选并发。',
        '预热后的首 token 延迟代表热态；要测冷启动请重启服务并设 --warmup 0。',
    ]
    if tokenizer.get('approximate'):
        notes.append('未找到同源 tokenizer，使用 aiperf 内置近似词表；token 类指标存在系统偏差，延迟与并发结论仍有效。')
    if warmup:
        notes.append('预热与正式阶段共用同一合成语料生成器，可能命中服务端前缀缓存，使 TTFT 偏低。')
    if code != 0:
        notes.append('aiperf 退出码 %s；已生成的产物保留，未解析到的指标不补零、不推断。' % code)
    meta = {'created': record['created'][:19].replace('T', ' ') + ' UTC',
            'levels': ', '.join(map(str, levels)), 'stop': stop_desc, 'base': base, 'model': model,
            'tokenizer': tokenizer_desc,
            'warmup': ('每档 %d 次（并发 1）' % warmup) if warmup else '未启用',
            'exit_code': code, 'notes': notes}
    report = directory / 'report.html'
    report.write_text(render_html(rows, artifacts_found, meta, directory), encoding='utf-8')
    record.update(exit_code=code, artifacts=[p.relative_to(directory).as_posix() for p in artifacts_found],
                  summary=[{'level': row['level'], 'label': row['label'],
                            'metrics': row['metrics'], 'source': row['source']} for row in rows])
    write_json(directory / 'run.json', record)

    print('\n并发 / 首 token 延迟 ms / 请求时延 ms / 输出吞吐 tok/s / 请求吞吐 req/s')
    if rows:
        for row in rows:
            values = [row['metrics'].get(tag) for tag, _t, _u in SUMMARY_METRICS]
            print(' / '.join([str(row['level'] if row['level'] is not None else row['label'])]
                             + ['—' if value is None else '%.2f' % value for value in values]))
    else:
        print('未解析到汇总指标；请查看 aiperf 原始 CSV/JSON 与 run.log。')
    print('报告：%s' % report.resolve())
    return 0 if code == 0 else (code if 0 < code <= 125 else 1)


class ChineseHelpFormatter(argparse.HelpFormatter):
    def _format_usage(self, usage, actions, groups, prefix):
        return super()._format_usage(usage, actions, groups, prefix or '用法：')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False, formatter_class=ChineseHelpFormatter)
    parser._positionals.title = '位置参数'
    parser._optionals.title = '选项'
    parser.add_argument('-h', '--help', action='help', help='显示帮助并退出')
    parser.add_argument('--base-url', help='接口地址（默认 http://127.0.0.1:11234/v1，不使用代理配置）')
    parser.add_argument('--root', default=str(Path.home() / 'AI'), help='兼容工具包参数；不用于选择接口')
    parser.add_argument('--models-dir', help='模型根目录；默认沿用菜单保存的设置')
    parser.add_argument('--model', help='精确模型 ID；省略时自动选择唯一模型或交互选择')
    parser.add_argument('--concurrency', default='1,4,8', help='并发档位，逗号分隔（默认 1,4,8，最多 8 档）')
    parser.add_argument('--request-count', type=int, default=DEFAULT_REQUEST_COUNT, help='每档请求数（默认 40）')
    parser.add_argument('--duration', type=float, help='每档按时长停止（秒），与 --request-count 互斥')
    parser.add_argument('--warmup', type=int, default=1, help='每档预热次数，并发固定为 1；0 表示不预热（默认 1）')
    parser.add_argument('--tokenizer', help='覆盖分词器：本地目录、HF 仓库或 builtin；默认自动匹配同源词表')
    parser.add_argument('--output', help='报告父目录；默认项目内 output/reports，每次新建时间戳子目录')
    parser.add_argument('--timeout', type=float, default=600, help='单请求超时秒数（默认 600）')
    parser.add_argument('--no-plot', action='store_true', help='不请求 aiperf 生成 PNG 图表')
    parser.add_argument('--note', default='', help='报告备注；仅记录，不修改服务设置')
    args = parser.parse_args(argv)
    given = sys.argv[1:] if argv is None else list(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout 必须为有限正数')
    if args.duration is not None and (not math.isfinite(args.duration) or not 5 <= args.duration <= 86400):
        parser.error('--duration 范围：5–86400 秒')
    if args.duration is not None and '--request-count' in given:
        parser.error('--duration 与 --request-count 只能选一个')
    if not 0 <= args.warmup <= 100:
        parser.error('--warmup 范围：0–100')
    if not 1 <= args.request_count <= 100000:
        parser.error('--request-count 范围：1–100000')
    try:
        levels = concurrencies(args.concurrency)
        models_dir = validate_model_root(args.models_dir if args.models_dir is not None else default_model_root())
        validate_model_root(args.root)
    except (ValueError, OSError) as exc:
        print('无法完成：%s' % exc, file=sys.stderr)
        return 1
    uvx = shutil.which('uvx')
    if not uvx:
        print('需要已安装的 uvx；请先在「安装运行工具」准备基础环境。不会自动安装。', file=sys.stderr)
        return 1
    print('AIPerf 并发压力测试')
    try:
        value = args.base_url
        if value is None:
            value = input('接口地址 [%s]（q 取消）：' % DEFAULT_URL).strip() or DEFAULT_URL
        if value.lower() == 'q':
            raise EOFError
        url = endpoint(value)
        rows = discover(url)
        selected = choose(rows, '请选择测试模型：', lambda row: row['id'])
        model = selected['id']
        tokenizer = ({'kind': 'override', 'value': str(Path(args.tokenizer).expanduser().resolve()) if
                      args.tokenizer.startswith(('/', './', '../', '~/')) else args.tokenizer,
                      'evidence': ['命令指定'], 'approximate': args.tokenizer == 'builtin'}
                     if args.tokenizer else tokenizer_choice(selected, models_dir))
        if args.output is not None:
            parent = output_directory(args.output)
            stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
            directory = parent / ('aiperf-benchmark-' + stamp + '-' + '%08x' % random.getrandbits(32))
        else:
            directory = default_report_directory()
            parent = directory.parent
        if (directory / 'run.json').exists():
            print('输出目录已有 run.json，请指定新目录，避免覆盖。', file=sys.stderr)
            return 1
        if tokenizer.get('approximate'):
            print('提示：未确定同源 tokenizer，使用 builtin 近似词表；token 数为估算。')
        elif tokenizer.get('needs_network'):
            print('提示：本地未找到同源词表，将从 Hugging Face 下载 %s 的 tokenizer 文件（不下载权重）。' % tokenizer['value'])
        print('\n接口：%s\n模型：%s\n并发档位：%s\n%s\n预热：%s'
              % (aiperf_base(url), model, ', '.join(map(str, levels)),
                 ('每档 %g 秒' % args.duration) if args.duration is not None else ('每档 %d 请求' % args.request_count),
                 ('每档 %d 次（并发 1）' % args.warmup) if args.warmup else '不预热'))
        print('报告目录：%s' % directory.resolve())
        return run(uvx, aiperf_base(url), model, tokenizer, levels, args.request_count, args.duration,
                   args.warmup, args.timeout, directory, parent, not args.no_plot, args.note)
    except EOFError:
        print('\n已取消。')
        return 0
    except KeyboardInterrupt:
        print('\n已取消。')
        return 0
    except (ValueError, OSError) as exc:
        print('无法完成：%s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
