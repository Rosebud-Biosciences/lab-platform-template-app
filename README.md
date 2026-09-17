# lab-platform template app

A hello-world consumer of
[terraform-aws-lab-platform](https://github.com/Rosebud-Biosciences/terraform-aws-lab-platform):
a `uv` workspace with a database, a webapp, and a Dagster pipeline, plus CI
that gives every pull request its **own preview environment** — branched
database included — and deploys `main` to prod. Every piece is deliberately
tiny; the value is the wiring.

Use it as a GitHub template ("Use this template"), then follow
[Adopt this template](#adopt-this-template).

## The loop

```mermaid
flowchart LR
    PR["PR labeled 'preview' / pushed"] --> B[build app + workflows images]
    B --> UP["preview-up (reusable):<br/>Neon branches + prefixed workloads<br/>on the shared cluster"]
    UP --> MIG[alembic upgrade head<br/>on the PR's DB branch]
    MIG --> REV[review at prN-webapp.tailnet]
    REV --> M[merge]
    M --> D["deploy: build :main-sha,<br/>migrate prod, kubectl set image"]
    PR -. label removed / closed .-> DOWN["preview-down (reusable):<br/>destroy workloads, branches, bucket"]
```

- **Previews** (`.github/workflows/preview-up.yml`): a PR labeled `preview`
  gets a Terraform workspace of [`infra/preview`](infra/preview/) stamped
  `pr<N>-`: the PR's images serve the webapp and the Dagster code location,
  against copy-on-write Neon branches of prod data. The PR's own migrations
  run on the PR's own branch — schema experiments never touch prod. The label
  is the gate, so most PRs deploy nothing.
- **Teardown** (`preview-down.yml` + nightly `sweep.yml`): removing the label
  or closing the PR destroys everything — a preview can be parked while its
  PR stays open — and the sweep catches anything that slips through.
- **Deploy** (`deploy.yml`): on `main`, migrate the prod database
  (expand-contract: the old revision keeps running during the rollout), then
  `kubectl set image`. The prod stack sets `webapp_ignore_image_changes = true`
  so CI owns the running tag and `tofu apply` never fights it.

## Layout

| Path | What |
| --- | --- |
| `packages/db` | SQLAlchemy models, engine helper, Alembic migrations (the schema) |
| `packages/app` | FastAPI webapp: `/` reads the DB, `/healthz` never does, marimo notebook at `/notebooks/`, tailnet identity at `/whoami`, the data it is wired to at `/data` |
| `packages/dataset` | The app's [tether](https://github.com/elyall/tether) dataset as a package: `tether.toml` + one manifest per data object (Neon x2, Icechunk, Iceberg, Lance, Delta, an S3 prefix) next to the `DATA_REFS` contract (`refs.py`) and the native openers (`openers.py`, extra `[stores]`) |
| `packages/workflows` | Dagster assets/jobs/schedule/sensor: Ray fan-out + streaming micro-batch examples, plus one asset per data store (`stores.py`) through `dataset` |
| `deployables.json` | The deployables (single source of truth for CI's build matrix and `infra/preview`) |
| `infra/preview` | This app's per-PR preview stack (platform modules, remote source); `fork_provider` picks who forks the data |
| `Dockerfile` | One shared recipe, per-package images via `TARGET_PACKAGE`; GPU via `BASE_IMAGE` + `--extra gpu` |
| `.github/workflows` | ci / preview-up / preview-down / sweep / deploy, plus data-pull (nightly pins of prod data) and tether-matrix (tether's live backend test, off by default) |
| `.github/scripts` | The tether half of the preview loop: `tether-fork.sh`, `tether-down.sh`, `tether-open-all.sh`, `tether-matrix.sh` |

The workspace mirrors a production monorepo at hello-world scale: packages
stay dependency-light and import each other through the workspace
(`app -> db`, `workflows -> db[migrate]`), tests and lint run from the root,
and one lockfile pins everything including the image build.

## Run it locally

```shell
uv sync                  # everything, including dev tooling
uv run pytest            # 27 tests, no database, services or containers needed (sqlite + local Ray + local stores)
uv run ruff check .
uv run ty check

podman compose up --build   # postgres -> alembic upgrade head -> webapp
curl localhost:8080/                             # {"message":"Hello, world",...}
curl -X POST localhost:8080/greetings -H 'content-type: application/json' -d '{"name":"me"}'
open http://localhost:8080/notebooks/            # reactive marimo page over the same DB

uv run dagster dev -f packages/workflows/repo.py   # Dagster UI on :3000
```

Everything container-side is plain compose spec / OCI, so `docker compose`
works identically if that's what you have.

With no `DATA_REFS` set, the store assets (`data_stores_job` in the Dagster
UI) write to local stores under `.data/` (`DATA_ROOT` to move it) with a sqlite
Iceberg catalog: every backend in the matrix runs on a laptop, and
`cd packages/dataset && uv run tether status` works against the same
directory. See "Ephemeral data".

The app itself demonstrates **notebooks as app pages**: `/notebooks/` is a
[marimo](https://marimo.io) notebook served by the webapp in run mode —
visitors get the reactive UI (filter the greetings table live) but can't edit
or execute code. Analytical views cost a notebook, not a frontend. It ships
with no login gate because the platform serves the app tailnet-private;
reachability is the access control — add auth before exposing it publicly.

It also demonstrates **identity for free on the tailnet**: `/whoami` echoes
the `Tailscale-User-Login` header the platform's private ingress proxy asserts
on every request. Because the tailnet's login provider is your IdP (e.g.
Google), that header is a real per-user identity with no OAuth client, no
redirect URIs, and no session state — exactly what per-PR preview URLs want.
Build on it only where the proxy is the sole route to the pod, and scope who
can reach each service at the ACL with the workloads module's
`private_ingress_annotations` (per-service Tailscale device tags).

Two workflow patterns to try in the Dagster UI:

- **Fan-out over Ray** — `ray_fanout_job` materializes `ray_fanned_greetings`:
  Dagster orchestrates, Ray fans eight tasks out (locally when `RAY_ADDRESS`
  is unset, across your Ray cluster when set, e.g.
  `ray://<prefix>kuberay-head-svc.ray.svc:10001`), and the results are written
  back to the database in one place. Ray client connections require the
  cluster and this image to agree on Ray and Python *minor* versions — this
  repo is on Python 3.14, so point the platform's `ray_image_tag` at a
  matching `-py314` image (or pin this repo to the cluster's Python). The
  version-proof approach: build the workflows image FROM the cluster's own
  base (`BASE_IMAGE=rayproject/ray:<ray_version>-py314`), and both match by
  construction.
- **Streaming (micro-batch)** — toggle `greeting_stream_sensor` on, then
  `curl -X POST localhost:8080/greetings ...` a few times: within a tick the
  sensor's cursor notices the new rows and launches `stream_digest_job` for
  exactly that id window. Cursor sensors emitting micro-batch runs are
  Dagster's idiom for streaming sources (a Kafka topic or S3 prefix slots into
  the same shape: poll offsets in the sensor, hand the window to the run).

## The image contract

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

### Adding a deployable

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

### GPU builds

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

## Ephemeral data

A preview needs data, and there are two ways to give it some. Both end in the
same contract, so nothing in `packages/` knows which is in use:

- `DATABASE_URL` — the preview's Postgres.
- `DATA_REFS` — JSON `key -> address`, one entry per object in the dataset's
  manifests, in the forms `tether open --json` prints
  (`s3://…/x.icechunk#<branch>`, `lake.table#<branch>`, `s3://…/x.lance#<branch>`,
  `s3://…/x.delta[@vN]`, `s3://…/prefix/`). `dataset.refs` parses it; a pinned
  version or snapshot makes the opener read-only. The webapp's `/data` echoes
  what it received.

### Where the dataset lives

The dataset is a package, `packages/dataset`: the tether root (`tether.toml`,
`.tether/objects/`) and the Python that names those objects (`dataset.refs`)
in one directory, so `workflows` and `app` depend on it like they depend on
`db`, the images carry it, and `packages/dataset/tests/test_manifests.py` fails
the moment the manifests, the key constants and `infra/preview/data.tf` stop
agreeing. Adding a store is `tether add`, one constant, one asset, one line of
HCL. This is the shape for a dataset one app owns — the greetings-derived
stores here.

A dataset several codebases share is tether's native case (its guides register
the *code* repository as an object of the dataset, not the reverse) and it
lives in a repository of its own. Bring it into this repo as a git submodule
and point the tether machinery at it:

```shell
git submodule add git@github.com:Rosebud-Biosciences/lab-dataset.git datasets/lab
# repository variable DATASET_ROOT=datasets/lab
```

Every `tether-*.sh` script and the `data-pull`, `preview-*`, `sweep` and
`tether-matrix` jobs read `DATASET_ROOT` (default `packages/dataset`), run
tether inside it, and run git against the repository that *contains* it — the
dataset repo, for a submodule. Three consequences follow from that:

- the `pr<N>` bookmark branches are pushed to, and retired from, the dataset
  repo, so the checkout needs a token or deploy key with write access to it
  (the default `GITHUB_TOKEN` only reaches this repo); the workflows already
  check out with `submodules: true`;
- `data-pull.yml` commits to the dataset repo's default branch, and this repo's
  submodule pointer trails it until bumped — the commented `gitsubmodule`
  ecosystem in `.github/dependabot.yml` opens those bumps as PRs;
- the code that names the objects (`dataset.refs`) still lives here, so the
  dataset repo's manifests and this package's keys are checked by the same
  test — set `DATASET_ROOT` for `pytest` too, or keep a `packages/dataset`
  whose `tether.toml` is a thin registration of the shared repo's objects.

`TETHER_REV` pins reproductions across the boundary the same way in both
layouts: `TETHER_REV=<dataset commit> uv run python make_report.py` opens
every object at that commit's pins.

`infra/preview`'s `fork_provider` (set from the `FORK_PROVIDER` repository
variable) selects the provider:

| | `tofu` (default) | `tether` |
| --- | --- | --- |
| Who forks | the preview stack, with the platform's `neon-branches`, `preview-storage`, `iceberg-branches` | CI's `fork-data` job, with `tether new -b pr<N> --eager` |
| Postgres | copy-on-write Neon branch, tuned compute | tether fork of the Neon project (one branch serves `app` and `dagster`), `--pin record` |
| Icechunk / Lance / Delta / files | fresh, **empty** copies in the preview's ephemeral bucket | branches inside the **production** stores, forked from the last pinned state |
| Iceberg | empty namespace of the preview's own | a branch on the prod table |
| Across pushes to the PR | data persists | re-forked from the baseline each push |
| Which prod state was tested | not recorded | the pinned dataset commit on `main` (nightly `data-pull`) |
| Landing preview data on prod | not possible | `tether promote` on merge for Icechunk and Iceberg (fast-forward); prod recomputes the rest |
| Preview's access to prod data | none | read/write (no delete) on the store prefixes, read/commit on the tables (`aws/data-access`) |
| Dependencies | none | `tether-vcs` (alpha; its Neon and Iceberg backends are `experimental`) |

Two facts about tether mode belong next to the decision. A fork of an Iceberg
table on S3 Tables is a branch on the *production* table, so preview pods need
commit rights IAM cannot scope to a branch — the code is trusted, tether's
operation log and `verify` audit it. And user refs on an S3 Tables table
suspend its automatic maintenance while they exist, so forks are short-lived
and swept.

The tether loop, end to end (tether mode):

```mermaid
flowchart LR
  Label[PR labeled preview] --> Fork[fork-data: tether new -b prN --eager]
  Fork --> Up[preview-up: apply with database_url + data_refs]
  Up --> Pods[pods: DATABASE_URL + DATA_REFS on the fork]
  Pods --> Migrate[migrate: alembic on the fork]
  Close[PR closed / unlabeled] --> Destroy[preview-down: destroy]
  Destroy --> DataDown[data-down: merged? promote zarr+lake, then release; else release + delete PR-created stores]
  Nightly[data-pull nightly] --> Main[main: tether pull + verify]
```

`data-pull.yml` runs in both modes: it is the record of which prod state each
dataset commit describes, and the baseline tether-mode previews fork from.
`tether-matrix.yml` (weekly, **off** until `ENABLE_TETHER_MATRIX=true`) forks
every store, writes through the assets above, pins, diffs, dry-runs a promote
and releases — tether's live test against the real services, reported per
backend in one self-refreshing issue. Backends that need a service of their
own (DuckLake, lakeFS, Dolt) are not registered; `tether add` them when you run
one.

## Adopt this template

Prerequisites (once, from the platform repo): a shared cluster + Tailscale
operator (`examples/complete`), the bootstrap stack's CI/preview OIDC roles and
state bucket (`aws/bootstrap`), an ECR repository, and Neon projects for
`app` and `dagster`.

1. The `Rosebud-Biosciences/terraform-aws-lab-platform` references (workflow
   `uses:` lines, `infra/preview` module sources, links) point at the upstream
   platform repo. Forking the platform too? Find-and-replace them with your
   fork. Either way, pin `?ref=main` to a platform release tag. (Neither
   `uses:` nor a module `source` accepts a variable, so this is a literal.)
2. Fill in `infra/preview/backend.tf` (state bucket/lock table) and
   `infra/preview/shared-platform.auto.tfvars` (cluster, VPC, Karpenter role,
   tailnet suffix, Neon parent branches).
3. Repository **variables**: `CI_ROLE_ARN`, `PREVIEW_ROLE_ARN`, `CLUSTER_NAME`,
   `AWS_REGION`, `ECR_REPOSITORY`. `CLUSTER_NAME` is also the switch: until it
   is set, every deployment workflow (deploy, preview-up/down, sweep,
   data-pull) skips itself, so a fresh copy of the template runs only `ci`.
4. Repository **secrets**: `NEON_API_KEY`, `TS_OAUTH_CLIENT_ID`,
   `TS_OAUTH_SECRET`, `PROD_DATABASE_URL`; and, while the platform repo is
   private, `MODULES_GIT_TOKEN` (a fine-grained PAT or App token with
   read-only Contents on it) so `tofu init` can fetch the module sources.
5. For `deploy.yml`: an EKS access entry granting `CI_ROLE_ARN` edit rights on
   the `webapp` and `dagster` namespaces, and a prod stack that enables the
   webapp with `webapp_ignore_image_changes = true`.
6. Create the `preview` repo label. Open a PR that adds a column and a
   migration, label it `preview` — watch it build, branch, migrate, and serve
   at `https://pr<N>-webapp.<tailnet>.ts.net`. Remove the label to tear the
   preview down without closing the PR.
7. Point the data objects at real stores: edit the locators in
   `packages/dataset/.tether/objects/*.toml` (or `tether remove` / `tether add`
   them from that directory), the Iceberg catalog in
   `packages/dataset/tether.toml`, and `data_bucket_arn` /
   `iceberg_table_bucket_arn` in `shared-platform.auto.tfvars`. Then
   `cd packages/dataset && uv run tether commit -m "Baseline"` on `main` pins
   prod's current state, and `data-pull.yml` keeps it fresh. Consuming a
   dataset from its own repository instead: "Where the dataset lives".
8. Optional, tether mode: set the Neon project's `default_endpoint_settings`
   (0.25–2 CU, 300 s suspend) — tether creates fork endpoints with the
   project defaults, so this is where the cost stays where the tofu path had
   it. Grant the data-access policy to a role and set `DATA_ROLE_ARN` (or
   attach it to `PREVIEW_ROLE_ARN`), set `DATA_ROOT_URI`
   (`s3://<bucket>/tether/`) so an abandoned PR's own stores can be deleted,
   then `FORK_PROVIDER=tether`. Same-repo PRs only: `fork-data` pushes the
   `pr<N>` bookmark branch. Once real stores exist, `ENABLE_TETHER_MATRIX=true`
   turns on the weekly live test.

## Costs

A preview is one small spot node (scales to zero when idle), Neon branches
(copy-on-write, ~free until written), an empty S3 bucket, and two tailnet
ingresses. The expensive things — cluster, NAT, operators — are shared and
already running. tether mode swaps the bucket for branches inside the prod
stores (only new chunks cost anything) and the Neon branch for tether's fork;
the weekly matrix, when enabled, wakes the Neon compute once and writes a few
kilobytes per store.

## License

Apache-2.0, same as the platform repo.
