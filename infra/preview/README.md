# Preview stack (one Terraform workspace per PR)

This is this app's copy of the platform's [preview pattern](https://github.com/Rosebud-Biosciences/terraform-aws-lab-platform/blob/main/docs/preview-environments.md):
everything is stamped with `pr<N>-`, lands on the **shared** cluster, and is
destroyed when the PR closes. The app repo owns this stack so the preview shape
(which workloads, which image) evolves with the app, while the mechanics come
from the platform repo's published modules and reusable workflows.

What a preview contains:

- **webapp** — `packages/app` served by the PR's image behind a prefixed
  private (tailnet) ingress: `https://pr123-webapp.<tailnet>.ts.net`.
- **Dagster** — same image as the code location (`packages/workflows`), with
  its run storage on a branched database.
- **Its data**, from one of two providers (`fork_provider`, README "Ephemeral
  data"). `tofu` (default): copy-on-write Neon branches of the prod `app` and
  `dagster` databases (the PR's alembic migrations run against the `app`
  branch), an ephemeral bucket for every other store, an ephemeral Iceberg
  namespace when `iceberg_table_bucket_arn` is set — all destroyed with the
  stack. `tether`: CI forks the production stores first and passes
  `database_url` / `dagster_db_*` / `data_refs` in (`external.auto.tfvars.json`
  via the reusable workflow's `extra_tfvars_json`); this stack then only grants
  the pods access to those stores (`data-access`). Either way the pods get
  `DATABASE_URL` + `DATA_REFS` (`data.tf`).
- **A small Karpenter NodePool** — scales to zero when idle.

Or, with `preview_profile = "app"` (the `preview:app-only` PR label), just the
first item: the webapp on its own database branch, with `DAGSTER_WEBSERVER_URL`
(and `MLFLOW_TRACKING_URI`, if prod runs MLflow) pointing at **prod's**
services through `shared_service_urls` in `shared-platform.auto.tfvars` — the
prod stack's `in_cluster_urls` output. No Dagster, no Ray, no NodePool, one
image build, ~2 minutes to green. The trade: runs the preview's app triggers
execute prod's code location on prod's data while the webapp reads its own
branch, so this is for frontend/API changes only. Full account of the
trade-offs: the platform's `modules/workloads` README, "Stamp or share".

CI drives this stack via the platform repo's reusable workflows
(`.github/workflows/preview-up.yml` in this repo passes
`working_directory: infra/preview`, gated on the PR wearing the `preview`
label). To drive it by hand:

Image references are derived, not passed one-by-one: `deployables.json` (the
same file CI's build matrix reads) times `image_base`/`image_stamp` yields
`local.image[<name>]` — adding a deployable adds no variables here.

```shell
tofu init
tofu workspace select -or-create pr123
tofu apply -var preview_name=pr123 \
  -var image_base=<account>.dkr.ecr.<region>.amazonaws.com/template-app \
  -var image_stamp=pr123-<sha>
# tether mode by hand: fork first, then feed the stack what the fork printed
#   uv run tether new -b pr123 --eager
#   .github/scripts/tether-open-all.sh > infra/preview/external.auto.tfvars.json
#   tofu apply -var preview_name=pr123 -var fork_provider=tether ...
tofu destroy -var preview_name=pr123   # image + tether-mode vars have defaults for destroy
tofu workspace select default && tofu workspace delete pr123
```

Before first use, replace the placeholders in:

- `backend.tf` — your state bucket / lock table (bootstrap module outputs);
- `shared-platform.auto.tfvars` — your cluster/VPC/Karpenter/Neon identity;
- the `github.com/Rosebud-Biosciences/...?ref=main` module sources — a pinned
  release tag (and your fork, if you forked the platform too).
