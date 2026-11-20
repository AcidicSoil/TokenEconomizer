"""Regression coverage for labelled, approved-workflow relevance benchmarks."""

import json
import os
import subprocess
import sys

import pytest
from test_workflow_reviews import CANDIDATE_ID, EVIDENCE_DIGEST, PROJECT, _seed

from tokeneconomizer.plugin import corpus
from tokeneconomizer.plugin.store import Store
from tokeneconomizer.plugin.workflow_evaluation import evaluate_project


def _labels(tmp_path):
    env, store = _seed(tmp_path)
    state = store.read("current.json")
    derived = Store(store.path("revisions", state["revision_dir"]))
    data = derived.read("workflows.json")
    second = dict(data["candidates"][0])
    second["candidate_id"] = "candidate-two"
    second["evidence_digest"] = "second-evidence"
    second["episodes"] = ["episode-two"]
    data["candidates"].append(second)
    episode = dict(data["episodes"][0])
    episode["id"] = "episode-two"
    episode["conversation_id"] = "conversation-two"
    data["episodes"].append(episode)
    derived.write("workflows.json", data)
    corpus.review_workflow(
        PROJECT,
        CANDIDATE_ID,
        "approved",
        EVIDENCE_DIGEST,
        reason="Reviewed fixture A",
        approved_steps=["Run pytest for focused tests", "Inspect trace outputs"],
        env=env,
    )
    corpus.review_workflow(
        PROJECT,
        "candidate-two",
        "approved",
        "second-evidence",
        reason="Reviewed fixture B",
        approved_steps=["Run pytest checks"],
        env=env,
    )
    labels = {
        "project_id": PROJECT,
        "revision_dir": state["revision_dir"],
        "cases": [
            {"query": "pytest", "relevant_candidate_ids": [CANDIDATE_ID]},
            {"query": "TRACE", "relevant_candidate_ids": [CANDIDATE_ID]},
            {"query": "unit", "relevant_candidate_ids": [CANDIDATE_ID]},
            {"query": "checks", "relevant_candidate_ids": []},
            {"query": "unrelated", "relevant_candidate_ids": []},
        ],
    }
    return env, labels


def test_evaluator_measures_relevance_without_claiming_success(tmp_path):
    env, labels = _labels(tmp_path)
    report = evaluate_project(PROJECT, labels, limit=2, env=env)
    assert report["project_id"] == PROJECT
    assert report["revision_dir"] == labels["revision_dir"]
    assert report["approved_candidates"] == 2
    assert report["cases_evaluated"] == 5
    assert report["precision_at_k"] == pytest.approx(0.5)
    assert report["recall_at_k"] == pytest.approx(2 / 3)
    assert report["hit_rate_at_k"] == pytest.approx(2 / 3)
    assert report["negative_query_accuracy"] == pytest.approx(0.5)
    assert report["totals"] == {
        "true_positives": 2,
        "false_positives": 2,
        "false_negatives": 1,
    }
    assert report["cases"][0]["returned_ids"] == [CANDIDATE_ID, "candidate-two"]
    assert report["cases"][2]["missed_ids"] == [CANDIDATE_ID]
    assert report["cases"][3]["unexpected_ids"] == ["candidate-two"]
    assert report["cases"][4]["returned_ids"] == []
    assert "goal_outcome" not in report
    assert report["evaluation_scope"] == "labelled_approved_steps"


def test_evaluation_ranks_only_up_to_limit(tmp_path):
    env, labels = _labels(tmp_path)
    report = evaluate_project(PROJECT, labels, limit=1, env=env)
    assert report["precision_at_k"] == pytest.approx(2 / 3)
    assert report["recall_at_k"] == pytest.approx(2 / 3)
    assert report["k"] == 1
    assert report["cases"][0]["returned_ids"] == [CANDIDATE_ID]


def test_evaluation_rejects_stale_and_unreviewed_labels(tmp_path):
    env, labels = _labels(tmp_path)
    corpus.review_workflow(
        PROJECT,
        CANDIDATE_ID,
        "rejected",
        EVIDENCE_DIGEST,
        reason="Not actually usable",
        env=env,
    )
    with pytest.raises(ValueError, match="approved"):
        evaluate_project(PROJECT, labels, env=env)
    assert corpus.workflow_search(PROJECT, "trace", env=env)["total"] == 0

    fresh_env, fresh_labels = _labels(tmp_path / "fresh")
    fresh_labels["revision_dir"] = "stale-revision"
    with pytest.raises(ValueError, match="revision"):
        evaluate_project(PROJECT, fresh_labels, env=fresh_env)


def test_evaluator_validates_project_and_labelled_cases(tmp_path):
    env, labels = _labels(tmp_path)
    wrong_project = dict(labels, project_id="other-project")
    with pytest.raises(ValueError, match="project"):
        evaluate_project(PROJECT, wrong_project, env=env)
    with pytest.raises(ValueError, match="positive"):
        evaluate_project(PROJECT, labels, limit=0, env=env)

    unapproved_env, _ = _seed(tmp_path / "no-review")
    with pytest.raises(ValueError, match="approved"):
        evaluate_project(PROJECT, labels, env=unapproved_env)

    unknown = dict(
        labels,
        cases=[{"query": "pytest", "relevant_candidate_ids": ["unknown-id"]}],
    )
    with pytest.raises(ValueError, match="approved"):
        evaluate_project(PROJECT, unknown, env=env)


def test_cli_evaluate_reports_metrics_without_writing(tmp_path):
    env, labels = _labels(tmp_path)
    cases_file = tmp_path / "labelled-cases.json"
    cases_file.write_text(json.dumps(labels), encoding="utf-8")
    before = corpus._scope(PROJECT, env).read("workflow-reviews.json")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tokeneconomizer.plugin.cli",
            "corpus",
            "workflow",
            "evaluate",
            "--project",
            PROJECT,
            "--cases-file",
            str(cases_file),
            "--count",
            "2",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, **env),
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["precision_at_k"] == pytest.approx(0.5)
    assert report["cases_evaluated"] == 5
    assert corpus._scope(PROJECT, env).read("workflow-reviews.json") == before


def test_negative_only_benchmark_reports_undefined_positive_metrics(tmp_path):
    env, labels = _labels(tmp_path)
    labels["cases"] = [
        {"query": "unrelated", "relevant_candidate_ids": []},
    ]
    report = evaluate_project(PROJECT, labels, env=env)
    assert report["precision_at_k"] is None
    assert report["recall_at_k"] is None
    assert report["hit_rate_at_k"] is None
    assert report["negative_query_accuracy"] == 1.0


def test_malformed_labels_report_user_error(tmp_path):
    env, labels = _labels(tmp_path)
    for bad in (None, {}, dict(labels, cases=[{"query": "pytest"}])):
        with pytest.raises(ValueError, match="label"):
            evaluate_project(PROJECT, bad, env=env)
