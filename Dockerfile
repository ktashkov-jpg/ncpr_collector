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

RUN groupadd --gid "${HOST_GID}" appgroup \
    && useradd \
        --uid "${HOST_UID}" \
        --gid "${HOST_GID}" \
        --create-home \
        --home-dir /tmp/app-home \
        --shell /bin/bash \
        appuser

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
