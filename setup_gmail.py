#!/usr/bin/env python3
"""
Gmail OAuth2 Setup Helper for Warm Email Sender

Usage:
    python3 setup_gmail.py              # Run interactive setup wizard

Prerequisites:
    1. Create a Google Cloud project at console.cloud.google.com
    2. Enable the Gmail API
    3. Configure OAuth consent screen (Internal for Workspace)
    4. Create OAuth 2.0 Client ID (Desktop application)
    5. Download credentials.json and place it in this folder
"""

import json
import sys
from pathlib import Path

# ============================================================================
# CONFIGURATION
# ============================================================================
SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
]
import os
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get('DATA_DIR', SCRIPT_DIR / 'data'))
CREDENTIALS_FILE = SCRIPT_DIR / 'credentials.json'
TOKEN_FILE = DATA_DIR / 'token.json'
# ============================================================================

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False


class GmailSetup:
    """Handles OAuth2 setup and provides authenticated Gmail service."""

    def __init__(self, credentials_file=None, token_file=None):
        self.credentials_file = Path(credentials_file) if credentials_file else CREDENTIALS_FILE
        self.token_file = Path(token_file) if token_file else TOKEN_FILE

    def check_dependencies(self):
        """Check if required packages are installed."""
        if not DEPS_AVAILABLE:
            print("\n✗ Missing required packages. Install them with:")
            print("  pip3 install google-auth google-auth-oauthlib google-api-python-client")
            return False
        print("✓ Required packages installed")
        return True

    def check_credentials_file(self):
        """Check if credentials.json exists."""
        if self.credentials_file.exists():
            print(f"✓ Found {self.credentials_file.name}")
            return True

        print(f"\n✗ {self.credentials_file.name} not found")
        print("\n📋 Setup Instructions:")
        print("   1. Go to https://console.cloud.google.com")
        print("   2. Create a new project (or select existing)")
        print("   3. Enable the Gmail API:")
        print("      → APIs & Services → Library → search 'Gmail API' → Enable")
        print("   4. Configure OAuth consent screen:")
        print("      → APIs & Services → OAuth consent screen")
        print("      → User Type: Internal (for Workspace) or External")
        print("      → Add scopes: gmail.send, gmail.readonly")
        print("   5. Create credentials:")
        print("      → APIs & Services → Credentials → Create Credentials")
        print("      → OAuth 2.0 Client ID → Application type: Desktop app")
        print("   6. Download the JSON file")
        print(f"   7. Save it as: {self.credentials_file}")
        return False

    def run_oauth_flow(self):
        """Run the OAuth2 consent flow and save token."""
        creds = None

        # Check for existing valid token
        if self.token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
            except Exception:
                creds = None

        # Refresh or run new flow
        if creds and creds.valid:
            print("✓ Existing token is valid")
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                print("✓ Token refreshed successfully")
                self._save_token(creds)
                return creds
            except Exception as e:
                print(f"⚠️  Token refresh failed: {e}")
                print("   Running new OAuth flow...")

        # New OAuth flow
        print("\n🔐 Starting OAuth2 authorization flow...")
        print("   A browser window will open — sign in with the Gmail account you want to send from.\n")

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_file), SCOPES
        )
        creds = flow.run_local_server(
            port=0,
            access_type='offline',
            prompt='consent'
        )

        self._save_token(creds)
        print("✓ Authorization complete — token saved")
        return creds

    def _save_token(self, creds):
        """Save credentials to token file."""
        with open(self.token_file, 'w') as f:
            f.write(creds.to_json())

    def get_gmail_service(self):
        """
        Return an authenticated Gmail API service object.

        This is the main method imported by send_emails.py and track_replies.py.
        """
        if not self.check_dependencies():
            sys.exit(1)

        if not self.check_credentials_file():
            sys.exit(1)

        creds = self.run_oauth_flow()
        service = build('gmail', 'v1', credentials=creds)
        return service

    def validate(self):
        """Validate setup by fetching the authenticated user's profile."""
        service = self.get_gmail_service()
        profile = service.users().getProfile(userId='me').execute()
        email = profile.get('emailAddress', 'unknown')
        print(f"\n✅ Setup complete! Authenticated as: {email}")
        return email


def main():
    print("=" * 60)
    print("GMAIL OAUTH2 SETUP WIZARD")
    print("=" * 60)

    setup = GmailSetup()

    if not setup.check_dependencies():
        sys.exit(1)

    if not setup.check_credentials_file():
        sys.exit(1)

    email = setup.validate()
    print(f"\n   All emails will be sent from: {email}")
    print(f"   Token saved to: {setup.token_file}")
    print(f"\n   You can now run: python3 send_emails.py <csv_file>")


if __name__ == '__main__':
    main()
