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


def get_today_count(for_sender=None):
    """Count emails sent today, optionally per sender account."""
    log = load_log()
    today = datetime.now().strftime('%Y-%m-%d')
    return sum(
        1 for e in log['emails']
        if e.get('sent_date') == today
        and e.get('status') == 'SENT'
        and (for_sender is None or e.get('sender_email', '') == for_sender)
    )


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
        today_count = get_today_count(sender_email)
        # Per-account usage for all connected accounts
        account_usage = {}
        try:
            for token_file in TOKENS_DIR.glob('*.json'):
                acct = token_file.stem
                acct_count = get_today_count(acct)
                account_usage[acct] = {"sent": acct_count, "remaining": DAILY_SEND_LIMIT - acct_count}
        except Exception:
            pass
        return jsonify({
            "authenticated": True,
            "sender_email": sender_email,
            "today_sent": today_count,
            "daily_limit": DAILY_SEND_LIMIT,
            "remaining": DAILY_SEND_LIMIT - today_count,
            "is_sending": send_status['is_sending'],
            "account_usage": account_usage,
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

        for opt in ['cc', 'bcc', 'sender_email']:
            search_keys = [opt] if opt != 'sender_email' else ['sender_email', 'sender', 'from', 'send_from']
            found = None
            for sk in search_keys:
                found = header_map.get(sk)
                if found:
                    break
            if not found:
                for h_lower, h_orig in header_map.items():
                    for sk in search_keys:
                        if sk in h_lower:
                            found = h_orig
                            break
                    if found:
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
            row_sender = (row.get(col_map.get('sender_email', ''), '') or '').strip() if col_map.get('sender_email') else ''

            if not email or '@' not in email:
                errors.append(f"Row {i}: invalid or missing email")
                continue
            if not subject or not body:
                errors.append(f"Row {i}: missing subject or body")
                continue

            # Validate sender if specified
            if row_sender and not (TOKENS_DIR / f'{row_sender}.json').exists():
                errors.append(f"Row {i}: sender '{row_sender}' is not a connected account — will use active account")
                row_sender = ''

            recipients.append({
                "row": i,
                "name": name,
                "email": email,
                "subject": subject,
                "body": body,
                "cc": cc,
                "bcc": bcc,
                "sender_email": row_sender,
            })

        return jsonify({
            "recipients": recipients,
            "count": len(recipients),
            "errors": errors,
            "remaining_today": DAILY_SEND_LIMIT - get_today_count(sender_email),
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

    # Check per-account limits and filter out recipients over limit
    account_sent = {}  # track how many we're queuing per account
    filtered = []
    skipped = []
    for r in recipients:
        acct = r.get('sender_email', '').strip() or sender_email or 'unknown'
        already = get_today_count(acct) + account_sent.get(acct, 0)
        if already >= DAILY_SEND_LIMIT:
            skipped.append(f"{r['email']} — {acct} hit daily limit ({DAILY_SEND_LIMIT})")
            continue
        account_sent[acct] = account_sent.get(acct, 0) + 1
        filtered.append(r)

    recipients = filtered
    if not recipients:
        return jsonify({"error": "All recipients skipped — sender accounts hit daily limit",
                        "skipped": skipped}), 429

    # Start background send
    send_thread = threading.Thread(target=_send_worker, args=(recipients,), daemon=True)
    send_thread.start()

    return jsonify({
        "message": f"Sending {len(recipients)} emails...",
        "count": len(recipients),
    })


_log_lock = threading.Lock()
_status_lock = threading.Lock()


def _get_sender_service(email_addr, service_cache):
    """Get Gmail service for a specific sender. Falls back to active account."""
    if email_addr in service_cache:
        return service_cache[email_addr], email_addr
    token_path = TOKENS_DIR / f'{email_addr}.json'
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_path, 'w') as f:
                    f.write(creds.to_json())
            svc = build('gmail', 'v1', credentials=creds)
            service_cache[email_addr] = svc
            return svc, email_addr
        except Exception:
            pass
    # Fallback to active account
    return get_service(), sender_email


def _send_one_email(r, svc, actual_sender, log):
    """Send a single email and append to log. Returns (status, error_msg)."""
    try:
        domain = actual_sender.split('@')[1]

        msg = MIMEText(r['body'], 'plain')
        msg['To'] = formataddr((r['name'], r['email']))
        msg['From'] = actual_sender
        msg['Subject'] = r['subject']
        if r.get('cc'):
            msg['Cc'] = r['cc']
        if r.get('bcc'):
            msg['Bcc'] = r['bcc']
        msg['Message-ID'] = make_msgid(domain=domain)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = svc.users().messages().send(userId='me', body={'raw': raw}).execute()

        now = datetime.now()
        entry = {
            "name": r['name'],
            "email": r['email'],
            "subject": r['subject'],
            "body": r.get('body', ''),
            "cc": r.get('cc', ''),
            "bcc": r.get('bcc', ''),
            "sender_email": actual_sender,
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
        with _log_lock:
            log['emails'].append(entry)
            save_log(log)
        return "SENT", None
    except Exception as e:
        return "FAILED", str(e)


def _account_worker(account_email, recipient_list, log, service_cache):
    """Send all emails for one account sequentially with delays."""
    global send_status

    for idx, r in enumerate(recipient_list):
        # Resolve the actual sender
        row_sender = r.get('sender_email', '').strip()
        if row_sender:
            svc, actual_sender = _get_sender_service(row_sender, service_cache)
        else:
            svc, actual_sender = get_service(), sender_email

        status, error = _send_one_email(r, svc, actual_sender, log)

        # Update global status atomically
        with _status_lock:
            send_status['current'] += 1
            result_entry = {
                "email": r['email'],
                "name": r['name'],
                "status": status,
                "sender_email": actual_sender,
            }
            if error:
                result_entry['error'] = error
            send_status['results'].append(result_entry)

            # Update per-account status
            acct_status = send_status['accounts'].get(account_email, {})
            acct_status['current'] = idx + 1
            if status == "SENT":
                acct_status['sent'] = acct_status.get('sent', 0) + 1
            else:
                acct_status['failed'] = acct_status.get('failed', 0) + 1
            send_status['accounts'][account_email] = acct_status

        # Delay between sends for this account only (skip after last)
        if idx < len(recipient_list) - 1:
            delay = random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
            with _status_lock:
                send_status['accounts'][account_email]['next_send_in'] = delay
            for remaining in range(delay, 0, -1):
                with _status_lock:
                    send_status['accounts'][account_email]['next_send_in'] = remaining
                time.sleep(1)
            with _status_lock:
                send_status['accounts'][account_email]['next_send_in'] = 0

    # Mark this account as finished
    with _status_lock:
        send_status['accounts'][account_email]['done'] = True


def _send_worker(recipients):
    """Background orchestrator — spawns one thread per sender account."""
    global send_status

    # Group recipients by sender account
    groups = {}
    for r in recipients:
        acct = r.get('sender_email', '').strip() or sender_email or 'unknown'
        groups.setdefault(acct, []).append(r)

    send_status = {
        "is_sending": True,
        "current": 0,
        "total": len(recipients),
        "results": [],
        "next_send_in": 0,  # kept for backward compat
        "accounts": {
            acct: {
                "total": len(rs),
                "current": 0,
                "sent": 0,
                "failed": 0,
                "next_send_in": 0,
                "done": False,
            }
            for acct, rs in groups.items()
        },
    }

    log = load_log()
    service_cache = {}

    # Spawn one thread per account — they all run in parallel
    threads = []
    for acct, rs in groups.items():
        t = threading.Thread(
            target=_account_worker,
            args=(acct, rs, log, service_cache),
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Wait for all account threads to finish
    for t in threads:
        t.join()

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


@app.route('/api/export')
def api_export():
    """Export all sent emails as a CSV download."""
    log = load_log()
    emails = log.get('emails', [])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Name', 'Recipient Email', 'Subject', 'Body', 'Sender Email',
        'CC', 'BCC', 'Sent Date', 'Sent At',
        'Send Status', 'Reply Status', 'Reply Date', 'Last Checked',
        'Gmail Message ID', 'Gmail Thread ID',
    ])
    for e in emails:
        writer.writerow([
            e.get('name', ''),
            e.get('email', ''),
            e.get('subject', ''),
            e.get('body', ''),
            e.get('sender_email', ''),
            e.get('cc', ''),
            e.get('bcc', ''),
            e.get('sent_date', ''),
            e.get('sent_at', ''),
            e.get('status', ''),
            e.get('reply_status', ''),
            (e.get('reply_received_at') or '')[:10],
            (e.get('reply_checked_at') or '')[:10],
            e.get('gmail_message_id', ''),
            e.get('gmail_thread_id', ''),
        ])

    from flask import Response
    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=email_log_{timestamp}.csv'}
    )


def _extract_body(msg):
    """Extract plain-text body from a Gmail message payload."""
    import base64

    def _decode(data):
        try:
            return base64.urlsafe_b64decode(data + '==').decode('utf-8', errors='replace')
        except Exception:
            return ''

    payload = msg.get('payload', {})

    # Multipart — walk parts looking for text/plain
    def _find_plain(part):
        mime = part.get('mimeType', '')
        if mime == 'text/plain':
            return _decode(part.get('body', {}).get('data', ''))
        for sub in part.get('parts', []):
            result = _find_plain(sub)
            if result:
                return result
        return ''

    body = _find_plain(payload)
    if body:
        return body.strip()

    # Fallback: snipped
    return msg.get('snippet', '')


def _validate_email(email):
    """
    Two-step email validation:
    1. MX record check — domain can receive mail
    2. SMTP RCPT TO check — mailbox exists on the server

    Returns dict: { email, valid, reason, method }
    """
    import smtplib
    import socket

    try:
        import dns.resolver
        DNS_AVAILABLE = True
    except ImportError:
        DNS_AVAILABLE = False

    result = {'email': email, 'valid': None, 'reason': '', 'method': ''}

    # Basic format check
    if '@' not in email or '.' not in email.split('@')[-1]:
        result.update({'valid': False, 'reason': 'Invalid email format', 'method': 'format'})
        return result

    domain = email.split('@')[1].lower()

    # Step 1: MX record lookup
    mx_host = None
    if DNS_AVAILABLE:
        try:
            records = dns.resolver.resolve(domain, 'MX')
            mx_host = str(sorted(records, key=lambda r: r.preference)[0].exchange).rstrip('.')
            result['method'] = 'mx+smtp'
        except Exception as e:
            result.update({'valid': False, 'reason': f'No MX records for domain: {domain}', 'method': 'mx'})
            return result
    else:
        # Fallback: try domain directly as MX
        mx_host = domain
        result['method'] = 'smtp'

    # Step 2: SMTP RCPT TO check
    try:
        smtp = smtplib.SMTP(timeout=10)
        smtp.connect(mx_host, 25)
        smtp.helo('rapidclaims.ai')
        smtp.mail('verify@rapidclaims.ai')
        code, msg = smtp.rcpt(email)
        smtp.quit()

        msg_str = msg.decode('utf-8', errors='replace') if isinstance(msg, bytes) else str(msg)

        if code == 250:
            result.update({'valid': True, 'reason': 'Mailbox exists'})
        elif code == 251:
            result.update({'valid': True, 'reason': 'Address will be forwarded'})
        elif code in (550, 551, 552, 553, 554):
            result.update({'valid': False, 'reason': f'Mailbox does not exist ({code}): {msg_str[:120]}'})
        else:
            # 252 = server can't verify but will try, 450/451 = temp error
            # Treat as unknown — don't block sending
            result.update({'valid': None, 'reason': f'Server could not verify ({code}) — unconfirmed'})

    except smtplib.SMTPConnectError:
        result.update({'valid': None, 'reason': 'SMTP connection refused — unconfirmed'})
    except smtplib.SMTPServerDisconnected:
        result.update({'valid': None, 'reason': 'Server disconnected early — unconfirmed'})
    except socket.timeout:
        result.update({'valid': None, 'reason': 'SMTP timeout — unconfirmed'})
    except Exception as e:
        result.update({'valid': None, 'reason': f'Could not verify: {str(e)[:100]}'})

    return result


@app.route('/api/validate', methods=['POST'])
def api_validate():
    """
    Validate a list of email addresses via MX + SMTP checks.
    Body: { "emails": ["a@b.com", ...] }
    Returns per-email validation results.
    """
    data = request.json or {}
    emails = data.get('emails', [])

    if not emails:
        return jsonify({'error': 'No emails provided'}), 400
    if len(emails) > 100:
        return jsonify({'error': 'Max 100 emails per request'}), 400

    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_validate_email, email): email for email in emails}
        for future in as_completed(futures):
            r = future.result()
            results[r['email']] = r

    # Summary counts
    valid = sum(1 for r in results.values() if r['valid'] is True)
    invalid = sum(1 for r in results.values() if r['valid'] is False)
    unknown = sum(1 for r in results.values() if r['valid'] is None)

    return jsonify({
        'results': results,
        'summary': {'valid': valid, 'invalid': invalid, 'unknown': unknown, 'total': len(emails)}
    })


@app.route('/api/replies')
def api_replies():
    """Return reply bodies for all emails with reply_status=REPLIED."""
    log = load_log()
    replied = []

    for entry in log['emails']:
        if entry.get('reply_status') != 'REPLIED':
            continue

        # If body already stored in log, return it directly
        if entry.get('reply_body'):
            replied.append({
                'name': entry['name'],
                'email': entry['email'],
                'subject': entry['subject'],
                'sender_email': entry.get('sender_email', ''),
                'sent_date': entry.get('sent_date', ''),
                'reply_from': entry.get('reply_from', entry['email']),
                'reply_received_at': entry.get('reply_received_at', ''),
                'reply_body': entry['reply_body'],
            })
            continue

        # Fetch body live from Gmail
        entry_sender = entry.get('sender_email') or sender_email
        thread_id = entry.get('gmail_thread_id')
        if not thread_id:
            continue

        try:
            token_path = TOKENS_DIR / f'{entry_sender}.json'
            if not token_path.exists():
                continue
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            svc = build('gmail', 'v1', credentials=creds)

            found_body = None
            found_from = None

            # Check 1: look for reply in the original thread
            try:
                thread = svc.users().threads().get(
                    userId='me', id=thread_id, format='full'
                ).execute()
                messages = thread.get('messages', [])
                for msg in messages[1:]:
                    headers = msg.get('payload', {}).get('headers', [])
                    from_val = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
                    if entry_sender.lower() not in from_val.lower():
                        found_body = _extract_body(msg)
                        found_from = from_val
                        break
            except Exception:
                pass

            # Check 2: search-based — reply may have arrived as a new thread
            if not found_body:
                try:
                    recipient = entry.get('email', '')
                    subject = entry.get('subject', '')
                    sent_date = entry.get('sent_date', '')
                    clean_subj = subject[:40].replace('"', '')
                    queries = [
                        f'from:{recipient} subject:"{clean_subj}" in:anywhere after:{sent_date}',
                        f'from:{recipient} subject:"Re: {clean_subj}" in:anywhere after:{sent_date}',
                        f'from:{recipient} in:anywhere after:{sent_date}',
                    ]
                    for q in queries:
                        results = svc.users().messages().list(userId='me', q=q, maxResults=3).execute()
                        if results.get('messages'):
                            msg_id = results['messages'][0]['id']
                            msg = svc.users().messages().get(userId='me', id=msg_id, format='full').execute()
                            headers = msg.get('payload', {}).get('headers', [])
                            from_val = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
                            found_body = _extract_body(msg)
                            found_from = from_val
                            break
                except Exception:
                    pass

            result = {
                'name': entry['name'],
                'email': entry['email'],
                'subject': entry['subject'],
                'sender_email': entry_sender,
                'sent_date': entry.get('sent_date', ''),
                'reply_from': found_from or entry.get('reply_from', entry['email']),
                'reply_received_at': entry.get('reply_received_at', ''),
                'reply_body': found_body or '[Reply detected but body could not be retrieved]',
            }
            # Cache in log
            if found_body:
                entry['reply_body'] = found_body
                entry['reply_from'] = found_from
            replied.append(result)

        except Exception as e:
            replied.append({
                'name': entry['name'],
                'email': entry['email'],
                'subject': entry['subject'],
                'sender_email': entry_sender,
                'sent_date': entry.get('sent_date', ''),
                'reply_from': entry.get('reply_from', ''),
                'reply_received_at': entry.get('reply_received_at', ''),
                'reply_body': f'[Error fetching body: {e}]',
            })

    save_log(log)
    return jsonify({'replies': replied, 'count': len(replied)})


@app.route('/api/track', methods=['POST'])
def api_track():
    """Run reply tracker and return updated log."""
    try:
        log = load_log()
        updated = 0
        track_errors = []

        # Build a service per sender_email so multi-account tracking works
        service_cache = {}

        def _get_service_for(email):
            if email in service_cache:
                return service_cache[email]
            token_path = TOKENS_DIR / f'{email}.json'
            if not token_path.exists():
                return None
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                svc = build('gmail', 'v1', credentials=creds)
                service_cache[email] = svc
                return svc
            except Exception:
                return None

        for entry in log['emails']:
            if entry.get('reply_status') != 'NO_REPLY' or entry.get('status') != 'SENT':
                continue

            thread_id = entry.get('gmail_thread_id')
            if not thread_id:
                continue

            # Use the account that sent this email for tracking
            entry_sender = entry.get('sender_email') or sender_email
            svc = _get_service_for(entry_sender)
            if not svc:
                track_errors.append(f"{entry_sender}: token missing or expired — cannot check replies")
                continue

            # Check 1: Thread-based — look for replies in the same thread
            #          Search in:anywhere to catch replies that land in spam
            try:
                thread = svc.users().threads().get(
                    userId='me', id=thread_id, format='full',
                    metadataHeaders=['From']
                ).execute()

                messages = thread.get('messages', [])
                if len(messages) > 1:
                    for msg in messages[1:]:
                        headers = msg.get('payload', {}).get('headers', [])
                        for h in headers:
                            if h['name'].lower() == 'from' and entry_sender.lower() not in h['value'].lower():
                                entry['reply_status'] = 'REPLIED'
                                entry['reply_received_at'] = datetime.now().isoformat()
                                entry['reply_body'] = _extract_body(msg)
                                entry['reply_from'] = h['value']
                                updated += 1
                                break
                        if entry['reply_status'] == 'REPLIED':
                            break
            except Exception as e:
                track_errors.append(f"{entry_sender}: thread check failed for {entry.get('email')} — {e}")

            # Check 2: Search-based — reply may land as a new thread or in spam
            #          Require subject match to avoid false positives from unrelated emails
            if entry['reply_status'] == 'NO_REPLY':
                try:
                    recipient = entry.get('email', '')
                    subject = entry.get('subject', '')
                    sent_date = entry.get('sent_date', '')

                    if subject and recipient:
                        # Search everywhere (inbox + spam) — require subject match
                        # Use Re: prefix since most replies add it
                        clean_subj = subject[:40].replace('"', '')
                        queries = [
                            f'from:{recipient} subject:"{clean_subj}" in:anywhere after:{sent_date}',
                            f'from:{recipient} subject:"Re: {clean_subj}" in:anywhere after:{sent_date}',
                        ]
                        found = False
                        for q in queries:
                            results = svc.users().messages().list(userId='me', q=q, maxResults=3).execute()
                            if results.get('messages'):
                                # Fetch full message to get body
                                msg_id = results['messages'][0]['id']
                                msg = svc.users().messages().get(userId='me', id=msg_id, format='full').execute()
                                headers = msg.get('payload', {}).get('headers', [])
                                from_val = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
                                entry['reply_status'] = 'REPLIED'
                                entry['reply_received_at'] = datetime.now().isoformat()
                                entry['reply_body'] = _extract_body(msg)
                                entry['reply_from'] = from_val
                                updated += 1
                                found = True
                                break
                except Exception as e:
                    track_errors.append(f"{entry_sender}: search failed for {entry.get('email')} — {e}")

            # Check 3: Bounce detection
            if entry['reply_status'] == 'NO_REPLY':
                try:
                    q = f'(from:mailer-daemon OR from:postmaster) "{entry["email"]}" in:anywhere after:{entry.get("sent_date", "")}'
                    results = svc.users().messages().list(userId='me', q=q, maxResults=3).execute()
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

        # Deduplicate errors
        unique_errors = list(dict.fromkeys(track_errors))

        return jsonify({
            "message": f"Checked {len(log['emails'])} emails, updated {updated}",
            "updated": updated,
            "errors": unique_errors,
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
        prompt='consent'
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


# ── Domain Analysis ──────────────────────────────────────────────────────

def _get_service_for_account(email_addr):
    """Load and return a Gmail service for the given account email."""
    token_path = TOKENS_DIR / f'{email_addr}.json'
    if not token_path.exists():
        raise ValueError(f"No token found for {email_addr}")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, 'w') as f:
            f.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)


def _fetch_domain_emails(svc, domain, max_emails=100):
    """Fetch up to max_emails messages from/to a given domain."""
    query = f'from:@{domain} OR to:@{domain}'
    messages = []
    page_token = None

    while len(messages) < max_emails:
        batch_size = min(50, max_emails - len(messages))
        kwargs = dict(userId='me', q=query, maxResults=batch_size)
        if page_token:
            kwargs['pageToken'] = page_token
        result = svc.users().messages().list(**kwargs).execute()
        msgs = result.get('messages', [])
        if not msgs:
            break
        messages.extend(msgs)
        page_token = result.get('nextPageToken')
        if not page_token:
            break

    return messages


def _get_header(headers, name):
    for h in headers:
        if h['name'].lower() == name.lower():
            return h['value']
    return ''


def _fetch_message_details(svc, msg_id):
    """Fetch subject, from, to, date, and body snippet for a message."""
    msg = svc.users().messages().get(userId='me', id=msg_id, format='full').execute()
    headers = msg.get('payload', {}).get('headers', [])
    body = _extract_body(msg)
    return {
        'date': _get_header(headers, 'Date'),
        'from': _get_header(headers, 'From'),
        'to': _get_header(headers, 'To'),
        'subject': _get_header(headers, 'Subject'),
        'body': body if body else msg.get('snippet', ''),
    }


@app.route('/api/analyze-domain', methods=['POST'])
def api_analyze_domain():
    """
    Fetch all emails from/to a domain for a given account and
    run a deep analysis via Claude.

    Body: { "account": "me@gmail.com", "domain": "acme.com" }
    """
    import anthropic

    data = request.json or {}
    account = data.get('account', '').strip()
    domain = data.get('domain', '').strip().lstrip('@').lower()

    if not account or not domain:
        return jsonify({"error": "account and domain are required"}), 400

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY environment variable not set"}), 500

    try:
        svc = _get_service_for_account(account)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"Failed to load Gmail account: {e}"}), 500

    # Fetch message list
    try:
        msg_refs = _fetch_domain_emails(svc, domain, max_emails=100)
    except Exception as e:
        return jsonify({"error": f"Gmail API error fetching messages: {e}"}), 500

    if not msg_refs:
        return jsonify({
            "domain": domain,
            "account": account,
            "email_count": 0,
            "analysis": f"No emails found from or to @{domain} in this account.",
            "emails": [],
        })

    # Fetch full details for each message (cap at 80 to stay within token limits)
    emails = []
    for ref in msg_refs[:80]:
        try:
            details = _fetch_message_details(svc, ref['id'])
            emails.append(details)
        except Exception:
            continue

    # Sort chronologically
    emails_sorted = sorted(emails, key=lambda e: e.get('date', ''))

    # Build the context block for Claude
    email_lines = []
    for i, e in enumerate(emails_sorted, 1):
        email_lines.append(
            f"--- Email {i} ---\n"
            f"Date: {e['date']}\n"
            f"From: {e['from']}\n"
            f"To: {e['to']}\n"
            f"Subject: {e['subject']}\n"
            f"Body:\n{e['body']}\n"
        )
    email_block = '\n'.join(email_lines)

    prompt = f"""You are a GTM and Product analyst reviewing the full email history between our team and the domain "{domain}".
Below are {len(emails_sorted)} emails (sorted chronologically) exchanged with people at @{domain}.

{email_block}

Analyze these emails from a GTM and Product lens. Provide a structured debrief covering:

## 1. Contact Registry
For each person at @{domain} who appears in the emails:
- Full name, email address, role/title (if inferrable)
- Whether they are a decision-maker, implementer, or influencer
- How actively they participated in the email threads

## 2. Current Situation Assessment
- What is the current status of this account? (active pilot, stalled, churned, prospect, etc.)
- Relationship temperature — warm, cold, at risk?
- When was the last meaningful interaction and what was it about?

## 3. Implementation & Delivery Review
- **Timeline & Pace**: Map out the key milestones. Were we delivering at an acceptable cadence? Were there gaps or delays?
- **Quality Signals**: What indicators of satisfaction or dissatisfaction appeared? Any explicit feedback, complaints, or praise?
- **Blockers & Friction**: What caused delays or friction? (technical issues, process bottlenecks, people dependencies, unclear requirements)
- **Feature Requests**: Any product capabilities they asked for or wished existed?

## 4. Specialty & EHR Intelligence
- What specialty-specific workflow nuances were discussed? (e.g., coding patterns, claim types, denial reasons)
- EHR-specific integration details, limitations, or requirements mentioned
- Domain terminology, jargon, or context that demonstrates deep knowledge of their world
- Any payer or regulatory nuances that came up

## 5. GTM & Sales Learnings
- What value propositions or claims resonated with them? What language did they respond positively to?
- What objections or concerns were raised? How were they handled?
- Are there proof points, metrics, or wins we can reference when prospecting similar accounts?
- Any competitive mentions — incumbent tools, alternatives they considered?

## 6. Pilot Experience Improvements
- What should we do differently in future pilots based on this experience?
- What went well that we should replicate?
- What went poorly that we should fix?

## 7. Next Steps
Ranked by priority — specific, actionable recommendations for this account right now.

Be thorough and reference specific emails (by date and subject) as evidence for your conclusions."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=16384,
            messages=[{'role': 'user', 'content': prompt}],
        )
        analysis_text = message.content[0].text
    except Exception as e:
        return jsonify({"error": f"Claude API error: {e}"}), 500

    return jsonify({
        "domain": domain,
        "account": account,
        "email_count": len(emails_sorted),
        "analysis": analysis_text,
        "emails": emails_sorted,
    })


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
