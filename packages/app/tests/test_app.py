from pathlib import Path

import pytest
from app import main
from db.models import Base
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/app.db")
    monkeypatch.setattr(main, "_engine", None)  # fresh engine per test
    Base.metadata.create_all(main.engine())
    return TestClient(main.app)


def test_healthz_needs_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(main, "_engine", None)
    assert TestClient(main.app).get("/healthz").json() == {"status": "ok"}


def test_index_counts_greetings(client: TestClient) -> None:
    body = client.get("/").json()
    assert body["message"] == "Hello, world"
    assert body["greetings"] == 0


def test_post_greeting_increments_count(client: TestClient) -> None:
    created = client.post("/greetings", json={"name": "ci"})
    assert created.status_code == 201
    assert created.json()["name"] == "ci"

    assert client.get("/").json()["greetings"] == 1


def test_notebooks_are_mounted(client: TestClient) -> None:
    # The marimo mount serves the notebook's HTML shell (the kernel itself
    # only spins up when a browser opens a session).
    response = client.get("/notebooks/", follow_redirects=True)
    assert response.status_code == 200
    assert "marimo" in response.text.lower()
