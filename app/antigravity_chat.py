"""Wait for the matching Antigravity App reply without replaying a send."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from PySide6.QtCore import QObject, Qt, Signal, Slot

from antigravity_chat_setup import workspace
from antigravity import PREFIX,conversation_overview,stop_outcome
from antigravity_sidecar import configured_project, request


class ChatFailure(OSError):
    pass


class ChatNotSent(ChatFailure):
    pass


class ChatEnded(ChatFailure):
    pass


STATE_ERRORS = {
    'setup': '请先配置 Antigravity 的独立闲聊项目，并让桌面 App 加载桥接。',
    'missing': '桥接没有找到这条发送记录。为防重复发送，请在 Antigravity 中检查，或新建白龙会话。',
    'uncertain': 'Antigravity 未确认是否收到消息。请在桌面 App 检查；白龙保留了这次发送记录。',
    'busy': 'Antigravity 会话仍有一条未确认的消息。',
    'mismatch': '闲聊项目已经变更，请新建白龙会话。',
    'unavailable': 'Antigravity App 的消息接口暂不可用。',
    'invalid': 'Antigravity 闲聊请求格式无效。',
    'storage': 'Antigravity 桥接无法保存发送记录。',
}


def object_file(path):
    try:
        with path.open('rb') as stream:
            value = json.loads(stream.read(8193))
        return value if isinstance(value,dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def transcript_reply(path, started, after, operation):
    """Read the last completed assistant step in the acknowledged turn."""
    response = None
    began=False
    tag='[Bailong turn '+operation+']'
    try:
        with path.open('rb') as stream:
            offset = max(0, path.stat().st_size - 2_000_000)
            stream.seek(offset)
            if offset:stream.readline(2_000_000)
            for line in stream.read(2_000_000).splitlines():
                try:
                    item = json.loads(line)
                    index=item.get('step_index')
                    if type(index) is not int or index<=after:continue
                    stamp=datetime.fromisoformat(item['created_at'].replace('Z','+00:00')).timestamp()
                    if stamp*1000 < started-1000:continue
                    text=item.get('content')
                    kind=item.get('type')
                    if kind in ('USER_INPUT','SYSTEM_MESSAGE') and isinstance(text,str):
                        if tag in text:began=True
                        elif began and (kind=='USER_INPUT' or '[Bailong turn ' in text):break
                        continue
                    if not began or item.get('source')!='MODEL' or item.get('status')!='DONE' or kind!='PLANNER_RESPONSE':continue
                    if isinstance(text,str) and text.strip() and (response is None or index>=response['index']):
                        response={'text':text,'index':index,'truncated':'content' in (item.get('truncated_fields') or ())}
                except (ValueError, AttributeError, KeyError, TypeError, RecursionError):continue
    except OSError:
        return None
    return response


def full_reply(path, index, preview):
    """Recover only the already matched, completed planner step on App 2.34."""
    from antigravity_usage import blob_fields, file_signature, first
    try:
        stamp=file_signature(path)
        uri=path.resolve().as_uri()+'?mode=ro'+('' if stamp[3] else '&immutable=1')
        with closing(sqlite3.connect(uri,uri=True,timeout=.2)) as db:
            # An older transcript can still say DONE after the App clears a step.
            row=db.execute('SELECT rowid FROM steps WHERE idx=? AND step_type=15 AND status=3',(index,)).fetchone()
            if not row:return None
            with db.blobopen('steps','step_payload',row[0],readonly=True) as blob:
                payload=first(blob_fields(blob,len(blob),{20:{1:True,8:True}},2_000_000),20,{})
        fingerprint=preview[:min(len(preview),256)].encode('utf-8')
        if not fingerprint:return None
        for candidate in (first(payload,1),first(payload,8)):
            if isinstance(candidate,bytes) and candidate.startswith(fingerprint):
                text=candidate.decode('utf-8')
                if len(text)>=len(preview):return text
    except (OSError, sqlite3.Error, ValueError, IndexError, TypeError, OverflowError):pass
    return None


def wait_reply(profile, chat, operation, previous, text, after, cancel, timeout=140):
    project=configured_project(profile)
    if not project:raise (ChatNotSent if text is not None else ChatFailure)(STATE_ERRORS['setup'])
    fields=dict(chat=chat,operation=operation,project=project)
    if text is not None:
        # Preflight observes configuration only. Prompt goes through one send.
        try:info=request(profile,{'action':'chat-health'},timeout=1.0)
        except (OSError,ValueError,RecursionError):info=None
        if not info or info.get('state')!='ready' or info.get('project')!=project:
            raise ChatNotSent(STATE_ERRORS['setup'])
        if cancel.is_set():return None
        receipt=request(profile,dict(fields,action='chat-start',previous=previous,text=text),timeout=2.7)
        if not receipt:raise ChatFailure(STATE_ERRORS['uncertain'])
    else:receipt=None
    deadline=time.monotonic()+timeout
    while not cancel.is_set() and time.monotonic()<deadline:
        if not receipt or receipt.get('state')=='starting':
            receipt=request(profile,dict(fields,action='chat-poll'),timeout=1.2)
            if not receipt:
                cancel.wait(.6)
                continue
        state=receipt.get('state')
        if state=='starting':
            cancel.wait(.45)
            continue
        if state!='sent':raise ChatFailure(STATE_ERRORS.get(state,'Antigravity 没有确认此次发送。'))
        try:
            sid=str(uuid.UUID(receipt['remote']))
            started=receipt['started']
            if type(started) not in (int,float):raise ValueError
        except (ValueError,TypeError,KeyError,AttributeError):
            raise ChatFailure(STATE_ERRORS['uncertain']) from None
        profile=Path(profile)
        marker=workspace(profile)/'.agents'/'receipts'/(sid+'.json')
        ack=object_file(marker)
        if type(ack.get('at')) in (int,float) and ack['at']>=started:
            outcome=stop_outcome(ack.get('reason',ack.get('result')),ack.get('failed',False))
            if outcome=='canceled':raise ChatEnded('Antigravity 已取消本轮回复。可以继续对话。')
            if outcome=='error':raise ChatEnded('Antigravity 本轮异常结束。可以继续对话。')
            if outcome!='complete':
                raise ChatFailure('Antigravity 这次回复已中断。')
            transcript=profile/'.gemini'/'antigravity'/'brain'/sid/'.system_generated'/'logs'/'transcript.jsonl'
            reply=transcript_reply(transcript,started,after,operation)
            if reply:
                if not reply['truncated']:return reply
                source=profile/'.gemini'/'antigravity'/'conversations'/(sid+'.db')
                original=full_reply(source,reply['index'],reply['text'])
                if original:return dict(reply,text=original,truncated=False)
                if time.time()*1000-ack['at']>2400:
                    raise ChatFailure('Antigravity 的本地记录截断了这条回复，请在桌面 App 查看完整内容。')
        cancel.wait(.5)
    if not cancel.is_set():raise ChatFailure('等待 Antigravity 回复超时，可以重新获取这条回复。')
    return None


def stop_remote_reply(profile, chat, operation):
    project=configured_project(profile)
    if not project:raise ChatFailure(STATE_ERRORS['setup'])
    packet=dict(chat=chat,operation=operation,project=project)
    deadline=time.monotonic()+6
    while time.monotonic()<deadline:
        outcome=request(profile,dict(packet,action='chat-cancel'),timeout=2.9)
        if not outcome:break
        state=outcome.get('state')
        if state=='starting':time.sleep(.2);continue
        if state=='not_current':raise ChatFailure('Antigravity 当前回合与白龙的消息不匹配，请在 App 中检查。')
        if state!='cancel_requested':break
        try:
            remote=str(uuid.UUID(outcome['remote']));started=outcome['started']
            if type(started) not in (int,float):raise ValueError
        except (KeyError,ValueError,TypeError,AttributeError):break
        # Give the App time to commit the cancellation or its final reply.
        idle=0
        while time.monotonic()<deadline:
            time.sleep(.25)
            ack=object_file(workspace(profile)/'.agents'/'receipts'/(remote+'.json'))
            if ack.get('at',0)>=started and stop_outcome(ack.get('reason'),ack.get('failed'))=='complete':
                return 'complete',remote
            info=conversation_overview(profile,[PREFIX+remote]).get(PREFIX+remote,{})
            if info.get('status')=='CASCADE_RUN_STATUS_IDLE' and info.get('background')==0:idle+=1
            else:idle=0
            if idle>=2:return 'stopped',remote
        break
    raise ChatFailure('白龙未能确认远端已停止，可以重新获取这条回复。')


class AntigravityChatStream(QObject):
    delta = Signal(str)
    done = Signal(str)
    failed = Signal(str)
    result = Signal(object, str)

    def __init__(self, parent):
        super().__init__(parent)
        self.cancelled=threading.Event()
        self.index=None
        self.not_sent=False
        self.ended=False
        self.result.connect(self.finish,Qt.ConnectionType.QueuedConnection)

    def start(self, profile, chat, operation, previous, text, after):
        def run():
            reply=None;error=''
            try:reply=wait_reply(profile,chat,operation,previous,text,after,self.cancelled)
            except ChatFailure as failure:
                error=str(failure);self.not_sent=isinstance(failure,ChatNotSent);self.ended=isinstance(failure,ChatEnded)
            except (OSError, ValueError, AttributeError, TypeError, RecursionError):error='与 Antigravity 桥接的连接未能确认。'
            if not self.cancelled.is_set():
                try:self.result.emit(reply,error)
                except RuntimeError:pass
        threading.Thread(target=run,name='BailongAntigravityChat',daemon=True).start()

    @Slot(object,str)
    def finish(self,reply,error):
        if self.cancelled.is_set():return
        if reply:
            self.index=reply['index'];self.done.emit(reply['text'])
        else:self.failed.emit(error or '未能读取 Antigravity 回复。')

    def cancel(self):
        self.cancelled.set()
