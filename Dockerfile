FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

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

