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
# EPHEMERAL DATA: which provider forks it (see main.tf header, README)
# ------------------------------------------------------------------------------

variable "preview_profile" {
  description = <<-EOT
    What this preview stamps (platform docs/preview-environments.md, "Two
    preview profiles"):
      full  webapp + Dagster (the PR's workflows image) + the Ray namespace,
            isolated on the preview's own database branch. Tests app AND
            pipeline changes.
      app   only the webapp, still on its own database branch; Dagster is
            PROD's, reached through shared_service_urls. Up in ~2 minutes,
            but runs the app triggers execute prod's code location on prod's
            data -- for frontend/API-only changes, never for a pipeline or
            schema change. CI sets it from the preview:app-only PR label.
  EOT
  type        = string
  default     = "full"

  validation {
    condition     = contains(["full", "app"], var.preview_profile)
    error_message = "preview_profile must be \"full\" or \"app\"."
  }
}

variable "shared_service_urls" {
  description = "In-cluster URLs of the shared (prod) services an app-only preview points at: the prod workloads module's in_cluster_urls output (shared-platform.auto.tfvars). Required with preview_profile = \"app\"."
  type = object({
    dagster_webserver_url = optional(string, "")
    mlflow_tracking_uri   = optional(string, "")
  })
  default = {}

  validation {
    condition     = var.preview_profile != "app" || var.shared_service_urls.dagster_webserver_url != ""
    error_message = "preview_profile = \"app\" needs shared_service_urls.dagster_webserver_url (prod's in_cluster_urls output, see shared-platform.auto.tfvars)."
  }
}

variable "fork_provider" {
  description = <<-EOT
    Who provides the preview's data. "tofu": this stack stamps isolated, empty
    copies (Neon branch, ephemeral bucket, Iceberg namespace) and builds
    DATA_REFS from them. "tether": CI forks the production stores with tether
    first and passes database_url / dagster_db_* / data_refs in; this stack only
    grants the pods access to those stores. CI sets it from the FORK_PROVIDER
    repository variable.
  EOT
  type        = string
  default     = "tofu"

  validation {
    condition     = contains(["tofu", "tether"], var.fork_provider)
    error_message = "fork_provider must be \"tofu\" or \"tether\"."
  }
}

# --- tether mode inputs (external.auto.tfvars.json, written by CI's fork-data
# job through the reusable workflow's extra_tfvars_json secret). Defaulted so a
# destroy or sweep needs none of them.

variable "database_url" {
  description = "tether mode: SQLAlchemy URL of the app database on the preview's fork (tether open db/app --with-password)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "dagster_db_host" {
  description = "tether mode: host of Dagster's metadata database on the preview's fork"
  type        = string
  default     = ""
}

variable "dagster_db_name" {
  description = "tether mode: Dagster's metadata database name"
  type        = string
  default     = ""
}

variable "dagster_db_user" {
  description = "tether mode: Dagster's metadata database role"
  type        = string
  default     = ""
}

variable "dagster_db_password" {
  description = "tether mode: Dagster's metadata database password"
  type        = string
  default     = ""
  sensitive   = true
}

variable "data_refs" {
  description = "tether mode: the DATA_REFS JSON (key -> address) CI built from `tether open --json`, pointing at the preview's fork branches"
  type        = string
  default     = ""
}

variable "data_bucket_arn" {
  description = "tether mode: ARN of the production data bucket holding the Icechunk / Lance / Delta stores and the raw prefix (the bucket in the dataset's manifest locators)"
  type        = string
  default     = ""
}

variable "data_prefixes" {
  description = "tether mode: key prefixes in data_bucket_arn the preview's pods may read and write (no delete) -- the store roots from the dataset's manifests"
  type        = list(string)
  default     = ["tether/", "raw/uploads/"]
}

variable "iceberg_table_arns" {
  description = "tether mode: S3 Tables table ARNs the preview's pods may read and commit to (the prod tables whose branches it writes; IAM cannot narrow this to a branch)"
  type        = list(string)
  default     = []
}

# --- both modes

variable "iceberg_table_bucket_arn" {
  description = "S3 Tables bucket the Iceberg objects live in. tofu mode: an ephemeral namespace is created in it; both modes: pods receive an ICEBERG_CATALOG for its REST endpoint. Empty skips Iceberg."
  type        = string
  default     = ""
}

variable "iceberg_read_namespaces" {
  description = "tofu mode: prod namespaces in iceberg_table_bucket_arn the preview may read"
  type        = list(string)
  default     = []
}

# ------------------------------------------------------------------------------
# NEON (copy-on-write DB branches, tofu mode)
# ------------------------------------------------------------------------------

variable "neon_api_key" {
  description = "Neon API key used to cut the preview's copy-on-write branches"
  type        = string
  default     = ""
  sensitive   = true
}

variable "neon_branch_sources" {
  description = <<-EOT
    Parent Neon branches to clone for this preview (tofu mode; ignored in tether
    mode, where tether forks the project instead). Key "app" feeds the webapp's
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
