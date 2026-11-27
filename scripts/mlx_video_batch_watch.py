#!/usr/bin/env python3
"""Monitor MLX queue receipts or a manifest, and safely download completed jobs."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import random
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

ID = re.compile(r'^[a-f0-9]{32}$')
TERMINAL_HTTP = {400, 401, 403, 404}


class TransientDownloadError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(ProxyHandler({}), NoRedirect())


def stamp():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def canonical_base(value):
    if not isinstance(value, str):
        raise ValueError('base must be a URL')
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password or parts.path not in ('', '/') or parts.query or parts.fragment:
        raise ValueError('base must contain only scheme, host and port')
    return f'{parts.scheme}://{parts.netloc}'.rstrip('/')


def read_json(path):
    with path.open(encoding='utf-8') as f:
        return json.load(f)


def load_jobs(args):
    if args.watch_dir:
        root = args.watch_dir.resolve()
        entries = [(p, {'receipt': str(p), 'output_dir': str(p.parent)})
                   for p in sorted(root.rglob('提交响应.json'))]
        parent = root
        default_base = args.base
    else:
        manifest = args.manifest.resolve()
        parent = manifest.parent
        data = read_json(manifest)
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('jobs'), list):
            raise ValueError('manifest requires version 1 and a jobs array')
        entries = [(manifest, item) for item in data['jobs']]
        default_base = data.get('base', args.base)
    jobs = []
    errors = []
    for origin, item in entries:
        try:
            if not isinstance(item, dict):
                raise ValueError('invalid job entry')
            if bool(item.get('id')) == bool(item.get('receipt')):
                raise ValueError('provide exactly one of id or receipt')
            if item.get('receipt'):
                receipt = Path(item['receipt'])
                receipt = receipt if receipt.is_absolute() else parent / receipt
                job_id = read_json(receipt)['id']
            else:
                job_id = item['id']
            if not isinstance(job_id, str) or not ID.fullmatch(job_id):
                raise ValueError('invalid job id')
            base = canonical_base(item.get('base', default_base))
            if not isinstance(item.get('output_dir'), str) or not item['output_dir']:
                raise ValueError('output_dir is required')
            folder = Path(item['output_dir'])
            folder = (folder if folder.is_absolute() else parent / folder).resolve()
            jobs.append({'id': job_id, 'base': base, 'folder': folder, 'file': folder / f'{job_id}.mp4'})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f'{origin}: {exc}')
    identities = [(j['base'], j['id']) for j in jobs]
    destinations = [j['file'] for j in jobs]
    invalid = {i for i, j in enumerate(jobs) if identities.count(identities[i]) > 1 or destinations.count(j['file']) > 1}
    for i in sorted(invalid):
        errors.append(f"duplicate job or destination: {jobs[i]['id']} {jobs[i]['file']}")
    return [j for i, j in enumerate(jobs) if i not in invalid], errors


def log(job, event, **data):
    job['folder'].mkdir(parents=True, exist_ok=True)
    path = job['folder'] / f"{job['id']}-状态.jsonl"
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps({'observed_at': stamp(), 'event': event, **data}, ensure_ascii=False) + '\n')


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def verified_existing(job):
    dest = job['file']
    meta = dest.with_suffix('.mp4.job.json')
    if not dest.exists() and not meta.exists():
        return False
    if not dest.is_file() or not meta.is_file():
        raise ValueError(f'file or provenance missing; refusing overwrite: {dest}')
    info = read_json(meta)
    if (info.get('id') != job['id'] or info.get('base') != job['base'] or
            info.get('bytes') != dest.stat().st_size or info.get('sha256') != digest_file(dest)):
        raise ValueError(f'existing file provenance or checksum mismatch: {dest}')
    return True


def download(job, url):
    dest = job['file']
    folder = job['folder']
    folder.mkdir(parents=True, exist_ok=True)
    if verified_existing(job):
        return
    fd, tmp_name = tempfile.mkstemp(prefix=f".{job['id']}.", suffix='.part', dir=folder)
    temp = Path(tmp_name)
    h = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, 'wb') as target, OPENER.open(url, timeout=120) as response:
            if response.headers.get_content_type() != 'video/mp4':
                raise ValueError('download is not video/mp4')
            expected = response.headers.get('Content-Length')
            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                size += len(chunk)
                h.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if not size or (expected is not None and size != int(expected)):
            raise TransientDownloadError('empty or truncated download')
        info = {'id': job['id'], 'base': job['base'], 'bytes': size, 'sha256': h.hexdigest(), 'downloaded_at': stamp()}
        # Publish without replacing another file, including during concurrent runs.
        os.link(temp, dest)
        meta = dest.with_suffix('.mp4.job.json')
        with meta.open('x', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
            f.write('\n')
        log(job, 'downloaded', **info, path=str(dest))
    finally:
        temp.unlink(missing_ok=True)


def check(job):
    try:
        if verified_existing(job):
            return 'downloaded', None
        endpoint = f"{job['base']}/v1/video/jobs/{job['id']}"
        with OPENER.open(endpoint, timeout=20) as response:
            status = json.load(response)
        if status.get('id') != job['id']:
            raise ValueError('status response job id does not match receipt')
        state = status.get('status')
        log(job, 'status', response=status)
        if state == 'succeeded':
            download(job, endpoint + '/video.mp4')
            return 'downloaded', None
        if state == 'failed':
            return 'failed', status.get('error')
        if state not in ('queued', 'running'):
            raise ValueError(f'unknown status: {state}')
        return state, status.get('progress')
    except HTTPError as exc:
        if exc.code in TERMINAL_HTTP or 300 <= exc.code < 400:
            return 'error', f'HTTP {exc.code}'
        return 'retry', f'HTTP {exc.code}'
    except (URLError, TimeoutError, ConnectionError, HTTPException, TransientDownloadError) as exc:
        return 'retry', str(exc)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return 'error', str(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--watch-dir', type=Path, help='Recursively find 提交响应.json; download beside each receipt')
    group.add_argument('--manifest', type=Path, help='JSON manifest with job IDs or receipt paths and output directories')
    parser.add_argument('--base', help='Default queue base URL')
    parser.add_argument('--interval', type=float, default=60, help='Poll seconds, minimum 5')
    parser.add_argument('--workers', type=int, default=4, help='Maximum concurrent checks/downloads, 1–8')
    parser.add_argument('--once', action='store_true', help='Check existing jobs once and exit')
    parser.add_argument('--follow', action='store_true', help='Keep watching for newly added receipts or jobs')
    args = parser.parse_args()
    if args.interval < 5 or not 1 <= args.workers <= 8:
        parser.error('interval must be >= 5 and workers must be between 1 and 8')
    if args.watch_dir and not args.base:
        parser.error('--watch-dir requires --base')
    if args.once and args.follow:
        parser.error('--once and --follow cannot be combined')
    if args.watch_dir and not args.watch_dir.is_dir():
        parser.error('--watch-dir must be an existing directory')
    state = {}
    while True:
        try:
            jobs, errors = load_jobs(args)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(stamp(), '任务清单无效：', str(exc), flush=True)
            if not args.follow:
                return 1
            time.sleep(args.interval)
            continue
        for error in errors:
            print(stamp(), '跳过无效任务：', error, flush=True)
        if not jobs:
            if not args.follow:
                print('没有任务；使用 --follow 等待新增任务。', flush=True)
                return 1 if errors else 0
            time.sleep(args.interval)
            continue
        for job in jobs:
            key = (job['base'], job['id'])
            previous = state.get(key)
            if previous and previous.get('file') != str(job['file']):
                state[key] = {'state': 'error', 'detail': 'output directory changed; restart after review',
                              'file': previous['file']}
                print(stamp(), job['id'], 'error', state[key]['detail'], flush=True)
        now = time.monotonic()
        ready = [j for j in jobs if now >= state.get((j['base'], j['id']), {}).get('next', 0)
                 and state.get((j['base'], j['id']), {}).get('state') not in ('downloaded', 'failed', 'error')]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            pending = {pool.submit(check, j): j for j in ready}
            for future in as_completed(pending):
                job = pending[future]
                key = (job['base'], job['id'])
                try:
                    result, detail = future.result()
                except Exception as exc:
                    result, detail = 'error', f'unexpected error: {exc}'
                previous = state.get(key, {})
                failures = previous.get('failures', 0) + 1 if result == 'retry' else 0
                delay = min(300, args.interval * (2 ** min(failures, 3)))
                state[key] = {'state': result, 'detail': detail, 'failures': failures,
                              'file': str(job['file']),
                              'next': time.monotonic() + delay + random.uniform(0, min(5, delay * .1))}
                if result != previous.get('state') or detail != previous.get('detail'):
                    print(stamp(), job['id'], result, json.dumps(detail, ensure_ascii=False), flush=True)
                if result in ('failed', 'error', 'retry'):
                    try:
                        log(job, result, detail=detail)
                    except OSError as exc:
                        print(stamp(), job['id'], '无法写日志：', exc, flush=True)
        values = [state.get((j['base'], j['id']), {}).get('state') for j in jobs]
        if args.once:
            return 1 if errors or any(x in ('failed', 'error') for x in values) else (2 if 'retry' in values else 0)
        if not args.follow and all(x in ('downloaded', 'failed', 'error') for x in values):
            return 1 if errors or any(x in ('failed', 'error') for x in values) else 0
        deadlines = [state.get((j['base'], j['id']), {}).get('next', time.monotonic() + args.interval)
                     for j in jobs if state.get((j['base'], j['id']), {}).get('state')
                     not in ('downloaded', 'failed', 'error')]
        delay = min(args.interval, max(.2, min(deadlines) - time.monotonic())) if deadlines else args.interval
        time.sleep(delay)


if __name__ == '__main__':
    raise SystemExit(main())
