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
}
