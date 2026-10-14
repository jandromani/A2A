#!/usr/bin/env bash
set -euo pipefail

command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v terraform >/dev/null || { echo "terraform is required" >&2; exit 1; }

TERRAFORM_DIR="${TERRAFORM_DIR:-$(cd "$(dirname "$0")" && pwd)}"
AWS_REGION="${AWS_REGION:-$(terraform -chdir="$TERRAFORM_DIR" output -raw region)}"
CLUSTER_NAME="${CLUSTER_NAME:-$(terraform -chdir="$TERRAFORM_DIR" output -raw cluster_name)}"
PUBLIC_HOST="$(terraform -chdir="$TERRAFORM_DIR" output -raw public_domain)"
ROUTE53_ZONE_ID="$(terraform -chdir="$TERRAFORM_DIR" output -raw route53_zone_id)"

aws eks update-kubeconfig --region "$AWS_REGION" --name "$CLUSTER_NAME"
ALB_DNS=""
for _ in $(seq 1 60); do
  ALB_DNS="$(kubectl -n a2a get ingress a2a-api \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  if [[ -n "$ALB_DNS" ]]; then
    break
  fi
  sleep 10
done
if [[ -z "$ALB_DNS" ]]; then
  echo "Ingress has no ALB hostname; check AWS Load Balancer Controller events." >&2
  exit 1
fi

ALB_ZONE_ID="$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
  --query "LoadBalancers[?DNSName=='${ALB_DNS}'].CanonicalHostedZoneId | [0]" \
  --output text)"
if [[ -z "$ALB_ZONE_ID" || "$ALB_ZONE_ID" == "None" ]]; then
  echo "Could not resolve the Route 53 canonical zone for ALB $ALB_DNS." >&2
  exit 1
fi

CHANGE_BATCH="$(mktemp)"
trap 'rm -f "$CHANGE_BATCH"' EXIT
python3 - "$PUBLIC_HOST" "$ALB_DNS" "$ALB_ZONE_ID" > "$CHANGE_BATCH" <<'PY'
import json
import sys

host, alb_dns, zone_id = sys.argv[1:]
batch = {
    "Comment": "A2A production alias to the managed application ALB",
    "Changes": [
        {
            "Action": "UPSERT",
            "ResourceRecordSet": {
                "Name": host,
                "Type": "A",
                "AliasTarget": {
                    "HostedZoneId": zone_id,
                    "DNSName": alb_dns,
                    "EvaluateTargetHealth": True,
                },
            },
        }
    ],
}
print(json.dumps(batch))
PY
aws route53 change-resource-record-sets --hosted-zone-id "$ROUTE53_ZONE_ID" \
  --change-batch "file://$CHANGE_BATCH"
echo "Route 53 alias $PUBLIC_HOST -> $ALB_DNS submitted."
