# CTA Metro staff permissions and audit

**Status of the role matrix: PROPOSED - awaiting the project owner's approval.** Until it is approved, only the `admin`
role can be given to an account, so nobody's access has changed: administrators can still do everything, as before.
What *has* changed is that every sensitive operation now checks, in the database service layer, that a real signed-in
staff member with that permission is asking.

## 1. Before this change

| | |
|---|---|
| Roles | one: `admin` (hard-coded when the account is created) |
| Accounts | the first person at a new installation creates the only admin; no way to add, disable or change accounts |
| Where rights were checked | the Admin tab was hidden until someone signed in. The functions behind it accepted any non-empty name, so `cancel_ticket(no, "anyone", now)` worked without signing in, and an edited booking could be saved directly. |
| Lockout | 5 wrong passwords -> 30 s, counted inside one sign-in window only (reopening the window reset it) |
| Passwords | salted PBKDF2-HMAC-SHA256, 200,000 iterations |
| Audit | bookings, ticket actions and payments were logged; sign-ins, refusals and staff changes were not |

## 2. Permissions (what is checked)

| Permission | Operation it guards |
|---|---|
| `ticket.check_in` | check a ticket in (scan or typed number) |
| `ticket.reentry` | give back one use of a used ticket |
| `ticket.cancel` | cancel a ticket or every unused journey of a booking |
| `ticket.refund` | record that a refund was paid out |
| `ticket.correct_name` | correct a misspelt passenger name |
| `payment.mark_paid` | record cash received at the counter (the only staff change to a payment's status) |
| `report.view` | open the admin portal (all bookings, passenger phone numbers, statistics) |
| `report.export` | export the ticket list as CSV (contains phone numbers) |
| `audit.read` | read the audit log |
| `audit.export` | export the audit log |
| `support.reply` | read passenger messages, reply to them, and decide passenger refund requests (approving also needs `ticket.cancel`) |
| `staff.manage` | create, disable or re-enable staff, change roles, reset passwords |
| `records.edit` | edit a stored booking directly (data repair) |

Not runtime permissions, by design: **fare rules** change only in code, with approval, and the fare matrix lock stops
sales if code and matrix disagree (see `FARE_POLICY.md`). **Settings, signing keys, backups and restores** are done with
command-line tools on the computer that holds the data, so they are protected by who can log on to that computer and
open the data folder. The app writes an audit event whenever it notices the fare policy, signing keys or settings file
changed since it last started.

## 3. Proposed role matrix - please approve or amend

| Permission | admin | supervisor | cashier | gate |
|---|:-:|:-:|:-:|:-:|
| `ticket.check_in` | yes | yes | yes | yes |
| `ticket.reentry` | yes | yes | | |
| `ticket.cancel` | yes | yes | | |
| `ticket.refund` | yes | yes | | |
| `ticket.correct_name` | yes | yes | | |
| `payment.mark_paid` | yes | yes | yes | |
| `report.view` | yes | yes | yes | |
| `report.export` | yes | yes | | |
| `audit.read` | yes | yes | | |
| `audit.export` | yes | | | |
| `support.reply` | yes | | | |
| `staff.manage` | yes | | | |
| `records.edit` | yes | | | |

Questions for the owner:
0. `support.reply` was added with the passenger-messages feature and is given to `admin` only, because nothing may be assumed
   for the other roles. Should supervisors answer passengers too?
1. Are these four roles the right ones (gate / ticketing staff, counter cashier, station supervisor, administrator)?
2. Should cashiers be able to cancel and refund at the counter, or only supervisors (proposed: only supervisors, so the
   person handing money back is not the only person deciding it)?
3. Should cashiers see the admin portal with passenger phone numbers (`report.view`), or only a narrower counter view?
4. Lockout: keep 5 wrong passwords -> 30 s (unchanged), or something stricter?
5. Session length: 12 hours (one shift) - is that right?

To approve: set `MATRIX_STATUS = 'approved'` and `ASSIGNABLE_ROLES` to the approved roles in `cta_auth.py`, update this
file, and record who approved it and when. Accounts are then created with `python cta_staff.py add <name> --role ...`.

## 4. How it is enforced

* Sign-in (`cta_auth.StaffAuth` / `Store.staff_login`) returns a `Principal` holding a random session token. The database
  keeps only a SHA-256 hash of it. Every guarded operation re-checks, inside its own transaction: the session exists and
  has not ended or expired, the account still exists and is enabled, and the account's **current** role (from the
  database, not from the `Principal`) has the permission. A username string, a dictionary, a made-up `Principal`, a
  signed-out session or someone else's token is refused, and the refusal is audited.
* Disabling an account, resetting its password or signing out ends its sessions at once; a role change applies to the
  very next operation.
* Saving bookings directly (`save_history`) can only add bookings in their just-booked state (unused, nothing cancelled,
  cash not received). Any change to a stored booking needs `records.edit`. Staff work goes through the dedicated
  operations. Writing the staff list directly is refused altogether.
* Lockout: 5 wrong passwords for one username lock it for 30 s, stored in the database, so it applies to every window
  and computer. Unknown usernames are counted the same way and hashed the same way, so neither the message nor the timing
  reveals whether a username exists.
* Passwords: PBKDF2-HMAC-SHA256, **600,000 iterations** (raised from 200,000, OWASP 2023), 16-byte random salt,
  constant-time comparison, 8-128 characters with a letter and a digit. Older hashes are re-hashed at the next correct
  sign-in. A stored hash asking for an absurd iteration count is refused instead of obeyed.
* Passenger sign-in codes are unchanged: 6 digits, 5 minutes, single use, 3 attempts (or Twilio Verify's own rules).
* The last active administrator cannot be disabled or demoted, and nobody can disable their own account.

## 5. The audit log

Every row has: an event id (`AUD-00000042`), timestamp, actor (staff username, or a masked phone number for passengers),
action, target (ticket, booking or username), outcome (`success`, `failure`, `denied`, `locked`, `disabled`), the
booking reference where relevant, and a small detail record.

Events: staff sign-in (success / wrong password / locked / disabled) and sign-out, passenger sign-in (outcome only),
every permission refusal, bookings created or directly edited, cash recorded as paid (old and new status, amount),
cancellations, refunds, re-entry, name corrections, check-in results (in the separate scan log), staff created /
disabled / enabled / role changed / password reset, first-admin creation, audit log read and exported, ticket list
exported (row count only), fare policy / signing keys / settings / environment changes, data migrations and restores.

* **Never logged:** passwords, sign-in codes, session tokens, recovery codes, signing keys. Anything in the detail whose
  name looks like a secret (password, code, token, PIN, key, ...) is removed before writing.
* **Minimal personal data:** no passenger names; passenger phones only masked; staff usernames are needed for
  accountability.
* **Cannot be changed:** database triggers block every update and delete of audit (and scan) rows, for every user of the
  app, administrators included. Each row also carries a hash of the previous row, so if someone with direct file access
  removes the triggers and edits or deletes a row, `python cta_recover.py verify` reports exactly where. Deleting the
  very last rows cannot be detected this way; an off-site copy of the log would be needed for that.
* **Who may read it:** `audit.read` (admin, supervisor) and `audit.export` (admin, `python cta_staff.py audit`).
  Reading and exporting are themselves logged.
* The business change and its audit row are written in one transaction: either both are saved or neither is.

## 6. Limits

* The app is a desktop program and loads all bookings into memory to work, so a determined user of the same computer
  account can read data outside the app; permissions protect the operations and the record, not the files. Keep the data
  folder readable only by the staff account that runs the app.
* There is no two-factor sign-in for staff.
* A provider callback (once a real payment provider exists) will need its own system identity with a narrow permission;
  none is defined yet because no provider is connected.
