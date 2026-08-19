from __future__ import annotations

import asyncio
from weakref import WeakKeyDictionary


_connection_locks: WeakKeyDictionary[object, asyncio.Lock] = WeakKeyDictionary()
_fallback_locks: dict[int, asyncio.Lock] = {}


def sqlite_write_lock(connection: object) -> asyncio.Lock:
    """Return the shared in-process write lock for one SQLite connection."""
    try:
        lock = _connection_locks.get(connection)
    except TypeError:
        # Test doubles and unusual wrappers may not support weak references.
        return _fallback_locks.setdefault(id(connection), asyncio.Lock())
    if lock is None:
        lock = asyncio.Lock()
        _connection_locks[connection] = lock
    return lock
