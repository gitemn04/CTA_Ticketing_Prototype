"""Tells you whether an administrator account exists and where your data folder is."""
import cta_core as core, cta_db, cta_auth as auth
folder = core.data_dir()
print('Data folder:', folder)
try:
    store = cta_db.Store.open(folder, create=False)
except Exception as exc:
    print('No database found yet (' + str(exc)[:80] + '). Start the app: it will ask you to create the first admin.')
else:
    if auth.StaffAuth(store).needs_bootstrap():
        print('NO admin exists yet. Start the app, click Admin, and it will let you CREATE one (you choose the name and password).')
    else:
        print('An admin account already exists. Its password cannot be shown. Reset it with:')
        print('    python cta_staff.py reset-password USERNAME')
