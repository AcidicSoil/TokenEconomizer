"""Compare ABPT-derived learning against canonical tool records, without modifying ABPT.

Example:
  python3 scripts/verify_abpt_evidence.py --database /path/to/local-chatgpt.db \
    --project PROJECT_ID:8 --project OTHER_ID:7 --report /tmp/abpt-proof.json
"""

import argparse
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tokeneconomizer.plugin import corpus
from tokeneconomizer.trace.abpt import (
    archive_revision,
    iter_conversations,
    open_archive,
    read_mapping,
)


def _message(raw_mapping, message):
    return (raw_mapping.get(message.get("metadata", {}).get("sourceNodeId")) or {}).get(
        "message"
    ) or {}


def source_counts(connection, project, limit, data_root):
    """Count only selected-branch invocations and matching canonical responses."""
    counted = Counter()
    permitted = set()
    for conversation, messages in iter_conversations(connection, project, limit):
        if conversation.get("captureState") != "complete":
            counted["incomplete"] += 1
            continue
        counted["sessions"] += 1
        permitted.add(conversation["id"])
        mapping = read_mapping(conversation, data_root)
        raw = {m["id"]: _message(mapping, m) for m in messages}
        results_by_parent = {}
        for m in messages:
            response = raw[m["id"]]
            if (response.get("author") or {}).get("role") == "tool":
                results_by_parent.setdefault(m.get("parentMessageId"), []).append(
                    response
                )
        for m in messages:
            if m["role"] != "tool":
                continue
            call = raw[m["id"]]
            if (call.get("author") or {}).get("role") != "assistant":
                continue
            recipient = call.get("recipient")
            if not recipient or recipient == "all":
                continue
            counted["tool_calls"] += 1
            output = next(
                (
                    r
                    for r in results_by_parent.get(m["id"], [])
                    if (r.get("author") or {}).get("name") == recipient
                ),
                None,
            )
            if output is None:
                counted["unverified_calls"] += 1
                continue
            counted["paired_calls"] += 1
            aggregate = (output.get("metadata") or {}).get("aggregate_result") or {}
            status = aggregate.get("status")
            if recipient == "python" and status == "failed_with_in_kernel_exception":
                counted["verified_failure_calls"] += 1
            elif (
                recipient == "python"
                and status == "success"
                and not aggregate.get("in_kernel_exception")
                and not aggregate.get("system_exception")
                and not aggregate.get("timeout_triggered")
            ):
                counted["verified_success_calls"] += 1
            else:
                counted["observed_output_calls"] += 1
    return dict(counted), permitted


def verify(database, scopes, report_path, data_root=None):
    database = Path(database).resolve()
    data_root = Path(data_root or database.parent.parent)
    report = {
        "database": str(database),
        "source": "read-only ABPT archive",
        "projects": [],
        "checks": {},
    }
    with (
        open_archive(database) as source,
        tempfile.TemporaryDirectory(prefix="te-abpt-evidence-") as state,
    ):
        initial = archive_revision(source)
        scoped_env = {"TOKENECONOMIZER_HOME": state}
        for project, limit in scopes:
            expected, permitted = source_counts(source, project, limit, data_root)
            learned = corpus.import_project(
                database, project, limit=limit, data_root=data_root, env=scoped_env
            )
            actual = learned["counts"]
            for name in (
                "sessions",
                "tool_calls",
                "paired_calls",
                "verified_success_calls",
                "verified_failure_calls",
                "observed_output_calls",
                "unverified_calls",
            ):
                assert actual.get(name, 0) == expected.get(name, 0), (
                    project,
                    name,
                    expected.get(name, 0),
                    actual.get(name, 0),
                )
            assert actual.get("verified_calls", 0) == (
                actual.get("verified_success_calls", 0)
                + actual.get("verified_failure_calls", 0)
            )
            results = corpus.context(project, limit=15, env=scoped_env)
            assert all(
                record["conversation_id"] in permitted for record in results["evidence"]
            ), "cross-project context leak"
            revision = corpus._snapshot_dir(corpus._scope(project, scoped_env), learned)
            with sqlite3.connect(revision / "evidence.db") as index:
                stored_ids = {
                    row[0]
                    for row in index.execute(
                        "select distinct conversation_id from snippets"
                    )
                }
            assert stored_ids <= permitted, "cross-project index leak"
            listing = corpus.workflow_candidates(
                project,
                limit=max(actual.get("workflow_candidates", 0), 1),
                env=scoped_env,
            )
            assert listing["total"] == actual.get("workflow_candidates", 0)
            assert (
                corpus.workflow_search(project, "python", env=scoped_env)["workflows"]
                == []
            ), "unreviewed history appeared in approved-only retrieval"
            for candidate in listing["candidates"]:
                assert candidate["project_id"] == project
                assert candidate["readiness"] == "observed"
                detail = corpus.workflow_detail(
                    project, candidate["candidate_id"], env=scoped_env
                )
                assert set(candidate["episodes"]) == {
                    episode["id"] for episode in detail["episodes"]
                }
                for episode in detail["episodes"]:
                    assert episode["project_id"] == project
                    assert episode["conversation_id"] in permitted
                    assert episode["goal_outcome"] == "unproven"
                    assert all(
                        step["call_message_id"] for step in episode["ordered_steps"]
                    )
            repeated = corpus.import_project(
                database, project, limit=limit, data_root=data_root, env=scoped_env
            )
            assert not repeated["changed"]
            assert repeated["revision_dir"] == learned["revision_dir"]
            report["projects"].append(
                {
                    "project_id": project,
                    "sample_limit": limit,
                    "source": expected,
                    "imported": actual,
                    "context_results": len(results["evidence"]),
                    "rendered_facts": learned["rendered"],
                    "repeat_sync_noop": True,
                }
            )
            print(
                project,
                "sessions",
                actual.get("sessions", 0),
                "calls",
                actual.get("tool_calls", 0),
                "paired",
                actual.get("paired_calls", 0),
                "verified",
                actual.get("verified_calls", 0),
                flush=True,
            )
        assert archive_revision(source) == initial, (
            "archive revision changed during test"
        )
    totals = Counter()
    for result in report["projects"]:
        totals.update(result["imported"])
    report["totals"] = dict(totals)
    report["checks"] = {
        "independent_canonical_oracle": True,
        "selected_branch_only": True,
        "context_and_index_isolation": True,
        "workflow_evidence_and_project_isolation": True,
        "workflow_goal_outcomes_unproven": True,
        "approved_retrieval_requires_review": True,
        "repeat_import_idempotent": True,
        "source_revision_stable": True,
        "production_state_untouched": True,
    }
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("PASS", dict(totals), "report", report_path)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--project", action="append", required=True, metavar="ABPT_PROJECT_ID:COUNT"
    )
    parser.add_argument("--data-root")
    parser.add_argument("--report", default="/tmp/te-abpt-evidence-replay.json")
    args = parser.parse_args(argv)
    scopes = []
    for token in args.project:
        project, separator, count = token.rpartition(":")
        if not separator or not project or not count.isdecimal() or int(count) < 1:
            parser.error("--project requires ABPT_PROJECT_ID:positive-count")
        scopes.append((project, int(count)))
    verify(args.database, scopes, args.report, args.data_root)


if __name__ == "__main__":
    main()
