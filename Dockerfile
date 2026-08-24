FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.lock .
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --system evoagent \
    && useradd --system --gid evoagent --home-dir /app --shell /usr/sbin/nologin evoagent \
    && install -d -o evoagent -g evoagent /app/dynamic-skills

COPY --chown=evoagent:evoagent evoagent ./evoagent
COPY --chown=evoagent:evoagent web ./web
COPY --chown=evoagent:evoagent skills ./skills

USER evoagent
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/ready' % os.getenv('EVOAGENT_PORT','8080'), timeout=2)"

CMD ["python", "-m", "evoagent"]

