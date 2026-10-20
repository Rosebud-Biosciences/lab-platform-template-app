# What the app demonstrates

The template's app is deliberately small, but each piece shows a pattern
worth copying: analytical pages as notebooks, login state that branches with
a preview, groups the app owns, row-level security, and two Dagster
workflow shapes.

## Notebooks as app pages

The app itself demonstrates **notebooks as app pages**: `/notebooks/` is a
[marimo](https://marimo.io) notebook served by the webapp in run mode —
visitors get the reactive UI (filter the greetings table live) but can't edit
or execute code. Analytical views cost a notebook, not a frontend. The page
knows who is looking and shows each viewer only the rows the API would (see
below); anonymous viewers see public rows.

## Login state that branches with a preview

It also demonstrates **auth whose state branches with the environment**
(`packages/app/src/app/auth.py`). The workloads module's `auth` input tells
the app how identity reaches it (`AUTH_MODE`), and the app accepts exactly
that source:

- **Identity headers** (`auth = { mode = "headers" }`, the default and what
  `infra/preview` uses): the platform's private ingress proxy names the
  caller on every request (`Tailscale-User-Login`), and the module tells the
  app which header that is (`IDENTITY_HEADER`). The app trusts no header the
  module has not named, and none at all in `oidc` or `none` mode. Because the
  tailnet's login provider is your IdP (e.g. Google), that header is a real
  per-user identity with no OAuth client, no redirect URIs and no session
  state — exactly what per-PR preview URLs want. Trust it only where the
  proxy is the sole route to the pod (the module's NetworkPolicy admits only
  the ingress where the CNI enforces it), and scope who can reach each
  service at the ACL (`private_ingress_annotations`).
- **OIDC login** (`auth = { mode = "oidc", ... }`): the app is its own relying
  party against the platform's issuer (Dex, or any other) using the `OIDC_*`
  env the module hands it: `/login`, `/auth/callback`, `POST /logout`, with
  PKCE, a verified email required, and users keyed by issuer and subject (an
  email is an attribute; a second account presenting it is refused). If the
  webapp itself sits behind the platform's oauth2-proxy, the app verifies the
  ID token the proxy forwards against the issuer's keys (RS256 only, a
  verified email required, the user keyed by issuer and subject as on login)
  instead of trusting forwarded headers.

Either way the caller becomes rows in the app's **own** database — `users`,
`memberships` (the IdP's groups claim, re-synced on every login; a header
without a groups header leaves them alone) and server-side `sessions` — so a
preview's logins, signups and permission experiments live on the preview's
database branch and never touch prod, while identity itself (who exists,
which groups) stays the identity provider's. Sessions are stored as
`HMAC(SESSION_SECRET, id)`: a preview's branch starts with prod's rows, and
hashed with prod's secret they cannot log anyone in; preview-up also purges
them (`python -m db.maintenance purge-sessions`) right after the migrations.
The branch also inherits the platform's `nb_<tenant>__<group>` notebook
roles **with their passwords** (Neon copies roles created by SQL as they
are), so preview-up turns their logins off on the branch
(`python -m db.maintenance lock-notebook-roles`, which refuses anywhere but
Neon, where roles are per branch).
`COOKIE_SECURE` (from the module's `auth.cookie_secure`) marks both of the
app's cookies Secure, since behind the ingress the app sees plain http.

The rest is ordinary authorization code: `/whoami` reports login, groups and
how the identity arrived; greetings carry an `owner_id` and an optional
`group`, `GET /greetings` returns public ones plus those of the caller's
groups, posting to a group needs membership, and `auth.require_group("...")`
gates any route. The `/notebooks/` page applies the same rule: it resolves
its viewer from the page request (`auth.identity_from`) and reads through
`Greeting.visible_to`, the one visibility rule every reader shares.

## App-managed groups

Not every group has to come from the IdP. **App-managed groups**
(`packages/app/src/app/groups.py`) are the app's own — a review panel, a
reading club — stored and shown as `app:<name>`, a namespace no IdP group (a
path: `/<tenant>/<group>`) can collide with; an IdP group spelled `app:…` is
dropped at sync. Anyone logged in may `POST /groups` and becomes the group's
admin; its admins add members by email (`POST /groups/<name>/members` — a
bare `users` row until that person's first login binds it), promote and
remove them; and the platform's **superadmins** — members of
`APP_ADMIN_GROUP` (default `/platform-admins`), or an address in
`APP_ADMIN_EMAILS` where identity arrives without groups (the tailnet's
header) — administer every group. Their memberships share the table with the
IdP's, marked `source = 'app'`, so each login's sync replaces only the IdP's
rows, and an `app:` group works anywhere a group does: as a greeting's group,
in `require_group`. Like the rest of the auth state, a preview's app groups
are the preview's.

## Row-level security

On PostgreSQL the database enforces the visibility rule as well: **row-level
security** on `greetings` (migration `0004`). The app's login role switches
to `app_reader` for each request's transaction and names the caller's groups
in the `app.groups` setting (`db.engine.scoped`, `SET LOCAL` — nothing
outlives the transaction). Notebooks and tenant compute that connect directly
log in as roles the platform creates, `nb_<tenant>__<group>`, granted
`app_notebook`: the policy reads their group from the name they logged in
with (`session_user`) — `nb_lab__authors` sees `/lab/authors` — whatever they
set `app.groups` to, and even after `SET ROLE app_notebook`, which changes
`current_user` but not `session_user`. The table owner — migrations, the Dagster
pipelines — is unaffected. The queries keep their `Greeting.visible_to`
filter all the same: on SQLite (the tests, a laptop) the migration is a no-op
and that filter is the only rule; on PostgreSQL it is the first of two. The
two roles are cluster-wide, so the migration creates them only if missing and
its downgrade leaves them. The row-security tests need a real server,
`TEST_POSTGRES_URL` (CI starts one); without it they skip.

The platform's [`docs/auth.md`](https://github.com/Rosebud-Biosciences/lab-platform/blob/main/docs/auth.md)
has the design and the state map.

## Two Dagster workflow patterns

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
