"""
At-rest encryption for customer PocketOption SSIDs.

An SSID is a live session credential — anyone who has it can act on that
customer's PocketOption account. Historically these were stored as plain
text in MongoDB, so a database leak alone would have been enough to expose
every customer's trading account, not just their settings. This module
encrypts them before they ever reach the database.

Key comes from SSID_ENCRYPTION_KEY (a Fernet key, see .env.example for how
to generate one) — kept in the server's own env, never in the database, so
a database-only leak can't decrypt anything.

Encrypted values are stored with an "enc:v1:" prefix so decrypt_ssid() can
tell them apart from any legacy plaintext SSID left over from before this
was added — those are returned unchanged instead of raising, so a
partially-migrated deployment (or a doc the migration script missed)
doesn't crash a customer's session.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"

_key = os.environ.get("SSID_ENCRYPTION_KEY", "").strip()
if not _key:
    raise RuntimeError(
        "SSID_ENCRYPTION_KEY is not set. Generate one with:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
        "and put it in .env as SSID_ENCRYPTION_KEY=... "
        "This key encrypts customer PocketOption SSIDs at rest — losing it "
        "means every stored SSID becomes unreadable, so back it up outside the database."
    )

_fernet = Fernet(_key.encode())


def encrypt_ssid(value: str) -> str:
    """Encrypt an SSID for storage. Empty strings pass through unchanged
    (a user with no SSID saved yet shouldn't get a spurious encrypted blob)."""
    if not value:
        return value
    if value.startswith(_PREFIX):
        return value  # already encrypted (e.g. re-saving a loaded settings object)
    token = _fernet.encrypt(value.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_ssid(value: str) -> str:
    """Decrypt an SSID read from storage. Values without the enc:v1: prefix
    are legacy plaintext (pre-migration) and are returned as-is."""
    if not value or not value.startswith(_PREFIX):
        return value
    token = value[len(_PREFIX):]
    try:
        return _fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt an SSID — wrong key or corrupted value.")
        return ""
