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

- The platform's known gaps apply: a preview deploys as cluster-admin, so a
  branch can change the whole cluster, prod's namespaces included; and the
  preview permissions boundary caps actions, not resources, until
  `preview_boundary_resources` is set. lab-platform's SECURITY.md says what
  each allows and what to do.
- With `FORK_PROVIDER=tether`, a preview's pods write into the production
  stores (never delete). Opt in only where the PR's code is trusted with that.
- The `preview` label check is in a workflow file the PR can edit. Grant
  write access to the repository accordingly.
