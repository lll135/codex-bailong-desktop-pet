from contextlib import closing
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFontMetricsF, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QToolButton, QWidget

from antigravity_projects import conversation_metadata, lookup_rate, project_snapshot, workspace_from_uris
from antigravity_quota import normalize_quota, quota_view
from antigravity_usage import AntigravityUsage, blob_fields, session_recent_requests, today_breakdown, usage_record
from compact_bubble import CompactBubble
import cost_preferences
from daily_report import DailyReport
from detail_sections import RecentRequestCard, explanation_page
from live_rate_ui import LiveRateCard
from project_cost_ui import TurnRow
from quota_history import visible_quota_change
from quota_summary import QuotaSummary, TodayTokens
from quota_summary_data import today_tokens
from token_charts import TokenPlot

APP = QApplication.instance() or QApplication([])


def wait_for(condition, timeout=2):
    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        QTest.qWait(20)
    return bool(condition())


def drain():
    APP.processEvents()
    APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()


def integer(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, int):
        return integer(number * 8) + integer(value)
    return integer(number * 8 + 2) + integer(len(value)) + value


class UsageFieldsTests(unittest.TestCase):
    def test_skips_large_unrelated_field_and_uses_step_timestamp(self):
        usage = field(2, 20) + field(5, 100) + field(4, 5) + field(3, 10) + field(11, b'request')
        chat = field(3, b'x' * 2_000_000) + field(4, usage) + field(21, b'Model')
        message = field(1, chat) + field(2, b'\x04\x05')
        root = blob_fields(io.BytesIO(message), len(message), {1: {4: True, 21: True}, 2: True})
        self.assertEqual(set(root[1][0]), {4, 21})
        result = usage_record(root, root[1][0], lambda step: 1_790_000_000 if step == 5 else None, None)
        self.assertEqual(result[1:], (1_790_000_000, 'Model', 20, 100, 5, 10))
        self.assertTrue(result[0].startswith('0:response:'))


class QuotaSnapshotTests(unittest.TestCase):
    def test_zero_remaining_missing_window_and_stale_values(self):
        document = {'version': 1, 'updatedAtMs': 1_790_000_000_000, 'groups': [
            {'displayName': 'Weekly group', 'buckets': [{'window': 'weekly', 'remainingFraction': 0}]},
            {'displayName': 'Two windows', 'buckets': [
                {'window': 'weekly', 'remainingFraction': .55},
                {'window': '5h', 'remainingFraction': 'unknown'}]},
        ]}
        quota = normalize_quota(document, 1_790_000_010)
        windows, _ = quota_view(quota, 'Weekly group', 1_790_000_010)
        self.assertEqual([(w['name'], w['remaining']) for w in windows], [('一周', 0)])
        windows, _ = quota_view(quota, 'Two windows', 1_790_000_010)
        self.assertEqual([w['remaining'] for w in windows], [None, 55])
        windows, caption = quota_view(quota, 'Two windows', 1_790_000_121)
        self.assertTrue(all(w['pending'] and w['remaining'] is None for w in windows))
        self.assertIn('待刷新', caption)


class UsageBreakdownAndRecentRequestsTests(unittest.TestCase):
    def test_today_breakdown_and_recent_requests_dedup_and_filter_by_day(self):
        now = datetime(2026, 11, 23, 15, 30, 0).timestamp()
        today_early = datetime(2026, 11, 23, 9, 0, 0).timestamp()
        today_late = datetime(2026, 11, 23, 14, 15, 0).timestamp()
        yesterday = (datetime(2026, 11, 23, 10, 0, 0) - timedelta(days=1)).timestamp()
        sid_a = '11111111-1111-1111-1111-111111111111'
        sid_b = '22222222-2222-2222-2222-222222222222'
        sid_old = '33333333-3333-3333-3333-333333333333'

        db_path = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / f'agy-usage-{uuid.uuid4().hex}.sqlite3'
        db_path.unlink(missing_ok=True)
        self.addCleanup(db_path.unlink, missing_ok=True)
        conv_dir = db_path.parent / f'agy-convs-{uuid.uuid4().hex}'
        usage = AntigravityUsage(db_path, conv_dir)
        with closing(usage.connect()) as db:
            db.execute('INSERT INTO files VALUES (?,?)', (sid_a + '.db', 'sig'))
            rows = [
                # Duplicate identity across two files: larger total (450 vs 40) wins.
                (sid_a + '.db', 1, 'id-dup', today_early, 'gemini-pro', 100, 200, 50, 100),
                (sid_b + '.db', 1, 'id-dup', today_early, 'gemini-pro', 10, 10, 10, 10),
                # Second request today in session A.
                (sid_a + '.db', 2, 'id-a2', today_late, 'gemini-flash', 200, 300, 20, 80),
                # Yesterday's request in old session.
                (sid_old + '.db', 1, 'id-old', yesterday, 'claude-sonnet', 500, 100, 0, 250),
                # Missing usage row (NULL counts) in session B.
                (sid_b + '.db', 2, 'id-b-null', today_late - 60, 'gemini-pro', None, 50, None, 20),
            ]
            db.executemany('INSERT INTO requests (name,idx,identity,stamp,model,uncached,cache_read,cache_write,output) VALUES (?,?,?,?,?,?,?,?,?)', rows)
            db.commit()

            breakdown = today_breakdown(db, now)
            self.assertEqual(breakdown['uncached'], 300)
            self.assertEqual(breakdown['cache_read'], 550)
            self.assertEqual(breakdown['cache_write'], 70)
            self.assertEqual(breakdown['output'], 200)
            self.assertEqual(breakdown['total'], 1120)
            self.assertEqual(breakdown['requests'], 3)

            recent_sessions, session_requests = session_recent_requests(
                db, now, priority_sessions=('antigravity:' + sid_old,), session_limit=2,
            )
            self.assertIn('antigravity:' + sid_a, recent_sessions)
            self.assertIn('antigravity:' + sid_old, recent_sessions)
            latest_a = session_requests['antigravity:' + sid_a][0]
            self.assertEqual((latest_a['model'], latest_a['uncached'], latest_a['cache_read'], latest_a['cache_write'], latest_a['output']),
                             ('gemini-flash', 200, 300, 20, 80))
            latest_b = session_requests['antigravity:' + sid_b][0]
            self.assertIsNone(latest_b['uncached'])
            self.assertEqual(latest_b['cache_read'], 50)
            self.assertIsNone(latest_b['total'])

        with patch('pathlib.Path.glob', return_value=[]):
            snap = usage.refresh(now=now, priority_sessions=('antigravity:' + sid_a,))
        self.assertEqual(snap['provider'], 'antigravity')
        self.assertEqual(snap['today_breakdown']['total'], 1120)
        self.assertIn('antigravity:' + sid_a, snap['session_requests'])

    def test_today_tokens_extracts_antigravity_breakdown_or_marks_missing(self):
        now = datetime(2026, 11, 23, 12, 0).timestamp()
        snap = {
            'provider': 'antigravity',
            'month': '2026-11',
            'days': {'2026-11-23': 1900},
            'today_breakdown': {'day': '2026-11-23', 'uncached': 1000, 'cache_read': 500, 'cache_write': 100, 'output': 300},
        }
        info = today_tokens(snap, now)
        self.assertEqual(info['provider'], 'antigravity')
        self.assertIn('本机 Antigravity 会话', info['description'])
        self.assertEqual(info['breakdown'], {'uncached': 1000, 'cache_read': 500, 'cache_write': 100, 'output': 300})

        partial_missing = dict(snap, today_breakdown={'day': '2026-11-23', 'uncached': 100, 'cache_read': None, 'cache_write': 'bad', 'output': 50})
        self.assertEqual(today_tokens(partial_missing, now)['breakdown'], {'uncached': 100, 'cache_read': None, 'cache_write': None, 'output': 50})
        self.assertIsNone(today_tokens(dict(snap, today_breakdown={'day': '2026-11-22', 'uncached': 1}), now)['breakdown'])


class AntigravityBubbleDetailsTests(unittest.TestCase):
    def _make_bubble(self):
        b = CompactBubble()
        b.antigravity_enabled = True
        b.card_source = 'antigravity'
        b.move(100, 400)
        b.show()
        b.size_transition.complete()
        drain()
        self.addCleanup(self._cleanup_bubble, b)
        return b

    def _cleanup_bubble(self, b):
        b.inline_explanations.dismiss(True)
        b.close()
        b.deleteLater()
        drain()

    def _snapshot(self, *, uncached=1200, cache_read=3400, cache_write=500, output=800, with_breakdown=True, requests=None, titles=None):
        now_dt = datetime.now()
        today = now_dt.strftime('%Y-%m-%d')
        total = (uncached or 0) + (cache_read or 0) + (cache_write or 0) + (output or 0)
        snap = {
            'provider': 'antigravity',
            'month': now_dt.strftime('%Y-%m'),
            'today': today,
            'days': {today: total},
            'hours': [0] * 24,
            'hour_models': [{} for _ in range(24)],
            'history_days': {today: total},
            'total': total,
            'all_total': total,
            'checked': time.time(),
            'report': {'day': today, 'daily_models': [], 'hours': [0] * 24, 'hour_models': [{} for _ in range(24)], 'all_total': total, 'supports_rates': False},
            'recent_sessions': list(requests or ()),
            'session_requests': dict(requests or {}),
            'session_titles': dict(titles or {}),
        }
        if with_breakdown:
            snap['today_breakdown'] = {
                'day': today,
                'uncached': uncached,
                'cache_read': cache_read,
                'cache_write': cache_write,
                'output': output,
            }
        return snap

    def test_today_tokens_hover_shows_four_part_breakdown_and_updates_in_place(self):
        b = self._make_bubble()
        b.antigravity_snapshot = self._snapshot()
        b.redraw()
        drain()
        daily = b.today_tokens_card
        self.assertEqual(daily.property('detailHintTitle'), '今日 Token')
        self.assertEqual(daily.property('detailHintRows'), [('未缓存输入', '1.2K'), ('缓存读取', '3.4K'), ('缓存写入', '500'), ('输出', '800')])

        QTest.mouseMove(daily, daily.rect().center())
        self.assertTrue(wait_for(b.inline_explanations.popup.isVisible))
        popup = b.inline_explanations.popup
        self.assertEqual(popup.detail_rows, [('未缓存输入', '1.2K'), ('缓存读取', '3.4K'), ('缓存写入', '500'), ('输出', '800')])
        self.assertFalse(popup.grab().isNull())

        b.antigravity_snapshot = self._snapshot(uncached=2500, cache_read=6000, cache_write=100, output=400)
        b.refresh_token_snapshot()
        drain()
        self.assertTrue(popup.isVisible())
        self.assertEqual(popup.detail_rows, [('未缓存输入', '2.5K'), ('缓存读取', '6.0K'), ('缓存写入', '100'), ('输出', '400')])

        b.antigravity_snapshot = self._snapshot(with_breakdown=False)
        b.refresh_token_snapshot()
        drain()
        self.assertEqual(popup.detail_rows, [('未缓存输入', '未知'), ('缓存读取', '未知'), ('缓存写入', '未知'), ('输出', '未知')])

        b.card_source = 'codex'
        b.redraw()
        drain()
        self.assertIsNone(b.today_tokens_card.property('detailHintRows'))
        self.assertFalse(popup.isVisible())

    def test_expanded_recent_request_card_multi_session_missing_and_scroll_preserve(self):
        b = self._make_bubble()
        sid1 = 'antigravity:11111111-1111-1111-1111-111111111111'
        sid2 = 'antigravity:22222222-2222-2222-2222-222222222222'
        now = time.time()
        b.roster.observe(dict(session=sid1, title='修复额度显示', state='working', display='正在编辑文件'))
        b.antigravity_snapshot = self._snapshot(
            requests={sid1: [{'session': sid1, 'stamp': now - 60, 'model': 'gemini-3-pro', 'uncached': 1500, 'cache_read': 2500, 'cache_write': 300, 'output': 700, 'total': 5000}]},
            titles={sid1: '修复额度显示'},
        )
        b.toggle_details()
        b.size_transition.complete()
        b.details_tabs.setCurrentIndex(1)
        b.size_transition.complete()
        drain()

        card = b.details_tabs.findChild(RecentRequestCard, 'antigravity_recent_request')
        self.assertIsNotNone(card)
        self.assertEqual(card.findChild(QLabel, 'antigravity_recent_session_name').text(), '修复额度显示')
        self.assertEqual(card.findChild(QLabel, 'recent_uncached').text(), '1.5K')
        self.assertEqual(card.findChild(QLabel, 'recent_cache_read').text(), '2.5K')
        self.assertEqual(card.findChild(QLabel, 'recent_cache_write').text(), '300')
        self.assertEqual(card.findChild(QLabel, 'recent_output').text(), '700')
        self.assertNotEqual(card.findChild(QLabel, 'recent_request_time').text(), '暂无记录')

        # Multiple active sessions: second active session missing usage shows 未知.
        b.roster.observe(dict(session=sid2, title='后台分析任务', state='thinking', display='正在思考'))
        b.refresh_status()
        b.size_transition.complete()
        drain()
        combo = b.details_tabs.findChild(QWidget, 'antigravity_recent_session')
        self.assertIsNotNone(combo)
        self.assertEqual(combo.count(), 2)
        combo.setCurrentIndex(combo.findData(sid2))
        b.size_transition.complete()
        drain()
        card = b.details_tabs.findChild(RecentRequestCard, 'antigravity_recent_request')
        for key in ('uncached', 'cache_read', 'cache_write', 'output'):
            self.assertEqual(card.findChild(QLabel, 'recent_' + key).text(), '未知')
        self.assertEqual(card.findChild(QLabel, 'recent_request_time').text(), '暂无记录')

        # Background refresh preserves scroll position in Token明细.
        b.expanded_height = lambda: 460
        b.expanded_height_limit = lambda: 430
        b.redraw()
        b.size_transition.complete()
        drain()
        area = b.details_tabs.areas['tokens']
        bar = area.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(min(28, bar.maximum()))
        saved_scroll = bar.value()
        self.assertGreater(saved_scroll, 0)

        b.antigravity_snapshot = self._snapshot(
            requests={
                sid1: [{'session': sid1, 'stamp': now - 60, 'model': 'gemini-3-pro', 'uncached': 1500, 'cache_read': 2500, 'cache_write': 300, 'output': 700, 'total': 5000}],
                sid2: [{'session': sid2, 'stamp': now, 'model': 'gemini-3-flash', 'uncached': 4200, 'cache_read': 8000, 'cache_write': 100, 'output': 900, 'total': 13200}],
            },
            titles={sid1: '修复额度显示', sid2: '后台分析任务'},
        )
        self.assertFalse(b.refresh_token_snapshot())
        b.redraw()
        drain()
        card = b.details_tabs.findChild(RecentRequestCard, 'antigravity_recent_request')
        self.assertEqual(card.findChild(QLabel, 'recent_uncached').text(), '4.2K')
        self.assertEqual(b.details_tabs.areas['tokens'].verticalScrollBar().value(), saved_scroll)

        # Codex tab 1 remains unaffected.
        b.card_source = 'codex'
        b.redraw()
        drain()
        self.assertIsNone(b.details_tabs.findChild(RecentRequestCard, 'antigravity_recent_request'))

    def test_antigravity_ui_polish(self):
        # 1. Normal card displays model group tag without colliding with 100% or countdown icon.
        now = 2_000_000_000.
        quota_doc = {'version': 1, 'updatedAtMs': int(now * 1000), 'groups': [{
            'displayName': 'Gemini 3 Pro',
            'buckets': [
                {'window': '5h', 'remainingFraction': 1.0, 'resetTime': '2033-05-18T08:29:20Z'},
                {'window': 'weekly', 'remainingFraction': .708, 'resetTime': '2033-05-23T10:33:20Z'},
            ],
        }]}
        windows, _ = quota_view(normalize_quota(quota_doc, now), 'Gemini 3 Pro', now)
        card = QuotaSummary(windows, clock=lambda: now)
        card.setFixedWidth(116)
        card._set_hover_at(None)
        card._static_key = None
        self.addCleanup(card.deleteLater)
        texts = {}
        icons = []

        class Recorder(QPainter):
            def drawText(self, *args):
                rect, _, text = args
                metrics = QFontMetricsF(self.font(), self.device())
                texts[text] = (rect.top(), rect.left() + metrics.tightBoundingRect(text).right())
                return super().drawText(*args)

            def drawArc(self, rect, start, span):
                icons.append(rect.left() - self.pen().widthF() / 2)
                return super().drawArc(rect, start, span)

        with patch('quota_summary.QPainter', Recorder):
            card.grab()
        self.assertTrue(any('Gemini' in key for key in texts))
        self.assertIn('100%', texts)
        self.assertTrue(icons)
        self.assertGreaterEqual(icons[0] - texts['100%'][1], 4)

        # 2. LiveRateCard disabled state blocks popup and renders dimmed.
        rate_card = LiveRateCard({'rate': 12.3})
        rate_card.resize(116, 22)
        self.addCleanup(rate_card.deleteLater)
        enabled_img = rate_card.grab().toImage()
        rate_card.setEnabled(False)
        rate_card.set_data(None)
        rate_card._show_hint()
        self.assertFalse(rate_card.popup.isVisible())
        disabled_img = rate_card.grab().toImage()
        self.assertNotEqual(enabled_img, disabled_img)

        # 3. Chat page external open button routes dynamically and header count covers Antigravity.
        b = self._make_bubble()
        agy_clicks = []
        codex_clicks = []
        b.antigravityRequested.connect(lambda: agy_clicks.append(True))
        b.codexRequested.connect(lambda: codex_clicks.append(True))

        class FakeChat(QWidget):
            def __init__(self, service):
                super().__init__()
                self.session = {'service': service}
                self.root = Path('.')

        chat_agy = FakeChat('antigravity')
        b.show_page('chat', chat_agy)
        b.size_transition.complete()
        drain()
        self.assertEqual(b.editor_codex_button.toolTip(), '打开 Antigravity')
        b.editor_codex_button.click()
        self.assertEqual(len(agy_clicks), 1)
        chat_agy.session['service'] = 'codex'
        b._redraw_editor()
        self.assertEqual(b.editor_codex_button.toolTip(), '打开 Codex')
        b.editor_codex_button.click()
        self.assertEqual(len(codex_clicks), 1)

        # 4. Header tooltip shows multi-session count when Antigravity is selected.
        b.return_to_status()
        b.size_transition.complete()
        b.roster.observe(dict(session='antigravity:a1', title='任务一', state='working'))
        b.roster.observe(dict(session='antigravity:a2', title='任务二', state='thinking'))
        b._update_header()
        self.assertIn('2 个会话正在进行', b.findChild(QToolButton, 'open_codex').toolTip())

        # 5. TokenPlot and explanation_page reflect Antigravity copy without middle dots.
        plot = TokenPlot(self._snapshot(), 'day')
        self.addCleanup(plot.deleteLater)
        self.assertIn('未缓存输入', plot.accessibleDescription())
        self.assertIn('Antigravity', plot.detail({'label': '10时', 'value': 10, 'future': False, 'current': False}))
        self.assertNotIn('·', plot.detail({'label': '10时', 'value': 10, 'future': False, 'current': False}))
        help_agy = explanation_page(b)
        self.addCleanup(help_agy.deleteLater)
        self.assertTrue(any('多来源与 Antigravity' in lbl.text() for lbl in help_agy.findChildren(QLabel)))
        b.antigravity_enabled = False
        help_codex = explanation_page(b)
        self.addCleanup(help_codex.deleteLater)
        self.assertFalse(any('多来源与 Antigravity' in lbl.text() for lbl in help_codex.findChildren(QLabel)))


class AntigravityFullFeatureTests(unittest.TestCase):
    def _clean_db(self, stem, ext='.sqlite3', unique=True):
        base = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())
        name = f'{stem}-{uuid.uuid4().hex}{ext}' if unique else f'{stem}{ext}'
        path = base / name
        for sibling in (path, path.with_name(path.name + '-wal'), path.with_name(path.name + '-shm')):
            sibling.unlink(missing_ok=True)
            self.addCleanup(sibling.unlink, missing_ok=True)
        return path

    def _build_conv_db(self, path, user_steps, gen_rows):
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB)')
            db.execute('CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)')
            db.executemany('INSERT INTO steps VALUES (?,?,?)', [(idx, 14, b'') for idx in user_steps])
            db.executemany('INSERT INTO gen_metadata VALUES (?,?)', gen_rows)
            db.commit()

    def _encode_gen(self, *, stamp, model, uncached, cache_read, cache_write, output,
                    req_id, duration=None, context=None, step_idxs=(1,)):
        usage = (
            field(2, uncached) + field(5, cache_read) + field(4, cache_write)
            + field(3, output) + field(11, req_id)
        )
        c9 = field(4, field(1, int(stamp)))
        if context is not None:
            used, limit = context
            c9 += field(10, field(1, used) + field(4, limit))
        chat = field(4, usage) + field(9, c9) + field(21, model.encode('utf-8'))
        if duration is not None:
            sec, nano = duration
            dur_blob = field(1, sec) + (field(2, nano) if nano else b'')
            chat += field(12, dur_blob)
        packed = b''.join(integer(i) for i in step_idxs)
        return field(1, chat) + field(2, packed)

    def test_blob_migration_turns_context_and_historical_rate(self):
        now = datetime(2026, 11, 23, 15, 30, 0).timestamp()
        today_1 = datetime(2026, 11, 23, 10, 5, 0).timestamp()
        today_2 = datetime(2026, 11, 23, 10, 12, 0).timestamp()
        yesterday_1 = (datetime(2026, 11, 23, 11, 0, 0) - timedelta(days=1)).timestamp()
        sid = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        sid_key = 'antigravity:' + sid

        usage_db = self._clean_db('agy-usage-mig')
        conv_db = self._clean_db(sid, ext='.db', unique=False)
        with closing(sqlite3.connect(usage_db)) as legacy:
            legacy.executescript('''
                CREATE TABLE files (name TEXT PRIMARY KEY, signature TEXT NOT NULL);
                CREATE TABLE requests (
                    name TEXT, idx INTEGER, identity TEXT, stamp REAL, model TEXT,
                    uncached INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER,
                    PRIMARY KEY(name, idx));
                INSERT INTO files VALUES ('stale.db', 'old-sig');
            ''')
            legacy.commit()

        self._build_conv_db(
            conv_db,
            user_steps=(2, 10),
            gen_rows=[
                (1, self._encode_gen(
                    stamp=today_1, model='gemini-2.5-pro', uncached=100, cache_read=200,
                    cache_write=20, output=300, req_id=b'req-1',
                    duration=(2, 500_000_000), context=(12000, 256000), step_idxs=(3,),
                )),
                (2, self._encode_gen(
                    stamp=today_2, model='gemini-2.5-pro', uncached=50, cache_read=250,
                    cache_write=0, output=150, req_id=b'req-2',
                    duration=(1, 500_000_000), context=(14500, 256000), step_idxs=(4,),
                )),
                (3, self._encode_gen(
                    stamp=yesterday_1, model='claude-sonnet-4-6', uncached=40, cache_read=0,
                    cache_write=0, output=200, req_id=b'req-y',
                    duration=(2, 0), context=(8000, 200000), step_idxs=(11,),
                )),
            ],
        )

        usage = AntigravityUsage(usage_db, conv_db.parent)
        with patch('pathlib.Path.glob', return_value=[conv_db]):
            snap = usage.refresh(now=now, priority_sessions=(sid_key,))

        turn = snap['session_turns'][sid_key]
        self.assertEqual((turn['turn_step'], turn['requests'], turn['exact']), (2, 2, True))
        self.assertEqual((turn['uncached'], turn['cache_read'], turn['cache_write'], turn['output'], turn['total']),
                         (150, 450, 20, 450, 1070))
        self.assertEqual((turn['duration_s'], turn['rate']), (4.0, 112.5))

        latest_req = snap['session_requests'][sid_key][0]
        self.assertEqual(latest_req['context'], {'used': 14500, 'limit': 256000, 'percent': 5.7})
        self.assertEqual(latest_req['rate'], 100.0)

        hist = snap['historical_rate']
        self.assertTrue(hist['historical'])
        self.assertEqual(hist['rate'], 112.5)
        self.assertEqual(hist['details'][0]['samples'], 2)

        self.assertTrue(snap['report']['supports_rates'])
        self.assertAlmostEqual(snap['report']['rates']['yesterday_mean']['rate'], 100.0, places=2)
        self.assertAlmostEqual(snap['rates']['today_output'][10]['rate'], 112.5, places=2)

    def test_multi_pool_quota_history_cycles_and_badge(self):
        now = datetime(2026, 11, 23, 16, 0, 0).timestamp()
        usage_db = self._clean_db('agy-quota-hist')
        a_sid = '44444444-4444-4444-8444-444444444444'
        conv_db = self._clean_db(a_sid, ext='.db', unique=False)
        self._build_conv_db(
            conv_db,
            user_steps=(1,),
            gen_rows=[(1, self._encode_gen(
                stamp=now - 1800, model='claude-sonnet-4-6', uncached=2500,
                cache_read=0, cache_write=0, output=500, req_id=b'id-c3',
                duration=(2, 0), context=(8000, 200000), step_idxs=(2,),
            ))],
        )
        usage = AntigravityUsage(usage_db, usage_db.parent)
        reset_2 = now + 86400 * 4
        reset_2_iso = datetime.fromtimestamp(reset_2, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        quota_doc = {
            'version': 1,
            'updatedAtMs': int(now * 1000),
            'groups': [
                {'displayName': 'Gemini Pro', 'buckets': [
                    {'window': '5h', 'remainingFraction': 0.8, 'resetTime': '2026-11-23T18:00:00Z'},
                    {'window': 'weekly', 'remainingFraction': 0.65, 'resetTime': reset_2_iso},
                ]},
                {'displayName': 'Claude', 'buckets': [
                    {'window': '5h', 'remainingFraction': 0.7, 'resetTime': '2026-11-23T18:00:00Z'},
                    {'window': 'weekly', 'remainingFraction': 0.65, 'resetTime': reset_2_iso},
                ]},
            ],
        }
        quota = normalize_quota(quota_doc, now=now)
        with patch('pathlib.Path.glob', return_value=[conv_db]):
            snap = usage.refresh(now=now, quota=quota, quota_group='Claude')

        self.assertEqual(snap['quota_history']['plan'], 'Claude')
        with closing(usage.connect()) as db:
            plans = sorted(r[0] for r in db.execute('SELECT plan FROM quota_observations'))
            self.assertEqual(plans, ['claude', 'gemini pro'])
            # Seed a completed closed baseline + 4 readings in current cycle for 'claude'.
            db.execute('DELETE FROM quota_observations')
            prev_start = now - 86400 * 10
            prev_deadline = now - 86400 * 3
            for i, rem in enumerate((100.0, 80.0, 50.0, 30.0)):
                t = prev_start + i * 3600
                db.execute('INSERT INTO quota_observations VALUES (?,?,?,?,?,?,?)',
                           (t, prev_deadline, rem, 'live', 'claude', None, ''))
            cur_deadline = reset_2
            for i, rem in enumerate((100.0, 90.0, 80.0, 65.0)):
                t = now - 3600 * (4 - i)
                db.execute('INSERT INTO quota_observations VALUES (?,?,?,?,?,?,?)',
                           (t, cur_deadline, rem, 'live', 'claude', None, ''))
            db.executemany(
                'INSERT INTO requests (name,idx,identity,stamp,model,uncached,cache_read,cache_write,output,duration_s) VALUES (?,?,?,?,?,?,?,?,?,?)',
                [
                    ('a.db', 1, 'id1', prev_start + 1800, 'claude-sonnet-4-6', 1000, 0, 0, 500, 2.0),
                    ('a.db', 2, 'id2', prev_start + 5400, 'claude-sonnet-4-6', 1000, 0, 0, 500, 2.0),
                    ('a.db', 3, 'id-c1', now - 3600 * 3 - 600, 'claude-sonnet-4-6', 1500, 0, 0, 500, 2.0),
                    ('a.db', 4, 'id-c2', now - 3600 * 2 - 600, 'claude-sonnet-4-6', 1500, 0, 0, 500, 2.0),
                ],
            )
            db.commit()

        with patch('pathlib.Path.glob', return_value=[conv_db]):
            snap2 = usage.refresh(now=now, quota=quota, quota_group='Claude')
        self.assertGreaterEqual(len(snap2['quota_history']['cycles']), 2)
        windows = [{'name': '一周', 'remaining': 65.0, 'resetsAt': cur_deadline, 'group': 'Claude'}]
        badge = visible_quota_change(snap2, windows, now=now)
        self.assertIsNotNone(badge)
        self.assertEqual(badge['kind'], 'reference')
        self.assertIsNotNone(badge['change'])

    def test_projects_workspace_mapping_costs_and_override(self):
        self.assertEqual(workspace_from_uris('["file:///e%3A/Code/demo-proj"]')[0], 'e:/code/demo-proj')
        self.assertEqual(workspace_from_uris(''), ('', ''))

        now = datetime(2026, 11, 23, 15, 0, 0).timestamp()
        yesterday = (datetime(2026, 11, 23, 10, 0, 0) - timedelta(days=1)).timestamp()
        sid_ws = '11111111-1111-4111-8111-111111111111'
        sid_un = '22222222-2222-4222-8222-222222222222'
        usage_db = self._clean_db('agy-proj-usage')
        base_profile = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / f'agy-proj-prof-{uuid.uuid4().hex}'
        summary_db = base_profile / '.gemini' / 'antigravity' / 'conversation_summaries.db'
        summary_db.parent.mkdir(parents=True, exist_ok=True)
        for sibling in (summary_db, summary_db.with_name(summary_db.name + '-wal'), summary_db.with_name(summary_db.name + '-shm')):
            sibling.unlink(missing_ok=True)
            self.addCleanup(sibling.unlink, missing_ok=True)
        price_path = self._clean_db('agy-prices', ext='.json')

        with closing(sqlite3.connect(summary_db)) as sdb:
            sdb.execute('CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, workspace_uris TEXT)')
            sdb.execute('INSERT INTO conversation_summaries VALUES (?,?,?)',
                        (sid_ws, 'Demo Task', '["file:///e%3A/Code/demo-proj"]'))
            sdb.execute('INSERT INTO conversation_summaries VALUES (?,?,?)',
                        (sid_un, 'Scratch Task', ''))
            sdb.commit()

        with closing(AntigravityUsage(usage_db, usage_db.parent).connect()) as db:
            db.executemany(
                'INSERT INTO requests (name,idx,identity,stamp,model,uncached,cache_read,cache_write,output,duration_s,turn_step) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                [
                    (sid_ws + '.db', 1, 'r1', yesterday, 'gemini-2.5-pro', 1_000_000, 0, 0, 100_000, 2.0, 2),
                    (sid_ws + '.db', 2, 'r2', now - 600, 'gemini-2.5-pro', 500_000, 0, 0, 50_000, 1.0, 2),
                    (sid_un + '.db', 1, 'r3', yesterday, 'gemini-2.5-flash', 1_000_000, 0, 0, 0, 1.0, 1),
                    (sid_ws + '.db', 3, 'r4', now - 300, 'internal-preview-x', 1000, 0, 0, 500, 1.0, 5),
                ],
            )
            db.commit()

            old_cached = cost_preferences._cached
            cost_preferences._cached = None
            self.addCleanup(lambda: setattr(cost_preferences, '_cached', old_cached))
            with patch.object(cost_preferences, 'path', return_value=price_path):
                snap = project_snapshot(db, base_profile, now)
                all_projects = snap['ranges']['all']
                ws_proj = next(p for p in all_projects if p['name'] == 'demo-proj')
                self.assertGreater(ws_proj['cost'], 3.0)
                self.assertEqual(ws_proj['pending_tokens'], 1500)
                self.assertEqual(len(ws_proj['session_usage'][0]['turns']), 2)
                # Unassigned session contributes to chart_cost days too.
                y_day = datetime.fromtimestamp(yesterday).strftime('%Y-%m-%d')
                self.assertIn(y_day, snap['chart_cost']['days'])

                self.assertIsNotNone(lookup_rate('gemini-2.5-pro'))
                self.assertIsNone(lookup_rate('internal-preview-x'))

                cost_preferences.save({'prices': {'internal-preview-x': [2.0, 0.2, 8.0]}})
                snap_custom = project_snapshot(db, base_profile, now)
                ws_custom = next(p for p in snap_custom['ranges']['all'] if p['name'] == 'demo-proj')
                self.assertEqual(ws_custom['pending_tokens'], 0)
                self.assertNotEqual(snap['price_revision'], snap_custom['price_revision'])

    def test_ui_rate_card_turn_summary_context_bar_and_daily_report(self):
        now = datetime(2026, 11, 23, 15, 30, 0).timestamp()
        today_str = datetime.fromtimestamp(now).strftime('%Y-%m-%d')
        y_str = (datetime.fromtimestamp(now) - timedelta(days=1)).strftime('%Y-%m-%d')
        sid = 'antigravity:99999999-1111-4111-8111-111111111111'

        # 1. Historical LiveRateCard & SessionRateBubble without calling Codex live service.
        card = LiveRateCard({'rate': 142.8, 'sessions': 1, 'historical': True,
                             'details': [{'session': sid, 'name': '演示会话', 'rate': 142.8, 'stamp': now, 'samples': 2, 'historical': True}]})
        card.resize(120, 22)
        card.show()
        self.addCleanup(card.deleteLater)
        self.assertEqual(card._rate_label(), '历史')
        with patch.object(card, '_inside_card', return_value=True), patch.object(card.popup.service, 'request') as req_mock:
            card._show_hint()
            self.addCleanup(card.popup.hide)
            req_mock.assert_not_called()
        self.assertTrue(card.popup.historical)

        # 2. Expanded CompactBubble -> RecentRequestCard with turn total & context bar, plus full DailyReport.
        b = CompactBubble()
        self.addCleanup(lambda: (b.report.close() if b.report else None, b.close(), drain()))
        b.antigravity_enabled = True
        b.card_source = 'antigravity'
        b.antigravity_quota = {
            'observed': now,
            'pools': {'Gemini Pro': [{'name': '5小时', 'remaining': 88.0, 'resetsAt': now + 3600, 'group': 'Gemini Pro'}]},
        }
        snapshot = {
            'provider': 'antigravity',
            'month': today_str[:7],
            'days': {today_str: 1800, y_str: 2400},
            'checked': now,
            'today_breakdown': {'day': today_str, 'uncached': 500, 'cache_read': 800, 'cache_write': 100, 'output': 400, 'total': 1800, 'requests': 2},
            'recent_sessions': [sid],
            'session_titles': {sid: '重构测试会话'},
            'session_requests': {sid: [{
                'stamp': now - 30, 'model': 'gemini-2.5-pro', 'uncached': 100, 'cache_read': 200,
                'cache_write': 20, 'output': 300, 'total': 620, 'duration_s': 2.0, 'rate': 150.0,
                'context_used': 32000, 'context_limit': 256000, 'turn_step': 2,
                'context': {'used': 32000, 'limit': 256000, 'percent': 12.5},
            }]},
            'session_turns': {sid: {
                'turn_step': 2, 'requests': 2, 'uncached': 300, 'cache_read': 500, 'cache_write': 20,
                'output': 450, 'total': 1270, 'duration_s': 3.5, 'rate': 128.6, 'exact': True,
            }},
            'historical_rate': {
                'rate': 128.6, 'sessions': 1, 'historical': True,
                'details': [{'session': sid, 'name': '重构测试会话', 'rate': 128.6, 'stamp': now, 'samples': 2, 'historical': True}],
            },
            'rates': {
                'days': {y_str: {'rate': 95.4, 'median_rate': 95.4, 'samples': 3, 'valid_samples': 3, 'output': 1200, 'duration_ms': 12578.0}},
                'today': [None] * 24,
                'yesterday': [None] * 24,
                'yesterday_mean': {'rate': 95.4, 'median_rate': 95.4, 'samples': 3, 'valid_samples': 3, 'output': 1200, 'duration_ms': 12578.0},
                'today_output': [None] * 24,
                'yesterday_output': [None] * 24,
                'pending': False, 'errors': 0, 'metric': 'generation', 'description': '测试描述',
            },
            'quota_history': {'cycles': [], 'anomaly': None, 'pending': False, 'partial': False, 'plan': 'gemini pro'},
            'projects': {
                'provider': 'antigravity',
                'ranges': {
                    'all': [{
                        'id': 'e:/code/demo', 'name': 'demo', 'path': 'E:/Code/demo',
                        'tokens': 2400, 'cost': 1.25, 'parts': [0.5, 0.25, 0.5],
                        'pending_tokens': 0, 'pending_reasons': {}, 'sessions': 1, 'session_ids': [sid],
                        'models': ['gemini-2.5-pro'], 'archived': False, 'priced_tokens': 2400, 'price_coverage': 1.0,
                        'model_usage': [{'model': 'gemini-2.5-pro', 'tokens': 2400, 'cost': 1.25, 'pending_tokens': 0, 'pending_reasons': {}, 'priced_tokens': 2400, 'price_coverage': 1.0}],
                        'session_usage': [{
                            'id': sid, 'name': '重构测试会话', 'tokens': 2400, 'cost': 1.25, 'pending_tokens': 0,
                            'detail_revision': 'all',
                            'turns': [{'id': '2', 'stamp': now - 60, 'models': ['gemini-2.5-pro'], 'tokens': 2400,
                                       'input': 1950, 'cached': 500, 'output': 450, 'cost': 1.25, 'pending_tokens': 0,
                                       'unknown': False, 'known': True}],
                        }],
                    }],
                    '30d': [], '7d': [], 'yesterday': [],
                },
                'chart_cost': {'days': {y_str: {'gemini-2.5-pro': [1.25, 2400]}},
                               'hours': {}, 'day': today_str, 'currency': 'USD', 'fx': 1.0},
                'yesterday': y_str, 'calendar_days': [y_str, today_str],
                'pending': False, 'initial_pending': False, 'syncing': False, 'errors': 0, 'limited': False,
                'price_revision': 'agy:test', 'price_table': {}, 'checked': now,
            },
            'report': {
                'day': y_str, 'hours': [0] * 24, 'hour_models': [{} for _ in range(24)],
                'yesterday_total': 2400, 'all_total': 4200,
                'daily_models': [
                    {'day': y_str, 'model': 'gemini-2.5-pro', 'total': 2400, 'input': 1950, 'output': 450, 'known': True},
                    {'day': today_str, 'model': 'gemini-2.5-pro', 'total': 1800, 'input': 1400, 'output': 400, 'known': True},
                ],
                'metadata_incomplete': False, 'supports_rates': True,
                'rates': {'days': {y_str: {'rate': 95.4, 'median_rate': 95.4, 'samples': 3, 'valid_samples': 3, 'output': 1200, 'duration_ms': 12578.0}},
                          'yesterday_mean': {'rate': 95.4, 'median_rate': 95.4, 'samples': 3, 'valid_samples': 3, 'output': 1200, 'duration_ms': 12578.0}},
            },
        }
        b.antigravity_snapshot = snapshot
        b.detail_tab = 'tokens'
        b.show_status(True)
        b.size_transition.complete()
        drain()

        self.assertTrue(b.live_rate_card.isEnabled())
        turn_lbl = b.findChild(QLabel, 'turn_summary_total')
        ctx_lbl = b.findChild(QLabel, 'context_usage_value')
        self.assertIsNotNone(turn_lbl)
        self.assertIn('2 次请求', turn_lbl.text())
        self.assertIsNotNone(ctx_lbl)
        self.assertIn('32.0K / 256.0K', ctx_lbl.text())
        self.assertIn('12.5%', ctx_lbl.text())

        report = DailyReport(b)
        self.addCleanup(report.deleteLater)
        report.set_snapshot(snapshot)
        report.open('overview')
        self.assertTrue(wait_for(lambda: not report._animating and report.surface.isVisible(), timeout=2))
        drain()
        self.assertFalse(report.page_buttons['cycle'].isHidden())
        self.assertFalse(report.page_buttons['projects'].isHidden())
        self.assertTrue(report.readout.isVisible())
        self.assertIn('昨日生成均速', report.readout.text())

        report.set_page('models')
        drain()
        self.assertTrue(report.model_rate_label.isVisible())
        self.assertEqual(report.model_rate_label.text(), '95.4')

        report.set_page('cycle')
        drain()
        self.assertTrue(report.cycle_view.isVisible())

        report.set_page('projects')
        drain()
        report.project_view.select(snapshot['projects']['ranges']['all'][0])
        drain()
        session_btn = next(
            btn for btn in report.project_view.findChildren(QToolButton)
            if '重构测试会话' in btn.accessibleName()
        )
        session_btn.click()
        drain()
        self.assertIsNotNone(report.project_view.session)
        self.assertEqual(len(report.project_view.session['turns']), 1)
        self.assertEqual(len(report.project_view.details.findChildren(TurnRow)), 1)
