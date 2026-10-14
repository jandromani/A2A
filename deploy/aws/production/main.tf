data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

data "aws_route53_zone" "public" {
  zone_id = var.route53_zone_id
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 3)
  tags = {
    Application = "a2a"
    Environment = "production"
  }
  public_subnets = { for index, az in local.azs : az => cidrsubnet(var.vpc_cidr, 8, index) }
  app_subnets    = { for index, az in local.azs : az => cidrsubnet(var.vpc_cidr, 8, index + 3) }
  data_subnets   = { for index, az in local.azs : az => cidrsubnet(var.vpc_cidr, 8, index + 6) }
  oidc_issuer    = aws_eks_cluster.platform.identity[0].oidc[0].issuer
  oidc_host      = replace(local.oidc_issuer, "https://", "")
}

resource "aws_vpc" "platform" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = "${var.name}-vpc" })
}

resource "aws_internet_gateway" "platform" {
  vpc_id = aws_vpc.platform.id
  tags   = merge(local.tags, { Name = "${var.name}-igw" })
}

resource "aws_subnet" "public" {
  for_each                = local.public_subnets
  vpc_id                  = aws_vpc.platform.id
  availability_zone       = each.key
  cidr_block              = each.value
  map_public_ip_on_launch = true
  tags = merge(local.tags, {
    Name                                = "${var.name}-public-${each.key}"
    "kubernetes.io/cluster/${var.name}" = "shared"
    "kubernetes.io/role/elb"            = "1"
  })
}

resource "aws_subnet" "app" {
  for_each          = local.app_subnets
  vpc_id            = aws_vpc.platform.id
  availability_zone = each.key
  cidr_block        = each.value
  tags = merge(local.tags, {
    Name                                = "${var.name}-app-${each.key}"
    "kubernetes.io/cluster/${var.name}" = "shared"
    "kubernetes.io/role/internal-elb"   = "1"
  })
}

resource "aws_subnet" "data" {
  for_each          = local.data_subnets
  vpc_id            = aws_vpc.platform.id
  availability_zone = each.key
  cidr_block        = each.value
  tags              = merge(local.tags, { Name = "${var.name}-data-${each.key}" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.platform.id
  tags   = merge(local.tags, { Name = "${var.name}-public" })
}

resource "aws_route" "public_default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.platform.id
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  for_each = aws_subnet.public
  domain   = "vpc"
  tags     = merge(local.tags, { Name = "${var.name}-nat-${each.key}" })
}

resource "aws_nat_gateway" "nat" {
  for_each      = aws_subnet.public
  allocation_id = aws_eip.nat[each.key].id
  subnet_id     = each.value.id
  depends_on    = [aws_internet_gateway.platform]
  tags          = merge(local.tags, { Name = "${var.name}-nat-${each.key}" })
}

resource "aws_route_table" "app" {
  for_each = aws_subnet.app
  vpc_id   = aws_vpc.platform.id
  tags     = merge(local.tags, { Name = "${var.name}-app-${each.key}" })
}

resource "aws_route" "app_default" {
  for_each               = aws_route_table.app
  route_table_id         = each.value.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.nat[each.key].id
}

resource "aws_route_table_association" "app" {
  for_each       = aws_subnet.app
  subnet_id      = each.value.id
  route_table_id = aws_route_table.app[each.key].id
}

resource "aws_route_table" "data" {
  for_each = aws_subnet.data
  vpc_id   = aws_vpc.platform.id
  tags     = merge(local.tags, { Name = "${var.name}-data-${each.key}" })
}

resource "aws_route_table_association" "data" {
  for_each       = aws_subnet.data
  subnet_id      = each.value.id
  route_table_id = aws_route_table.data[each.key].id
}

resource "aws_kms_key" "rds" {
  description             = "Encryption key for A2A PostgreSQL and EKS secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = merge(local.tags, { Name = "${var.name}-rds-key" })
}

resource "aws_kms_key" "s3" {
  description             = "Encryption key for A2A WORM and backup bucket"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = merge(local.tags, { Name = "${var.name}-s3-key" })
}

resource "aws_kms_key" "efs" {
  description             = "Encryption key for A2A shared data lake"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = merge(local.tags, { Name = "${var.name}-efs-key" })
}

resource "aws_cloudwatch_log_group" "eks" {
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.rds.arn
  tags              = local.tags
}

resource "aws_iam_role" "eks_cluster" {
  name = "${var.name}-eks-cluster"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_security_group" "eks_control_plane" {
  name        = "${var.name}-control-plane"
  description = "EKS control plane managed security group"
  vpc_id      = aws_vpc.platform.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}

resource "aws_eks_cluster" "platform" {
  name     = var.name
  role_arn = aws_iam_role.eks_cluster.arn
  version  = var.kubernetes_version
  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }
  vpc_config {
    subnet_ids              = [for subnet in aws_subnet.app : subnet.id]
    security_group_ids      = [aws_security_group.eks_control_plane.id]
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.cluster_admin_cidrs
  }
  encryption_config {
    provider { key_arn = aws_kms_key.rds.arn }
    resources = ["secrets"]
  }
  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster_policy,
    aws_cloudwatch_log_group.eks,
  ]
  tags = merge(local.tags, { Name = var.name })
}

resource "aws_iam_role" "eks_nodes" {
  name = "${var.name}-eks-nodes"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "eks_node_policies" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
  ])
  role       = aws_iam_role.eks_nodes.name
  policy_arn = each.value
}

resource "aws_eks_node_group" "platform" {
  cluster_name    = aws_eks_cluster.platform.name
  node_group_name = "${var.name}-workers"
  node_role_arn   = aws_iam_role.eks_nodes.arn
  subnet_ids      = [for subnet in aws_subnet.app : subnet.id]
  instance_types  = var.node_instance_types
  ami_type        = "AL2023_x86_64_STANDARD"
  scaling_config {
    min_size     = 3
    max_size     = 20
    desired_size = 3
  }
  update_config { max_unavailable = 1 }
  depends_on = [aws_iam_role_policy_attachment.eks_node_policies]
  tags       = local.tags
}

resource "aws_eks_addon" "core" {
  for_each                    = toset(["vpc-cni", "coredns", "kube-proxy", "metrics-server"])
  cluster_name                = aws_eks_cluster.platform.name
  addon_name                  = each.value
  resolve_conflicts_on_update = "PRESERVE"
  depends_on                  = [aws_eks_node_group.platform]
}

resource "aws_iam_role" "ebs_csi" {
  name = "${var.name}-ebs-csi"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
      } }
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicyV2"
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.platform.name
  addon_name                  = "aws-ebs-csi-driver"
  service_account_role_arn    = aws_iam_role.ebs_csi.arn
  resolve_conflicts_on_update = "PRESERVE"
  depends_on = [
    aws_eks_node_group.platform,
    aws_iam_role_policy_attachment.ebs_csi,
  ]
}

resource "aws_ecr_repository" "application" {
  name                 = "${var.name}/application"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
  lifecycle { prevent_destroy = true }
  tags = merge(local.tags, { Name = "${var.name}-application" })
}

resource "aws_ecr_repository" "operations" {
  name                 = "${var.name}/operations"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
  lifecycle { prevent_destroy = true }
  tags = merge(local.tags, { Name = "${var.name}-operations" })
}

resource "aws_ecr_lifecycle_policy" "application" {
  repository = aws_ecr_repository.application.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 30 newest immutable release images"
      selection = {
        tagStatus     = "tagged"
        tagPrefixList = ["release-"]
        countType     = "imageCountMoreThan"
        countNumber   = 30
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "operations" {
  repository = aws_ecr_repository.operations.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 30 newest immutable release images"
      selection = {
        tagStatus     = "tagged"
        tagPrefixList = ["release-"]
        countType     = "imageCountMoreThan"
        countNumber   = 30
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = local.oidc_issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
  depends_on      = [aws_eks_cluster.platform]
  tags            = local.tags
}

data "tls_certificate" "eks" {
  url = local.oidc_issuer
}

resource "aws_iam_role" "efs_csi" {
  name = "${var.name}-efs-csi"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = "system:serviceaccount:kube-system:efs-csi-controller-sa"
      } }
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "efs_csi" {
  role       = aws_iam_role.efs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"
}

resource "aws_eks_addon" "efs_csi" {
  cluster_name             = aws_eks_cluster.platform.name
  addon_name               = "aws-efs-csi-driver"
  service_account_role_arn = aws_iam_role.efs_csi.arn
  depends_on               = [aws_iam_role_policy_attachment.efs_csi]
}

resource "aws_iam_policy" "load_balancer_controller" {
  name        = "${var.name}-load-balancer-controller"
  description = "Pinned AWS Load Balancer Controller permissions for the A2A EKS cluster."
  policy      = file("${path.module}/aws-load-balancer-controller-policy.json")
  tags        = local.tags
}

resource "aws_iam_role" "load_balancer_controller" {
  name = "${var.name}-load-balancer-controller"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = "system:serviceaccount:kube-system:aws-load-balancer-controller"
      } }
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "load_balancer_controller" {
  role       = aws_iam_role.load_balancer_controller.name
  policy_arn = aws_iam_policy.load_balancer_controller.arn
}

resource "aws_acm_certificate" "public" {
  domain_name       = var.public_domain
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = endswith("${var.public_domain}.", data.aws_route53_zone.public.name)
      error_message = "public_domain must be inside the selected public Route 53 zone."
    }
  }
  tags = merge(local.tags, {
    Name = "${var.name}-public-certificate"
  })
}

resource "aws_route53_record" "public_certificate_validation" {
  for_each = {
    for option in aws_acm_certificate.public.domain_validation_options : option.domain_name => {
      name   = option.resource_record_name
      type   = option.resource_record_type
      record = option.resource_record_value
    }
  }
  zone_id         = data.aws_route53_zone.public.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "public" {
  certificate_arn         = aws_acm_certificate.public.arn
  validation_record_fqdns = [for record in aws_route53_record.public_certificate_validation : record.fqdn]
}

resource "aws_security_group" "rds" {
  name        = "${var.name}-postgres"
  description = "PostgreSQL only accepts connections from the EKS cluster security group"
  vpc_id      = aws_vpc.platform.id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_eks_cluster.platform.vpc_config[0].cluster_security_group_id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}

resource "aws_db_subnet_group" "platform" {
  name       = "${var.name}-database"
  subnet_ids = [for subnet in aws_subnet.data : subnet.id]
  tags       = local.tags
}

resource "aws_db_parameter_group" "platform" {
  name   = "${var.name}-postgres17"
  family = "postgres17"
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }
  tags = local.tags
}

resource "aws_iam_role" "rds_monitoring" {
  name = "${var.name}-rds-monitoring"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_db_instance" "platform" {
  identifier                   = "${var.name}-postgres"
  engine                       = "postgres"
  engine_version               = var.postgres_engine_version
  instance_class               = var.database_instance_class
  allocated_storage            = var.database_storage_gb
  max_allocated_storage        = 2048
  storage_type                 = "gp3"
  storage_encrypted            = true
  kms_key_id                   = aws_kms_key.rds.arn
  db_name                      = var.database_name
  username                     = var.database_admin_username
  password                     = var.database_admin_password
  port                         = 5432
  multi_az                     = true
  publicly_accessible          = false
  db_subnet_group_name         = aws_db_subnet_group.platform.name
  vpc_security_group_ids       = [aws_security_group.rds.id]
  parameter_group_name         = aws_db_parameter_group.platform.name
  backup_retention_period      = var.backup_retention_days
  backup_window                = "03:00-03:30"
  maintenance_window           = "sun:04:00-sun:05:00"
  auto_minor_version_upgrade   = true
  copy_tags_to_snapshot        = true
  deletion_protection          = true
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.name}-final"
  performance_insights_enabled = true
  monitoring_interval          = 60
  monitoring_role_arn          = aws_iam_role.rds_monitoring.arn
  apply_immediately            = false
  lifecycle { prevent_destroy = true }
  depends_on = [aws_iam_role_policy_attachment.rds_monitoring]
  tags       = merge(local.tags, { Name = "${var.name}-postgres" })
}

resource "aws_security_group" "valkey" {
  name        = "${var.name}-valkey"
  description = "TLS Valkey accepts connections only from EKS"
  vpc_id      = aws_vpc.platform.id
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_eks_cluster.platform.vpc_config[0].cluster_security_group_id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}

resource "aws_elasticache_subnet_group" "platform" {
  name       = "${var.name}-cache"
  subnet_ids = [for subnet in aws_subnet.data : subnet.id]
  tags       = local.tags
}

resource "aws_elasticache_replication_group" "platform" {
  replication_group_id       = "${var.name}-valkey"
  description                = "A2A production event streams"
  engine                     = "valkey"
  engine_version             = var.valkey_engine_version
  node_type                  = var.valkey_node_type
  num_cache_clusters         = 3
  automatic_failover_enabled = true
  multi_az_enabled           = true
  auth_token                 = var.valkey_auth_token
  auth_token_update_strategy = "ROTATE"
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.rds.arn
  subnet_group_name          = aws_elasticache_subnet_group.platform.name
  security_group_ids         = [aws_security_group.valkey.id]
  snapshot_retention_limit   = 7
  snapshot_window            = "02:00-02:30"
  maintenance_window         = "sun:03:00-sun:04:00"
  apply_immediately          = false
  lifecycle { prevent_destroy = true }
  tags = merge(local.tags, { Name = "${var.name}-valkey" })
}

resource "aws_efs_file_system" "lake" {
  creation_token   = "${var.name}-data-lake"
  encrypted        = true
  kms_key_id       = aws_kms_key.efs.arn
  throughput_mode  = "elastic"
  performance_mode = "generalPurpose"
  lifecycle_policy { transition_to_ia = "AFTER_30_DAYS" }
  lifecycle { prevent_destroy = true }
  tags = merge(local.tags, { Name = "${var.name}-data-lake" })
}

resource "aws_security_group" "efs" {
  name        = "${var.name}-efs"
  description = "NFS is reachable only by EKS worker nodes"
  vpc_id      = aws_vpc.platform.id
  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_eks_cluster.platform.vpc_config[0].cluster_security_group_id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}

resource "aws_efs_mount_target" "lake" {
  for_each        = aws_subnet.app
  file_system_id  = aws_efs_file_system.lake.id
  subnet_id       = each.value.id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_backup_vault" "lake" {
  name        = "${var.name}-lake"
  kms_key_arn = aws_kms_key.efs.arn
  tags        = local.tags
}

resource "aws_iam_role" "backup_service" {
  name = "${var.name}-aws-backup"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "backup.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "backup_service" {
  for_each = toset([
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores",
  ])
  role       = aws_iam_role.backup_service.name
  policy_arn = each.value
}

resource "aws_backup_plan" "lake" {
  name = "${var.name}-lake-daily"
  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.lake.name
    schedule          = "cron(0 5 * * ? *)"
    lifecycle { delete_after = var.backup_retention_days }
  }
  tags = local.tags
}

resource "aws_backup_selection" "lake" {
  name         = "${var.name}-lake-selection"
  plan_id      = aws_backup_plan.lake.id
  iam_role_arn = aws_iam_role.backup_service.arn
  resources    = [aws_efs_file_system.lake.arn]
  depends_on   = [aws_iam_role_policy_attachment.backup_service]
}

resource "aws_s3_bucket" "archive" {
  bucket              = var.audit_bucket_name
  force_destroy       = false
  object_lock_enabled = true
  lifecycle { prevent_destroy = true }
  tags = merge(local.tags, { Name = var.audit_bucket_name })
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_object_lock_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.audit_retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.archive]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.s3.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket                  = aws_s3_bucket.archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "archive" {
  bucket = aws_s3_bucket.archive.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.archive.arn, "${aws_s3_bucket.archive.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.archive]
}

resource "aws_s3_bucket" "backup" {
  bucket              = var.backup_bucket_name
  force_destroy       = false
  object_lock_enabled = true
  lifecycle { prevent_destroy = true }
  tags = merge(local.tags, { Name = var.backup_bucket_name })
}

resource "aws_s3_bucket_versioning" "backup" {
  bucket = aws_s3_bucket.backup.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_object_lock_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.backup_retention_days + 7
    }
  }
  depends_on = [aws_s3_bucket_versioning.backup]
}

resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule {
    id     = "expire-backups-after-retention"
    status = "Enabled"
    filter {}
    expiration { days = var.backup_retention_days + 14 }
    noncurrent_version_expiration { noncurrent_days = var.backup_retention_days + 14 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.s3.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "backup" {
  bucket                  = aws_s3_bucket.backup.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "backup" {
  bucket = aws_s3_bucket.backup.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.backup.arn, "${aws_s3_bucket.backup.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.backup]
}

resource "aws_iam_role" "audit_archiver" {
  name = "${var.name}-audit-archiver"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = [
          "system:serviceaccount:a2a:a2a-audit-archiver",
          "system:serviceaccount:a2a:a2a-production-preflight",
        ]
      } }
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "audit_archiver" {
  name = "${var.name}-write-audit-archive"
  role = aws_iam_role.audit_archiver.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketObjectLockConfiguration", "s3:ListBucket", "s3:ListBucketVersions"]
        Resource = [aws_s3_bucket.archive.arn]
        Condition = {
          StringLike = { "s3:prefix" = ["a2a/production/audit/*"] }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:PutObjectRetention"]
        Resource = ["${aws_s3_bucket.archive.arn}/a2a/production/audit/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = [aws_kms_key.s3.arn]
      }
    ]
  })
}

resource "aws_iam_role" "backup_writer" {
  name = "${var.name}-backup-writer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = "system:serviceaccount:a2a:a2a-postgres-backup"
      } }
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "backup_writer" {
  name = "${var.name}-write-backups"
  role = aws_iam_role.backup_writer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketObjectLockConfiguration", "s3:ListBucket", "s3:ListBucketMultipartUploads"]
        Resource = [aws_s3_bucket.backup.arn]
        Condition = {
          StringLike = { "s3:prefix" = ["a2a/production/*"] }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "s3:AbortMultipartUpload",
          "s3:GetObject",
          "s3:ListMultipartUploadParts",
          "s3:PutObject",
        ]
        Resource = ["${aws_s3_bucket.backup.arn}/a2a/production/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = [aws_kms_key.s3.arn]
      }
    ]
  })
}
