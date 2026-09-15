# ------------------------------------------------------------------------------
# DATA_REFS: where each data object lives for this preview.
#
# The pods' contract (packages/dataset, dataset.refs): a JSON object
# key -> address, one entry per object registered in the dataset's manifests
# (packages/dataset/.tether/objects/), in the address forms `tether open
# --json` prints.
#
#   tofu mode    built HERE: every store is a fresh copy under the preview's
#                ephemeral bucket, and the Iceberg table sits in the preview's
#                own namespace. The keys must match packages/dataset (its
#                tests/test_manifests.py fails when they drift).
#   tether mode  built by CI's fork-data job from `tether open`, and passed in
#                as var.data_refs -- the addresses point at branches inside the
#                production stores.
#
# ICEBERG_CATALOG is the pyiceberg catalog both modes use on AWS: S3 Tables'
# Iceberg REST endpoint, SigV4-signed with the pod's IRSA credentials.
# ------------------------------------------------------------------------------

locals {
  bucket_uri        = local.tofu_forks ? one(module.storage[*].bucket_uri) : ""
  iceberg_namespace = local.iceberg_enabled ? one(module.iceberg[*].namespace) : ""

  tofu_data_refs = {
    "zarr/greetings"       = "${local.bucket_uri}tether/greetings.icechunk#main"
    "lake/greetings_daily" = local.iceberg_enabled ? "${local.iceberg_namespace}.greetings_daily#main" : ""
    "vec/greetings"        = "${local.bucket_uri}tether/greetings.lance#main"
    "delta/greetings_log"  = "${local.bucket_uri}tether/greetings_log.delta"
    "raw/uploads"          = "${local.bucket_uri}raw/uploads/"
  }

  data_refs_json = local.tether_forks ? var.data_refs : jsonencode(
    { for k, v in local.tofu_data_refs : k => v if v != "" }
  )

  iceberg_catalog_json = var.iceberg_table_bucket_arn == "" ? "" : jsonencode({
    type                  = "rest"
    uri                   = "https://s3tables.${var.region}.amazonaws.com/iceberg"
    warehouse             = var.iceberg_table_bucket_arn
    "rest.sigv4-enabled"  = "true"
    "rest.signing-name"   = "s3tables"
    "rest.signing-region" = var.region
  })

  # Handed to both the webapp and Dagster's user code (main.tf).
  data_env = merge(
    { DATA_REFS = local.data_refs_json },
    local.iceberg_catalog_json != "" ? { ICEBERG_CATALOG = local.iceberg_catalog_json } : {},
  )
}
