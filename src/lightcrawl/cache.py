"""v0.3 local fetch cache (PR 2.2).

Storage model and public API mirror ``docs/v0.3/design.md §5.2``. PR 2.2
ships the module standalone; the Router cache aspect (PR 2.3) and CLI
flags (PR 2.4) wire it in. ETag / Last-Modified conditional requests
are out of scope here — that's PR 3 once the R1 probe (design §11 R1)
proves curl_cffi's ``impersonate`` template tolerates conditional
headers.

Invariants worth knowing about:
- ``url_hash`` mixes the profile dimension into the cache key (see
  ``canonical.url_hash``), so a profile-bound fetch of x.com can never
  replay a no-profile cache entry of the same URL (design §5.2 A2).
- Atomic writes use ``payloads/<sha1>.json.tmp`` → ``os.replace`` →
  ``INSERT OR REPLACE`` in SQLite. ``os.replace`` is cross-platform; on
  Windows it succeeds even when the target exists (design §13).
- GC uses its own connection so a long ``gc`` pass doesn't share a
  cursor with concurrent ``store`` (design §11 R2).
- All time reads go through ``time_ms()`` so tests can monkeypatch a
  fake clock without touching the OS clock.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import paths
from .canonical import canonicalize_url, url_hash
from .url_safety import etld1


def time_ms() -> int:
    """Unix time in milliseconds.

    All cache time reads go through this helper so a monkeypatched
    ``cache.time_ms`` advances the cache's notion of ``now`` for tests.
    Calling ``time.time()`` directly inside ``Cache`` methods would
    bypass the monkeypatch and pin tests to wall-clock timing.
    """
    return int(time.time() * 1000)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  url_hash         TEXT PRIMARY KEY,
  canonical_url    TEXT NOT NULL,
  profile          TEXT NOT NULL DEFAULT '',
  host             TEXT NOT NULL,
  etld1            TEXT NOT NULL,
  fetched_at       INTEGER NOT NULL,
  accessed_at      INTEGER NOT NULL,
  status_code      INTEGER NOT NULL,
  etag             TEXT,
  last_modified    TEXT,
  content_hash     TEXT NOT NULL,
  payload_bytes    INTEGER NOT NULL,
  dump_bytes       INTEGER DEFAULT 0,
  screenshot_bytes INTEGER DEFAULT 0,
  has_dump         INTEGER DEFAULT 0,
  has_screenshot   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_host ON entries(host);
CREATE INDEX IF NOT EXISTS idx_fetched_at ON entries(fetched_at);
CREATE INDEX IF NOT EXISTS idx_accessed_at ON entries(accessed_at);
CREATE INDEX IF NOT EXISTS idx_profile_host ON entries(profile, host);
"""


@dataclass
class CacheHit:
    url: str
    canonical_url: str
    profile: str
    fetched_at_ms: int
    age_ms: int
    status_code: int
    headers: dict
    markdown: str
    # Carried over from the fetch response so the router can rebuild a
    # complete success envelope on cache hit. Default empty so the
    # dataclass can still be constructed from a minimal payload (older
    # entries written before these fields were added still load).
    final_url: str = ""
    title: str = ""
    content_truncated: bool = False
    headings: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    dump_path: str | None = None
    screenshot_path: str | None = None
    content_hash: str = ""


@dataclass
class CacheStats:
    entry_count: int
    total_bytes: int
    payload_bytes: int
    dump_bytes: int
    screenshot_bytes: int
    hosts: int


@dataclass
class GCStats:
    deleted_entries: int
    freed_bytes: int


class Cache:
    """File + SQLite WAL cache. See ``docs/v0.3/design.md §5.2``."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else paths.CACHE_ROOT
        self.db_path = self.root / "index.sqlite"
        self.payloads_dir = self.root / "payloads"
        self.dumps_dir = self.root / "dumps"
        self.screenshots_dir = self.root / "screenshots"
        for d in (self.root, self.payloads_dir, self.dumps_dir, self.screenshots_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- connection helpers ----------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # WAL + 5 s busy timeout per design §11 R2. New connection per
        # public call so GC and ``store``/``lookup`` don't share a cursor.
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # -- lookup ----------------------------------------------------------

    def lookup(
        self, url: str, *, profile: str | None, max_age_ms: int | None,
    ) -> CacheHit | None:
        """Return the cache hit if ``age ≤ max_age_ms``. Touches
        ``accessed_at`` on the way back so LRU GC reflects use.

        ``max_age_ms is None`` is a defensive no-op — callers should
        short-circuit before reaching this branch (design §5.2).
        """
        if max_age_ms is None:
            return None
        hit = self._read_entry(url, profile=profile)
        if hit is None:
            return None
        now = time_ms()
        age = now - hit.fetched_at_ms
        if age > max_age_ms:
            return None
        hit.age_ms = age
        self._touch_db(url, profile=profile, now=now)
        return hit

    def lookup_for_revalidation(
        self, url: str, *, profile: str | None,
    ) -> CacheHit | None:
        """Age-agnostic read for the conditional-request path (PR 3).
        Does NOT touch ``accessed_at`` because the caller hasn't decided
        whether to keep the cached body yet — a 304 will commit; a 200
        will overwrite."""
        return self._read_entry(url, profile=profile)

    # -- store -----------------------------------------------------------

    def store(self, url: str, *, profile: str | None, response: dict) -> None:
        """Atomic write: payload tmp → ``os.replace`` → ``INSERT OR
        REPLACE`` into the index. Designed so a crash between the two
        steps leaves an orphan payload file (visible to ``gc(repair=)``
        in a later PR) rather than a half-written index row.

        ``response`` follows the Router's success-envelope shape (see
        ``router._success_from_http``). The dispatch handles both the
        v0.3 router shape (``metadata.status_code``, ``screenshots`` as
        a list of ``{stage, path}`` dicts) and the flatter shape used by
        the unit tests (``status_code`` top-level, ``screenshot_path``
        singular) so the cache module stays testable in isolation."""
        canonical = canonicalize_url(url)
        key = url_hash(canonical, profile=profile)
        now = time_ms()
        markdown = response.get("content", "") or ""
        metadata = response.get("metadata") or {}
        # Router puts status_code under metadata; tests pass it top-level.
        status_code = response.get("status_code")
        if status_code is None:
            status_code = metadata.get("status_code", 200)
        # Router has no top-level ``headers`` yet — PR 3 adds them for
        # ETag/Last-Modified. Until then headers is usually {}. Accept
        # either location so PR 3 can land without a payload-schema bump.
        headers = response.get("headers") or metadata.get("headers") or {}
        # Router exposes screenshots as a list of stage dicts; tests use
        # ``screenshot_path`` singular. Cache the final stage's path.
        screenshot_path = response.get("screenshot_path")
        if screenshot_path is None:
            screenshot_path = _final_screenshot_path(response.get("screenshots"))
        payload = {
            "url": url,
            "canonical_url": canonical,
            "profile": profile or None,
            "fetched_at": now,
            "status_code": status_code,
            "final_url": response.get("final_url") or url,
            "title": response.get("title") or "",
            "content_truncated": bool(response.get("content_truncated")),
            "headers": headers,
            "markdown": markdown,
            "headings": response.get("headings") or [],
            "metadata": metadata,
            "dump_path": response.get("dump_path"),
            "screenshot_path": screenshot_path,
            "content_hash": response.get("full_content_hash") or "",
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        payload_path = self.payloads_dir / f"{key}.json"
        tmp_path = payload_path.with_suffix(".json.tmp")
        try:
            tmp_path.write_bytes(encoded)
            os.replace(tmp_path, payload_path)
        except OSError:
            # Disk-full / permission error mid-write: leave no .tmp behind.
            # ``os.replace`` either succeeds atomically or leaves the source
            # intact, so the cleanup is only meaningful for the write step.
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

        host_value = _host(canonical)
        etld1_value = etld1(canonical) or host_value
        content_hash = hashlib.sha1(markdown.encode("utf-8")).hexdigest()
        dump_path = payload["dump_path"]
        dump_bytes = _file_size(dump_path) if dump_path else 0
        screenshot_bytes = _file_size(screenshot_path) if screenshot_path else 0

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO entries (
                  url_hash, canonical_url, profile, host, etld1,
                  fetched_at, accessed_at, status_code,
                  etag, last_modified, content_hash,
                  payload_bytes, dump_bytes, screenshot_bytes,
                  has_dump, has_screenshot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, canonical, profile or "", host_value, etld1_value,
                    now, now, payload["status_code"],
                    headers.get("etag"), headers.get("last-modified"),
                    content_hash,
                    len(encoded), dump_bytes, screenshot_bytes,
                    1 if dump_path else 0, 1 if screenshot_path else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # -- touch / delete --------------------------------------------------

    def touch(self, url: str, *, profile: str | None) -> None:
        self._touch_db(url, profile=profile, now=time_ms())

    def mark_revalidated(self, url: str, *, profile: str | None) -> int:
        """304 path (PR 3): the cached body is confirmed current as of now.
        Refresh BOTH ``accessed_at`` (LRU) and ``fetched_at`` (age) so the
        entry counts as fresh for subsequent ``max_age`` lookups. ``touch``
        deliberately bumps only ``accessed_at``; revalidation needs the
        stronger freshness reset, hence the separate method. Returns the
        timestamp written so the caller can report a ``cache_fetched_at_ms``
        that agrees with ``cache_age_ms == 0``."""
        canonical = canonicalize_url(url)
        key = url_hash(canonical, profile=profile)
        now = time_ms()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE entries SET accessed_at = ?, fetched_at = ? WHERE url_hash = ?",
                (now, now, key),
            )
            conn.commit()
        finally:
            conn.close()
        return now

    def _touch_db(self, url: str, *, profile: str | None, now: int) -> None:
        canonical = canonicalize_url(url)
        key = url_hash(canonical, profile=profile)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE entries SET accessed_at = ? WHERE url_hash = ?",
                (now, key),
            )
            conn.commit()
        finally:
            conn.close()

    def delete(self, url: str, *, profile: str | None) -> None:
        canonical = canonicalize_url(url)
        key = url_hash(canonical, profile=profile)
        # Read dump/screenshot side-paths from the payload BEFORE deleting
        # the JSON, otherwise we lose the reference and the files leak.
        dump_path, shot_path = self._read_payload_side_paths_one(key)
        conn = self._connect()
        try:
            conn.execute("DELETE FROM entries WHERE url_hash = ?", (key,))
            conn.commit()
        finally:
            conn.close()
        self._unlink_payload(key)
        self._unlink_side_files(dump_path, shot_path)

    # -- gc / stats / legacy ---------------------------------------------

    def gc(
        self, *, max_total_bytes: int | None = None,
        older_than_ms: int | None = None, host: str | None = None,
    ) -> GCStats:
        """One of three modes per call:

          older_than_ms : delete entries with fetched_at < now - older_than_ms
          host          : delete entries on a host or eTLD+1
          max_total_bytes : LRU drain to 80 % watermark when total bytes
                            exceed the cap

        Uses its own connection so a long GC pass doesn't share a cursor
        with concurrent ``store`` calls (design §11 R2)."""
        conn = self._connect()
        try:
            if older_than_ms is not None:
                cutoff = time_ms() - older_than_ms
                deleted, freed = self._delete_where(
                    conn, "fetched_at < ?", (cutoff,),
                )
            elif host is not None:
                deleted, freed = self._delete_where(
                    conn, "host = ? OR etld1 = ?", (host, host),
                )
            elif max_total_bytes is not None:
                deleted, freed = self._gc_lru(conn, max_total_bytes)
            else:
                deleted, freed = 0, 0
        finally:
            conn.close()
        return GCStats(deleted_entries=deleted, freed_bytes=freed)

    def clear_all(self) -> GCStats:
        """Delete every entry and its side files — the explicit full-wipe
        behind ``cache clear --all``. Distinct from ``gc()``'s scoped modes:
        a full wipe is deliberate, never a default. Reuses ``_delete_where``
        with an always-true predicate so side-file cleanup stays in one place."""
        conn = self._connect()
        try:
            deleted, freed = self._delete_where(conn, "1=1", ())
        finally:
            conn.close()
        return GCStats(deleted_entries=deleted, freed_bytes=freed)

    def _delete_where(
        self, conn: sqlite3.Connection, where: str, params: tuple,
    ) -> tuple[int, int]:
        rows = conn.execute(
            f"SELECT url_hash, payload_bytes, dump_bytes, screenshot_bytes "
            f"FROM entries WHERE {where}",
            params,
        ).fetchall()
        if not rows:
            return 0, 0
        keys = [r[0] for r in rows]
        side = self._read_payload_side_paths(keys)
        total = 0
        for (key, pbytes, dbytes, sbytes), (dump_path, shot_path) in zip(rows, side):
            total += int(pbytes or 0) + int(dbytes or 0) + int(sbytes or 0)
            self._unlink_payload(key)
            self._unlink_side_files(dump_path, shot_path)
        conn.execute(f"DELETE FROM entries WHERE {where}", params)
        conn.commit()
        return len(rows), total

    def _gc_lru(
        self, conn: sqlite3.Connection, max_total_bytes: int,
    ) -> tuple[int, int]:
        cur_total = conn.execute(
            "SELECT COALESCE(SUM(payload_bytes + dump_bytes + screenshot_bytes), 0) "
            "FROM entries"
        ).fetchone()[0]
        if cur_total <= max_total_bytes:
            return 0, 0
        target = int(max_total_bytes * 0.8)
        rows = conn.execute(
            "SELECT url_hash, payload_bytes, dump_bytes, screenshot_bytes "
            "FROM entries ORDER BY accessed_at ASC"
        ).fetchall()
        deleted_keys: list[str] = []
        freed = 0
        for key, pbytes, dbytes, sbytes in rows:
            row_bytes = int(pbytes or 0) + int(dbytes or 0) + int(sbytes or 0)
            deleted_keys.append(key)
            freed += row_bytes
            if cur_total - freed <= target:
                break
        if not deleted_keys:
            return 0, 0
        # Files BEFORE DB commit, matching ``_delete_where``. The reverse
        # order races with a concurrent ``store()`` for one of the keys:
        # store can write a fresh payload between the commit and the
        # unlink loop, and then the loop deletes the freshly written
        # file. The files-first order can at worst leave an orphan
        # payload (which ``lookup`` already treats as a miss), never a
        # phantom DB row pointing at missing bytes.
        side = self._read_payload_side_paths(deleted_keys)
        for key, (dump_path, shot_path) in zip(deleted_keys, side):
            self._unlink_payload(key)
            self._unlink_side_files(dump_path, shot_path)
        placeholders = ",".join("?" * len(deleted_keys))
        conn.execute(
            f"DELETE FROM entries WHERE url_hash IN ({placeholders})",
            deleted_keys,
        )
        conn.commit()
        return len(deleted_keys), freed

    def stats(self) -> CacheStats:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(payload_bytes), 0),
                       COALESCE(SUM(dump_bytes), 0),
                       COALESCE(SUM(screenshot_bytes), 0),
                       COUNT(DISTINCT host)
                FROM entries
                """
            ).fetchone()
        finally:
            conn.close()
        entry_count, pbytes, dbytes, sbytes, hosts = row
        return CacheStats(
            entry_count=int(entry_count),
            total_bytes=int(pbytes + dbytes + sbytes),
            payload_bytes=int(pbytes),
            dump_bytes=int(dbytes),
            screenshot_bytes=int(sbytes),
            hosts=int(hosts),
        )

    def legacy_dumps_usage(self) -> int:
        """Total bytes under the v0.2 ``~/.lightcrawl/dumps/`` directory.
        Returned by ``lightcrawl cache stats`` (PR 2.4) so users see
        their pre-v0.3 dumps and can ``rm`` the legacy dir manually.
        Returns 0 if the legacy directory doesn't exist."""
        legacy = paths.DUMPS
        if not legacy.exists():
            return 0
        return sum(
            p.stat().st_size for p in legacy.rglob("*") if p.is_file()
        )

    # -- internals -------------------------------------------------------

    def _read_entry(
        self, url: str, *, profile: str | None,
    ) -> CacheHit | None:
        canonical = canonicalize_url(url)
        key = url_hash(canonical, profile=profile)
        payload_path = self.payloads_dir / f"{key}.json"
        if not payload_path.exists():
            return None
        # The index is authoritative: an orphan payload (write crashed
        # between os.replace and INSERT) must be invisible to lookups
        # until a future ``gc(repair=True)`` reconciles. (design §5.2)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT fetched_at FROM entries WHERE url_hash = ?", (key,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        # ValueError/TypeError catch fields a corrupted payload — e.g.
        # ``status_code = "ok"`` (the JSON loaded fine but int() blows up)
        # or a non-dict ``headers``. A corrupted payload is treated as a
        # cache miss; the next ``store`` will overwrite it cleanly.
        try:
            data = json.loads(payload_path.read_text(encoding="utf-8"))
            return CacheHit(
                url=data.get("url", url),
                canonical_url=data.get("canonical_url", canonical),
                profile=data.get("profile") or "",
                fetched_at_ms=int(row[0]),
                age_ms=0,  # filled in by ``lookup``; revalidation path leaves 0
                status_code=int(data.get("status_code", 200)),
                headers=data.get("headers") or {},
                markdown=data.get("markdown", ""),
                final_url=data.get("final_url") or data.get("url", url),
                title=data.get("title") or "",
                content_truncated=bool(data.get("content_truncated")),
                headings=data.get("headings") or [],
                metadata=data.get("metadata") or {},
                dump_path=data.get("dump_path"),
                screenshot_path=data.get("screenshot_path"),
                content_hash=data.get("content_hash") or "",
            )
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return None


    # -- side-file helpers (dump/screenshot cleanup) ---------------------

    def _read_payload_side_paths(
        self, keys: list[str],
    ) -> list[tuple[str | None, str | None]]:
        """For each ``url_hash`` key, peek into the payload JSON to recover
        the dump/screenshot file paths so deletion paths can clean them
        up. The DB doesn't track these paths — only their byte counts —
        so the payload JSON is the only place the path is recorded.

        Returns ``(None, None)`` for any missing or unreadable payload.
        Side files outside ``self.dumps_dir`` / ``self.screenshots_dir``
        are returned unchanged; ``_unlink_side_files`` confines the
        actual unlink to those directories.
        """
        out: list[tuple[str | None, str | None]] = []
        for key in keys:
            out.append(self._read_payload_side_paths_one(key))
        return out

    def _read_payload_side_paths_one(
        self, key: str,
    ) -> tuple[str | None, str | None]:
        payload_path = self.payloads_dir / f"{key}.json"
        if not payload_path.exists():
            return None, None
        try:
            data = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        return data.get("dump_path"), data.get("screenshot_path")

    def _unlink_payload(self, key: str) -> None:
        payload = self.payloads_dir / f"{key}.json"
        if payload.exists():
            try:
                payload.unlink()
            except OSError:
                pass

    def _unlink_side_files(
        self, dump_path: str | None, screenshot_path: str | None,
    ) -> None:
        """Unlink dump and screenshot files, but ONLY when they live
        under this cache's ``dumps_dir`` / ``screenshots_dir``. A v0.2
        dump path under ``~/.lightcrawl/dumps/`` belongs to the legacy
        directory and is the user's to manage, so we skip it."""
        self._unlink_if_under(dump_path, self.dumps_dir)
        self._unlink_if_under(screenshot_path, self.screenshots_dir)

    def _unlink_if_under(self, path: str | None, root: Path) -> None:
        if not path:
            return
        try:
            resolved = Path(path).resolve()
            root_resolved = root.resolve()
        except OSError:
            return
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            return  # not under this cache's directory; not ours to delete
        if resolved.exists():
            try:
                resolved.unlink()
            except OSError:
                pass


def _host(canonical_url: str) -> str:
    return (urlparse(canonical_url).hostname or "").lower()


def _file_size(path: str | None) -> int:
    if not path:
        return 0
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _final_screenshot_path(screenshots: object) -> str | None:
    """Extract the ``path`` of the final screenshot from the Router's
    ``screenshots`` list (``[{stage: "action"|"final", path, ...}, ...]``).
    Prefers the ``stage="final"`` entry; falls back to the last entry.
    Returns None if the list is empty or malformed."""
    if not isinstance(screenshots, list) or not screenshots:
        return None
    for entry in screenshots:
        if isinstance(entry, dict) and entry.get("stage") == "final":
            path = entry.get("path")
            return path if isinstance(path, str) else None
    last = screenshots[-1]
    if isinstance(last, dict):
        path = last.get("path")
        return path if isinstance(path, str) else None
    return None
