# Preview stack (one Terraform workspace per PR)

This is this app's copy of the platform's [preview pattern](https://github.com/your-org/terraform-aws-lab-platform/blob/main/docs/preview-environments.md):
everything is stamped with `pr<N>-`, lands on the **shared** cluster, and is
destroyed when the PR closes. The app repo owns this stack so the preview shape
(which workloads, which image) evolves with the app, while the mechanics come
from the platform repo's published modules and reusable workflows.

What a preview contains:

- **webapp** — `packages/app` served by the PR's image behind a prefixed
  private (tailnet) ingress: `https://pr123-webapp.<tailnet>.ts.net`.
- **Dagster** — same image as the code location (`packages/workflows`), with
  its run storage on a branched database.
- **Neon branches** — copy-on-write clones of the prod `app` and `dagster`
  databases; the PR's alembic migrations run against the `app` branch.
- **Ephemeral bucket** — anything the preview writes to object storage.
- **A small Karpenter NodePool** — scales to zero when idle.

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
# ...
tofu destroy -var preview_name=pr123   # image vars have defaults for destroy
tofu workspace select default && tofu workspace delete pr123
```

Before first use, replace the placeholders in:

- `backend.tf` — your state bucket / lock table (bootstrap module outputs);
- `shared-platform.auto.tfvars` — your cluster/VPC/Karpenter/Neon identity;
- the `github.com/your-org/...?ref=main` module sources — your fork and a
  pinned release tag.
