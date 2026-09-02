"""
Local single-process storage backend.

This dashboard is a local-only, single-user tool. Storage is therefore the
simplest thing that works: tabular data lives in a single SQLite database and
"blobs" (snapshot JSON) live as files on disk. There is no Azure Storage,
Azurite, or managed-identity dependency.

The public surface intentionally mirrors the small slice of the
``azure-data-tables`` / ``azure-storage-blob`` client APIs the rest of the
codebase already calls, so callers (``compile``, ``bom_storage``,
``bom_catalog``, ``activity_log``, the ``snapshots_*`` endpoints) did not have
to change:

    get_table_service()           -> service with create/delete/get-client
    get_table_client(name)        -> table with CRUD + query_entities
    get_blob_service()            -> service with get_container_client
    get_blob_container(name=None) -> container with upload/download_blob

Entities are plain dicts that must carry ``PartitionKey`` and ``RowKey``;
all other keys are stored as a JSON document, preserving str/bool/int/float.

Storage location: ``LOCAL_STORAGE_DIR`` env var, else ``<repo>/local-storage``.
Concurrency: a fresh SQLite connection per operation (WAL mode), so the
availability ThreadPoolExecutor can write activity-log rows safely.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Dict, Iterator, List, Optional

_lock = threading.Lock()


# ─── Storage location ────────────────────────────────────────────────────────

def _storage_root() -> str:
    env = os.getenv("LOCAL_STORAGE_DIR", "").strip()
    if env:
        root = Path(env)
    else:
        # api/_shared/storage.py -> api/_shared -> api -> <repo root>
        root = Path(__file__).resolve().parents[2] / "local-storage"
    # Hosted multi-customer isolation: partition the store per signed-in customer
    # so concurrent customers can never read each other's transient data. The
    # durable customer BOM lives in their browser; anything written here is
    # ephemeral and per-user. Gated by MULTIUSER_ISOLATION so local single-user
    # mode (and the test suite) keep the flat layout.
    if os.getenv("MULTIUSER_ISOLATION", "").lower() in ("true", "1", "yes"):
        uk = _current_user_key()
        if uk:
            root = root / "u" / uk
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _current_user_key() -> Optional[str]:
    """Sanitized per-request user key from auth_token, or None. Imported lazily
    to avoid any import cycle and to stay a no-op when no request is active."""
    try:
        from . import auth_token
        raw = auth_token.current_user_key()
    except Exception:
        return None
    if not raw:
        return None
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(raw))
    return safe[:80] or None


def _db_path() -> str:
    return os.path.join(_storage_root(), "app.db")


def _blob_root() -> str:
    path = os.path.join(_storage_root(), "blobs")
    os.makedirs(path, exist_ok=True)
    return path


def storage_root() -> str:
    """Absolute path of the local-storage root (DB + blobs live here)."""
    return _storage_root()


def snapshots_dir() -> str:
    """Absolute path of the directory where snapshot blobs are persisted."""
    return os.path.join(_blob_root(), "snapshots")


# ─── SQLite helpers ──────────────────────────────────────────────────────────

# SQLite physical table name derived from the logical Azure-table name.
_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


def _phys_table(name: str) -> str:
    return "tbl_" + _NAME_RE.sub("_", name or "default")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def _ensure_table(conn: sqlite3.Connection, phys: str) -> None:
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{phys}" ('
        " pk TEXT NOT NULL,"
        " rk TEXT NOT NULL,"
        " data TEXT NOT NULL,"
        " PRIMARY KEY (pk, rk) )"
    )
    # The primary key indexes (pk, rk) — but several hot paths query by RowKey
    # alone (e.g. fetching a run/snapshot by run_id), which would otherwise scan
    # the whole table. A secondary index on rk keeps those lookups cheap.
    conn.execute(
        f'CREATE INDEX IF NOT EXISTS "{phys}_rk" ON "{phys}" (rk)'
    )


# ─── OData-subset filter parsing ─────────────────────────────────────────────
# Only ``<PartitionKey|RowKey> eq <'literal'|@param>`` is ever used.

_FILTER_RE = re.compile(
    r"^\s*(PartitionKey|RowKey)\s+eq\s+(.+?)\s*$", re.IGNORECASE
)


def _parse_eq_filter(
    query_filter: Optional[str], parameters: Optional[Dict]
) -> Optional[tuple]:
    """Return ``(field, value)`` for a simple equality filter, or ``None``
    if the filter is absent/unrecognized (caller then sees all rows and may
    apply its own in-Python filtering)."""
    if not query_filter:
        return None
    m = _FILTER_RE.match(query_filter)
    if not m:
        return None
    field = m.group(1).lower()  # 'partitionkey' | 'rowkey'
    raw = m.group(2).strip()
    if raw.startswith("@"):
        key = raw[1:]
        if not parameters or key not in parameters:
            return None
        value = str(parameters[key])
    elif raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        value = raw[1:-1].replace("''", "'")
    else:
        value = raw
    col = "pk" if field == "partitionkey" else "rk"
    return (col, value)


def _row_to_entity(pk: str, rk: str, data: str) -> Dict:
    try:
        props = json.loads(data) if data else {}
    except Exception:
        props = {}
    props["PartitionKey"] = pk
    props["RowKey"] = rk
    return props


def _split_entity(entity: Dict) -> tuple:
    if not isinstance(entity, dict):
        raise ValueError("entity must be a dict")
    pk = entity.get("PartitionKey")
    rk = entity.get("RowKey")
    if pk is None or rk is None:
        raise ValueError("entity requires PartitionKey and RowKey")
    props = {k: v for k, v in entity.items() if k not in ("PartitionKey", "RowKey")}
    return str(pk), str(rk), props


# ─── Table client ────────────────────────────────────────────────────────────

class _LocalTable:
    def __init__(self, logical_name: str):
        self._name = logical_name
        self._phys = _phys_table(logical_name)
        with _lock:
            conn = _connect()
            try:
                _ensure_table(conn, self._phys)
            finally:
                conn.close()

    def create_entity(self, entity: Optional[Dict] = None, **kwargs) -> Dict:
        entity = entity if entity is not None else kwargs.get("entity")
        pk, rk, props = _split_entity(entity)
        with _lock:
            conn = _connect()
            try:
                _ensure_table(conn, self._phys)
                try:
                    conn.execute(
                        f'INSERT INTO "{self._phys}" (pk, rk, data) VALUES (?, ?, ?)',
                        (pk, rk, json.dumps(props, ensure_ascii=False)),
                    )
                except sqlite3.IntegrityError as ex:
                    raise FileExistsError(
                        f"entity ({pk!r},{rk!r}) already exists"
                    ) from ex
            finally:
                conn.close()
        return entity

    def upsert_entity(self, entity: Dict, mode: str = "merge", **kwargs) -> Dict:
        pk, rk, props = _split_entity(entity)
        with _lock:
            conn = _connect()
            try:
                _ensure_table(conn, self._phys)
                if str(mode).lower() == "merge":
                    cur = conn.execute(
                        f'SELECT data FROM "{self._phys}" WHERE pk=? AND rk=?',
                        (pk, rk),
                    )
                    row = cur.fetchone()
                    if row:
                        try:
                            existing = json.loads(row[0]) if row[0] else {}
                        except Exception:
                            existing = {}
                        existing.update(props)
                        props = existing
                conn.execute(
                    f'INSERT INTO "{self._phys}" (pk, rk, data) VALUES (?, ?, ?) '
                    "ON CONFLICT(pk, rk) DO UPDATE SET data=excluded.data",
                    (pk, rk, json.dumps(props, ensure_ascii=False)),
                )
            finally:
                conn.close()
        return entity

    def get_entity(self, partition_key: str, row_key: str, **kwargs) -> Dict:
        with _lock:
            conn = _connect()
            try:
                _ensure_table(conn, self._phys)
                cur = conn.execute(
                    f'SELECT pk, rk, data FROM "{self._phys}" WHERE pk=? AND rk=?',
                    (str(partition_key), str(row_key)),
                )
                row = cur.fetchone()
            finally:
                conn.close()
        if not row:
            raise KeyError(f"entity ({partition_key!r},{row_key!r}) not found")
        return _row_to_entity(row[0], row[1], row[2])

    def delete_entity(self, partition_key: str, row_key: str, **kwargs) -> None:
        with _lock:
            conn = _connect()
            try:
                _ensure_table(conn, self._phys)
                cur = conn.execute(
                    f'DELETE FROM "{self._phys}" WHERE pk=? AND rk=?',
                    (str(partition_key), str(row_key)),
                )
                if cur.rowcount == 0:
                    raise KeyError(
                        f"entity ({partition_key!r},{row_key!r}) not found"
                    )
            finally:
                conn.close()

    def list_entities(self, **kwargs) -> List[Dict]:
        with _lock:
            conn = _connect()
            try:
                _ensure_table(conn, self._phys)
                rows = conn.execute(
                    f'SELECT pk, rk, data FROM "{self._phys}"'
                ).fetchall()
            finally:
                conn.close()
        return [_row_to_entity(r[0], r[1], r[2]) for r in rows]

    def query_entities(
        self,
        query_filter: Optional[str] = None,
        *,
        parameters: Optional[Dict] = None,
        **kwargs,
    ) -> List[Dict]:
        parsed = _parse_eq_filter(query_filter, parameters)
        with _lock:
            conn = _connect()
            try:
                _ensure_table(conn, self._phys)
                if parsed is None:
                    rows = conn.execute(
                        f'SELECT pk, rk, data FROM "{self._phys}"'
                    ).fetchall()
                else:
                    col, value = parsed
                    rows = conn.execute(
                        f'SELECT pk, rk, data FROM "{self._phys}" WHERE {col}=?',
                        (value,),
                    ).fetchall()
            finally:
                conn.close()
        return [_row_to_entity(r[0], r[1], r[2]) for r in rows]


class _LocalTableService:
    def get_table_client(self, name: str) -> _LocalTable:
        return _LocalTable(name)

    def create_table_if_not_exists(self, name: str) -> _LocalTable:
        return _LocalTable(name)

    def create_table(self, name: str) -> _LocalTable:
        return _LocalTable(name)

    def delete_table(self, name: str) -> None:
        phys = _phys_table(name)
        with _lock:
            conn = _connect()
            try:
                conn.execute(f'DROP TABLE IF EXISTS "{phys}"')
            finally:
                conn.close()


# ─── Blob container ──────────────────────────────────────────────────────────

class _Downloader:
    def __init__(self, path: str):
        self._path = path

    def readall(self) -> bytes:
        with open(self._path, "rb") as f:
            return f.read()


class _LocalBlobContainer:
    def __init__(self, name: str):
        self._name = name
        self._dir = os.path.join(_blob_root(), name)
        os.makedirs(self._dir, exist_ok=True)

    def _path(self, blob_name: str) -> str:
        # Blob names can contain '/', producing nested directories. Reject any
        # traversal so a crafted name can never escape the container dir.
        safe = (blob_name or "").replace("\\", "/").lstrip("/")
        parts = [p for p in safe.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValueError(f"unsafe blob name: {blob_name!r}")
        full = os.path.join(self._dir, *parts)
        # Defense in depth: the resolved path must stay under the container dir.
        base = os.path.realpath(self._dir)
        resolved = os.path.realpath(full)
        if resolved != base and not resolved.startswith(base + os.sep):
            raise ValueError(f"unsafe blob name: {blob_name!r}")
        return full

    def upload_blob(
        self,
        name: str,
        data,
        overwrite: bool = False,
        content_type: Optional[str] = None,
        **kwargs,
    ) -> None:
        path = self._path(name)
        if os.path.exists(path) and not overwrite:
            raise FileExistsError(f"blob {name!r} already exists")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(data, str):
            data = data.encode("utf-8")
        with open(path, "wb") as f:
            f.write(data)

    def download_blob(self, blob, **kwargs) -> _Downloader:
        name = getattr(blob, "name", blob)
        path = self._path(name)
        if not os.path.exists(path):
            raise FileNotFoundError(f"blob {name!r} not found")
        return _Downloader(path)

    def list_blobs(self, name_starts_with: Optional[str] = None, **kwargs) -> List["_BlobItem"]:
        base = os.path.realpath(self._dir)
        out: List[_BlobItem] = []
        for root, _dirs, files in os.walk(self._dir):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), base).replace(os.sep, "/")
                if name_starts_with and not rel.startswith(name_starts_with):
                    continue
                out.append(_BlobItem(rel))
        return out

    def delete_blob(self, blob, **kwargs) -> None:
        name = getattr(blob, "name", blob)
        try:
            os.remove(self._path(name))
        except (FileNotFoundError, ValueError):
            pass


class _BlobItem:
    """Minimal stand-in for the azure-storage-blob list item (``.name`` only)."""

    def __init__(self, name: str):
        self.name = name


class _LocalBlobService:
    def get_container_client(self, name: str) -> _LocalBlobContainer:
        return _LocalBlobContainer(name)

    def create_container(self, name: str) -> _LocalBlobContainer:
        return _LocalBlobContainer(name)


# ─── Public API (names/signatures preserved) ─────────────────────────────────

def get_blob_service() -> _LocalBlobService:
    return _LocalBlobService()


def get_table_service() -> _LocalTableService:
    return _LocalTableService()


def get_table_client(name: str) -> _LocalTable:
    return _LocalTable(name)


def get_blob_container(name: str = None) -> _LocalBlobContainer:
    name = name or os.getenv("STORAGE_CONTAINER", "snapshots")
    return _LocalBlobContainer(name)


def wipe_snapshot_blobs() -> int:
    """Delete every snapshot blob file. Returns the number removed."""
    container = get_blob_container("snapshots")
    count = 0
    for item in container.list_blobs():
        container.delete_blob(item)
        count += 1
    return count
