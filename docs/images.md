# Images

One `Dockerfile`, parameterized by `TARGET_PACKAGE`, builds a slim image
per deployable from the same workspace and lockfile — the images reviewed in
the preview are what deploy:

| Image (`TARGET_PACKAGE`) | How it runs |
| --- | --- |
| `app` | default `CMD` — uvicorn on `:8080`, probes on `/healthz` (module defaults in `infra/preview`) |
| `workflows` | the Dagster chart injects `dagster api grpc --python-file /opt/dagster/app/repo.py` (`dagster_user_code_image`) |
| `workflows` (again) | migrations: `alembic -c packages/db/alembic.ini upgrade head` — it carries `db[migrate]` |

`/healthz` deliberately skips the database: preview pods become Ready before
the migrate job has run, and `/` starts answering the moment the branch is
migrated.

## Adding a deployable

`deployables.json` is the single source of truth; to add `app2`:

1. Create `packages/app2` (a normal workspace member) and add `"app2"` to
   `deployables.json` — CI now builds/pushes `app2-<stamp>` images and
   `infra/preview` gets `local.image["app2"]` for free.
2. Wire it into `infra/preview/main.tf` — a second webapp is a second thin
   `module "workloads"` block with only `enable_webapp = true`, a distinct
   `webapp_app_name`, and `webapp_image = local.image["app2"]`; a second
   Dagster *code location* instead shares the one Dagster instance.
3. Optionally add a compose service for local runs.

The shared recipe covers packages that differ only in dependencies. If a
package needs a genuinely different recipe (system libraries, another base),
give it its own `packages/<name>/Dockerfile` — CI prefers it over the root
one automatically.

## GPU builds

`packages/workflows` has a `gpu` extra (torch as the stand-in — swap in your
real GPU stack). It is never installed by default; a GPU image is an ordinary
build with a CUDA base (uv provisions the pinned Python itself, so any base
works):

```shell
podman build \
  --build-arg TARGET_PACKAGE=workflows \
  --build-arg BASE_IMAGE=nvidia/cuda:12.8.1-runtime-ubuntu24.04 \
  --build-arg EXTRA_DEPENDENCIES="--extra gpu" \
  --build-arg GPU_DEPENDENCIES="torch" \
  -t workflows:gpu .
```

`GPU_DEPENDENCIES` names the heavy wheels: a dedicated layer installs them at
the exact versions pinned in `uv.lock`, so it only rebuilds when those pins
change — not on every lockfile edit. The platform's `examples/complete`
already runs GPU nodes (NVIDIA GPU Operator + a tainted g5/g6 Karpenter
NodePool); pods just add the `nvidia.com/gpu` toleration and resource limit.
