variable "region" {
  description = "AWS region of the shared cluster"
  type        = string
  default     = "us-west-2"
}

variable "preview_name" {
  description = "Unique per-PR name (e.g. pr123); CI derives it from the PR number"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,19}$", var.preview_name))
    error_message = "preview_name must be <= 20 lowercase alphanumeric/dash characters starting with an alphanumeric."
  }
}

# ------------------------------------------------------------------------------
# SHARED CLUSTER (created once by your platform stack)
# ------------------------------------------------------------------------------

variable "cluster_name" {
  description = "Name of the existing shared EKS cluster"
  type        = string
}

variable "oidc_provider_arn" {
  description = "IRSA OIDC provider ARN of the shared cluster"
  type        = string
}

variable "vpc_name" {
  description = "VPC name used by Karpenter NodePools for subnet/SG discovery"
  type        = string
}

variable "karpenter_node_iam_role_name" {
  description = "Name of the shared cluster's Karpenter node IAM role"
  type        = string
}

variable "private_ingress_dns_suffix" {
  description = "MagicDNS suffix of the tailnet (e.g. tailXXXX.ts.net). Empty skips private ingress."
  type        = string
  default     = ""
}

# ------------------------------------------------------------------------------
# IMAGES UNDER TEST
#
# CI builds one image per entry in deployables.json, tagged
# <image_base>:<name>-<image_stamp>. Passing base + stamp (instead of one
# variable per image) keeps this stack unchanged as deployables are added:
# main.tf derives every reference via local.image[<name>].
# ------------------------------------------------------------------------------

variable "image_base" {
  description = "Registry/repository holding the PR's images (e.g. <account>.dkr.ecr.<region>.amazonaws.com/template-app). Defaulted so destroy/sweep need not pass it."
  type        = string
  default     = "unset"
}

variable "image_stamp" {
  description = "Per-PR tag suffix (e.g. pr123-<sha>); full tags are <name>-<stamp>. Defaulted so destroy/sweep need not pass it."
  type        = string
  default     = "unset"
}

# ------------------------------------------------------------------------------
# NEON (copy-on-write DB branches)
# ------------------------------------------------------------------------------

variable "neon_api_key" {
  description = "Neon API key used to cut the preview's copy-on-write branches"
  type        = string
  default     = ""
  sensitive   = true
}

variable "neon_branch_sources" {
  description = <<-EOT
    Parent Neon branches to clone for this preview. Key "app" feeds the webapp's
    DATABASE_URL and the alembic migration step; key "dagster" feeds Dagster's
    run storage. Set once in shared-platform.auto.tfvars, or read from your
    platform stack's remote state.
  EOT
  type = map(object({
    project_id       = string
    parent_branch_id = string
    role_name        = string
    db_name          = string
  }))
  default = {}
}

variable "tags" {
  description = "Extra tags (merged with the preview identity tags)"
  type        = map(string)
  default     = {}
}
