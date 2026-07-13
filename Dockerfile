FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Glob, not an explicit list: v0.12.0 shipped broken because two new root
# modules weren't added to the old hand-maintained list. Every root .py is
# an app module, so copy them all.
COPY *.py ./
COPY bot/ ./bot/

# /data holds settings.json, the SQLite mappings DB, the encryption key,
# the session secret, and the Plex client id
VOLUME ["/data"]
ENV STORE_PATH=/data/mappings.sqlite

# HTTP server: /webhook/seerr (webhook receiver) + /admin (webui)
EXPOSE 8765

CMD ["python", "-m", "bot"]
