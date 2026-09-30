"""Central side-effect classification for executable tools.

One place decides whether a tool invocation could have produced an
externally visible (or durable mutating) effect. The classification
feeds evidence/readback surfaces (operator attribution of audit rows);
it is NOT an authorization primitive — the email poller's retry policy
no longer consults tool windows at all (a closed ``executive_failed``
attempt is ``uncertain`` by construction, because a missing
``tool_invocation`` row can never prove an effect did not happen).

This is deliberately NOT the gateway's egress roster: the egress sets
gate *recipient authorization* for a handful of Google tools, while this
inventory answers a different question — "could this call have changed
something?". A broadcast to Slack/Discord is not gated by the Gmail
roster but absolutely is an external effect.

Classification rule: a tool is read-only only when its *exact,
fully-qualified identifier* is explicitly registered below — internal
Executive tool names in ``_READ_ONLY_INTERNAL`` or attested MCP
``server__tool`` names in ``_READ_ONLY_MCP``. Verb-shaped prefixes
(``get_*``, ``list_*``, ``check_*`` …) carry NO weight: a mutator named
``acme_corp__get_inventory`` must never pass as read-only, and any
unknown or dynamically-discovered tool is conservatively effectful.
"""
from __future__ import annotations

# Internal (non-MCP) Executive tools that produce no durable mutation and
# no outbound dispatch. Delegation/consultation rows are safe because a
# specialist's own tool calls are journaled as separate tool_invocation
# rows — they get classified individually.
_READ_ONLY_INTERNAL: frozenset[str] = frozenset({
    "consult_specialist",
    "web_search",
    "search_tools",
    "list_people",
    "lookup_person",
    "ask_about_person",
    "list_watchlist",
    "list_department_goals",
    "list_workflows",
    "search_skills",
})

# MCP tools arrive namespaced as ``{server}__{tool}``. Only these exact,
# attested identifiers are read-only — the workspace-mcp Gmail read
# surface the poller itself drives (contract verified on workspace-mcp
# 1.21.1). A *name alone* never qualifies a tool: no verb-prefix
# inference, no substring rules, no per-server wildcard. Every other
# namespaced tool — drafts, sends, shares, label mutations, and anything
# discovered dynamically — is effectful by construction.
_READ_ONLY_MCP: frozenset[str] = frozenset({
    "google_workspace__search_gmail_messages",
    "google_workspace__get_gmail_message_content",
    "google_workspace__get_gmail_attachment_content",
    "google_workspace__list_gmail_labels",
})


def is_read_only_tool(tool_name: str) -> bool:
    """True only for explicitly registered read-only tools.
    Unknown or dynamic names → False."""
    name = (tool_name or "").strip()
    if not name:
        return False
    return name in _READ_ONLY_INTERNAL or name in _READ_ONLY_MCP


def has_external_effect(tool_name: str) -> bool:
    """Conservative inverse: anything not provably read-only could have
    produced an external or durable effect."""
    return not is_read_only_tool(tool_name)
