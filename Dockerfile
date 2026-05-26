FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY seerr.py store.py radarr.py sonarr.py plex.py webhook.py webui.py settings.py bot.py ./

# /data holds settings.json, the SQLite mappings DB, the encryption key,
# the session secret, and the Plex client id
VOLUME ["/data"]
ENV STORE_PATH=/data/mappings.sqlite

# HTTP server: /webhook/seerr (webhook receiver) + /admin (webui)
EXPOSE 8765

CMD ["python", "bot.py"]
