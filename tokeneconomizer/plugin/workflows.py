"""Deterministic, source-backed workflow candidates from ABPT trace events.

These are observed tool sequences, not verified goal completions or instructions.
All extraction and grouping is pure; corpus.py owns reading and persistence.
"""

import hashlib
import json


def _digest(*parts):
    encoded = json.dumps(parts, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def extract_episodes(events):
    """Partition one selected conversation into user-anchored tool episodes."""
    header = next(e for e in events if e["type"] == "abpt_session_start")
    project = header["project_id"]
    conversation = header["session_id"]
    episodes = []
    steps = []
    request_id = None
    request_text = ""
    ordinal = 0

    def finish():
        if not steps:
            return
        episodes.append(
            {
                "id": _digest(project, conversation, ordinal, request_id),
                "project_id": project,
                "conversation_id": conversation,
                "request_message_id": request_id,
                "request_text": request_text,
                "goal_outcome": "unproven",
                "ordered_steps": list(steps),
            }
        )

    for event in events:
        if event["type"] == "abpt_message" and event["role"] == "user":
            finish()
            ordinal += 1
            request_id = event["message_id"]
            request_text = event.get("text", "")
            steps.clear()
        elif event["type"] == "abpt_tool_call":
            tool = event.get("tool_name") or "unknown tool"
            input_text = event.get("input_text", "")
            steps.append(
                {
                    "tool_name": tool,
                    "input_text": input_text,
                    "tool_args": event.get("tool_args") or {},
                    "step_signature": _digest(tool, input_text),
                    "call_message_id": event["message_id"],
                    "result_message_id": event.get("result_id"),
                    "outcome": event["outcome"],
                    "outcome_evidence": event.get("outcome_evidence"),
                    "exit_code": event.get("exit_code"),
                    "output_digest": _digest(event.get("output_text")),
                    "source_refs": sorted(
                        {
                            ref["relativePath"]
                            for ref in event.get("evidence_refs", [])
                            if ref.get("relativePath")
                        }
                    ),
                }
            )
    finish()
    return episodes


def build_candidates(episodes):
    """Group exact ordered actions; keep evidence and project ownership intact."""
    grouped = {}
    ordered = sorted(episodes, key=lambda e: (e["project_id"], e["id"]))
    for episode in ordered:
        steps = episode["ordered_steps"]
        if len(steps) < 2:
            continue
        signature = _digest(*(step["step_signature"] for step in steps))
        grouped.setdefault((episode["project_id"], signature), []).append(episode)

    candidates = []
    for (project, signature), group in sorted(grouped.items()):
        recovered = any(
            any(
                a["outcome"] == "verified_failure"
                and b["outcome"] == "verified_success"
                and a["tool_name"] == b["tool_name"]
                for index, a in enumerate(episode["ordered_steps"])
                for b in episode["ordered_steps"][index + 1 :]
            )
            for episode in group
        )
        evidence = [
            (
                episode["id"],
                episode["request_text"],
                [
                    (
                        step["call_message_id"],
                        step["result_message_id"],
                        step["outcome"],
                        step["outcome_evidence"],
                        step["output_digest"],
                        step["source_refs"],
                    )
                    for step in episode["ordered_steps"]
                ],
            )
            for episode in group
        ]
        candidates.append(
            {
                "candidate_id": _digest(project, signature),
                "project_id": project,
                "signature": signature,
                "evidence_digest": _digest(evidence),
                "episodes": [episode["id"] for episode in group],
                "kind": "observed_recovery" if recovered else "tool_sequence",
                "readiness": "observed",
            }
        )
    return {
        "project_id": ordered[0]["project_id"]
        if ordered and all(e["project_id"] == ordered[0]["project_id"] for e in ordered)
        else None,
        "candidates": candidates,
        "episodes": ordered,
    }
