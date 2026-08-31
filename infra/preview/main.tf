# ------------------------------------------------------------------------------
# THIS APP'S PREVIEW STACK (one Terraform workspace per PR)
#
# The app repo owns its preview shape: which workloads to enable and where the
# PR's image goes. The heavy lifting comes from the platform's published
# modules; the shared cluster/VPC already exist. CI drives this stack through
# the platform repo's reusable preview-up/preview-down workflows with
# working_directory: infra/preview.
#
# Replace `your-org` (and ?ref=) with your fork/release of the platform repo.
# ------------------------------------------------------------------------------

locals {
  name_prefix = "${var.preview_name}-"

  preview_tags = merge(var.tags, {
    Environment = "preview"
    Preview     = var.preview_name
    Terraform   = "true"
  })

  neon_enabled = length(var.neon_branch_sources) > 0

  # One image reference per deployable, derived from the same single source of
  # truth CI's build matrix uses. Adding app2 to deployables.json makes
  # local.image["app2"] available here with no new variables.
  deployables = jsondecode(file("${path.module}/../../deployables.json"))
  image       = { for d in local.deployables : d => "${var.image_base}:${d}-${var.image_stamp}" }
}

# Disposable per-preview storage: an ephemeral processed-data bucket and
# copy-on-write Neon branches. Destroyed with the preview.
module "storage" {
  source = "github.com/your-org/terraform-aws-lab-platform//modules/preview-storage?ref=main"

  name_prefix = var.preview_name
  tags        = local.preview_tags
}

module "neon" {
  source = "github.com/your-org/terraform-aws-lab-platform//modules/neon-branches?ref=main"

  providers = { neon = neon }

  name_prefix    = var.preview_name
  branch_sources = var.neon_branch_sources
}

module "workloads" {
  source = "github.com/your-org/terraform-aws-lab-platform//modules/workloads?ref=main"

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
  webapp_env               = { APP_ENV = "preview-${var.preview_name}" }

  # Dagster's code location is the workflows image (packages/workflows via
  # /opt/dagster/app/repo.py). enable_ray creates the Ray namespace/RBAC for
  # ray_fanout_job (set RAY_ADDRESS to reach a cluster); enable_ray_cluster
  # stays off so previews don't pay for a standing Ray cluster.
  enable_dagster          = true
  enable_ray              = true
  enable_ray_cluster      = false
  dagster_user_code_image = local.image["workflows"]

  # DB connections come from the ephemeral Neon branches.
  database_url = local.neon_enabled ? module.neon.postgres_urls["app"] : ""

  dagster_db_host     = local.neon_enabled ? module.neon.connections["dagster"].host : ""
  dagster_db_name     = local.neon_enabled ? module.neon.connections["dagster"].dbname : ""
  dagster_db_user     = local.neon_enabled ? module.neon.connections["dagster"].user : ""
  dagster_db_password = local.neon_enabled ? module.neon.connections["dagster"].password : ""

  # Anything the app writes to object storage lands in the ephemeral bucket.
  webapp_bucket_policies      = { processeddata = module.storage.get_policy_arn }
  ray_storage_bucket_policies = { processeddata = module.storage.putget_policy_arn }

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
