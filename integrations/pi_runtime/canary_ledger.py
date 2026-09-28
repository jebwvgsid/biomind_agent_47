#!/usr/bin/env python3
"""Durable, transactional reservation ledger for the authorized migration canary.

This is test-send accounting, not an agent scheduler or scientific job policy.
There is deliberately no reset, refund, delete or configurable-limit operation.
No credentials, prompts, headers or response bodies enter this database.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit

TASK_ID = 'BioMind-Pi-20260926'
SCENARIOS = ('R1', 'R2', 'R3')
PER_SCENARIO = 6
TOTAL = 18


class LedgerError(Exception):
    pass


def small(value, name, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise LedgerError('INVALID_' + name.upper())
    return value


def usage_value(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LedgerError('INVALID_USAGE')
    result = {}
    for key in ('input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens'):
        item = value.get(key)
        if not isinstance(item, (int, float)) or isinstance(item, bool) or item < 0 or item != item or item == float('inf'):
            raise LedgerError('INVALID_USAGE_' + key)
        result[key] = item
    # No local pricing guess. Preserve only cost explicitly supplied by caller.
    cost = value.get('cost')
    if isinstance(cost, dict):
        result['cost'] = {key: number for key, number in cost.items()
                          if key in ('input', 'output', 'cacheRead', 'cacheWrite', 'total')
                          and isinstance(number, (int, float)) and not isinstance(number, bool)
                          and 0 <= number < float('inf')}
    return result


def schema(db, task_id):
    db.execute('CREATE TABLE IF NOT EXISTS config (singleton INTEGER PRIMARY KEY CHECK(singleton=1), task_id TEXT NOT NULL, per_scenario INTEGER NOT NULL, total INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS sends (id TEXT PRIMARY KEY, scenario TEXT NOT NULL, provider_attempt_id TEXT NOT NULL, reserved_at REAL NOT NULL, metadata TEXT NOT NULL, outcome TEXT NOT NULL, http_status INTEGER, reported_usage TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS provider_results (id TEXT PRIMARY KEY, scenario TEXT NOT NULL, result TEXT NOT NULL, recorded_at REAL NOT NULL)')
    db.execute('INSERT OR IGNORE INTO config VALUES (1,?,?,?)', (task_id, PER_SCENARIO, TOTAL))
    expected = (task_id, PER_SCENARIO, TOTAL)
    if tuple(db.execute('SELECT task_id,per_scenario,total FROM config WHERE singleton=1').fetchone()) != expected:
        raise LedgerError('BUDGET_IDENTITY_OR_LIMIT_MISMATCH')


def snapshot(db):
    rows = db.execute('SELECT * FROM sends ORDER BY reserved_at,id').fetchall()
    provider_rows = db.execute('SELECT * FROM provider_results ORDER BY recorded_at,id').fetchall()
    results = {row['id']: json.loads(row['result']) for row in provider_rows}
    attempts = []
    totals = {key: 0 for key in ('input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens')}
    unknown = 0
    # Provider terminal usage describes its last physical response, never add
    # it again to that response's already observed usage.
    terminal_by_send = {}
    for result in results.values():
        ids = result.get('physicalAttemptIds', [])
        if ids and result.get('usage') is not None:
            terminal_by_send[ids[-1]] = result['usage']
    for row in rows:
        physical = json.loads(row['reported_usage']) if row['reported_usage'] else None
        usage = terminal_by_send.get(row['id'], physical)
        if usage is None:
            unknown += 1
        else:
            for key in totals:
                totals[key] += usage[key]
        attempts.append({'attempt_id': row['id'], 'scenario': row['scenario'],
                         'provider_attempt_id': row['provider_attempt_id'], 'reserved_at': row['reserved_at'],
                         'metadata': json.loads(row['metadata']), 'outcome': row['outcome'],
                         'http_status': row['http_status'], 'usage': usage,
                         'usage_source': 'provider_terminal' if row['id'] in terminal_by_send else 'physical_response' if physical else 'unknown'})
    counts = {scenario: sum(row['scenario'] == scenario for row in rows) for scenario in SCENARIOS}
    return {'task_id': db.execute('SELECT task_id FROM config').fetchone()[0],
            'limits': {'per_scenario': PER_SCENARIO, 'total': TOTAL},
            'reserved_sends': len(rows), 'counts': counts, 'attempts': attempts,
            'provider_results': list(results.values()), 'known_reported_usage': totals,
            'unknown_usage_sends': unknown, 'remaining_total': TOTAL - len(rows),
            'usage_complete': unknown == 0,
            'reservation_policy': 'Reserved/uncertain/cancelled sends consume quota; no refunds.'}


def execute(path: Path, request: dict):
    action = request.get('action')
    if action not in ('init', 'reserve', 'send_result', 'provider_result', 'summary'):
        raise LedgerError('UNKNOWN_ACTION')
    task_id = small(request.get('taskId', TASK_ID), 'task_id', True)
    scenario = request.get('scenario')
    if action in ('reserve', 'send_result', 'provider_result') and scenario not in SCENARIOS:
        raise LedgerError('UNKNOWN_SCENARIO')
    path = path.absolute()
    if path.is_symlink():
        raise LedgerError('LEDGER_SYMLINK_REFUSED')
    if action != 'init' and not path.is_file():
        raise LedgerError('LEDGER_NOT_INITIALIZED')
    if action == 'init':
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        schema(db, task_id)
        info = request.get('info', {})
        if action == 'reserve':
            attempt = small(info.get('attemptId'), 'attempt_id', True)
            provider_attempt = small(info.get('providerAttemptId'), 'provider_attempt_id', True)
            if db.execute('SELECT 1 FROM sends WHERE id=?', (attempt,)).fetchone():
                # Reusing an ID must never buy a second real send for free.
                raise LedgerError('DUPLICATE_PHYSICAL_SEND_ID')
            total, count = db.execute('SELECT count(*),coalesce(sum(scenario=?),0) FROM sends', (scenario,)).fetchone()
            if total >= TOTAL:
                raise LedgerError('CANARY_TOTAL_BUDGET_EXHAUSTED')
            if count >= PER_SCENARIO:
                raise LedgerError('CANARY_SCENARIO_BUDGET_EXHAUSTED')
            metadata = {key: small(info.get(key), key) for key in
                        ('sessionId', 'requestId', 'operation', 'provider', 'model', 'api', 'transport')}
            dest = info.get('destination') or {}
            if dest:
                url = urlsplit(str(dest.get('origin', '')))
                if url.scheme not in ('http', 'https') or not url.hostname:
                    raise LedgerError('INVALID_DESTINATION')
                # Origin only. Even paths can carry secrets on some providers.
                metadata['destination_origin'] = url.scheme + '://' + url.hostname + (':' + str(url.port) if url.port else '')
            db.execute('INSERT INTO sends VALUES (?,?,?,?,?,?,?,?)',
                       (attempt, scenario, provider_attempt, time.time(), json.dumps(metadata), 'reserved_unknown', None, None))
        elif action == 'send_result':
            attempt = small(info.get('attemptId'), 'attempt_id', True)
            row = db.execute('SELECT * FROM sends WHERE id=? AND scenario=?', (attempt, scenario)).fetchone()
            if not row:
                raise LedgerError('UNRESERVED_SEND_RESULT')
            outcome = info.get('outcome')
            if outcome not in ('response', 'error', 'aborted'):
                raise LedgerError('INVALID_SEND_OUTCOME')
            status = info.get('status')
            if status is not None and (not isinstance(status, int) or not 100 <= status <= 599):
                raise LedgerError('INVALID_HTTP_STATUS')
            usage = usage_value(info.get('usage'))
            if row['outcome'] != 'reserved_unknown' and (row['outcome'] != outcome or row['http_status'] != status):
                raise LedgerError('CONFLICTING_SEND_RESULT')
            if row['reported_usage'] and usage is not None and json.loads(row['reported_usage']) != usage:
                raise LedgerError('CONFLICTING_SEND_USAGE')
            db.execute('UPDATE sends SET outcome=?,http_status=?,reported_usage=coalesce(?,reported_usage) WHERE id=?',
                       (outcome, status, json.dumps(usage) if usage is not None else None, attempt))
        elif action == 'provider_result':
            provider_attempt = small(info.get('providerAttemptId'), 'provider_attempt_id', True)
            ids = info.get('physicalAttemptIds', [])
            if not isinstance(ids, list) or len(set(ids)) != len(ids):
                raise LedgerError('INVALID_PHYSICAL_IDS')
            actual = {r[0] for r in db.execute('SELECT id FROM sends WHERE provider_attempt_id=? AND scenario=?', (provider_attempt, scenario))}
            if set(ids) != actual:
                raise LedgerError('PROVIDER_SEND_ATTRIBUTION_MISMATCH')
            result = {key: small(info.get(key), key) for key in
                      ('sessionId', 'requestId', 'operation', 'provider', 'model', 'api', 'stopReason')}
            result.update(providerAttemptId=provider_attempt, physicalAttemptIds=ids, usage=usage_value(info.get('usage')))
            old = db.execute('SELECT result FROM provider_results WHERE id=?', (provider_attempt,)).fetchone()
            if old and json.loads(old[0]) != result:
                raise LedgerError('CONFLICTING_PROVIDER_RESULT')
            db.execute('INSERT OR IGNORE INTO provider_results VALUES (?,?,?,?)', (provider_attempt, scenario, json.dumps(result), time.time()))
        result = snapshot(db)
        db.execute('COMMIT')
        return result
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def main():
    try:
        if len(sys.argv) != 2:
            raise LedgerError('EXPECTED_LEDGER_PATH')
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise LedgerError('OVERSIZED_LEDGER_REQUEST')
        result = execute(Path(sys.argv[1]), json.loads(raw))
        print(json.dumps({'ok': True, 'summary': result}, ensure_ascii=False))
        return 0
    except Exception as error:
        # No paths, command payloads, secrets or exception traceback exported.
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, LedgerError) else type(error).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
