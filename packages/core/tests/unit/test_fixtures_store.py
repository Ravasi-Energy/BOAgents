"""Store-level tests for user-generated fixtures."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openexecutive.fixtures import store as fx_store


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(fx_store, "DB_PATH", path)
    fx_store.initialize_db(path)
    return path


def _insert(name: str = "acme", **over: object) -> int:
    kwargs: dict[str, object] = dict(
        name=name,
        display_name="Acme Inc",
        scenario_description="a test company",
        profile_yaml="company:\n  name: Acme Inc\n",
        people_yaml="people: []\n",
        departments_yaml="departments: []\n",
        memory_json="{}",
        docs_json="{}",
        industry="Widgets",
        stage="Seed",
        arr=1000000.0,
        headcount=10,
        founding_year=2020,
        mission="make widgets",
        doc_count=3,
    )
    kwargs.update(over)
    return fx_store.insert_fixture(**kwargs)  # type: ignore[arg-type]


def test_initialize_idempotent(db: Path) -> None:
    fx_store.initialize_db(db)
    fx_store.initialize_db(db)
    assert fx_store.list_fixtures(db) == []


def test_insert_and_get(db: Path) -> None:
    rid = _insert()
    assert rid > 0
    row = fx_store.get_fixture("acme", db)
    assert row is not None
    assert row["display_name"] == "Acme Inc"
    assert row["profile_yaml"].startswith("company:")


def test_list_tags_source_and_summary(db: Path) -> None:
    _insert(
        dept_summary_json='[{"title": "Finance", "head": "Sam"}]',
        people_summary_json='[{"name": "Sam", "role": "CFO", "is_principal": true}]',
    )
    rows = fx_store.list_fixtures(db)
    assert len(rows) == 1
    assert rows[0]["source"] == "generated"
    assert rows[0]["industry"] == "Widgets"
    assert rows[0]["doc_count"] == 3
    # Org summaries are parsed back into the curated list shape for the cards.
    assert rows[0]["departments"] == [{"title": "Finance", "head": "Sam"}]
    assert rows[0]["people"] == [{"name": "Sam", "role": "CFO", "is_principal": True}]
    # The serialized artifact + raw JSON columns are NOT in the list summary.
    assert "profile_yaml" not in rows[0]
    assert "dept_summary_json" not in rows[0]


def test_name_exists(db: Path) -> None:
    assert fx_store.fixture_name_exists("acme", db) is False
    _insert()
    assert fx_store.fixture_name_exists("acme", db) is True


def test_unique_name_collision_raises(db: Path) -> None:
    _insert()
    with pytest.raises(sqlite3.IntegrityError):
        _insert()


def test_soft_delete(db: Path) -> None:
    _insert()
    assert fx_store.delete_fixture("acme", db) is True
    assert fx_store.get_fixture("acme", db) is None
    assert fx_store.list_fixtures(db) == []
    # The name STILL occupies the UNIQUE slot — so slug derivation routes around
    # it instead of colliding on re-create (regression guard for the delete →
    # recreate-same-name 409 dead-end).
    assert fx_store.fixture_name_exists("acme", db) is True
    # Deleting again is a no-op.
    assert fx_store.delete_fixture("acme", db) is False


def test_archived_name_still_collides_on_insert(db: Path) -> None:
    _insert()
    fx_store.delete_fixture("acme", db)
    # Re-using a soft-deleted name must raise (UNIQUE is over all rows), which
    # is exactly why fixture_name_exists reports archived names as taken.
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db_path=db)


def test_get_missing_returns_none(db: Path) -> None:
    assert fx_store.get_fixture("nope", db) is None


def test_arr_currency_roundtrip(db: Path) -> None:
    """ARR + ISO currency insert and list back identically."""
    _insert(arr=6000.0, arr_currency="EUR")
    row = fx_store.list_fixtures(db)[0]
    assert row["arr"] == 6000.0
    assert row["arr_currency"] == "EUR"

    full = fx_store.get_fixture("acme", db)
    assert full["arr_currency"] == "EUR"


def test_arr_currency_defaults_none(db: Path) -> None:
    """Fixtures without a currency keep NULL — unknown stays unknown."""
    _insert(arr=6000.0)
    assert fx_store.list_fixtures(db)[0]["arr_currency"] is None


def test_arr_currency_migration_idempotent(tmp_path: Path) -> None:
    """A DB created before the column existed gains it on initialize_db,
    keeps existing rows, and tolerates repeated initialize_db calls."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE generated_fixtures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            scenario_description TEXT NOT NULL DEFAULT '',
            profile_yaml TEXT NOT NULL DEFAULT '',
            people_yaml TEXT NOT NULL DEFAULT '',
            departments_yaml TEXT NOT NULL DEFAULT '',
            memory_json TEXT NOT NULL DEFAULT '{}',
            docs_json TEXT NOT NULL DEFAULT '{}',
            dept_summary_json TEXT NOT NULL DEFAULT '[]',
            people_summary_json TEXT NOT NULL DEFAULT '[]',
            industry TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT '',
            arr REAL,
            headcount INTEGER,
            founding_year INTEGER,
            mission TEXT NOT NULL DEFAULT '',
            doc_count INTEGER NOT NULL DEFAULT 0,
            scenario_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1))
        );
        INSERT INTO generated_fixtures
            (name, display_name, arr, created_at, updated_at)
        VALUES ('legacy', 'Legacy Co', 6000, 't', 't');
        """
    )
    conn.commit()
    conn.close()

    fx_store.initialize_db(db)
    fx_store.initialize_db(db)  # repeated migration must not error or drop

    cols = {
        r[1]
        for r in sqlite3.connect(str(db)).execute(
            "PRAGMA table_info(generated_fixtures)"
        )
    }
    assert "arr_currency" in cols
    row = fx_store.list_fixtures(db)[0]
    assert row["display_name"] == "Legacy Co"
    assert row["arr"] == 6000.0
    assert row["arr_currency"] is None  # historical row stays unknown
