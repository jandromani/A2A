FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY packages/tools-sdk ./packages/tools-sdk
COPY alembic.ini ./
COPY migrations ./migrations

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl \
  && curl --fail --show-error --location --retry 3 \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    -o /etc/ssl/certs/rds-global-bundle.pem \
  && apt-get purge -y --auto-remove curl \
  && rm -rf /var/lib/apt/lists/* \
  && pip install --upgrade pip \
  && pip install ./packages/tools-sdk .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
