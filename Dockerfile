FROM python:3.15-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUXRIS_DB_PATH=/data/auxris.sqlite3 \
    AUXRIS_ALLOWED_HOSTS=localhost,127.0.0.1

RUN groupadd --system auxris && useradd --system --gid auxris --home-dir /app auxris
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"

RUN mkdir -p /data && chown auxris:auxris /data
USER auxris
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2)"

CMD ["uvicorn", "auxris.server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

