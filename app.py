#!/usr/bin/env python3
"""
Web UI for Plex DB Merge. Run with: python app.py
Then open http://127.0.0.1:5000 in your browser.
"""

import os
import sys
import logging
import threading
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify

import plex_db_merge
from plex_db_merge import (
    run_merge,
    preview_merge,
    run_pragma_integrity_check,
    run_vacuum,
    run_reindex,
    recover_db,
)

app = Flask(__name__)

# Log requests and errors to stdout so "docker logs" on Unraid shows what's happening
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
app.logger.setLevel(logging.INFO)
app.logger.handlers = []
app.logger.addHandler(logging.StreamHandler(sys.stdout))
werk = logging.getLogger("werkzeug")
werk.setLevel(logging.INFO)
werk.handlers = []
werk.addHandler(logging.StreamHandler(sys.stdout))

# Root path for the file browser (restricts browsing to this and below). Set BROWSE_ROOT env to override.
BROWSE_ROOT = os.path.abspath(os.environ.get("BROWSE_ROOT", "/mnt"))

# Shared state for the current merge job (single job at a time)
_state = {"status": "idle", "log": [], "success": False, "error": None, "log_path": None}
# Compare (preview) job state - runs in background so the request doesn't time out
_compare_state = {"status": "idle", "log": [], "stats": None, "error": None, "log_path": None}
# Radarr repair job state (single repair at a time)
_radarr_state = {"status": "idle", "log": [], "success": False, "error": None, "log_path": None}
_lock = threading.Lock()


def _log_file_path(prefix: str) -> str:
    """Path for a timestamped log file in the logs/ directory (under cwd)."""
    log_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")


def _append_log(msg: str) -> None:
    with _lock:
        _state["log"].append(msg)
        f = _state.get("log_file")
        if f:
            try:
                f.write(msg + "\n")
                f.flush()
            except OSError:
                pass


def _append_compare_log(msg: str) -> None:
    with _lock:
        _compare_state["log"].append(msg)
        f = _compare_state.get("log_file")
        if f:
            try:
                f.write(msg + "\n")
                f.flush()
            except OSError:
                pass


def _append_radarr_log(msg: str) -> None:
    with _lock:
        _radarr_state["log"].append(msg)
        f = _radarr_state.get("log_file")
        if f:
            try:
                f.write(msg + "\n")
                f.flush()
            except OSError:
                pass


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json() or {}
    old_path = (data.get("old_path") or "").strip()
    new_path = (data.get("new_path") or "").strip()
    output_path = (data.get("output_path") or "").strip()
    recover = bool(data.get("recover"))
    merge_new_items = bool(data.get("merge_new_items"))

    with _lock:
        if _state["status"] == "running":
            return jsonify({"ok": False, "error": "A merge is already running."}), 400
        if _compare_state["status"] == "running":
            return jsonify({"ok": False, "error": "A compare is running. Wait for it to finish."}), 400
        _state["status"] = "running"
        _state["log"] = []
        _state["success"] = False
        _state["error"] = None
        _state["log_path"] = None
        log_path = _log_file_path("plexdb_merge")
        try:
            _state["log_file"] = open(log_path, "w")
            _state["log_path"] = log_path
        except OSError:
            _state["log_file"] = None

    def do_merge():
        try:
            success, err = run_merge(
                old_path,
                new_path,
                output_path,
                recover=recover,
                merge_new_items=merge_new_items,
                log_callback=_append_log,
            )
            with _lock:
                _state["status"] = "done"
                _state["success"] = success
                _state["error"] = err
        finally:
            with _lock:
                if _state.get("log_file"):
                    try:
                        _state["log_file"].close()
                    except OSError:
                        pass
                    _state["log_file"] = None

    thread = threading.Thread(target=do_merge)
    thread.start()
    return jsonify({"ok": True})


@app.route("/compare", methods=["POST"])
def compare():
    """Start preview in background; returns immediately. Poll /compare_status for result."""
    data = request.get_json() or {}
    old_path = (data.get("old_path") or "").strip()
    new_path = (data.get("new_path") or "").strip()
    recover = bool(data.get("recover"))
    merge_new_items = bool(data.get("merge_new_items"))

    with _lock:
        if _state["status"] == "running":
            return jsonify({"ok": False, "error": "A merge is already running."}), 400
        if _compare_state["status"] == "running":
            return jsonify({"ok": False, "error": "A compare is already running."}), 400
        _compare_state["status"] = "running"
        _compare_state["log"] = []
        _compare_state["stats"] = None
        _compare_state["error"] = None
        _compare_state["log_path"] = None
        log_path = _log_file_path("plexdb_compare")
        try:
            _compare_state["log_file"] = open(log_path, "w")
            _compare_state["log_path"] = log_path
        except OSError:
            _compare_state["log_file"] = None

    def do_compare():
        try:
            plex_db_merge._log_callback = _append_compare_log
            try:
                success, err, stats = preview_merge(
                    old_path, new_path, recover=recover, merge_new_items=merge_new_items
                )
                with _lock:
                    _compare_state["status"] = "done"
                    _compare_state["success"] = success
                    _compare_state["stats"] = stats
                    _compare_state["error"] = err
            finally:
                plex_db_merge._log_callback = None
        except Exception as e:
            with _lock:
                _compare_state["status"] = "done"
                _compare_state["success"] = False
                _compare_state["stats"] = None
                _compare_state["error"] = str(e)
        finally:
            with _lock:
                if _compare_state.get("log_file"):
                    try:
                        _compare_state["log_file"].close()
                    except OSError:
                        pass
                    _compare_state["log_file"] = None

    thread = threading.Thread(target=do_compare)
    thread.start()
    return jsonify({"ok": True})


@app.route("/compare_status")
def compare_status():
    """Poll this after POST /compare to get status, log, and result."""
    with _lock:
        return jsonify({
            "status": _compare_state["status"],
            "log": _compare_state["log"].copy(),
            "stats": _compare_state.get("stats"),
            "success": _compare_state.get("success"),
            "error": _compare_state.get("error"),
            "log_path": _compare_state.get("log_path"),
        })


@app.route("/status")
def status():
    with _lock:
        return jsonify({
            "status": _state["status"],
            "log": _state["log"].copy(),
            "success": _state["success"],
            "error": _state["error"],
            "log_path": _state.get("log_path"),
        })


@app.route("/integrity_check", methods=["POST"])
def integrity_check():
    """Run PRAGMA integrity_check on a given DB path and return the raw messages."""
    data = request.get_json() or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "Path is required."}), 400
    ok_ic, messages_ic, err_ic = run_pragma_integrity_check(path)
    resp = {
        "ok": bool(ok_ic),
        "messages": messages_ic or [],
        "error": err_ic,
    }
    # Always 200 so the UI can display detailed messages, even when there are errors.
    return jsonify(resp)


# ---------- Radarr DB repair (inspired by DBRepair) ----------

@app.route("/radarr/check", methods=["POST"])
def radarr_check():
    """Run integrity_check on Radarr DB path."""
    data = request.get_json() or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "Path is required."}), 400
    ok_ic, messages_ic, err_ic = run_pragma_integrity_check(path)
    return jsonify({
        "ok": bool(ok_ic),
        "messages": messages_ic or [],
        "error": err_ic,
    })


@app.route("/radarr/repair", methods=["POST"])
def radarr_repair():
    """Start Radarr DB repair (recover) in background. Writes to output_path."""
    data = request.get_json() or {}
    path = (data.get("path") or "").strip()
    output_path = (data.get("output_path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "Path is required."}), 400
    if not output_path:
        return jsonify({"ok": False, "error": "Output path is required."}), 400
    with _lock:
        if _radarr_state["status"] == "running":
            return jsonify({"ok": False, "error": "A Radarr repair is already running."}), 400
        _radarr_state["status"] = "running"
        _radarr_state["log"] = []
        _radarr_state["success"] = False
        _radarr_state["error"] = None
        _radarr_state["log_path"] = None
        log_path = _log_file_path("radarr_repair")
        try:
            _radarr_state["log_file"] = open(log_path, "w")
            _radarr_state["log_path"] = log_path
        except OSError:
            _radarr_state["log_file"] = None

    def do_repair():
        try:
            plex_db_merge._log_callback = _append_radarr_log
            try:
                success = recover_db(path, output_path)
                with _lock:
                    _radarr_state["status"] = "done"
                    _radarr_state["success"] = success
                    _radarr_state["error"] = None if success else "Recovery failed. See log."
            finally:
                plex_db_merge._log_callback = None
        except Exception as e:
            with _lock:
                _radarr_state["status"] = "done"
                _radarr_state["success"] = False
                _radarr_state["error"] = str(e)
        finally:
            with _lock:
                if _radarr_state.get("log_file"):
                    try:
                        _radarr_state["log_file"].close()
                    except OSError:
                        pass
                    _radarr_state["log_file"] = None

    thread = threading.Thread(target=do_repair)
    thread.start()
    return jsonify({"ok": True})


@app.route("/radarr/status")
def radarr_status():
    """Poll Radarr repair job status."""
    with _lock:
        return jsonify({
            "status": _radarr_state["status"],
            "log": _radarr_state["log"].copy(),
            "success": _radarr_state["success"],
            "error": _radarr_state["error"],
            "log_path": _radarr_state.get("log_path"),
        })


@app.route("/radarr/vacuum", methods=["POST"])
def radarr_vacuum():
    """Run VACUUM on Radarr DB. Stop Radarr first."""
    data = request.get_json() or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "Path is required."}), 400
    success, err = run_vacuum(path)
    if success:
        return jsonify({"ok": True, "message": "VACUUM completed."})
    return jsonify({"ok": False, "error": err or "VACUUM failed."})


@app.route("/radarr/reindex", methods=["POST"])
def radarr_reindex():
    """Run REINDEX on Radarr DB. Stop Radarr first."""
    data = request.get_json() or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "Path is required."}), 400
    success, err = run_reindex(path)
    if success:
        return jsonify({"ok": True, "message": "REINDEX completed."})
    return jsonify({"ok": False, "error": err or "REINDEX failed."})


@app.route("/browse_root")
def browse_root():
    """Return the path that the file browser is restricted to (for UI hint)."""
    return jsonify({"browse_root": BROWSE_ROOT})


@app.route("/browse")
def browse():
    """List directory contents. path must be under BROWSE_ROOT."""
    path = (request.args.get("path") or BROWSE_ROOT).strip()
    path = os.path.normpath(path)
    if not path.startswith("/"):
        path = os.path.abspath(path)
    # Restrict to BROWSE_ROOT
    try:
        common = os.path.commonpath([os.path.realpath(path), os.path.realpath(BROWSE_ROOT)])
    except ValueError:
        common = ""
    if os.path.realpath(BROWSE_ROOT) != os.path.commonpath([os.path.realpath(common), os.path.realpath(BROWSE_ROOT)]):
        return jsonify({"error": "Path not allowed"}), 403
    if not os.path.exists(path):
        return jsonify({"error": f"Path does not exist on this server: {path}"}), 400
    if not os.path.isdir(path):
        return jsonify({"error": f"Not a directory (or not accessible): {path}"}), 400
    try:
        entries = os.listdir(path)
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    dirs = []
    files = []
    for name in sorted(entries):
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full):
                dirs.append(name)
            else:
                files.append(name)
        except OSError:
            continue
    parent = os.path.dirname(path) if path != BROWSE_ROOT else None
    if parent:
        try:
            rp = os.path.realpath(parent)
            rb = os.path.realpath(BROWSE_ROOT)
            if rp != rb and not rp.startswith(rb + os.sep):
                parent = None  # don't allow Up above BROWSE_ROOT
        except (ValueError, OSError):
            parent = None
    return jsonify({
        "path": path,
        "parent": parent,
        "directories": dirs,
        "files": files,
    })


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Plex DB Merge</title>
  <style>
    :root {
      --bg: #1a1d23;
      --surface: #252930;
      --border: #3d434e;
      --text: #e6e9ef;
      --muted: #9096a3;
      --accent: #7c9ce0;
      --accent-hover: #94b0f0;
      --success: #8fbc8f;
      --danger: #d48989;
    }
    * { box-sizing: border-box; }
    body {
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 1.5rem;
      min-height: 100vh;
    }
    .container { max-width: 640px; margin: 0 auto; }
    h1 {
      font-size: 1.5rem;
      font-weight: 600;
      margin: 0 0 0.5rem 0;
      color: var(--text);
    }
    .sub {
      color: var(--muted);
      font-size: 0.9rem;
      margin-bottom: 1.5rem;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.25rem;
      margin-bottom: 1rem;
    }
    label {
      display: block;
      font-size: 0.85rem;
      color: var(--muted);
      margin-bottom: 0.35rem;
    }
    input[type="text"] {
      width: 100%;
      padding: 0.6rem 0.75rem;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      font-size: 0.95rem;
      margin-bottom: 1rem;
    }
    input[type="text"]::placeholder { color: var(--muted); }
    input[type="text"]:focus {
      outline: none;
      border-color: var(--accent);
    }
    .row {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-bottom: 0.75rem;
    }
    input[type="checkbox"] {
      width: 1.1rem;
      height: 1.1rem;
      accent-color: var(--accent);
    }
    .btn {
      display: inline-block;
      padding: 0.65rem 1.25rem;
      background: var(--accent);
      color: var(--bg);
      border: none;
      border-radius: 6px;
      font-size: 0.95rem;
      font-weight: 600;
      cursor: pointer;
      margin-top: 0.5rem;
    }
    .btn:hover { background: var(--accent-hover); }
    .btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .log-box {
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.75rem 1rem;
      font-family: ui-monospace, 'Cascadia Code', monospace;
      font-size: 0.8rem;
      line-height: 1.45;
      max-height: 320px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-word;
      margin-top: 1rem;
      min-height: 120px;
    }
    .log-box:empty::before {
      content: 'Log output will appear here when you run the merge.';
      color: var(--muted);
    }
    .status-idle { color: var(--muted); }
    .status-running { color: var(--accent); }
    .status-done { color: var(--success); }
    .status-done.err { color: var(--danger); }
    .message { margin-top: 0.5rem; font-size: 0.9rem; }
    code { background: var(--bg); padding: 0.15rem 0.4rem; border-radius: 4px; font-size: 0.9em; }
    .path-row { display: flex; gap: 0.5rem; margin-bottom: 1rem; align-items: center; }
    .path-row input { flex: 1; margin-bottom: 0; }
    .btn-browse {
      padding: 0.6rem 1rem;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.9rem;
      white-space: nowrap;
    }
    .btn-browse:hover { border-color: var(--accent); color: var(--accent); }
    .modal-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.6);
      z-index: 100;
      align-items: center;
      justify-content: center;
      padding: 1rem;
    }
    .modal-overlay.open { display: flex; }
    .modal {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      max-width: 520px;
      width: 100%;
      max-height: 80vh;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .modal-header {
      padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .modal-path { flex: 1; font-size: 0.85rem; color: var(--muted); word-break: break-all; }
    .modal-body {
      overflow-y: auto;
      padding: 0.5rem;
      min-height: 200px;
    }
    .browser-item {
      padding: 0.5rem 0.75rem;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.95rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .browser-item:hover { background: var(--bg); }
    .browser-item.dir::before { content: '📁'; }
    .browser-item.file::before { content: '📄'; }
    .browser-item.file.db::before { content: '🗄'; }
    .modal-footer { padding: 0.75rem 1rem; border-top: 1px solid var(--border); }
  </style>
</head>
<body>
  <div class="container">
    <h1>Plex DB Merge</h1>
    <p class="sub">Use an old (good) backup as base, merge in data from a newer or corrupt DB.</p>

    <div class="card">
      <label for="old_path">Old backup DB (good)</label>
      <div class="path-row">
        <input type="text" id="old_path" placeholder="e.g. /mnt/user/.../com.plexapp.plugins.library.db-2025-10-19">
        <button type="button" class="btn-browse" data-target="old_path">Browse</button>
      </div>

      <label for="new_path">New / corrupt DB</label>
      <div class="path-row">
        <input type="text" id="new_path" placeholder="e.g. /mnt/user/.../com.plexapp.plugins.library.db">
        <button type="button" class="btn-browse" data-target="new_path">Browse</button>
      </div>

      <label for="output_path">Output merged DB</label>
      <div class="path-row">
        <input type="text" id="output_path" placeholder="e.g. /mnt/user/.../com.plexapp.plugins.library.db-merged">
        <button type="button" class="btn-browse" data-target="output_path">Browse</button>
      </div>
      <p class="sub" style="margin-top: -0.5rem; margin-bottom: 0.5rem;">Browse shows folders on the <strong>machine where this app is running</strong>. If you see "Not a directory" or "does not exist", run this app on the same host that has the Plex DB (e.g. on your Unraid box).</p>
      <label for="browse_start">Start browse at (optional)</label>
      <div class="path-row">
        <input type="text" id="browse_start" placeholder="e.g. /mnt/user — use when server root is empty">
      </div>

      <div class="row">
        <input type="checkbox" id="recover" name="recover">
        <label for="recover" style="margin:0;">Try to recover corrupt DB (sqlite3 .recover) if it won't open</label>
      </div>
      <div class="row">
        <input type="checkbox" id="merge_new_items" name="merge_new_items">
        <label for="merge_new_items" style="margin:0;">Also copy new library items (metadata + media) with ID remap</label>
      </div>

      <div style="display: flex; gap: 0.5rem; margin-top: 0.75rem;">
        <button type="button" class="btn-browse" id="previewBtn">Preview comparison</button>
        <button type="button" class="btn" id="runBtn">Run merge</button>
      </div>
      <div id="compareResult" class="compare-result" style="display: none; margin-top: 1rem; padding: 0.75rem; background: var(--bg); border-radius: 6px; font-size: 0.9rem;"></div>
    </div>

    <div class="card">
      <h2 style="font-size: 1.1rem; margin: 0 0 0.75rem 0;">Radarr DB Repair</h2>
      <p class="sub" style="margin-bottom: 1rem;">Check, recover, vacuum, or reindex a Radarr SQLite DB (e.g. <code>radarr.db</code> or <code>com.radarr.db</code>). Stop Radarr before repair, vacuum, or reindex.</p>
      <label for="radarr_path">Radarr DB path</label>
      <div class="path-row">
        <input type="text" id="radarr_path" placeholder="e.g. /mnt/user/appdata/Radarr/radarr.db">
        <button type="button" class="btn-browse" data-target="radarr_path">Browse</button>
      </div>
      <label for="radarr_output_path">Output path (for Repair only)</label>
      <div class="path-row">
        <input type="text" id="radarr_output_path" placeholder="e.g. /mnt/user/appdata/Radarr/radarr-repaired.db">
        <button type="button" class="btn-browse" data-target="radarr_output_path">Browse</button>
      </div>
      <div style="display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.75rem;">
        <button type="button" class="btn-browse" id="radarrCheckBtn">Check</button>
        <button type="button" class="btn" id="radarrRepairBtn">Repair</button>
        <button type="button" class="btn-browse" id="radarrVacuumBtn">Vacuum</button>
        <button type="button" class="btn-browse" id="radarrReindexBtn">Reindex</button>
      </div>
      <div id="radarrCheckResult" style="display: none; margin-top: 1rem; padding: 0.75rem; background: var(--bg); border-radius: 6px; font-size: 0.9rem;"></div>
      <div id="radarrRepairStatus" class="status-idle" style="margin-top: 0.75rem;"></div>
      <div class="log-box" id="radarrLogBox" style="margin-top: 0.5rem; min-height: 80px;"></div>
    </div>

    <div class="card">
      <div id="statusLine" class="status-idle">Idle</div>
      <div id="message" class="message" style="display:none;"></div>
      <div class="log-box" id="logBox"></div>
    </div>
  </div>

  <div class="modal-overlay" id="browseModal">
    <div class="modal">
      <div class="modal-header">
        <button type="button" class="btn-browse" id="browseUp">↑ Up</button>
        <span class="modal-path" id="browsePath"></span>
      </div>
      <div class="modal-body" id="browseList"></div>
      <div class="modal-footer">
        <button type="button" class="btn-browse" id="browseCancel">Cancel</button>
      </div>
    </div>
  </div>

  <script>
    const runBtn = document.getElementById('runBtn');
    const oldPath = document.getElementById('old_path');
    const newPath = document.getElementById('new_path');
    const outputPath = document.getElementById('output_path');
    const recover = document.getElementById('recover');
    const mergeNewItems = document.getElementById('merge_new_items');
    const statusLine = document.getElementById('statusLine');
    const message = document.getElementById('message');
    const logBox = document.getElementById('logBox');
    const browseModal = document.getElementById('browseModal');
    const browsePathEl = document.getElementById('browsePath');
    const browseListEl = document.getElementById('browseList');
    const browseUpBtn = document.getElementById('browseUp');
    const browseCancelBtn = document.getElementById('browseCancel');
    const previewBtn = document.getElementById('previewBtn');
    const compareResult = document.getElementById('compareResult');
    const checkPath = document.getElementById('check_path');
    const checkBtn = document.getElementById('checkBtn');
    const checkResult = document.getElementById('checkResult');
    // Plex "Check" elements may be missing; Radarr section has its own Check.

    let pollTimer = null;
    let browseTargetId = null;

    function loadBrowse(path) {
      const url = '/browse' + (path ? '?path=' + encodeURIComponent(path) : '');
      fetch(url)
        .then(r => {
          if (!r.ok) return r.json().then(d => { throw new Error(d.error || r.statusText); });
          return r.json();
        })
        .then(d => {
          browsePathEl.textContent = d.path;
          browseListEl.innerHTML = '';
          if (d.parent !== null) {
            browseUpBtn.style.display = 'inline-block';
            browseUpBtn.dataset.parent = d.parent;
          } else {
            browseUpBtn.style.display = 'none';
          }
          d.directories.forEach(name => {
            const div = document.createElement('div');
            div.className = 'browser-item dir';
            div.textContent = name + ' /';
            div.onclick = () => loadBrowse(d.path + '/' + name);
            browseListEl.appendChild(div);
          });
          d.files.forEach(name => {
            const div = document.createElement('div');
            div.className = 'browser-item file' + (name.endsWith('.db') ? ' db' : '');
            div.textContent = name;
            div.onclick = () => {
              const full = d.path + '/' + name;
              const input = document.getElementById(browseTargetId);
              if (input) input.value = full;
              browseModal.classList.remove('open');
            };
            browseListEl.appendChild(div);
          });
          if (d.directories.length === 0 && d.files.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'sub';
            empty.textContent = 'This folder is empty.';
            browseListEl.appendChild(empty);
          }
        })
        .catch(err => {
          browseListEl.innerHTML = '<div class="sub" style="color: var(--danger);">' + err.message + '</div>';
        });
    }

    const browseStartInput = document.getElementById('browse_start');
    document.querySelectorAll('.btn-browse[data-target]').forEach(btn => {
      btn.addEventListener('click', () => {
        browseTargetId = btn.dataset.target;
        const input = document.getElementById(browseTargetId);
        let startPath = (browseStartInput && browseStartInput.value.trim()) ? browseStartInput.value.trim().replace(/\\\\/g, '/') : '';
        if (!startPath && input && input.value.trim()) {
          const p = input.value.trim().replace(/\\\\/g, '/');
          const last = p.lastIndexOf('/');
          startPath = last > 0 ? p.slice(0, last) : '';
        }
        browseModal.classList.add('open');
        loadBrowse(startPath || undefined);
      });
    });
    browseUpBtn.addEventListener('click', () => {
      const parent = browseUpBtn.dataset.parent;
      if (parent !== undefined) loadBrowse(parent);
    });
    browseCancelBtn.addEventListener('click', () => browseModal.classList.remove('open'));
    browseModal.addEventListener('click', e => {
      if (e.target === browseModal) browseModal.classList.remove('open');
    });

    let comparePollTimer = null;
    function pollCompare() {
      fetch('/compare_status')
        .then(r => r.json())
        .then(d => {
          if (d.status === 'running') {
            compareResult.innerHTML = '<strong>Comparing…</strong> (can take 10–20 min for large DBs; logs update every 2s)<br><pre class="sub" style="margin-top:0.5rem; white-space:pre-wrap; font-size:0.85rem; max-height:12rem; overflow:auto; border:1px solid var(--border); padding:0.5rem;">' + (d.log && d.log.length ? d.log.join('\\n') : '…') + '</pre>';
            if (!comparePollTimer) comparePollTimer = setInterval(pollCompare, 2000);
            return;
          }
          if (comparePollTimer) {
            clearInterval(comparePollTimer);
            comparePollTimer = null;
          }
          previewBtn.disabled = false;
          if (d.status === 'done') {
            const logNote = d.log_path ? '<br><span class="sub">Log saved to: ' + d.log_path + '</span>' : '';
            if (d.success && d.stats) {
              const s = d.stats;
              compareResult.innerHTML = '<strong>What will be merged (preview):</strong><br>' +
                '• Watch history entries to add: ' + s.views_to_add + '<br>' +
                '• Per-item settings to add: ' + s.settings_to_add + '<br>' +
                (s.new_metadata_items_to_add > 0 ? '• New library items to copy: ' + s.new_metadata_items_to_add + ' (with "Also copy new library items" enabled)<br>' : '') +
                '<span class="sub">Run merge to apply. After merging, replace the Plex DB with the output file (see steps below when done).</span>' + logNote;
            } else {
              compareResult.innerHTML = '<span style="color: var(--danger);">' + (d.error || 'Preview failed') + '</span>' + (d.log_path ? '<br><span class="sub">Log saved to: ' + d.log_path + '</span>' : '');
            }
          } else {
            compareResult.innerHTML = '<span class="sub">Idle.</span>';
          }
        })
        .catch(() => {
          if (comparePollTimer) clearInterval(comparePollTimer);
          comparePollTimer = null;
          previewBtn.disabled = false;
          compareResult.innerHTML = '<span style="color: var(--danger);">Request failed.</span>';
        });
    }
    previewBtn.addEventListener('click', () => {
      const old = oldPath.value.trim();
      const new_ = newPath.value.trim();
      if (!old || !new_) {
        alert('Fill in at least Old and New DB paths to preview.');
        return;
      }
      compareResult.style.display = 'block';
      compareResult.textContent = 'Starting compare…';
      previewBtn.disabled = true;
      fetch('/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_path: old,
          new_path: new_,
          recover: recover.checked,
          merge_new_items: mergeNewItems.checked
        })
      })
        .then(r => r.json())
        .then(d => {
          if (!d.ok) {
            compareResult.innerHTML = '<span style="color: var(--danger);">' + (d.error || 'Preview failed') + '</span>';
            previewBtn.disabled = false;
            return;
          }
          pollCompare();
        })
        .catch(() => {
          compareResult.innerHTML = '<span style="color: var(--danger);">Request failed.</span>';
          previewBtn.disabled = false;
        });
    });

    function setStatus(text, klass) {
      statusLine.textContent = text;
      statusLine.className = klass || 'status-idle';
    }

    function poll() {
      fetch('/status')
        .then(r => r.json())
        .then(d => {
          logBox.textContent = d.log.join('\\n');
          logBox.scrollTop = logBox.scrollHeight;

          if (d.status === 'running') {
            setStatus('Running…', 'status-running');
            runBtn.disabled = true;
            if (!pollTimer) pollTimer = setInterval(poll, 500);
            return;
          }

          if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
          }
          runBtn.disabled = false;

          if (d.status === 'done') {
            const logNote = d.log_path ? '<br><br>Log saved to: ' + d.log_path : '';
            if (d.success) {
              setStatus('Done', 'status-done');
              message.style.display = 'block';
              message.style.color = 'var(--success)';
              message.innerHTML = 'Merge completed.<br><strong>Next steps:</strong><br>1. Stop Plex Media Server.<br>2. Back up the current DB (rename or move it).<br>3. Replace it with the merged file (rename the output to com.plexapp.plugins.library.db).<br>4. Fix ownership if needed (e.g. chown on Unraid).<br>5. Start Plex. It should start with the old library plus merged watch history/settings.<br>6. Optional: run Scan Library or Refresh metadata if anything looks missing.' + logNote;
            } else {
              setStatus('Failed', 'status-done err');
              message.style.display = 'block';
              message.style.color = 'var(--danger)';
              message.innerHTML = (d.error || 'Merge failed.') + logNote;
            }
          } else {
            setStatus('Idle', 'status-idle');
            message.style.display = 'none';
          }
        })
        .catch(() => {
          if (pollTimer) clearInterval(pollTimer);
          runBtn.disabled = false;
          setStatus('Error', 'status-done err');
        });
    }

    runBtn.addEventListener('click', () => {
      const old = oldPath.value.trim();
      const new_ = newPath.value.trim();
      const out = outputPath.value.trim();
      if (!old || !new_ || !out) {
        alert('Please fill in all three paths.');
        return;
      }
      message.style.display = 'none';
      logBox.textContent = '';
      setStatus('Starting…', 'status-running');
      runBtn.disabled = true;

      fetch('/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_path: old,
          new_path: new_,
          output_path: out,
          recover: recover.checked,
          merge_new_items: mergeNewItems.checked
        })
      })
        .then(r => r.json())
        .then(d => {
          if (!d.ok) {
            alert(d.error || 'Failed to start.');
            runBtn.disabled = false;
            setStatus('Idle', 'status-idle');
            return;
          }
          pollTimer = setInterval(poll, 500);
          poll();
        })
        .catch(() => {
          alert('Request failed.');
          runBtn.disabled = false;
          setStatus('Idle', 'status-idle');
        });
    });

    const radarrPath = document.getElementById('radarr_path');
    const radarrOutputPath = document.getElementById('radarr_output_path');
    const radarrCheckBtn = document.getElementById('radarrCheckBtn');
    const radarrRepairBtn = document.getElementById('radarrRepairBtn');
    const radarrVacuumBtn = document.getElementById('radarrVacuumBtn');
    const radarrReindexBtn = document.getElementById('radarrReindexBtn');
    const radarrCheckResult = document.getElementById('radarrCheckResult');
    const radarrRepairStatus = document.getElementById('radarrRepairStatus');
    const radarrLogBox = document.getElementById('radarrLogBox');

    function showRadarrResult(html) {
      radarrCheckResult.style.display = 'block';
      radarrCheckResult.innerHTML = html;
    }

    radarrCheckBtn.addEventListener('click', () => {
      const p = (radarrPath.value || '').trim();
      if (!p) {
        alert('Enter Radarr DB path.');
        return;
      }
      radarrCheckBtn.disabled = true;
      showRadarrResult('<span class="sub">Running integrity_check…</span>');
      fetch('/radarr/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p })
      })
        .then(r => r.json())
        .then(d => {
          radarrCheckBtn.disabled = false;
          const msgs = d.messages || [];
          if (d.error && !msgs.length && !d.ok) {
            showRadarrResult('<span style="color: var(--danger);">' + (d.error || '') + '</span>');
            return;
          }
          if (d.ok && msgs.length === 1 && msgs[0].toLowerCase() === 'ok') {
            showRadarrResult('<span style="color: var(--success);">integrity_check: OK</span>');
            return;
          }
          let html = '';
          if (d.error) html += '<span style="color: var(--danger);">' + d.error + '</span><br>';
          if (msgs.length) {
            html += '<strong>integrity_check reported ' + msgs.length + ' issue(s):</strong><br><pre class="sub" style="margin-top:0.5rem; white-space:pre-wrap; max-height:12rem; overflow:auto; border:1px solid var(--border); padding:0.5rem;">' + msgs.join('\\n') + '</pre>';
          }
          showRadarrResult(html || '<span class="sub">No output.</span>');
        })
        .catch(() => {
          radarrCheckBtn.disabled = false;
          showRadarrResult('<span style="color: var(--danger);">Request failed.</span>');
        });
    });

    let radarrPollTimer = null;
    function pollRadarrStatus() {
      fetch('/radarr/status')
        .then(r => r.json())
        .then(d => {
          radarrLogBox.textContent = (d.log || []).join('\\n');
          radarrLogBox.scrollTop = radarrLogBox.scrollHeight;
          if (d.status === 'running') {
            radarrRepairStatus.textContent = 'Repair running…';
            radarrRepairStatus.className = 'status-running';
            radarrRepairBtn.disabled = true;
            if (!radarrPollTimer) radarrPollTimer = setInterval(pollRadarrStatus, 500);
            return;
          }
          if (radarrPollTimer) {
            clearInterval(radarrPollTimer);
            radarrPollTimer = null;
          }
          radarrRepairBtn.disabled = false;
          if (d.status === 'done') {
            if (d.success) {
              radarrRepairStatus.textContent = 'Done';
              radarrRepairStatus.className = 'status-done';
              showRadarrResult('<span style="color: var(--success);">Repair completed. Stop Radarr, replace the DB file with the output file, then start Radarr.</span>' + (d.log_path ? '<br><span class="sub">Log: ' + d.log_path + '</span>' : ''));
            } else {
              radarrRepairStatus.textContent = 'Failed';
              radarrRepairStatus.className = 'status-done err';
              showRadarrResult('<span style="color: var(--danger);">' + (d.error || 'Repair failed.') + '</span>' + (d.log_path ? '<br><span class="sub">Log: ' + d.log_path + '</span>' : ''));
            }
          } else {
            radarrRepairStatus.textContent = 'Idle';
            radarrRepairStatus.className = 'status-idle';
          }
        })
        .catch(() => {
          if (radarrPollTimer) clearInterval(radarrPollTimer);
          radarrPollTimer = null;
          radarrRepairBtn.disabled = false;
          radarrRepairStatus.textContent = 'Error';
          radarrRepairStatus.className = 'status-done err';
        });
    }

    radarrRepairBtn.addEventListener('click', () => {
      const p = (radarrPath.value || '').trim();
      const out = (radarrOutputPath.value || '').trim();
      if (!p || !out) {
        alert('Enter both Radarr DB path and Output path.');
        return;
      }
      radarrCheckResult.style.display = 'block';
      radarrLogBox.textContent = '';
      radarrRepairStatus.textContent = 'Starting…';
      radarrRepairStatus.className = 'status-running';
      radarrRepairBtn.disabled = true;
      fetch('/radarr/repair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p, output_path: out })
      })
        .then(r => r.json())
        .then(d => {
          if (!d.ok) {
            alert(d.error || 'Failed to start repair.');
            radarrRepairBtn.disabled = false;
            radarrRepairStatus.className = 'status-idle';
            return;
          }
          radarrPollTimer = setInterval(pollRadarrStatus, 500);
          pollRadarrStatus();
        })
        .catch(() => {
          alert('Request failed.');
          radarrRepairBtn.disabled = false;
          radarrRepairStatus.className = 'status-idle';
        });
    });

    radarrVacuumBtn.addEventListener('click', () => {
      const p = (radarrPath.value || '').trim();
      if (!p) {
        alert('Enter Radarr DB path.');
        return;
      }
      radarrVacuumBtn.disabled = true;
      showRadarrResult('<span class="sub">Running VACUUM… (stop Radarr first)</span>');
      fetch('/radarr/vacuum', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p })
      })
        .then(r => r.json())
        .then(d => {
          radarrVacuumBtn.disabled = false;
          if (d.ok) showRadarrResult('<span style="color: var(--success);">' + (d.message || 'VACUUM completed.') + '</span>');
          else showRadarrResult('<span style="color: var(--danger);">' + (d.error || 'VACUUM failed.') + '</span>');
        })
        .catch(() => {
          radarrVacuumBtn.disabled = false;
          showRadarrResult('<span style="color: var(--danger);">Request failed.</span>');
        });
    });

    radarrReindexBtn.addEventListener('click', () => {
      const p = (radarrPath.value || '').trim();
      if (!p) {
        alert('Enter Radarr DB path.');
        return;
      }
      radarrReindexBtn.disabled = true;
      showRadarrResult('<span class="sub">Running REINDEX… (stop Radarr first)</span>');
      fetch('/radarr/reindex', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p })
      })
        .then(r => r.json())
        .then(d => {
          radarrReindexBtn.disabled = false;
          if (d.ok) showRadarrResult('<span style="color: var(--success);">' + (d.message || 'REINDEX completed.') + '</span>');
          else showRadarrResult('<span style="color: var(--danger);">' + (d.error || 'REINDEX failed.') + '</span>');
        })
        .catch(() => {
          radarrReindexBtn.disabled = false;
          showRadarrResult('<span style="color: var(--danger);">Request failed.</span>');
        });
    });

    if (checkPath && checkBtn && checkResult) {
      checkBtn.addEventListener('click', () => {
        let p = (checkPath.value || '').trim();
        if (!p) p = (outputPath.value || newPath.value || oldPath.value || '').trim();
        if (!p) {
          alert('Enter a DB path to check (or fill one of the Old/New/Output fields).');
          return;
        }
        checkPath.value = p;
        checkBtn.disabled = true;
        checkResult.style.display = 'block';
        checkResult.innerHTML = '<span class="sub">Running PRAGMA integrity_check…</span>';
        fetch('/integrity_check', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: p })
        })
          .then(r => r.json())
          .then(d => {
            checkBtn.disabled = false;
            const msgs = d.messages || [];
            if (d.error && !msgs.length && !d.ok) {
              checkResult.innerHTML = '<span style="color: var(--danger);">' + d.error + '</span>';
              return;
            }
            if (d.ok && msgs.length === 1 && msgs[0].toLowerCase() === 'ok') {
              checkResult.innerHTML = '<span style="color: var(--success);">integrity_check: OK</span>';
              return;
            }
            let html = '';
            if (d.error) html += '<span style="color: var(--danger);">' + d.error + '</span><br>';
            if (msgs.length) html += '<strong>integrity_check reported ' + msgs.length + ' issue(s):</strong><br><pre class="sub" style="margin-top:0.5rem; white-space:pre-wrap; max-height:12rem; overflow:auto; border:1px solid var(--border); padding:0.5rem;">' + msgs.join('\\n') + '</pre>';
            checkResult.innerHTML = html || '<span class="sub">No output from integrity_check.</span>';
          })
          .catch(() => {
            checkBtn.disabled = false;
            checkResult.innerHTML = '<span style="color: var(--danger);">Request failed.</span>';
          });
      });
    }
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser(description="Plex DB Merge web UI")
    _p.add_argument("--host", default="0.0.0.0", help="Bind address (use 127.0.0.1 for local-only)")
    _p.add_argument("--port", type=int, default=5000, help="Port (default 5000)")
    _args = _p.parse_args()
    # Log to stdout so docker logs shows why it's not working (e.g. bind address, paths)
    print(f"[Plex DB Merge] Starting: host={_args.host!r} port={_args.port} BROWSE_ROOT={BROWSE_ROOT!r}", flush=True)
    print(f"[Plex DB Merge] If you cannot reach the UI, ensure host is 0.0.0.0 and the container port is mapped (e.g. -p 2000:5000).", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    app.run(host=_args.host, port=_args.port, debug=False, threaded=True)
