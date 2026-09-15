# ------------------------------------------------------------------------------
# THIS APP'S PREVIEW STACK (one Terraform workspace per PR)
#
# The app repo owns its preview shape: which workloads to enable and where the
# PR's image goes. The heavy lifting comes from the platform's published
# modules; the shared cluster/VPC already exist. CI drives this stack through
# the platform repo's reusable preview-up/preview-down workflows with
# working_directory: infra/preview.
#
# The preview's DATA comes from one of two providers (var.fork_provider):
#   tofu    (default) this stack stamps isolated, empty copies: a copy-on-write
#           Neon branch, an ephemeral bucket, an ephemeral Iceberg namespace.
#   tether  CI's fork-data job forks the PRODUCTION stores with tether and hands
#           this stack the results (external.auto.tfvars.json: database_url,
#           dagster_db_*, data_refs); this stack grants the pods access to the
#           prod stores (data-access) and creates no data resources itself.
# Both end in the same pod contract -- DATABASE_URL + DATA_REFS -- so the app
# never learns which. See data.tf and README "Ephemeral data".
#
# Module sources point at the upstream platform repo; a fork of the platform
# replaces the org, and everyone pins ?ref= to a release tag. (A module source
# is a literal: no variable can stand in for the org.)
# ------------------------------------------------------------------------------

locals {
  name_prefix = "${var.preview_name}-"

  preview_tags = merge(var.tags, {
    Environment = "preview"
    Preview     = var.preview_name
    Terraform   = "true"
  })

  tofu_forks   = var.fork_provider == "tofu"
  tether_forks = var.fork_provider == "tether"

  neon_enabled    = local.tofu_forks && length(var.neon_branch_sources) > 0
  iceberg_enabled = local.tofu_forks && var.iceberg_table_bucket_arn != ""

  # One image reference per deployable, derived from the same single source of
  # truth CI's build matrix uses. Adding app2 to deployables.json makes
  # local.image["app2"] available here with no new variables.
  deployables = jsondecode(file("${path.module}/../../deployables.json"))
  image       = { for d in local.deployables : d => "${var.image_base}:${d}-${var.image_stamp}" }
}

# ------------------------------------------------------------------------------
# tofu mode: disposable per-preview data resources, destroyed with the preview.
# ------------------------------------------------------------------------------

module "storage" {
  count  = local.tofu_forks ? 1 : 0
  source = "github.com/Rosebud-Biosciences/terraform-aws-lab-platform//modules/preview-storage?ref=main"

  name_prefix = var.preview_name
  tags        = local.preview_tags
}

module "neon" {
  count  = local.neon_enabled ? 1 : 0
  source = "github.com/Rosebud-Biosciences/terraform-aws-lab-platform//modules/neon-branches?ref=main"

  providers = { neon = neon }

  name_prefix    = var.preview_name
  branch_sources = var.neon_branch_sources
}

# The lakehouse analogue of the Neon branch: an empty namespace of the preview's
# own in the shared S3 Tables bucket, IAM-confined. Tables must be dropped
# before destroy (see the module README).
module "iceberg" {
  count  = local.iceberg_enabled ? 1 : 0
  source = "github.com/Rosebud-Biosciences/terraform-aws-lab-platform//modules/iceberg-branches?ref=main"

  name_prefix      = var.preview_name
  table_bucket_arn = var.iceberg_table_bucket_arn
  read_namespaces  = var.iceberg_read_namespaces
  tags             = local.preview_tags
}

# ------------------------------------------------------------------------------
# tether mode: the forks already exist inside the production stores; the pods
# need to reach them. Get/put/list (never delete) on the store prefixes and
# read/commit on the Iceberg tables -- the trust boundary is documented where
# it is granted (the module README) and in this repo's README.
# ------------------------------------------------------------------------------

module "data_access" {
  count  = local.tether_forks ? 1 : 0
  source = "github.com/Rosebud-Biosciences/terraform-aws-lab-platform//modules/data-access?ref=main"

  name       = "${var.preview_name}-data-access"
  bucket_arn = var.data_bucket_arn
  prefixes   = var.data_prefixes
  table_arns = var.iceberg_table_arns
  tags       = local.preview_tags
}

# ------------------------------------------------------------------------------
# The resolved contract, whichever provider filled it (see data.tf for
# DATA_REFS). one() is null when a module has count 0, so no branch indexes an
# empty list.
# ------------------------------------------------------------------------------

locals {
  database_url = local.tether_forks ? var.database_url : (
    local.neon_enabled ? one(module.neon[*].postgres_urls["app"]) : ""
  )

  neon_dagster = local.neon_enabled ? one(module.neon[*].connections["dagster"]) : null
  dagster_db = {
    host     = local.tether_forks ? var.dagster_db_host : try(local.neon_dagster.host, "")
    dbname   = local.tether_forks ? var.dagster_db_name : try(local.neon_dagster.dbname, "")
    user     = local.tether_forks ? var.dagster_db_user : try(local.neon_dagster.user, "")
    password = local.tether_forks ? var.dagster_db_password : try(local.neon_dagster.password, "")
  }

  # IAM the data-writing service accounts get: the ephemeral copies in tofu
  # mode, the production stores (no delete) in tether mode.
  data_policies = local.tofu_forks ? merge(
    { processeddata = one(module.storage[*].putget_policy_arn) },
    local.iceberg_enabled ? { iceberg_rw = one(module.iceberg[*].readwrite_policy_arn) } : {},
    local.iceberg_enabled && length(var.iceberg_read_namespaces) > 0
    ? { iceberg_read = one(module.iceberg[*].read_policy_arn) } : {},
  ) : { data = one(module.data_access[*].policy_arn) }

  webapp_policies = local.tofu_forks ? {
    processeddata = one(module.storage[*].get_policy_arn)
  } : { data = one(module.data_access[*].policy_arn) }
}

module "workloads" {
  source = "github.com/Rosebud-Biosciences/terraform-aws-lab-platform//modules/workloads?ref=main"

  providers = {
    aws        = aws
    kubernetes = kubernetes
    helm       = helm
    kubectl    = kubectl
  }

  environment = "preview"
  region      = var.region

  # Target the existing shared cluster.
  cluster_name                 = var.cluster_name
  oidc_provider_arn            = var.oidc_provider_arn
  vpc_name                     = var.vpc_name
  karpenter_node_iam_role_name = var.karpenter_node_iam_role_name

  # Everything is prefixed so it never collides with prod or other previews.
  name_prefix = local.name_prefix

  # Private Ingresses on the shared Tailscale operator, hostnames prefixed.
  enable_private_ingress          = var.private_ingress_dns_suffix != ""
  private_ingress_class_name      = "tailscale"
  private_ingress_hostname_prefix = local.name_prefix
  private_ingress_dns_suffix      = var.private_ingress_dns_suffix

  tags = local.preview_tags

  # --- The app under test -----------------------------------------------------
  # webapp: packages/app served by the PR's image (uvicorn on :8080).
  enable_webapp            = true
  webapp_image             = local.image["app"]
  webapp_container_port    = 8080
  webapp_health_check_path = "/healthz"
  webapp_env = merge(
    { APP_ENV = "preview-${var.preview_name}" },
    local.data_env,
  )

  # Dagster's code location is the workflows image (packages/workflows via
  # /opt/dagster/app/repo.py). enable_ray creates the Ray namespace/RBAC for
  # ray_fanout_job (set RAY_ADDRESS to reach a cluster); enable_ray_cluster
  # stays off so previews don't pay for a standing Ray cluster. The user-code
  # deployment and every run it launches get DATABASE_URL (from database_url)
  # plus the same DATA_REFS / ICEBERG_CATALOG the webapp does.
  enable_dagster          = true
  enable_ray              = true
  enable_ray_cluster      = false
  dagster_user_code_image = local.image["workflows"]
  dagster_user_code_env   = local.data_env

  # The preview's database, whichever provider forked it.
  database_url = local.database_url

  dagster_db_host     = local.dagster_db.host
  dagster_db_name     = local.dagster_db.dbname
  dagster_db_user     = local.dagster_db.user
  dagster_db_password = local.dagster_db.password

  # Object-store access follows the provider (see local.data_policies).
  webapp_bucket_policies      = local.webapp_policies
  ray_storage_bucket_policies = local.data_policies
  dagster_bucket_policies     = local.data_policies

  # One small preview-scoped NodePool (scales to zero when idle).
  karpenter_node_pools = {
    default = {
      instance_families = ["m7i"]
      instance_sizes    = ["large", "xlarge"]
      capacity_types    = ["spot", "on-demand"]
      limits            = { cpu = "8", memory = "32Gi" }
    }
  }
}
