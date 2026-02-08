#!/usr/bin/env python3
"""
Web UI for Plex DB Merge. Run with: python app.py
Then open http://127.0.0.1:5000 in your browser.
"""

import os
import threading
from flask import Flask, render_template_string, request, jsonify

from plex_db_merge import run_merge

app = Flask(__name__)

# Root path for the file browser (restricts browsing to this and below). Set BROWSE_ROOT env to override.
BROWSE_ROOT = os.path.abspath(os.environ.get("BROWSE_ROOT", "/mnt"))

# Shared state for the current merge job (single job at a time)
_state = {"status": "idle", "log": [], "success": False, "error": None}
_lock = threading.Lock()


def _append_log(msg: str) -> None:
    with _lock:
        _state["log"].append(msg)


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
        _state["status"] = "running"
        _state["log"] = []
        _state["success"] = False
        _state["error"] = None

    def do_merge():
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

    thread = threading.Thread(target=do_merge)
    thread.start()
    return jsonify({"ok": True})


@app.route("/status")
def status():
    with _lock:
        return jsonify({
            "status": _state["status"],
            "log": _state["log"].copy(),
            "success": _state["success"],
            "error": _state["error"],
        })


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

      <button type="button" class="btn" id="runBtn">Run merge</button>
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
            if (d.success) {
              setStatus('Done', 'status-done');
              message.style.display = 'block';
              message.style.color = 'var(--success)';
              message.textContent = 'Merge completed. Replace your Plex DB with the output file (with Plex stopped), then start Plex.';
            } else {
              setStatus('Failed', 'status-done err');
              message.style.display = 'block';
              message.style.color = 'var(--danger)';
              message.textContent = d.error || 'Merge failed.';
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
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
