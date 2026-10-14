terraform {
  required_version = ">= 1.10.0"
  backend "s3" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0, < 5.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Application = "a2a"
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}
