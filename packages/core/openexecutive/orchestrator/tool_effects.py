"""Central side-effect classification for executable tools.

One place decides whether a tool invocation could have produced an
externally visible (or durable mutating) effect. The email poller's
duplicate-effect guard relies on it: a retry may only run when every
prior tool call in the attempt window is provably read-only — anything
else (mutators, broadcast/department dispatch, skills, and any unknown
dynamic tool name) is treated conservatively as effectful.

This is deliberately NOT the gateway's egress roster: the egress sets
gate *recipient authorization* for a handful of Google tools, while this
inventory answers a different question — "could this call have changed
something?". A broadcast to Slack/Discord is not gated by the Gmail
roster but absolutely is an external effect.

Classification rule: a tool is read-only only if it is on the explicit
allowlist or its server-prefixed MCP name carries a universally
read-shaped verb (``search_*``, ``get_*``, ``list_*``, ``read_*``,
``fetch_*``, ``download_*``, ``check_*``). Everything else — every
``send_*``, ``create_*``, ``manage_*``, ``delete_*``, ``update_*``,
``upsert_*``, ``schedule_*``, ``run_*``, ``archive_*``, ``ack_*``,
broadcast, and any unknown name — is treated as effectful. Unknown is
never assumed safe.
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

# MCP tools arrive namespaced as ``{server}__{verb}_{rest}``. Only these
# verb stems are provably read-shaped on the workspace-mcp surface; a
# stem outside the list is NOT assumed safe (drafts, shares and label
# mutations are effects too).
_READ_ONLY_MCP_VERBS: tuple[str, ...] = (
    "search_",
    "get_",
    "list_",
    "read_",
    "fetch_",
    "download_",
    "check_",
)


def is_read_only_tool(tool_name: str) -> bool:
    """True only for provably read-only tools. Unknown → False."""
    name = (tool_name or "").strip()
    if not name:
        return False
    if name in _READ_ONLY_INTERNAL:
        return True
    if "__" in name:
        _, _, tool = name.partition("__")
        return any(tool.startswith(v) for v in _READ_ONLY_MCP_VERBS)
    return False


def has_external_effect(tool_name: str) -> bool:
    """Conservative inverse: anything not provably read-only could have
    produced an external or durable effect."""
    return not is_read_only_tool(tool_name)
