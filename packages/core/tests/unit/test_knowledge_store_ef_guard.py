"""SEC-03: guard-ul de integritate pe configurația EF persistată.

ChromaDB 1.5.9 instanțiază embedding function din config persistat la
primul embed (CollectionCommon._embed → load_collection_configuration_
from_json → known_embedding_functions[name].build_from_config). Un
director chroma_db alterat pe disc (schema_str în chroma.sqlite3) poate
astfel transforma următorul add/query în încărcare de cod (ex.
sentence_transformer + trust_remote_code). Guard-ul validează numai prin
``configuration_json`` — citire probată fără build — înainte de orice
operație care ar putea embed-ui.

Nicio probă de aici nu execută payload-uri și nu descarcă modele:
``get_or_create_collection`` serializarea EF-ului default este
instanțiere ieftină fără rețea (download-ul ONNX are loc la __call__).
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from openexecutive.knowledge.store import (
    ChromaDBStore,
    PersistedEmbeddingConfigError,
    _validate_persisted_ef_config,
)


def _make_db_with_collection(path: Path, name: str = "company_docs") -> None:
    import chromadb
    from chromadb.config import Settings

    # Same settings as ChromaDBStore — SharedSystemClient caches per path
    # and refuses a second client with different settings.
    chromadb.PersistentClient(
        path=str(path), settings=Settings(anonymized_telemetry=False)
    ).get_or_create_collection(name=name)


def _tamper_schema(path: Path, collection: str, ef_payload: dict, *, key: str = "#embedding") -> None:
    """Inject an embedding_function config into the persisted schema_str.

    key="#embedding" tampers the dense vector index (surfaced into
    configuration_json); key="sparse" tampers defaults.sparse_vector
    instead, which does NOT surface — the residual-limit case.
    """
    db = path / "chroma.sqlite3"
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT schema_str FROM collections WHERE name=?", (collection,)
    ).fetchone()
    sch = json.loads(row[0])
    if key == "#embedding":
        sch["keys"]["#embedding"]["float_list"]["vector_index"]["config"][
            "embedding_function"
        ] = ef_payload
    else:
        sch["defaults"]["sparse_vector"]["sparse_vector_index"]["config"][
            "embedding_function"
        ] = ef_payload
    con.execute(
        "UPDATE collections SET schema_str=? WHERE name=?",
        (json.dumps(sch), collection),
    )
    con.commit()
    con.close()


_HOSTILE_EF = {
    "type": "known",
    "name": "sentence_transformer",
    "config": {
        "model_name": "synthetic/not-real",
        "device": "cpu",
        "normalize_embeddings": False,
        "kwargs": {"trust_remote_code": True},
    },
}


def test_fresh_db_init_ok(tmp_path):
    ChromaDBStore(persist_directory=tmp_path / "chroma")


def test_legit_default_collection_passes(tmp_path):
    _make_db_with_collection(tmp_path / "db")
    ChromaDBStore(persist_directory=tmp_path / "db")


def test_tampered_dense_ef_rejected_at_init(tmp_path):
    _make_db_with_collection(tmp_path / "db")
    _tamper_schema(tmp_path / "db", "company_docs", _HOSTILE_EF)
    with pytest.raises(PersistedEmbeddingConfigError):
        ChromaDBStore(persist_directory=tmp_path / "db")


def test_tamper_after_init_rejected_before_embed(tmp_path):
    """Post-validation on-disk tamper is caught by the per-operation
    re-validation in _get_or_create_collection — before any embed can run."""
    db = tmp_path / "db"
    _make_db_with_collection(db)
    store = ChromaDBStore(persist_directory=db)
    _tamper_schema(db, "company_docs", _HOSTILE_EF)
    with pytest.raises(PersistedEmbeddingConfigError):
        store.add_documents(
            texts=["x"], metadatas=[{}], ids=["i1"], collection="company_docs"
        )
    with pytest.raises(PersistedEmbeddingConfigError):
        store.query("x", collection="company_docs")


def test_malformed_ef_shapes_rejected():
    with pytest.raises(PersistedEmbeddingConfigError):
        _validate_persisted_ef_config("c", {"embedding_function": "not-a-dict"})
    with pytest.raises(PersistedEmbeddingConfigError):
        _validate_persisted_ef_config(
            "c", {"embedding_function": {"type": "unknown", "name": "x"}}
        )
    with pytest.raises(PersistedEmbeddingConfigError):
        _validate_persisted_ef_config(
            "c",
            {"embedding_function": {"type": "known", "name": "sentence_transformer",
                                    "config": {}}},
        )
    with pytest.raises(PersistedEmbeddingConfigError):
        _validate_persisted_ef_config(
            "c",
            {"embedding_function": {"type": "known", "name": "default",
                                    "config": {"unexpected": 1}}},
        )


def test_valid_ef_shapes_pass():
    _validate_persisted_ef_config("c", {})
    _validate_persisted_ef_config("c", {"embedding_function": None})
    _validate_persisted_ef_config("c", {"embedding_function": {"type": "legacy"}})
    _validate_persisted_ef_config(
        "c",
        {"embedding_function": {"type": "known", "name": "default", "config": {}}},
    )
    _validate_persisted_ef_config(
        "c",
        {"embedding_function": {"type": "known", "name": "onnx_mini_lm_l6_v2",
                                "config": {}}},
    )


def test_guard_runs_under_python_dash_O(tmp_path):
    """The check is an explicit raise, not an assert — it must still fire
    under `python -O` where assert statements are stripped."""
    db = tmp_path / "db"
    _make_db_with_collection(db)
    _tamper_schema(db, "company_docs", _HOSTILE_EF)
    script = (
        "from openexecutive.knowledge.store import ChromaDBStore,"
        " PersistedEmbeddingConfigError\n"
        "try:\n"
        f"    ChromaDBStore(persist_directory={str(db)!r})\n"
        "    print('NO-RAISE')\n"
        "except PersistedEmbeddingConfigError:\n"
        "    print('RAISED')\n"
    )
    out = subprocess.run(
        [sys.executable, "-O", "-c", script], capture_output=True, text=True
    )
    assert "RAISED" in out.stdout, out.stderr


def test_sparse_vector_ef_not_surfaced_documents_residual_limit(tmp_path):
    """RESIDUAL LIMIT — not a security PASS. A hostile EF injected under
    ``defaults.sparse_vector`` does not appear in ``configuration_json``,
    so this guard does not see it. BOAgents never enables sparse indexes,
    so the tampered path is unreachable without further tampering, but the
    guard itself cannot prove that. This test pins the limit so nobody
    mistakes the guard for full coverage."""
    db = tmp_path / "db"
    _make_db_with_collection(db)
    _tamper_schema(db, "company_docs", _HOSTILE_EF, key="sparse")
    import chromadb
    from chromadb.config import Settings

    col = chromadb.PersistentClient(
        path=str(db), settings=Settings(anonymized_telemetry=False)
    ).get_collection("company_docs")
    surfaced = json.dumps(col.configuration_json)
    assert "sentence_transformer" not in surfaced
    # Guard passes — the sparse payload is invisible to configuration_json.
    ChromaDBStore(persist_directory=db)
