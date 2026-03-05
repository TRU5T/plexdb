# Plex DB Merge – run with bundled sqlite3 for .recover support
FROM python:3.11-slim

WORKDIR /app

# Install sqlite3 CLI, gosu (for PUID/PGID), plus curl/unzip
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    gosu \
    unzip \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && gosu nobody true

# App and deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py plex_db_merge.py ./

# Entrypoint: ensure /data exists and is owned by PUID:PGID, then run app as that user
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 5000

ENV BROWSE_ROOT=/data
ENV PUID=99
ENV PGID=100
VOLUME /data

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "app.py"]
