"""Read Antigravity App usage into a separate, durable Bailong index."""
from bisect import bisect_right
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import math
import sqlite3
from statistics import median
import time
import uuid

from token_analytics import UNKNOWN
from token_calendar import usage_snapshot


SOURCE = '本机 Antigravity 会话'
DEDUP_CTE = f'''
    WITH dedup AS (
        SELECT name, idx, identity, stamp, model,
               uncached, cache_read, cache_write, output,
               duration_s, context_used, context_limit, turn_step,
               ROW_NUMBER() OVER (
                   PARTITION BY identity
                   ORDER BY stamp IS NULL,
                            COALESCE(uncached, 0) + COALESCE(cache_read, 0) +
                            COALESCE(cache_write, 0) + COALESCE(output, 0) DESC,
                            model = {UNKNOWN!r},
                            name, idx
               ) AS preference
        FROM requests
    )
'''


def session_id_from_file(name):
    stem = Path(str(name)).stem
    try:
        return 'antigravity:' + str(uuid.UUID(stem))
    except (ValueError, TypeError, AttributeError):
        return 'antigravity:' + stem


def file_name_for_session(session):
    if not isinstance(session, str) or not session.startswith('antigravity:'):
        return None
    raw = session[len('antigravity:'):]
    try:
        return str(uuid.UUID(raw)) + '.db'
    except (ValueError, TypeError, AttributeError):
        return None


def today_breakdown(db, now):
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    row = db.execute(
        DEDUP_CTE + '''
        SELECT COALESCE(SUM(uncached), 0), COALESCE(SUM(cache_read), 0),
               COALESCE(SUM(cache_write), 0), COALESCE(SUM(output), 0),
               COUNT(*)
        FROM dedup
        WHERE preference=1 AND stamp IS NOT NULL AND stamp>=? AND stamp<=?
        ''',
        (midnight, now),
    ).fetchone()
    uncached, cache_read, cache_write, output, count = (int(v or 0) for v in row)
    return {
        'day': datetime.fromtimestamp(now).strftime('%Y-%m-%d'),
        'uncached': uncached,
        'cache_read': cache_read,
        'cache_write': cache_write,
        'output': output,
        'total': uncached + cache_read + cache_write + output,
        'requests': count,
    }


def session_recent_requests(db, now, priority_sessions=(), *, session_limit=12, per_session=5):
    top_names = [
        row[0] for row in db.execute(
            DEDUP_CTE + '''
            SELECT name
            FROM dedup
            WHERE preference=1 AND stamp IS NOT NULL AND stamp<=?
            GROUP BY name
            ORDER BY MAX(stamp) DESC, name ASC
            LIMIT ?
            ''',
            (now, int(session_limit)),
        )
    ]
    priority_names = [name for sid in priority_sessions if (name := file_name_for_session(sid))]
    target_names = list(dict.fromkeys((*priority_names[:24], *top_names)))
    if not target_names:
        return [], {}
    placeholders = ','.join('?' for _ in target_names)
    rows = db.execute(
        DEDUP_CTE + f''',
        ranked AS (
            SELECT name, idx, identity, stamp, model,
                   uncached, cache_read, cache_write, output,
                   duration_s, context_used, context_limit, turn_step,
                   ROW_NUMBER() OVER (PARTITION BY name ORDER BY stamp DESC, idx DESC) AS rn
            FROM dedup
            WHERE preference=1 AND stamp IS NOT NULL AND stamp<=? AND name IN ({placeholders})
        )
        SELECT name, idx, identity, stamp, model, uncached, cache_read, cache_write, output,
               duration_s, context_used, context_limit, turn_step
        FROM ranked
        WHERE rn <= ?
        ORDER BY stamp DESC, idx DESC
        ''',
        (now, *target_names, int(per_session)),
    ).fetchall()
    session_requests = {}
    for name, idx, identity, stamp, model, uncached, cache_read, cache_write, output, duration_s, context_used, context_limit, turn_step in rows:
        sid = session_id_from_file(name)
        parts = (uncached, cache_read, cache_write, output)
        total = sum(parts) if all(type(v) is int and v >= 0 for v in parts) else None
        measured_rate = (round(output / duration_s, 1)
                         if type(output) is int and output > 0 and isinstance(duration_s, (int, float))
                         and not isinstance(duration_s, bool) and math.isfinite(duration_s) and 0.05 <= duration_s <= 3600
                         else None)
        used = context_used if type(context_used) is int and 0 <= context_used <= 2**53 else (
            sum(parts[:3]) if all(type(v) is int and v >= 0 for v in parts[:3]) else None
        )
        context = (
            {'used': used, 'limit': context_limit, 'percent': round(used * 100.0 / context_limit, 1)}
            if type(context_limit) is int and 0 < context_limit <= 2**53 and used is not None
            else None
        )
        session_requests.setdefault(sid, []).append({
            'session': sid,
            'idx': idx,
            'identity': identity,
            'stamp': stamp,
            'model': model or UNKNOWN,
            'uncached': uncached,
            'cache_read': cache_read,
            'cache_write': cache_write,
            'output': output,
            'total': total,
            'duration_s': duration_s,
            'rate': measured_rate,
            'context_used': context_used,
            'context_limit': context_limit,
            'turn_step': turn_step,
            'context': context,
        })
    recent_sessions = list(session_requests)
    return recent_sessions, session_requests


def session_turn_summaries(db, now, target_sessions):
    summaries = {}
    for sid in dict.fromkeys(target_sessions):
        name = file_name_for_session(sid)
        if not name:
            continue
        latest = db.execute(
            DEDUP_CTE + '''
            SELECT idx, turn_step, uncached, cache_read, cache_write, output, duration_s
            FROM dedup
            WHERE preference=1 AND stamp IS NOT NULL AND stamp<=? AND name=?
            ORDER BY stamp DESC, idx DESC
            LIMIT 1
            ''',
            (now, name),
        ).fetchone()
        if latest is None:
            continue
        latest_idx, turn_step, *single_tail = latest
        if type(turn_step) is int and turn_step >= 0:
            rows = db.execute(
                DEDUP_CTE + '''
                SELECT uncached, cache_read, cache_write, output, duration_s
                FROM dedup
                WHERE preference=1 AND stamp IS NOT NULL AND stamp<=? AND name=? AND turn_step=?
                ORDER BY stamp ASC, idx ASC
                ''',
                (now, name, turn_step),
            ).fetchall()
            exact = True
        else:
            rows = [tuple(single_tail)]
            exact = False
        sums = []
        for pos in range(4):
            vals = [row[pos] for row in rows]
            sums.append(sum(vals) if vals and all(type(v) is int and v >= 0 for v in vals) else None)
        u, cr, cw, out = sums
        total = sum(sums) if all(v is not None for v in sums) else None
        dur_vals = [row[4] for row in rows if type(row[3]) is int and row[3] > 0 and isinstance(row[4], (int, float))
                    and not isinstance(row[4], bool) and math.isfinite(row[4]) and 0.05 <= row[4] <= 3600]
        measured_out = sum(row[3] for row in rows if type(row[3]) is int and row[3] > 0 and isinstance(row[4], (int, float))
                           and not isinstance(row[4], bool) and math.isfinite(row[4]) and 0.05 <= row[4] <= 3600)
        total_dur = sum(dur_vals) if dur_vals else None
        summaries[sid] = {
            'turn_step': turn_step,
            'requests': len(rows),
            'uncached': u,
            'cache_read': cr,
            'cache_write': cw,
            'output': out,
            'total': total,
            'duration_s': round(total_dur, 2) if total_dur else None,
            'rate': round(measured_out / total_dur, 1) if total_dur and total_dur > 0 else None,
            'exact': exact,
        }
    return summaries


def historical_rate_snapshots(db, now, titles=None):
    rows = db.execute(
        DEDUP_CTE + '''
        SELECT name, idx, stamp, output, duration_s, turn_step
        FROM dedup
        WHERE preference=1 AND stamp IS NOT NULL AND stamp<=?
          AND output IS NOT NULL AND output>0
          AND duration_s IS NOT NULL AND duration_s>=0.05 AND duration_s<=3600
        ORDER BY stamp ASC, idx ASC
        ''',
        (now,),
    ).fetchall()
    today_dt = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_dt = today_dt - timedelta(days=1)
    day_groups = {}
    hour_groups = {}
    session_buckets = {}
    for name, idx, stamp, output, duration_s, turn_step in rows:
        if type(output) is not int or not isinstance(duration_s, (int, float)) or not math.isfinite(duration_s):
            continue
        local = datetime.fromtimestamp(stamp)
        day_key = local.strftime('%Y-%m-%d')
        hour_key = local.strftime('%Y-%m-%dT%H')
        rate_val = output / duration_s
        for store, key in ((day_groups, day_key), (hour_groups, hour_key)):
            entry = store.setdefault(key, [0, 0.0, []])
            entry[0] += output
            entry[1] += duration_s
            entry[2].append(rate_val)
        sb = session_buckets.setdefault(name, {'latest_stamp': stamp, 'latest_idx': idx, 'turn_step': turn_step, 'rows': []})
        sb['latest_stamp'] = stamp
        sb['latest_idx'] = idx
        sb['turn_step'] = turn_step
        sb['rows'].append((stamp, idx, output, duration_s, turn_step))

    def pack(entry):
        out, dur, vals = entry
        return dict(
            rate=out / dur,
            median_rate=median(vals),
            first_token_ms=None,
            samples=len(vals),
            valid_samples=len(vals),
            output=out,
            duration_ms=dur * 1000.0,
        )

    days = {key: pack(entry) for key, entry in day_groups.items()}
    def hourly_list(day_start, output_only=False):
        result = []
        for h in range(24):
            entry = hour_groups.get(day_start.replace(hour=h).strftime('%Y-%m-%dT%H'))
            if entry is None:
                result.append(None)
            elif output_only:
                out, dur, _ = entry
                result.append(dict(rate=out / dur, sampled_seconds=dur))
            else:
                result.append(pack(entry))
        return result

    report_rates = dict(
        days=days,
        today=hourly_list(today_dt),
        yesterday=hourly_list(yesterday_dt),
        yesterday_mean=days.get(yesterday_dt.strftime('%Y-%m-%d')),
        today_output=hourly_list(today_dt, output_only=True),
        yesterday_output=hourly_list(yesterday_dt, output_only=True),
        pending=False,
        errors=0,
        metric='generation',
        description='输出 Token（含推理）÷ 模型服务端生成耗时',
    )

    details = []
    ordered_sessions = sorted(session_buckets.items(), key=lambda kv: (-kv[1]['latest_stamp'], -kv[1]['latest_idx'], kv[0]))[:12]
    for name, info in ordered_sessions:
        sid = session_id_from_file(name)
        ts = info['turn_step']
        if type(ts) is int and ts >= 0:
            turn_rows = [r for r in info['rows'] if r[4] == ts]
        else:
            turn_rows = [info['rows'][-1]]
        out_sum = sum(r[2] for r in turn_rows)
        dur_sum = sum(r[3] for r in turn_rows)
        if dur_sum <= 0:
            continue
        details.append({
            'session': sid,
            'name': (titles or {}).get(sid) or ('会话 ' + sid[-8:]),
            'rate': round(out_sum / dur_sum, 1),
            'stamp': info['latest_stamp'],
            'samples': len(turn_rows),
            'historical': True,
        })
    card_rate = {
        'rate': details[0]['rate'] if details else None,
        'sessions': len(details),
        'details': details,
        'metric': 'historical_output',
        'historical': True,
        'recent': False,
        'expires': None,
    }
    return card_rate, report_rates


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            break
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('invalid protobuf integer')


def fields(data):
    values = {}
    offset = 0
    while offset < len(data):
        tag, offset = varint(data, offset)
        number, wire = divmod(tag, 8)
        if not number:
            raise ValueError('invalid protobuf field')
        if wire == 0:
            value, offset = varint(data, offset)
        elif wire == 2:
            length, offset = varint(data, offset)
            end = offset + length
            value = data[offset:end]
            offset = end
        elif wire in (1, 5):
            value = None
            offset += 8 if wire == 1 else 4
        else:
            raise ValueError('unsupported protobuf wire type')
        if offset > len(data):
            raise ValueError('truncated protobuf field')
        values.setdefault(number, []).append(value)
    return values


def first(values, key, default=None):
    return values.get(key, [default])[0]


def nested(values, key):
    return fields(first(values, key, b''))


def step_indices(entries):
    for entry in entries:
        if isinstance(entry, int):
            yield entry
        elif isinstance(entry, bytes):
            offset = 0
            while offset < len(entry):
                index, offset = varint(entry, offset)
                yield index


def timestamp(values):
    second = first(values, 1)
    return second if isinstance(second, int) and 946684800 <= second < 4_000_000_000 else None


def usage_record(root, chat, step_at, direct_stamp):
    # Local fields inspected against App 2.34 and agy-capsule's usage reader.
    usage = nested(chat, 4)
    counts = [first(usage, field, 0) for field in (2, 5, 4, 3)]
    if any(not isinstance(count, int) or count > 2**53 for count in counts):
        raise ValueError('invalid usage counters')
    if not any(counts):
        return None
    stamp = direct_stamp
    if stamp is None:
        for index in step_indices(root.get(2, ())):
            if index <= 2**63-1 and (stamp := step_at(index)) is not None:
                break
    response, message = first(usage, 11), first(usage, 7)
    identifier, category = (response, 'response') if response else (message, 'message')
    if identifier and not isinstance(identifier, bytes):
        raise ValueError('invalid request identifier')
    key = f'{first(usage, 6, 0)}:{category}:{identifier[:256].hex()}' if identifier else None
    model = first(chat, 21) or first(chat, 19) or b''
    return key, stamp, model[:320].decode('utf-8', errors='replace').strip()[:80] or UNKNOWN, *counts


def blob_varint(blob, end):
    encoded = bytearray()
    while len(encoded) < 10 and blob.tell() < end:
        encoded.extend(blob.read(1))
        if encoded and encoded[-1] < 128:
            return varint(encoded, 0)[0]
    raise ValueError('truncated protobuf metadata')


def blob_fields(blob, end, wanted, max_field=65536):
    """Keep small telemetry fields and seek past stored prompt/response data."""
    retained = {}
    while blob.tell() < end:
        number, wire = divmod(blob_varint(blob, end), 8)
        if not number:
            raise ValueError('invalid protobuf field')
        if wire == 0:
            value = blob_varint(blob, end)
            if number in wanted:
                retained.setdefault(number, []).append(value)
            continue
        if wire == 2:
            length = blob_varint(blob, end)
        elif wire in (1, 5):
            length = 8 if wire == 1 else 4
        else:
            raise ValueError('unsupported protobuf wire type')
        start = blob.tell()
        if start + length > end:
            raise ValueError('truncated protobuf field')
        rule = wanted.get(number) if wire == 2 else None
        if isinstance(rule, dict):
            value = blob_fields(blob, start + length, rule, max_field)
        elif rule and length <= max_field:
            value = blob.read(length)
        elif rule:
            raise ValueError('oversized usage field')
        else:
            blob.seek(start + length)
            continue
        retained.setdefault(number, []).append(value)
    return retained


def file_signature(path):
    stat = path.stat()
    try:
        wal = path.with_name(path.name + '-wal').stat()
    except FileNotFoundError:
        wal = None
    return (stat.st_size, stat.st_mtime_ns, stat.st_ino,
            wal.st_size if wal else 0, wal.st_mtime_ns if wal and wal.st_size else 0)


def duration_seconds(values):
    if not isinstance(values, dict):
        return None
    sec = first(values, 1, 0)
    nano = first(values, 2, 0)
    if type(sec) is not int or type(nano) is not int or not (0 <= sec <= 86400 and 0 <= nano < 1_000_000_000):
        return None
    total = sec + nano / 1e9
    return total if total > 0 else None


def turn_for_steps(user_steps, indices):
    if not indices:
        return None
    anchor = min(indices)
    pos = bisect_right(user_steps, anchor) - 1
    return user_steps[pos] if pos >= 0 else -1


def telemetry_record(root, chat, user_steps=()):
    try:
        duration_s = duration_seconds(nested(chat, 12)) if 12 in chat else None
    except (ValueError, IndexError):
        duration_s = None
    c9 = first(chat, 9, {})
    try:
        c10 = nested(c9, 10) if isinstance(c9, dict) and 10 in c9 else {}
    except (ValueError, IndexError):
        c10 = {}
    used = first(c10, 1)
    limit = first(c10, 4)
    context_used = used if type(used) is int and 0 <= used <= 2**53 else None
    context_limit = limit if type(limit) is int and 0 < limit <= 2**53 else None
    indices = [i for i in step_indices(root.get(2, ())) if type(i) is int and 0 <= i <= 2**63 - 1]
    return duration_s, context_used, context_limit, turn_for_steps(user_steps, indices)


def read_records(path, signature, cancelled):
    uri = path.resolve().as_uri() + '?mode=ro'
    if not signature[3]:
        uri += '&immutable=1'
    with closing(sqlite3.connect(uri, uri=True, timeout=.2)) as db:
        db.set_progress_handler(lambda: int(cancelled()), 1000)

        def step_at(index):
            row = db.execute('SELECT metadata FROM steps WHERE idx=?', (index,)).fetchone()
            return timestamp(nested(fields(row[0]), 1)) if row and row[0] else None

        # Keep the step metadata and generation rows on the same read snapshot.
        db.execute('BEGIN')
        try:
            user_steps = [row[0] for row in db.execute('SELECT idx FROM steps WHERE step_type=14 ORDER BY idx')]
        except sqlite3.OperationalError:
            user_steps = []
        for rowid, index in db.execute('SELECT rowid,idx FROM gen_metadata ORDER BY idx'):
            if cancelled():
                return
            with db.blobopen('gen_metadata', 'data', rowid, readonly=True) as blob:
                root = blob_fields(blob, len(blob), {1: {4: True, 9: {4: True, 10: True}, 12: True, 19: True, 21: True}, 2: True})
            chat = first(root, 1, {})
            if 9 in chat:
                # Both readers share usage_record's timestamp lookup.
                start = first(chat, 9, {})
                stamp = timestamp(nested(start, 4))
            else:
                stamp = None
            record = usage_record(root, chat, step_at, stamp)
            if record is not None:
                yield index, (*record, *telemetry_record(root, chat, user_steps))


class AntigravityUsage:
    def __init__(self, path, conversations):
        self.path = Path(path)
        self.conversations = Path(conversations)

    def connect(self):
        from quota_history import migrate as migrate_quota
        db = sqlite3.connect(self.path, timeout=.4)
        existing = db.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='usage'").fetchone()
        if existing and 'COALESCE' not in (existing[0] or ''):
            db.execute('DROP VIEW usage')
        db.executescript(f'''
            CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, signature TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                name TEXT, idx INTEGER, identity TEXT, stamp REAL, model TEXT,
                uncached INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER,
                duration_s REAL, context_used INTEGER, context_limit INTEGER, turn_step INTEGER,
                PRIMARY KEY(name, idx));
            CREATE INDEX IF NOT EXISTS requests_identity ON requests(identity);
            CREATE VIEW IF NOT EXISTS usage AS
                SELECT identity AS fingerprint, stamp, model, name AS session,
                       COALESCE(uncached,0)+COALESCE(cache_read,0)+COALESCE(cache_write,0)+COALESCE(output,0) AS tokens,
                       COALESCE(uncached,0)+COALESCE(cache_read,0)+COALESCE(cache_write,0) AS input_tokens,
                       COALESCE(output,0) AS output_tokens, 1 AS known
                FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY identity ORDER BY stamp IS NULL,
                      COALESCE(uncached,0)+COALESCE(cache_read,0)+COALESCE(cache_write,0)+COALESCE(output,0) DESC,
                      model = {UNKNOWN!r}, name, idx) AS preference
                      FROM requests)
                WHERE preference=1 AND stamp IS NOT NULL
                  AND (uncached IS NOT NULL OR cache_read IS NOT NULL OR cache_write IS NOT NULL OR output IS NOT NULL);
        ''')
        cols = {row[1] for row in db.execute('PRAGMA table_info(requests)')}
        missing = [(col, col_type) for col, col_type in (
            ('duration_s', 'REAL'), ('context_used', 'INTEGER'), ('context_limit', 'INTEGER'), ('turn_step', 'INTEGER')
        ) if col not in cols]
        with db:
            for col, col_type in missing:
                db.execute(f'ALTER TABLE requests ADD COLUMN {col} {col_type}')
            if missing:
                db.execute('DELETE FROM files')
            migrate_quota(db, multi_plan=True)
        return db

    def replace_file(self, db, path, signature, cancelled):
        with db:
            db.execute('DELETE FROM requests WHERE name=?', (path.name,))
            def rows():
                for index, (key, *record) in read_records(path, signature, cancelled):
                    yield path.name, index, key or f'file:{path.name}:{index}', *record
            db.executemany('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', rows())
            if cancelled() or file_signature(path) != signature:
                raise OSError('Antigravity 会话在读取期间发生变化')
            db.execute('INSERT OR REPLACE INTO files VALUES (?,?)', (path.name, repr(signature)))

    def refresh(self, cancelled=lambda: False, budget=.7, now=None, priority_sessions=(),
                quota=None, quota_group='', profile=None, calendar_range=None):
        from quota_history import anomaly, store as store_quota, summarize_cycles
        now = time.time() if now is None else now
        deadline = time.monotonic() + budget
        with closing(self.connect()) as db:
            if isinstance(quota, dict) and now - quota.get('observed', 0) <= 120:
                with db:
                    for group, windows in (quota.get('pools') or {}).items():
                        for w in windows:
                            if w.get('name') == '一周' and w.get('remaining') is not None:
                                store_quota(db, quota['observed'], w.get('resetsAt'), w['remaining'], source='live', plan=group, multi_plan=True)
            known = dict(db.execute('SELECT name,signature FROM files'))
            paths, errors = [], 0
            try:
                paths = sorted(self.conversations.glob('*.db'))
            except OSError:
                errors += 1
            found = {path.name for path in paths}
            errors += len(known.keys() - found)
            pending = False
            readable = 0
            for path in paths:
                if cancelled():
                    return None
                try:
                    signature = file_signature(path)
                    if known.get(path.name) != repr(signature):
                        if time.monotonic() >= deadline:
                            pending = True
                            continue
                        self.replace_file(db, path, signature, cancelled)
                    readable += 1
                except (OSError, sqlite3.Error, ValueError, TypeError, IndexError, OverflowError):
                    errors += 1
            if cancelled():
                return None
            undated = db.execute('SELECT 1 FROM requests WHERE stamp IS NULL LIMIT 1').fetchone() is not None
            result = usage_snapshot(db, now, errors or pending or undated, source=SOURCE, count_messages=False)
            unavailable = not readable and not known and not pending
            breakdown = None if unavailable else today_breakdown(db, now)
            recent_sessions, session_requests = ([], {}) if unavailable else session_recent_requests(db, now, priority_sessions)
            session_turns = {} if unavailable else session_turn_summaries(db, now, recent_sessions)
            card_rate, report_rates = historical_rate_snapshots(db, now)
            report_rates['pending'] = pending
            report_rates['errors'] = errors
            pools = (quota or {}).get('pools') or {}
            active_pool = quota_group if quota_group in pools else next(iter(pools), '')
            if not active_pool:
                row = db.execute("SELECT plan FROM quota_observations WHERE plan<>'' ORDER BY stamp DESC LIMIT 1").fetchone()
                active_pool = row[0] if row else ''
            cycles = summarize_cycles(db, now, complete=not (errors or pending or undated), pending=pending, plan=active_pool or None)
            qh = {
                'cycles': cycles,
                'anomaly': anomaly(cycles, now) if not (errors or pending or undated) else None,
                'pending': pending,
                'partial': bool(errors or pending or undated),
                'plan': active_pool,
            }
            if unavailable:
                projects = None
            else:
                from antigravity_projects import project_snapshot
                projects = project_snapshot(db, profile, now, calendar_range=calendar_range)
            result['rates'] = report_rates
            result['report']['rates'] = report_rates
            result['report']['supports_rates'] = True
            result.update(provider='antigravity', pending=pending, readable_files=readable,
                          unavailable=unavailable, today_breakdown=breakdown,
                          recent_sessions=recent_sessions, session_requests=session_requests,
                          session_turns=session_turns, historical_rate=card_rate,
                          quota_history=qh, projects=projects)
            return result
