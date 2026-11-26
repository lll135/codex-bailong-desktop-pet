import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import uuid

from antigravity import HOOK_GROUP, LEGACY_PRE_TOOL_MATCHERS, PRE_TOOL_MATCHER, PREFIX, Tasks, configure_hooks, conversation_overview, source_of
from conversation_roster import ConversationRoster
from desktop_core import normalized
from session_read_state import session_id


CONVERSATION = '23e53a97-3d24-4df6-9da6-9b58949062fd'
_QT_APP = None


def _qt_app():
    global _QT_APP
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


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

    def test_tool_classification_covers_edit_and_research_tools(self):
        tasks=Tasks()
        tasks.accept(packet('PreInvocation'))
        for tool in ('write_file','edit_file'):
            event,=tasks.accept(packet('PreToolUse',tool=tool))
            self.assertEqual((event['activity'],event['display']),('edit','正在编辑文件'))
        for tool in ('read_file','view_file','grep_search','find_by_name','search_web'):
            event,=tasks.accept(packet('PreToolUse',tool=tool))
            self.assertEqual((event['activity'],event['display']),('research','正在查阅资料'))
        unknown,=tasks.accept(packet('PreToolUse',tool='custom_tool'))
        self.assertEqual((unknown['activity'],unknown['display']),('tool','正在使用工具'))

    def test_configure_hooks_upgrades_legacy_matcher_and_rejects_unrelated_entry(self):
        base=Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())/'bailong-hook-tests'
        profile=base/'profile'
        state_dir=base/'state'
        config=profile/'.gemini'/'config'/'hooks.json'
        script=state_dir/'antigravity-hook.ps1'
        backup_uuid=uuid.UUID(int=1)
        backup=state_dir/f'antigravity-hooks-before-{backup_uuid.hex}.json'
        for path in (config,script,backup):
            path.unlink(missing_ok=True)
            self.addCleanup(path.unlink,missing_ok=True)
        config.parent.mkdir(parents=True,exist_ok=True)
        state_dir.mkdir(parents=True,exist_ok=True)
        with patch('antigravity.uuid.uuid4',return_value=backup_uuid):
            configure_hooks(profile,state_dir,True)
        current=json.loads(config.read_text(encoding='utf-8'))
        self.assertEqual(current[HOOK_GROUP]['PreToolUse'][0]['matcher'],PRE_TOOL_MATCHER)

        legacy=json.loads(json.dumps(current))
        legacy[HOOK_GROUP]['PreToolUse'][0]['matcher']=LEGACY_PRE_TOOL_MATCHERS[0]
        config.write_text(json.dumps(legacy),encoding='utf-8')
        backup.unlink(missing_ok=True)
        with patch('antigravity.uuid.uuid4',return_value=backup_uuid):
            configure_hooks(profile,state_dir,True)
        upgraded=json.loads(config.read_text(encoding='utf-8'))
        self.assertEqual(upgraded[HOOK_GROUP]['PreToolUse'][0]['matcher'],PRE_TOOL_MATCHER)
        self.assertTrue(backup.is_file())

        tampered=json.loads(json.dumps(upgraded))
        tampered[HOOK_GROUP]['PreToolUse'][0]['matcher']='unrelated_tool'
        config.write_text(json.dumps(tampered),encoding='utf-8')
        with self.assertRaises(ValueError):
            configure_hooks(profile,state_dir,True)


class StatusRecoveryTests(unittest.TestCase):
    def _clean_files(self,paths):
        for path in paths:
            path.unlink(missing_ok=True)
            self.addCleanup(path.unlink,missing_ok=True)

    def _isolated_state_patches(self,name):
        base=Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())/name
        base.mkdir(parents=True,exist_ok=True)
        known=[base/fname for fname in (
            'settings.json','runtime.json','task-notifications.json','task-notifications.sqlite3',
            'task-notifications.sqlite3-wal','task-notifications.sqlite3-shm',
            'local-reset-evidence.json','codex-connection-check.json','antigravity-hook.ps1',
        )]
        self._clean_files(known)
        return base,[
            patch(f'{mod}.{key}',base/('settings.json' if key=='CONFIG' else ''))
            for mod in ('enhanced','desktop') for key in ('CONFIG','STATE_DIR')
        ]

    def test_rollout_tail_recovers_from_offline_when_logs_appear(self):
        from enhanced_core import RolloutTail
        base=Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())/'bailong-rollout-recovery-test'
        sessions_dir=base/'sessions'/'2026'/'11'/'23'
        sessions_dir.mkdir(parents=True,exist_ok=True)
        log_file=sessions_dir/'rollout-2026-11-23T00-00-00-11111111-2222-3333-4444-555555555555.jsonl'
        self._clean_files([log_file])

        tail=RolloutTail(base)
        batch1=tail.poll()
        self.assertTrue(any(e.get('kind')=='status' and e.get('state')=='offline' for e in batch1))
        self.assertTrue(tail._offline_reported)

        log_file.write_text(
            json.dumps(dict(type='session_meta',timestamp='2026-11-23T00:00:01Z',payload=dict(id='11111111-2222-3333-4444-555555555555')))+'\n',
            encoding='utf-8',
        )
        tail.catalog.last_check=-1e9;tail.catalog.last_full=-1e9
        batch2=tail.poll()
        self.assertTrue(any(e.get('kind')=='status' and e.get('state')=='idle' for e in batch2))
        self.assertFalse(tail._offline_reported)

        batch3=tail.poll()
        self.assertFalse(any(e.get('kind')=='status' for e in batch3))

    def test_activity_thread_recovery_status_uses_active_session_or_file_presence(self):
        from enhanced import ActivityThread
        from enhanced_core import Session
        _qt_app()
        base,patches=self._isolated_state_patches('bailong-activity-recovery-test')
        home=base/'codex-home'
        (home/'sessions').mkdir(parents=True,exist_ok=True)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        thread=ActivityThread(home=home)
        self.assertEqual(thread._recovery_status()['state'],'offline')

        dummy=home/'sessions'/'dummy.jsonl'
        self._clean_files([dummy])
        thread.tail.files[dummy]=dict(sid='dummy',offset=0,buffer=b'',history=False,skip=False,recognized=True,touched=0.)
        self.assertEqual(thread._recovery_status()['state'],'idle')

        thread.tail.reducer.sessions['s1']=Session(title='测试任务',state='working',done=False,updated=10.0)
        status=thread._recovery_status()
        self.assertEqual(status['state'],'working')
        self.assertEqual(status['active'],1)

    def test_antigravity_combined_status_does_not_inherit_codex_offline(self):
        from types import SimpleNamespace
        from PySide6.QtCore import QEvent
        from desktop import DesktopPet
        APP=_qt_app()
        _,patches=self._isolated_state_patches('bailong-agy-combined-status-test')
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        pet=DesktopPet(monitor=False)
        self.addCleanup(lambda:(pet.close(),APP.processEvents(),APP.sendPostedEvents(None,QEvent.Type.DeferredDelete),APP.processEvents()))
        pet.apply_settings(dict(antigravity_enabled=True,wandering=False,random_actions=False),save=False)

        pet.on_activity(dict(kind='status',state='offline',active=0,text='尚未找到本机 Codex 会话'))
        self.assertEqual(pet.last_activity['state'],'offline')
        self.assertEqual(pet.bubble.status_indicator.visual(),'idle')

        bridge=SimpleNamespace(tasks=Tasks(),server=SimpleNamespace(isListening=lambda:True))
        pet.antigravity_bridge=bridge
        clock=[100.]
        pet.bubble.status_indicator.clock=lambda:clock[0]
        for ev in bridge.tasks.accept(packet('PreInvocation')):
            pet.on_activity(ev)
        self.assertEqual(pet.bubble.status_indicator.visual(),'busy')
        turn=bridge.tasks.active[PREFIX+CONVERSATION]['turn']
        with patch.object(pet,'notify'):
            for ev in bridge.tasks.accept(packet('Stop',idle=True,reason='model_stop',failed=False)):
                pet.on_activity(ev)
        pet.bubble.status_indicator.acknowledge()
        pet.on_activity(dict(kind='status',state='offline',active=0,text='尚未找到本机 Codex 会话'))
        self.assertEqual(pet.bubble.status_indicator.state,'idle')
        self.assertEqual(pet.bubble.status_indicator.visual(),'complete')

        for ev in bridge.tasks.accept(packet('PreInvocation')):
            pet.on_activity(ev)
        turn=bridge.tasks.active[PREFIX+CONVERSATION]['turn']
        with patch.object(pet,'notify'):
            for ev in bridge.tasks.accept(packet('Stop',idle=True,reason='model_stop',failed=False)):
                pet.on_activity(ev)
        clock[0]+=5.
        pet.bubble.status_indicator.acknowledge_seen([(PREFIX+CONVERSATION,turn)])
        pet.on_activity(dict(kind='status',state='offline',active=0,text='尚未找到本机 Codex 会话'))
        self.assertEqual(pet.bubble.status_indicator.state,'idle')
        self.assertEqual(pet.bubble.status_indicator.visual(),'idle')

        pet.antigravity_bridge=None
        pet.apply_settings(dict(antigravity_enabled=False),save=False)
        self.assertEqual(pet.bubble.status_indicator.visual(),'warning')
        pet.apply_settings(dict(antigravity_enabled=True),save=False)
        self.assertEqual(pet.bubble.status_indicator.visual(),'idle')

        pet.antigravity_bridge=SimpleNamespace(tasks=Tasks(),server=SimpleNamespace(isListening=lambda:False))
        pet.observe_combined_status()
        self.assertEqual(pet.bubble.status_indicator.visual(),'warning')
        pet.antigravity_bridge=SimpleNamespace(tasks=Tasks(),server=SimpleNamespace(isListening=lambda:True))
        pet.observe_combined_status()
        self.assertEqual(pet.bubble.status_indicator.visual(),'idle')

    def test_low_quota_alert_source_labels_diagnostics_focus_and_exit_linkage(self):
        import time
        from types import SimpleNamespace
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QLabel
        from antigravity import connection_diagnostics
        from antigravity_quota import normalize_quota
        from desktop import DesktopPet
        from island_widgets import StatusIsland
        from status_indicator import StatusIndicator
        from surface_policy import SurfacePolicy

        APP=_qt_app()
        base,patches=self._isolated_state_patches('bailong-agy-linkage-test')
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        pet=DesktopPet(monitor=False)
        self.addCleanup(lambda:(pet.close(),APP.processEvents(),APP.sendPostedEvents(None,QEvent.Type.DeferredDelete),APP.processEvents()))
        pet.apply_settings(dict(antigravity_enabled=True,card_source='antigravity',codex_exit=True,wandering=False,random_actions=False),save=False)

        # 1. StatusIndicator & StatusIsland source labels (no middle dots for Antigravity).
        self.assertEqual(pet.bubble.status_indicator.source_label,'Antigravity')
        ind=StatusIndicator()
        self.assertEqual(ind.toolTip(),'Codex 待命')
        ind.set_source_label('Antigravity')
        self.assertEqual(ind.toolTip(),'Antigravity 待命')
        self.assertNotIn('·',ind.toolTip())
        ind.observe(dict(state='complete'))
        self.assertEqual(ind.toolTip(),'Antigravity 任务已完成，已确认')
        self.assertNotIn('·',ind.toolTip())
        island=StatusIsland(ind)
        self.addCleanup(lambda:(island.close(),APP.processEvents()))
        self.assertTrue(island.accessibleName().startswith('Antigravity '))
        ind.set_source_label('Codex')
        self.assertIn('·',ind.toolTip())

        # 2. Low quota alert deduplicated and ignores stale (>120s) snapshots.
        now=time.time()
        q_low=normalize_quota({'version':1,'updatedAtMs':int(now*1000),'groups':[
            {'displayName':'Claude','buckets':[{'window':'5h','remainingFraction':0.12,'resetTime':'2026-11-24T12:00:00Z'}]}
        ]},now=now)
        snap_ok={'provider':'antigravity','report':{'day':'2026-11-23','yesterday_total':0,'all_total':0,'hours':[0]*24},
                 'today_breakdown':{},'recent_sessions':[],'session_requests':{},'historical_rate':{'rate':None,'sessions':0,'details':[],'historical':True}}
        pet.antigravity_data=SimpleNamespace(snapshot=snap_ok,quota=q_low,isRunning=lambda:False)
        with patch.object(pet,'notify') as notify:
            pet.antigravity_data_ready()
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[0],'Antigravity 额度提醒')
            pet.antigravity_data_ready()
            notify.assert_called_once()
            q_stale={'observed':now-400,'pools':{'Gemini':[{'name':'一周','remaining':5.0,'resetsAt':now+999,'group':'Gemini'}]}}
            pet.antigravity_data=SimpleNamespace(snapshot=snap_ok,quota=q_stale,isRunning=lambda:False)
            pet.antigravity_data_ready()
            notify.assert_called_once()

        # 3. SettingsDialog health summary & cross-source diagnostics (no middle dots).
        pet.bubble.antigravity_snapshot={'report':{'daily_models':[{'day':'2026-11-23','model':'gemini-2.5-pro','total':100}]}}
        pet.open_settings()
        pet.settings_dialog.activate_page(2)
        health=pet.settings_dialog.findChild(QLabel,'antigravity_health_summary')
        self.assertIsNotNone(health)
        self.assertIn('Antigravity 桌面端',health.text())
        self.assertNotIn('·',health.text())
        with patch('pricing_ui.open_editor') as open_editor:
            pet.settings_dialog.open_pricing()
            open_editor.assert_called_once()
            self.assertIn('gemini-2.5-pro',open_editor.call_args.args[1])
            self.assertEqual(open_editor.call_args.args[2],pet.refresh_pricing_targets)

        hook_file=base/'.gemini'/'config'/'hooks.json'
        hook_file.parent.mkdir(parents=True,exist_ok=True)
        self._clean_files([hook_file])
        hook_file.write_text(json.dumps({HOOK_GROUP:{'enabled':True}}),encoding='utf-8')
        diag=connection_diagnostics(
            base,
            bridge=SimpleNamespace(server=SimpleNamespace(isListening=lambda:True)),
            quota=q_low,
            snapshot={'unavailable':False,'partial':False,'readable_files':2},
            enabled=True,
            presence=True,
            now=now,
        )
        self.assertEqual([item['state'] for item in diag['items'] if item['key'] in ('app','bridge','usage')],['available','available','available'])
        self.assertNotIn('·',diag['summary'])

        # 4. SurfacePolicy foreground match includes antigravity.exe when antigravity_enabled is True.
        from PySide6.QtCore import QObject
        fake_pet=QObject()
        fake_pet.settings={'codex_focus_only':True,'antigravity_enabled':False}
        fake_pet._focus_editor_override=False
        fake_pet.set_focus_hidden=Mock()
        policy=SurfacePolicy(fake_pet,api=SimpleNamespace(foreground=lambda:42,window_pid=lambda hwnd:999))
        self.addCleanup(lambda:(policy.timer.stop(),APP.removeEventFilter(policy)))
        with patch('desktop_services.codex_desktop_process',return_value=False),patch('antigravity.desktop_process',return_value=True):
            policy.poll_focus()
            fake_pet.set_focus_hidden.assert_called_with(True)
            fake_pet.settings['antigravity_enabled']=True
            policy.poll_focus()
            fake_pet.set_focus_hidden.assert_called_with(False)

        # 5. Combined codex_exit gate: waits for both Codex & Antigravity to exit.
        fake_exit_pet=SimpleNamespace(
            settings={'codex_exit':True,'antigravity_enabled':True},
            _codex_exit_gate=pet._codex_exit_gate,
            _codex_polling=False,_codex_closing=False,
            _assistant_presence={'antigravity':True},
            close=Mock(),codexPresence=SimpleNamespace(emit=Mock()),
        )
        fake_exit_pet._codex_exit_gate.reset()
        gen=fake_exit_pet._codex_exit_gate.generation
        def inline_thread(**kwargs):
            return SimpleNamespace(start=kwargs['target'])
        with patch('codex_connection.is_wsl_source',return_value=True),patch('antigravity.desktop_open',side_effect=[True,False,False,False]),patch('threading.Thread',side_effect=inline_thread):
            for _ in range(4):
                DesktopPet.poll_codex_presence(fake_exit_pet)
                DesktopPet.observe_codex_presence(fake_exit_pet,fake_exit_pet.codexPresence.emit.call_args.args[0])
        fake_exit_pet.close.assert_called_once()
        # Reset gate when antigravity_enabled toggles.
        prev_gen=pet._codex_exit_gate.generation
        pet.apply_settings(dict(antigravity_enabled=False),save=False)
        self.assertGreater(pet._codex_exit_gate.generation,prev_gen)
