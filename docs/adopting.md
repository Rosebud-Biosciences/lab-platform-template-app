# Adopting the template

## Prerequisites

Prerequisites (once, from the platform repo): a shared cluster + Tailscale
operator (`examples/complete`), the bootstrap stack's CI/preview OIDC roles and
state bucket (`aws/bootstrap`, with `preview_state_read_keys =
["template-app/terraform.tfstate"]`, this stack's backend key: the preview
role reads no other state), an ECR repository, and Neon databases for
`app`, `dagster`, `mlflow` and `argo` (one project with four databases, or
several; see `shared-platform.auto.tfvars`). On the cluster, scope the
preview role to its own namespaces: `aws/eks-platform`'s `preview_access`
(with `dex_namespace` if the platform runs Dex), and an access entry mapping
the preview role to its `preview_access_group` with no access policy, not
cluster admin. This stack names every preview namespace `preview-pr<N>-…` to
match and binds itself admin in each (`preview_namespace_admin`, defaulting
to `preview_access`'s names).

## Steps

1. The `Rosebud-Biosciences/lab-platform` references (workflow
   `uses:` lines, `infra/preview` module sources, links) point at the upstream
   platform repo, pinned to its release `v0.3.0`: the reusable workflows run
   with your AWS roles, so never point them at a moving branch. Forking the
   platform too? Find-and-replace them with your fork. Upgrading: bump every
   `@v0.3.0` and `?ref=v0.3.0` together, after reading the platform's
   CHANGELOG. (Neither `uses:` nor a module `source` accepts a variable, so
   this is a literal.)
2. Fill in `infra/preview/backend.tf` (state bucket/lock table) and
   `infra/preview/shared-platform.auto.tfvars` (cluster, VPC, Karpenter role,
   tailnet suffix, Neon parent branches, and the bootstrap stack's
   `preview_permissions_boundary_arn`: the preview role creates IAM only
   under `/preview/` and no role without that boundary).
3. Repository **variables**: `CI_ROLE_ARN`, `PREVIEW_ROLE_ARN`, `CLUSTER_NAME`,
   `AWS_REGION`, `ECR_REPOSITORY`. `CLUSTER_NAME` is also the switch: until it
   is set, every deployment workflow (deploy, preview-up/down, sweep) skips
   itself, and so does data-pull until `AWS_REGION` and a role are set (it
   needs the stores, not the cluster, so it keeps pinning while a cluster is
   down). A fresh copy of the template runs only `ci`.
4. Repository **secrets**: `NEON_API_KEY`, `TS_OAUTH_CLIENT_ID`,
   `TS_OAUTH_SECRET`, `PROD_DATABASE_URL`. Pointing at a private fork of the
   platform instead? `tofu init` then needs a token that can read it: pass
   one to the reusable workflows as their `modules_git_token` secret, and
   configure git with it before the `tofu init` steps here.
5. For `deploy.yml`: an EKS access entry granting `CI_ROLE_ARN` edit rights on
   the `webapp` and `dagster` namespaces, and a prod stack that enables the
   webapp with `webapp_ignore_image_changes = true`.
6. Create the `preview` repo label. Open a PR that adds a column and a
   migration, label it `preview` — watch it build, branch, migrate, and serve
   at `https://pr<N>-webapp.<tailnet>.ts.net`. Remove the label to tear the
   preview down without closing the PR.
7. Point the data objects at real stores: edit the locators in
   `packages/dataset/.tether/objects/*.toml` (or `tether remove` / `tether add`
   them from that directory), the Iceberg catalog's `warehouse` in
   `packages/dataset/tether.toml`, and `data_bucket_arn` (with its KMS key,
   `data_bucket_kms_key_arn`, unless the bucket is SSE-S3) /
   `iceberg_table_bucket_arn` in `shared-platform.auto.tfvars`. tether reads
   the catalog's endpoint from the environment, never the committed file: CI
   gets it from `.github/scripts/tether-env.sh`; on a laptop, export the same
   four `PYICEBERG_CATALOG__S3TABLES__*` variables, or put the whole catalog
   (`type`, `warehouse`, `uri` and the three `rest.*` signing keys: it
   replaces the committed table rather than merging) in the untracked
   `packages/dataset/.tether/secrets.toml` under `[backends.iceberg.catalog]`.
   Then
   `cd packages/dataset && uv run tether commit -m "Baseline"` on `main` pins
   prod's current state, and `data-pull.yml` keeps it fresh. Consuming a
   dataset from its own repository instead: [Where the dataset lives](data.md#where-the-dataset-lives).
8. Optional, tether mode: set the Neon project's `default_endpoint_settings`
   (0.25–2 CU, 300 s suspend) — tether creates fork endpoints with the
   project defaults, so this is where the cost stays where the tofu path had
   it. Grant the data-access policy to a role and set `DATA_ROLE_ARN` (or
   attach it to `PREVIEW_ROLE_ARN`), with `working_branch_prefix = "tether.ws."`
   so Preview Down can delete Lance forks, and `protect_trunk = true`, so a
   PR's workflow -- like its pods (`infra/preview`) -- writes its forks and
   never prod's trunk or pins. List the same stores in `aws/bootstrap`'s
   `preview_boundary_access` (the data bucket under `read`, its prefixes and
   the prod tables under `write`, the Lance working branches under `delete`,
   its key under `kms_key_arns`): the preview boundary refuses anything
   unlisted. For what a pull_request run must not hold (deleting the stores a
   PR created and MLflow artifacts; writing the pins), enable
   `aws/bootstrap`'s teardown role (main's runs only), attach the data-access
   policy with `allow_delete = true` to it, and set `TEARDOWN_ROLE_ARN`: the
   nightly sweep, `data-pull` and `tether-matrix` use it. Set `DATA_ROOT_URI` (`s3://<bucket>/tether/`), where
   those deletes may happen, then `FORK_PROVIDER=tether`. Same-repo PRs only:
   `fork-data` pushes the `pr<N>` bookmark branch. Once real stores exist,
   `ENABLE_TETHER_MATRIX=true` turns on the weekly live test.
9. Replace `SECURITY.md` and `.github/CODEOWNERS`, which name this template's
   maintainers, with your own. A copy that keeps tracking this template: set
   the `TEMPLATE_REPOSITORY` variable to turn on `template-drift` ("Where the
   dataset lives").
