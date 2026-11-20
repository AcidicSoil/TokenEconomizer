"""Candidate workflow behavior from source-linked ABPT trace events."""

from tokeneconomizer.plugin.workflows import build_candidates, extract_episodes


def _conversation(project, sid, goal="Run the checks", first="check", second="repair"):
    return [
        {"type": "abpt_session_start", "project_id": project, "session_id": sid},
        {
            "type": "abpt_message",
            "session_id": sid,
            "message_id": f"{sid}-user",
            "role": "user",
            "text": goal,
        },
        {
            "type": "abpt_tool_call",
            "session_id": sid,
            "message_id": f"{sid}-failed",
            "tool_name": "python",
            "input_text": first,
            "tool_args": {"input_text": first},
            "result_id": f"{sid}-failed-output",
            "outcome": "verified_failure",
            "outcome_evidence": "python.aggregate_result.status",
            "exit_code": None,
            "evidence_refs": [{"relativePath": f"canonical/{sid}.json"}],
            "output_text": "ValueError",
        },
        {
            "type": "abpt_message",
            "session_id": sid,
            "message_id": f"{sid}-unpaired",
            "role": "tool",
            "text": "success!",
        },
        {
            "type": "abpt_tool_call",
            "session_id": sid,
            "message_id": f"{sid}-success",
            "tool_name": "python",
            "input_text": second,
            "tool_args": {"input_text": second},
            "result_id": f"{sid}-success-output",
            "outcome": "verified_success",
            "outcome_evidence": "python.aggregate_result.status",
            "exit_code": None,
            "evidence_refs": [{"relativePath": f"canonical/{sid}.json"}],
            "output_text": "ok",
        },
        {
            "type": "abpt_message",
            "session_id": sid,
            "message_id": f"{sid}-assistant",
            "role": "assistant",
            "text": "I fixed everything.",
        },
        {"type": "abpt_session_end", "session_id": sid},
    ]


def test_candidate_preserves_outcomes_and_never_promotes_assistant_claim():
    episodes = extract_episodes(_conversation("project-a", "c1"))
    result = build_candidates(episodes)

    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["kind"] == "observed_recovery"
    assert candidate["readiness"] == "observed"
    assert candidate["episodes"] == [episodes[0]["id"]]
    assert episodes[0]["goal_outcome"] == "unproven"
    assert episodes[0]["request_message_id"] == "c1-user"
    assert [step["call_message_id"] for step in episodes[0]["ordered_steps"]] == [
        "c1-failed",
        "c1-success",
    ]
    assert [step["result_message_id"] for step in episodes[0]["ordered_steps"]] == [
        "c1-failed-output",
        "c1-success-output",
    ]
    assert [step["outcome"] for step in episodes[0]["ordered_steps"]] == [
        "verified_failure",
        "verified_success",
    ]
    assert all(step["source_refs"] for step in episodes[0]["ordered_steps"])
    assert len(result["episodes"]) == 1
    assert result["project_id"] == "project-a"


def test_episodes_split_on_user_turn_and_ignore_unpaired_tool_output():
    events = _conversation("project-a", "c1")[:-1]
    events.extend(
        [
            {
                "type": "abpt_message",
                "session_id": "c1",
                "message_id": "u2",
                "role": "user",
                "text": "Next question",
            },
            {
                "type": "abpt_tool_call",
                "session_id": "c1",
                "message_id": "t3",
                "tool_name": "web.run",
                "input_text": "search",
                "tool_args": {},
                "result_id": None,
                "outcome": "unverified",
                "outcome_evidence": None,
                "evidence_refs": [],
            },
            {"type": "abpt_session_end", "session_id": "c1"},
        ]
    )
    episodes = extract_episodes(events)
    assert len(episodes) == 2
    assert [e["request_message_id"] for e in episodes] == ["c1-user", "u2"]
    assert [len(e["ordered_steps"]) for e in episodes] == [2, 1]
    assert len(build_candidates(episodes)["candidates"]) == 1


def test_identical_steps_group_across_independent_conversations():
    first = extract_episodes(_conversation("project-a", "c1"))
    second = extract_episodes(_conversation("project-a", "c2", goal="Another task"))
    result = build_candidates(first + second)

    assert len(result["candidates"]) == 1
    assert len(result["candidates"][0]["episodes"]) == 2
    assert result["candidates"][0]["readiness"] == "observed"
    assert (
        build_candidates(list(reversed(first + second)))["candidates"]
        == result["candidates"]
    )
    assert (
        build_candidates(first)["candidates"][0]["candidate_id"]
        == result["candidates"][0]["candidate_id"]
    )
    assert (
        build_candidates(first)["candidates"][0]["evidence_digest"]
        != result["candidates"][0]["evidence_digest"]
    )


def test_scope_and_missing_tool_status_cannot_create_recovery():
    a = extract_episodes(_conversation("project-a", "c1"))
    b = extract_episodes(_conversation("project-b", "c2"))
    assert len(build_candidates(a + b)["candidates"]) == 2

    no_failure = _conversation("project-a", "c3")
    no_failure[2]["outcome"] = "observed_output"
    no_failure[2]["outcome_evidence"] = "result_observed"
    candidates = build_candidates(extract_episodes(no_failure))["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["kind"] == "tool_sequence"


def test_changed_source_output_invalidates_candidate_evidence_digest():
    original = extract_episodes(_conversation("project-a", "c1"))
    changed_events = _conversation("project-a", "c1")
    changed_events[4]["output_text"] = "a different verified result"
    changed = extract_episodes(changed_events)

    a = build_candidates(original)["candidates"][0]
    b = build_candidates(changed)["candidates"][0]
    assert a["candidate_id"] == b["candidate_id"]
    assert a["evidence_digest"] != b["evidence_digest"]
