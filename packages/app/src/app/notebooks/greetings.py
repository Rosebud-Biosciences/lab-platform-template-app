# A marimo notebook the webapp serves at /notebooks/ (see app.main). Run-mode
# only: visitors interact with the UI elements, they can't edit or run code.
# Edit it live with `uv run marimo edit packages/app/src/app/notebooks/greetings.py`.
import marimo

app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
def _(mo):
    mo.md("# Greetings explorer")
    return


@app.cell
def _(mo):
    # Who is looking: the identity the API would resolve for this page's own
    # request (session cookie, or the trusted identity header), so the page
    # shows a group's greetings only to its members, exactly as GET /greetings
    # does. No request (a script run, `marimo edit` without a login): public
    # greetings only.
    from app.auth import identity_from

    _request = mo.app_meta().request
    _viewer = identity_from(_request.headers, _request.cookies) if _request else None
    viewer_groups = _viewer.groups if _viewer else []
    return (viewer_groups,)


@app.cell
def _(mo):
    # Reactive input: retyping re-runs only the cells that read `pattern`.
    pattern = mo.ui.text(label="Name contains", value="")
    pattern
    return (pattern,)


@app.cell
def _(mo, pattern, viewer_groups):
    from db.engine import get_engine, scoped
    from db.models import Greeting
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    # scoped: on PostgreSQL, row-level security holds the query to the
    # viewer's groups too, whatever the filter below says.
    with Session(get_engine()) as session:
        scoped(session, viewer_groups)
        names = list(
            session.scalars(
                select(Greeting.name)
                .where(Greeting.visible_to(viewer_groups))
                .where(Greeting.name.contains(pattern.value))
                .order_by(Greeting.id.desc())
                .limit(50)
            )
        )

    mo.vstack(
        [
            mo.md(f"**{len(names)}** matching greetings (newest first, max 50):"),
            mo.ui.table([{"name": n} for n in names], selection=None),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
