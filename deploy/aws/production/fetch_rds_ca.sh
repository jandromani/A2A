#!/usr/bin/env bash
set -euo pipefail

OUTPUT_PATH="${1:?Pass a private local path for the downloaded RDS CA bundle}"
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
mkdir -p "$(dirname -- "$OUTPUT_PATH")"
umask 077
curl --fail --show-error --location --retry 3 \
  https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
  --output "$OUTPUT_PATH"
chmod 600 "$OUTPUT_PATH"
printf 'Saved AWS RDS CA bundle to %s\n' "$OUTPUT_PATH"
