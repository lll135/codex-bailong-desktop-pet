import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from antigravity_chat import ChatFailure, ChatNotSent, stop_remote_reply, transcript_reply, wait_reply


OPERATION = 'a' * 32
CREATED = '2026-11-23T00:00:00Z'


def step(index, kind, content, **fields):
    return dict(step_index=index, created_at=CREATED, type=kind, status='DONE', content=content, **fields)


class AntigravityChatTests(unittest.TestCase):
    def read(self, records, after=-1):
        raw = b'\n'.join(json.dumps(item).encode('utf-8') for item in records)
        path = Mock()
        path.stat.return_value.st_size = len(raw)
        path.open.return_value = io.BytesIO(raw)
        return transcript_reply(path, 0, after, OPERATION)

    def test_receipt_is_matched_to_the_requested_turn(self):
        records = [step(1, 'PLANNER_RESPONSE', 'old', source='MODEL'),
                   step(2, 'SYSTEM_MESSAGE', '[Bailong turn '+OPERATION+']\nHi'),
                   step(3, 'PLANNER_RESPONSE', 'correct', source='MODEL'),
                   step(4, 'USER_INPUT', 'Different turn'),
                   step(5, 'PLANNER_RESPONSE', 'wrong', source='MODEL')]
        self.assertEqual(self.read(records), dict(text='correct',index=3,truncated=False))
        self.assertIsNone(self.read(records,after=3))

    def test_truncated_assistant_text_is_identified(self):
        records = [step(1,'USER_INPUT','[Bailong turn '+OPERATION+']\nHi'),
                   step(2,'PLANNER_RESPONSE','clipped',source='MODEL',truncated_fields=['content'])]
        self.assertTrue(self.read(records)['truncated'])

    def test_wrong_project_preflight_sends_no_message(self):
        with patch('antigravity_chat.configured_project',return_value='first'), \
             patch('antigravity_chat.request',return_value={'state':'ready','project':'second'}) as call:
            with self.assertRaises(ChatNotSent):
                wait_reply(Path('unused'),'chat',OPERATION,'','Hi',-1,threading.Event())
        self.assertEqual([item.args[1]['action'] for item in call.call_args_list],['chat-health'])

    def test_resume_only_polls_the_existing_operation(self):
        status=dict(state='sent',remote='1f4e2b37-5941-4291-a4ea-89b40647093e',started=2)
        reply=dict(text='result',index=4,truncated=False)
        with patch('antigravity_chat.configured_project',return_value='project'), \
             patch('antigravity_chat.request',return_value=status) as call, \
             patch('antigravity_chat.object_file',return_value={'at':3,'reason':'NO_TOOL_CALL','failed':False}), \
             patch('antigravity_chat.transcript_reply',return_value=reply):
            self.assertEqual(wait_reply(Path('unused'),'chat',OPERATION,'',None,0,threading.Event()),reply)
        self.assertEqual([item.args[1]['action'] for item in call.call_args_list],['chat-poll'])

    def test_remote_cancel_waits_for_confirmed_native_idle(self):
        remote='1f4e2b37-5941-4291-a4ea-89b40647093e'
        receipt=dict(state='cancel_requested',remote=remote,started=2)
        busy={'antigravity:'+remote:dict(status='CASCADE_RUN_STATUS_IDLE',background=1)}
        idle={'antigravity:'+remote:dict(status='CASCADE_RUN_STATUS_IDLE',background=0)}
        with patch('antigravity_chat.configured_project',return_value='project'), \
             patch('antigravity_chat.request',return_value=receipt) as call, \
             patch('antigravity_chat.object_file',return_value={}), \
             patch('antigravity_chat.conversation_overview',side_effect=[busy,idle,idle]) as samples, \
             patch('antigravity_chat.time.sleep'):
            self.assertEqual(stop_remote_reply(Path('unused'),'chat',OPERATION),('stopped',remote))
        self.assertEqual(call.call_args.args[1]['action'],'chat-cancel')
        self.assertEqual(samples.call_count,3)

    def test_cancel_does_not_assume_an_unmatched_turn_stopped(self):
        with patch('antigravity_chat.configured_project',return_value='project'), \
             patch('antigravity_chat.request',return_value={'state':'not_current'}), \
             patch('antigravity_chat.conversation_overview') as samples:
            with self.assertRaises(ChatFailure):stop_remote_reply(Path('unused'),'chat',OPERATION)
        samples.assert_not_called()
