#!/usr/bin/env python3
"""Restore a stopped service's exact pre-rebind files using its APPLIED receipt.

Check-only unless --apply is explicit. Never starts/stops/signals a service,
reads credentials, changes sessions or removes the newly selected runtime.
"""
import argparse
import datetime
import importlib.util
import json
import os
from pathlib import Path
import uuid

spec = importlib.util.spec_from_file_location('rebind_managed', Path(__file__).with_name('rebind-managed-services.py'))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def execute(args):
    receipt = r.snapshot(args.receipt, private=True)
    doc = receipt['value']
    r.require(doc.get('schema') == 'BioMindManagedServiceRebindV1' and doc.get('status') == 'APPLIED', 'APPLIED_REBIND_RECEIPT_REQUIRED')
    current = r.snapshot(doc.get('serviceConfig'), private=True)
    r.reject_secrets(current['value'])
    r.require(current['sha256'] == doc.get('serviceConfigAfterSha256'), 'CURRENT_CONFIG_CHANGED')
    private = current['path'].parent
    state = r.absolute(current['value'].get('stateRoot'), directory=True, private=True)
    r.require(state == private / 'state', 'CONFIGURATION_LAYOUT_CHANGE_REFUSED')
    backup = r.absolute(doc.get('backupDirectory'), directory=True, private=True)
    r.require(backup.parent == private and backup.name.startswith('rebind-backup-'), 'BACKUP_LAYOUT_REFUSED')
    old_config = r.snapshot(backup / 'config.json', private=True)
    old_runtime = r.snapshot(backup / 'runtime.json', private=True)
    old_lifecycle = r.snapshot(backup / 'lifecycle.json', private=True)
    r.reject_secrets(old_config['value'])
    r.reject_secrets(old_runtime['value'])
    r.require(old_config['sha256'] == doc.get('serviceConfigBeforeSha256')
              and old_runtime['sha256'] == doc.get('runtimeBeforeSha256'), 'BACKUP_HASH_MISMATCH')
    r.require(old_lifecycle['value'].get('status') == 'stopped', 'BACKUP_NOT_STOPPED')
    for name in ['schema', 'serviceId', 'serverId', 'stateRoot', 'environmentFile']:
        r.require(current['value'].get(name) == old_config['value'].get(name), 'PRESERVED_IDENTITY_CHANGED')
    for name in ['serviceId', 'serverId']:
        r.require(current['value'].get(name) == doc.get(name) == old_lifecycle['value'].get(name), 'RECEIPT_IDENTITY_MISMATCH')
    # The original runtime was retained by rebind. Refuse if it changed later.
    original_runtime = r.snapshot(old_config['value']['environment']['BIOMIND_RUNTIME_CONFIG'], private=True)
    r.require(original_runtime['sha256'] == old_runtime['sha256'], 'ORIGINAL_RUNTIME_CHANGED')
    lifecycle_path = state / 'service.json'
    lifecycle = r.snapshot(lifecycle_path, private=True) if os.path.lexists(lifecycle_path) else None
    if lifecycle:
        r.require(lifecycle['value'].get('status') == 'stopped', 'SERVICE_NOT_STOPPED')
        r.require(all(lifecycle['value'].get(k) == doc[k] for k in ['serviceId', 'serverId']), 'LIFECYCLE_IDENTITY_MISMATCH')
    output = r.fresh_output(args.output_receipt)
    r.require(not output.is_relative_to(private) and output != receipt['path'], 'RECEIPT_LOCATION_REFUSED')
    fds = []
    try:
        for lock in [private / 'configure.lock', state / 'service.lock', state / 'backend.lock']:
            r.absolute(lock, regular=True, private=True)
            fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
            fds.append(fd)
            actual, expected = os.fstat(fd), lock.stat()
            r.require((actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino), 'LOCK_IDENTITY_CHANGED')
            try:
                r.fcntl.flock(fd, r.fcntl.LOCK_EX | r.fcntl.LOCK_NB)
            except BlockingIOError:
                raise r.Refused('SERVICE_ACTIVE_CONFIGURATION_LOCKED') from None
        for snap in [receipt, current, old_config, old_runtime, old_lifecycle, original_runtime]:
            r.unchanged(snap)
        if lifecycle:
            r.unchanged(lifecycle)
        else:
            r.require(not os.path.lexists(lifecycle_path), 'LIFECYCLE_APPEARED')
        r.require(not os.path.lexists(output), 'OUTPUT_ALREADY_EXISTS')
        result = {'schema': 'BioMindManagedServiceRollbackV1',
                  'status': 'ROLLED_BACK' if args.apply else 'CHECK_ONLY_READY',
                  'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'serviceConfig': str(current['path']), 'sourceRebindReceipt': str(receipt['path']),
                  'restoredConfigSha256': old_config['sha256'], 'previousConfigSha256': current['sha256'],
                  'credentialContentsRead': False, 'serviceStarted': False, 'signalsSent': False,
                  'sessionsChanged': False, 'newRuntimePreserved': doc.get('runtimeOutput')}
        if args.apply:
            journal = private / ('rollback-backup-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12])
            journal.mkdir(mode=0o700)
            r.write_new(journal / 'config.json', current['raw'])
            if lifecycle:
                r.write_new(journal / 'lifecycle.json', lifecycle['raw'])
            r.fsync_dir(journal)
            r.unchanged(current)
            if lifecycle:
                r.unchanged(lifecycle)
                os.replace(lifecycle_path, journal / 'retired-lifecycle.json')
            else:
                r.require(not os.path.lexists(lifecycle_path), 'LIFECYCLE_APPEARED')
            # Publish old stopped binding before its config, so a failure in
            # between remains configuration-changed and cannot launch an owner.
            r.atomic_create(lifecycle_path, old_lifecycle['raw'])
            r.atomic_replace(current['path'], old_config['raw'])
            for folder in [state, private, journal]:
                r.fsync_dir(folder)
            result['rollbackBackupDirectory'] = str(journal)
        r.atomic_create(output, r.encoded(result))
        r.fsync_dir(output.parent)
        return result
    finally:
        for fd in reversed(fds):
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--output-receipt', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = execute(args)
    except r.Refused as error:
        print(json.dumps({'status': 'BLOCKED', 'code': str(error), 'serviceStarted': False, 'signalsSent': False}))
        return 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'BLOCKED', 'code': 'ROLLBACK_IO_OR_METADATA_ERROR',
                          'errorType': type(error).__name__, 'errno': getattr(error, 'errno', None),
                          'outcome': 'CHECK_CONFIG_AND_PRIVATE_BACKUP_BEFORE_RETRY',
                          'serviceStarted': False, 'signalsSent': False}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
