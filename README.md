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
- **sqlite3 CLI** (optional): for **Recover corrupt DB** when the DB won’t open.  
  - **Why your system sqlite3 is old:** Ubuntu/Debian ship an older sqlite3 (often 3.22) for stability. The **.recover** command needs **SQLite 3.26+**.  
  - **Option A – Use the fallback:** The tool falls back to **.dump** if .recover isn’t available, so `apt install sqlite3` is enough for most cases.  
  - **Option B – Use the newest SQLite (for .recover):** Download the official **Precompiled Binaries for Linux** from [sqlite.org/download.html](https://www.sqlite.org/download.html):  
    1. Install unzip if needed: `sudo apt install unzip`  
    2. Open the download page in a browser, find **Precompiled Binaries for Linux** → **sqlite-tools-linux-x64-XXXXX.zip**, right‑click and “Copy link address” (the direct URL varies; the page uses JavaScript for links).  
    3. Or use this direct URL (Linux x64, version 3510200):  
       `https://www.sqlite.org/2026/sqlite-tools-linux-x64-3510200.zip`  
    4. Then:  
    ```bash
    cd /tmp
    wget https://www.sqlite.org/2026/sqlite-tools-linux-x64-3510200.zip
    unzip sqlite-tools-linux-x64-*.zip
    SQLITE3=/tmp/sqlite-tools-linux-x64-3510200/sqlite3 .venv/bin/python app.py
    ```
    (Replace the path with the folder name from the zip if the version number differs.)  
    (Or put that `sqlite3` on your PATH, or set `SQLITE3` in the environment.)  
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

## Docker

The app can run in Docker with **sqlite3 bundled** in the image (no need to install or set `SQLITE3`).

```bash
cd /path/to/plexdb
docker build -t plexdb-merge .
docker run -p 5000:5000 -v /path/to/your/dbs:/data plexdb-merge
```

Then open **http://localhost:5000**. Set **Start browse at** to `/data` (or the path where you mounted your DBs). The image includes a Linux x64 sqlite3 with `.recover` support.

- **Build with a different sqlite tools URL** (e.g. if the default 404s):  
  `docker build --build-arg SQLITE_TOOLS_URL=https://...zip -t plexdb-merge .`
- **Use your own `bin/sqlite3`**: Put a `sqlite3` binary in `bin/` before building; the Dockerfile will still run the download but you can overwrite by copying first:  
  `COPY bin/ /app/bin/` before the RUN that downloads.

### Unraid Docker template

An Unraid XML template is in **`docs/plexdb-merge.xml`**. It uses **GitHub Container Registry** (`ghcr.io/tru5t/plexdb-merge`).

1. **Build and push the image** to GHCR (see [Publishing Docker images](https://docs.github.com/en/actions/publishing-packages/publishing-docker-images)):
   ```bash
   docker build -t ghcr.io/TRU5T/plexdb-merge:latest .
   docker push ghcr.io/TRU5T/plexdb-merge:latest
   ```
2. **On Unraid**: **Docker** → **Add Container** → add a template repository `https://raw.githubusercontent.com/TRU5T/plexdb/main/docs/` (or copy `docs/plexdb-merge.xml` to `/boot/config/plugins/dockerMan/templates-user/`), then add the container from the **Plex DB Merge** template.

Map the **Data** path to where your Plex DBs live (e.g. `/mnt/user/appdata/plex/.../Databases`). Open the Web UI on **http://Tower:5000** (or your Unraid IP) and set **Start browse at** to `/data`.

## Run on Unraid

Run the app **on the Unraid server** so the file browser can see `/mnt/user` and your Plex DB paths.

1. **SSH into Unraid**  
   Enable SSH in **Settings → SSH** (or use the terminal from the Unraid web UI). From your PC:
   ```bash
   ssh root@Tower
   ```
   (Use your Unraid IP or hostname instead of `Tower` if needed.)

2. **Install Python 3** (if not already installed)  
   Unraid doesn’t ship Python by default. Install the **NerdPack** plugin (Community Applications → search “NerdPack”), then in **Settings → NerdPack** enable **Python 3** and click **Apply**.

3. **Download the project from GitHub**  
   Replace `YOUR_USERNAME` and `YOUR_REPO` with your GitHub user and repo (e.g. `sam` / `plexdb`):
   ```bash
   cd /boot
   git clone https://github.com/YOUR_USERNAME/plexdb.git
   cd plexdb
   ```
   If `git` isn’t available, download the ZIP from your repo’s **Code → Download ZIP**, copy it to Unraid (e.g. via flash or SMB), then:
   ```bash
   cd /boot
   unzip plexdb-main.zip
   mv plexdb-main plexdb
   cd plexdb
   ```

4. **Create a virtual environment and install dependencies**
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

5. **Run the app** so you can open it from your browser on the network:
   ```bash
   BROWSE_ROOT=/mnt/user .venv/bin/python app.py --host 0.0.0.0 --port 5000
   ```
   Then open **http://Tower:5000** (or **http://YOUR-UNRAID-IP:5000**) in your browser. Use **Browse** to select your old backup DB, new (corrupt) DB, and output path.

6. **Optional: run in background**  
   To keep it running after you close SSH (until reboot), use `nohup` or run inside `screen`/`tmux`:
   ```bash
   nohup env BROWSE_ROOT=/mnt/user .venv/bin/python app.py --host 0.0.0.0 --port 5000 >> /var/log/plexdb-merge.log 2>&1 &
   ```

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
- **Disk full during recovery**: Recovery writes a large temp SQL file (often as big as the DB or larger). If you see “database or disk is full”, free space on the drive that holds `/tmp` (or set `TMPDIR` to a path on a bigger drive, e.g. `TMPDIR=/mnt/user/tmp python app.py`).
- **Testing**: Prefer testing with copies of the DBs (e.g. in a temp folder) before replacing the live file.

## Where is the Plex DB?

Typical paths:

- **Windows**: `%LOCALAPPDATA%\Plex Media Server\Plug-in Support\Databases\`
- **Linux**: `$PLEX_HOME/Library/Application Support/Plex Media Server/Plug-in Support/Databases/` (or similar under `/var/lib/plexmediaserver` etc.)
- **Docker (e.g. linuxserver/plex)**: `/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases/`

The main file is `com.plexapp.plugins.library.db`.

## License

Use and modify as you like; no warranty. Back up your data before running.
