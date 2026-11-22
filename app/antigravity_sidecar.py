"""Configure Bailong's on-demand quota sidecar under the Antigravity App."""
import json
from pathlib import Path
import re
import secrets
import socket
import time

from desktop_core import atomic_json
from antigravity_chat_setup import CHAT_SCRIPT, exclusive_text, prepare_workspace


SIDE_ID = 'bailong-quota'


def project_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value.strip()):
        raise ValueError('请填写独立闲聊项目的有效 ID。')
    return value.strip()

SCRIPT = r'''import net from 'node:net';
import { timingSafeEqual } from 'node:crypto';
import { renameSync, writeFileSync } from 'node:fs';

const [token] = process.argv.slice(2);
const address = process.env.ANTIGRAVITY_LS_ADDRESS || '';
const csrf = process.env.ANTIGRAVITY_CSRF_TOKEN;
const localAddress = /^(?:127\.0\.0\.1|localhost|\[::1\]):\d{1,5}$/i;
let pending = null;
let cached = null;

async function retrieveQuota() {
  if (!localAddress.test(address) || !csrf) return null;
  const response = await fetch(`http://${address}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary`, {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(2200),
    headers: { 'Content-Type': 'application/json', 'x-codeium-csrf-token': csrf }, body: '{}',
  });
  if (!response.ok) return null;
  const document = await response.json();
  const groups = document.response?.groups ?? document.groups;
  if (!Array.isArray(groups)) return null;
  return {
    version: 1, updatedAtMs: Date.now(),
    groups: groups.slice(0, 64).map(group => ({
      displayName: typeof group?.displayName === 'string' ? group.displayName.slice(0, 100) : '',
      buckets: (Array.isArray(group?.buckets) ? group.buckets : []).filter(bucket => ['5h', 'weekly'].includes(bucket?.window)).slice(0, 2).map(bucket => ({
        window: bucket.window,
        resetTime: typeof bucket.resetTime === 'string' ? bucket.resetTime.slice(0, 80) : '',
        remainingFraction: Number.isFinite(bucket.remainingFraction) ? bucket.remainingFraction : null,
      })),
    })),
  };
}

function requestQuota() {
  if (cached && Date.now() - cached.updatedAtMs < 8_000) return Promise.resolve(cached);
  if (!pending) pending = retrieveQuota().catch(() => null).then(result => {
    cached = result; return result;
  }).finally(() => { pending = null; });
  return pending;
}

const expected = Buffer.from(token);
const server = net.createServer({ allowHalfOpen: true }, socket => {
  let data = Buffer.alloc(0);
  socket.setTimeout(3200, () => socket.destroy());
  socket.on('error', () => socket.destroy());
  const receive = chunk => {
    if (data.length + chunk.length > 115000) { socket.destroy(); return; }
    data = Buffer.concat([data, chunk]);
    if (!data.includes(10)) return;
    socket.off('data', receive);
    const line = data.subarray(0,data.indexOf(10)).toString('utf8');
    let request = {token:line,action:'quota'};
    if (line.startsWith('{')) {
      try { request = JSON.parse(line); } catch { socket.destroy(); return; }
    }
    if (typeof request?.token !== 'string') { socket.destroy(); return; }
    const supplied = Buffer.from(request.token);
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) { socket.destroy(); return; }
    const result = request.action === 'quota' ? requestQuota() : chatRequest(request);
    Promise.resolve(result).then(value => { if (!socket.destroyed) socket.end(JSON.stringify(value) + '\n'); });
  };
  socket.on('data', receive);
  socket.on('end', () => { if (!data.includes(10)) socket.destroy(); });
});
server.maxConnections = 8;
server.listen(0, '127.0.0.1', () => {
  const temp = `endpoint.${process.pid}.tmp`;
  writeFileSync(temp, JSON.stringify({ version: 1, port: server.address().port }), 'utf8');
  renameSync(temp, 'endpoint.json');
});
''' + CHAT_SCRIPT


def config_dir(profile):
    return Path(profile) / '.gemini' / 'config'


def sidecar_dir(profile):
    return config_dir(profile) / 'sidecars' / SIDE_ID


def configured_project(profile):
    try:
        data=json.loads((config_dir(profile)/'config.json').read_bytes())
        value=data['sidecars'][SIDE_ID]['projectId']
        return value if isinstance(value,str) else ''
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return ''


def installed_token(profile):
    path = sidecar_dir(profile) / 'sidecar.json'
    try:
        document = json.loads(path.read_bytes())
        args = document['args']
        if args[0] == 'server.mjs' and isinstance(args[1], str) and len(args[1]) == 48:
            return args[1]
    except (OSError, ValueError, KeyError, IndexError, TypeError, RecursionError):
        pass
    return None


def endpoint(profile):
    token=installed_token(profile)
    if token is None:return None
    try:
        info=json.loads((sidecar_dir(profile)/'endpoint.json').read_bytes())
        port=info.get('port')
        if info.get('version')==1 and type(port) is int and 0<port<65536:
            return port,token
    except (OSError,ValueError,AttributeError,RecursionError):pass
    return None


def request(profile, packet=None, timeout=2.7):
    address=endpoint(profile)
    if address is None:return None
    port, token=address
    payload=token if packet is None else json.dumps(dict(packet,token=token),ensure_ascii=False,separators=(',',':'))
    with socket.create_connection(('127.0.0.1',port),timeout=.25) as client:
        client.sendall((payload+'\n').encode('utf-8'))
        deadline=time.monotonic()+timeout
        data=bytearray()
        while len(data)<=131072 and b'\n' not in data:
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            client.settimeout(remaining)
            fragment=client.recv(4096)
            if not fragment:break
            data.extend(fragment)
        if b'\n' in data and len(data)<=131072:return json.loads(data.partition(b'\n')[0])
    return None


def install(profile, project=None):
    directory = sidecar_dir(profile)
    config = config_dir(profile) / 'config.json'
    try:
        original = config.read_bytes()
        document = json.loads(original.decode('utf-8-sig'))
    except FileNotFoundError:
        original, document = None, {}
    if not isinstance(document, dict) or not isinstance(document.get('sidecars', {}), dict):
        raise ValueError('Antigravity Sidecar 配置结构无法识别。')
    if project:
        project=project_id(project)
        prepare_workspace(profile)
    manifest = directory / 'sidecar.json'
    legacy = installed_token(profile)
    if manifest.exists() and legacy is None:
        raise ValueError('同名 Sidecar 已存在，请先检查其来源。')
    token = legacy or secrets.token_hex(24)
    entry = dict(command='node', args=['server.mjs', token], restart_policy='on-failure',
                 display_name='白龙桌宠额度桥接')
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / 'server.mjs'
    exclusive_text(script,SCRIPT,(
        '9765d32d153c549d7e3f2ea7c3b32c74e01261578f4bcb5b489ec2ed15660380',
        '5beacf8df555a0c8ea1a20dffc00eec99b44a5052e439352c78835fd5f60e9b4',
        '22681a27aaa212f9bc224bc404347d673fad66b96178d0749edccdce17260477',
    ))
    if not manifest.is_file():atomic_json(manifest, entry)
    sidecars = document.setdefault('sidecars', {})
    previous = sidecars.get(SIDE_ID, {})
    if not isinstance(previous, dict):raise ValueError('现有白龙 Sidecar 配置无法识别。')
    updated=dict(previous,enabled=True)
    if project:updated['projectId']=project
    if previous==updated:return
    try:
        current = config.read_bytes()
    except FileNotFoundError:
        current = None
    if current != original:raise OSError('Antigravity 配置已更新，请重试。')
    if original is not None:
        with (directory / f'config-before-{secrets.token_hex(8)}.json').open('xb') as backup:backup.write(original)
    sidecars[SIDE_ID] = updated
    atomic_json(config, document)
