"""
MongoDB connection — single shared client for every data store.

pymongo's MongoClient is lazy: constructing it doesn't actually open a
connection, so importing this module is cheap even if Mongo isn't reachable
yet. The first real query is what triggers (and would raise on) a connection
attempt. One client is reused everywhere — pymongo pools connections
internally, so there's no need (and no benefit) to create more than one.
"""

from __future__ import annotations

import logging

from pymongo import MongoClient
from pymongo.database import Database

from config import MONGODB_DB_NAME, MONGODB_URI

logger = logging.getLogger(__name__)

_client: MongoClient = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=5000,
    # pymongo's own defaults leave these unbounded — a stalled socket (a
    # network blip between this VPS and Atlas) then hangs the calling
    # request until the OS-level TCP stack gives up, which can take 100+
    # seconds. Bounding them makes a hiccup fail fast (and pymongo retries
    # reads/writes automatically by default) instead of freezing whatever
    # HTTP request or admin panel load was waiting on it.
    connectTimeoutMS=10000,
    socketTimeoutMS=10000,
)
_db: Database = _client[MONGODB_DB_NAME]


def get_db() -> Database:
    return _db
