"""Antigravity project cost mapping, verified baseline rates, and turn breakdown."""
from contextlib import closing
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import sqlite3
from urllib.parse import unquote, urlparse

from antigravity_usage import DEDUP_CTE, session_id_from_file
from cost_preferences import load as load_pricing_settings
from project_cost import add_chart_cost, project_path
from report_dates import MONTH, WEEK, date_bounds, range_key
from token_analytics import UNKNOWN


# Verified public standard USD rates per 1M tokens: (input, cached_read, output).
# Internal or preview model codenames without published pricing remain unknown.
ANTIGRAVITY_RATES = {
    'claude-opus-4-6': (5.0, 0.5, 25.0),
    'claude-sonnet-4-6': (3.0, 0.3, 15.0),
    'claude-sonnet-4-5': (3.0, 0.3, 15.0),
    'gemini-2.5-pro': (1.25, 0.125, 10.0),
    'gemini-2.5-flash': (0.30, 0.03, 2.50),
    'gemini-3-pro': (2.0, 0.2, 12.0),
    'gemini-3-flash': (0.50, 0.05, 3.0),
}


def _valid_rate(values):
    if isinstance(values, (list, tuple)) and len(values) == 3 and all(v is not None for v in values):
        return (float(values[0]), float(values[1]), float(values[2]))
    return None


def _canonical_slugs(model):
    base = re.sub(r'\s*\([^)]*\)', '', str(model or '').lower()).strip()
    dot_slug = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', re.sub(r'\s+', '-', base))
    hyphen_slug = re.sub(r'(\d)\.(\d)', r'\1-\2', dot_slug)
    return dot_slug, hyphen_slug


def lookup_rate(model, manual_prices=None):
    name = str(model or '').strip()
    if not name or name == UNKNOWN:
        return None
    manual = manual_prices or {}
    if name in manual:
        return _valid_rate(manual[name])
    lowered = {k.lower(): v for k, v in manual.items()}
    if name.lower() in lowered:
        return _valid_rate(lowered[name.lower()])
    for slug in _canonical_slugs(name):
        if slug in lowered:
            return _valid_rate(lowered[slug])
    for slug in _canonical_slugs(name):
        if slug in ANTIGRAVITY_RATES:
            return ANTIGRAVITY_RATES[slug]
    return None


def workspace_from_uris(raw):
    if not isinstance(raw, str) or not raw.strip():
        return '', ''
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return '', ''
    if not isinstance(items, list):
        return '', ''
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        parsed = urlparse(item.strip())
        if parsed.scheme.lower() != 'file':
            continue
        raw_path = unquote(parsed.path or '')
        if parsed.netloc and parsed.netloc.lower() not in ('', 'localhost'):
            raw_path = '//' + parsed.netloc + raw_path
        elif re.match(r'^/[a-zA-Z]:/', raw_path):
            raw_path = raw_path[1:]
        norm = project_path(raw_path)
        if norm:
            return norm, raw_path.rstrip('/')
    return '', ''


def conversation_metadata(profile):
    if profile is None:
        return {}
    path = Path(profile) / '.gemini' / 'antigravity' / 'conversation_summaries.db'
    if not path.is_file():
        return {}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.15)) as db:
            rows = db.execute('SELECT conversation_id, title, workspace_uris FROM conversation_summaries').fetchall()
        result = {}
        for cid, title, uris in rows:
            if not isinstance(cid, str) or not cid:
                continue
            sid = 'antigravity:' + cid.lower()
            norm, display_path = workspace_from_uris(uris)
            clean_title = ' '.join(title.split())[:160] if isinstance(title, str) and title.strip() else ''
            result[sid] = {'title': clean_title, 'path': norm, 'display_path': display_path}
        return result
    except (OSError, sqlite3.Error, ValueError):
        return {}


def _request_cost_parts(uncached, cache_read, cache_write, output, rates):
    return (
        (uncached + cache_write) * rates[0] / 1e6,
        cache_read * rates[1] / 1e6,
        output * rates[2] / 1e6,
    )


def project_snapshot(db, profile, now, calendar_range=None):
    settings = load_pricing_settings()
    manual = settings.get('prices') or {}
    meta = conversation_metadata(profile)
    rows = db.execute(
        DEDUP_CTE + '''
        SELECT name, idx, stamp, model, uncached, cache_read, cache_write, output, turn_step
        FROM dedup
        WHERE preference=1 AND stamp IS NOT NULL AND stamp<=?
        ORDER BY stamp ASC, idx ASC
        ''',
        (now,),
    ).fetchall()

    current = datetime.fromtimestamp(now)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    today = current.date().isoformat()
    yesterday = (midnight - timedelta(days=1)).strftime('%Y-%m-%d')
    cutoff = date.fromisoformat(yesterday)
    ranges = [
        ('all', '', ''),
        ('30d', (midnight - timedelta(days=29)).date().isoformat(), ''),
        ('7d', (midnight - timedelta(days=6)).date().isoformat(), ''),
        ('yesterday', yesterday, yesterday),
    ]
    calendar_ranges = [date_bounds(period, cutoff) for period in (MONTH, WEEK)]
    if calendar_range:
        calendar_ranges.append(calendar_range)
    for first_day, last_day in dict.fromkeys(calendar_ranges):
        ranges.append((range_key((first_day, last_day)), first_day.isoformat(), min(last_day.isoformat(), yesterday)))

    unassigned = 'unassigned:'
    chart_days = {}
    chart_hours = {}
    events = []
    calendar_days = set()
    price_table = dict(ANTIGRAVITY_RATES)
    for k, v in manual.items():
        valid = _valid_rate(v)
        if valid is not None:
            price_table[k] = valid
        else:
            price_table.pop(k, None)

    for name, idx, stamp, model, u, cr, cw, out, turn_step in rows:
        sid = session_id_from_file(name)
        info = meta.get(sid, {})
        proj = info.get('path') or unassigned
        disp_path = info.get('display_path') or ''
        sess_title = info.get('title') or ('会话 ' + sid[-8:])
        parts_valid = all(type(v) is int and v >= 0 for v in (u, cr, cw, out))
        tokens = sum((u, cr, cw, out)) if parts_valid else sum(v for v in (u, cr, cw, out) if type(v) is int and v >= 0)
        rates = lookup_rate(model, manual) if parts_valid else None
        recorded = datetime.fromtimestamp(stamp)
        day = recorded.date().isoformat()
        calendar_days.add(day)
        events.append((
            proj, disp_path, sid, sess_title, idx, stamp, day, recorded.hour,
            model or '未识别模型', tokens, u, cr, cw, out, turn_step, parts_valid, rates,
        ))
        if parts_valid and rates is not None:
            cost_total = sum(_request_cost_parts(u, cr, cw, out, rates))
            add_chart_cost(chart_days.setdefault(day, {}), model or '未识别模型', tokens, cost_total)
            if day == today:
                add_chart_cost(chart_hours.setdefault(str(recorded.hour), {}), model or '未识别模型', tokens, cost_total)

    result = {}
    for period, start, end in ranges:
        projects = {}
        for proj, disp_path, sid, sess_title, idx, stamp, day, _hour, model, tokens, u, cr, cw, out, turn_step, known, rates in events:
            if day < start or (end and day > end):
                continue
            pname = '未归属项目' if proj == unassigned else (PureWindowsPath(disp_path).name or disp_path or '未识别项目')
            p = projects.setdefault(proj, {
                'id': proj, 'name': pname, 'path': '' if proj == unassigned else disp_path,
                'tokens': 0, 'cost': 0.0, 'parts': [0.0, 0.0, 0.0],
                'pending_tokens': 0, 'pending_reasons': {}, 'sessions': set(),
                'models': set(), 'archived': False, 'model_usage': {}, 'session_usage': {},
            })
            p['tokens'] += tokens
            p['sessions'].add(sid)
            p['models'].add(model)

            m = p['model_usage'].setdefault(model, {
                'model': model, 'tokens': 0, 'cost': 0.0, 'pending_tokens': 0, 'pending_reasons': {},
            })
            m['tokens'] += tokens

            s = p['session_usage'].setdefault(sid, {
                'id': sid, 'name': sess_title, 'tokens': 0, 'cost': 0.0, 'pending_tokens': 0,
                'detail_revision': period, '_turns': {},
            })
            s['tokens'] += tokens

            has_turn = type(turn_step) is int and turn_step >= 0
            turn_key = ('turn', turn_step) if has_turn else ('req', idx)
            t = s['_turns'].setdefault(turn_key, {
                'id': str(turn_step) if has_turn else f'req-{idx}',
                'stamp': stamp, 'models': set(), 'tokens': 0,
                'input': 0, 'cached': 0, 'output': 0,
                'cost': 0.0, 'pending_tokens': 0, 'unknown': not has_turn, 'known': True,
            })
            t['stamp'] = min(t['stamp'], stamp)
            t['models'].add(model)
            t['tokens'] += tokens
            t['known'] = t['known'] and known
            if known:
                t['input'] += u + cr + cw
                t['cached'] += cr
                t['output'] += out

            if not known or rates is None:
                reason = 'incomplete_counts' if not known else 'unknown_model'
                for target in (p, m):
                    target['pending_tokens'] += tokens
                    target['pending_reasons'][reason] = target['pending_reasons'].get(reason, 0) + tokens
                s['pending_tokens'] += tokens
                t['pending_tokens'] += tokens
                continue

            parts = _request_cost_parts(u, cr, cw, out, rates)
            cost_val = sum(parts)
            p['parts'] = [a + b for a, b in zip(p['parts'], parts)]
            p['cost'] += cost_val
            m['cost'] += cost_val
            s['cost'] += cost_val
            t['cost'] += cost_val

        for p in projects.values():
            for row in [p, *p['model_usage'].values()]:
                row['priced_tokens'] = row['tokens'] - row['pending_tokens']
                row['price_coverage'] = row['priced_tokens'] / row['tokens'] if row['tokens'] else None
            p['session_ids'] = sorted(p['sessions'])
            p['sessions'] = len(p['sessions'])
            p['models'] = sorted(p['models'])
            p['cost'] = round(p['cost'], 6)
            p['model_usage'] = sorted(p['model_usage'].values(), key=lambda v: (-v['cost'], -v['tokens']))
            sessions_list = []
            for s in p['session_usage'].values():
                turns = sorted(s.pop('_turns').values(), key=lambda t: -t['stamp'])
                for t in turns:
                    t['models'] = sorted(t['models'])
                s['turns'] = turns
                sessions_list.append(s)
            p['session_usage'] = sorted(sessions_list, key=lambda v: (-v['cost'], -v['tokens']))
        result[period] = sorted(projects.values(), key=lambda v: (-v['cost'], -v['tokens']))

    price_revision = 'agy:' + hashlib.sha256(
        json.dumps(manual, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()[:16]
    return {
        'provider': 'antigravity',
        'ranges': result,
        'chart_cost': {
            'days': chart_days,
            'hours': chart_hours,
            'day': today,
            'currency': settings['currency'],
            'fx': settings['fx'],
        },
        'yesterday': yesterday,
        'calendar_days': sorted(calendar_days),
        'pending': False,
        'initial_pending': False,
        'syncing': False,
        'errors': 0,
        'limited': False,
        'price_revision': price_revision,
        'price_table': price_table,
        'checked': now,
    }
