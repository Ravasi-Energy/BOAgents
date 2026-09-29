"""SEC-03/04: guard de integritate pe schema persistată chroma_db.

ChromaDB 1.5.9 instanțiază embedding function din schema persistată la
deserializare (``Schema.deserialize_from_json`` →
``_deserialize_*_value_type`` → ``known_embedding_functions[name]
.build_from_config``) — atinsă de ``collection.schema``,
``collection.configuration``, ``add`` și ``query``. Un ``schema_str``
alterat în ``chroma.sqlite3`` (defaults, keys.#embedding sau orice
``sparse_vector_index``) devine încărcare de cod la primul embed.

Guard-ul inspectează ``col._model.serialized_schema`` — citire probată
fără build — și respinge orice nod ``embedding_function`` diferit de
formele pe care store-ul le scrie, fail-closed pe structură necunoscută.

Zero payload executat, zero descărcare: crearea colecțiilor legitime
serializează EF-ul default fără rețea (download ONNX doar la __call__).
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
    _validate_ef_node,
)


def _make_db_with_collection(path: Path, name: str = "company_docs") -> None:
    import chromadb
    from chromadb.config import Settings

    # Same settings as ChromaDBStore — SharedSystemClient caches per path
    # and refuses a second client with different settings.
    chromadb.PersistentClient(
        path=str(path), settings=Settings(anonymized_telemetry=False)
    ).get_or_create_collection(name=name)


def _tamper_schema(path: Path, collection: str, ef_payload: dict, where: str) -> None:
    """Inject an embedding_function node into persisted schema_str.

    where="dense"   → keys.#embedding.float_list.vector_index
    where="sparse"  → keys.sparse_probe.sparse_vector.sparse_vector_index
    where="defaults"→ defaults.float_list.vector_index (F3)
    """
    db = path / "chroma.sqlite3"
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT schema_str FROM collections WHERE name=?", (collection,)
    ).fetchone()
    sch = json.loads(row[0])
    if where == "dense":
        sch["keys"]["#embedding"]["float_list"]["vector_index"]["config"][
            "embedding_function"
        ] = ef_payload
    elif where == "sparse":
        sch["keys"].setdefault("sparse_probe", {}).setdefault(
            "sparse_vector", {}
        )["sparse_vector_index"] = {
            "enabled": True,
            "config": {"embedding_function": ef_payload},
        }
    elif where == "defaults":
        sch["defaults"]["float_list"]["vector_index"]["config"][
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
_HOSTILE_SPARSE_EF = {
    "type": "known",
    "name": "huggingface_sparse",
    "config": {"kwargs": {"trust_remote_code": True}},
}
_UNKNOWN_WITH_NAME = {
    "type": "unknown",
    "name": "sentence_transformer",
    "config": {"kwargs": {"trust_remote_code": True}},
}


def test_fresh_db_init_ok(tmp_path):
    ChromaDBStore(persist_directory=tmp_path / "chroma")


def test_legit_default_collection_passes(tmp_path):
    _make_db_with_collection(tmp_path / "db")
    ChromaDBStore(persist_directory=tmp_path / "db")


def test_f1_dense_tamper_rejected_at_init(tmp_path):
    _make_db_with_collection(tmp_path / "db")
    _tamper_schema(tmp_path / "db", "company_docs", _HOSTILE_EF, "dense")
    with pytest.raises(PersistedEmbeddingConfigError):
        ChromaDBStore(persist_directory=tmp_path / "db")


def test_f2_sparse_tamper_rejected_at_init(tmp_path):
    """F2: hostile EF under a keys.* sparse_vector_index — invisible to
    configuration_json, must now be caught via serialized_schema."""
    _make_db_with_collection(tmp_path / "db")
    _tamper_schema(tmp_path / "db", "company_docs", _HOSTILE_SPARSE_EF, "sparse")
    with pytest.raises(PersistedEmbeddingConfigError):
        ChromaDBStore(persist_directory=tmp_path / "db")


def test_f3_defaults_tamper_rejected_at_init(tmp_path):
    """F3: hostile EF under defaults.float_list.vector_index — built at
    schema deserialize even when the real embed uses 'default'."""
    _make_db_with_collection(tmp_path / "db")
    _tamper_schema(tmp_path / "db", "company_docs", _HOSTILE_EF, "defaults")
    with pytest.raises(PersistedEmbeddingConfigError):
        ChromaDBStore(persist_directory=tmp_path / "db")


def test_unknown_type_node_is_stripped_by_rust_projection(tmp_path):
    """Observed upstream behavior, not a guard pass-through: the Rust
    backend strips name/config from ``{"type": "unknown"}`` nodes, so a
    tampered unknown node arrives sanitized as ``{"type": "unknown"}``
    and never reaches ``build_from_config``. Init passes BECAUSE the
    projected node is inert — the node-level rejection of the un-sanitized
    shape is covered in test_malformed_ef_nodes_rejected."""
    import chromadb
    from chromadb.config import Settings

    db = tmp_path / "db"
    _make_db_with_collection(db)
    _tamper_schema(db, "company_docs", _UNKNOWN_WITH_NAME, "dense")
    col = chromadb.PersistentClient(
        path=str(db), settings=Settings(anonymized_telemetry=False)
    ).get_collection("company_docs")
    node = col._model.serialized_schema["keys"]["#embedding"]["float_list"][
        "vector_index"
    ]["config"]["embedding_function"]
    assert node == {"type": "unknown"}  # rust stripped name+config
    ChromaDBStore(persist_directory=db)


def test_tamper_after_init_rejected_before_embed(tmp_path):
    """Post-validation on-disk tamper is caught by the per-operation
    re-validation in _get_or_create_collection — before any embed."""
    db = tmp_path / "db"
    _make_db_with_collection(db)
    store = ChromaDBStore(persist_directory=db)
    _tamper_schema(db, "company_docs", _HOSTILE_EF, "defaults")
    with pytest.raises(PersistedEmbeddingConfigError):
        store.add_documents(
            texts=["x"], metadatas=[{}], ids=["i1"], collection="company_docs"
        )
    with pytest.raises(PersistedEmbeddingConfigError):
        store.query("x", collection="company_docs")


def test_tampered_second_collection_blocks_init(tmp_path):
    import chromadb
    from chromadb.config import Settings

    db = tmp_path / "db"
    client = chromadb.PersistentClient(
        path=str(db), settings=Settings(anonymized_telemetry=False)
    )
    client.get_or_create_collection(name="company_docs")
    client.get_or_create_collection(name="other")
    _tamper_schema(db, "other", _HOSTILE_EF, "defaults")
    with pytest.raises(PersistedEmbeddingConfigError):
        ChromaDBStore(persist_directory=db)


def test_malformed_ef_nodes_rejected():
    with pytest.raises(PersistedEmbeddingConfigError):
        _validate_ef_node("c", ("x", "embedding_function"), "not-a-dict")
    with pytest.raises(PersistedEmbeddingConfigError):
        _validate_ef_node("c", ("x", "embedding_function"),
                          {"type": "weird"})
    with pytest.raises(PersistedEmbeddingConfigError):
        _validate_ef_node("c", ("x", "embedding_function"), _HOSTILE_EF)
    with pytest.raises(PersistedEmbeddingConfigError):
        _validate_ef_node("c", ("x", "embedding_function"), _UNKNOWN_WITH_NAME)
    with pytest.raises(PersistedEmbeddingConfigError):
        _validate_ef_node(
            "c", ("x", "embedding_function"),
            {"type": "known", "name": "default", "config": {"unexpected": 1}},
        )


def test_valid_ef_nodes_pass():
    _validate_ef_node("c", ("k", "embedding_function"), None)
    _validate_ef_node("c", ("k", "embedding_function"), {"type": "legacy"})
    _validate_ef_node("c", ("k", "embedding_function"), {"type": "unknown"})
    _validate_ef_node(
        "c", ("k", "embedding_function"),
        {"type": "known", "name": "default", "config": {}},
    )
    _validate_ef_node(
        "c", ("k", "embedding_function"),
        {"type": "known", "name": "onnx_mini_lm_l6_v2", "config": {}},
    )


def test_guard_runs_under_python_dash_O(tmp_path):
    """The check is explicit raises, not asserts — still fires under -O."""
    db = tmp_path / "db"
    _make_db_with_collection(db)
    _tamper_schema(db, "company_docs", _HOSTILE_EF, "sparse")
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


def test_missing_serialized_schema_fails_closed(tmp_path):
    """If the private _model/serialized_schema interface disappears or
    changes shape, the guard raises instead of silently skipping."""
    _validate_ef_node("c", ("k", "embedding_function"), None)  # sanity
    import chromadb
    from chromadb.config import Settings

    db = tmp_path / "db"
    _make_db_with_collection(db)
    client = chromadb.PersistentClient(
        path=str(db), settings=Settings(anonymized_telemetry=False)
    )
    col = client.get_collection("company_docs")

    from openexecutive.knowledge.store import _validate_collection_schema

    class _FakeCol:
        name = "company_docs"
        _model = None

    with pytest.raises(PersistedEmbeddingConfigError):
        _validate_collection_schema(_FakeCol())
    assert isinstance(col._model.serialized_schema, dict)


def _insert_marker(path: Path, collection: str = "recent_research") -> None:
    """Insert a marker row with explicit embeddings — no EF build, no download."""
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=str(path), settings=Settings(anonymized_telemetry=False)
    )
    col = client.get_or_create_collection(name=collection)
    col.upsert(
        ids=["marker-a"],
        documents=["client A secret marker"],
        metadatas=[{"type": "recent_research"}],
        embeddings=[[0.1] * 4],
    )


def _marker_present(path: Path, collection: str = "recent_research") -> bool:
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=str(path), settings=Settings(anonymized_telemetry=False)
    )
    rows = client.get_collection(collection).get(ids=["marker-a"])
    return bool(rows.get("ids"))


def test_delete_documents_propagates_integrity_refusal(tmp_path):
    """SEC-08: a tampered swept collection must ABORT cleanup, not pass for empty."""
    db = tmp_path / "db"
    _insert_marker(db)
    store = ChromaDBStore(persist_directory=db)  # preflight on clean schema
    _tamper_schema(db, "recent_research", _HOSTILE_EF, "dense")

    with pytest.raises(PersistedEmbeddingConfigError):
        store.delete_documents("recent_research", {"type": "recent_research"})

    # Refusal did not clean: the row provably survives until a repaired
    # volume lets cleanup actually run — refusal ≠ empty.
    import json as _json
    import sqlite3 as _sq

    con = _sq.connect(db / "chroma.sqlite3")
    sch = _json.loads(
        con.execute(
            "SELECT schema_str FROM collections WHERE name=?", ("recent_research",)
        ).fetchone()[0]
    )
    sch["keys"]["#embedding"]["float_list"]["vector_index"]["config"][
        "embedding_function"
    ] = {"type": "known", "name": "default", "config": {}}
    con.execute(
        "UPDATE collections SET schema_str=? WHERE name=?",
        (_json.dumps(sch), "recent_research"),
    )
    con.commit()
    con.close()
    repaired = ChromaDBStore(persist_directory=db)
    assert _marker_present(db)
    repaired.delete_documents("recent_research", {"type": "recent_research"})
    assert not _marker_present(db)


def test_drop_and_recreate_validates_before_drop(tmp_path):
    """A tampered company_docs must not be silently laundered by drop+recreate."""
    db = tmp_path / "db"
    _insert_marker(db, collection="company_docs")
    store = ChromaDBStore(persist_directory=db)
    _tamper_schema(db, "company_docs", _HOSTILE_EF, "dense")

    with pytest.raises(PersistedEmbeddingConfigError):
        store.delete_company_docs()

    # The collection was NOT dropped — the refusal stayed loud.
    assert _marker_present(db, collection="company_docs")


def test_collection_read_helpers_propagate_refusal(tmp_path):
    """Absent ≠ refused: read helpers must not launder tamper into 0/False/[]."""
    db = tmp_path / "db"
    _insert_marker(db)
    store = ChromaDBStore(persist_directory=db)
    _tamper_schema(db, "recent_research", _HOSTILE_EF, "dense")

    with pytest.raises(PersistedEmbeddingConfigError):
        store.collection_exists("recent_research")
    with pytest.raises(PersistedEmbeddingConfigError):
        store.get_collection_count("recent_research")
    with pytest.raises(PersistedEmbeddingConfigError):
        store.iter_chunk_metadata("recent_research")
    with pytest.raises(PersistedEmbeddingConfigError):
        store.delete_by_ids("recent_research", ["marker-a"])
    with pytest.raises(PersistedEmbeddingConfigError):
        store.get_documents_by_ids("recent_research", ["marker-a"])


def test_legit_cleanup_and_absent_collection_unaffected(tmp_path):
    db = tmp_path / "db"
    _insert_marker(db)
    store = ChromaDBStore(persist_directory=db)

    # Contract tolerance preserved: absent collection is fine. Note
    # delete_documents creates a missing collection (get-or-create) —
    # pre-existing behavior, kept verbatim.
    store.delete_documents("never_existed", {"type": "x"})
    assert store.collection_exists("never_existed")
    assert store.get_collection_count("never_existed") == 0
    store.delete_documents("still_absent", {"type": "x"})  # no raise
    assert store.iter_chunk_metadata("still_absent") == []
    assert store.get_documents_by_ids("still_absent", ["nope"]) == []

    # Real cleanup still works on a healthy schema.
    store.delete_documents("recent_research", {"type": "recent_research"})
    assert not _marker_present(db)
    _insert_marker(db)
    store.delete_by_ids("recent_research", ["marker-a"])
    assert not _marker_present(db)
    store.delete_company_docs()  # drop+recreate on healthy schema still fine
    assert store.collection_exists("company_docs")
