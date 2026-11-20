"""Project-scoped manual workflow-review state.

This module owns review validation and persistence, never ABPT extraction.
"""

import re
from datetime import datetime, timezone

REVIEW_FILE = "workflow-reviews.json"


def load_reviews(store):
    """Read the project ledger without modifying its contents."""
    return store.read(REVIEW_FILE) or {"records": {}}


def query_terms(text):
    """Case-folded whole-word terms shared by corpus and approved-step search."""
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


def review_status(candidate, history):
    """A decision is current only while its source fingerprint still matches."""
    if not history:
        return None
    record = history[-1]
    matches = candidate is not None and (
        record["evidence_digest"] == candidate["evidence_digest"]
    )
    return dict(
        record,
        state=record["decision"] if matches else "stale",
        history_count=len(history),
    )


def reviewed_candidate(candidate, records):
    return dict(
        candidate,
        review=review_status(candidate, records.get(candidate["candidate_id"], [])),
    )


def validate_review(decision, reason, approved_steps):
    if decision not in ("approved", "rejected"):
        raise ValueError("workflow decision must be approved or rejected")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a review reason is required")
    if decision == "approved":
        if (
            not isinstance(approved_steps, list)
            or not approved_steps
            or any(
                not isinstance(step, str) or not step.strip() for step in approved_steps
            )
        ):
            raise ValueError("approval requires a nonempty list of reviewed steps")
    elif approved_steps is not None:
        raise ValueError("a rejected workflow cannot have approved steps")


def store_decision(
    store,
    candidate,
    project_id,
    candidate_id,
    decision,
    evidence_digest,
    reason,
    approved_steps,
):
    """Append one explicit review; caller owns the project import lock."""
    if candidate["evidence_digest"] != evidence_digest:
        raise ValueError("workflow evidence changed; inspect the current candidate")
    ledger = load_reviews(store)
    record = {
        "candidate_id": candidate_id,
        "project_id": project_id,
        "evidence_digest": evidence_digest,
        "decision": decision,
        "approved_steps": approved_steps if decision == "approved" else None,
        "reason": reason.strip(),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    history = ledger.setdefault("records", {}).setdefault(candidate_id, [])
    history.append(record)
    store.write(REVIEW_FILE, ledger)
    return review_status(candidate, history)


def find_approved(candidates, episodes, records, terms):
    """Retrieve only current human-approved steps with traceable source IDs."""
    sources = {episode["id"]: episode for episode in episodes}
    matches = []
    required = set(terms)
    for candidate in candidates:
        review = review_status(candidate, records.get(candidate["candidate_id"], []))
        if review is None or review["state"] != "approved":
            continue
        steps = review["approved_steps"]
        if not required.issubset(query_terms(" ".join(steps))):
            continue
        matches.append(
            {
                "candidate_id": candidate["candidate_id"],
                "evidence_digest": candidate["evidence_digest"],
                "approved_steps": steps,
                "review_reason": review["reason"],
                "reviewed_at": review["reviewed_at"],
                "goal_outcome": "unproven",
                "sources": [
                    {
                        "conversation_id": source["conversation_id"],
                        "request_message_id": source.get("request_message_id"),
                        "steps": [
                            {
                                field: step.get(field)
                                for field in (
                                    "call_message_id",
                                    "result_message_id",
                                    "outcome",
                                    "source_refs",
                                )
                            }
                            for step in source["ordered_steps"]
                        ],
                    }
                    for source in (sources[eid] for eid in candidate["episodes"])
                ],
            }
        )
    return matches
