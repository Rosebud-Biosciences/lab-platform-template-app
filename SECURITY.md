# Security policy

A repository created from this template copies this file: replace it with
your own policy.

## Reporting a vulnerability

Report it privately: this repository's **Security** tab, **Report a
vulnerability**. Only the maintainers can read the report. Please do not open
a public issue, pull request or discussion about it.

## Scope

In scope: the code and automation in this repository -- the app's login and
sessions (`packages/app`), row-level security (`packages/db`), the preview,
data and deploy workflows and their scripts (`.github/`), and
`infra/preview`. The platform modules the workflows call are
[lab-platform](https://github.com/Rosebud-Biosciences/lab-platform)'s; report
those there.

## Before you adopt

A preview runs a pull request's code, and the preview role runs its workflow.
For a branch of the repository (a fork gets no OIDC token, so its previews
skip), whoever can push the branch controls both:

- The preview role is confined to `preview-*` namespaces only once the
  platform installs `preview_access` and the role's access entry is scoped
  to them (README, "Adopt this template"); otherwise it is cluster-admin and
  a branch can change the whole cluster, prod's namespaces included. The
  preview permissions boundary reaches only what a preview owns plus
  `aws/bootstrap`'s `preview_boundary_access`. lab-platform's SECURITY.md
  says what each allows.
- With `FORK_PROVIDER=tether`, a preview's pods, and its workflow, write
  into the production stores: their own forks, never delete, and never the
  trunk or its pins (`protect_trunk`), except Iceberg, whose refs share one
  metadata file. Opt in only where the PR's code is trusted with that.
- The `preview` label check is in a workflow file the PR can edit. Grant
  write access to the repository accordingly.
