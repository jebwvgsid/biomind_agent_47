"""Disposable display index with bounded RAM; never a business state store.

Indices remain absolute after eviction so a slow terminal can read every
unprinted receipt. JSON (not pickle) is used for this private local cache.
Render callbacks read only resident active text; disk reads are for history
draining or explicit detail requests. Original evidence retains authority.
"""
from collections import OrderedDict, deque
from collections.abc import MutableMapping, MutableSet, Sequence
import json
import sqlite3
import tempfile
import threading
from pathlib import Path


def encode(value):
    from agent_core.cli_display import DisplayTicket
    if isinstance(value, DisplayTicket):
        fields = {k: v for k, v in vars(value).items() if k != 'confirmation'}
        fields['_finished'] = value.confirmation.done()
        fields['_success'] = value.confirmation.result() if fields['_finished'] else False
        return ['ticket', encode(fields)]
    if isinstance(value, dict):
        return ['dict', [[encode(k), encode(v)] for k, v in value.items()]]
    if isinstance(value, (list, tuple, deque)):
        return [type(value).__name__, [encode(v) for v in value]]
    return ['scalar', value]


def decode(value):
    kind, body = value
    if kind == 'scalar':
        return body
    if kind == 'dict':
        return {decode(k): decode(v) for k, v in body}
    if kind == 'ticket':
        from agent_core.cli_display import DisplayTicket
        fields = decode(body)
        finished, success = fields.pop('_finished'), fields.pop('_success')
        ticket = DisplayTicket(**fields)
        if finished:
            ticket.finish(success, ticket.degradation)
        return ticket
    values = [decode(v) for v in body]
    return tuple(values) if kind == 'tuple' else deque(values, maxlen=16) if kind == 'deque' else values


class DisplayArchive:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='biomind-display-')
        self.path = str(Path(self.directory.name) / 'display.sqlite3')
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.execute('PRAGMA cache_size=-512')
        self.db.execute('PRAGMA journal_mode=OFF')
        self.db.execute('CREATE TABLE cache (collection TEXT, key TEXT, value TEXT, PRIMARY KEY(collection,key))')
        self.db.execute('CREATE TABLE bodies (identity TEXT PRIMARY KEY, length INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE body_chunks (identity TEXT, start INTEGER, end INTEGER, body TEXT, PRIMARY KEY(identity,start))')

    def get(self, name, key):
        with self.lock:
            row = self.db.execute('SELECT value FROM cache WHERE collection=? AND key=?', (name, key)).fetchone()
        if row is None:
            raise KeyError(key)
        return decode(json.loads(row[0]))

    def put(self, name, key, value):
        wire = json.dumps(encode(value), ensure_ascii=False)
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)', (name, key, wire))

    def keys(self, name):
        with self.lock:
            cursor = self.db.execute('SELECT key FROM cache WHERE collection=?', (name,))
            while rows := cursor.fetchmany(64):
                yield from (row[0] for row in rows)

    def body(self, identity, text=None, *, append=False, offset=0, limit=8192):
        key = json.dumps(identity, ensure_ascii=False)
        with self.lock:
            if text is not None:
                row = self.db.execute('SELECT length FROM bodies WHERE identity=?', (key,)).fetchone() if append else None
                start = row[0] if row else 0
                if not append:
                    self.db.execute('DELETE FROM body_chunks WHERE identity=?', (key,))
                for position in range(0, len(text), 4096):
                    chunk = text[position:position+4096]
                    self.db.execute('INSERT INTO body_chunks VALUES (?,?,?,?)',
                        (key, start+position, start+position+len(chunk), chunk))
                self.db.execute('INSERT OR REPLACE INTO bodies VALUES (?,?)', (key, start+len(text)))
                return
            offset, limit = max(0, offset), min(8192, max(0, limit))
            cursor = self.db.execute('SELECT start,body FROM body_chunks WHERE identity=? AND start<? AND end>? ORDER BY start',
                                     (key, offset+limit, offset))
            pieces = []
            for start, body in cursor:
                pieces.append(body[max(0, offset-start):min(len(body), offset+limit-start)])
            return ''.join(pieces)


class DetailWindow(deque):
    """Bound diagnostic previews by both entries and characters.

    Active approval keeps its complete review Document in the terminal; past
    request/public details use the existing lineage or paged body references.
    """
    def __init__(self):
        super().__init__(maxlen=100)
        self.character_limit = 262144
        self.characters = 0

    def append(self, value):
        value = str(value)
        if len(value) > 65536:
            value = value[:65500] + '\n[详情预览省略；请按来源引用读取原文]'
        while self and (len(self) == self.maxlen or self.characters + len(value) > self.character_limit):
            self.characters -= len(super().popleft())
        super().append(value)
        self.characters += len(value)

    def clear(self):
        super().clear()
        self.characters = 0


class WindowMap(MutableMapping):
    """Fixed number of resident records, with historical identity lookup."""
    def __init__(self, archive, name, limit=64):
        self.archive, self.name, self.limit = archive, name, limit
        self.cache = OrderedDict()
        self.count = 0

    @staticmethod
    def key(key):
        return json.dumps(encode(key), ensure_ascii=False)

    def trim(self):
        while len(self.cache) > self.limit:
            key, value = self.cache.popitem(last=False)
            self.archive.put(self.name, key, value)

    def __getitem__(self, key):
        encoded = self.key(key)
        if encoded not in self.cache:
            self.cache[encoded] = self.archive.get(self.name, encoded)
            self.trim()
        self.cache.move_to_end(encoded)
        return self.cache[encoded]

    def __setitem__(self, key, value):
        encoded = self.key(key)
        try:
            self[key]
        except KeyError:
            self.count += 1
        self.cache[encoded] = value
        self.cache.move_to_end(encoded)
        self.trim()

    def __delitem__(self, key):
        self[key]
        encoded = self.key(key)
        self.cache.pop(encoded, None)
        with self.archive.lock:
            self.archive.db.execute('DELETE FROM cache WHERE collection=? AND key=?', (self.name, encoded))
        self.count -= 1

    def __iter__(self):
        # Snapshot only the bounded resident keys. Database cursor streams.
        resident = set(self.cache)
        for key in self.archive.keys(self.name):
            if key not in resident:
                yield decode(json.loads(key))
        for key in resident:
            yield decode(json.loads(key))

    def __len__(self):
        return self.count

    def recent(self):
        return list(self.cache.values())


class WindowSet(MutableSet):
    def __init__(self, archive, name):
        self.values = WindowMap(archive, name, 256)
    def __contains__(self, value):
        return value in self.values
    def __iter__(self):
        return iter(self.values)
    def __len__(self):
        return len(self.values)
    def add(self, value):
        self.values[value] = True
    def discard(self, value):
        if value in self.values:
            del self.values[value]
    def update(self, values):
        for value in values:
            self.add(value)
    def clear(self):
        with self.values.archive.lock:
            self.values.archive.db.execute('DELETE FROM cache WHERE collection=?', (self.values.name,))
        self.values.cache.clear()
        self.values.count = 0


class SessionDedupe:
    """Lazy display-only deduplication for the existing OutputRouter."""
    def __init__(self):
        self.seen = None
    def __contains__(self, value):
        try:
            return self.seen is not None and value in self.seen
        except Exception:
            return False
    def add(self, value):
        try:
            if self.seen is None:
                self.seen = WindowSet(DisplayArchive(), 'router_seen')
            self.seen.add(value)
        except Exception:
            pass  # A disposable display cache cannot fail a business action.
    def update(self, values):
        for value in values:
            self.add(value)
    def clear(self):
        if self.seen is not None:
            try:
                self.seen.clear()
            except Exception:
                pass


class WindowSequence(Sequence):
    """Absolute-indexed sequence; slicing callers must specify a small batch."""
    def __init__(self, archive, name, limit=128):
        self.rows = WindowMap(archive, name, limit)
        self.length = 0
    def append(self, value):
        self.rows[self.length] = value
        self.length += 1
    def __len__(self):
        return self.length
    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self.length))]
        if index < 0:
            index += self.length
        if not 0 <= index < self.length:
            raise IndexError(index)
        return self.rows[index]
    def __setitem__(self, index, value):
        if index < 0:
            index += self.length
        if not 0 <= index < self.length:
            raise IndexError(index)
        self.rows[index] = value
    def __eq__(self, other):
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))
