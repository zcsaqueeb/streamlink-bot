FROM python:3.11.9-slim

WORKDIR /app

# FFmpeg powers subtitle/audio-track extraction for the web player
# (web/media.py); libmagic1 lets web/cache.py sniff real file types when a
# name/extension is missing or wrong. Both are strictly optional at runtime —
# the code probes for them and degrades quietly — but without them in the
# image, every deployment silently loses those features even though the bot
# reports no error. --no-install-recommends keeps ffmpeg from dragging in
# its whole docs/examples dependency tree (~200MB+ of it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
# Without this, Python fully buffers stdout when it isn't attached to a
# terminal (true for basically every container host — Railway, Render,
# Docker logs, etc.). That silently swallows every print() — including
# the rotating owner-claim code from owner_claim.py — until the buffer
# happens to flush or the process exits, so nothing shows up "live" in
# the logs even though the code is being generated correctly.
ENV PYTHONUNBUFFERED=1
EXPOSE $PORT

# The web server answers /health (a cheap, dependency-free 200 that deliberately
# never touches Telegram or the DB), so a hung event loop gets caught here
# instead of looking healthy while serving nothing.
#
# But the web server only starts once a site URL is configured (info.py:112,
# `WEB_SERVER = bool(URL)`), and a brand-new container has no bot_settings.json
# yet — it's sitting in the owner-claim /start wizard waiting for exactly that.
# Probing unconditionally would mark it unhealthy during the one window where
# an operator is most needed, and an orchestrator would restart-loop it before
# setup could finish. So: no listener on this port means the web layer is
# deliberately off, which is healthy; a listener that doesn't answer 200 is not.
# stdlib urllib only: no curl/wget needed in the slim image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os,socket,sys,urllib.request;p=os.environ.get('PORT','8080');s=socket.socket();r=s.connect_ex(('127.0.0.1',int(p)));s.close();sys.exit(0 if r else (0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/health',timeout=4).status==200 else 1))"]

CMD ["python", "-u", "bot.py"]
