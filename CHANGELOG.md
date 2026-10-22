# Changelog

All notable changes to this template are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). A copy
made with "Use this template" starts from `main`; each release names the
lab-platform release it pins and was validated with.

## [Unreleased]

## [0.1.0] - 2026-10-22

The first release. It pins lab-platform
[v0.3.0](https://github.com/Rosebud-Biosciences/lab-platform/releases/tag/v0.3.0)
and was validated end to end on a deployment that tracks this template
(the sandbox), 2026-10-08 to 2026-10-22.

### What a copy gets

- **Deploy:** a merge to `main` builds every deployable's image, migrates
  prod (expand-contract) and rolls the webapp and Dagster's user code.
- **A preview per pull request:** the `preview` label stamps the app,
  Dagster, MLflow and Argo in the PR's own `preview-*` namespaces and node
  pools, reachable on the tailnet; `preview:app-only` stamps the webapp
  alone, pointed at prod's services. Removing the label, closing or merging
  tears it down, and a nightly sweep catches leftovers.
- **Preview data** from one of two providers (`FORK_PROVIDER`): `tofu`, with
  fresh Neon branches and empty stores, or `tether`, with forks of prod's
  stores (Neon, Icechunk, Lance, Iceberg, Delta) taken from the pins
  `data-pull` records twice a day. Either way the PR's migrations run on its
  own database branch, and services keep their state (Dagster runs, MLflow
  experiments, Argo's archive) on branches of prod's (docs/data.md).
- **Login state on the preview's branch:** the tailnet names the caller, and
  users and sessions live on the preview's database, never prod's.
- **A drift check** for copies that keep tracking the template
  (`template-drift.yml`, `.github/template-drift/`).

### Requirements

- lab-platform v0.3.0's shared pieces: a cluster with the Tailscale operator
  and `preview_access`, the bootstrap stack's CI and preview roles, an ECR
  repository, and Neon databases (README, "Adopt this template").
- **A protected Neon branch** for prod's databases, which needs a paid Neon
  plan. Neon copies roles to a branch with their passwords unless its parent
  is protected, so `infra/preview` refuses to apply on a branch made from an
  unprotected one (docs/app.md).
- Tether mode only: `tether-vcs`, which is alpha; its Neon and Iceberg
  backends are experimental.

### Validated

On the sandbox, in tether mode: previews forked every store and left prod's
untouched, and teardown retired every fork; a preview's login state stayed on
its branch; a preview refused an unprotected Neon parent, and on a protected
one prod's notebook passwords no longer opened the fork; `tether-matrix`
passed for every backend; `data-pull` and Deploy ran green; and the
platform's overnight and several-days pauses resumed without hand repairs.
The record is the sandbox's `docs/sandbox-validation.md`.

Not validated on AWS, only in lab-platform's kind smoke test:
`auth = { mode = "oidc" }` with the platform's Dex, and the platform's
identity and tenancy modules.

[Unreleased]: https://github.com/Rosebud-Biosciences/lab-platform-template-app/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Rosebud-Biosciences/lab-platform-template-app/releases/tag/v0.1.0
