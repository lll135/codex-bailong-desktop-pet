"""Windows Antigravity App hook configuration and task events."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import uuid

from desktop_core import atomic_json


SOURCE = 'antigravity'
LABEL = 'Antigravity'
PREFIX = SOURCE + ':'
HOOK_GROUP = 'bailong-desktop-pet'
EVENTS = ('PreInvocation', 'PreToolUse', 'Stop')
FRAME_LIMIT = 2048
SUCCESS_REASONS = frozenset(('model_stop', 'no_tool_call', 'executor_termination_reason_no_tool_call'))
CANCEL_REASONS = frozenset(('cancelled','canceled','user_cancelled','user_canceled',
                            'executor_termination_reason_user_canceled'))


def stop_outcome(reason, failed):
    reason=str(reason or '').lower()
    if reason in CANCEL_REASONS:return 'canceled'
    if failed is True or reason in ('error','executor_termination_reason_error'):return 'error'
    if failed is False and reason in SUCCESS_REASONS:return 'complete'
    return 'unknown'


def pipe_name(profile):
    identity = os.path.normcase(str(Path(profile).resolve())).encode('utf-8')
    return 'BailongAntigravity-' + hashlib.sha256(identity).hexdigest()[:24]


def source_of(session):
    return SOURCE if str(session).startswith(PREFIX) else 'codex'


def executable(local_app_data=None):
    root = local_app_data or os.environ.get('LOCALAPPDATA')
    if not root:
        return None
    path = Path(root) / 'Programs' / LABEL / 'Antigravity.exe'
    return path if path.is_file() else None


def open_app(session=None):
    path = executable()
    if path is None:
        raise OSError('未找到 Antigravity 桌面 App 的安装。')
    os.startfile(str(path))


def desktop_open():
    import psutil
    try:
        return any((process.info['name'] or '').lower() == 'antigravity.exe'
                   for process in psutil.process_iter(['name']))
    except (OSError, psutil.Error):
        return None


def conversation_overview(profile, sessions):
    ids = []
    for session in dict.fromkeys(sessions):
        if source_of(session) != SOURCE:
            continue
        try:
            ids.append(str(uuid.UUID(session[len(PREFIX):])))
        except (ValueError, TypeError, AttributeError):
            continue
        if len(ids) >= 48:
            break
    if not ids:
        return {}
    path = Path(profile) / '.gemini' / SOURCE / 'conversation_summaries.db'
    bindings = ','.join('?' for _ in ids)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.15)) as db:
            rows=db.execute(f'SELECT conversation_id,title,status,not_fully_idle FROM conversation_summaries WHERE conversation_id IN ({bindings})',ids)
            return {PREFIX+sid:dict(title=title.strip()[:160] if isinstance(title,str) else '',
                                    status=status,background=background) for sid,title,status,background in rows}
    except (OSError,sqlite3.Error,ValueError):
        return {}


# A hook has a synchronous contract with the host. Send only a small frame and
# return the neutral result even when Bailong is closed or the pipe is busy.
HOOK_SCRIPT = r'''param([string]$EventName, [string]$PipeName)
$ErrorActionPreference = 'Stop'
$response = switch ($EventName) {
    'PreToolUse' { '{"decision":"allow"}' }
    'Stop' { '{"decision":"stop"}' }
    default { '{}' }
}
try {
    [Console]::InputEncoding = [Text.Encoding]::UTF8
    $data = [Console]::In.ReadToEnd() | ConvertFrom-Json
    # The global hook file is shared with IDE and CLI. Match the App's root.
    $path = [string]$data.transcriptPath
    $sid = [string]$data.conversationId
    $app = $path -match '(?i)[\\/]antigravity[\\/]brain[\\/]'
    if (-not $path -and $sid -match '^[0-9a-fA-F-]{36}$') {
        $appRoot = Join-Path $env:USERPROFILE '.gemini\antigravity'
        $app = (Test-Path -LiteralPath (Join-Path $appRoot ('brain\' + $sid))) -or
               (Test-Path -LiteralPath (Join-Path $appRoot ('conversations\' + $sid + '.db')))
    }
    if ($app -and $sid -match '^[0-9a-fA-F-]{36}$') {
        $tool = [string]$data.toolCall.name
        if ($tool -notmatch '^[a-zA-Z0-9_-]{0,100}$') { $tool = '' }
        $reason = [string]$data.terminationReason
        if ($reason -notmatch '^[a-zA-Z0-9_-]{0,80}$') { $reason = '' }
        $packet = @{version=1; event=$EventName; conversation=[string]$data.conversationId;
            tool=$tool; reason=$reason; idle=$data.fullyIdle; failed=[bool]$data.error}
        $pipe = New-Object IO.Pipes.NamedPipeClientStream('.', $PipeName, [IO.Pipes.PipeDirection]::Out)
        try {
            $pipe.Connect(100)
            $bytes = [Text.Encoding]::UTF8.GetBytes(($packet | ConvertTo-Json -Compress) + "`n")
            if ($bytes.Length -le 2048) { $pipe.Write($bytes, 0, $bytes.Length) }
        } finally { $pipe.Dispose() }
    }
} catch { }
[Console]::Out.WriteLine($response)
'''


def configure_hooks(profile, state_dir, enabled):
    profile, state_dir = Path(profile), Path(state_dir)
    config = profile / '.gemini' / 'config' / 'hooks.json'
    script = state_dir / 'antigravity-hook.ps1'
    if re.search(r'[\r\n"%!?]', str(script)):
        raise ValueError('数据目录包含 Antigravity Hook 命令不支持的字符。')
    try:
        original = config.read_bytes()
        document = json.loads(original.decode('utf-8-sig'))
    except FileNotFoundError:
        original, document = None, {}
    if not isinstance(document, dict):
        raise ValueError('Antigravity hooks.json 的根节点需要是对象。')
    command = subprocess.list2cmdline(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                                      '-ExecutionPolicy', 'Bypass', '-File', str(script)])
    pipe = pipe_name(profile)
    handlers = {event: {'type': 'command', 'command': f'{command} -EventName {event} -PipeName {pipe}', 'timeout': 3}
                for event in EVENTS}
    group = {event: [handler] for event, handler in handlers.items()}
    group['PreToolUse'] = [{'matcher': 'run_command|write_to_file|replace_file_content|multi_replace_file_content',
                           'hooks': [handlers['PreToolUse']]}]
    group['enabled'] = enabled
    previous = document.get(HOOK_GROUP)
    if HOOK_GROUP not in document and not enabled:
        return config
    if HOOK_GROUP in document and (not isinstance(previous, dict) or dict(previous, enabled=enabled) != group):
        raise ValueError('已存在同名 Antigravity Hook，请先在 Antigravity 检查该条目。')
    if enabled:
        script.parent.mkdir(parents=True, exist_ok=True)
        from antigravity_chat_setup import exclusive_text
        exclusive_text(script,HOOK_SCRIPT,'bb81e96fa29c80f0cdab62470e0365b0f64514b355369d378502e3a166acf2b6')
    if previous != group:
        try:
            latest = config.read_bytes()
        except FileNotFoundError:
            latest = None
        if latest != original:
            raise OSError('Antigravity Hook 配置已被其他程序更新，请重试。')
        if original is not None:
            with (state_dir / f'antigravity-hooks-before-{uuid.uuid4().hex}.json').open('xb') as backup:
                backup.write(original)
        document[HOOK_GROUP] = group
        atomic_json(config, document)
    return config


class Tasks:
    def __init__(self):
        self.active = {}
        self.native = {}

    def accept(self, frame):
        try:
            data = json.loads(frame)
            if not isinstance(data, dict) or data.get('version') != 1 or data.get('event') not in EVENTS:
                return []
            session = PREFIX + str(uuid.UUID(data['conversation']))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return []
        event = data['event']
        previous = self.active.get(session)
        common = {'session': session, 'turn': previous['turn'] if previous else uuid.uuid4().hex,
                  'title': 'Antigravity 会话 ' + session[-4:]}
        if event == 'Stop':
            if not previous:
                return []
            if data.get('idle') is True:
                self.active.pop(session)
                self.native.pop(session,None)
                outcome=stop_outcome(data.get('reason'),data.get('failed'))
                if outcome=='complete':
                    return [dict(common, kind='complete')]
                display={'error':'任务异常结束','canceled':'任务已取消'}.get(outcome,'任务已中断')
                return [dict(common, kind='status', state='interrupted', display=display)]
            if data.get('idle') is not False:
                return []
            self.native[session]=(common['turn'],'busy')
            status = dict(common, kind='status', state='working', display='后台任务运行中')
        elif event == 'PreToolUse':
            tool = data.get('tool')
            activity, display = {
                'run_command': ('execute', '正在执行命令'),
                'write_to_file': ('edit', '正在编辑文件'),
                'replace_file_content': ('edit', '正在编辑文件'),
                'multi_replace_file_content': ('edit', '正在编辑文件'),
            }.get(tool if isinstance(tool, str) else '', ('tool', '正在使用工具'))
            status = dict(common, kind='status', state='working', activity=activity, display=display)
        else:
            status = dict(common, kind='status', state='thinking', display='正在思考', turn_started=not previous)
        if not previous and len(self.active) >= 128:
            return []
        self.active[session] = status
        return [status]

    def disconnect(self):
        events = [dict(value, state='interrupted', display='Antigravity 会话连接已断开', turn_started=False)
                  for value in self.active.values()]
        self.active.clear()
        self.native.clear()
        return events

    def interrupt(self, sid, turn):
        task=self.active.get(sid)
        if not task or task['turn']!=turn:return None
        self.active.pop(sid);self.native.pop(sid,None)
        return dict(task,state='interrupted',display='任务已取消',turn_started=False)

    def observe_native(self, turns, summary):
        """Retire an unconfirmed stop without issuing a completion notification."""
        events=[]
        for sid,turn in turns.items():
            task=self.active.get(sid)
            if not task or task['turn']!=turn or sid not in summary:continue
            value=summary[sid]
            status=value['status']
            if value['background']==1 or status in ('CASCADE_RUN_STATUS_RUNNING','CASCADE_RUN_STATUS_BUSY','CASCADE_RUN_STATUS_CANCELING'):
                self.native[sid]=(turn,'busy')
            elif status=='CASCADE_RUN_STATUS_IDLE' and value['background']==0:
                observed=self.native.get(sid)
                if observed==(turn,'busy'):
                    self.native[sid]=(turn,'idle')
                elif observed==(turn,'idle'):
                    self.active.pop(sid);self.native.pop(sid)
                    events.append(dict(task,state='idle',display='任务已停止，未收到结束回执',turn_started=False))
        return events
