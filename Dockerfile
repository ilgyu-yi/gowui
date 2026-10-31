# The gowui server-mode image (SPEC §10.1). Configured only by the GOWUI_* variables of §10.
#
# The base is pinned by digest (Dependabot proposes bumps). The dependencies are floor-bounded, so
# the build is not reproducible: the digest pins the OS and Python only.

FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
WORKDIR /src
# Exactly the .dockerignore allowlist.
COPY pyproject.toml README.md ./
COPY gowui ./gowui
RUN /opt/venv/bin/pip install .

FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    GOWUI_DB=/data/gowui.db
RUN groupadd --system --gid 10001 gowui \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin gowui \
    && install -d -m 0700 -o 10001 -g 10001 /data
COPY --from=builder /opt/venv /opt/venv
USER 10001:10001
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
ENTRYPOINT ["gowui"]
CMD ["serve"]
