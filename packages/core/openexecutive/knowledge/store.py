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
    def delete_documents(self, collection: str, where: dict[str, Any]) -> None: ...


class PersistedEmbeddingConfigError(RuntimeError):
    """Raised when a persisted ChromaDB collection carries an embedding
    function configuration this store never writes.

    ChromaDB 1.5.9 defers embedding-function instantiation from persisted
    collection config to the first embed call (``CollectionCommon._embed``
    → ``load_collection_configuration_from_json`` →
    ``known_embedding_functions[name].build_from_config``). A tampered
    ``schema_str`` row in ``chroma.sqlite3`` can therefore turn the next
    ``add``/``query`` into arbitrary code loading (e.g.
    ``sentence_transformer`` with ``trust_remote_code``). This check is the
    only safe pre-embed gate: ``configuration_json`` is a raw dict read
    that performs no builds, while ``collection.configuration``,
    ``collection.schema``, ``add`` and ``query`` all instantiate from the
    persisted config.
    """


# Embedding functions this store ever persists. Anything else found in a
# collection's persisted configuration is rejected before the first embed
# can build it into running code.
_ALLOWED_EF_NAMES = frozenset({"default", "onnx_mini_lm_l6_v2"})


def _validate_persisted_ef_config(
    collection_name: str, configuration_json: dict[str, Any]
) -> None:
    """Reject a persisted collection whose embedding-function config this
    store did not write. Reads only ``configuration_json`` — a raw
    ``self._model.configuration_json`` dict that builds nothing (verified
    against chromadb 1.5.9). ``ef`` shape follows upstream
    ``load_collection_configuration_from_json``: ``"legacy"`` configs are
    skipped by upstream too, ``"known"`` is built from
    ``known_embedding_functions[name]``, and any other/malformed ``type``
    reaches the same name lookup at embed time — so only the exact shapes
    this store produces are allowed.
    """
    ef = configuration_json.get("embedding_function")
    if ef is None:
        return
    if not isinstance(ef, dict):
        raise PersistedEmbeddingConfigError(
            f"knowledge collection {collection_name!r}: persisted "
            "embedding_function is not a mapping"
        )
    ef_type = ef.get("type")
    if ef_type == "legacy":
        return
    if ef_type != "known":
        raise PersistedEmbeddingConfigError(
            f"knowledge collection {collection_name!r}: persisted "
            f"embedding_function type {ef_type!r} is not 'known'"
        )
    name = ef.get("name")
    if name not in _ALLOWED_EF_NAMES:
        raise PersistedEmbeddingConfigError(
            f"knowledge collection {collection_name!r}: persisted "
            f"embedding function {name!r} is not one this store writes"
        )
    # This store's legitimate collections persist an empty EF config. Any
    # payload here (e.g. kwargs.trust_remote_code) is rejected outright.
    if ef.get("config"):
        raise PersistedEmbeddingConfigError(
            f"knowledge collection {collection_name!r}: persisted "
            "embedding_function carries a non-empty config"
        )


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
        # embed against a tampered collection config would. See
        # PersistedEmbeddingConfigError.
        for col in self._client.list_collections():
            cfg = col.configuration_json
            if not isinstance(cfg, dict):
                raise PersistedEmbeddingConfigError(
                    f"knowledge collection {col.name!r}: persisted "
                    "configuration_json is not a mapping"
                )
            _validate_persisted_ef_config(col.name, cfg)

    def _get_or_create_collection(self, name: str) -> Any:
        # Re-validate before handing out a collection handle: the DB can be
        # modified on disk after construction, and ``get_collection`` /
        # ``configuration_json`` are the probe-verified zero-build reads.
        # Only a missing collection falls through; any other failure is
        # re-raised rather than silently skipping validation.
        from chromadb.errors import NotFoundError

        try:
            existing = self._client.get_collection(name)
        except NotFoundError:
            existing = None
        if existing is not None:
            cfg = existing.configuration_json
            if not isinstance(cfg, dict):
                raise PersistedEmbeddingConfigError(
                    f"knowledge collection {name!r}: persisted "
                    "configuration_json is not a mapping"
                )
            _validate_persisted_ef_config(name, cfg)
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
            self._client.get_collection(collection)
            return True
        except Exception:
            return False

    def get_collection_count(self, collection: str) -> int:
        try:
            col = self._client.get_collection(collection)
            return col.count()
        except Exception:
            return 0

    def delete_documents(self, collection: str, where: dict[str, Any]) -> None:
        try:
            col = self._get_or_create_collection(collection)
            col.delete(where=where)
        except Exception:
            pass

    def iter_chunk_metadata(self, collection: str) -> list[tuple[str, dict[str, Any]]]:
        """Return every ``(chunk_id, metadata)`` pair in *collection*.

        Chroma's ``where`` has no prefix/substring operator, so metadata
        patterns (rather than exact matches) have to be filtered in Python.
        Only used against the small ``company_docs`` collection.
        """
        try:
            col = self._get_or_create_collection(collection)
            rows = col.get(include=["metadatas"])
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
        except Exception:
            logging.getLogger(__name__).exception(
                "get_documents_by_ids failed for %d id(s) in %s", len(ids), collection
            )
            return []
        return out

    def delete_company_docs(self) -> None:
        """Delete and recreate the company_docs collection, clearing all indexed documents."""
        import contextlib

        with contextlib.suppress(Exception):
            self._client.delete_collection(self.COMPANY_COLLECTION)
        # Recreate with the same HNSW settings so subsequent upserts work normally.
        self._get_or_create_collection(self.COMPANY_COLLECTION)

    def delete_notion_docs(self) -> None:
        """Drop synced Notion chunks from the isolated collection and any
        leftover COMPANY rows tagged ``type=notion`` (pre-isolation ingest)."""
        self.delete_documents(collection=self.NOTION_COLLECTION, where={"type": "notion"})
        self.delete_documents(collection=self.COMPANY_COLLECTION, where={"type": "notion"})

    def delete_attachment_docs(self) -> None:
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
        import contextlib

        with contextlib.suppress(Exception):
            self._client.delete_collection(self.ATTACHMENT_COLLECTION)
        self._get_or_create_collection(self.ATTACHMENT_COLLECTION)
        self.delete_documents(
            collection=self.COMPANY_COLLECTION, where={"type": "attachment"}
        )
