#!/usr/bin/env python3
"""
Warm Email Sender — Web UI Backend

Usage:
    python3 app.py                  # Starts on http://localhost:5050

Provides API endpoints for the React dashboard:
    GET  /api/status        — Auth status + daily send count
    POST /api/upload        — Upload CSV, returns parsed recipients
    POST /api/send          — Send emails from uploaded CSV
    GET  /api/log           — Full sent log
    POST /api/track         — Run reply tracker, return updated log
"""

import base64
import csv
import io
import json
import random
import sys
import threading
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import make_msgid, formataddr
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, redirect, session
from flask_cors import CORS

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ============================================================================
# CONFIGURATION
# ============================================================================
import os
PORT = int(os.environ.get('PORT', 5050))
DAILY_SEND_LIMIT = int(os.environ.get('DAILY_SEND_LIMIT', 10))
MIN_DELAY_SECONDS = 180   # 3 minutes
MAX_DELAY_SECONDS = 300   # 5 minutes
SCRIPT_DIR = Path(__file__).parent

# Railway persistent volume: mount at /data in Railway settings
# Locally: falls back to ./data
DATA_DIR = Path(os.environ.get('DATA_DIR', SCRIPT_DIR / 'data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SENT_LOG_FILE = DATA_DIR / 'sent_log.json'
TOKENS_DIR = DATA_DIR / 'tokens'
CREDENTIALS_FILE = SCRIPT_DIR / 'credentials.json'
SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
]
# ============================================================================

sys.path.insert(0, str(SCRIPT_DIR))
from setup_gmail import GmailSetup

TOKENS_DIR.mkdir(exist_ok=True)

# If GOOGLE_CREDENTIALS env var is set (Railway), write it to credentials.json
if os.environ.get('GOOGLE_CREDENTIALS') and not CREDENTIALS_FILE.exists():
    with open(CREDENTIALS_FILE, 'w') as f:
        f.write(os.environ['GOOGLE_CREDENTIALS'])
    print("✓ Wrote credentials.json from GOOGLE_CREDENTIALS env var")

app = Flask(__name__, static_folder=str(SCRIPT_DIR))
app.secret_key = os.environ.get('SECRET_KEY', 'warm-email-sender-session-key-2026')
CORS(app)

# Railway terminates SSL at their load balancer — tell Flask to trust
# the X-Forwarded-Proto header so request.is_secure returns True
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ── Global state ────────────────────────────────────────────────────────

gmail_service = None
sender_email = None
send_thread = None
send_status = {
    "is_sending": False,
    "current": 0,
    "total": 0,
    "results": [],
    "next_send_in": 0,
}


def get_service():
    """Lazy-init Gmail service. Returns None if not authenticated."""
    global gmail_service, sender_email
    if gmail_service is None:
        # Try loading first available token from tokens dir
        for token_file in sorted(TOKENS_DIR.glob('*.json')):
            try:
                creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(token_file, 'w') as f:
                        f.write(creds.to_json())
                if creds.valid:
                    gmail_service = build('gmail', 'v1', credentials=creds)
                    profile = gmail_service.users().getProfile(userId='me').execute()
                    sender_email = profile['emailAddress']
                    return gmail_service
            except Exception:
                continue

        # Fallback: try legacy token.json
        legacy_token = SCRIPT_DIR / 'token.json'
        if legacy_token.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(legacy_token), SCOPES)
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                if creds.valid:
                    gmail_service = build('gmail', 'v1', credentials=creds)
                    profile = gmail_service.users().getProfile(userId='me').execute()
                    sender_email = profile['emailAddress']
                    return gmail_service
            except Exception:
                pass

        raise Exception("No authenticated Gmail account. Go to /setup to connect one.")
    return gmail_service


def load_log():
    """Load sent log from disk."""
    if SENT_LOG_FILE.exists():
        try:
            with open(SENT_LOG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError):
            pass
    return {
        "metadata": {
            "created_at": datetime.now().isoformat(),
            "sender_email": sender_email or "",
            "total_sent": 0,
            "total_replied": 0,
            "total_bounced": 0,
        },
        "emails": []
    }


def save_log(log):
    """Persist sent log."""
    emails = log['emails']
    log['metadata']['total_sent'] = sum(1 for e in emails if e.get('status') == 'SENT')
    log['metadata']['total_replied'] = sum(1 for e in emails if e.get('reply_status') == 'REPLIED')
    log['metadata']['total_bounced'] = sum(1 for e in emails if e.get('reply_status') == 'BOUNCED')
    with open(SENT_LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def get_today_count():
    """Count emails sent today."""
    log = load_log()
    today = datetime.now().strftime('%Y-%m-%d')
    return sum(1 for e in log['emails'] if e.get('sent_date') == today and e.get('status') == 'SENT')


# ── UI Route ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(str(SCRIPT_DIR), 'index.html')


# ── API Routes ──────────────────────────────────────────────────────────

@app.route('/api/status')
def api_status():
    """Return auth status and daily counts. Always returns 200 for healthcheck."""
    try:
        service = get_service()
        today_count = get_today_count()
        return jsonify({
            "authenticated": True,
            "sender_email": sender_email,
            "today_sent": today_count,
            "daily_limit": DAILY_SEND_LIMIT,
            "remaining": DAILY_SEND_LIMIT - today_count,
            "is_sending": send_status['is_sending'],
        })
    except Exception as e:
        return jsonify({
            "authenticated": False,
            "error": str(e),
            "message": "Go to /setup to connect a Gmail account",
        })


@app.route('/api/upload', methods=['POST'])
def api_upload():
    """Parse uploaded CSV and return recipients."""
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['file']
    if not file.filename.endswith('.csv'):
        return jsonify({"error": "File must be a .csv"}), 400

    try:
        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))

        if not reader.fieldnames:
            return jsonify({"error": "CSV is empty or has no headers"}), 400

        # Case-insensitive header mapping
        header_map = {h.strip().lower(): h for h in reader.fieldnames}
        col_map = {}

        for canonical in ['name', 'email', 'subject', 'body']:
            found = header_map.get(canonical)
            if not found:
                for h_lower, h_orig in header_map.items():
                    if canonical in h_lower:
                        found = h_orig
                        break
            if not found:
                return jsonify({"error": f"Missing required column: {canonical}",
                                "found_columns": list(reader.fieldnames)}), 400
            col_map[canonical] = found

        for opt in ['cc', 'bcc']:
            found = header_map.get(opt)
            if not found:
                for h_lower, h_orig in header_map.items():
                    if opt in h_lower:
                        found = h_orig
                        break
            col_map[opt] = found

        recipients = []
        errors = []
        for i, row in enumerate(reader, 1):
            name = (row.get(col_map['name']) or '').strip()
            email = (row.get(col_map['email']) or '').strip()
            subject = (row.get(col_map['subject']) or '').strip()
            body = (row.get(col_map['body']) or '').strip()
            cc = (row.get(col_map.get('cc', ''), '') or '').strip() if col_map.get('cc') else ''
            bcc = (row.get(col_map.get('bcc', ''), '') or '').strip() if col_map.get('bcc') else ''

            if not email or '@' not in email:
                errors.append(f"Row {i}: invalid or missing email")
                continue
            if not subject or not body:
                errors.append(f"Row {i}: missing subject or body")
                continue

            recipients.append({
                "row": i,
                "name": name,
                "email": email,
                "subject": subject,
                "body": body,
                "cc": cc,
                "bcc": bcc,
            })

        return jsonify({
            "recipients": recipients,
            "count": len(recipients),
            "errors": errors,
            "remaining_today": DAILY_SEND_LIMIT - get_today_count(),
        })

    except Exception as e:
        return jsonify({"error": f"Failed to parse CSV: {str(e)}"}), 400


@app.route('/api/send', methods=['POST'])
def api_send():
    """Start sending emails in a background thread."""
    global send_thread

    if send_status['is_sending']:
        return jsonify({"error": "Already sending — wait for current batch to finish"}), 409

    data = request.json
    recipients = data.get('recipients', [])

    if not recipients:
        return jsonify({"error": "No recipients provided"}), 400

    remaining = DAILY_SEND_LIMIT - get_today_count()
    if remaining <= 0:
        return jsonify({"error": "Daily limit reached"}), 429

    if len(recipients) > remaining:
        recipients = recipients[:remaining]

    # Start background send
    send_thread = threading.Thread(target=_send_worker, args=(recipients,), daemon=True)
    send_thread.start()

    return jsonify({
        "message": f"Sending {len(recipients)} emails...",
        "count": len(recipients),
    })


def _send_worker(recipients):
    """Background worker that sends emails with delays."""
    global send_status

    send_status = {
        "is_sending": True,
        "current": 0,
        "total": len(recipients),
        "results": [],
        "next_send_in": 0,
    }

    service = get_service()
    log = load_log()
    domain = sender_email.split('@')[1]

    for i, r in enumerate(recipients):
        send_status['current'] = i + 1

        try:
            msg = MIMEText(r['body'], 'plain')
            msg['To'] = formataddr((r['name'], r['email']))
            msg['From'] = sender_email
            msg['Subject'] = r['subject']
            if r.get('cc'):
                msg['Cc'] = r['cc']
            if r.get('bcc'):
                msg['Bcc'] = r['bcc']
            msg['Message-ID'] = make_msgid(domain=domain)

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            result = service.users().messages().send(userId='me', body={'raw': raw}).execute()

            now = datetime.now()
            entry = {
                "name": r['name'],
                "email": r['email'],
                "subject": r['subject'],
                "cc": r.get('cc', ''),
                "bcc": r.get('bcc', ''),
                "gmail_message_id": result.get('id', ''),
                "gmail_thread_id": result.get('threadId', ''),
                "rfc_message_id": msg['Message-ID'],
                "sent_date": now.strftime('%Y-%m-%d'),
                "sent_at": now.isoformat(),
                "status": "SENT",
                "reply_status": "NO_REPLY",
                "reply_checked_at": None,
                "reply_received_at": None,
            }
            log['emails'].append(entry)
            save_log(log)

            send_status['results'].append({"email": r['email'], "name": r['name'], "status": "SENT"})

        except Exception as e:
            send_status['results'].append({"email": r['email'], "name": r['name'], "status": "FAILED", "error": str(e)})

        # Delay between sends
        if i < len(recipients) - 1:
            delay = random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
            send_status['next_send_in'] = delay
            for remaining in range(delay, 0, -1):
                send_status['next_send_in'] = remaining
                time.sleep(1)
            send_status['next_send_in'] = 0

    send_status['is_sending'] = False


@app.route('/api/send-status')
def api_send_status():
    """Poll current send progress."""
    return jsonify(send_status)


@app.route('/api/log')
def api_log():
    """Return full sent log."""
    log = load_log()
    emails = log['emails']

    total = len(emails)
    replied = sum(1 for e in emails if e.get('reply_status') == 'REPLIED')
    bounced = sum(1 for e in emails if e.get('reply_status') == 'BOUNCED')
    no_reply = sum(1 for e in emails if e.get('reply_status') == 'NO_REPLY')

    return jsonify({
        "emails": emails,
        "stats": {
            "total": total,
            "replied": replied,
            "bounced": bounced,
            "no_reply": no_reply,
            "reply_rate": round(replied / total * 100, 1) if total > 0 else 0,
        }
    })


@app.route('/api/track', methods=['POST'])
def api_track():
    """Run reply tracker and return updated log."""
    try:
        service = get_service()
        log = load_log()
        updated = 0

        for entry in log['emails']:
            if entry.get('reply_status') != 'NO_REPLY' or entry.get('status') != 'SENT':
                continue

            thread_id = entry.get('gmail_thread_id')
            if not thread_id:
                continue

            # Check thread for replies
            try:
                thread = service.users().threads().get(
                    userId='me', id=thread_id, format='metadata',
                    metadataHeaders=['From']
                ).execute()

                messages = thread.get('messages', [])
                if len(messages) > 1:
                    for msg in messages[1:]:
                        headers = msg.get('payload', {}).get('headers', [])
                        for h in headers:
                            if h['name'].lower() == 'from' and sender_email.lower() not in h['value'].lower():
                                entry['reply_status'] = 'REPLIED'
                                entry['reply_received_at'] = datetime.now().isoformat()
                                updated += 1
                                break
                        if entry['reply_status'] == 'REPLIED':
                            break
            except Exception:
                pass

            # Bounce check
            if entry['reply_status'] == 'NO_REPLY':
                try:
                    q = f'from:mailer-daemon "{entry["email"]}"'
                    results = service.users().messages().list(userId='me', q=q, maxResults=3).execute()
                    if results.get('resultSizeEstimate', 0) > 0:
                        entry['reply_status'] = 'BOUNCED'
                        updated += 1
                except Exception:
                    pass

            entry['reply_checked_at'] = datetime.now().isoformat()

        save_log(log)

        emails = log['emails']
        total = len(emails)
        replied = sum(1 for e in emails if e.get('reply_status') == 'REPLIED')
        bounced = sum(1 for e in emails if e.get('reply_status') == 'BOUNCED')

        return jsonify({
            "message": f"Checked {len(log['emails'])} emails, updated {updated}",
            "updated": updated,
            "emails": emails,
            "stats": {
                "total": total,
                "replied": replied,
                "bounced": bounced,
                "no_reply": total - replied - bounced,
                "reply_rate": round(replied / total * 100, 1) if total > 0 else 0,
            }
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── OAuth Web Flow (for adding accounts via browser) ────────────────────

def _get_redirect_uri():
    """Build the OAuth redirect URI based on the request. Force https on Railway."""
    url = request.host_url.rstrip('/')
    if 'railway.app' in url or os.environ.get('RAILWAY_ENVIRONMENT'):
        url = url.replace('http://', 'https://')
    return url + '/oauth/callback'


@app.route('/setup')
def setup_page():
    return send_from_directory(str(SCRIPT_DIR), 'setup.html')


@app.route('/oauth/start')
def oauth_start():
    """Redirect user to Google's OAuth consent screen."""
    if not CREDENTIALS_FILE.exists():
        return jsonify({"error": "credentials.json not found on server"}), 500

    flow = Flow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        scopes=SCOPES,
        redirect_uri=_get_redirect_uri()
    )
    auth_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true'
    )
    session['oauth_state'] = state
    session['code_verifier'] = flow.code_verifier  # Save PKCE verifier
    return redirect(auth_url)


@app.route('/oauth/callback')
def oauth_callback():
    """Handle the OAuth callback from Google."""
    try:
        flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            scopes=SCOPES,
            redirect_uri=_get_redirect_uri()
        )
        flow.code_verifier = session.get('code_verifier')  # Restore PKCE verifier
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        # Get the email address for this account
        service = build('gmail', 'v1', credentials=creds)
        profile = service.users().getProfile(userId='me').execute()
        email = profile['emailAddress']

        # Save token keyed by email
        token_path = TOKENS_DIR / f'{email}.json'
        with open(token_path, 'w') as f:
            f.write(creds.to_json())

        # Also save as the legacy token.json for backward compat
        legacy_token = SCRIPT_DIR / 'token.json'
        if not legacy_token.exists():
            with open(legacy_token, 'w') as f:
                f.write(creds.to_json())

        return redirect(f'/setup?success={email}')

    except Exception as e:
        return redirect(f'/setup?error={str(e)}')


@app.route('/api/accounts')
def api_accounts():
    """List all connected Gmail accounts."""
    accounts = []
    for token_file in TOKENS_DIR.glob('*.json'):
        email = token_file.stem
        # Check if token is still valid
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_file, 'w') as f:
                    f.write(creds.to_json())
            valid = creds.valid
        except Exception:
            valid = False

        accounts.append({
            "email": email,
            "valid": valid,
            "token_file": token_file.name,
        })

    return jsonify({"accounts": accounts})


@app.route('/api/accounts/switch', methods=['POST'])
def api_switch_account():
    """Switch the active sending account."""
    global gmail_service, sender_email

    data = request.json
    email = data.get('email', '')
    token_path = TOKENS_DIR / f'{email}.json'

    if not token_path.exists():
        return jsonify({"error": f"No token found for {email}"}), 404

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, 'w') as f:
                f.write(creds.to_json())

        gmail_service = build('gmail', 'v1', credentials=creds)
        profile = gmail_service.users().getProfile(userId='me').execute()
        sender_email = profile['emailAddress']

        return jsonify({"message": f"Switched to {sender_email}", "sender_email": sender_email})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/accounts/remove', methods=['POST'])
def api_remove_account():
    """Remove a connected account's token."""
    data = request.json
    email = data.get('email', '')
    token_path = TOKENS_DIR / f'{email}.json'

    if token_path.exists():
        token_path.unlink()
        return jsonify({"message": f"Removed {email}"})
    return jsonify({"error": "Account not found"}), 404


# ── Migrate legacy data to DATA_DIR ─────────────────────────────────────

def _migrate_legacy_data():
    """Move tokens and log from SCRIPT_DIR to DATA_DIR if they exist."""
    try:
        legacy_log = SCRIPT_DIR / 'sent_log.json'
        if legacy_log.exists() and not SENT_LOG_FILE.exists():
            import shutil
            shutil.copy2(legacy_log, SENT_LOG_FILE)
            print(f"✓ Migrated sent_log.json to {DATA_DIR}")

        legacy_tokens = SCRIPT_DIR / 'tokens'
        if legacy_tokens.exists() and legacy_tokens != TOKENS_DIR:
            for tf in legacy_tokens.glob('*.json'):
                dest = TOKENS_DIR / tf.name
                if not dest.exists():
                    import shutil
                    shutil.copy2(tf, dest)
                    print(f"✓ Migrated token {tf.name} to {TOKENS_DIR}")
    except Exception as e:
        print(f"⚠️  Migration skipped: {e}")

_migrate_legacy_data()


# ── Main ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 60)
    print("WARM EMAIL SENDER — WEB UI")
    print("=" * 60)

    # Try pre-auth on startup (non-fatal if it fails)
    try:
        get_service()
        print(f"✓ Authenticated as: {sender_email}")
    except Exception:
        print(f"⚠️  No Gmail account connected yet — go to /setup to connect one")

    print(f"\n🌐 Dashboard: http://localhost:{PORT}")
    print(f"   Setup:     http://localhost:{PORT}/setup")
    print(f"   Press Ctrl+C to stop\n")

    app.run(host='0.0.0.0', port=PORT, debug=False)
