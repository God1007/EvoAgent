FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EVOAGENT_DB_PATH=/data/evoagent.db

WORKDIR /app

COPY requirements.lock .
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --system evoagent \
    && useradd --system --gid evoagent --home-dir /app --shell /usr/sbin/nologin evoagent \
    && mkdir -p /data \
    && chown evoagent:evoagent /data

COPY --chown=evoagent:evoagent evoagent ./evoagent
COPY --chown=evoagent:evoagent web ./web
COPY --chown=evoagent:evoagent skills ./skills

USER evoagent
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"

CMD ["python", "-m", "evoagent"]

