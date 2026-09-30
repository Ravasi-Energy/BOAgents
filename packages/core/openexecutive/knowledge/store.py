from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class KnowledgeStore(ABC):
    @abstractmethod
    def add_documents(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
        collection: str,
    ) -> None: ...

    @abstractmethod
    def query(
        self,
        query_text: str,
        collection: str,
        domain_filter: list[str] | None = None,
        n_results: int = 5,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def collection_exists(self, collection: str) -> bool: ...

    @abstractmethod
    def get_collection_count(self, collection: str) -> int: ...

    @abstractmethod
    def delete_documents(
        self, collection: str, where: dict[str, Any], *, strict: bool = False
    ) -> None: ...


class PersistedEmbeddingConfigError(RuntimeError):
    """Raised when a persisted ChromaDB collection carries an embedding
    function configuration this store never writes.

    ChromaDB 1.5.9 defers embedding-function instantiation from persisted
    collection schema to embed/schema access: ``_embed`` and
    ``_get_sparse_embedding_targets`` reach ``self.schema`` /
    ``self.configuration``, whose deserialization calls
    ``known_embedding_functions[name].build_from_config`` for every
    ``embedding_function`` node (dense ``vector_index`` AND sparse /
    ``defaults`` keys — including nodes the projected
    ``configuration_json`` never surfaces). A tampered ``schema_str`` row
    in ``chroma.sqlite3`` can therefore turn the next ``add``/``query``
    into arbitrary code loading (e.g. ``sentence_transformer`` or
    ``huggingface_sparse`` with ``trust_remote_code``).

    The pre-embed gate must inspect ``col._model.serialized_schema``: the
    raw schema dict the Rust backend returns, read with zero builds
    (probe-verified). ``collection.configuration``, ``collection.schema``,
    ``add`` and ``query`` all deserialize/build — they are the effectful
    side, never the inspection side.
    """


# Embedding functions this store ever persists in a collection schema.
# Anything else found at any embedding_function node is rejected before
# the first schema deserialize can build it into running code.
_ALLOWED_EF_NAMES = frozenset({"default", "onnx_mini_lm_l6_v2"})


def _iter_ef_nodes(node: Any, path: tuple[str, ...] = ()) -> Any:
    """Yield ``(json_path, ef_node)`` for every ``embedding_function`` key
    at any depth of the serialized schema — ``defaults`` and all
    ``keys.*`` included, so no schema branch hides a node."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "embedding_function":
                yield path + (key,), value
            else:
                yield from _iter_ef_nodes(value, path + (key,))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _iter_ef_nodes(item, path + (str(i),))


def _validate_ef_node(collection_name: str, path: tuple[str, ...], ef: Any) -> None:
    """Validate one persisted ``embedding_function`` node with exact
    upstream semantics (chromadb 1.5.9 ``_deserialize_*_value_type``):

    - ``None`` / ``{"type": "legacy"}`` / ``{"type": "unknown"}`` without
      a name never reach ``build_from_config`` — allowed.
    - ``{"type": "known", ...}`` builds at deserialize; only
      ``default``/``onnx_mini_lm_l6_v2`` with an empty config are shapes
      this store writes.
    - A ``"known"``-shaped node under a dense ``vector_index`` builds
      even when typed ``"unknown"`` (the dense branch only skips
      ``"legacy"``), so any node carrying a registered ``name`` outside
      the whitelist is rejected regardless of its declared ``type``.
    """
    where = f"{collection_name!r} schema {'.'.join(path)}"
    if ef is None:
        return
    if not isinstance(ef, dict):
        raise PersistedEmbeddingConfigError(
            f"knowledge collection {where}: persisted embedding_function "
            "is not a mapping"
        )
    ef_type = ef.get("type")
    name = ef.get("name")
    if name is not None and name not in _ALLOWED_EF_NAMES:
        raise PersistedEmbeddingConfigError(
            f"knowledge collection {where}: persisted embedding function "
            f"{name!r} is not one this store writes"
        )
    if ef_type == "legacy":
        return
    if ef_type == "unknown":
        # Upstream treats unknown as None only in the sparse branch; a
        # named node can still build on the dense path — reject any
        # unknown node that carries a name or a config payload.
        if name is not None or ef.get("config"):
            raise PersistedEmbeddingConfigError(
                f"knowledge collection {where}: persisted "
                "embedding_function 'unknown' node carries name/config"
            )
        return
    if ef_type != "known":
        raise PersistedEmbeddingConfigError(
            f"knowledge collection {where}: persisted "
            f"embedding_function type {ef_type!r} is not 'known'"
        )
    if name not in _ALLOWED_EF_NAMES:
        raise PersistedEmbeddingConfigError(
            f"knowledge collection {where}: persisted "
            f"embedding_function 'known' node has no name"
        )
    if ef.get("config"):
        raise PersistedEmbeddingConfigError(
            f"knowledge collection {where}: persisted "
            "embedding_function carries a non-empty config"
        )


def _serialized_schema(collection: Any) -> dict[str, Any]:
    """Return the raw serialized schema — the zero-build read.

    ``_model`` is chromadb-internal; if a future chromadb renames the
    attribute or changes the shape, this fails closed instead of silently
    skipping validation.
    """
    model = getattr(collection, "_model", None)
    schema = getattr(model, "serialized_schema", None)
    if not isinstance(schema, dict):
        raise PersistedEmbeddingConfigError(
            f"knowledge collection {getattr(collection, 'name', '?')!r}: "
            "cannot inspect persisted schema — failing closed"
        )
    return schema


def _validate_collection_schema(collection: Any) -> None:
    name = getattr(collection, "name", "?")
    for path, ef in _iter_ef_nodes(_serialized_schema(collection)):
        _validate_ef_node(name, path, ef)


class ChromaDBStore(KnowledgeStore):
    BUILTIN_COLLECTION = "builtin_knowledge"
    COMPANY_COLLECTION = "company_docs"
    FAILURES_COLLECTION = "failure_cases"
    # Web-research artifacts persisted from executive_research runs. Kept
    # SEPARATE from COMPANY_COLLECTION so unvetted, machine-generated
    # research never blends into curated company knowledge — it is
    # retrieved under its own clearly-labelled, lower-ranked section.
    RESEARCH_COLLECTION = "recent_research"
    # Synced Notion wiki pages. Kept SEPARATE from COMPANY_COLLECTION
    # because a Notion share is multi-writer and unreviewed — anyone
    # who can edit a shared page can inject text the agents will read.
    # Retrieved under its own clearly-labelled, lower-ranked section.
    NOTION_COLLECTION = "notion_wiki"
    # Files attached in an integration channel. Same isolation reasoning as
    # the two above, taken one step further: this collection is NEVER
    # queried — not by ``retriever.retrieve``, not by anything else.
    #
    # An attachment's content is chosen by whoever sent the message. It
    # passes through no upload endpoint, gets no review, and leaves no copy
    # in ``company/docs/``. It is already inlined into the turn that carried
    # it, which is where it is useful; what it must not become is company
    # knowledge that resurfaces in later, unrelated turns.
    #
    # These rows used to live in COMPANY_COLLECTION, isolated only by a
    # domain outside the specialist set. That isolated nothing — see
    # ``query`` below, and knowledge.general_catch_all in
    # architecture-facts.yaml. The collection is the boundary; a domain
    # value never was.
    ATTACHMENT_COLLECTION = "inbound_attachments"

    def __init__(self, persist_directory: str | Path = "./chroma_db") -> None:
        import chromadb
        from chromadb.config import Settings

        self._client = chromadb.PersistentClient(
            path=str(persist_directory),
            settings=Settings(anonymized_telemetry=False),
        )
        # Validate every already-persisted collection at construction:
        # opening a DB performs no embedding-function builds, but the first
        # embed/schema access against a tampered schema_str would. See
        # PersistedEmbeddingConfigError.
        for col in self._client.list_collections():
            _validate_collection_schema(col)

    def _get_or_create_collection(self, name: str) -> Any:
        # Re-validate before handing out a collection handle: the DB can be
        # modified on disk after construction, and ``get_collection`` /
        # ``_model.serialized_schema`` are the probe-verified zero-build
        # reads. Only a missing collection falls through; any other failure
        # is re-raised rather than silently skipping validation.
        from chromadb.errors import NotFoundError

        try:
            existing = self._client.get_collection(name)
        except NotFoundError:
            existing = None
        if existing is not None:
            _validate_collection_schema(existing)
        return self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    def add_documents(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
        collection: str = BUILTIN_COLLECTION,
    ) -> None:
        col = self._get_or_create_collection(collection)
        batch_size = 100
        for i in range(0, len(texts), batch_size):
            col.upsert(
                documents=texts[i : i + batch_size],
                metadatas=metadatas[i : i + batch_size],
                ids=ids[i : i + batch_size],
            )

    def query(
        self,
        query_text: str,
        collection: str = BUILTIN_COLLECTION,
        domain_filter: list[str] | None = None,
        n_results: int = 5,
    ) -> list[dict[str, Any]]:
        col = self._get_or_create_collection(collection)

        count = col.count()
        if count == 0:
            return []

        where: dict[str, Any] | None = None
        if domain_filter:
            if len(domain_filter) == 1:
                where = {"domain": domain_filter[0]}
            else:
                where = {"domain": {"$in": domain_filter}}

        query_kwargs: dict[str, Any] = {
            "query_texts": [query_text],
            "n_results": min(n_results, count),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            query_kwargs["where"] = where

        results = col.query(**query_kwargs)

        output = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
                strict=False,
            ):
                output.append({"text": doc, "metadata": meta, "distance": dist})
        return output

    def collection_exists(self, collection: str) -> bool:
        try:
            col = self._client.get_collection(collection)
            _validate_collection_schema(col)
            return True
        except PersistedEmbeddingConfigError:
            raise
        except Exception:
            return False

    def get_collection_count(self, collection: str) -> int:
        try:
            col = self._client.get_collection(collection)
            _validate_collection_schema(col)
            return col.count()
        except PersistedEmbeddingConfigError:
            raise
        except Exception:
            return 0

    def delete_documents(
        self, collection: str, where: dict[str, Any], *, strict: bool = False
    ) -> None:
        """Delete rows matching ``where``.

        ``strict`` is for client/factory-switch cleanup callers: a failed
        delete there leaves one company's rows readable under the next
        company's identity, so ANY failure — not just the persisted-schema
        refusal — must propagate and abort the transition (the caller's
        marker/recovery machinery then decides whether the live state is
        coherent). Runtime callers (per-page sync deletes, route deletes)
        keep the default tolerant mode: a failed delete is logged and the
        operation reports what it actually did.
        """
        try:
            col = self._get_or_create_collection(collection)
            col.delete(where=where)
        except PersistedEmbeddingConfigError:
            # Integrity refusal is not a cleanup miss — it must abort the
            # caller, not masquerade as an empty/absent collection. A
            # swallowed refusal here is exactly how client A's rows used to
            # survive a slot switch into client B's hands.
            raise
        except Exception:
            if strict:
                raise
            logging.getLogger(__name__).warning(
                "delete_documents failed for %s (where=%s)",
                collection, where, exc_info=True,
            )

    def iter_chunk_metadata(self, collection: str) -> list[tuple[str, dict[str, Any]]]:
        """Return every ``(chunk_id, metadata)`` pair in *collection*.

        Chroma's ``where`` has no prefix/substring operator, so metadata
        patterns (rather than exact matches) have to be filtered in Python.
        Only used against the small ``company_docs`` collection.
        """
        try:
            col = self._get_or_create_collection(collection)
            rows = col.get(include=["metadatas"])
        except PersistedEmbeddingConfigError:
            raise
        except Exception:
            return []
        ids = rows.get("ids") or []
        metas = rows.get("metadatas") or []
        return [
            (str(cid), dict(md) if isinstance(md, dict) else {})
            for cid, md in zip(ids, metas, strict=False)
        ]

    def delete_by_ids(self, collection: str, ids: list[str]) -> int:
        """Delete specific chunk ids; returns how many were actually deleted.

        No-op on an empty list — Chroma treats a delete with neither ids nor
        where as 'delete everything', so the guard must come before the call,
        not inside it.

        Returns 0 rather than ``len(ids)`` when the delete raises, so a caller
        reporting the count cannot claim to have removed rows that are still
        there.
        """
        if not ids:
            return 0
        try:
            col = self._get_or_create_collection(collection)
            col.delete(ids=ids)
            return len(ids)
        except PersistedEmbeddingConfigError:
            raise
        except Exception:
            logging.getLogger(__name__).exception(
                "delete_by_ids failed for %d id(s) in %s", len(ids), collection
            )
            return 0

    def get_documents_by_ids(
        self, collection: str, ids: list[str]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Return ``(chunk_id, text, metadata)`` for each id that exists.

        The counterpart to :meth:`iter_chunk_metadata`, which deliberately
        fetches metadata only: that scan runs on every boot, and pulling the
        text of every chunk along with it would cost the full collection in
        memory each time to find nothing in the steady state. Callers that
        need the text find candidate ids with the cheap scan first, then ask
        for those ids here.

        Empty list in, empty list out — guarded before the call, because
        ``col.get(ids=[])`` degrades to "fetch everything", the same footgun
        :meth:`delete_by_ids` guards against.

        Batched for the same reason ``add_documents`` batches: a channel that
        has been collecting attachments for months holds more rows than one
        request should materialise at once.
        """
        if not ids:
            return []
        out: list[tuple[str, str, dict[str, Any]]] = []
        batch_size = 100
        try:
            col = self._get_or_create_collection(collection)
            for i in range(0, len(ids), batch_size):
                rows = col.get(
                    ids=ids[i : i + batch_size], include=["documents", "metadatas"]
                )
                got_ids = rows.get("ids") or []
                docs = rows.get("documents") or []
                metas = rows.get("metadatas") or []
                for cid, doc, md in zip(got_ids, docs, metas, strict=False):
                    out.append(
                        (
                            str(cid),
                            str(doc) if doc is not None else "",
                            dict(md) if isinstance(md, dict) else {},
                        )
                    )
        except PersistedEmbeddingConfigError:
            raise
        except Exception:
            logging.getLogger(__name__).exception(
                "get_documents_by_ids failed for %d id(s) in %s", len(ids), collection
            )
            return []
        return out

    def _drop_and_recreate(self, name: str) -> None:
        """Validate the persisted schema, then drop + recreate the collection.

        The guard runs BEFORE the drop on purpose: silently deleting a
        tampered collection would launder an integrity refusal into a clean
        success, hiding the incident. Refusal propagates instead.
        """
        from chromadb.errors import NotFoundError

        try:
            existing = self._client.get_collection(name)
        except NotFoundError:
            existing = None
        if existing is not None:
            _validate_collection_schema(existing)
            self._client.delete_collection(name)
        self._get_or_create_collection(name)

    def delete_company_docs(self) -> None:
        """Delete and recreate the company_docs collection, clearing all indexed documents."""
        self._drop_and_recreate(self.COMPANY_COLLECTION)

    def delete_notion_docs(self, *, strict: bool = False) -> None:
        """Drop synced Notion chunks from the isolated collection and any
        leftover COMPANY rows tagged ``type=notion`` (pre-isolation ingest)."""
        self.delete_documents(
            collection=self.NOTION_COLLECTION, where={"type": "notion"}, strict=strict
        )
        self.delete_documents(
            collection=self.COMPANY_COLLECTION, where={"type": "notion"}, strict=strict
        )

    def delete_attachment_docs(self, *, strict: bool = False) -> None:
        """Drop every inbound attachment chunk, plus any pre-isolation
        leftovers still tagged ``type=attachment`` in COMPANY.

        Drop-and-recreate rather than a ``where`` delete, unlike
        :meth:`delete_notion_docs`: the callers are client-slot restore and
        fixture reset, where one surviving row is one company's attachment
        visible to the next. "Deleted every row carrying the tag" is not the
        same guarantee as "the collection is empty", and here only the
        second one is good enough.

        The COMPANY sweep is belt-and-braces — every caller drops that
        collection wholesale a line or two earlier — but it keeps this
        method correct when called on its own.
        """
        self._drop_and_recreate(self.ATTACHMENT_COLLECTION)
        self.delete_documents(
            collection=self.COMPANY_COLLECTION,
            where={"type": "attachment"},
            strict=strict,
        )
