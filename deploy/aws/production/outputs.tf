output "cluster_name" {
  value       = aws_eks_cluster.platform.name
  description = "EKS cluster name for kubectl configuration."
}

output "public_domain" {
  value       = var.public_domain
  description = "Public application hostname provisioned in Route 53."
}

output "route53_zone_id" {
  value       = data.aws_route53_zone.public.zone_id
  description = "Public Route 53 zone for certificate validation and the application alias."
}

output "public_certificate_arn" {
  value       = aws_acm_certificate_validation.public.certificate_arn
  description = "DNS-validated ACM certificate for the application ALB."
}

output "application_image_repository" {
  value       = aws_ecr_repository.application.repository_url
  description = "Private immutable ECR repository for the A2A application image."
}

output "operations_image_repository" {
  value       = aws_ecr_repository.operations.repository_url
  description = "Private immutable ECR repository for database backup and audit jobs."
}

output "cluster_endpoint" {
  value       = aws_eks_cluster.platform.endpoint
  description = "Private-and-allowlisted EKS API endpoint."
}

output "region" {
  value       = var.region
  description = "AWS region for all resources."
}

output "postgres_address" {
  value       = aws_db_instance.platform.address
  description = "Private RDS hostname; password is never included."
}

output "postgres_port" {
  value       = aws_db_instance.platform.port
  description = "RDS PostgreSQL port."
}

output "postgres_database" {
  value       = var.database_name
  description = "RDS database name."
}

output "postgres_admin_username" {
  value       = var.database_admin_username
  description = "RDS migration/bootstrap username; do not use for the API runtime."
}

output "postgres_runtime_username" {
  value       = var.database_runtime_username
  description = "Least-privilege application username to be created by bootstrap_database.py."
}

output "postgres_admin_password" {
  value       = var.database_admin_password
  sensitive   = true
  description = "RDS migration/bootstrap password for the Vault and database bootstrap tools."
}

output "valkey_primary_endpoint" {
  value       = aws_elasticache_replication_group.platform.primary_endpoint_address
  description = "Private Valkey primary endpoint."
}

output "valkey_port" {
  value       = aws_elasticache_replication_group.platform.port
  description = "TLS Valkey port."
}

output "valkey_auth_token" {
  value       = var.valkey_auth_token
  sensitive   = true
  description = "Valkey AUTH token for the Vault bootstrap tool."
}

output "audit_bucket_name" {
  value       = aws_s3_bucket.archive.bucket
  description = "Private versioned bucket with default COMPLIANCE retention."
}

output "backup_bucket_name" {
  value       = aws_s3_bucket.backup.bucket
  description = "Separate private backup bucket with short COMPLIANCE retention."
}

output "audit_bucket_arn" {
  value       = aws_s3_bucket.archive.arn
  description = "Audit archive bucket ARN."
}

output "audit_kms_key_arn" {
  value       = aws_kms_key.s3.arn
  description = "KMS key used by the WORM archive."
}

output "audit_archiver_role_arn" {
  value       = aws_iam_role.audit_archiver.arn
  description = "IRSA role to annotate a2a-audit-archiver ServiceAccount."
}

output "backup_writer_role_arn" {
  value       = aws_iam_role.backup_writer.arn
  description = "IRSA role to annotate the a2a-postgres-backup ServiceAccount."
}

output "audit_retention_days" {
  value       = var.audit_retention_days
  description = "Default immutable audit retention, in days."
}

output "backup_kms_key_arn" {
  value       = aws_kms_key.s3.arn
  description = "KMS key for backup and audit objects."
}

output "efs_file_system_id" {
  value       = aws_efs_file_system.lake.id
  description = "Encrypted shared filesystem ID for the Kubernetes RWX StorageClass."
}

output "efs_csi_role_arn" {
  value       = aws_iam_role.efs_csi.arn
  description = "IRSA role for the EFS CSI controller add-on."
}

output "vpc_id" {
  value       = aws_vpc.platform.id
  description = "VPC ID used by the AWS Load Balancer Controller."
}

output "load_balancer_controller_role_arn" {
  value       = aws_iam_role.load_balancer_controller.arn
  description = "IRSA role restricted to the AWS Load Balancer Controller service account."
}

output "database_kms_key_arn" {
  value       = aws_kms_key.rds.arn
  description = "KMS key for RDS and EKS envelope encryption."
}
