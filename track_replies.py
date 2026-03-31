#!/usr/bin/env python3
"""
Reply Tracker — Check Gmail for replies to sent warm emails

Usage:
    python3 track_replies.py            # Check all pending emails
    python3 track_replies.py --summary  # Just print the summary table

Checks Gmail threads for replies and bounce notifications.
Updates sent_log.json with: REPLIED / BOUNCED / NO_REPLY status.

Can be run manually or via cron:
    0 9 * * * cd /path/to/warm_email_sender && python3 track_replies.py
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ============================================================================
# CONFIGURATION
# ============================================================================
CHECK_WINDOW_DAYS = 30    # How far back to check for replies
SCRIPT_DIR = Path(__file__).parent
SENT_LOG_FILE = SCRIPT_DIR / 'sent_log.json'
# ============================================================================

sys.path.insert(0, str(SCRIPT_DIR))
from setup_gmail import GmailSetup


class ReplyTracker:
    """Checks Gmail for replies and bounces to previously sent emails."""

    def __init__(self):
        print("\n🔐 Connecting to Gmail...")
        setup = GmailSetup()
        self.service = setup.get_gmail_service()

        profile = self.service.users().getProfile(userId='me').execute()
        self.sender_email = profile['emailAddress']
        print(f"✓ Authenticated as: {self.sender_email}")

        self.log = self._load_log()

    def _load_log(self):
        """Load the sent log."""
        if not SENT_LOG_FILE.exists():
            print("✗ No sent_log.json found — nothing to track")
            print("  Run send_emails.py first to send some emails")
            sys.exit(0)

        try:
            with open(SENT_LOG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"✗ Error reading sent_log.json: {e}")
            sys.exit(1)

    def _save_log(self):
        """Persist the log."""
        # Update metadata counts
        emails = self.log['emails']
        self.log['metadata']['total_replied'] = sum(
            1 for e in emails if e.get('reply_status') == 'REPLIED'
        )
        self.log['metadata']['total_bounced'] = sum(
            1 for e in emails if e.get('reply_status') == 'BOUNCED'
        )
        with open(SENT_LOG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.log, f, indent=2, ensure_ascii=False)

    # ── Reply Detection ─────────────────────────────────────────────────

    def _check_thread_for_reply(self, entry):
        """
        Check if a Gmail thread has a reply from someone other than the sender.
        Returns 'REPLIED' if reply found, None otherwise.
        """
        thread_id = entry.get('gmail_thread_id')
        if not thread_id:
            return None

        try:
            thread = self.service.users().threads().get(
                userId='me',
                id=thread_id,
                format='metadata',
                metadataHeaders=['From']
            ).execute()

            messages = thread.get('messages', [])
            if len(messages) <= 1:
                return None

            # Check if any message is from someone other than the sender
            for msg in messages[1:]:  # Skip the first (our sent message)
                headers = msg.get('payload', {}).get('headers', [])
                for header in headers:
                    if header['name'].lower() == 'from':
                        from_value = header['value'].lower()
                        if self.sender_email.lower() not in from_value:
                            return 'REPLIED'

        except Exception as e:
            print(f"  ⚠️  Thread check failed for {entry['email']}: {e}")

        return None

    def _check_for_bounce(self, entry):
        """
        Search for bounce notifications related to a sent email.
        Returns 'BOUNCED' if bounce detected, None otherwise.
        """
        subject = entry.get('subject', '')
        recipient = entry.get('email', '')

        # Search for mailer-daemon / postmaster messages about this email
        queries = [
            f'from:mailer-daemon "{recipient}"',
            f'from:postmaster "{recipient}"',
            f'from:mailer-daemon subject:"{subject[:50]}"',
        ]

        for query in queries:
            try:
                results = self.service.users().messages().list(
                    userId='me',
                    q=query,
                    maxResults=3
                ).execute()

                if results.get('resultSizeEstimate', 0) > 0:
                    # Verify the bounce is recent (within check window)
                    messages = results.get('messages', [])
                    for msg_ref in messages:
                        msg = self.service.users().messages().get(
                            userId='me',
                            id=msg_ref['id'],
                            format='metadata',
                            metadataHeaders=['Date']
                        ).execute()
                        internal_date = int(msg.get('internalDate', 0)) / 1000
                        msg_date = datetime.fromtimestamp(internal_date)
                        sent_date = datetime.fromisoformat(entry['sent_at'])

                        # Bounce should be after the send date
                        if msg_date >= sent_date:
                            return 'BOUNCED'

            except Exception:
                continue

        return None

    # ── Main Check Loop ─────────────────────────────────────────────────

    def check_all(self):
        """Check all pending emails for replies and bounces."""
        cutoff = datetime.now() - timedelta(days=CHECK_WINDOW_DAYS)
        pending = []

        for entry in self.log['emails']:
            if entry.get('reply_status') != 'NO_REPLY':
                continue
            if entry.get('status') != 'SENT':
                continue

            sent_at = datetime.fromisoformat(entry['sent_at'])
            if sent_at < cutoff:
                continue

            pending.append(entry)

        if not pending:
            print("\n📭 No pending emails to check")
            self.print_summary()
            return

        print(f"\n📬 Checking {len(pending)} emails for replies...")
        updated = 0

        for i, entry in enumerate(pending, 1):
            print(f"  [{i}/{len(pending)}] {entry['name']} <{entry['email']}>...", end=' ')

            # Check for reply first
            reply_status = self._check_thread_for_reply(entry)

            if reply_status == 'REPLIED':
                entry['reply_status'] = 'REPLIED'
                entry['reply_received_at'] = datetime.now().isoformat()
                print("✓ REPLIED")
                updated += 1
            else:
                # Check for bounce
                bounce_status = self._check_for_bounce(entry)
                if bounce_status == 'BOUNCED':
                    entry['reply_status'] = 'BOUNCED'
                    print("✗ BOUNCED")
                    updated += 1
                else:
                    print("— no reply yet")

            entry['reply_checked_at'] = datetime.now().isoformat()

        self._save_log()
        print(f"\n✓ Updated {updated} entries")
        self.print_summary()

    # ── Summary Table ───────────────────────────────────────────────────

    def print_summary(self):
        """Print a formatted tracking summary."""
        emails = self.log['emails']

        if not emails:
            print("\n📭 No emails in log")
            return

        total = len(emails)
        replied = sum(1 for e in emails if e.get('reply_status') == 'REPLIED')
        bounced = sum(1 for e in emails if e.get('reply_status') == 'BOUNCED')
        pending = sum(1 for e in emails if e.get('reply_status') == 'NO_REPLY')
        reply_rate = (replied / total * 100) if total > 0 else 0

        print(f"\n{'='*75}")
        print("REPLY TRACKING SUMMARY")
        print(f"{'='*75}")
        print(f"  {'#':<4} {'Name':<18} {'Email':<26} {'Sent':<12} {'Status':<10} {'Reply Date':<12}")
        print(f"  {'─'*4} {'─'*18} {'─'*26} {'─'*12} {'─'*10} {'─'*12}")

        for i, e in enumerate(emails, 1):
            status = e.get('reply_status', 'NO_REPLY')
            sent_date = e.get('sent_date', '-')
            reply_date = '-'
            if e.get('reply_received_at'):
                reply_date = e['reply_received_at'][:10]

            icon = {'REPLIED': '✓', 'BOUNCED': '✗', 'NO_REPLY': '—'}.get(status, '?')

            print(f"  {i:<4} {e['name'][:18]:<18} {e['email'][:26]:<26} "
                  f"{sent_date:<12} {icon} {status:<8} {reply_date:<12}")

        print(f"{'='*75}")
        print(f"  Total: {total} | Replied: {replied} ({reply_rate:.0f}%) | "
              f"Bounced: {bounced} | Pending: {pending}")
        print(f"{'='*75}")


def main():
    print("=" * 60)
    print("REPLY TRACKER")
    print("=" * 60)

    tracker = ReplyTracker()

    if '--summary' in sys.argv:
        tracker.print_summary()
    else:
        tracker.check_all()


if __name__ == '__main__':
    main()
