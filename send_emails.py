#!/usr/bin/env python3
"""
Warm Email Sender — Send personalized emails via Gmail API

Usage:
    python3 send_emails.py emails.csv
    python3 send_emails.py                # prompts for CSV path

CSV Format (headers are case-insensitive):
    Name, Email, Subject, Body, CC (optional), BCC (optional)

Sends plain-text emails from your authenticated Gmail account with
random 3-5 minute delays between sends. Max 10 emails per day.
"""

import base64
import csv
import json
import random
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from email.mime.text import MIMEText
from email.utils import make_msgid, formataddr
import email.policy
from pathlib import Path


def _parse_email_list(raw):
    """Split a comma/semicolon-separated email string into a cleaned list."""
    if not raw:
        return []
    return [e.strip() for e in raw.replace(';', ',').split(',') if e.strip()]

# ============================================================================
# CONFIGURATION
# ============================================================================
DAILY_SEND_LIMIT = 10
MIN_DELAY_SECONDS = 180   # 3 minutes
MAX_DELAY_SECONDS = 300   # 5 minutes
SCRIPT_DIR = Path(__file__).parent
SENT_LOG_FILE = SCRIPT_DIR / 'sent_log.json'
# ============================================================================

# Import setup helper
sys.path.insert(0, str(SCRIPT_DIR))
from setup_gmail import GmailSetup


class WarmEmailSender:
    """Sends personalized emails via Gmail API with human-like pacing."""

    def __init__(self):
        print("\n🔐 Connecting to Gmail...")
        setup = GmailSetup()
        self.service = setup.get_gmail_service()

        profile = self.service.users().getProfile(userId='me').execute()
        self.sender_email = profile['emailAddress']
        self.sender_domain = self.sender_email.split('@')[1]
        print(f"✓ Authenticated as: {self.sender_email}")

        self.log = self._load_log()

    # ── Log Management ──────────────────────────────────────────────────

    def _load_log(self):
        """Load or initialize the sent log."""
        if SENT_LOG_FILE.exists():
            try:
                with open(SENT_LOG_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, KeyError):
                print("⚠️  Corrupt sent_log.json — starting fresh")

        return {
            "metadata": {
                "created_at": datetime.now().isoformat(),
                "sender_email": self.sender_email,
                "total_sent": 0,
                "total_replied": 0,
                "total_bounced": 0,
            },
            "emails": []
        }

    def _save_log(self):
        """Persist the sent log to disk."""
        with open(SENT_LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.log, f, indent=2, ensure_ascii=False)

    def _get_today_send_count(self):
        """Count how many emails were sent today."""
        today = datetime.now().strftime('%Y-%m-%d')
        return sum(
            1 for e in self.log['emails']
            if e.get('sent_date') == today and e.get('status') == 'SENT'
        )

    # ── CSV Reading ─────────────────────────────────────────────────────

    def read_csv(self, csv_path):
        """
        Read recipients from CSV with flexible header matching.

        Required columns: Name, Email, Subject, Body
        Optional columns: CC, BCC
        """
        recipients = []
        path = Path(csv_path)

        if not path.exists():
            print(f"✗ CSV file not found: {csv_path}")
            return []

        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)

                if not reader.fieldnames:
                    print("✗ CSV file is empty or has no headers")
                    return []

                # Build case-insensitive header map
                header_map = {h.strip().lower(): h for h in reader.fieldnames}

                # Map required columns
                col_map = {}
                required = {'name': 'name', 'email': 'email', 'subject': 'subject', 'body': 'body'}
                optional = {'cc': 'cc', 'bcc': 'bcc'}

                for canonical, search_key in required.items():
                    found = header_map.get(search_key)
                    if not found:
                        # Try variations
                        for h_lower, h_orig in header_map.items():
                            if search_key in h_lower:
                                found = h_orig
                                break
                    if not found:
                        print(f"✗ Missing required column: {canonical}")
                        print(f"  Found columns: {', '.join(reader.fieldnames)}")
                        return []
                    col_map[canonical] = found

                for canonical, search_key in optional.items():
                    found = header_map.get(search_key)
                    if not found:
                        for h_lower, h_orig in header_map.items():
                            if search_key in h_lower:
                                found = h_orig
                                break
                    col_map[canonical] = found  # None if not found

                for i, row in enumerate(reader, 1):
                    name = (row.get(col_map['name']) or '').strip()
                    email_raw = (row.get(col_map['email']) or '').strip()
                    subject = (row.get(col_map['subject']) or '').strip()
                    body = (row.get(col_map['body']) or '').strip()
                    cc_raw = (row.get(col_map.get('cc', ''), '') or '').strip() if col_map.get('cc') else ''
                    bcc_raw = (row.get(col_map.get('bcc', ''), '') or '').strip() if col_map.get('bcc') else ''

                    if not email_raw or not subject or not body:
                        print(f"  ⚠️  Row {i}: skipping — missing email, subject, or body")
                        continue

                    # Support multiple comma/semicolon-separated To addresses
                    to_emails = _parse_email_list(email_raw)
                    if not to_emails or any('@' not in e for e in to_emails):
                        print(f"  ⚠️  Row {i}: skipping — invalid email(s): {email_raw}")
                        continue
                    email = ', '.join(to_emails)

                    # Normalize CC and BCC
                    cc = ', '.join(_parse_email_list(cc_raw))
                    bcc = ', '.join(_parse_email_list(bcc_raw))

                    recipients.append({
                        'name': name,
                        'email': email,
                        'subject': subject,
                        'body': body,
                        'cc': cc,
                        'bcc': bcc,
                    })

            print(f"✓ Loaded {len(recipients)} recipients from CSV")
            return recipients

        except Exception as e:
            print(f"✗ Error reading CSV: {e}")
            return []

    # ── Email Composition & Sending ─────────────────────────────────────

    def _compose_message(self, recipient):
        """Build a plain-text MIME message."""
        msg = MIMEText(recipient['body'], 'plain')

        to_emails = _parse_email_list(recipient['email'])

        msg = EmailMessage()
        msg['Subject'] = recipient['subject']
        msg['From'] = self.sender_email
        msg['To'] = ', '.join(to_emails) if to_emails else recipient['email']
        if recipient.get('cc'):
            msg['Cc'] = recipient['cc']
        if recipient.get('bcc'):
            msg['Bcc'] = recipient['bcc']
        msg['Message-ID'] = make_msgid(domain=self.sender_domain)
        msg.set_content(recipient['body'])

        return msg

    def _send_single(self, recipient):
        """Send one email via Gmail API. Returns (success, api_response, message_id)."""
        msg = self._compose_message(recipient)
        message_id = msg['Message-ID']

        raw = base64.urlsafe_b64encode(msg.as_bytes(policy=email.policy.SMTP)).decode()

        try:
            result = self.service.users().messages().send(
                userId='me',
                body={'raw': raw}
            ).execute()
            return True, result, message_id
        except Exception as e:
            return False, str(e), message_id

    def _log_sent(self, recipient, api_response, message_id):
        """Add a sent email to the log."""
        now = datetime.now()
        entry = {
            "name": recipient['name'],
            "email": recipient['email'],
            "subject": recipient['subject'],
            "cc": recipient.get('cc', ''),
            "bcc": recipient.get('bcc', ''),
            "gmail_message_id": api_response.get('id', ''),
            "gmail_thread_id": api_response.get('threadId', ''),
            "rfc_message_id": message_id,
            "sent_date": now.strftime('%Y-%m-%d'),
            "sent_at": now.isoformat(),
            "status": "SENT",
            "reply_status": "NO_REPLY",
            "reply_checked_at": None,
            "reply_received_at": None,
        }
        self.log['emails'].append(entry)
        self.log['metadata']['total_sent'] = sum(
            1 for e in self.log['emails'] if e['status'] == 'SENT'
        )
        self._save_log()

    # ── Batch Sending ───────────────────────────────────────────────────

    def send_batch(self, csv_path):
        """Read CSV and send emails with delays."""
        recipients = self.read_csv(csv_path)
        if not recipients:
            return

        # Check daily limit
        already_sent = self._get_today_send_count()
        remaining = DAILY_SEND_LIMIT - already_sent

        if remaining <= 0:
            print(f"\n⚠️  Daily limit reached ({DAILY_SEND_LIMIT} emails). Try again tomorrow.")
            return

        if len(recipients) > remaining:
            print(f"\n⚠️  Daily limit: {remaining} of {DAILY_SEND_LIMIT} remaining — "
                  f"will send first {remaining}, skip {len(recipients) - remaining}")
            recipients = recipients[:remaining]

        # Confirm before sending
        print(f"\n{'='*60}")
        print(f"READY TO SEND")
        print(f"{'='*60}")
        print(f"  From:       {self.sender_email}")
        print(f"  Recipients: {len(recipients)}")
        print(f"  Today used: {already_sent}/{DAILY_SEND_LIMIT}")
        print(f"  Delay:      {MIN_DELAY_SECONDS//60}-{MAX_DELAY_SECONDS//60} min between sends")
        print(f"{'='*60}")

        for i, r in enumerate(recipients, 1):
            print(f"  {i}. {r['name']} <{r['email']}> — {r['subject'][:50]}")

        print()
        confirm = input("Send these emails? (yes/no): ").strip().lower()
        if confirm not in ('yes', 'y'):
            print("Cancelled.")
            return

        # Send loop
        results = []
        print(f"\n{'='*60}")
        print("SENDING")
        print(f"{'='*60}")

        for i, recipient in enumerate(recipients, 1):
            print(f"\n[{i}/{len(recipients)}] Sending to {recipient['name']} <{recipient['email']}>...")

            success, response, message_id = self._send_single(recipient)

            if success:
                print(f"  ✓ Sent successfully")
                self._log_sent(recipient, response, message_id)
                results.append(('SENT', recipient))
            else:
                print(f"  ✗ Failed: {response}")
                results.append(('FAILED', recipient))

            # Delay between sends (skip after last email)
            if i < len(recipients):
                delay = random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
                print(f"\n  ⏱️  Waiting {delay // 60}m {delay % 60}s before next send...")
                self._countdown(delay)

        # Summary
        self._print_summary(results, already_sent)

    def _countdown(self, seconds):
        """Display a countdown timer."""
        for remaining in range(seconds, 0, -30):
            mins, secs = divmod(remaining, 60)
            print(f"     {mins}m {secs}s remaining...", end='\r')
            time.sleep(min(30, remaining))
        print("     Ready!                    ")

    def _print_summary(self, results, previously_sent):
        """Print a formatted summary table."""
        sent_count = sum(1 for status, _ in results if status == 'SENT')
        failed_count = sum(1 for status, _ in results if status == 'FAILED')

        print(f"\n{'='*60}")
        print("SEND SUMMARY")
        print(f"{'='*60}")
        print(f"  {'#':<4} {'Name':<20} {'Email':<28} {'Status':<8}")
        print(f"  {'─'*4} {'─'*20} {'─'*28} {'─'*8}")

        for i, (status, r) in enumerate(results, 1):
            icon = '✓' if status == 'SENT' else '✗'
            print(f"  {i:<4} {r['name'][:20]:<20} {r['email'][:28]:<28} {icon} {status}")

        print(f"{'='*60}")
        print(f"  Sent: {sent_count} | Failed: {failed_count} | "
              f"Daily usage: {previously_sent + sent_count}/{DAILY_SEND_LIMIT}")
        print(f"{'='*60}")


def main():
    print("=" * 60)
    print("WARM EMAIL SENDER")
    print("=" * 60)

    # Get CSV path
    if len(sys.argv) > 1:
        csv_path = sys.argv[1]
    else:
        csv_path = input("\nEnter path to CSV file: ").strip()

    if not csv_path:
        print("✗ No CSV path provided")
        sys.exit(1)

    sender = WarmEmailSender()
    sender.send_batch(csv_path)


if __name__ == '__main__':
    main()
