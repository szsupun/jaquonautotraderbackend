"""
One-time migration: encrypt any plaintext SSIDs already sitting in the
`users` collection from before SSID_ENCRYPTION_KEY / crypto_util.py existed.

Safe to re-run — encrypt_ssid() is a no-op on values already carrying the
"enc:v1:" prefix, so already-migrated docs are skipped.
"""

from __future__ import annotations

from crypto_util import encrypt_ssid
from db import get_db

_users = get_db()["users"]


def main() -> None:
    migrated = 0
    skipped = 0
    for doc in _users.find({}, {"_id": 1, "demo_ssid": 1, "real_ssid": 1}):
        uid = doc["_id"]
        demo = doc.get("demo_ssid", "") or ""
        real = doc.get("real_ssid", "") or ""

        new_demo = encrypt_ssid(demo)
        new_real = encrypt_ssid(real)

        if new_demo == demo and new_real == real:
            skipped += 1
            continue

        _users.update_one({"_id": uid}, {"$set": {"demo_ssid": new_demo, "real_ssid": new_real}})
        migrated += 1
        print(f"  encrypted SSIDs for user {uid}")

    print(f"\nDone. {migrated} user(s) migrated, {skipped} already encrypted or empty.")


if __name__ == "__main__":
    main()
