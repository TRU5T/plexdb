# Plex DB Merge – run with bundled sqlite3 for .recover support
FROM python:3.11-slim

WORKDIR /app

# Install unzip for sqlite tools; optional curl/wget for download
RUN apt-get update && apt-get install -y --no-install-recommends \
    unzip \
    curl \
    && rm -rf /var/lib/apt/lists/*

# App and deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py plex_db_merge.py ./
RUN mkdir -p bin
# Bundle sqlite3 (Linux x64) for .recover; or copy repo bin/ before this and comment out next RUN
ARG SQLITE_TOOLS_URL=https://www.sqlite.org/2026/sqlite-tools-linux-x64-3510200.zip
RUN curl -fsSLo /tmp/sqlite.zip "${SQLITE_TOOLS_URL}" \
    && unzip -j -o /tmp/sqlite.zip "sqlite-tools-linux-x64-3510200/sqlite3" -d /app/bin \
    && chmod +x /app/bin/sqlite3 \
    && rm /tmp/sqlite.zip \
    || true

EXPOSE 5000

ENV BROWSE_ROOT=/data
VOLUME /data

CMD ["python", "app.py"]
