# Evidence-backed workflow learning: design

Status: **candidate extraction, manual reviews, and approved-only search implemented**.
Live-agent injection and workflow execution remain unimplemented.

## Goal

Turn selected ABPT conversation histories into **reviewable, reusable procedures**
without mistaking a successful tool invocation for a completed user task. The
existing LLM-free environment-fact learner remains unchanged. No paid model
calls, external execution, or automatic context injection are required.

The authoritative inputs are the archived selected-path messages and canonical
snapshots already consumed by `tokeneconomizer.trace.abpt`. The existing
`tokeneconomizer.plugin.corpus` project scope and atomic revision publication
remain the storage boundary.

### User journey

1. Run `te corpus import --database DB --project PROJECT_ID`. The importer
   keeps source ABPT data read-only and produces an isolated project snapshot.
2. Run `te corpus workflows --project PROJECT_ID`. See candidate IDs, their
   observed tool sequences, and whether their outcomes are independently
   verified. There is no automatic recommendation.
3. Run `te corpus workflow show WORKFLOW_ID --project PROJECT_ID`. Inspect
   the exact conversation/message references, tool invocations, matching result
   IDs, output digests, and outcome classification.
4. Compare the observed steps with the originating goal. Approve a reviewed
   procedure using `te corpus workflow approve WORKFLOW_ID --project PROJECT_ID`
   with `--evidence-digest`, `--steps-file`, and `--reason`; or reject it with
   `te corpus workflow reject WORKFLOW_ID --project PROJECT_ID` and an evidence
   digest and reason. List past decisions using `te corpus workflow reviews`.
5. Query current approved procedures using
   `te corpus workflow search --project PROJECT_ID --query "pytest" --json`.
   Only a separate, future opt-in could enable live prompt injection.

Steps 1 through 5 are implemented; automatic injection remains disabled.
Tool outputs stay in the source-linked evidence index; the workflow snapshot
records result message IDs and digests rather than duplicating text.
Manual decisions live in `workflow-reviews.json` at the project scope root,
outside the derived revisions. Approving a procedure records reviewer-authored
steps, not evidence that the original user goal was completed.

## Evidence that bounds the design

The fixed translator and independent verification script
`scripts/verify_abpt_evidence.py` matched the raw ABPT source in a
20-conversation, three-project sample. The source had **84 tool invocations**,
**79 paired results**, **31 explicit Python execution successes**, **two Python
kernel exceptions**, **46 observed-only results**, and **five unverified calls**.
Two separate conversations have a Python failure followed by a Python execution
success. That establishes *execution chronology* but does **not** establish
that the second action fixed the same problem or completed the user's goal.

See [the evidence repair decision](ABPT_EVIDENCE_REPAIR.md) and
[the import contract](CORPUS_INTEGRATION.md).

### What is verified

- A canonical assistant tool invocation belongs to a specific tool identity.
- Its paired result belongs to that invocation through selected-branch parent
  linkage and a matching canonical tool identity.
- An explicit Python aggregate execution status can establish that the code
  executed successfully or raised an exception.
- Explicit process exit evidence can establish the process's exit status.

### What remains a claim or observation

- Assistant declarations such as "fixed", "done", or "all tests pass" are claims.
- A successful code execution alone does not prove an entire workflow worked.
- Browser/search output establishes only that an output was observed.
- A tool request without a paired result is unverified.
- A sequence of failed and successful calls does not, by itself, prove causation
  or general applicability.

These distinctions are part of the data model, not wording added at rendering.

## Competing designs

| Option | Source of steps | Main problem | Decision |
| --- | --- | --- | --- |
| A. Summarize whole conversations | Assistant prose and optional LLM proposal | Confuses intentions, claims and executions; may require model calls | Reject as authority |
| B. Build episodes from canonical tool events | Selected-path invocation/result pairs plus the user's request | Does not automatically explain *why* the procedure worked | **Choose**; keep that uncertainty explicit |
| C. Extend `EnvFactsDistiller` with procedure strings | Existing environment-fact observations | Its detector, promotion and firing rules model atomic environment facts | Reject; preserve its tested semantics |

An optional **local** model can later draft a title or a human-readable
description from an already extracted episode. Its output is annotation only.
It cannot change tool outcomes, insert unsupported steps, claim goal
completion, or promote a procedure.

## Data model

The existing `convert_conversation` emits `abpt_tool_call` and
`abpt_message`. Use those events rather than parsing ABPT a second time.

```python
# Proposed contracts, not executable code.
StepEvidence = {
    "tool_name": str,
    "call_message_id": str,
    "result_message_id": str | None,
    "outcome": "verified_success | verified_failure | observed_output | unverified",
    "outcome_evidence": str | None,
    "source_refs": list[str],
    "step_signature": str,  # normalized, safe action identity
}

ObservedEpisode = {
    "project_id": str,
    "conversation_id": str,
    "request_message_id": str | None,
    "ordered_steps": list[StepEvidence],
    "goal_outcome": "unproven",  # until separately evidenced
}

WorkflowCandidate = {
    "candidate_id": str,
    "project_id": str,
    "signature": str,           # stable within the owning project
    "evidence_digest": str,     # changes when the source evidence changes
    "episodes": list[str],      # independent source episode IDs
    "readiness": "observed | reviewable",
}

ReviewDecision = {
    "candidate_id": str,
    "project_id": str,
    "evidence_digest": str,
    "decision": "approved | rejected",
    "approved_steps": list[str] | None,
    "reason": str,
    "reviewed_at": str,         # UTC timestamp
}

# Display status: approved | rejected | stale.
# Each candidate retains a chronological list of review decisions.
```

Keep **execution outcome**, **goal outcome**, and **review state** separate.
A candidate can contain a verified Python failure followed by a verified
Python success and still have an unproven goal outcome.

`step_signature` is a conservative grouping key, not inferred semantics.
For existing classified shell commands, reuse the intent and form from
`microskills.observe`. For arbitrary Python code or browser actions, retain
the exact source identity and a fingerprint; do not generalize scripts by
guessing their purpose. Group only equivalent normalized actions within
the same project. Never silently group two projects.

## Ownership and data flow

```
ABPT SQLite + canonical snapshots (read-only)
           |
           v
trace.abpt.convert_conversation (existing, single source of truth)
           |
           +----> EnvFactsDistiller (existing and unchanged)
           |
           +----> workflow episode extractor (new)
                     |-- user-turn boundary (goal anchor, not proof)
                     |-- ordered canonical calls and results
                     |-- explicit outcome and source message IDs
                     v
                candidate builder (new)
                     |-- no invented steps
                     |-- no cross-project grouping
                     |-- candidate identity, sources, state
                     v
corpus/<project-digest>/revisions/<revision>/workflows.json (derived)
           |
           +----> existing atomic current.json publication
           |
           v
project-scoped CLI inspection and evidence retrieval (new)
           |
           v
persistent project-scoped manual review decisions (implemented)
```

Source-owned evidence remains immutable. Derived candidates belong to the
existing corpus revision. Human decisions must **not** live only inside a
rebuildable revision: store them in a separate project-scoped review ledger.
A replay keeps a decision only while the candidate's verified signature and
source evidence still match. A changed source marks the decision **stale**,
rather than silently reusing it.

### Proposed module boundary

- `tokeneconomizer/trace/abpt.py`: existing archive adapter; no new heuristic
  about the user's goals.
- `tokeneconomizer/plugin/workflows.py`: one owner for episode extraction,
  step signatures, candidates, and evidence-to-goal distinctions.
- `tokeneconomizer/plugin/corpus.py`: reuse the existing replay, revision and
  project-scope facilities; invoke the workflow builder while the same
  conversation events are in memory. Avoid a second ABPT database scan.
- `tokeneconomizer/plugin/workflow_reviews.py`: validate manual decisions,
  append review history, and mark old evidence as stale. The corpus adapter
  owns project-scoped locks and path selection.
- `tokeneconomizer/plugin/cli.py`: add narrow inspection and review commands.
  Keep live hooks and existing `te history` behavior untouched.
- `tests/test_workflows.py`: selected-branch episodes, evidence links,
  candidate lifecycle, and source-backed behavior.

The workflow extractor should consume `list[TraceEvent]` and return
`list[ObservedEpisode]`. The candidate builder receives episodes and returns
candidates. Neither function reads SQLite, writes files, runs tools, or calls a
model. Only the corpus adapter owns I/O.

## Candidate formation and review

1. Partition selected messages at source user-turn boundaries. When a source
   has no user anchor, keep its steps as evidence but mark the episode
   **unanchored** instead of inventing the goal.
2. Convert each canonical call and result into a step. Preserve call/result
   IDs, outcome evidence and recorded order. Unpaired tool output remains
   searchable evidence; it is not a successful step.
3. Identify observable patterns such as **failure followed by a later
   successful execution** in the same episode. Label them *observed recovery
   sequences*, not proven fixes. A sequence is a candidate, never an
   automatically active instruction.
4. Attach the source user's requested outcome as a **goal claim**. Separate
   evidence from statements about intent or completion.
5. Render a candidate only as a historical observation until a reviewer checks
   applicability, preconditions and scope. The reviewer may author a concise,
   parameterized procedure and approve it for reference.
6. Keep rejections, staleness and reviews separate from derived revisions.
   Preserve an explicit explanation for each decision. An approval does not
   authorize command execution or global injection.

For the initial version, do **not** copy the fact lifecycle's thresholds into
workflow learning. The existing `Lifecycle` promotes atomic facts after
measured confirmations and a firing gate. It does not establish that a
multi-step goal was accomplished. Establish reuse and applicability from
real replay results before designing automatic promotion criteria.

## Verification plan

1. **Red tests:** a canonical failure/success episode produces a candidate
   with both step references and `goal_outcome=unproven`. An unpaired tool
   result cannot become a successful step.
2. **Source oracle:** rerun the existing 20-conversation ABPT fixture through
   `scripts/verify_abpt_evidence.py`. Extracted calls and their outcomes must
   still match the independent raw-source totals (84 and 79). The two observed
   Python failure-to-success sequences remain observations, not verified
   goal-completion instructions.
3. **Review tests:** approving a candidate does not promote unrelated
   candidates or make it available in another project. Rejected candidates
   stay suppressed; changed source evidence stales a prior approval.
4. **Revision tests:** repeat import is a no-op, a changed ABPT journal
   replays derived candidates, failed replay preserves the previous pointer,
   and approved review records persist only against matching evidence.
5. **User-facing tests:** `te corpus workflows` and `workflow show` return
   exact conversation and message IDs. The existing `te corpus context`,
   environment facts, and all live hooks retain their current behavior.
6. **Resource contract:** no paid calls, no automatic shell execution, and no
   new dependencies for the deterministic extraction phase. Run targeted tests
   and the existing CLI and formal-verification skills; avoid full test suites.

## Implementation sequence

1. Add the episode extractor and read-only behavioral tests.
2. Add candidate grouping and derived storage to the existing corpus replay;
   validate the source oracle and idempotence.
3. Add project-scoped candidate inspection with source references.
4. Add the persistent review ledger and explicit approve/reject commands;
   verify source-change staleness.
5. Retrieve only currently approved procedures through precise keyword search.
   Automatic injection or model-assisted summaries require a separate decision.

**Implemented baseline:** `workflows.py` partitions user-anchored tool episodes,
groups exact ordered action signatures, and preserves source message IDs, output
digests, and separately classified execution outcomes. `corpus.py` writes
`workflows.json` inside each project revision; schema version 2 upgrades earlier
imports. The CLI exposes read-only `workflows` and `workflow show` commands.
The initial real replay of 20 conversations found 14 episodes and eight
candidates, including two observed recovery sequences. All goal outcomes
remain `unproven`. This is verified by `scripts/verify_abpt_evidence.py`.

**Implemented manual review ledger:** `corpus.review_workflow` uses
`workflow_reviews.py` to append approvals or rejections under a per-project
import/review lock. A valid approval
requires a matching evidence digest, an explicit reason, and nonempty reviewed
steps from a JSON array. Rejecting a candidate also requires a reason and the
inspected digest. `workflow_detail` and `workflow_reviews` expose full decision
history. When an ABPT replay changes the evidence or removes the candidate,
existing decisions remain on disk and display as `stale`; they are never silently
reapplied. The change is tested against both synthetic candidates and a real
archive re-import with a changed canonical execution outcome.

**Implemented approved-only retrieval:** `corpus.workflow_search` reads the
project's current candidate snapshot and private review ledger. It matches
case-folded, whole-word query terms only against human-authored approved steps,
requiring all terms to appear. The result contains reviewer-authored steps and
rationale, the evidence digest, and per-episode source identifiers, with the
historical goal outcome still `unproven`. Last-decision rejection, source
staleness, removed candidates, and projects outside the requested scope are
excluded. `te corpus workflow search` exposes this behavior without model
calls, execution, or prompt injection.

**Implemented labelled relevance evaluation:**
`tokeneconomizer/plugin/workflow_evaluation.py` and
`te corpus workflow evaluate --project ID --cases-file LABELS.json` compare
current approved-only search results against independent query-to-candidate
labels pinned to a specific corpus revision. The read-only report includes
per-query false positives and misses, precision@k, recall@k, positive-query
hit rate, and negative-query accuracy. It validates the project, revision,
and current approval state so changed source evidence cannot silently pass.

The synthetic regression benchmark demonstrates the expected exact-keyword
limitations: relevant paraphrases may be missed, and general keywords can
return irrelevant approved candidates. No real user-approved review ledger
exists in the configured production corpus at this stage, so **real-world
relevance remains unmeasured**. Next, obtain human-labelled queries for
actually reviewed procedures before considering automatic session integration.
