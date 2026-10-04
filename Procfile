# `python -u` (and PYTHONUNBUFFERED) matter here for the same reason they do
# in the Dockerfile: with stdout attached to a pipe, Python buffers every
# print() — including the rotating owner-claim code the setup wizard needs
# the admin to read out of the logs — until the buffer flushes or the process
# exits, so nothing appears live.
web: PYTHONUNBUFFERED=1 python -u bot.py
