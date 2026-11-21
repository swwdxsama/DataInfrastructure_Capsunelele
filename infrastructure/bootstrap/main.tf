# Bootstrap (o singura data, cu un cont AWS cu drepturi de admin, de ex. din CloudShell):
#   - bucket S3 + tabel DynamoDB pentru starea Terraform a stack-ului principal (infrastructure/)
#   - provider OIDC pentru GitHub Actions + rolul IAM pe care il foloseste deploy-ul (fara chei statice)
#
#   cd infrastructure/bootstrap && terraform init && terraform apply
#
# Starea acestui bootstrap ramane locala (resursele lui se schimba foarte rar).

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "github_repo" {
  description = "owner/repo care are voie sa faca deploy"
  type        = string
  default     = "swwdxsama/DataInfrastructure_Capsunelele"
}

# GitHub poate trimite sub-ul cu ID-urile numerice (repo:owner@ID/repo@ID:...); acceptam ambele formate.
# ID-urile sunt publice: https://api.github.com/repos/<owner>/<repo> -> owner.id si id
variable "github_owner_id" {
  type    = string
  default = "189281215"
}

variable "github_repo_id" {
  type    = string
  default = "1465638439"
}

variable "deploy_branch" {
  description = "Singurul branch de pe care GitHub Actions poate prelua rolul"
  type        = string
  default     = "main"
}

variable "create_oidc_provider" {
  description = "false daca providerul token.actions.githubusercontent.com exista deja in cont"
  type        = bool
  default     = true
}

data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "capsunelele-tfstate-${local.account_id}"
  oidc_url     = "token.actions.githubusercontent.com"
  repo_owner   = split("/", var.github_repo)[0]
  repo_name    = split("/", var.github_repo)[1]
  allowed_subjects = [
    "repo:${var.github_repo}:ref:refs/heads/${var.deploy_branch}",
    "repo:${local.repo_owner}@${var.github_owner_id}/${local.repo_name}@${var.github_repo_id}:ref:refs/heads/${var.deploy_branch}",
  ]
  oidc_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:aws:iam::${local.account_id}:oidc-provider/${local.oidc_url}"
}

# ---------------------------------------------------------------- starea Terraform

resource "aws_s3_bucket" "tf_state" {
  bucket = local.state_bucket

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket                  = aws_s3_bucket.tf_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tf_lock" {
  name         = "capsunelele-tf-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}

# ---------------------------------------------------------------- GitHub OIDC

resource "aws_iam_openid_connect_provider" "github" {
  count           = var.create_oidc_provider ? 1 : 0
  url             = "https://${local.oidc_url}"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_deploy" {
  name = "capsunelele-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.oidc_arn }
      Condition = {
        StringEquals = {
          "${local.oidc_url}:aud" = "sts.amazonaws.com"
          "${local.oidc_url}:sub" = local.allowed_subjects # lista = oricare dintre valori
        }
      }
    }]
  })
}

# Tot ce gestioneaza stack-ul principal (DynamoDB, Lambda, API Gateway, SNS, S3, CloudFront, CloudWatch)...
resource "aws_iam_role_policy_attachment" "power_user" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

# ...plus IAM doar pentru rolurile proiectului (quake_*), nu pentru restul contului.
resource "aws_iam_role_policy" "project_iam" {
  name = "capsunelele-project-iam"
  role = aws_iam_role.github_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["iam:*"]
      Resource = "arn:aws:iam::${local.account_id}:role/quake_*"
    }]
  })
}

# ---------------------------------------------------------------- outputs -> GitHub

output "state_bucket" {
  description = "GitHub -> Settings -> Variables: TF_STATE_BUCKET"
  value       = aws_s3_bucket.tf_state.bucket
}

output "deploy_role_arn" {
  description = "GitHub -> Settings -> Secrets: AWS_ROLE_ARN"
  value       = aws_iam_role.github_deploy.arn
}
