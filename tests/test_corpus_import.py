"""ABPT corpus import, read-only source, revision replay, evidence and scope contracts."""

import json
import sqlite3
from pathlib import Path

from tokeneconomizer.plugin import corpus
from tokeneconomizer.trace.abpt import abpt_calls, convert_conversation, read_mapping


def _message(project, sid, mid, role, index, text, parent=None, kind=None):
    metadata = {"sourceNodeId": mid}
    if kind:
        metadata["toolArtifactKind"] = kind
    return {
        "id": mid,
        "conversationId": sid,
        "projectId": project,
        "role": role,
        "index": index,
        "text": text,
        "parentMessageId": parent,
        "metadata": metadata,
        "evidenceRefs": [{"relativePath": "canonical/c1.json"}],
    }


def _conversation(project, sid, n):
    return {
        "id": sid,
        "providerId": "chatgpt",
        "projectId": project,
        "captureState": "complete",
        "createdAt": f"2026-01-0{n}T00:00:00Z",
        "evidenceRefs": [{"kind": "snapshot", "relativePath": "canonical/c1.json"}],
    }


def _archive(tmp_path):
    database = tmp_path / "abpt.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        create table archive_conversations (
          conversation_id text primary key, project_id text, capture_state text, record_json text);
        create table archive_messages (
          conversation_id text, message_id text, message_index integer, record_json text);
        create table archive_journal_identity (id text, journal_id text);
        create table archive_change_log (sequence integer primary key, kind text);
        insert into archive_journal_identity values ('current','journal-one');
        insert into archive_change_log values (1,'conversation');
    """)
    (tmp_path / "canonical").mkdir()
    return database, connection


def _insert(connection, project, n):
    sid = f"c{n}"
    c = _conversation(project, sid, n)
    connection.execute(
        "insert into archive_conversations values(?,?,?,?)",
        (sid, project, c["captureState"], json.dumps(c)),
    )
    rows = [
        _message(project, sid, "u" + sid, "user", 0, "pytest is missing"),
        _message(project, sid, "t" + sid, "tool", 1, '{"command":"pytest"}', "u" + sid),
        _message(
            project,
            sid,
            "o" + sid,
            "tool",
            2,
            "pytest: command not found\nExit code 127",
            "t" + sid,
            "execution-output",
        ),
        _message(project, sid, "a" + sid, "assistant", 3, "I fixed it", "o" + sid),
    ]
    for m in rows:
        connection.execute(
            "insert into archive_messages values(?,?,?,?)",
            (sid, m["id"], m["index"], json.dumps(m)),
        )


def _mapping(tmp_path):
    mapping = {}
    for n in (1, 2, 3, 4):
        sid = f"c{n}"
        mapping["t" + sid] = {
            "message": {
                "recipient": "mcp__Remote_Desktop_Commander__start_process",
                "author": {"role": "assistant"},
            }
        }
        mapping["o" + sid] = {
            "message": {
                "author": {
                    "role": "tool",
                    "name": "mcp__Remote_Desktop_Commander__start_process",
                },
                "metadata": {},
            }
        }
    (tmp_path / "canonical").mkdir(exist_ok=True)
    (tmp_path / "canonical/c1.json").write_text(
        json.dumps({"payload": {"mapping": mapping}})
    )


def test_snapshot_identifies_real_tool_and_exit_code(tmp_path):
    _mapping(tmp_path)
    conversation = _conversation("project-x", "c1", 1)
    mapping = read_mapping(conversation, tmp_path)
    messages = [
        _message("project-x", "c1", "uc1", "user", 0, "run tests"),
        _message("project-x", "c1", "tc1", "tool", 1, '{"command":"pytest"}', "uc1"),
        _message(
            "project-x",
            "c1",
            "oc1",
            "tool",
            2,
            "pytest: command not found\nExit code 127",
            "tc1",
            "execution-output",
        ),
    ]
    events = convert_conversation(conversation, messages, mapping)
    tool = next(event for event in events if event["type"] == "abpt_tool_call")
    assert tool["tool_name"] == "mcp__Remote_Desktop_Commander__start_process"
    assert tool["exit_code"] == 127
    assert tool["outcome"] == "verified_failure"
    calls = list(abpt_calls(json.dumps(event) for event in events))
    assert calls == [
        (
            "bash",
            {"command": "pytest"},
            "pytest: command not found\nExit code 127",
            False,
        )
    ]


def test_import_learns_and_sync_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "state"))
    database, con = _archive(tmp_path)
    _mapping(tmp_path)
    for n in (1, 2, 3):
        _insert(con, "project-x", n)
    con.commit()
    first = corpus.import_project(database, "project-x", data_root=tmp_path)
    assert first["changed"] is True
    assert first["counts"]["sessions"] == 3
    assert first["counts"]["verified_calls"] == 3
    assert first["counts"]["tool_calls"] == 3
    retrieved = corpus.context("project-x", query="pytest")
    assert retrieved["evidence"]
    assert all(
        record["conversation_id"].startswith("c") for record in retrieved["evidence"]
    )
    assert any(record["role"] == "tool_observation" for record in retrieved["evidence"])
    assert "pytest" in corpus.format_context(retrieved)
    assert corpus.context("project-x", query="nonexistentzz")["evidence"] == []
    again = corpus.import_project(database, "project-x", data_root=tmp_path)
    assert again["changed"] is False
    assert again["revision_dir"] == first["revision_dir"]
    _insert(con, "project-x", 4)
    con.execute("insert into archive_change_log values (2,'conversation')")
    con.commit()
    after = corpus.import_project(database, "project-x", data_root=tmp_path)
    assert after["changed"] is True
    assert after["counts"]["sessions"] == 4
    assert "pytest" in after["rendered"]
    assert len(after["fact_sources"]["absent_tool:pytest"]) == 4
    assert after["revision_dir"] != first["revision_dir"]
    assert corpus.context("project-x", query="pytest")["evidence"]
    assert con.execute("select count(*) from archive_messages").fetchone()[0] == 16
    con.close()


def test_project_isolation_and_source_failure_preserve_current(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "state"))
    database, con = _archive(tmp_path)
    _mapping(tmp_path)
    _insert(con, "project-a", 1)
    _insert(con, "project-b", 2)
    con.commit()
    before = corpus.import_project(database, "project-a", data_root=tmp_path)
    assert before["counts"]["sessions"] == 1
    assert corpus.inspect(database, "project-a")["capture"]["complete"] == 1
    assert corpus.context("project-a", query="pytest")["evidence"]
    try:
        corpus.context("project-b")
    except ValueError:
        pass
    else:
        raise AssertionError("cross-project context leaked")
    current_path = corpus._scope("project-a").path("current.json")
    previous = Path(current_path).read_bytes()
    try:
        corpus.import_project(tmp_path / "absent.db", "project-a")
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("missing source should fail")
    assert Path(current_path).read_bytes() == previous
    con.close()


def test_message_status_and_embedded_exit_text_are_not_success(tmp_path):
    conversation = _conversation("project-x", "c1", 1)
    rows = [
        _message("project-x", "c1", "tc1", "tool", 0, "print(1)"),
        _message(
            "project-x",
            "c1",
            "oc1",
            "tool",
            1,
            "Displayed sample: Exit code 0",
            "tc1",
            "execution-output",
        ),
    ]
    mapping = {
        "tc1": {"message": {"recipient": "python", "status": "finished_successfully"}},
        "oc1": {"message": {"author": {"role": "tool"}}},
    }
    event = next(
        e
        for e in convert_conversation(conversation, rows, mapping)
        if e["type"] == "abpt_tool_call"
    )
    assert event["tool_name"] == "python"
    assert event["outcome"] == "observed_output"
    assert event["exit_code"] is None
    assert next(abpt_calls([json.dumps(event)]))[3] is None


def test_nested_snapshot_output_is_not_a_tool_invocation(tmp_path, monkeypatch):
    """ABPT also wraps ChatGPT mapping inside payload.conversationJson."""
    database, connection = _archive(tmp_path)
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "state"))
    mapping = {
        "t1": {
            "message": {
                "author": {"role": "tool", "name": "file_search"},
                "recipient": "all",
                "content": {"content_type": "tether_quote", "text": "quoted"},
            }
        }
    }
    (tmp_path / "canonical/c1.json").write_text(
        json.dumps({"payload": {"conversationJson": {"mapping": mapping}}})
    )
    conversation = _conversation("project-x", "c1", 1)
    actual_mapping = read_mapping(conversation, tmp_path)
    assert actual_mapping == mapping
    messages = [_message("project-x", "c1", "t1", "tool", 0, "quoted text")]
    events = convert_conversation(conversation, messages, actual_mapping)
    assert [event["type"] for event in events] == [
        "abpt_session_start",
        "abpt_message",
        "abpt_session_end",
    ]
    assert events[1]["role"] == "tool"
    assert events[1]["evidence_refs"] == messages[0]["evidenceRefs"]
    assert list(abpt_calls(json.dumps(event) for event in events)) == []
    connection.execute(
        "insert into archive_conversations values(?,?,?,?)",
        ("c1", "project-x", "complete", json.dumps(conversation)),
    )
    connection.execute(
        "insert into archive_messages values(?,?,?,?)",
        ("c1", "t1", 0, json.dumps(messages[0])),
    )
    connection.commit()
    connection.close()
    imported = corpus.import_project(database, "project-x", data_root=tmp_path)
    assert imported["counts"].get("tool_calls", 0) == 0
    matches = corpus.context("project-x", query="quoted")["evidence"]
    assert len(matches) == 1
    assert matches[0]["role"] == "unpaired_tool_output"


def _workflow_archive(tmp_path):
    database, connection = _archive(tmp_path)
    project = "project-x"
    conversation = _conversation(project, "c1", 1)
    rows = [
        _message(project, "c1", "user", "user", 0, "Repair the Python error"),
        _message(project, "c1", "call1", "tool", 1, "raise ValueError()", "user"),
        _message(
            project, "c1", "out1", "tool", 2, "ValueError", "call1", "execution-output"
        ),
        _message(project, "c1", "call2", "tool", 3, "print('ok')", "out1"),
        _message(project, "c1", "out2", "tool", 4, "ok", "call2", "execution-output"),
        _message(project, "c1", "assistant", "assistant", 5, "Fixed", "out2"),
    ]
    mapping = {
        "call1": {"message": {"author": {"role": "assistant"}, "recipient": "python"}},
        "out1": {
            "message": {
                "author": {"role": "tool", "name": "python"},
                "metadata": {
                    "aggregate_result": {
                        "status": "failed_with_in_kernel_exception",
                        "in_kernel_exception": {"name": "ValueError"},
                    }
                },
            }
        },
        "call2": {"message": {"author": {"role": "assistant"}, "recipient": "python"}},
        "out2": {
            "message": {
                "author": {"role": "tool", "name": "python"},
                "metadata": {"aggregate_result": {"status": "success"}},
            }
        },
    }
    (tmp_path / "canonical/c1.json").write_text(
        json.dumps({"payload": {"mapping": mapping}})
    )
    connection.execute(
        "insert into archive_conversations values(?,?,?,?)",
        ("c1", project, "complete", json.dumps(conversation)),
    )
    for row in rows:
        connection.execute(
            "insert into archive_messages values (?,?,?,?)",
            (
                "c1",
                row["id"],
                row["index"],
                json.dumps(row),
            ),
        )
    connection.commit()
    connection.close()

    return database, project


def test_workflow_import_and_cli_inspection(tmp_path, monkeypatch):
    """Candidates are derived in one import and retrievable through public CLI."""
    import os
    import subprocess
    import sys

    database, project = _workflow_archive(tmp_path)
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "state"))
    imported = corpus.import_project(database, project, data_root=tmp_path)
    assert imported["counts"]["workflow_episodes"] == 1
    assert imported["counts"]["workflow_candidates"] == 1
    listing = corpus.workflow_candidates(project)
    assert listing["total"] == 1
    candidate = listing["candidates"][0]
    assert candidate["kind"] == "observed_recovery"
    details = corpus.workflow_detail(project, candidate["candidate_id"])
    assert [
        step["call_message_id"] for step in details["episodes"][0]["ordered_steps"]
    ] == [
        "call1",
        "call2",
    ]
    assert details["episodes"][0]["goal_outcome"] == "unproven"
    assert (
        corpus.import_project(database, project, data_root=tmp_path)["changed"] is False
    )
    try:
        corpus.workflow_detail("other-project", candidate["candidate_id"])
    except ValueError:
        pass
    else:
        raise AssertionError("cross-project candidate leaked")

    def invoke(*args):
        proc = subprocess.run(
            [sys.executable, "-m", "tokeneconomizer.plugin.cli", "corpus", *args],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    actual_listing = invoke("workflows", "--project", project, "--json")
    assert actual_listing["candidates"][0]["candidate_id"] == candidate["candidate_id"]
    actual_details = invoke(
        "workflow", "show", candidate["candidate_id"], "--project", project, "--json"
    )
    assert (
        actual_details["episodes"][0]["ordered_steps"][0]["result_message_id"] == "out1"
    )


def test_earlier_corpus_revision_rebuilds_workflow_index(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "state"))
    database, connection = _archive(tmp_path)
    _mapping(tmp_path)
    _insert(connection, "project-a", 1)
    connection.commit()
    connection.close()

    initial = corpus.import_project(database, "project-a", data_root=tmp_path)
    store = corpus._scope("project-a")
    previous = store.read("current.json")
    del previous["schema_version"]
    store.write("current.json", previous)

    migrated = corpus.import_project(database, "project-a", data_root=tmp_path)
    assert migrated["changed"] is True
    assert migrated["revision_dir"] != initial["revision_dir"]
    assert migrated["schema_version"] == corpus.CORPUS_SCHEMA_VERSION
    assert corpus.workflow_candidates("project-a")["total"] == 0
    assert (
        corpus.import_project(database, "project-a", data_root=tmp_path)["changed"]
        is False
    )


def test_manual_review_survives_real_archive_replay_and_becomes_stale(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "state"))
    database, project = _workflow_archive(tmp_path)
    first = corpus.import_project(database, project, data_root=tmp_path)
    candidate = corpus.workflow_candidates(project)["candidates"][0]
    corpus.review_workflow(
        project,
        candidate["candidate_id"],
        "approved",
        candidate["evidence_digest"],
        reason="Checked original result",
        approved_steps=["Run the reviewed check", "Validate the result"],
    )
    assert corpus.workflow_search(project, "reviewed")["total"] == 1
    ledger = corpus._scope(project).read("workflow-reviews.json")

    canonical = tmp_path / "canonical/c1.json"
    snapshot = json.loads(canonical.read_text())
    snapshot["payload"]["mapping"]["out2"]["message"]["metadata"][
        "aggregate_result"
    ] = {
        "status": "failed_with_in_kernel_exception",
        "in_kernel_exception": {"name": "ValueError"},
    }
    canonical.write_text(json.dumps(snapshot))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "insert into archive_change_log values (?, ?)", (2, "conversation")
        )

    second = corpus.import_project(database, project, data_root=tmp_path)
    assert second["changed"] is True
    assert first["revision_dir"] != second["revision_dir"]
    current = corpus.workflow_candidates(project)["candidates"][0]
    assert current["candidate_id"] == candidate["candidate_id"]
    assert current["evidence_digest"] != candidate["evidence_digest"]
    assert current["review"]["state"] == "stale"
    assert corpus.workflow_search(project, "reviewed")["workflows"] == []
    assert corpus.workflow_reviews(project)["reviews"][0]["state"] == "stale"
    assert corpus._scope(project).read("workflow-reviews.json") == ledger
    assert (
        corpus.import_project(database, project, data_root=tmp_path)["changed"] is False
    )
