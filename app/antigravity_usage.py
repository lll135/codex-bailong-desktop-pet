"""Read Antigravity App usage into a separate, durable Bailong index."""
from contextlib import closing
from pathlib import Path
import sqlite3
import time

from token_analytics import UNKNOWN
from token_calendar import usage_snapshot


SOURCE = '本机 Antigravity 会话'


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
        for rowid, index in db.execute('SELECT rowid,idx FROM gen_metadata ORDER BY idx'):
            if cancelled():
                return
            with db.blobopen('gen_metadata', 'data', rowid, readonly=True) as blob:
                root = blob_fields(blob, len(blob), {1: {4: True, 9: {4: True}, 19: True, 21: True}, 2: True})
            chat = first(root, 1, {})
            if 9 in chat:
                # Both readers share usage_record's timestamp lookup.
                start = first(chat, 9, {})
                stamp = timestamp(nested(start, 4))
            else:
                stamp = None
            record = usage_record(root, chat, step_at, stamp)
            if record is not None:
                yield index, record


class AntigravityUsage:
    def __init__(self, path, conversations):
        self.path = Path(path)
        self.conversations = Path(conversations)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=.4)
        db.executescript('''
            CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, signature TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                name TEXT, idx INTEGER, identity TEXT, stamp REAL, model TEXT,
                uncached INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER,
                PRIMARY KEY(name, idx));
            CREATE INDEX IF NOT EXISTS requests_identity ON requests(identity);
            CREATE VIEW IF NOT EXISTS usage AS
                SELECT identity AS fingerprint, stamp, model, name AS session,
                       uncached+cache_read+cache_write+output AS tokens,
                       uncached+cache_read+cache_write AS input_tokens,
                       output AS output_tokens, 1 AS known
                FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY identity ORDER BY stamp IS NULL,
                      uncached+cache_read+cache_write+output DESC, model = '未识别模型', name, idx) AS preference
                      FROM requests)
                WHERE preference=1 AND stamp IS NOT NULL;
        ''')
        return db

    def replace_file(self, db, path, signature, cancelled):
        with db:
            db.execute('DELETE FROM requests WHERE name=?', (path.name,))
            def rows():
                for index, (key, *record) in read_records(path, signature, cancelled):
                    yield path.name, index, key or f'file:{path.name}:{index}', *record
            db.executemany('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?)', rows())
            if cancelled() or file_signature(path) != signature:
                raise OSError('Antigravity 会话在读取期间发生变化')
            db.execute('INSERT OR REPLACE INTO files VALUES (?,?)', (path.name, repr(signature)))

    def refresh(self, cancelled=lambda: False, budget=.7, now=None):
        deadline = time.monotonic() + budget
        with closing(self.connect()) as db:
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
            result['report']['supports_rates'] = False
            result.update(provider='antigravity', pending=pending, readable_files=readable,
                          unavailable=not readable and not known and not pending)
            return result
