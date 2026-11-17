"""CTA Metro data recovery tool (for administrators).

    python cta_recover.py status              what is wrong (if anything) and which backups exist
    python cta_recover.py verify              full integrity check of cta.db
    python cta_recover.py backup              make a checked backup of cta.db now
    python cta_recover.py restore [FILE]      put a backup back (default: the newest one). Close every CTA window first.
    python cta_recover.py migrate             copy history.json / accounts.json / staff.json into cta.db again
                                              (safe to repeat: existing records are never duplicated or overwritten)
    python cta_recover.py key-status          which ticket signing keys exist (ids only, never the secrets)
    python cta_recover.py rotate-key          add a new signing key; tickets printed with older keys keep working
    python cta_recover.py retire-keys         keep only the newest key (old printed QR codes then need reprinting)

Uses the same data folder as the app (CTA_DATA_DIR, otherwise Documents/CTA_Vouchers), or --folder PATH.
Nothing here deletes data: every file that is replaced is first copied into backups/.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

import cta_core as core
import cta_db
import cta_tickets
from cta_storage import StorageError, backup_dir, list_backups


def _print(*lines: str) -> None:
    for line in lines:
        print(line)


def status(folder: Path) -> int:
    _print(f'Data folder: {folder}')
    import cta_routing
    _provider, health = cta_routing.load_routing()
    _print(f'Routing: {health.label()} - {health.message}')
    backups = list_backups(backup_dir(folder), 'cta', '.db')
    try:
        store = cta_db.Store.open(folder, create=False)
    except StorageError as exc:
        _print(f'PROBLEM: {exc}')
        _print(f'{len(backups)} database backup(s) available.' + (f' Newest: {backups[0].name}' if backups else ''))
        return 1
    try:
        problems = store.verify()
        counts = store.counts()
        _print('Database: ' + ('read-only' if store.read_only else 'OK'),
               '  ' + ', '.join(f'{k} {v}' for k, v in counts.items()))
        for p in problems:
            _print(f'PROBLEM: {p}')
    finally:
        store.close()
    _print(f'{len(backups)} database backup(s).' + (f' Newest: {backups[0].name}' if backups else ''))
    return 1 if problems else 0


def verify(folder: Path) -> int:
    store = cta_db.Store.open(folder, create=False)
    try:
        problems = store.verify()
    finally:
        store.close()
    _print(*(problems or ['All checks passed.']))
    return 1 if problems else 0


def backup(folder: Path) -> int:
    store = cta_db.Store.open(folder, create=False)
    try:
        path = store.backup('manual')
    finally:
        store.close()
    _print(f'Backup written and checked: {path}')
    return 0


def restore(folder: Path, file: Optional[str], yes: bool) -> int:
    backups = list_backups(backup_dir(folder), 'cta', '.db')
    chosen = Path(file) if file else (backups[0] if backups else None)
    if chosen is None:
        _print('No backups found.')
        return 1
    _print(f'Restoring {chosen.name}. Close every CTA Metro window on every computer using this folder first.')
    if not yes and input('Type YES to continue: ').strip() != 'YES':
        _print('Cancelled. Nothing was changed.')
        return 1
    kept = cta_db.restore_backup(chosen, folder)
    _print('Restored.' + (f' The previous database was kept as {kept}' if kept else ''))
    return 0


def migrate(folder: Path) -> int:
    store = cta_db.Store.open(folder)
    try:
        report = cta_db.migrate_json(store, folder)
    finally:
        store.close()
    _print(report.summary())
    if report.backup_folder:
        _print(f'Original files copied to {report.backup_folder}')
    return 0 if report.verified and not report.conflicts else 1


def key_status(folder: Path) -> int:
    if cta_tickets.signer_from_env() is not None:
        _print('Signing key: taken from the CTA_TICKET_KEY environment variable (key file ignored).')
        return 0
    keys = cta_tickets.key_summary(folder)
    if not keys:
        _print('No ticket signing key file in this folder. One is created the first time the app starts on a new '
               'installation. If tickets were issued before, restore ticket_keys.json from your backup.')
        return 1
    for i, (kid, created) in enumerate(keys):
        _print(f"{'current' if i == 0 else 'older  '}  key {kid}  created {created}")
    _print('Back up ticket_keys.json separately from cta.db. Keep it private: anyone holding it can forge tickets.')
    return 0


def rotate_key(folder: Path, yes: bool) -> int:
    if not yes and input('Add a new signing key now? Type YES to continue: ').strip() != 'YES':
        _print('Cancelled. Nothing was changed.')
        return 1
    kid = cta_tickets.rotate_key(folder)
    _print(f'New signing key {kid} is now current. Restart every CTA window. Older keys still verify printed tickets.')
    return 0


def retire_keys(folder: Path, yes: bool) -> int:
    if not yes and input('Old keys will stop verifying tickets printed with them. Type YES to continue: ').strip() != 'YES':
        _print('Cancelled. Nothing was changed.')
        return 1
    _print(f'{cta_tickets.retire_old_keys(folder)} older key(s) removed.')
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='CTA Metro data recovery')
    ap.add_argument('command', choices=('status', 'verify', 'backup', 'restore', 'migrate', 'key-status', 'rotate-key', 'retire-keys'))
    ap.add_argument('file', nargs='?', help='backup file for restore (default: newest)')
    ap.add_argument('--folder', help='data folder (default: the app data folder)')
    ap.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    args = ap.parse_args(argv)
    folder = Path(args.folder) if args.folder else core.data_dir()
    try:
        if args.command == 'status':
            return status(folder)
        if args.command == 'verify':
            return verify(folder)
        if args.command == 'backup':
            return backup(folder)
        if args.command == 'restore':
            return restore(folder, args.file, args.yes)
        if args.command == 'key-status':
            return key_status(folder)
        if args.command == 'rotate-key':
            return rotate_key(folder, args.yes)
        if args.command == 'retire-keys':
            return retire_keys(folder, args.yes)
        return migrate(folder)
    except StorageError as exc:
        _print(f'PROBLEM: {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
