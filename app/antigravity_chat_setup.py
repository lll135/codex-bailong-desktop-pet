"""Workspace-scoped chat profile and authenticated Sidecar dispatch."""
import hashlib
import os
from pathlib import Path
import subprocess
import uuid

from desktop_core import atomic_json


CHAT_PROFILE = '''---
name: bailong-chat
description: Text-only chat with Bailong.
tools: []
mainAgent: true
subagent: false
commandExecutionPolicy: off
---
Respond directly in text as the user's desktop companion. Do not request tools.
The first Bailong turn line is a tracking tag. Never quote it in a reply.
'''

WORKSPACE_HOOK = r'''param([string]$EventName)
$ErrorActionPreference = 'Stop'
if ($EventName -eq 'PreToolUse') {
    [Console]::Out.WriteLine('{"decision":"deny","reason":"This workspace is for text-only Bailong chat."}')
    exit 0
}
try {
    [Console]::InputEncoding = [Text.Encoding]::UTF8
    $data = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $sid = [string]$data.conversationId
    if ($sid -match '^[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$' -and $data.fullyIdle -is [bool] -and $data.fullyIdle) {
        $root = Join-Path $PSScriptRoot 'receipts'
        [IO.Directory]::CreateDirectory($root) | Out-Null
        $file = Join-Path $root ($sid.ToLowerInvariant() + '.json')
        $temp = $file + '.' + $PID + '.tmp'
        $reason = [string]$data.terminationReason
        if ($reason -notmatch '^[a-zA-Z0-9_-]{0,80}$') { $reason = '' }
        $receipt = @{at=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(); reason=$reason; failed=[bool]$data.error;
            transcriptPresent=[bool]$data.transcriptPath}
        [IO.File]::WriteAllText($temp,($receipt | ConvertTo-Json -Compress))
        if ([IO.File]::Exists($file)) { [IO.File]::Replace($temp,$file,[NullString]::Value) }
        else { [IO.File]::Move($temp,$file) }
    }
} catch { }
[Console]::Out.WriteLine('{"decision":"stop"}')
'''


def workspace(profile):
    return Path(profile) / '.gemini' / 'bailong-chat-workspace'


def exclusive_text(path, content, previous=()):
    try:
        with path.open('x', encoding='ascii', newline='\n') as stream:
            stream.write(content)
    except FileExistsError:
        existing=path.read_text(encoding='ascii')
        if existing==content:return
        accepted=(previous,) if isinstance(previous,str) else previous
        if hashlib.sha256(existing.encode('ascii')).hexdigest() not in accepted:
            raise ValueError('白龙闲聊目录已有修改，请检查 '+path.name)
        revision=uuid.uuid4().hex
        backup=path.with_name(path.name+'.previous-'+revision)
        with backup.open('x',encoding='ascii',newline='\n') as stream:stream.write(existing)
        temp=path.with_name(path.name+'.next-'+revision)
        with temp.open('x',encoding='ascii',newline='\n') as stream:stream.write(content)
        if path.read_text(encoding='ascii')!=existing:
            raise OSError('白龙桥接脚本已在更新期间变更，请重试。')
        os.replace(temp,path)


def prepare_workspace(profile):
    root = workspace(profile)
    agents = root / '.agents'
    (agents / 'agents').mkdir(parents=True, exist_ok=True)
    script = agents / 'bailong-chat-hook.ps1'
    if any(char in str(script) for char in '"%!?\r\n'):
        raise ValueError('闲聊目录包含 Hook 命令不支持的字符。')
    exclusive_text(agents / 'agents' / 'bailong-chat.md', CHAT_PROFILE)
    exclusive_text(script, WORKSPACE_HOOK, 'b4f04f183799f039f0321c2689f9152c5171b5df0f99b0fa0612dec73461fa0a')
    command = subprocess.list2cmdline(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                                      '-ExecutionPolicy', 'Bypass', '-File', os.fspath(script)])
    hooks = {'bailong-text-chat': {
        'PreToolUse': [{'matcher': '.*', 'hooks': [{'command': command+' -EventName PreToolUse', 'timeout': 3}]}],
        'Stop': [{'command': command+' -EventName Stop', 'timeout': 3}],
    }}
    path = agents / 'hooks.json'
    if path.exists():
        import json
        if json.loads(path.read_bytes()) != hooks:
            raise ValueError('闲聊目录里的 hooks.json 已有修改，请检查。')
    else:
        atomic_json(path, hooks)
    return root


CHAT_SCRIPT = r'''
import { execFile } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, readSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';

const uuid = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i;
const opId = /^[a-f0-9]{32}$/;
const projectId = /^[a-zA-Z0-9_-]{1,128}$/;
const chatProject = process.env.ANTIGRAVITY_PROJECT_ID || '';
const chatRoot = 'chat-state';
const appRoot = process.env.ANTIGRAVITY_APP_DATA_DIR;
const agentEvents = process.env.ANTIGRAVITY_EXECUTABLE_DATA_DIR ?
  join(dirname(process.env.ANTIGRAVITY_EXECUTABLE_DATA_DIR),'events') : null;
const activeChats = new Set();

function writeChat(file, value) {
  mkdirSync(chatRoot,{recursive:true});
  const temp = file + '.new';
  writeFileSync(temp,JSON.stringify(value),'utf8');
  renameSync(temp,file);
}

function readChat(file) {
  try { return JSON.parse(readFileSync(file, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

function acceptedFromEvent(receipt) {
  if (!agentEvents) return null;
  const tag = '[Bailong turn '+receipt.operation+']';
  try {
    const files = readdirSync(agentEvents).filter(name => /^[A-Za-z0-9_.-]+\.json$/.test(name)).sort().slice(-64);
    for (const name of files.reverse()) {
      const path = join(agentEvents,name);
      const stat = statSync(path);
      if (stat.size > 262144 || stat.mtimeMs < receipt.started-1000) continue;
      let event;
      try { event = JSON.parse(readFileSync(path,'utf8')); } catch { continue; }
      if (event.error) continue;
      const payload = receipt.remote ? event.payload?.sendMessage : event.payload?.newConversation;
      const text = payload?.prompt || payload?.content || payload?.message;
      const id = payload?.conversationId || payload?.recipientId || receipt.remote;
      if (typeof text === 'string' && text.split('\n',1)[0] === tag && uuid.test(id) &&
          (!receipt.remote || receipt.remote === id.toLowerCase())) return id.toLowerCase();
    }
  } catch {}
  return null;
}

function sendChat(file, receipt, text) {
  const remote = receipt.remote;
  const native = process.env.ANTIGRAVITY_AGENTAPI_EXE || (process.platform === 'win32' && process.env.LOCALAPPDATA ?
    join(process.env.LOCALAPPDATA,'Programs','Antigravity','resources','bin','language_server.exe') : '');
  const command = native || 'agentapi';
  if (native && !existsSync(native)) { writeChat(file,{...receipt,state:'unavailable'}); return; }
  const args = remote ? ['send-message', remote, text] :
    ['new-conversation','--title=Bailong chat','--profile=bailong-chat',text];
  if (native) args.unshift('agentapi');
  activeChats.add(receipt.chat);
  execFile(command,args,{encoding:'utf8',timeout:20000,windowsHide:true,maxBuffer:262144},(error,stdout) => {
    activeChats.delete(receipt.chat);
    try {
      // An invocation error may arrive after the host accepted the message.
      // Keep that request id sticky to prevent duplicate delivery.
      let value = null;
      try { value = JSON.parse(stdout.trim()); } catch {}
      const eventId = acceptedFromEvent(receipt);
      const candidate = eventId || value?.conversationId || value?.conversation_id || value?.cascadeId || value?.payload?.newConversation?.conversationId || remote;
      if ((eventId || !error) && candidate && uuid.test(candidate)) {
        writeChat(file,{...receipt,remote:candidate.toLowerCase(),state:'sent'});
      } else {
        writeChat(file,{...receipt,state:'uncertain'});
      }
    } catch {}
  });
}

function isCurrentChat(receipt) {
  if (!appRoot || !uuid.test(receipt.remote)) return false;
  const path = join(appRoot,'brain',receipt.remote,'.system_generated','logs','transcript.jsonl');
  let fd;
  try {
    fd=openSync(path,'r');
    const size=statSync(path).size;
    const data=Buffer.alloc(Math.min(size,1_000_000));
    const count=readSync(fd,data,0,data.length,size-data.length);
    for (const line of data.subarray(0,count).toString('utf-8').split('\n').reverse()) {
      let item;
      try { item=JSON.parse(line); } catch { continue; }
      const text=typeof item.content === 'string' ? item.content : '';
      const tag=/\[Bailong turn ([a-f0-9]{32})\]/.exec(text);
      if (item.type==='USER_INPUT' || item.type==='SYSTEM_MESSAGE' && tag) return tag?.[1]===receipt.operation;
    }
  } catch {} finally { if (fd!==undefined) closeSync(fd); }
  return false;
}

async function cancelChat(receipt) {
  if (receipt.state==='starting') return {state:'starting'};
  if (receipt.state!=='sent' || !isCurrentChat(receipt)) return {state:'not_current'};
  if (!localAddress.test(address) || !csrf) return {state:'unavailable'};
  try {
    const response=await fetch(`http://${address}/exa.language_server_pb.LanguageServerService/CancelCascadeInvocation`,{
      method:'POST',redirect:'error',signal:AbortSignal.timeout(2200),
      headers:{'Content-Type':'application/json','x-codeium-csrf-token':csrf},
      body:JSON.stringify({cascadeId:receipt.remote}),
    });
    return response.ok ? {state:'cancel_requested',remote:receipt.remote,started:receipt.started} : {state:'unavailable'};
  } catch { return {state:'unavailable'}; }
}

function chatRequest(request) {
  if (!projectId.test(chatProject)) return {state:'setup'};
  if (request.action === 'chat-health') return {state:'ready',project:chatProject};
  const { chat, operation, previous } = request;
  if (!opId.test(chat) || !opId.test(operation)) return {state:'invalid'};
  if (request.project !== chatProject) return {state:'mismatch'};
  const file = join(chatRoot, chat+'.json');
  try {
    const old = readChat(file);
    if (old && old.project !== chatProject) return {state:'mismatch'};
    if (old?.operation === operation) {
      if (old.state !== 'sent') {
        const id = acceptedFromEvent(old);
        if (id) { const receipt={...old,remote:id,state:'sent'};writeChat(file,receipt);return request.action==='chat-cancel'?cancelChat(receipt):receipt; }
      }
      if (old.state === 'starting' && !activeChats.has(chat)) return {...old,state:'uncertain'};
      return request.action==='chat-cancel'?cancelChat(old):old;
    }
    if (request.action !== 'chat-start') return {state:'missing'};
    if (old && (old.operation !== previous || old.state !== 'sent' || activeChats.has(chat))) return {state:'busy'};
    if (!old && previous) return {state:'missing'};
    if (typeof request.text !== 'string' || !request.text.trim() || request.text.length > 14000) return {state:'invalid'};
    // A leading dash can be interpreted as an agentapi flag. Keep the prompt
    // positional by giving the tool an ordinary text prefix on every send.
    const text = '[Bailong turn '+operation+']\n'+request.text;
    const receipt = {chat, operation, project:chatProject, remote:old?.remote || '', started:Date.now(),state:'starting'};
    writeChat(file,receipt);
    sendChat(file,receipt,text);
    return readChat(file);
  } catch { return {state:'storage'}; }
}
'''
