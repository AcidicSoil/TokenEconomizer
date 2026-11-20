"""Persistent, project-scoped workflow review decisions and CLI contracts."""

import json
import os
import subprocess
import sys

import pytest

from tokeneconomizer.plugin import corpus
from tokeneconomizer.plugin.store import Store

PROJECT = "project-review"
CANDIDATE_ID = "candidate-one"
EVIDENCE_DIGEST = "evidence-v1"
STEPS = ["Run the check", "Inspect its output", "Apply the reviewed correction"]


def _seed(tmp_path, project=PROJECT, evidence=EVIDENCE_DIGEST, candidate=True):
    """Create a stored derived revision; never write to the ABPT source."""
    root = tmp_path / "state"
    env = {"TOKENECONOMIZER_HOME": str(root)}
    store = corpus._scope(project, env)
    revision_name = "revision-" + evidence
    derived = Store(store.ensure("revisions", revision_name))
    derived.write(
        "workflows.json",
        {
            "project_id": project,
            "candidates": (
                [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "project_id": project,
                        "signature": "same-steps",
                        "evidence_digest": evidence,
                        "episodes": ["episode-one"],
                        "kind": "observed_recovery",
                        "readiness": "observed",
                    }
                ]
                if candidate
                else []
            ),
            "episodes": (
                [
                    {
                        "id": "episode-one",
                        "conversation_id": "conversation-one",
                        "project_id": project,
                        "request_message_id": "request-one",
                        "goal_outcome": "unproven",
                        "ordered_steps": [
                            {
                                "call_message_id": "call-one",
                                "result_message_id": "result-one",
                                "outcome": "verified_success",
                                "source_refs": ["canonical/source-one.json"],
                            }
                        ],
                    }
                ]
                if candidate
                else []
            ),
        },
    )
    store.write("current.json", {"project_id": project, "revision_dir": revision_name})
    return env, store


def test_approval_persists_outside_derived_revisions(tmp_path):
    env, store = _seed(tmp_path)
    approved = corpus.review_workflow(
        PROJECT,
        CANDIDATE_ID,
        "approved",
        EVIDENCE_DIGEST,
        reason="Verified prerequisites manually",
        approved_steps=STEPS,
        env=env,
    )
    assert approved["state"] == "approved"
    assert approved["approved_steps"] == STEPS
    assert approved["reason"] == "Verified prerequisites manually"
    assert approved["evidence_digest"] == EVIDENCE_DIGEST

    ledger = store.read("workflow-reviews.json")
    assert ledger["records"][CANDIDATE_ID][-1]["decision"] == "approved"
    assert not os.path.exists(
        store.path("revisions", "revision-" + EVIDENCE_DIGEST, "workflow-reviews.json")
    )
    assert os.path.isfile(store.path("workflow-reviews.json"))
    if os.name == "posix":
        assert os.stat(store.path("workflow-reviews.json")).st_mode & 0o077 == 0

    listed = corpus.workflow_candidates(PROJECT, env=env)["candidates"][0]
    shown = corpus.workflow_detail(PROJECT, CANDIDATE_ID, env=env)
    assert listed["review"]["state"] == "approved"
    assert shown["candidate"]["review"]["state"] == "approved"
    assert shown["episodes"][0]["goal_outcome"] == "unproven"
    assert (
        corpus.workflow_reviews(PROJECT, env=env)["reviews"][0]["state"] == "approved"
    )


def test_replay_marks_review_stale_until_explicit_reapproval(tmp_path):
    env, store = _seed(tmp_path)
    corpus.review_workflow(
        PROJECT,
        CANDIDATE_ID,
        "approved",
        EVIDENCE_DIGEST,
        reason="Reviewed",
        approved_steps=STEPS,
        env=env,
    )
    _seed(tmp_path, evidence="evidence-v2")

    listing = corpus.workflow_candidates(PROJECT, env=env)
    assert listing["candidates"][0]["review"]["state"] == "stale"
    assert (
        corpus.workflow_detail(PROJECT, CANDIDATE_ID, env=env)["candidate"]["review"][
            "state"
        ]
        == "stale"
    )
    assert corpus.workflow_reviews(PROJECT, env=env)["reviews"][0]["state"] == "stale"
    with pytest.raises(ValueError, match="evidence"):
        corpus.review_workflow(
            PROJECT,
            CANDIDATE_ID,
            "approved",
            EVIDENCE_DIGEST,
            reason="Old source",
            approved_steps=STEPS,
            env=env,
        )
    assert len(store.read("workflow-reviews.json")["records"][CANDIDATE_ID]) == 1

    refreshed = corpus.review_workflow(
        PROJECT,
        CANDIDATE_ID,
        "approved",
        "evidence-v2",
        reason="Inspected changed output",
        approved_steps=["Reviewed revised result"],
        env=env,
    )
    assert refreshed["state"] == "approved"
    assert len(store.read("workflow-reviews.json")["records"][CANDIDATE_ID]) == 2
    assert (
        len(corpus.workflow_detail(PROJECT, CANDIDATE_ID, env=env)["review_history"])
        == 2
    )
    reviews = corpus.workflow_reviews(PROJECT, env=env)["reviews"]
    assert reviews[0]["state"] == "approved"
    assert [entry["decision"] for entry in reviews[0]["history"]] == [
        "approved",
        "approved",
    ]


def test_rejected_candidate_stays_suppressed_and_missing_source_is_stale(tmp_path):
    env, store = _seed(tmp_path)
    rejected = corpus.review_workflow(
        PROJECT,
        CANDIDATE_ID,
        "rejected",
        EVIDENCE_DIGEST,
        reason="No supported reusable procedure",
        env=env,
    )
    assert rejected["state"] == "rejected"
    assert rejected["approved_steps"] is None
    assert (
        corpus.workflow_candidates(PROJECT, env=env)["candidates"][0]["review"]["state"]
        == "rejected"
    )

    _seed(tmp_path, evidence="evidence-v2", candidate=False)
    records = corpus.workflow_reviews(PROJECT, env=env)["reviews"]
    assert len(records) == 1
    assert records[0]["state"] == "stale"
    with pytest.raises(ValueError, match="not found"):
        corpus.workflow_detail(PROJECT, CANDIDATE_ID, env=env)
    assert len(store.read("workflow-reviews.json")["records"][CANDIDATE_ID]) == 1


def test_review_validation_does_not_write_or_cross_project_boundary(tmp_path):
    env, store = _seed(tmp_path)
    _seed(tmp_path, project="project-other")
    bad = (
        ("approved", "", STEPS),
        ("approved", "Reviewed", []),
        ("approved", "Reviewed", ["", "check"]),
        ("approved", "Reviewed", None),
        ("rejected", "", None),
    )
    for decision, reason, steps in bad:
        with pytest.raises(ValueError):
            corpus.review_workflow(
                PROJECT,
                CANDIDATE_ID,
                decision,
                EVIDENCE_DIGEST,
                reason=reason,
                approved_steps=steps,
                env=env,
            )
    with pytest.raises(ValueError, match="evidence"):
        corpus.review_workflow(
            PROJECT,
            CANDIDATE_ID,
            "approved",
            "stale-fingerprint",
            reason="Reviewed",
            approved_steps=STEPS,
            env=env,
        )
    with pytest.raises(ValueError):
        corpus.review_workflow(
            "project-other",
            "not-in-other",
            "rejected",
            EVIDENCE_DIGEST,
            reason="Not present",
            env=env,
        )
    assert store.read("workflow-reviews.json") is None
    assert corpus.workflow_reviews("project-other", env=env)["reviews"] == []


def test_cli_approve_reject_and_review_listing(tmp_path):
    env, _ = _seed(tmp_path)
    steps_file = tmp_path / "procedure.json"
    steps_file.write_text(json.dumps(STEPS), encoding="utf-8")
    cli_env = dict(os.environ, **env)

    def invoke(*tokens, ok=True):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tokeneconomizer.plugin.cli",
                "corpus",
                "workflow",
                *tokens,
                "--project",
                PROJECT,
                "--json",
            ],
            capture_output=True,
            text=True,
            env=cli_env,
            timeout=30,
            check=False,
        )
        assert (result.returncode == 0) is ok, result.stderr
        return json.loads(result.stdout) if ok else result.stderr

    approved = invoke(
        "approve",
        CANDIDATE_ID,
        "--reason",
        "Checked the source",
        "--evidence-digest",
        EVIDENCE_DIGEST,
        "--steps-file",
        str(steps_file),
    )
    assert approved["state"] == "approved"
    assert approved["approved_steps"] == STEPS
    assert invoke("show", CANDIDATE_ID)["candidate"]["review"]["state"] == "approved"
    assert invoke("reviews")["reviews"][0]["state"] == "approved"
    searched = invoke("search", "--query", "reviewed correction")
    assert searched["total"] == 1
    assert searched["workflows"][0]["candidate_id"] == CANDIDATE_ID
    assert searched["workflows"][0]["goal_outcome"] == "unproven"
    assert "query" in invoke("search", ok=False)

    rejected = invoke(
        "reject",
        CANDIDATE_ID,
        "--reason",
        "No longer a safe procedure",
        "--evidence-digest",
        EVIDENCE_DIGEST,
    )
    assert rejected["state"] == "rejected"
    assert invoke("reviews")["reviews"][0]["state"] == "rejected"
    assert "evidence" in invoke(
        "approve",
        CANDIDATE_ID,
        "--reason",
        "Invalid stale review",
        "--evidence-digest",
        "wrong",
        "--steps-file",
        str(steps_file),
        ok=False,
    )


def test_approved_search_matches_reviewed_steps_and_cites_sources(tmp_path):
    env, _ = _seed(tmp_path)
    assert corpus.workflow_search(PROJECT, "pytest", env=env)["workflows"] == []
    corpus.review_workflow(
        PROJECT,
        CANDIDATE_ID,
        "approved",
        EVIDENCE_DIGEST,
        reason="Manually checked command",
        approved_steps=[
            "Use uv run pytest for the focused tests",
            "Inspect the trace output",
        ],
        env=env,
    )

    found = corpus.workflow_search(PROJECT, "PyTeSt TRACE", env=env)
    assert found["project_id"] == PROJECT
    assert found["total"] == 1
    match = found["workflows"][0]
    assert match["candidate_id"] == CANDIDATE_ID
    assert match["evidence_digest"] == EVIDENCE_DIGEST
    assert match["approved_steps"] == [
        "Use uv run pytest for the focused tests",
        "Inspect the trace output",
    ]
    assert match["review_reason"] == "Manually checked command"
    assert match["goal_outcome"] == "unproven"
    assert match["sources"] == [
        {
            "conversation_id": "conversation-one",
            "request_message_id": "request-one",
            "steps": [
                {
                    "call_message_id": "call-one",
                    "result_message_id": "result-one",
                    "outcome": "verified_success",
                    "source_refs": ["canonical/source-one.json"],
                }
            ],
        }
    ]
    assert "request_text" not in match
    assert corpus.workflow_search(PROJECT, "test", env=env)["total"] == 0
    assert corpus.workflow_search(PROJECT, "unrelated", env=env)["total"] == 0


def test_approved_search_excludes_rejection_stale_and_other_projects(tmp_path):
    env, _ = _seed(tmp_path)
    _seed(tmp_path, project="project-other")
    for project in (PROJECT, "project-other"):
        corpus.review_workflow(
            project,
            CANDIDATE_ID,
            "approved",
            EVIDENCE_DIGEST,
            reason="Reviewed",
            approved_steps=["Run the pytest checks"],
            env=env,
        )
    assert corpus.workflow_search(PROJECT, "pytest", env=env)["total"] == 1
    assert corpus.workflow_search("project-other", "pytest", env=env)["total"] == 1

    corpus.review_workflow(
        PROJECT,
        CANDIDATE_ID,
        "rejected",
        EVIDENCE_DIGEST,
        reason="Outdated approach",
        env=env,
    )
    assert corpus.workflow_search(PROJECT, "pytest", env=env)["total"] == 0
    assert corpus.workflow_search("project-other", "pytest", env=env)["total"] == 1

    corpus.review_workflow(
        PROJECT,
        CANDIDATE_ID,
        "approved",
        EVIDENCE_DIGEST,
        reason="Re-reviewed",
        approved_steps=["Run pytest through uv"],
        env=env,
    )
    _seed(tmp_path, evidence="evidence-v2")
    assert corpus.workflow_search(PROJECT, "pytest", env=env)["total"] == 0
    corpus.review_workflow(
        PROJECT,
        CANDIDATE_ID,
        "approved",
        "evidence-v2",
        reason="Checked revised source",
        approved_steps=["Run pytest through uv"],
        env=env,
    )
    assert corpus.workflow_search(PROJECT, "pytest", env=env)["total"] == 1
    _seed(tmp_path, evidence="evidence-v3", candidate=False)
    assert corpus.workflow_search(PROJECT, "pytest", env=env)["workflows"] == []


def test_approved_search_requires_task_terms_and_positive_limit(tmp_path):
    env, _ = _seed(tmp_path)
    for query, count in (("", 6), ("?!", 6), ("pytest", 0)):
        with pytest.raises(ValueError):
            corpus.workflow_search(PROJECT, query, limit=count, env=env)
