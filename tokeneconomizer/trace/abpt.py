"""Translate ABPT's conversation archive into evidence-bearing trace events.

The archive owns conversation history. This reader never changes it. A tool response
is evidence that output was observed, not necessarily that a command succeeded.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

SOURCE = "abpt"
EXIT_RE = re.compile(
    r"(?im)^\s*(?:[✅⚠]\s*)?(?:process completed with exit code|"
    r"process exited with code|exit code)\s*[:=]?\s*(-?\d+)\b"
)


def read_mapping(conversation, data_root):
    """Look up the canonical snapshot named by ABPT's evidence references."""
    if data_root is None:
        return {}
    root = Path(data_root).resolve()
    for ref in conversation.get("evidenceRefs", []):
        if ref.get("kind") != "snapshot" or not ref.get("relativePath"):
            continue
        path = (root / ref["relativePath"]).resolve()
        if not path.is_relative_to(root):
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                payload = json.load(stream).get("payload", {})
                canonical = payload.get("conversationJson") or payload
                return canonical.get("mapping", {})
        except (OSError, ValueError, AttributeError):
            continue
    return {}


def _raw_message(msg, mapping):
    node = mapping.get(msg.get("metadata", {}).get("sourceNodeId")) or {}
    return node.get("message") or {}


def _tool_details(msg, mapping):
    raw = _raw_message(msg, mapping)
    recipient = raw.get("recipient")
    name = (
        recipient
        if isinstance(recipient, str) and recipient not in ("", "all")
        else None
    )
    name = name or msg.get("metadata", {}).get("toolName")
    args = {}
    text = msg.get("text", "")
    if isinstance(text, str) and text.lstrip().startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                args = parsed
        except ValueError:
            pass
    if not args:
        args = {"input_text": text}
    return name, args


def _result_belongs_to(parent, result, mapping):
    """Join a selected-branch result to its original recipient."""
    original = _raw_message(parent, mapping)
    response = _raw_message(result, mapping)
    caller_role = (original.get("author") or {}).get("role")
    responder = response.get("author") or {}
    responder_role = responder.get("role")
    recipient = original.get("recipient")
    result_name = responder.get("name")
    if caller_role == "assistant" and responder_role == "tool" and result_name:
        return bool(recipient) and recipient != "all" and recipient == result_name
    # Keep the explicit artifact fallback when canonical attribution is incomplete.
    # Canonical role conflicts cannot be overridden by archive presentation labels.
    return (
        result.get("metadata", {}).get("toolArtifactKind") == "execution-output"
        and caller_role in (None, "assistant")
        and responder_role in (None, "tool")
        and recipient != "all"
    )


def _outcome(name, result, mapping):
    if result is None:
        return "unverified", None, None
    raw = _raw_message(result, mapping)
    metadata = raw.get("metadata") or {}
    aggregate = metadata.get("aggregate_result") or {}
    # ChatGPT message.status=finished_successfully means the message finished;
    # Python aggregate_result.status describes actual kernel execution.
    if name == "python":
        if aggregate.get("status") == "failed_with_in_kernel_exception":
            return "verified_failure", None, "python.aggregate_result.status"
        if (
            aggregate.get("status") == "success"
            and not aggregate.get("in_kernel_exception")
            and not aggregate.get("system_exception")
            and not aggregate.get("timeout_triggered")
        ):
            return "verified_success", None, "python.aggregate_result.status"
    # A container.exec aggregate_result.status=success does not establish the
    # process exit code; only explicit process output can establish that.
    process_tools = {
        "mcp__Remote_Desktop_Commander__start_process",
        "functions.shell",
        "functions.command",
        "exec_command",
    }
    if name in process_tools:
        match = EXIT_RE.search(result.get("text", ""))
        if match:
            code = int(match.group(1))
            return (
                ("verified_success" if code == 0 else "verified_failure"),
                code,
                "process.exit_code",
            )
    if metadata.get("is_error") is True:
        return "verified_failure", None, "tool.metadata.is_error"
    return "observed_output", None, "result_observed"


def convert_conversation(conversation, messages, mapping=None):
    """Preserve selected-branch order and pair outputs through parent message IDs."""
    mapping = mapping or {}
    sid = conversation["id"]
    ordered = sorted(messages, key=lambda m: (m["index"], m["id"]))
    by_id = {m["id"]: m for m in ordered}
    outputs = {}
    for msg in ordered:
        if msg["role"] != "tool":
            continue
        parent = by_id.get(msg.get("parentMessageId"))
        if (
            parent
            and parent["role"] == "tool"
            and not parent.get("metadata", {}).get("toolArtifactKind")
            and _result_belongs_to(parent, msg, mapping)
        ):
            outputs[parent["id"]] = msg
    paired = {msg["id"] for msg in outputs.values()}
    events = [
        {
            "type": "abpt_session_start",
            "source": SOURCE,
            "session_id": sid,
            "project_id": conversation.get("projectId"),
            "provider": conversation.get("providerId"),
            "account": conversation.get("accountFingerprint"),
            "created_at": conversation.get("createdAt"),
            "updated_at": conversation.get("updatedAt"),
            "capture_state": conversation.get("captureState"),
            "evidence_refs": conversation.get("evidenceRefs", []),
        }
    ]
    for msg in ordered:
        if msg["id"] in paired:
            continue
        base = {
            "source": SOURCE,
            "session_id": sid,
            "message_id": msg["id"],
            "message_index": msg["index"],
            "parent_id": msg.get("parentMessageId"),
            "evidence_refs": msg.get("evidenceRefs", []),
        }
        kind = msg.get("metadata", {}).get("toolArtifactKind")
        raw = _raw_message(msg, mapping)
        raw_author = (raw.get("author") or {}).get("role")
        recipient = raw.get("recipient")
        canonical_call = raw_author == "assistant" and recipient not in (
            None,
            "",
            "all",
        )
        legacy_call = raw_author is None and bool(msg.get("text"))
        if msg["role"] != "tool" or kind or not (canonical_call or legacy_call):
            events.append(
                {
                    "type": "abpt_message",
                    **base,
                    "role": msg["role"],
                    "text": msg.get("text", ""),
                    "artifact_kind": kind,
                }
            )
            continue
        result = outputs.get(msg["id"])
        name, args = _tool_details(msg, mapping)
        outcome, exit_code, outcome_evidence = _outcome(name, result, mapping)
        events.append(
            {
                "type": "abpt_tool_call",
                **base,
                "tool_name": name,
                "tool_args": args,
                "activity_title": msg.get("metadata", {}).get("activityTitle"),
                "input_text": msg["text"],
                "output_text": result.get("text", "") if result else None,
                "result_id": result["id"] if result else None,
                "outcome": outcome,
                "outcome_evidence": outcome_evidence,
                "exit_code": exit_code,
                "evidence_refs": base["evidence_refs"]
                + (result.get("evidenceRefs", []) if result else []),
            }
        )
    events.append({"type": "abpt_session_end", "source": SOURCE, "session_id": sid})
    return events


def abpt_calls(lines):
    """Project real tool observations to the shared trace tuple contract."""
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "abpt_tool_call":
            continue
        name = event.get("tool_name") or "chatgpt.tool"
        args = event.get("tool_args") or {"input_text": event.get("input_text", "")}
        outcome = event.get("outcome")
        # Promote known command-execution tools only when the actual command is present.
        # Unknown exit statuses remain unknown even with observed output.
        if name in (
            "mcp__Remote_Desktop_Commander__start_process",
            "functions.shell",
            "functions.command",
            "exec_command",
        ) and isinstance(args.get("command"), str):
            name = "bash"
        success = (
            True
            if outcome == "verified_success"
            else (False if outcome == "verified_failure" else None)
        )
        yield (name, args, event.get("output_text") or "", success)


def open_archive(database):
    connection = sqlite3.connect(
        Path(database).resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.execute("pragma query_only = on")
    return connection


def archive_revision(connection):
    journal = connection.execute(
        "select journal_id from archive_journal_identity where id='current'"
    ).fetchone()[0]
    latest = connection.execute(
        "select coalesce(max(sequence), 0) from archive_change_log"
    ).fetchone()[0]
    return {"journal": journal, "sequence": latest}


def iter_conversations(connection, project_id=None, limit=None):
    query = "select conversation_id, record_json from archive_conversations"
    args = []
    if project_id is not None:
        query += " where project_id = ?"
        args.append(project_id)
    query += " order by json_extract(record_json, '$.createdAt'), conversation_id"
    if limit is not None:
        query += " limit ?"
        args.append(limit)
    for sid, record in connection.execute(query, args):
        messages = (
            json.loads(row[0])
            for row in connection.execute(
                "select record_json from archive_messages where conversation_id = ? "
                "order by message_index, message_id",
                (sid,),
            )
        )
        yield json.loads(record), list(messages)


def export_corpus(database, output, conversation_id=None, limit=None, data_root=None):
    """Stream one JSONL trace per conversation without modifying ABPT."""
    output_dir = Path(output)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    count = 0
    with open_archive(database) as connection:
        if conversation_id is not None:
            row = connection.execute(
                "select record_json from archive_conversations "
                "where conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            conversations = (
                []
                if row is None
                else [
                    (
                        json.loads(row[0]),
                        [
                            json.loads(r[0])
                            for r in connection.execute(
                                "select record_json from archive_messages "
                                "where conversation_id=? order by message_index,message_id",
                                (conversation_id,),
                            )
                        ],
                    )
                ]
            )
        else:
            conversations = iter_conversations(connection, limit=limit)
        for conversation, messages in conversations:
            if limit is not None and count >= limit:
                break
            path = output_dir / (
                hashlib.sha256(conversation["id"].encode()).hexdigest() + ".jsonl"
            )
            events = convert_conversation(
                conversation, messages, read_mapping(conversation, data_root)
            )
            with os.fdopen(
                os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                "w",
                encoding="utf-8",
            ) as target:
                target.writelines(
                    json.dumps(event, ensure_ascii=False) + "\n" for event in events
                )
            count += 1
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Translate ABPT's conversation corpus into traces"
    )
    parser.add_argument("database")
    parser.add_argument("output", help="one JSONL per conversation")
    parser.add_argument("--conversation-id")
    parser.add_argument("--data-root")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    print(
        f"translated {export_corpus(args.database, args.output, args.conversation_id, args.limit, args.data_root)} conversation(s)"
    )


if __name__ == "__main__":
    main()
