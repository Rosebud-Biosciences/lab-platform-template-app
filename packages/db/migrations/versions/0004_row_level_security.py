"""row-level security on greetings (PostgreSQL)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22

The database enforces the visibility rule the app's queries already apply
(`Greeting.visible_to`: public greetings plus the viewer's groups'), for two
kinds of reader:

- ``app_reader``: the app itself. Its login role (the one running these
  migrations, which is granted ``app_reader``) switches to it per transaction
  and names the caller's groups in ``app.groups`` (`db.engine.scoped`).
- ``app_notebook``: notebooks and tenant compute that connect directly, as
  LOGIN roles the platform creates and grants ``app_notebook``. A role
  ``nb_<tenant>__<group>`` sees the group ``/<tenant>/<group>``, read from the
  name it logged in with (``session_user``), whatever it sets ``app.groups``
  to -- and even after ``SET ROLE app_notebook``, which a plain grant allows on
  PostgreSQL 16+ and which changes ``current_user`` but never
  ``session_user``. (A grant ``WITH SET FALSE`` would forbid the ``SET ROLE``
  too; the platform's provider cannot express it, so the function does not
  rely on it.) This migration does not create those roles, and no other
  login role may be named ``nb_...``.

The table owner -- migrations, the pipelines -- is unaffected (no FORCE ROW
LEVEL SECURITY). Both roles are cluster-wide, so they are created only if
missing and left in place on downgrade: other databases on the cluster may
use them. A migrating role that did not create them needs them granted WITH
ADMIN OPTION to grant ``app_reader`` to itself.

Anything but PostgreSQL (SQLite in tests and on a laptop) has no roles and
no row security: this revision is a no-op there, and the query filter is
the only rule.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

ROLES = ("app_reader", "app_notebook")

VIEWER_GROUPS = r"""
CREATE FUNCTION app_viewer_groups() RETURNS text[] STABLE LANGUAGE sql AS $$
  SELECT CASE
    WHEN session_user LIKE 'nb\_%' THEN ARRAY['/' || replace(substr(session_user, 4), '__', '/')]
    ELSE coalesce(string_to_array(nullif(current_setting('app.groups', true), ''), ','), '{}')
  END $$
"""

GREETINGS_SEQUENCE = "pg_get_serial_sequence('greetings', 'id')"


def _postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _postgres():
        return
    for role in ROLES:
        op.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') "
            f"THEN CREATE ROLE {role} NOLOGIN; END IF; END $$"
        )
    # So the app's login role can SET ROLE app_reader (db.engine.scoped).
    op.execute("GRANT app_reader TO current_user")

    op.execute("GRANT SELECT, INSERT ON greetings TO app_reader")
    op.execute(
        "DO $$ BEGIN EXECUTE format('GRANT USAGE ON SEQUENCE %s TO app_reader', "
        f"{GREETINGS_SEQUENCE}); END $$"
    )
    op.execute("GRANT SELECT ON greetings TO app_notebook")

    op.execute(VIEWER_GROUPS)
    # The table owner (migrations, pipelines) is unaffected.
    op.execute("ALTER TABLE greetings ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY greetings_read ON greetings FOR SELECT TO app_reader, app_notebook "
        "USING (group_name IS NULL OR group_name = ANY (app_viewer_groups()))"
    )
    op.execute(
        "CREATE POLICY greetings_write ON greetings FOR INSERT TO app_reader "
        "WITH CHECK (group_name IS NULL OR group_name = ANY (app_viewer_groups()))"
    )


def downgrade() -> None:
    if not _postgres():
        return
    op.execute("DROP POLICY greetings_write ON greetings")
    op.execute("DROP POLICY greetings_read ON greetings")
    op.execute("ALTER TABLE greetings DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION app_viewer_groups()")
    op.execute("REVOKE SELECT ON greetings FROM app_notebook")
    op.execute(
        "DO $$ BEGIN EXECUTE format('REVOKE USAGE ON SEQUENCE %s FROM app_reader', "
        f"{GREETINGS_SEQUENCE}); END $$"
    )
    op.execute("REVOKE SELECT, INSERT ON greetings FROM app_reader")
    # The roles, and the login role's membership in app_reader, stay: they
    # are cluster-wide and may serve other databases.
