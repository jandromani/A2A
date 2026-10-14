variable "name" {
  description = "Short resource prefix."
  type        = string
  default     = "a2a-prod"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,19}$", var.name))
    error_message = "name must be 3-20 lowercase letters, digits, or hyphens and start with a letter."
  }
}

variable "region" {
  description = "AWS region in which the private production stack will run."
  type        = string
}

variable "public_domain" {
  description = "Public hostname for the A2A application; it must be in the selected Route 53 hosted zone."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9.-]*\\.[A-Za-z]{2,}$", var.public_domain)) && !strcontains(var.public_domain, "..")
    error_message = "public_domain must be a valid DNS hostname without scheme, path, or wildcard."
  }
}

variable "route53_zone_id" {
  description = "ID of the existing public Route 53 hosted zone for public_domain."
  type        = string
  validation {
    condition     = can(regex("^Z[A-Z0-9]+$", var.route53_zone_id))
    error_message = "route53_zone_id must be an existing Route 53 hosted-zone ID."
  }
}

variable "cluster_admin_cidrs" {
  description = "Public IPv4 CIDR allowlist for the EKS control-plane endpoint; use trusted VPN or runner ranges only."
  type        = list(string)
  validation {
    condition     = length(var.cluster_admin_cidrs) > 0 && alltrue([for cidr in var.cluster_admin_cidrs : can(cidrnetmask(cidr))])
    error_message = "Set one or more valid trusted IPv4 CIDRs; the EKS API is not open to the internet."
  }
}

variable "vpc_cidr" {
  description = "Production VPC IPv4 range; must be a /16 so nine isolated /24 subnets fit."
  type        = string
  default     = "10.40.0.0/16"
  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && tonumber(split("/", var.vpc_cidr)[1]) == 16
    error_message = "vpc_cidr must be a valid /16 CIDR."
  }
}

variable "kubernetes_version" {
  description = "Pinned EKS Kubernetes minor version supported in the selected region."
  type        = string
  default     = "1.35"
  validation {
    condition     = can(regex("^1\\.[0-9]{2}$", var.kubernetes_version))
    error_message = "kubernetes_version must be a Kubernetes major.minor version."
  }
}

variable "node_instance_types" {
  description = "EKS managed-node instance types for the application and platform services."
  type        = list(string)
  default     = ["m7i.large"]
}

variable "database_name" {
  description = "PostgreSQL database name."
  type        = string
  default     = "agentic"
  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{2,62}$", var.database_name))
    error_message = "database_name must be a valid lowercase PostgreSQL database name."
  }
}

variable "database_admin_username" {
  description = "PostgreSQL migration/bootstrap owner account; never use it for the API runtime."
  type        = string
  default     = "a2a_db_admin"
  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{2,62}$", var.database_admin_username))
    error_message = "database_admin_username must be a valid lowercase PostgreSQL role name."
  }
}

variable "database_runtime_username" {
  description = "Least-privilege application role created by bootstrap_database.py."
  type        = string
  default     = "a2a_runtime"
  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{2,62}$", var.database_runtime_username))
    error_message = "database_runtime_username must be a valid lowercase PostgreSQL role name."
  }
}

variable "database_admin_password" {
  description = "High-entropy migration/admin password, injected through a protected CI/secret-manager variable."
  type        = string
  sensitive   = true
  validation {
    condition     = length(var.database_admin_password) >= 24 && !can(regex("[\\r\\n]", var.database_admin_password))
    error_message = "database_admin_password must contain at least 24 characters and no line breaks."
  }
}

variable "database_instance_class" {
  description = "RDS instance class; production default is a Graviton general-purpose instance."
  type        = string
  default     = "db.r7g.large"
}

variable "database_storage_gb" {
  description = "Initial encrypted RDS storage in GiB; RDS autoscaling is enabled up to 2 TiB."
  type        = number
  default     = 100
  validation {
    condition     = var.database_storage_gb >= 100 && var.database_storage_gb <= 2048
    error_message = "database_storage_gb must be between 100 and 2048 GiB."
  }
}

variable "postgres_engine_version" {
  description = "PostgreSQL major or minor version available in the selected AWS region."
  type        = string
  default     = "17.11"
}

variable "valkey_auth_token" {
  description = "High-entropy ElastiCache AUTH token, injected through a protected CI/secret-manager variable."
  type        = string
  sensitive   = true
  validation {
    condition     = length(var.valkey_auth_token) >= 32 && !can(regex("[\\r\\n]", var.valkey_auth_token))
    error_message = "valkey_auth_token must contain at least 32 characters and no line breaks."
  }
}

variable "valkey_node_type" {
  description = "ElastiCache Valkey node size."
  type        = string
  default     = "cache.r7g.large"
}

variable "valkey_engine_version" {
  description = "Valkey engine version supported in the selected region."
  type        = string
  default     = "8.0"
}

variable "audit_bucket_name" {
  description = "Globally unique S3 bucket name dedicated to the immutable audit chain."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.audit_bucket_name)) && !can(regex("\\.\\.", var.audit_bucket_name))
    error_message = "audit_bucket_name must be a valid globally unique S3 DNS bucket name."
  }
}

variable "backup_bucket_name" {
  description = "Globally unique S3 bucket name dedicated to database and data-lake backups."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.backup_bucket_name)) && !can(regex("\\.\\.", var.backup_bucket_name))
    error_message = "backup_bucket_name must be a valid globally unique S3 DNS bucket name."
  }
}

variable "audit_retention_days" {
  description = "S3 Object Lock COMPLIANCE retention for each audit event."
  type        = number
  default     = 2557
  validation {
    condition     = var.audit_retention_days >= 365 && var.audit_retention_days <= 36500
    error_message = "audit_retention_days must be at least 365 days and at most 100 years."
  }
}

variable "backup_retention_days" {
  description = "RDS automated backup and EFS backup retention."
  type        = number
  default     = 35
  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35
    error_message = "backup_retention_days must be between 7 and 35 days."
  }
}
