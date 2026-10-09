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
#           this stack the results (external.auto.tfvars.json: fork_dbs,
#           data_refs -- no passwords, which this stack reads from Neon);
#           this stack grants the pods access to the prod stores
#           (data-access) and creates no data resources itself.
# Both end in the same pod contract -- DATABASE_URL + DATA_REFS, plus each
# service's own database -- so the app never learns which. The services'
# STATE branches too: Dagster's run storage, MLflow's tracking store and
# Argo's archive are db/<svc> objects (dataset) / neon_branch_sources keys, so
# a preview shows prod's history and writes none back. See data.tf and README
# "Ephemeral data".
#
# Module sources point at the upstream platform repo; a fork of the platform
# replaces the org, and everyone pins ?ref= to a release tag. (A module source
# is a literal: no variable can stand in for the org.)
# ------------------------------------------------------------------------------

locals {
  # Every Kubernetes name the preview creates starts with "preview-": the
  # preview role is an admin only in preview-* namespaces, and admitted only
  # preview-* namespaces and NodePools (lab-platform modules/preview-access).
  # Hostnames keep the short form.
  name_prefix = "preview-${var.preview_name}-"

  preview_tags = merge(var.tags, {
    Environment = "preview"
    Preview     = var.preview_name
    Terraform   = "true"
  })

  tofu_forks   = var.fork_provider == "tofu"
  tether_forks = var.fork_provider == "tether"

  neon_enabled    = local.tofu_forks && length(var.neon_branch_sources) > 0
  iceberg_enabled = local.tofu_forks && var.iceberg_table_bucket_arn != ""

  # "app": the webapp alone; Dagster/MLflow/Argo are prod's (variables.tf
  # preview_profile).
  pipelines = var.preview_profile == "full"

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
  source = "github.com/Rosebud-Biosciences/lab-platform//aws/preview-storage?ref=v0.3.0"

  name_prefix = var.preview_name
  iam_path    = var.preview_iam_path
  tags        = local.preview_tags
}

module "neon" {
  count  = local.neon_enabled ? 1 : 0
  source = "github.com/Rosebud-Biosciences/lab-platform//modules/neon-branches?ref=v0.3.0"

  providers = { neon = neon }

  name_prefix    = var.preview_name
  branch_sources = var.neon_branch_sources
}

# The lakehouse analogue of the Neon branch: an empty namespace of the preview's
# own in the shared S3 Tables bucket, IAM-confined. Destroy drops the tables
# the migrations created, then the namespace.
module "iceberg" {
  count  = local.iceberg_enabled ? 1 : 0
  source = "github.com/Rosebud-Biosciences/lab-platform//aws/iceberg-branches?ref=v0.3.0"

  name_prefix      = var.preview_name
  table_bucket_arn = var.iceberg_table_bucket_arn
  read_namespaces  = var.iceberg_read_namespaces
  iam_path         = var.preview_iam_path
  tags             = local.preview_tags
}

# ------------------------------------------------------------------------------
# tether mode: the forks already exist inside the production stores; the pods
# need to reach them. Get/put/list (never delete) on the store prefixes and
# read/commit on the Iceberg tables -- the trust boundary is documented where
# it is granted (the module README) and in this repo's README.
# ------------------------------------------------------------------------------

# An empty key is right for an SSE-S3 bucket, so it cannot be an error; but
# aws/s3-bucket encrypts with its own KMS key, and without that key the pods
# and MLflow can neither read nor write a single object.
check "tether_data_bucket_key" {
  assert {
    condition     = !local.tether_forks || var.data_bucket_arn == "" || var.data_bucket_kms_key_arn != ""
    error_message = "tether mode without data_bucket_kms_key_arn: if ${var.data_bucket_arn} is SSE-KMS encrypted (aws/s3-bucket's default), previews cannot read or write it. Set its key in shared-platform.auto.tfvars; ignore this for an SSE-S3 bucket."
  }
}

module "data_access" {
  count  = local.tether_forks ? 1 : 0
  source = "github.com/Rosebud-Biosciences/lab-platform//aws/data-access?ref=v0.3.0"

  name        = "${var.preview_name}-data-access"
  bucket_arn  = var.data_bucket_arn
  kms_key_arn = var.data_bucket_kms_key_arn
  prefixes    = var.data_prefixes
  table_arns  = var.iceberg_table_arns
  iam_path    = var.preview_iam_path
  tags        = local.preview_tags

  # The fork is the PR's to write; prod's trunk and the pins on it are not.
  protect_trunk = true
}

# ------------------------------------------------------------------------------
# The resolved contract, whichever provider filled it (see data.tf for
# DATA_REFS). one() is null when a module has count 0, so no branch indexes an
# empty list.
# ------------------------------------------------------------------------------

# tether mode: the fork's role passwords, read here rather than carried in from
# CI (var.fork_dbs says why).
data "neon_branch_role_password" "fork" {
  for_each = local.tether_forks ? var.fork_dbs : {}

  project_id = each.value.project_id
  branch_id  = each.value.branch_id
  role_name  = each.value.user
}

locals {
  # Every database's connection, keyed like the Neon module's (app, dagster,
  # ...), whichever provider forked it.
  db_connections = local.tether_forks ? {
    for k, d in var.fork_dbs : k => {
      host     = d.host
      dbname   = d.dbname
      user     = d.user
      password = data.neon_branch_role_password.fork[k].password
    }
  } : (local.neon_enabled ? one(module.neon[*].connections) : {})

  app_db = lookup(local.db_connections, "app", null)
  database_url = local.app_db == null ? "" : (
    "postgresql+psycopg://${local.app_db.user}:${urlencode(local.app_db.password)}@${local.app_db.host}/${local.app_db.dbname}?sslmode=require"
  )

  # The service databases: every connection but the app's.
  service_dbs = {
    for k, c in local.db_connections :
    k => { host = c.host, dbname = c.dbname, user = c.user, password = c.password } if k != "app"
  }
  no_db      = { host = "", dbname = "", user = "", password = "" }
  dagster_db = lookup(local.service_dbs, "dagster", local.no_db)
  mlflow_db  = lookup(local.service_dbs, "mlflow", local.no_db)
  argo_db    = lookup(local.service_dbs, "argo", local.no_db)
  # Which services have a database is not a secret, and it drives resource
  # counts downstream (Argo's archive), where a sensitive value is refused.
  services_with_db = try(nonsensitive(keys(local.service_dbs)), keys(local.service_dbs))

  # MLflow's artifacts: write-once blobs per run, not a tether object (the
  # object-store backend has no fork). tofu mode: under the ephemeral bucket,
  # gone with it. tether mode: a per-preview prefix inside the prod data
  # bucket's tether/ area, which data-access already grants and tether-down.sh
  # deletes when the preview retires.
  data_bucket_name       = var.data_bucket_arn != "" ? element(split(":", var.data_bucket_arn), 5) : ""
  mlflow_artifact_bucket = local.tofu_forks ? one(module.storage[*].bucket_name) : local.data_bucket_name
  mlflow_artifact_prefix = local.tofu_forks ? "mlflow" : "tether/mlflow/${var.preview_name}"
  mlflow_artifact_root   = "s3://${local.mlflow_artifact_bucket}/${local.mlflow_artifact_prefix}"

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

# ------------------------------------------------------------------------------
# Backend adapters: the AWS backend's two axes, stamped with the preview's
# prefix. modules/workloads itself is cloud-agnostic; these hand it its
# contract inputs (identity, scheduling).
# ------------------------------------------------------------------------------

# Data axis: per-service IAM roles (IRSA on the shared EKS cluster) carrying
# whichever object-store access the fork provider calls for.
module "data" {
  source = "github.com/Rosebud-Biosciences/lab-platform//aws/data-adapter?ref=v0.3.0"

  cluster_name      = var.cluster_name
  name_prefix       = local.name_prefix
  oidc_provider_arn = var.oidc_provider_arn
  region            = var.region

  # The preview role may create roles only here, and only with the boundary.
  iam_path                 = var.preview_iam_path
  permissions_boundary_arn = var.preview_permissions_boundary_arn

  enable_webapp         = true
  enable_dagster        = local.pipelines
  enable_ray            = local.pipelines
  enable_argo_workflows = local.pipelines
  enable_mlflow         = local.pipelines

  webapp_policy_arns  = local.webapp_policies
  dagster_policy_arns = local.data_policies
  ray_policy_arns     = local.data_policies

  # MLflow's tracking server writes artifacts to the preview's prefix.
  mlflow_artifact_bucket      = local.mlflow_artifact_bucket
  mlflow_artifact_bucket_arn  = local.tofu_forks ? one(module.storage[*].bucket_arn) : var.data_bucket_arn
  mlflow_artifact_prefix      = local.mlflow_artifact_prefix
  mlflow_artifact_kms_key_arn = local.tofu_forks ? one(module.storage[*].kms_key_arn) : var.data_bucket_kms_key_arn

  tags = local.preview_tags
}

# Compute axis: two small preview-scoped NodePools (scale to zero when idle).
# The long-running services share on-demand nodes -- a spot reclaim of the Ray
# head ends the Ray cluster and its jobs, and one of Dagster's kills its
# in-flight runs -- while Ray workers, whose tasks Ray reschedules, run on spot
# behind a taint that keeps everything else off those nodes.
module "compute" {
  source = "github.com/Rosebud-Biosciences/lab-platform//aws/compute-adapter?ref=v0.3.0"

  providers = { aws = aws, helm = helm }

  cluster_name                 = var.cluster_name
  name_prefix                  = local.name_prefix
  environment                  = "preview"
  vpc_name                     = var.vpc_name
  karpenter_node_iam_role_name = var.karpenter_node_iam_role_name
  # The NodePool releases' records live in the preview's own namespace, not
  # Karpenter's, which the preview role cannot write.
  node_pools_namespace = module.workloads.webapp_namespace

  # No pipelines, no pools: an app-only preview rides the shared node group.
  # (Filtered for-expressions: the two pools differ in shape, which a
  # conditional cannot unify.)
  karpenter_node_pools = { for k, v in {
    services = {
      instance_families = ["m7i"]
      instance_sizes    = ["large", "xlarge"]
      capacity_types    = ["on-demand"]
      limits            = { cpu = "4", memory = "16Gi" }
    }
    workers = {
      instance_families = ["m7i"]
      instance_sizes    = ["large", "xlarge"]
      capacity_types    = ["spot", "on-demand"]
      limits            = { cpu = "4", memory = "16Gi" }
      taints            = [{ key = "lab-platform.io/interruptible", value = "true", effect = "NoSchedule" }]
    }
  } : k => v if local.pipelines }
  node_pool_roles = { for k, v in {
    services = ["dagster", "argo", "mlflow", "ray_head"]
    workers  = ["ray_worker"]
  } : k => v if local.pipelines }

  tags = local.preview_tags
}

module "workloads" {
  source = "github.com/Rosebud-Biosciences/lab-platform//modules/workloads?ref=v0.3.0"

  providers = {
    kubernetes = kubernetes
    helm       = helm
    kubectl    = kubectl
  }

  environment = "preview"

  # Everything is prefixed so it never collides with prod or other previews.
  name_prefix     = local.name_prefix
  namespace_admin = var.preview_namespace_admin

  # Contract inputs from the adapters.
  workload_identity = module.data.workload_identity
  scheduling        = module.compute.scheduling

  # Auth: the tailnet is the login (its Ingress proxy names the caller in
  # Tailscale-User-Login, which the app trusts as IDENTITY_HEADER). Switch to
  # { mode = "oidc", issuer_url = ..., dex_namespace = ... } once the platform
  # runs modules/dex, and the app runs its own login instead -- users,
  # sessions and memberships then live on this preview's database branch
  # (platform docs/auth.md).
  auth = { mode = "headers" }

  # Private Ingresses on the shared Tailscale operator, hostnames prefixed.
  enable_private_ingress          = var.private_ingress_dns_suffix != ""
  private_ingress_class_name      = "tailscale"
  private_ingress_hostname_prefix = "${var.preview_name}-"
  private_ingress_dns_suffix      = var.private_ingress_dns_suffix

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
  enable_dagster          = local.pipelines
  enable_ray              = local.pipelines
  enable_ray_cluster      = false
  dagster_user_code_image = local.image["workflows"]
  dagster_user_code_env   = local.data_env

  # MLflow and Argo Workflows (with its archive) are stamped alongside Dagster
  # in a full preview, each on its own branch of its own database, so the
  # preview shows prod's experiments and writes to neither. Prod's archived
  # workflows are on the Argo branch but unlisted: Argo keys them by namespace,
  # and the preview's server sees only its own. Argo's archive is optional: no
  # db/argo object, no archive.
  enable_mlflow        = local.pipelines
  mlflow_artifact_root = local.pipelines ? local.mlflow_artifact_root : ""
  mlflow_db_host       = local.mlflow_db.host
  mlflow_db_name       = local.mlflow_db.dbname
  mlflow_db_user       = local.mlflow_db.user
  mlflow_db_password   = local.mlflow_db.password

  enable_argo_workflows        = local.pipelines
  enable_argo_workflow_archive = local.pipelines && contains(local.services_with_db, "argo")
  argo_db_host                 = local.argo_db.host
  argo_db_name                 = local.argo_db.dbname
  argo_db_user                 = local.argo_db.user
  argo_db_password             = local.argo_db.password

  # preview_profile "app": no Dagster/MLflow/Argo of its own; the webapp's
  # DAGSTER_WEBSERVER_URL / MLFLOW_TRACKING_URI / ARGO_SERVER_URL point at
  # prod's. Same variable names as when stamped, so packages/app never knows
  # which -- but runs it triggers now execute prod's code on prod's data.
  dagster_webserver_url = local.pipelines ? "" : var.shared_service_urls.dagster_webserver_url
  mlflow_tracking_uri   = local.pipelines ? "" : var.shared_service_urls.mlflow_tracking_uri
  argo_server_url       = local.pipelines ? "" : var.shared_service_urls.argo_server_url

  # The preview's database, whichever provider forked it.
  database_url = local.database_url

  dagster_db_host     = local.dagster_db.host
  dagster_db_name     = local.dagster_db.dbname
  dagster_db_user     = local.dagster_db.user
  dagster_db_password = local.dagster_db.password
}
