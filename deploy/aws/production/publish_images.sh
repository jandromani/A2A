#!/usr/bin/env bash
set -euo pipefail

command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v terraform >/dev/null || { echo "terraform is required" >&2; exit 1; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
APP_REPOSITORY="$(terraform -chdir="$SCRIPT_DIR" output -raw application_image_repository)"
OPS_REPOSITORY="$(terraform -chdir="$SCRIPT_DIR" output -raw operations_image_repository)"
AWS_REGION="$(terraform -chdir="$SCRIPT_DIR" output -raw region)"
IMAGE_TAG="${IMAGE_TAG:-release-$(git -C "$ROOT_DIR" rev-parse --short=12 HEAD)}"
REGISTRY="${APP_REPOSITORY%%/*}"
if [[ "$IMAGE_TAG" == "latest" || ! "$IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "IMAGE_TAG must be an immutable, non-latest Docker tag" >&2
  exit 1
fi

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"
docker build --pull -f "$ROOT_DIR/Dockerfile" -t "$APP_REPOSITORY:$IMAGE_TAG" "$ROOT_DIR"
docker build --pull -f "$ROOT_DIR/deploy/backup/Dockerfile" -t "$OPS_REPOSITORY:$IMAGE_TAG" "$ROOT_DIR"
docker push "$APP_REPOSITORY:$IMAGE_TAG"
docker push "$OPS_REPOSITORY:$IMAGE_TAG"

printf 'A2A_IMAGE=%s:%s\nBACKUP_IMAGE=%s:%s\n' \
  "$APP_REPOSITORY" "$IMAGE_TAG" "$OPS_REPOSITORY" "$IMAGE_TAG"
