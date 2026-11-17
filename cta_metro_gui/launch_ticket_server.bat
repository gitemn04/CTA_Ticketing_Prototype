@echo off
REM CTA Metro v3.0 - Ticket server launcher (Windows)
REM Starts the Flask ticket access server.
REM
REM The public HTTPS ticket URL is read from cta_config.json (next to cta.db):
REM     { "ticket_server": { "url": "https://your-domain.com" } }
REM It can be overridden for one run with the CTA_TICKET_SERVER_URL environment
REM variable or by passing --url https://your-domain.com to this launcher.
REM If a URL is configured but invalid, the server refuses to start.
REM If none is configured, PDFs are produced without a ticket-link QR.
REM See DEPLOYMENT.md.

cd /d "%~dp0"

echo Starting CTA Metro Ticket Server on port 5050...
echo.
echo IMPORTANT: A localhost URL will NOT work from a phone camera.
echo See DEPLOYMENT.md for how to get a public HTTPS URL.
echo.
echo Press Ctrl+C to stop the server.
echo.

python cta_ticket_server.py --host 127.0.0.1 --port 5050 --db cta.db %*
set CTA_EXIT=%ERRORLEVEL%

if not "%CTA_EXIT%"=="0" (
    echo.
    echo The ticket server stopped with error code %CTA_EXIT%.
)

pause
exit /b %CTA_EXIT%
