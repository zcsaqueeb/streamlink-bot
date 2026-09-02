FROM python:3.11.9-slim

WORKDIR /app

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

CMD ["python", "-u", "bot.py"]
