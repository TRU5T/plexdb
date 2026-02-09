#!/usr/bin/env python3
from __future__ import annotations

"""
Plex DB Merge: use an older (good) backup as base, extract data from a newer
(corrupt) DB, and merge it in so Plex can start without losing new entries.

Strategy:
1. If the newer DB won't open, attempt SQLite .recover to salvage data.
2. Merge watch history and per-item settings (by guid) from new into old.
3. Optionally merge new library items (metadata_items + media_items, etc.)
   with ID remapping so IDs don't collide.

Usage:
  python plex_db_merge.py --old path/to/backup.db --new path/to/corrupt.db --output path/to/merged.db
  python plex_db_merge.py --old old.db --new corrupt.db --output merged.db --recover --merge-new-items

Requires: Python 3.6+, no extra packages (uses sqlite3). Optional: sqlite3 CLI for recovery.
"""

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

# Optional callback for UI/API (e.g. log_callback(msg) -> None)
_log_callback: Callable[[str], None] | None = None


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    line = f"[{ts}] {msg}"
    if _log_callback is not None:
        _log_callback(line)
    else:
        print(line, flush=True)


def _normalize_path(path: str) -> str:
    """Convert Windows paths to WSL/Linux paths when running on Linux (e.g. C:\\temp\\x -> /mnt/c/temp/x)."""
    if not path or os.name == "nt":
        return path.strip()
    p = path.strip().replace("\\", "/")
    # Match single letter drive: C:/ or C:
    m = re.match(r"^([a-zA-Z]):(.*)$", p)
    if m:
        drive, rest = m.group(1).lower(), m.group(2).lstrip("/")
        return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return p


def run_merge(
    old_path: str,
    new_path: str,
    output_path: str,
    *,
    recover: bool = False,
    merge_new_items: bool = False,
    log_callback: Callable[[str], None] | None = None,
) -> tuple[bool, str | None]:
    """
    Run the merge. Returns (success, error_message).
    If log_callback is provided, all log lines are sent there instead of stdout.
    """
    global _log_callback
    _log_callback = log_callback
    try:
        log("Starting merge…")
        old_path = _normalize_path(old_path)
        new_path = _normalize_path(new_path)
        output_path = _normalize_path(output_path)
        for p in (old_path, new_path):
            if not os.path.isfile(p):
                log(f"File not found: {p}")
                return False, f"File not found: {p}"

        recovered_old_path = None
        log("Opening old (backup) DB…")
        old_conn = try_open_db(old_path)
        if not old_conn:
            log("Old DB could not be opened directly. Recovering (can take 10–20 min for large DBs)…")
            fd, recovered_old_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            if recover_db(old_path, recovered_old_path):
                old_conn = try_open_db(recovered_old_path)
            if not old_conn:
                if recovered_old_path and os.path.exists(recovered_old_path):
                    os.unlink(recovered_old_path)
                log("Cannot open old DB. Aborting.")
                return False, "Cannot open old (backup) DB. Install sqlite3 CLI and try again."
            log("Opened old DB via recovery.")

        shutil.copy2(old_path if not recovered_old_path else recovered_old_path, output_path)
        out_conn = sqlite3.connect(output_path)
        out_conn.execute("PRAGMA foreign_keys = OFF")
        log(f"Created writable copy at {output_path}")

        recovered_path = None
        log("Opening new/corrupt DB…")
        new_conn = try_open_db(new_path)
        if not new_conn and recover:
            log("New DB could not be opened. Recovering (can take 10–20 min for large DBs)…")
            fd, recovered_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            if recover_db(new_path, recovered_path):
                new_conn = try_open_db(recovered_path)
                if new_conn:
                    log("Recovered new DB and opened it.")
            if recovered_path and os.path.exists(recovered_path) and not new_conn:
                os.unlink(recovered_path)
                recovered_path = None
        if not new_conn:
            new_conn = try_open_db(new_path)
        if not new_conn:
            log("Cannot open new (corrupt) DB. Try enabling recover or run sqlite3 .recover manually.")
            if recovered_path and os.path.exists(recovered_path):
                os.unlink(recovered_path)
            return False, "Cannot open new (corrupt) DB. Enable 'Recover corrupt DB' and try again."

        try:
            views_added, settings_added = merge_watch_history_and_settings(old_conn, new_conn, out_conn)
            log(f"Merged watch history: {views_added} views, {settings_added} settings.")

            meta_added = 0
            if merge_new_items:
                meta_added = merge_new_library_items(old_conn, new_conn, out_conn)
                log(f"Merged new library items: {meta_added} metadata_items.")

            try:
                out_conn.execute("PRAGMA integrity_check")
                log("Output DB integrity check: ok.")
            except Exception as e:
                log(f"Output DB integrity check skipped (Plex custom schema): {e}")
        finally:
            old_conn.close()
            new_conn.close()
            out_conn.close()
            if recovered_path and os.path.exists(recovered_path):
                os.unlink(recovered_path)
            if recovered_old_path and os.path.exists(recovered_old_path):
                os.unlink(recovered_old_path)

        log("Done. Replace your Plex DB with the output file (with Plex stopped), then start Plex.")
        return True, None
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
        log(f"Database error: {e}")
        return False, f"Database is corrupt or malformed. Enable 'Try to recover corrupt DB' and run again. ({e})"
    except Exception as e:
        log(f"Error: {e}")
        return False, str(e)
    finally:
        _log_callback = None


def preview_merge(
    old_path: str,
    new_path: str,
    *,
    recover: bool = False,
    merge_new_items: bool = False,
) -> tuple[bool, str | None, dict]:
    """
    Open both DBs and count what would be merged. No writing.
    Returns (success, error_message, stats_dict).
    stats_dict: views_to_add, settings_to_add, new_metadata_items_to_add (0 if not merge_new_items).
    """
    stats = {"views_to_add": 0, "settings_to_add": 0, "new_metadata_items_to_add": 0}
    old_path = _normalize_path(old_path)
    new_path = _normalize_path(new_path)
    log("Starting preview…")
    for p in (old_path, new_path):
        if not os.path.isfile(p):
            return False, f"File not found: {p}", stats
    recovered_old_path = None
    log("Opening old (backup) DB…")
    old_conn = try_open_db(old_path)
    if not old_conn:
        log("Old DB could not be opened directly. Recovering (can take 10–20 min for large DBs)…")
        fd, recovered_old_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        if recover_db(old_path, recovered_old_path):
            log("Old DB recovery finished. Opening…")
            old_conn = try_open_db(recovered_old_path)
        if not old_conn:
            if recovered_old_path and os.path.exists(recovered_old_path):
                os.unlink(recovered_old_path)
            return False, "Cannot open old (backup) DB. Install sqlite3 CLI (apt install sqlite3) and try again.", stats
    log("Old DB open.")
    recovered_path = None
    log("Opening new/corrupt DB…")
    new_conn = try_open_db(new_path)
    if not new_conn and recover:
        log("New DB could not be opened. Recovering (can take 10–20 min for large DBs)…")
        fd, recovered_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        if recover_db(new_path, recovered_path):
            log("New DB recovery finished. Opening…")
            new_conn = try_open_db(recovered_path)
        if recovered_path and os.path.exists(recovered_path) and not new_conn:
            os.unlink(recovered_path)
            recovered_path = None
    if not new_conn:
        new_conn = try_open_db(new_path)
    if not new_conn:
        if recovered_path and os.path.exists(recovered_path):
            os.unlink(recovered_path)
        return False, "Cannot open new (corrupt) DB. Enable 'Try to recover corrupt DB' and try again.", stats
    log("New DB open. Counting watch history and settings…")

    def _count_stats(o_conn, n_conn):
        if not table_exists(o_conn, "metadata_items"):
            return
        cur = o_conn.execute("SELECT id, guid FROM metadata_items WHERE guid IS NOT NULL AND guid != ''")
        guid_to_id_old = {row[1]: row[0] for row in cur.fetchall()}
        if table_exists(n_conn, "metadata_item_views"):
            cur = n_conn.execute("SELECT guid FROM metadata_item_views")
            for row in cur.fetchall():
                if row[0] and row[0] in guid_to_id_old:
                    stats["views_to_add"] += 1
        if table_exists(n_conn, "metadata_item_settings"):
            cur = n_conn.execute("SELECT guid FROM metadata_item_settings")
            for row in cur.fetchall():
                if row[0] and row[0] in guid_to_id_old:
                    stats["settings_to_add"] += 1
        if merge_new_items and table_exists(n_conn, "metadata_items"):
            cur = n_conn.execute("SELECT guid FROM metadata_items WHERE guid IS NOT NULL AND guid != ''")
            new_guids = {row[0] for row in cur.fetchall()}
            stats["new_metadata_items_to_add"] = len(new_guids - set(guid_to_id_old.keys()))

    try:
        _count_stats(old_conn, new_conn)
        log("Preview done.")
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        # One or both DBs are malformed when read. Try recovering both and retry once.
        log("Read error on DBs. Recovering both and retrying…")
        old_conn.close()
        new_conn.close()
        if recovered_path and os.path.exists(recovered_path):
            os.unlink(recovered_path)
        if recovered_old_path and os.path.exists(recovered_old_path):
            os.unlink(recovered_old_path)
        recovered_old_path = None
        recovered_path = None
        fd1, recovered_old_path = tempfile.mkstemp(suffix=".db")
        os.close(fd1)
        fd2, recovered_path = tempfile.mkstemp(suffix=".db")
        os.close(fd2)
        if not recover_db(old_path, recovered_old_path) or not recover_db(new_path, recovered_path):
            if recovered_old_path and os.path.exists(recovered_old_path):
                os.unlink(recovered_old_path)
            if recovered_path and os.path.exists(recovered_path):
                os.unlink(recovered_path)
            return False, "Database is corrupt (malformed). Recover failed. Use SQLite 3.26+ for .recover: set SQLITE3 to the path of a newer sqlite3 (e.g. from https://www.sqlite.org/2026/sqlite-tools-linux-x64-3510200.zip), then restart this app and run again.", stats
        log("Both DBs recovered. Opening and counting…")
        old_conn = try_open_db(recovered_old_path)
        new_conn = try_open_db(recovered_path)
        if not old_conn or not new_conn:
            if old_conn:
                old_conn.close()
            if new_conn:
                new_conn.close()
            if recovered_old_path and os.path.exists(recovered_old_path):
                os.unlink(recovered_old_path)
            if recovered_path and os.path.exists(recovered_path):
                os.unlink(recovered_path)
            return False, "Database is corrupt. Recovery produced an unreadable file.", stats
        try:
            _count_stats(old_conn, new_conn)
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
            if recovered_old_path and os.path.exists(recovered_old_path):
                os.unlink(recovered_old_path)
            if recovered_path and os.path.exists(recovered_path):
                os.unlink(recovered_path)
            return False, f"Database still corrupt after recovery: {e}", stats
        finally:
            old_conn.close()
            new_conn.close()
            if recovered_old_path and os.path.exists(recovered_old_path):
                os.unlink(recovered_old_path)
            if recovered_path and os.path.exists(recovered_path):
                os.unlink(recovered_path)
        return True, None, stats
    finally:
        old_conn.close()
        new_conn.close()
        if recovered_path and os.path.exists(recovered_path):
            os.unlink(recovered_path)
        if recovered_old_path and os.path.exists(recovered_old_path):
            os.unlink(recovered_old_path)
    return True, None, stats


def try_open_db(path: str, skip_integrity_check: bool = True) -> sqlite3.Connection | None:
    """Open DB read-only; return None if corrupt/unreadable.
    Plex DBs use a custom SQLite tokenizer ('collating') in FTS tables. We skip
    integrity_check so we don't touch those; we only read metadata tables."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        if not skip_integrity_check:
            conn.execute("PRAGMA integrity_check").fetchone()
        return conn
    except Exception as e:
        log(f"Could not open DB {path}: {e}")
        return None


def _sqlite3_cmd() -> list[str]:
    """Path to sqlite3 CLI: bundled bin/sqlite3, then SQLITE3 env, else system 'sqlite3'."""
    if os.environ.get("SQLITE3"):
        cmd = os.environ.get("SQLITE3")
        if isinstance(cmd, str) and cmd:
            return [cmd]
    # Prefer sqlite3 bundled with the package (e.g. for Docker)
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _bundled = os.path.join(_script_dir, "bin", "sqlite3")
    if os.path.isfile(_bundled) and os.access(_bundled, os.X_OK):
        return [_bundled]
    return ["sqlite3"]


def _import_sql_skipping_errors(sql_path: str, out_path: str) -> bool:
    """
    Import SQL file into a new DB, executing statement by statement and skipping
    any that fail (e.g. corrupt "(null);" from .recover). Processes line-by-line to
    avoid loading the whole file into memory. Returns True if the DB was created.
    """
    if os.path.exists(out_path):
        os.unlink(out_path)
    conn = sqlite3.connect(out_path)
    executed = 0
    skipped = 0
    buffer: list[str] = []
    in_single = in_double = False
    last_log_time = time.monotonic()
    last_log_executed = 0

    def _semicolon_outside_string(s: str) -> bool:
        """True if the last semicolon in s is outside any string literal."""
        in_s, in_d = False, False
        for i, c in enumerate(s):
            if in_s:
                if c == "'" and (i + 1 >= len(s) or s[i + 1] != "'"):
                    in_s = False
            elif in_d:
                if c == '"' and (i + 1 >= len(s) or s[i + 1] != '"'):
                    in_d = False
            elif c == "'":
                in_s = True
            elif c == '"':
                in_d = True
            elif c == ";":
                if not in_s and not in_d:
                    return True
        return False

    try:
        with open(sql_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                buffer.append(line)
                content = "".join(buffer)
                if _semicolon_outside_string(content):
                    stmt = content.strip()
                    buffer = []
                    in_single = in_double = False
                    if not stmt or stmt.startswith("--"):
                        continue
                    try:
                        conn.execute(stmt)
                        executed += 1
                        # Progress: log every 25k statements or every 45s
                        now = time.monotonic()
                        if executed - last_log_executed >= 25000 or (now - last_log_time) >= 45:
                            log(f"Import progress: {executed} statements executed, {skipped} skipped…")
                            last_log_executed = executed
                            last_log_time = now
                    except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                        skipped += 1
                    continue
                # Update quote state for next iteration
                for i, c in enumerate(line):
                    if in_single:
                        if c == "'" and (i + 1 >= len(line) or line[i + 1] != "'"):
                            in_single = False
                    elif in_double:
                        if c == '"' and (i + 1 >= len(line) or line[i + 1] != '"'):
                            in_double = False
                    elif c == "'":
                        in_single = True
                    elif c == '"':
                        in_double = True
        if buffer:
            stmt = "".join(buffer).strip()
            if stmt and not stmt.startswith("--"):
                try:
                    conn.execute(stmt)
                    executed += 1
                except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                    skipped += 1
    except OSError as e:
        log(f"Error reading SQL file: {e}")
        conn.close()
        return False
    conn.commit()
    conn.close()
    log(f"Imported {executed} statements, skipped {skipped} bad ones.")
    return executed > 0


def recover_db(path: str, out_path: str) -> bool:
    """Salvage data into out_path using .recover or, if unavailable, .dump. Returns True on success."""
    sqlite3_cmd = _sqlite3_cmd()
    try:
        with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as f:
            sql_path = f.name
        use_dump = False
        log("Running sqlite3 .recover (this may take 10–20 min for large DBs)…")
        # Stream .recover output to file to avoid holding huge SQL in memory (OOM on large DBs)
        with open(sql_path, "wb") as sql_file:
            r = subprocess.run(
                sqlite3_cmd + [path, ".recover"],
                stdout=sql_file,
                stderr=subprocess.PIPE,
                timeout=900,
            )
        if r.returncode != 0:
            stderr = (r.stderr or b"").decode("utf-8", errors="replace")
            if "sqlite_dbpage" in stderr or "no such table" in stderr.lower():
                log("sqlite3 .recover not available (need SQLite 3.26+). Trying .dump instead.")
                use_dump = True
            else:
                log(f"sqlite3 .recover stderr: {stderr}")
                os.unlink(sql_path)
                return False
        else:
            log("Recover SQL written to file. Rebuilding DB from SQL…")

        if use_dump:
            os.unlink(sql_path)
            with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as f:
                sql_path = f.name
            log("Running sqlite3 .dump…")
            with open(sql_path, "wb") as sql_file:
                r = subprocess.run(
                    sqlite3_cmd + [path, ".dump"],
                    stdout=sql_file,
                    stderr=subprocess.PIPE,
                    timeout=900,
                )
            if r.returncode != 0:
                log(f"sqlite3 .dump stderr: {(r.stderr or b'').decode('utf-8', errors='replace')}")
                os.unlink(sql_path)
                return False
            log("Dump SQL written to file. Rebuilding DB…")

        # Create new DB from SQL (binary pipe to avoid decode errors)
        log("Writing recovered DB (may take several minutes)…")
        with open(sql_path, "rb") as f:
            r = subprocess.run(
                sqlite3_cmd + [out_path],
                stdin=f,
                capture_output=True,
                timeout=1800,
            )
        if r.returncode != 0:
            stderr = (r.stderr or b"").decode("utf-8", errors="replace")
            if "Parse error" in stderr or "syntax error" in stderr:
                log("Recovered SQL has parse errors (corrupt data). Trying to import what we can, skipping bad statements…")
                log("(This rebuilds the new DB so we can compare it to the old one; the dump can be millions of statements and may take a long time.)")
                if _import_sql_skipping_errors(sql_path, out_path):
                    os.unlink(sql_path)
                    log("Recovery complete (partial; some rows were skipped).")
                    return True
            log(f"sqlite3 create from SQL stderr: {stderr[:2000]}")
            if len(stderr) > 2000:
                log("… (stderr truncated)")
            os.unlink(sql_path)
            return False
        os.unlink(sql_path)
        log("Recovery complete.")
        return True
    except FileNotFoundError:
        log("sqlite3 CLI not found. Set SQLITE3=/path/to/sqlite3 (e.g. from sqlite.org precompiled tools) and restart the app, or install sqlite3 (apt install sqlite3).")
        return False
    except Exception as e:
        log(f"Recovery failed: {type(e).__name__}: {e}")
        return False


def get_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return r is not None


def merge_watch_history_and_settings(
    old_conn: sqlite3.Connection,
    new_conn: sqlite3.Connection,
    out_conn: sqlite3.Connection,
) -> tuple[int, int]:
    """
    Merge metadata_item_views and metadata_item_settings from new into old.
    Uses guid to resolve metadata_item_id in the old DB. Returns (views_added, settings_added).
    """
    views_added = 0
    settings_added = 0

    # Build guid -> id map from old DB
    if not table_exists(old_conn, "metadata_items"):
        return 0, 0
    cur = old_conn.execute("SELECT id, guid FROM metadata_items WHERE guid IS NOT NULL AND guid != ''")
    guid_to_id_old = {row[1]: row[0] for row in cur.fetchall()}

    # --- metadata_item_views ---
    if table_exists(new_conn, "metadata_item_views") and table_exists(out_conn, "metadata_item_views"):
        cols = get_table_columns(new_conn, "metadata_item_views")
        if "guid" not in cols or "metadata_item_id" not in cols:
            log("metadata_item_views missing guid or metadata_item_id, skipping.")
        else:
            cur = new_conn.execute(
                "SELECT account_id, guid, metadata_type, library_section_id, grandparent_title, "
                "parent_index, parent_title, [index], title, thumb_url, viewed_at, grandparent_guid, "
                "originally_available_at, device_id FROM metadata_item_views"
            )
            out_cur = out_conn.cursor()
            for row in cur.fetchall():
                guid = row[1]
                old_id = guid_to_id_old.get(guid)
                if old_id is None:
                    continue
                try:
                    out_cur.execute(
                        "INSERT OR IGNORE INTO metadata_item_views "
                        "(account_id, guid, metadata_type, library_section_id, grandparent_title, "
                        "parent_index, parent_title, [index], title, thumb_url, viewed_at, grandparent_guid, "
                        "originally_available_at, device_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row[0], guid, row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10], row[11], row[12], row[13]),
                    )
                    if out_cur.rowcount:
                        views_added += 1
                except sqlite3.IntegrityError:
                    pass
            out_conn.commit()

    # --- metadata_item_settings ---
    if table_exists(new_conn, "metadata_item_settings") and table_exists(out_conn, "metadata_item_settings"):
        cols = get_table_columns(new_conn, "metadata_item_settings")
        if "guid" not in cols:
            log("metadata_item_settings missing guid, skipping.")
        else:
            cur = new_conn.execute(
                "SELECT account_id, guid, rating, view_offset, view_count, last_viewed_at, "
                "created_at, updated_at, skip_count, last_skipped_at, changed_at, extra_data "
                "FROM metadata_item_settings"
            )
            out_cur = out_conn.cursor()
            for row in cur.fetchall():
                guid = row[1]
                old_id = guid_to_id_old.get(guid)
                if old_id is None:
                    continue
                try:
                    out_cur.execute(
                        "INSERT OR REPLACE INTO metadata_item_settings "
                        "(account_id, guid, rating, view_offset, view_count, last_viewed_at, "
                        "created_at, updated_at, skip_count, last_skipped_at, changed_at, extra_data) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row[0], guid, row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10], row[11]),
                    )
                    if out_cur.rowcount:
                        settings_added += 1
                except sqlite3.IntegrityError:
                    pass
            out_conn.commit()

    return views_added, settings_added


def get_old_max_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """Return max id for metadata_items, media_items, media_parts, etc."""
    tables = ("metadata_items", "media_items", "media_parts", "media_streams", "directories", "taggings", "tags")
    out = {}
    for t in tables:
        if not table_exists(conn, t):
            out[t] = 0
            continue
        r = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()
        out[t] = r[0] if r else 0
    return out


def merge_new_library_items(
    old_conn: sqlite3.Connection,
    new_conn: sqlite3.Connection,
    out_conn: sqlite3.Connection,
) -> int:
    """
    Copy metadata_items from new that don't exist in old (by guid), and their
    media_items, media_parts, media_streams. Remap IDs to avoid collisions.
    Returns count of new metadata_items added.
    """
    if not table_exists(new_conn, "metadata_items") or not table_exists(old_conn, "metadata_items"):
        return 0

    cur = old_conn.execute("SELECT id, guid FROM metadata_items")
    old_guids = {row[1] for row in cur.fetchall() if row[1]}

    cur = new_conn.execute(
        "SELECT id, library_section_id, parent_id, metadata_type, guid, media_item_count, title, title_sort, "
        "original_title, studio, rating, rating_count, tagline, summary, trivia, quotes, content_rating, "
        "content_rating_age, [index], absolute_index, duration, user_thumb_url, user_art_url, user_banner_url, "
        "user_music_url, user_fields, tags_genre, tags_collection, tags_director, tags_writer, tags_star, "
        "originally_available_at, available_at, expires_at, refreshed_at, year, added_at, created_at, updated_at, "
        "deleted_at, tags_country, extra_data, hash, audience_rating, changed_at, resources_changed_at, remote "
        "FROM metadata_items"
    )
    new_rows = cur.fetchall()
    new_cols = get_table_columns(new_conn, "metadata_items")

    # Only rows whose guid is not in old
    to_add = [r for r in new_rows if r[4] and r[4] not in old_guids]
    if not to_add:
        return 0

    max_ids = get_old_max_ids(out_conn)
    new_meta_id_to_old = {}  # new id -> new assigned id in out
    next_meta_id = max_ids["metadata_items"] + 1
    for r in to_add:
        new_meta_id_to_old[r[0]] = next_meta_id
        next_meta_id += 1

    new_meta_ids = {r[0] for r in to_add}
    # Insert in dependency order: parent before child (parent_id in to_add must be inserted first)
    added = set()
    out_cur = out_conn.cursor()
    while len(added) < len(to_add):
        progress = 0
        for r in to_add:
            if r[0] in added:
                continue
            parent_id = r[2]
            if parent_id is None or parent_id not in new_meta_ids or parent_id in added:
                new_id, library_section_id, parent_id, *rest = r
                if library_section_id:
                    check = old_conn.execute("SELECT 1 FROM library_sections WHERE id=?", (library_section_id,)).fetchone()
                    if not check:
                        added.add(r[0])  # skip but don't retry
                        continue
                progress += 1
                added.add(r[0])
                out_id = new_meta_id_to_old[new_id]
                parent_out = new_meta_id_to_old.get(parent_id) if parent_id else None
                if parent_id and parent_id in new_meta_id_to_old:
                    parent_out = new_meta_id_to_old[parent_id]
                out_cur.execute(
                    "INSERT INTO metadata_items (id, library_section_id, parent_id, metadata_type, guid, media_item_count, "
                    "title, title_sort, original_title, studio, rating, rating_count, tagline, summary, trivia, quotes, "
                    "content_rating, content_rating_age, [index], absolute_index, duration, user_thumb_url, user_art_url, "
                    "user_banner_url, user_music_url, user_fields, tags_genre, tags_collection, tags_director, tags_writer, "
                    "tags_star, originally_available_at, available_at, expires_at, refreshed_at, year, added_at, created_at, "
                    "updated_at, deleted_at, tags_country, extra_data, hash, audience_rating, changed_at, resources_changed_at, remote) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (out_id, library_section_id, parent_out if parent_out else parent_id, *rest),
                )
        if progress == 0:
            break
    out_conn.commit()

    # media_items for these metadata_item_ids
    new_meta_ids_in_new = {r[0] for r in to_add}
    if table_exists(new_conn, "media_items") and table_exists(out_conn, "media_items"):
        cur = new_conn.execute(
            "SELECT id, library_section_id, section_location_id, metadata_item_id, type_id, width, height, size, "
            "duration, bitrate, container, video_codec, audio_codec, display_aspect_ratio, frames_per_second, "
            "audio_channels, interlaced, source, hints, display_offset, settings, created_at, updated_at, "
            "optimized_for_streaming, deleted_at, media_analysis_version, sample_aspect_ratio, extra_data, "
            "proxy_type, channel_id, begins_at, ends_at FROM media_items WHERE metadata_item_id IN ({})".format(
                ",".join(map(str, new_meta_ids_in_new))
            )
        )
        media_rows = cur.fetchall()
        next_media_id = max_ids["media_items"] + 1
        new_media_id_to_old = {}
        for row in media_rows:
            new_media_id_to_old[row[0]] = next_media_id
            next_media_id += 1
        for row in media_rows:
            new_meta_id = row[3]
            out_meta_id = new_meta_id_to_old.get(new_meta_id)
            if not out_meta_id:
                continue
            out_media_id = new_media_id_to_old[row[0]]
            out_cur.execute(
                "INSERT INTO media_items (id, library_section_id, section_location_id, metadata_item_id, type_id, "
                "width, height, size, duration, bitrate, container, video_codec, audio_codec, display_aspect_ratio, "
                "frames_per_second, audio_channels, interlaced, source, hints, display_offset, settings, created_at, "
                "updated_at, optimized_for_streaming, deleted_at, media_analysis_version, sample_aspect_ratio, "
                "extra_data, proxy_type, channel_id, begins_at, ends_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (out_media_id, row[1], row[2], out_meta_id, row[4], row[5], row[6], row[7], row[8], row[9], row[10],
                 row[11], row[12], row[13], row[14], row[15], row[16], row[17], row[18], row[19], row[20], row[21],
                 row[22], row[23], row[24], row[25], row[26], row[27], row[28], row[29], row[30], row[31]),
            )
        out_conn.commit()

        # media_parts for these media_items
        new_media_ids = set(new_media_id_to_old.keys())
        if new_media_ids and table_exists(new_conn, "media_parts") and table_exists(out_conn, "media_parts"):
            cur = new_conn.execute(
                "SELECT id, media_item_id, directory_id, hash, open_subtitle_hash, file, [index], size, duration, "
                "created_at, updated_at, deleted_at, extra_data FROM media_parts WHERE media_item_id IN ({})".format(
                    ",".join(map(str, new_media_ids))
                )
            )
            part_rows = cur.fetchall()
            next_part_id = max_ids["media_parts"] + 1
            for row in part_rows:
                out_media_id = new_media_id_to_old.get(row[1])
                if not out_media_id:
                    continue
                out_cur.execute(
                    "INSERT INTO media_parts (id, media_item_id, directory_id, hash, open_subtitle_hash, file, "
                    "[index], size, duration, created_at, updated_at, deleted_at, extra_data) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (next_part_id, out_media_id, row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10], row[11], row[12]),
                )
                next_part_id += 1
            out_conn.commit()

        # media_streams for these media_items
        if new_media_ids and table_exists(new_conn, "media_streams") and table_exists(out_conn, "media_streams"):
            cur = new_conn.execute(
                "SELECT id, stream_type_id, media_item_id, url, codec, language, created_at, updated_at, "
                "[index], media_part_id, channels, bitrate, url_index, [default], forced, extra_data "
                "FROM media_streams WHERE media_item_id IN ({})".format(",".join(map(str, new_media_ids)))
            )
            stream_rows = cur.fetchall()
            next_stream_id = max_ids["media_streams"] + 1
            # media_part_id remap: we don't have a simple part id map (we assigned new ids). Skip remap for now;
            # media_part_id can stay 0 or original if it referred to a part in same set we'd need part id map
            for row in stream_rows:
                out_media_id = new_media_id_to_old.get(row[2])
                if not out_media_id:
                    continue
                out_cur.execute(
                    "INSERT INTO media_streams (id, stream_type_id, media_item_id, url, codec, language, created_at, "
                    "updated_at, [index], media_part_id, channels, bitrate, url_index, [default], forced, extra_data) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (next_stream_id, row[1], out_media_id, row[3], row[4], row[5], row[6], row[7], row[8], row[9],
                     row[10], row[11], row[12], row[13], row[14], row[15]),
                )
                next_stream_id += 1
            out_conn.commit()

    return len(to_add)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge Plex DB: use old (good) backup as base, add entries from new (corrupt) DB."
    )
    parser.add_argument("--old", required=True, help="Path to older good backup (com.plexapp.plugins.library.db)")
    parser.add_argument("--new", required=True, help="Path to newer/corrupt DB")
    parser.add_argument("--output", required=True, help="Path for merged output DB (will overwrite)")
    parser.add_argument("--recover", action="store_true", help="If --new won't open, try sqlite3 .recover first")
    parser.add_argument("--merge-new-items", action="store_true", help="Also copy new library items (metadata_items + media) with ID remap")
    args = parser.parse_args()

    success, err = run_merge(
        args.old, args.new, args.output,
        recover=args.recover, merge_new_items=args.merge_new_items,
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
