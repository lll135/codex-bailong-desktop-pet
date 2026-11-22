import json
import unittest
from unittest.mock import Mock,patch

from antigravity import PREFIX, Tasks, conversation_overview, source_of
from conversation_roster import ConversationRoster
from desktop_core import normalized
from session_read_state import session_id


CONVERSATION = '23e53a97-3d24-4df6-9da6-9b58949062fd'


def packet(event, conversation=CONVERSATION, **fields):
    return json.dumps(dict(version=1, event=event, conversation=conversation, **fields)).encode()


class AntigravityTests(unittest.TestCase):
    def test_background_work_does_not_complete_until_idle(self):
        tasks = Tasks()
        start, = tasks.accept(packet('PreInvocation'))
        work, = tasks.accept(packet('PreToolUse', tool='run_command'))
        background, = tasks.accept(packet('Stop', idle=False, reason='model_stop', failed=False))
        done, = tasks.accept(packet('Stop', idle=True, reason='model_stop', failed=False))
        self.assertEqual([event['turn'] for event in (work, background, done)], [start['turn']] * 3)
        self.assertEqual((start['state'], work['activity'], background['state'], done['kind']),
                         ('thinking', 'execute', 'working', 'complete'))
        self.assertFalse(tasks.active)
        self.assertEqual(tasks.accept(packet('Stop', idle=True, reason='model_stop', failed=False)), [])

    def test_failed_stop_or_disconnect_does_not_notify_success(self):
        tasks = Tasks()
        tasks.accept(packet('PreInvocation'))
        failed, = tasks.accept(packet('Stop', idle=True, reason='error', failed=True))
        self.assertEqual((failed['kind'], failed['state']), ('status', 'interrupted'))
        tasks.accept(packet('PreInvocation'))
        dropped, = tasks.disconnect()
        self.assertEqual(dropped['state'], 'interrupted')
        self.assertFalse(tasks.active)
        self.assertNotEqual(failed['turn'], dropped['turn'])

    def test_desktop_app_no_tool_call_is_a_normal_stop(self):
        tasks=Tasks()
        tasks.accept(packet('PreInvocation'))
        event,=tasks.accept(packet('Stop',idle=True,reason='NO_TOOL_CALL',failed=False))
        self.assertEqual(event['kind'],'complete')

    def test_invalid_packets_do_not_create_tasks(self):
        tasks = Tasks()
        for payload in (b'null', b'{"version":1,"event":[]}', packet('PreInvocation', '..'), packet('unknown')):
            self.assertEqual(tasks.accept(payload), [])

    def test_colliding_provider_ids_route_to_independent_views(self):
        tasks = Tasks()
        agy, = tasks.accept(packet('PreInvocation'))
        codex = dict(agy, session=CONVERSATION)
        roster = ConversationRoster()
        for event in (codex, agy):
            roster.observe(event)
        self.assertEqual(session_id(agy['session']), PREFIX + CONVERSATION)
        self.assertEqual(source_of(agy['session']), 'antigravity')
        for source, event in (('codex', codex), ('antigravity', agy)):
            rows, remaining = roster.selection([], source=source)
            self.assertEqual([row['session'] for row in rows], [event['session']])
            self.assertEqual(remaining, 0)
        self.assertEqual(len(roster.selection([])[0]), 2)

    def test_card_source_defaults_preserve_existing_settings(self):
        old = {'bubble_mode': 'minimal', 'capsule_action': 'report', 'panel_detached': True}
        saved = normalized(old)
        self.assertEqual({key: saved[key] for key in old}, old)
        self.assertFalse(saved['antigravity_enabled'])
        self.assertEqual(saved['card_source'], 'codex')
        self.assertEqual(normalized({'card_source': 'antigravity'})['card_source'], 'antigravity')

    def test_dual_mode_gives_each_active_source_a_normal_card_slot(self):
        roster = ConversationRoster()
        for session in ('codex-1', 'codex-2', PREFIX + CONVERSATION):
            roster.observe(dict(kind='status', session=session, state='working'))
        rows, remaining = roster.selection([], balanced=True)
        self.assertEqual([source_of(row['session']) for row in rows], ['codex', 'antigravity'])
        self.assertEqual(remaining, 1)

    def test_title_lookup_accepts_only_explicit_antigravity_ids(self):
        db=Mock()
        db.execute.return_value=[(CONVERSATION,' Real App title ','CASCADE_RUN_STATUS_IDLE',0)]
        with patch('antigravity.sqlite3.connect',return_value=db):
            overview=conversation_overview('unused',[CONVERSATION,PREFIX+'invalid',PREFIX+CONVERSATION])
        self.assertEqual(overview[PREFIX+CONVERSATION]['title'],'Real App title')
        self.assertEqual(db.execute.call_args.args[1],[CONVERSATION])
        db.close.assert_called_once()

    def test_missing_stop_retires_only_the_observed_turn_without_completion(self):
        tasks=Tasks()
        task,=tasks.accept(packet('PreInvocation'))
        sid=task['session'];turns={sid:task['turn']}
        running={sid:dict(status='CASCADE_RUN_STATUS_RUNNING',background=1)}
        idle={sid:dict(status='CASCADE_RUN_STATUS_IDLE',background=0)}
        self.assertEqual(tasks.observe_native(turns,idle),[])
        self.assertEqual(tasks.observe_native(turns,running),[])
        self.assertEqual(tasks.observe_native(turns,idle),[])
        self.assertEqual(tasks.observe_native({sid:'stale'},idle),[])
        result,=tasks.observe_native(turns,idle)
        self.assertEqual((result['kind'],result['state']),('status','idle'))
        self.assertFalse(tasks.active)
        self.assertEqual(tasks.accept(packet('Stop',idle=True,reason='NO_TOOL_CALL',failed=False)),[])

    def test_background_survives_idle_summary_until_fully_idle(self):
        tasks=Tasks()
        task,=tasks.accept(packet('PreInvocation'))
        sid=task['session'];turns={sid:task['turn']}
        tasks.accept(packet('Stop',idle=False,reason='NO_TOOL_CALL',failed=False))
        for _ in range(3):
            self.assertEqual(tasks.observe_native(turns,{sid:dict(status='CASCADE_RUN_STATUS_IDLE',background=1)}),[])
        self.assertEqual(tasks.active[sid]['display'],'后台任务运行中')
        completed,=tasks.accept(packet('Stop',idle=True,reason='NO_TOOL_CALL',failed=False))
        self.assertEqual(completed['kind'],'complete')
