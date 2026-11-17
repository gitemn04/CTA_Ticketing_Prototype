# CTA Metro v3.0 — Deployment Guide

## 1. Running the desktop application (Windows)

```
cd CTA_Metro_App_v3
pip install -r requirements.txt
python cta_app.py
```

Or double-click **launch.bat** (see below).

---

## 2. Setting up real SMS (Twilio)

Run the interactive setup script once:

```
python setup_sms.py
```

This writes **cta_config.json** (in the same directory, excluded from git).
If a `cta_config.json` already exists, the script updates only the `sms` section
and preserves everything else. You will need:

- **Twilio Account SID** — starts with `AC`
- **Twilio Auth Token**
- **Twilio Verify Service SID** — starts with `VA`

After setup, set the environment variable to enable production mode:

```
set CTA_ENV=production          # Windows cmd
$env:CTA_ENV = "production"     # PowerShell
```

In production mode the app **blocks sign-in** if Twilio is unavailable instead of falling back to on-screen codes.

---

## 2b. WhatsApp verification (your own sender)

1. Copy `cta_whatsapp_example.py` to e.g. `my_whatsapp.py` (same folder as `cta_app.py`) and put your WhatsApp
   API call in `send_code(phone, code)`. Keep tokens in environment variables, not in the file.
2. In `cta_config.json`:
   ```json
   "sms": { "provider": "custom", "module": "my_whatsapp" }
   ```
3. Restart the app. If the file is missing or has an error, sign-in shows the reason instead of crashing.

Using Twilio instead? Keep the Twilio settings and set `"channel": "whatsapp"`.

## 2c. Test payments

Add `"environment": "development"` to `cta_config.json`. Card, mada, STC Pay, PayPal, Tabby and Tamara then open a
test form: type made-up details (CVV 3 digits, expiry in the future). Card ending 0000 = "not enough money".
No money moves and the header says DEVELOPMENT MODE.

## 3. Running the ticket server (`cta_ticket_server.py`)

The ticket server is a small Flask app that serves mobile-friendly ticket pages.
It is **optional** — the desktop app works without it, but QR codes still work: any phone camera shows the ticket details as text, and staff scanners check the ticket in. The server adds a live web page (current status) when a phone scans the code.

### Start locally

```
python cta_ticket_server.py --db cta.db --port 5050
```

### Public ticket URL (QR links)

On startup the server (and `launch_ticket_server.bat`, which runs it) reads the public
HTTPS base URL from `cta_config.json` next to `cta.db`:

```json
{ "ticket_server": { "url": "https://your-domain.com" } }
```

* Override for one run with `--url https://...` (also accepted by the `.bat`) or the
  `CTA_TICKET_SERVER_URL` environment variable. Precedence: `--url`, then the
  environment variable, then `cta_config.json`.
* The URL is validated (HTTPS, real public hostname). A configured but invalid URL, or a
  damaged `cta_config.json`, makes the server refuse to start (exit code 2 for a bad URL).
* With no URL configured the server still starts, but downloaded PDFs carry no
  ticket-link QR. The request `Host` header is never used to build QR URLs.
* `python cta_ticket_server.py --check` validates the configuration and exits.
* Validation checks the URL's form only; it does not prove the domain is deployed or
  reachable. Confirm that separately on your real host.

> ⚠️ **A localhost URL will NOT work from a phone camera.**
> If you start the server on `127.0.0.1`, the server itself will print a warning.

> ⚠️ **The `--db` flag must point at the same `cta.db` file the desktop app uses.**
> Copying the database does not keep it synchronised. The server reads live from
> this file; new tickets only appear on the web if they are in the same file.

> ℹ️ **The desktop app does NOT automatically start the ticket server.**
> You must start `cta_ticket_server.py` manually (or set it up as a system
> service). The desktop app works without the server; the difference is that
> QR codes in exported PDFs will open a ticket page on a phone only when the
> server is running and reachable via a public HTTPS URL.

---

## 4. Making the ticket server reachable from a phone

To scan the QR code with a phone camera you need a **public HTTPS URL**.
Choose one of the options below.

### Option A — Cloudflare Tunnel (free, no account needed for testing)

```
# Install cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cloudflared tunnel --url http://localhost:5050
```

Cloudflare prints a URL like `https://random-name.trycloudflare.com`.
Copy it (without trailing slash) into `cta_config.json`:

```json
{
  "ticket_server": {
    "url": "https://random-name.trycloudflare.com"
  }
}
```

> ⚠️ Cloudflare Tunnel URLs change each time you restart the tunnel.
> Each PDF re-export after a URL change will produce new QR codes.

### Option B — ngrok (free tier)

```
# Install ngrok: https://ngrok.com/download
ngrok http 5050
```

ngrok prints a URL like `https://abcd1234.ngrok-free.app`.
Add it to `cta_config.json` as above.

> ⚠️ Free ngrok URLs also change on restart.

### Option C — Production hosting (permanent URL)

For a permanent URL, deploy `cta_ticket_server.py` to any server or
platform-as-a-service that supports Python and HTTPS:

- **VPS (DigitalOcean, Linode, AWS EC2, etc.)**: run the server behind
  nginx or Caddy as a reverse proxy with Let's Encrypt TLS.
- **Railway / Render / Fly.io**: push the `cta_ticket_server.py` as the
  entry point. Flask listens on `0.0.0.0:$PORT`.

The server exposes:
- `GET /ticket/<token>` — mobile-friendly HTML ticket page
- `GET /ticket/<token>/pdf` — download PDF for this ticket
- `GET /health` — health check (returns `{"status": "ok"}`)

---

## 5. Wiring the ticket server URL into the desktop app

Edit (or create) **cta_config.json** in the same folder as `cta_app.py`:

```json
{
  "ticket_server": {
    "url": "https://your-domain.com"
  }
}
```

No trailing slash. Leave `url` blank or omit the key to use signed-token
QR codes only (staff-scanner mode, no phone camera link).

When the app exports a ticket PDF with a valid server URL configured, each
ticket's QR code embeds the full HTTPS URL. The PDF contains a note that
passengers can scan the QR code to view their ticket on their phone.

---

## 6. Real-device test checklist

Before going live, verify the following manually:

### SMS OTP (one outgoing SMS to a real Saudi number)

- [ ] Set `CTA_ENV=production`
- [ ] Run `python setup_sms.py` with real Twilio credentials
- [ ] Create a test account with a real Saudi number (`05XXXXXXXX` format)
- [ ] Trigger "Sign in / Verify phone" in the app
- [ ] Confirm SMS arrives on the phone within ~30 seconds
- [ ] Enter the code — confirm sign-in succeeds
- [ ] Enter a wrong code — confirm error message appears
- [ ] Let the code expire (5 minutes) — confirm expiry message appears
- [ ] Check Twilio console to confirm one verification was sent (no extras)
- [ ] **Confirm the code was never displayed on screen or in logs**

### QR code / phone camera scan

- [ ] Start `cta_ticket_server.py` with a real DB
- [ ] Set up a public HTTPS URL (Cloudflare Tunnel or ngrok)
- [ ] Add the URL to `cta_config.json`
- [ ] Export a ticket PDF from the app
- [ ] Open the PDF and scan the QR code with a phone camera
- [ ] Confirm the ticket page loads on the phone (HTTPS, no warnings)
- [ ] Confirm the page shows the correct passenger name, route, date, status
- [ ] Confirm no check-in occurred (ticket status still "issued" in the app)
- [ ] Revoke the token (`revoke_web_token()` in Python or via a future admin UI)
- [ ] Re-scan — confirm the page shows "cancelled or refunded" banner

---

## 7. Feature status table

| Feature | Implemented | Automated test result | Real-device result | Remaining setup / blocker |
|---|---|---|---|---|
| Saudi phone normalisation | ✅ | 12 tests pass | — | None |
| Production SMS block (env var) | ✅ | Pass | — | Set `CTA_ENV=production` |
| Production SMS block (config file) | ✅ | Pass (Bug 3 fix) | — | `{"environment":"production"}` in config |
| Real SMS via Twilio | ✅ (code) | Mocked only | **Needs real test** | Run `setup_sms.py` with credentials |
| OTP code expiry (Twilio default) | ✅ | — | — | **Twilio Verify default is 10 min, not 15.** A 15-min expiry requires provider-level configuration via Twilio support |
| Personalised OTP message | ⚠️ | — | — | Requires an **approved Twilio Verify template** (not enabled by default). Contact Twilio to activate custom message wording |
| OTP single-use / expiry | ✅ | 6 tests pass | — | None |
| OTP attempt limit | ✅ | 1 test passes | — | None |
| No OTP leakage | ✅ | 3 tests pass | — | None |
| Ticket web token generation | ✅ | Pass | — | None |
| Ticket server DB integration | ✅ | Real DB tests pass (Bug 1 fix) | — | None |
| PDF download correct URL | ✅ | Pass (Bug 2 fix) | — | None |
| Token revocation blocks page + PDF | ✅ | Pass (Bug 7 fix) | — | None |
| Phone-scannable QR (on-screen) | ✅ | Pass (Bug 5 fix) | **Needs phone scan** | Deploy + HTTPS URL |
| Staff scanner URL-token support | ✅ | Pass (Bug 6 fix) | — | None |
| Viewing ≠ check-in | ✅ | Pass | — | None |
| Simulated payment labels | ✅ | Existing tests | — | None |
| Refund states | ✅ | Pass | — | None |
| PDF with working QR | ✅ | Pass (Bug 2 fix) | **Needs print** | Deploy + HTTPS URL |

**Outstanding external blockers (cannot be resolved in code alone):**
- **Real Saudi SMS delivery** — requires real Twilio credentials + phone with Saudi SIM
- **15-minute OTP expiry** — requires Twilio Verify service-level configuration (contact Twilio support)
- **Personalised OTP wording** — requires an approved Twilio Verify custom template
- **Phone camera scanning** — requires a public HTTPS URL (see section 4 above)
- **PDF print quality** — requires physical printer and paper

**Legend:**  
✅ Implemented and tested in automated suite.  
⚠️ Implemented; has a provider-side prerequisite not yet met.  
"Needs real test" = cannot be automated without live credentials/hardware.

---

## 8. What is deliberately NOT done (per requirements)

- No real Twilio credentials are embedded in the package.
- No real SMS is sent during tests.
- No real money is charged.
- No fabricated screenshots of SMS delivery or phone scanning are provided.
- No public domain is claimed — a localhost URL is warned against.
- The QR scanner route does not fetch any URL supplied by the scanner.
