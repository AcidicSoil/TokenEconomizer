"""Read-only, labelled relevance evaluation over current reviewed procedures.

Evaluation labels are supplied independently; retrieval has no access to them.
No evaluator writes state, triggers execution, or promotes review decisions.
"""

from .workflow_reviews import find_approved, query_terms, review_status


def _ratio(part, whole):
    return part / whole if whole else None


def evaluate_project(project_id, labels, limit=6, env=None):
    """Measure top-k retrieval against explicit candidate-ID relevance labels."""
    from . import corpus

    if limit < 1:
        raise ValueError("evaluation count must be positive")
    if not isinstance(labels, dict) or not {
        "project_id",
        "revision_dir",
        "cases",
    }.issubset(labels):
        raise ValueError(
            "evaluation labels require project_id, revision_dir, and cases"
        )
    if not isinstance(labels["cases"], list):
        raise TypeError("evaluation labels cases must be a list")
    if labels["project_id"] != project_id:
        raise ValueError("evaluation project does not match the requested project")
    state, data = corpus._read_workflow_snapshot(project_id, env)
    if labels["revision_dir"] != state["revision_dir"]:
        raise ValueError("evaluation labels reference a different corpus revision")

    records = corpus._review_records(project_id, env)
    approved = {
        candidate["candidate_id"]
        for candidate in data["candidates"]
        if (
            review := review_status(
                candidate, records.get(candidate["candidate_id"], [])
            )
        )
        is not None
        and review["state"] == "approved"
    }
    if not approved:
        raise ValueError("evaluation requires current approved workflows")
    cases = labels["cases"]
    if not cases:
        raise ValueError("evaluation requires labelled query cases")

    results = []
    tp = fp = fn = positive = hits = negative = true_negative = 0
    for case in cases:
        if not isinstance(case, dict) or not {
            "query",
            "relevant_candidate_ids",
        }.issubset(case):
            raise ValueError(
                "each labelled case requires query and relevant_candidate_ids"
            )
        query = case["query"]
        ids = case["relevant_candidate_ids"]
        if (
            not isinstance(query, str)
            or not isinstance(ids, list)
            or any(not isinstance(item, str) for item in ids)
        ):
            raise ValueError("labelled cases need a string query and candidate-ID list")
        terms = query_terms(query)
        if not terms:
            raise ValueError("labelled query must contain search terms")
        expected = set(ids)
        if not expected.issubset(approved):
            raise ValueError("relevance labels must name current approved candidates")
        returned = [
            item["candidate_id"]
            for item in find_approved(
                data["candidates"], data["episodes"], records, terms
            )[:limit]
        ]
        returned_set = set(returned)
        correct = returned_set & expected
        missed = expected - returned_set
        unexpected = returned_set - expected
        tp += len(correct)
        fp += len(unexpected)
        fn += len(missed)
        if expected:
            positive += 1
            hits += bool(correct)
        else:
            negative += 1
            true_negative += not returned
        results.append(
            {
                "query": query,
                "expected_ids": sorted(expected),
                "returned_ids": returned,
                "correct_ids": sorted(correct),
                "missed_ids": sorted(missed),
                "unexpected_ids": sorted(unexpected),
            }
        )

    return {
        "evaluation_scope": "labelled_approved_steps",
        "project_id": project_id,
        "revision_dir": state["revision_dir"],
        "k": limit,
        "approved_candidates": len(approved),
        "cases_evaluated": len(cases),
        "precision_at_k": _ratio(tp, tp + fp),
        "recall_at_k": _ratio(tp, tp + fn),
        "hit_rate_at_k": _ratio(hits, positive),
        "negative_query_accuracy": _ratio(true_negative, negative),
        "totals": {
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
        },
        "cases": results,
    }
