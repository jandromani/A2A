#!/usr/bin/env bash
set -euo pipefail

command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 1; }
command -v helm >/dev/null || { echo "helm is required" >&2; exit 1; }
command -v terraform >/dev/null || { echo "terraform is required" >&2; exit 1; }

TERRAFORM_DIR="${TERRAFORM_DIR:-$(cd "$(dirname "$0")/../aws/production" && pwd)}"
AWS_REGION="${AWS_REGION:-$(terraform -chdir="$TERRAFORM_DIR" output -raw region)}"
CLUSTER_NAME="${CLUSTER_NAME:-$(terraform -chdir="$TERRAFORM_DIR" output -raw cluster_name)}"
VPC_ID="$(terraform -chdir="$TERRAFORM_DIR" output -raw vpc_id)"
LOAD_BALANCER_ROLE_ARN="$(terraform -chdir="$TERRAFORM_DIR" output -raw load_balancer_controller_role_arn)"
LOAD_BALANCER_CHART_VERSION="${LOAD_BALANCER_CHART_VERSION:-3.6.0}"

aws eks update-kubeconfig --region "$AWS_REGION" --name "$CLUSTER_NAME"
kubectl create serviceaccount aws-load-balancer-controller -n kube-system \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl annotate serviceaccount aws-load-balancer-controller -n kube-system \
  "eks.amazonaws.com/role-arn=$LOAD_BALANCER_ROLE_ARN" --overwrite
helm repo add eks https://aws.github.io/eks-charts --force-update
helm repo update eks
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --namespace kube-system \
  --version "$LOAD_BALANCER_CHART_VERSION" \
  --set clusterName="$CLUSTER_NAME" \
  --set region="$AWS_REGION" \
  --set vpcId="$VPC_ID" \
  --set replicaCount=2 \
  --set serviceAccount.create=false \
  --set serviceAccount.name=aws-load-balancer-controller \
  --wait --timeout 10m
kubectl -n kube-system rollout status deployment/aws-load-balancer-controller --timeout=5m

echo "AWS Load Balancer Controller is ready. Apply the rendered platform ingress and run deploy/aws/production/publish_dns.sh to create the Route 53 alias."
