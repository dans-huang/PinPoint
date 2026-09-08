FROM rust:1.83-slim-bookworm AS rust-toolchain

FROM python:3.13-slim-bookworm AS builder

# pyca's current prebuilt ARM64 wheels use CPU instructions that are not
# available in every virtualized Apple-silicon Docker environment. Build only
# cryptography from source against Debian's OpenSSL; keep the Rust/compiler
# toolchain out of the final image.
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential libffi-dev libssl-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir \
        --no-binary=cryptography -r requirements.txt

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PINPOINT_STATE_DB_PATH=/var/lib/pinpoint/pinpoint.sqlite3 \
    PATH=/opt/venv/bin:$PATH

RUN groupadd --system --gid 10001 pinpoint \
    && useradd --system --uid 10001 --gid pinpoint --home-dir /nonexistent --shell /usr/sbin/nologin pinpoint \
    && install -d -o pinpoint -g pinpoint -m 0700 /var/lib/pinpoint

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=pinpoint:pinpoint pinpoint_backend ./pinpoint_backend

USER 10001:10001
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3); assert r.status == 200 and json.load(r) == {'status':'ok'}"]

# One process is intentional: every deployment must mount one durable SQLite
# volume and run exactly one persistent replica.
CMD ["python", "-m", "uvicorn", "pinpoint_backend.app:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1", "--no-access-log"]
