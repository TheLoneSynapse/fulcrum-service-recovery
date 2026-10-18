"""Shared pytest configuration.

``app.main`` binds its store at import time. Force the in-memory store for the
whole test session so tests never read or write the server's SQLite file and
every run starts from a clean slate (ReviewStore() defaults to in-memory for
tests and the eval runner anyway; this covers the app-level store too).
"""

import os

os.environ.setdefault("FULCRUM_STORE_PATH", ":memory:")