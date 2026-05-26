FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY seerr.py store.py radarr.py sonarr.py plex.py bot.py ./

# /data holds the SQLite store (user mappings + auto-fix audit)
VOLUME ["/data"]
ENV STORE_PATH=/data/mappings.sqlite

CMD ["python", "bot.py"]
