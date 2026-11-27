#!/usr/bin/env python3
"""Monitor one existing MLX video queue job and download its result."""
import argparse
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def timestamp():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True, help='Queue base URL, including port')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--job-id', help='Existing queue job ID')
    group.add_argument('--receipt', type=Path, help='JSON submission receipt containing id')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=60, help='Poll interval in seconds (minimum 5)')
    parser.add_argument('--once', action='store_true', help='Check once; download if complete')
    args = parser.parse_args()
    if args.interval < 5:
        parser.error('interval must be at least 5 seconds')
    job_id = args.job_id or json.loads(args.receipt.read_text(encoding='utf-8'))['id']
    if not re.fullmatch(r'[a-fA-F0-9]{32}', job_id):
        parser.error('invalid queue job ID')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip('/')
    status_url = f'{base}/v1/video/jobs/{job_id}'
    log_path = args.output_dir / f'{job_id}-状态.jsonl'

    def record(event, **extra):
        with log_path.open('a', encoding='utf-8') as log:
            log.write(json.dumps({'observed_at': timestamp(), 'event': event, **extra}, ensure_ascii=False) + '\n')

    previous = None
    failures = 0
    while True:
        try:
            with urlopen(status_url, timeout=20) as response:
                data = json.load(response)
            failures = 0
            state = data.get('status')
            record('status', response=data)
            visible = (state, json.dumps(data.get('progress'), sort_keys=True, ensure_ascii=False))
            if visible != previous:
                print(timestamp(), job_id, state, visible[1], flush=True)
                previous = visible
            if state == 'failed':
                print('任务失败：', data.get('error'), flush=True)
                return 1
            if state == 'succeeded':
                temporary = args.output_dir / f'{job_id}.mp4.part'
                final = args.output_dir / f'{job_id}.mp4'
                with urlopen(f'{status_url}/video.mp4', timeout=120) as response, temporary.open('wb') as target:
                    shutil.copyfileobj(response, target, length=1024 * 1024)
                os.replace(temporary, final)
                record('downloaded', path=str(final), bytes=final.stat().st_size)
                print('已下载：', final, flush=True)
                return 0
            if state not in ('queued', 'running'):
                raise ValueError('Unexpected job status: ' + str(state))
        except HTTPError as exc:
            record('http_error', status_code=exc.code)
            if exc.code in (400, 401, 403, 404):
                print('查询或下载失败：HTTP', exc.code, flush=True)
                return 1
            failures += 1
        except (URLError, TimeoutError, OSError) as exc:
            failures += 1
            record('network_error', error=str(exc))
        if args.once:
            return 2 if failures else 0
        delay = min(300, args.interval * (2 ** min(failures, 3)))
        time.sleep(delay + random.uniform(0, min(5, delay * 0.1)))


if __name__ == '__main__':
    sys.exit(main())
