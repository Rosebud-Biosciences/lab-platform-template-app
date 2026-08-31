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
    # Reactive input: retyping re-runs only the cells that read `pattern`.
    pattern = mo.ui.text(label="Name contains", value="")
    pattern
    return (pattern,)


@app.cell
def _(mo, pattern):
    from db.engine import get_engine
    from db.models import Greeting
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    with Session(get_engine()) as session:
        names = list(
            session.scalars(
                select(Greeting.name)
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
