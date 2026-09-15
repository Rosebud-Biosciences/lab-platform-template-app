output "preview_name" {
  description = "This preview's stamp"
  value       = var.preview_name
}

output "fork_provider" {
  description = "Who forked this preview's data: tofu (stamped copies) or tether (forks of prod)"
  value       = var.fork_provider
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
  description = "Ephemeral processed-data bucket for this preview (tofu mode; empty in tether mode, where data lives on branches in the prod stores)"
  value       = local.tofu_forks ? one(module.storage[*].bucket_name) : ""
}

output "data_refs" {
  description = "The DATA_REFS the pods received, whichever provider built it"
  value       = local.data_refs_json
}

# Consumed by CI's migrate step: `tofu output -raw app_database_url` feeds
# `alembic upgrade head` against this preview's database -- the tofu-mode Neon
# branch or the tether-mode fork, the same way.
output "app_database_url" {
  description = "SQLAlchemy URL of the preview's app database"
  value       = local.database_url
  sensitive   = true
}
