"""ABPT conversation archive translation, preserving provenance and uncertain outcomes."""

import json
import sqlite3

from tokeneconomizer import trace
from tokeneconomizer.trace.abpt import abpt_calls, convert_conversation, export_corpus


def _message(mid, role, index, text, parent=None, kind=None):
    metadata = {"sourceNodeId": mid}
    if kind:
        metadata["toolArtifactKind"] = kind
    return {
        "id": mid,
        "conversationId": "c1",
        "projectId": "project-1",
        "role": role,
        "index": index,
        "text": text,
        "parentMessageId": parent,
        "metadata": metadata,
        "evidenceRefs": [{"relativePath": f"evidence/{mid}.json"}],
    }


def _conversation(cid="c1"):
    return {
        "id": cid,
        "providerId": "chatgpt",
        "projectId": "project-1",
        "captureState": "complete",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "evidenceRefs": [{"kind": "snapshot"}],
    }


def test_conversation_translation_pairs_observed_tool_result_without_claiming_success():
    rows = [
        _message("u1", "user", 0, "Fix tests"),
        _message("t1", "tool", 1, "pytest -q", parent="u1"),
        _message("t2", "tool", 2, "2 failed", parent="t1", kind="execution-output"),
        _message("a1", "assistant", 3, "Two tests failed", parent="t2"),
        _message("t3", "tool", 4, "npm test", parent="a1"),
    ]
    events = convert_conversation(_conversation(), rows)
    assert [event["type"] for event in events] == [
        "abpt_session_start",
        "abpt_message",
        "abpt_tool_call",
        "abpt_message",
        "abpt_tool_call",
        "abpt_session_end",
    ]
    observed = events[2]
    assert observed["tool_name"] is None
    assert observed["input_text"] == "pytest -q"
    assert observed["output_text"] == "2 failed"
    assert observed["outcome"] == "observed_output"
    assert observed["result_id"] == "t2"
    assert len(observed["evidence_refs"]) == 2
    assert events[4]["outcome"] == "unverified"
    assert events[4]["output_text"] is None
    assert events[0]["capture_state"] == "complete"
    assert events[1]["text"] == "Fix tests"


def test_orphan_output_keeps_evidence_but_does_not_create_tool_call():
    events = convert_conversation(
        _conversation(),
        [
            _message(
                "o1", "tool", 0, "traceback", parent="missing", kind="execution-output"
            )
        ],
    )
    assert [ev["type"] for ev in events] == [
        "abpt_session_start",
        "abpt_message",
        "abpt_session_end",
    ]
    assert events[1]["role"] == "tool"
    assert events[1]["evidence_refs"][0]["relativePath"] == "evidence/o1.json"


def test_export_is_read_only_and_compatible_with_trace_reader(tmp_path):
    database = tmp_path / "abpt.db"
    con = sqlite3.connect(database)
    con.execute(
        "create table archive_conversations (conversation_id text, record_json text)"
    )
    con.execute(
        "create table archive_messages (conversation_id text, message_index integer, message_id text, record_json text)"
    )
    con.execute(
        "insert into archive_conversations values (?, ?)",
        ("c1", json.dumps(_conversation())),
    )
    for m in [
        _message("u1", "user", 0, "hello"),
        _message("t1", "tool", 1, "pytest -q", parent="u1"),
        _message("t2", "tool", 2, "2 passed", parent="t1", kind="execution-output"),
    ]:
        con.execute(
            "insert into archive_messages values (?, ?, ?, ?)",
            ("c1", m["index"], m["id"], json.dumps(m)),
        )
    con.commit()
    con.close()
    output = tmp_path / "translated-sessions"
    assert export_corpus(database, output, conversation_id="c1") == 1
    files = list(output.glob("*.jsonl"))
    assert len(files) == 1
    result = trace.load_trajectory(files[0])
    assert result == [("chatgpt.tool", {"input_text": "pytest -q"}, "2 passed", None)]
    con = sqlite3.connect(database)
    assert con.execute("select count(*) from archive_messages").fetchone()[0] == 3
    con.close()


def _raw_tool_path(tool, result_kind=None, result_status=None, result_author=None):
    """One selected ChatGPT invocation and its canonical child output."""
    output_meta = {}
    if result_status is not None:
        output_meta["aggregate_result"] = result_status
    mapping = {
        "t1": {
            "message": {
                "author": {"role": "assistant"},
                "recipient": tool,
                "content": {"content_type": "code", "text": "request"},
            }
        },
        "r1": {
            "message": {
                "author": {"role": "tool", "name": result_author or tool},
                "recipient": "all",
                "content": {"content_type": "text"},
                "metadata": output_meta,
                "status": "finished_successfully",
            }
        },
    }
    messages = [
        _message("t1", "tool", 0, "request"),
        _message("r1", "tool", 1, "tool result", parent="t1", kind=result_kind),
    ]
    return convert_conversation(_conversation(), messages, mapping)


def test_raw_graph_pairs_tool_result_without_execution_output_label():
    events = _raw_tool_path("web.run")
    calls = [event for event in events if event["type"] == "abpt_tool_call"]
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "web.run"
    assert calls[0]["result_id"] == "r1"
    assert calls[0]["output_text"] == "tool result"
    assert calls[0]["outcome"] == "observed_output"
    assert [event["type"] for event in events].count("abpt_message") == 0


def test_explicit_python_aggregate_result_verifies_execution_outcome():
    for aggregate, expected in (
        (
            {
                "status": "success",
                "in_kernel_exception": None,
                "system_exception": None,
                "timeout_triggered": None,
            },
            "verified_success",
        ),
        (
            {
                "status": "failed_with_in_kernel_exception",
                "in_kernel_exception": {"name": "ValueError"},
            },
            "verified_failure",
        ),
    ):
        events = _raw_tool_path(
            "python", result_kind="execution-output", result_status=aggregate
        )
        tool = next(e for e in events if e["type"] == "abpt_tool_call")
        assert tool["outcome"] == expected
        assert tool["result_id"] == "r1"
        assert next(iter(abpt_calls([json.dumps(tool)])))[3] is (
            expected == "verified_success"
        )


def test_container_tool_success_does_not_infer_process_exit_code():
    events = _raw_tool_path(
        "container.exec",
        result_kind="execution-output",
        result_status={"status": "success"},
    )
    tool = next(e for e in events if e["type"] == "abpt_tool_call")
    assert tool["outcome"] == "observed_output"
    assert tool["exit_code"] is None


def test_canonical_tool_name_mismatch_cannot_pair_result():
    events = _raw_tool_path(
        "python", result_kind="execution-output", result_author="web.run"
    )
    tool = next(e for e in events if e["type"] == "abpt_tool_call")
    assert tool["result_id"] is None
    assert tool["outcome"] == "unverified"
    assert len([e for e in events if e.get("role") == "tool"]) == 1


def test_empty_canonical_invocation_remains_a_tool_call():
    mapping = {
        "t1": {
            "message": {
                "author": {"role": "assistant"},
                "recipient": "web.run",
                "content": {"content_type": "text", "parts": [""]},
            }
        }
    }
    events = convert_conversation(
        _conversation(), [_message("t1", "tool", 0, "")], mapping
    )
    calls = [event for event in events if event["type"] == "abpt_tool_call"]
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "web.run"
    assert calls[0]["input_text"] == ""
    assert calls[0]["outcome"] == "unverified"
