FROM docker:29.7.2-cli@sha256:000bb62ff495f986c9f5578eb67cc2cb98b91138eda81d7762d5371eb8a497fe AS docker-client

FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS runtime

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

RUN install -d -m 0700 -o evoagent -g evoagent /run/evoagent-proof
USER evoagent
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/ready' % os.getenv('EVOAGENT_PORT','8080'), timeout=2)"

CMD ["python", "-m", "evoagent"]

# Only the trusted, explicitly selected executor target contains a Docker client.
FROM runtime AS proof-executor
COPY --from=docker-client /usr/local/bin/docker /usr/local/bin/docker
HEALTHCHECK --interval=10s --timeout=6s --start-period=10s --retries=3 \
    CMD python -c "from evoagent.proof_service import SocketProofExecutor; SocketProofExecutor('/run/evoagent-proof/executor.sock')"
CMD ["python", "-m", "evoagent.proof_service"]

# Keep ordinary builds as the unprivileged Web/API image without Docker access.
FROM runtime AS application

