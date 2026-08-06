FROM python:3.11-slim

ARG HOST_UID=1000
ARG HOST_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ca-certificates is the actual fix for the TLS problem the runbook worked
# around with `curl -k` (§13): the host could not build the Let's Encrypt
# issuer chain. Installing a current CA store in the image means the
# collector can verify certificates properly and NCPR_INSECURE_TLS stays 0.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libxml2-utils \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create the runtime identity only if that uid/gid is not already present.
# On a host where the operator is root, HOST_UID/HOST_GID are 0 and a plain
# groupadd/useradd fails because root already exists -- which broke the build
# rather than the run, so it looked like a code fault. /tmp/app-home is
# created unconditionally because an existing account (root) will not get it
# from useradd.
RUN if ! getent group "${HOST_GID}" >/dev/null 2>&1; then \
        groupadd --gid "${HOST_GID}" appgroup; \
    fi \
    && if ! getent passwd "${HOST_UID}" >/dev/null 2>&1; then \
        useradd \
            --uid "${HOST_UID}" \
            --gid "${HOST_GID}" \
            --create-home \
            --home-dir /tmp/app-home \
            --shell /bin/bash \
            appuser; \
    fi \
    && mkdir -p /tmp/app-home \
    && chown "${HOST_UID}:${HOST_GID}" /tmp/app-home

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY docs ./docs
COPY Dockerfile ./Dockerfile
COPY docker-compose.yml ./docker-compose.yml
COPY README.md .

CMD ["python", "-m", "app.main", "--help"]
