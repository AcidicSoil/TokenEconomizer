"""Read-only ABPT corpus import into isolated, rebuildable historical learning.

ABPT owns source data. Each project has a private, versioned derived snapshot.
The manifest pointer moves only after an entire replay succeeds.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from ..loopguard import state_from_trajectory
from ..microskills import GENERIC
from ..microskills.envfacts import EnvFactsDistiller
from ..trace.abpt import (
    archive_revision,
    convert_conversation,
    iter_conversations,
    open_archive,
    read_mapping,
)
from .paths import repo_identity, state_root
from .store import Store
from .workflow_reviews import (
    find_approved,
    load_reviews,
    query_terms,
    review_status,
    reviewed_candidate,
    store_decision,
    validate_review,
)
from .workflows import build_candidates, extract_episodes

SNIPPET_LENGTH = 800
CORPUS_SCHEMA_VERSION = 2


def _key(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _scope(project_id, env=None):
    return Store(Path(state_root(env)) / "corpus" / _key(project_id))


def _snapshot_dir(store, manifest):
    return Path(store.path("revisions", manifest["revision_dir"]))


def _source_meta(database, project_id, repo, limit, data_root, connection):
    if repo is not None:
        root = Path(repo).resolve()
        if not (root / ".git").exists():
            raise ValueError("repository binding requires an existing git checkout")
        repo_binding = {"path": str(root), "key": repo_identity(str(root)).key}
    else:
        repo_binding = None
    return {
        "source": str(Path(database).resolve()),
        "data_root": str(Path(data_root).resolve()),
        "project_id": project_id,
        "repo": repo_binding,
        "limit": limit,
        "journal": archive_revision(connection),
        "schema_version": CORPUS_SCHEMA_VERSION,
    }


def _source_equal(left, right):
    return all(
        left.get(k) == right.get(k)
        for k in (
            "source",
            "data_root",
            "project_id",
            "repo",
            "limit",
            "journal",
            "schema_version",
        )
    )


def _index(connection):
    connection.execute(
        "create virtual table snippets using fts5("
        "conversation_id unindexed, message_id unindexed, "
        "role unindexed, content, evidence_ref unindexed)"
    )


def _add_snippet(connection, conversation_id, message_id, role, content, evidence_refs):
    if not content or not content.strip():
        return
    source = (evidence_refs or [{}])[0].get("relativePath")
    connection.execute(
        "insert into snippets values (?, ?, ?, ?, ?)",
        (conversation_id, message_id, role, content[:SNIPPET_LENGTH], source),
    )


def _learn_project(source, output, limit, data_root, database):
    """Replay complete conversations oldest-first against the existing free learner."""
    from collections import Counter

    learner = EnvFactsDistiller("abpt:" + _key(source["project_id"]), vocab=GENERIC)
    counts = Counter()
    fact_sources = {}
    episodes = []
    index = sqlite3.connect(output / "evidence.db")
    _index(index)
    with tempfile.TemporaryDirectory(prefix="te-corpus-trace-") as scratch:
        trace = Path(scratch) / "transcript.jsonl"
        for conversation, messages in iter_conversations(
            database, source["project_id"], limit
        ):
            if conversation.get("captureState") != "complete":
                counts["incomplete"] += 1
                continue
            events = convert_conversation(
                conversation, messages, read_mapping(conversation, data_root)
            )
            with trace.open("w", encoding="utf-8") as stream:
                stream.writelines(
                    json.dumps(event, ensure_ascii=False) + "\n" for event in events
                )
            learner.update(str(trace), iid=conversation["id"])
            episodes.extend(extract_episodes(events))
            verified_events = [
                event
                for event in events
                if event["type"] == "abpt_tool_call"
                and event["outcome"].startswith("verified_")
            ]
            for fid, verdict in (learner.last_digest or {}).get("verdicts", {}).items():
                if verdict[0] == "confirm" and verified_events:
                    records = fact_sources.setdefault(fid, [])
                    if len(records) < 5:
                        records.append(
                            {
                                "conversation_id": conversation["id"],
                                "message_ids": [
                                    e["message_id"] for e in verified_events
                                ],
                            }
                        )
            signals = state_from_trajectory(str(trace)).snapshot()
            counts["sessions"] += 1
            counts["error_outputs"] += signals["error_outputs"]
            for event in events:
                typ = event["type"]
                if typ == "abpt_tool_call":
                    counts["tool_calls"] += 1
                    counts["paired_calls"] += event["result_id"] is not None
                    counts[f"{event['outcome']}_calls"] += 1
                    if event["outcome"].startswith("verified_"):
                        counts["verified_calls"] += 1
                    if event["result_id"] is not None:
                        _add_snippet(
                            index,
                            conversation["id"],
                            event["message_id"],
                            "tool_observation",
                            (event["tool_name"] or "unknown tool")
                            + ": "
                            + (event["output_text"] or ""),
                            event["evidence_refs"],
                        )
                elif typ == "abpt_message" and event["role"] in (
                    "user",
                    "assistant",
                    "tool",
                ):
                    role = {
                        "user": "user",
                        "assistant": "assistant_claim",
                        "tool": "unpaired_tool_output",
                    }[event["role"]]
                    _add_snippet(
                        index,
                        conversation["id"],
                        event["message_id"],
                        role,
                        event["text"],
                        event["evidence_refs"],
                    )
    index.commit()
    index.close()
    derived = build_candidates(episodes)
    Store(output).write("workflows.json", derived)
    counts["workflow_episodes"] = len(episodes)
    counts["workflow_candidates"] = len(derived["candidates"])
    learner.save(output)
    rendered = learner.render()
    visible = set(learner.injected_ids())
    return {
        "counts": dict(counts),
        "facts": len(learner.facts),
        "fact_sources": {
            fid: refs for fid, refs in fact_sources.items() if fid in visible
        },
        "rendered": rendered,
    }


def inspect(database, project_id=None):
    """Read-only archive summary for a project or the corpus."""
    with closing(open_archive(database)) as connection:
        if project_id:
            counts = connection.execute(
                "select capture_state, count(*) from archive_conversations "
                "where project_id=? group by capture_state",
                (project_id,),
            ).fetchall()
            return {
                "project_id": project_id,
                "capture": dict(counts),
                "journal": archive_revision(connection),
            }
        return {
            "projects": [
                {"project_id": row[0], "conversations": row[1]}
                for row in connection.execute(
                    "select project_id, count(*) from archive_conversations "
                    "where project_id is not null group by project_id "
                    "order by count(*) desc"
                )
            ]
        }


def import_project(
    database, project_id, repo=None, data_root=None, limit=None, env=None
):
    """Atomically publish a new learning snapshot; repeat requests are no-ops."""
    if not project_id:
        raise ValueError("an explicit ABPT project ID is required")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    data_root = Path(data_root or Path(database).resolve().parent.parent)
    store = _scope(project_id, env)
    previous_umask = os.umask(0o077)
    try:
        with store.locked("import", timeout=5) as acquired:
            if not acquired:
                raise RuntimeError("another corpus import is running for this project")
            with closing(open_archive(database)) as connection:
                connection.execute("begin")
                source = _source_meta(
                    database, project_id, repo, limit, data_root, connection
                )
                current = store.read("current.json") or {}
                if (
                    _source_equal(current, source)
                    and _snapshot_dir(store, current).exists()
                ):
                    return dict(current, changed=False)
                revisions = Path(store.ensure("revisions"))
                staging = Path(tempfile.mkdtemp(prefix=".building-", dir=revisions))
                try:
                    details = _learn_project(
                        source, staging, limit, data_root, connection
                    )
                    revision_dir = (
                        _key(json.dumps(source, sort_keys=True))
                        + "-"
                        + staging.name.removeprefix(".building-")
                    )
                    final = revisions / revision_dir
                    next_state = dict(source, **details, revision_dir=revision_dir)
                    Store(staging).write("manifest.json", next_state)
                    os.replace(staging, final)
                    # Store.write atomically replaces the pointer after all outputs exist.
                    store.write("current.json", next_state)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
                return dict(next_state, changed=True)
    finally:
        os.umask(previous_umask)


def _read_workflow_snapshot(project_id, env=None):
    store = _scope(project_id, env)
    state = store.read("current.json")
    if not state:
        raise ValueError("project has no corpus import; run te corpus import first")
    version = _snapshot_dir(store, state)
    data = Store(version).read("workflows.json")
    if data is None:
        raise ValueError("import predates workflow extraction; run te corpus sync")
    return state, data


def _review_records(project_id, env):
    return load_reviews(_scope(project_id, env))["records"]


def workflow_candidates(project_id, limit=6, env=None):
    """List source-backed candidates and current review decisions for one project."""
    if limit < 1:
        raise ValueError("count must be positive")
    state, data = _read_workflow_snapshot(project_id, env)
    records = _review_records(project_id, env)
    return {
        "project_id": project_id,
        "revision_dir": state["revision_dir"],
        "total": len(data["candidates"]),
        "candidates": [
            reviewed_candidate(candidate, records)
            for candidate in data["candidates"][:limit]
        ],
    }


def workflow_detail(project_id, candidate_id, env=None):
    """Inspect one candidate, its review, and each supporting source episode."""
    state, data = _read_workflow_snapshot(project_id, env)
    candidate = next(
        (item for item in data["candidates"] if item["candidate_id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError("workflow candidate not found in this project")
    records = _review_records(project_id, env)
    ids = set(candidate["episodes"])
    return {
        "project_id": project_id,
        "revision_dir": state["revision_dir"],
        "candidate": reviewed_candidate(candidate, records),
        "review_history": records.get(candidate_id, []),
        "episodes": [episode for episode in data["episodes"] if episode["id"] in ids],
    }


def workflow_reviews(project_id, env=None):
    """Return the latest review, including stale and disappeared candidates."""
    state, data = _read_workflow_snapshot(project_id, env)
    candidates = {item["candidate_id"]: item for item in data["candidates"]}
    records = _review_records(project_id, env)
    return {
        "project_id": project_id,
        "revision_dir": state["revision_dir"],
        "reviews": [
            dict(
                review_status(candidates.get(candidate_id), records[candidate_id]),
                history=records[candidate_id],
            )
            for candidate_id in sorted(records)
            if records[candidate_id]
        ],
    }


def review_workflow(
    project_id,
    candidate_id,
    decision,
    evidence_digest,
    reason,
    approved_steps=None,
    env=None,
):
    """Append a human decision to the project's private, persistent ledger."""
    validate_review(decision, reason, approved_steps)
    store = _scope(project_id, env)
    previous_umask = os.umask(0o077)
    try:
        # Import and review share a lock so the checked revision stays current.
        with store.locked("import", timeout=5) as acquired:
            if not acquired:
                raise RuntimeError("another corpus import or review is running")
            _state, data = _read_workflow_snapshot(project_id, env)
            candidate = next(
                (
                    item
                    for item in data["candidates"]
                    if item["candidate_id"] == candidate_id
                ),
                None,
            )
            if candidate is None:
                raise ValueError("workflow candidate not found in this project")
            return store_decision(
                store,
                candidate,
                project_id,
                candidate_id,
                decision,
                evidence_digest,
                reason,
                approved_steps,
            )
    finally:
        os.umask(previous_umask)


def workflow_search(project_id, query, limit=6, env=None):
    """Search current reviewed procedures; do not auto-inject or execute them."""
    if limit < 1:
        raise ValueError("count must be positive")
    words = query_terms(query or "")
    if not words:
        raise ValueError("workflow search requires a nonempty --query")
    state, data = _read_workflow_snapshot(project_id, env)
    reviews = _review_records(project_id, env)
    matches = find_approved(data["candidates"], data["episodes"], reviews, words)
    return {
        "project_id": project_id,
        "revision_dir": state["revision_dir"],
        "query": query,
        "total": len(matches),
        "workflows": matches[:limit],
    }


def context(project_id, query=None, limit=6, env=None):
    """Return cited evidence and the free learner's rendered project facts."""
    store = _scope(project_id, env)
    state = store.read("current.json")
    if not state:
        raise ValueError("project has no corpus import; run te corpus import first")
    version = _snapshot_dir(store, state)
    with closing(sqlite3.connect(version / "evidence.db")) as connection:
        if query:
            words = query_terms(query)
            if not words:
                return dict(state, evidence=[])
            expression = " AND ".join(
                '"' + term.replace('"', '""') + '"' for term in words
            )
            rows = connection.execute(
                "select conversation_id, message_id, role, content, evidence_ref "
                "from snippets where snippets match ? order by bm25(snippets) limit ?",
                (expression, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "select conversation_id, message_id, role, content, evidence_ref "
                "from snippets order by rowid desc limit ?",
                (limit,),
            ).fetchall()
    evidence = [
        dict(
            zip(
                ("conversation_id", "message_id", "role", "content", "evidence_ref"),
                row,
            )
        )
        for row in rows
    ]
    return dict(state, evidence=evidence, facts=state.get("rendered", ""))


def format_context(result):
    lines = ["ABPT historical context (" + result["project_id"] + ")"]
    if result.get("repo"):
        lines.append("Repository: " + result["repo"]["path"])
    lines.append("Imported sessions: " + str(result["counts"].get("sessions", 0)))
    if result.get("facts"):
        lines.append("Learned facts:\n" + result["facts"].strip())
        for fact_id, sources in result.get("fact_sources", {}).items():
            if sources:
                lines.append(
                    "  Supporting sessions for "
                    + fact_id
                    + ": "
                    + ", ".join(s["conversation_id"] for s in sources)
                )
    if result.get("evidence"):
        lines.append("Historical evidence (assistant text is an unverified claim):")
    for item in result.get("evidence", []):
        lines.append(
            "- ["
            + item["role"]
            + "] "
            + item["content"][:350].replace("\n", " ")
            + "\n  Source: conversation="
            + item["conversation_id"]
            + " message="
            + item["message_id"]
            + (" evidence=" + item["evidence_ref"] if item["evidence_ref"] else "")
        )
    return "\n".join(lines)
