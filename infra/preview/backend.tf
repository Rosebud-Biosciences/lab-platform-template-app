# State for every preview workspace lives under preview/ in the org state
# bucket -- the exact prefix the bootstrap module's least-privilege preview role
# is scoped to. Replace the placeholders with your bootstrap stack's outputs
# (state_bucket_name / lock_table_name).
#
# workspace_key_prefix does the scoping, not key: workspace pr<N> stores state
# at <workspace_key_prefix>/pr<N>/<key>. The backend default ("env:") would
# land previews at env:/pr<N>/... where the preview role may not write.
terraform {
  backend "s3" {
    bucket               = "your-org-terraform-state"
    key                  = "template-app/terraform.tfstate"
    region               = "us-west-2"
    dynamodb_table       = "terraform-locks"
    encrypt              = true
    workspace_key_prefix = "preview" # pr<N> -> preview/pr<N>/template-app/terraform.tfstate
  }
}
