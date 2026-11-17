"""CTA Metro staff administration (for administrators).

You sign in with your own staff username and password first; every command is checked against your role and written
to the audit log, exactly as in the app.

    python cta_staff.py list
    python cta_staff.py add <username> [--role admin]
    python cta_staff.py disable <username>        (also signs them out everywhere at once)
    python cta_staff.py enable <username>
    python cta_staff.py role <username> <role>
    python cta_staff.py reset-password <username>  (also signs them out everywhere)
    python cta_staff.py audit [--out FILE]         export the audit log (needs 'audit.export')

Options: --folder PATH (data folder; default as the app), --as USERNAME (who you are; asked if missing).
Roles other than 'admin' are part of a proposed permission matrix and are refused until it is approved
(see docs/PERMISSIONS.md).
"""
from __future__ import annotations

import argparse
import getpass
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import cta_auth as auth
import cta_core as core
import cta_db
from cta_storage import StorageError


def _new_password(ask=getpass.getpass) -> str:
    first = ask('New password: ')
    if ask('Repeat the new password: ') != first:
        raise ValueError('The two passwords are not the same.')
    return first


def main(argv: Optional[List[str]] = None, ask=getpass.getpass, out=print) -> int:
    ap = argparse.ArgumentParser(description='CTA Metro staff administration')
    ap.add_argument('command', choices=('list', 'add', 'disable', 'enable', 'role', 'reset-password', 'audit'))
    ap.add_argument('username', nargs='?')
    ap.add_argument('new_role', nargs='?')
    ap.add_argument('--role', default='admin')
    ap.add_argument('--as', dest='me')
    ap.add_argument('--folder')
    ap.add_argument('--out')
    args = ap.parse_args(argv)
    folder = Path(args.folder) if args.folder else core.data_dir()
    try:
        store = cta_db.Store.open(folder, create=False)
    except StorageError as exc:
        out(f'PROBLEM: {exc}')
        return 1
    device = ('cli:' + socket.gethostname())[:40]
    staff = auth.StaffAuth(store)
    principal = None
    try:
        me = args.me or input('Your staff username: ')
        principal = staff.login(me, ask('Your password: '), datetime.now(), device)
        now = datetime.now()
        if args.command == 'list':
            for row in staff.list_staff(principal, now):
                out(f"{row['username']:<30} {row['role']:<12} {'DISABLED' if row['disabled'] else 'active'}")
        elif args.command == 'audit':
            path = Path(args.out or f'cta_audit_{now:%Y%m%d_%H%M%S}.csv')
            n = store.export_audit_csv(principal, path, now)
            out(f'Exported {n} audit records to {path}. The file contains staff names; keep it private.')
        else:
            if not args.username:
                out('Give the username of the account to change.')
                return 2
            if args.command in ('add', 'reset-password'):
                store.authorize(principal, 'staff.manage', now, target=args.username)   # refuse before asking
            if args.command == 'add':
                rec = staff.create_staff(principal, args.username, _new_password(ask), args.role, now)
                out(f"Created {rec['username']} ({rec['role']}).")
            elif args.command == 'disable':
                out(staff.set_enabled(principal, args.username, False, now))
            elif args.command == 'enable':
                out(staff.set_enabled(principal, args.username, True, now))
            elif args.command == 'role':
                out(staff.set_role(principal, args.username, args.new_role or '', now))
            else:
                out(staff.reset_password(principal, args.username, _new_password(ask), now))
        return 0
    except (auth.AuthError, auth.PermissionDenied, ValueError, StorageError) as exc:
        out(f'REFUSED: {exc}')
        return 1
    finally:
        # Always end the sign-in, including after a refusal: a CLI run must not leave a usable session behind
        # in the database for the rest of the shift.
        try:
            if principal is not None:
                staff.logout(principal, datetime.now())
        except StorageError as exc:
            out(f'WARNING: your sign-in could not be closed ({exc}). It ends by itself within '
                f'{auth.SESSION_HOURS} hours.')
        store.close()


if __name__ == '__main__':
    sys.exit(main())
