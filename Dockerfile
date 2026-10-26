FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 DATA_DIR=/data
WORKDIR /app
COPY papertrail ./papertrail
COPY samples ./samples
COPY LICENSE README.md ./

RUN useradd --create-home --uid 10001 papertrail && mkdir /data && chown papertrail:papertrail /data
USER papertrail

VOLUME ["/data"]
EXPOSE 8000
# The server shuts down cleanly on Ctrl+C; SIGINT lets `docker stop` do the same.
STOPSIGNAL SIGINT
CMD ["sh", "-c", "exec python3 -m papertrail.web --host 0.0.0.0 --port \"$PORT\""]
