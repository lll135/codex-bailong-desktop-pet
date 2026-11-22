"""Normalize an Antigravity desktop Sidecar's local quota snapshot."""
from datetime import datetime
import json
import math
from pathlib import Path
import time


WINDOW_NAMES = {'5h': '5小时', 'weekly': '一周'}


def snapshot_path(profile):
    return Path(profile) / '.gemini' / 'config' / 'agy-gemini-pet' / 'quota_snapshot.json'


def reset_time(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def normalize_quota(document, now=None):
    now = time.time() if now is None else now
    if not isinstance(document, dict) or document.get('version') != 1:
        raise ValueError('Unsupported Antigravity quota snapshot')
    stamp = document.get('updatedAtMs')
    if type(stamp) not in (int, float) or not 0 < stamp <= (now + 60) * 1000:
        raise ValueError('Invalid Antigravity quota timestamp')
    pools = {}
    if not isinstance(document.get('groups'), list):
        raise ValueError('Invalid Antigravity quota groups')
    for group in document['groups'][:64]:
        if not isinstance(group, dict) or not isinstance(group.get('displayName'), str) or not isinstance(group.get('buckets'), list):
            continue
        label = group['displayName'].strip()[:100]
        if not label or label in pools:
            continue
        windows = {}
        for bucket in group['buckets'][:24]:
            if not isinstance(bucket, dict):
                continue
            key = bucket.get('window')
            if not isinstance(key, str) or key not in WINDOW_NAMES or key in windows:
                continue
            fraction = bucket.get('remainingFraction')
            valid = type(fraction) in (float, int) and 0 <= fraction <= 1 and math.isfinite(fraction)
            reset = reset_time(bucket.get('resetTime'))
            windows[key] = {'name': WINDOW_NAMES[key], 'remaining': round(fraction * 100, 1) if valid else None,
                            'resetsAt': reset, 'group': label}
        if windows:
            pools[label] = [windows[key] for key in WINDOW_NAMES if key in windows]
    return {'pools': pools, 'observed': stamp / 1000}


def read_quota(path):
    try:
        with Path(path).open('rb') as stream:
            contents = stream.read(131073)
        if len(contents) <= 131072:
            return normalize_quota(json.loads(contents))
    except (OSError, ValueError, RecursionError):
        pass
    return None


def quota_view(snapshot, selected, now=None):
    if snapshot is None:
        return [], '未发现 Antigravity 额度桥接数据'
    now = time.time() if now is None else now
    pools = snapshot['pools']
    if not pools:
        return [], 'Antigravity 账户未提供额度窗口'
    pool = selected if selected in pools else next(iter(pools))
    observed = snapshot['observed']
    when = time.strftime('%H:%M', time.localtime(observed))
    if now - observed > 120:
        return [dict(w, remaining=None, pending=True) for w in pools[pool]], f'{pool} {when} 待刷新'
    return pools[pool], f'{pool} {when}'
