# Identity of the shared platform every preview lands on. Committed (none of it
# is secret — the Neon API key lives in GitHub secrets) so CI can apply without
# a copy step; auto-loaded by tofu. Replace the placeholders when adopting.
# preview_name and app_image are passed per-PR by CI as -var flags.

region                       = "us-west-2"
cluster_name                 = "eks-prod"
oidc_provider_arn            = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/XXXXXXXX"
vpc_name                     = "vpc-prod"
karpenter_node_iam_role_name = "eks-prod-karpenter-node"
private_ingress_dns_suffix   = "tailXXXX.ts.net"

# In-cluster URLs of prod's services, for app-only previews (the
# preview:app-only label -> preview_profile = "app"): the prod workloads
# module's in_cluster_urls output. Only dagster_webserver_url is required;
# leave the others empty if prod runs no MLflow / Argo.
shared_service_urls = {
  dagster_webserver_url = "http://dagster-dagster-webserver.dagster.svc.cluster.local:80"
  mlflow_tracking_uri   = ""
  argo_server_url       = ""
}

# Where the data objects in packages/dataset/.tether/objects/ live (README
# "Ephemeral data").
# tofu mode needs only iceberg_table_bucket_arn (for the ephemeral namespace);
# tether mode also grants the pods access to these prod stores. Leave
# iceberg_table_bucket_arn empty to skip Iceberg entirely.
data_bucket_arn = "arn:aws:s3:::your-data-bucket"
# tether mode: the bucket's customer-managed KMS key (aws/s3-bucket makes one;
# `aws s3api get-bucket-encryption` names it). Without it the pods and MLflow
# can neither read nor write the bucket. Empty only for an SSE-S3 bucket.
data_bucket_kms_key_arn  = ""
iceberg_table_bucket_arn = "arn:aws:s3tables:us-west-2:123456789012:bucket/your-table-bucket"
iceberg_read_namespaces  = ["lake"]
# tether mode: the prod tables whose branches previews write (one ARN per
# iceberg object in the dataset; `aws s3tables get-table` prints it).
iceberg_table_arns = []

# tofu mode only; tether forks the Neon project itself. One key per database
# a full preview stamps: the app's, and each service's own state (Dagster run
# storage, MLflow tracking store, Argo workflow archive) -- the same keys as
# the dataset's db/<svc> objects. Sources sharing a project and parent branch
# share one Neon branch (platform modules/neon-branches).
neon_branch_sources = {
  app = {
    project_id       = "prod-app-project"
    parent_branch_id = "br-prod-main-0000"
    role_name        = "app"
    db_name          = "app"
  }
  dagster = {
    project_id       = "prod-dagster-project"
    parent_branch_id = "br-prod-main-1111"
    role_name        = "dagster"
    db_name          = "dagster"
  }
  mlflow = {
    project_id       = "prod-dagster-project"
    parent_branch_id = "br-prod-main-1111"
    role_name        = "mlflow"
    db_name          = "mlflow"
  }
  argo = {
    project_id       = "prod-dagster-project"
    parent_branch_id = "br-prod-main-1111"
    role_name        = "argo"
    db_name          = "argo"
  }
}
