output "preview_name" {
  description = "This preview's stamp"
  value       = var.preview_name
}

output "webapp_private_url" {
  description = "Private (tailnet) URL for the preview webapp"
  value       = module.workloads.webapp_private_url
}

output "dagster_private_url" {
  description = "Private (tailnet) URL for the preview Dagster UI"
  value       = module.workloads.dagster_private_url
}

output "ephemeral_bucket" {
  description = "Ephemeral processed-data bucket for this preview"
  value       = module.storage.bucket_name
}

# Consumed by CI's migrate step: `tofu output -raw app_database_url` feeds
# `alembic upgrade head` against this preview's copy-on-write branch.
output "app_database_url" {
  description = "SQLAlchemy URL of the preview's branched app database"
  value       = length(var.neon_branch_sources) > 0 ? module.neon.postgres_urls["app"] : ""
  sensitive   = true
}
