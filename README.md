# Plex DB Merge

Merge data from a **newer (corrupt)** Plex database into an **older (good)** backup so Plex can start again without losing watch history, settings, and optionally new library items.

When the built-in repair fails or Plex won’t start after repair, you can:

1. Use an older backup as the **base** (known good).
2. **Recover** the corrupt DB with SQLite’s `.recover` if it won’t open.
3. **Compare** and **merge** entries from the new DB into the old one.

## Requirements

- **Python 3.8+**
- **CLI only**: no extra packages (uses stdlib `sqlite3`).
- **Web UI**: `pip install -r requirements.txt` (adds Flask).
- **sqlite3 CLI** (optional): only needed if you use **Recover corrupt DB** and the corrupt DB doesn’t open.  
  - Linux/WSL: `sudo apt install sqlite3`.  
  - The script does **not** use Plex’s custom “Plex SQLite” for the merge; it uses standard SQLite.

## Web UI

Run the app and use the merge from your browser:

```bash
# Optional: use a virtual environment (recommended on Linux)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py

# Or, if you install Flask globally:
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000**. Enter the paths to your old (backup) DB, new (corrupt) DB, and where to write the merged output. Optionally enable “Try to recover corrupt DB” and “Also copy new library items”, then click **Run merge**. Log output updates live; when it’s done, replace your Plex DB with the output file (with Plex stopped) and start Plex.

If **Browse** shows "This folder is empty" (e.g. in Docker), use **Start browse at (optional)** with e.g. `/mnt/user`, or run with `BROWSE_ROOT=/mnt/user python app.py`.

## Usage

**Always stop Plex Media Server before touching the database.**

1. **Back up both files**  
   Copy your current (corrupt) DB and your older backup somewhere safe. Do not overwrite the only good copy.

2. **Run the merge** (paths are examples; use your real paths):

   ```bash
   # Basic: merge watch history and per-item settings from new into old
   python3 plex_db_merge.py \
     --old /path/to/backup/com.plexapp.plugins.library.db \
     --new /path/to/corrupt/com.plexapp.plugins.library.db \
     --output /path/to/merged.db
   ```

   If the **new** DB won’t open (too corrupt), try recovery first:

   ```bash
   python3 plex_db_merge.py \
     --old /path/to/backup/com.plexapp.plugins.library.db \
     --new /path/to/corrupt/com.plexapp.plugins.library.db \
     --output /path/to/merged.db \
     --recover
   ```

   To also **copy new library items** (metadata + media parts) that exist in the new DB but not in the old backup (with ID remapping):

   ```bash
   python3 plex_db_merge.py \
     --old /path/to/backup/com.plexapp.plugins.library.db \
     --new /path/to/corrupt/com.plexapp.plugins.library.db \
     --output /path/to/merged.db \
     --recover \
     --merge-new-items
   ```

3. **Replace the live DB with the merged one**  
   With Plex still stopped:

   - Rename or move the current corrupt DB (e.g. `com.plexapp.plugins.library.db` → `com.plexapp.plugins.library.db.corrupt`).
   - Copy `merged.db` to the Plex database directory and rename it to `com.plexapp.plugins.library.db`.
   - On Linux, fix ownership if needed (e.g. `chown plex:plex com.plexapp.plugins.library.db`).

4. **Start Plex**  
   Start the server and check that it comes up and libraries look correct. You may need to run **Scan Library** or **Refresh metadata** for sections if anything is missing.

## What gets merged

- **Always merged (no extra flags)**  
  - **Watch history** (`metadata_item_views`): rows from the new DB whose `guid` exists in the old DB are added (so resume/watch counts are preserved where the item exists in the backup).  
  - **Per-item settings** (`metadata_item_settings`): same idea — matched by `guid` to the old DB so ratings, view offset, etc. are preserved for existing items.

- **With `--merge-new-items`**  
  - **New library entries**: `metadata_items` in the new DB whose `guid` is not in the old DB are copied, plus their `media_items`, `media_parts`, and `media_streams`, with IDs remapped so they don’t collide with the old DB.  
  - Only items whose `library_section_id` exists in the old DB are copied (so library sections must match).

## Caveats

- **Plex SQLite**: Official repair uses Plex’s custom SQLite. This script uses the standard Python `sqlite3` and the system `sqlite3` CLI for `.recover`. The merged DB should still be loadable by Plex; if you see schema or version issues, try the official repair/replace flow first.
- **Blobs DB**: Only the main library DB (`com.plexapp.plugins.library.db`) is merged. The blobs DB is not touched; keep your existing (good) blobs DB if you have one.
- **Recovery**: `.recover` can lose or alter data. Prefer using a backup from “before” corruption when possible.
- **Testing**: Prefer testing with copies of the DBs (e.g. in a temp folder) before replacing the live file.

## Where is the Plex DB?

Typical paths:

- **Windows**: `%LOCALAPPDATA%\Plex Media Server\Plug-in Support\Databases\`
- **Linux**: `$PLEX_HOME/Library/Application Support/Plex Media Server/Plug-in Support/Databases/` (or similar under `/var/lib/plexmediaserver` etc.)
- **Docker (e.g. linuxserver/plex)**: `/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases/`

The main file is `com.plexapp.plugins.library.db`.

## License

Use and modify as you like; no warranty. Back up your data before running.
