"""
One-time migration: copies the existing data/*.json files into MongoDB.

Run this once, after filling in a real MONGODB_URI in .env, before the app
starts relying on Mongo for storage — otherwise everything currently in
data/users, data/permissions, data/profiles, and data/sessions (settings,
granted subscriptions, profiles, trade history) starts over empty.

Safe to re-run: each collection is upserted by user id, so running it twice
just overwrites with the same data rather than duplicating anything.

Usage:
    venv\\Scripts\\python.exe migrate_json_to_mongo.py
"""

from __future__ import annotations

import json
import os

from db import get_db

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _load_json_files(subdir: str) -> dict:
    """Returns {user_id: parsed_json} for every <id>.json file in data/<subdir>/."""
    path = os.path.join(DATA_DIR, subdir)
    out = {}
    if not os.path.isdir(path):
        return out
    for name in os.listdir(path):
        if not name.endswith(".json"):
            continue
        try:
            uid = int(name[:-5])
        except ValueError:
            continue
        with open(os.path.join(path, name), "r", encoding="utf-8") as f:
            out[uid] = json.load(f)
    return out


def migrate():
    db = get_db()

    users = _load_json_files("users")
    if users:
        coll = db["users"]
        for uid, data in users.items():
            coll.replace_one({"_id": uid}, {"_id": uid, **data}, upsert=True)
        print(f"users: migrated {len(users)}")
    else:
        print("users: nothing to migrate")

    perms = _load_json_files("permissions")
    if perms:
        coll = db["permissions"]
        for uid, data in perms.items():
            coll.replace_one({"_id": uid}, {"_id": uid, **data}, upsert=True)
        print(f"permissions: migrated {len(perms)}")
    else:
        print("permissions: nothing to migrate")

    profiles = _load_json_files("profiles")
    if profiles:
        coll = db["profiles"]
        for uid, data in profiles.items():
            coll.replace_one({"_id": uid}, {"_id": uid, **data}, upsert=True)
        print(f"profiles: migrated {len(profiles)}")
    else:
        print("profiles: nothing to migrate")

    sessions = _load_json_files("sessions")
    if sessions:
        coll = db["session_history"]
        for uid, session_list in sessions.items():
            coll.replace_one({"_id": uid}, {"_id": uid, "sessions": session_list}, upsert=True)
        print(f"session_history: migrated {len(sessions)}")
    else:
        print("session_history: nothing to migrate")

    print("Done.")


if __name__ == "__main__":
    migrate()
