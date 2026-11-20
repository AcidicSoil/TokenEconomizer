# ABPT tool-evidence repair plan

## Goal and reviewable user journey

Run `te corpus import --database DB --project ID` in a temporary TokenEconomizer home. Search with `te corpus context --project ID --query QUERY`. A tool observation must cite an archived message and, when available, the canonical snapshot. A repeated sync must leave the imported revision unchanged. A Python exception must be reported as a failed execution; a browser result without an execution status must remain observed-only.

**Definition of done:** in the same 20 complete conversations from three ABPT projects, align every tool invocation and result with its selected-path source nodes. Count source-verified outcomes only when the tool's own execution record supports them; prove that previous unpaired tool outputs without an invocation remain independent; keep the source archive and normal TokenEconomizer state untouched.

## Findings before changes

Baseline from `/tmp/te-corpus-larger-batch-report.json` (previous import), raw provenance audit `/tmp/te-abpt-provenance-audit.json`, and status audit `/tmp/te-abpt-outcome-audit.json`:

- 84 raw assistant-to-tool invocations, including one empty-input `web.run` call.
- 79 raw tool results are direct children of those invocations. Both ABPT's archived parent IDs and the raw ChatGPT graph parent IDs identify the same 79 pairs, with matching tool names.
- Before repair, the importer paired 38 results because it required `metadata.toolArtifactKind == "execution-output"`.
- 33 Python tool results expose `metadata.aggregate_result.status`: 31 `success`, two `failed_with_in_kernel_exception` with structured exception objects.
- Five `container.exec` results contain `aggregate_result.status=success`, but the archived output does not supply a process exit code. Do not confuse successful tool delivery with command success.
- Other `web.run`, browser, computer, and search outputs have no demonstrated execution-status contract. Keep them observed-only.
- The free `EnvFactsDistiller` learns environment facts from classified shell commands. Improving result provenance need not produce an injectable fact from browsing/Python sessions.

## Design alternatives

**A. Join selected archive messages to raw provenance (chosen).** Keep `archive_messages` as the authority for selected-branch identity/order. Enrich those messages from the canonical mapping by `sourceNodeId`. Pair a tool-authored result only with the directly preceding archived parent if the parent's raw author is `assistant` and its recipient equals the result's author name. Keep the existing explicit `execution-output` fallback for archives lacking canonical details. Read Python execution status from the raw result's `aggregate_result`. The change stays in `tokeneconomizer/trace/abpt.py`.

**B. Replay the full canonical ChatGPT graph.** Walk `payload.mapping` / `payload.conversationJson.mapping` and generate all calls and results. This duplicates ABPT's selection rules and risks alternate-branch contamination. In the measured sample it finds no more valid pairs than A.

A synthetic Claude/Codex transcript is also rejected: it invents semantics that the ChatGPT source did not record.

## Contract and ownership

```
ABPT selected messages (identity, order, branch) + canonical mapping (author,
recipient, parent, execution result)
   -> convert_conversation
      abpt_tool_call { tool_name, tool_args, message_id, result_id?,
                       outcome, outcome_evidence?, exit_code?, evidence_refs }
      abpt_message { role: user | assistant | tool, artifact_kind?, evidence_refs }
   -> existing trace loader / existing free learner / private search index
   -> te corpus context
```

Canonical tool status is evidence of execution, not proof that a user-facing goal was solved. `finished_successfully` on a ChatGPT message is *not* command success. Only Python's observed explicit `aggregate_result` status and explicit process exit status may establish execution success/failure. The `container.exec` wrapper's `success` is not a process exit code. Unpaired results cannot yield successful-call evidence.

## Figure-it-out execution units

1. **Baseline:** record raw count, paired count, status distribution and source mismatch counts; freeze a repeatable diagnostic. Gate: raw provenance read-only and branch-scoped.
2. **Prove the bug:** focused tests with canonical `assistant` recipient and direct `tool` result lacking `toolArtifactKind`; Python structured success, structured kernel exception, and wrapper success without exit code. Gate: tests fail against the old converter.
3. **Minimal fix:** in `trace/abpt.py`, join by real author/recipient/parent and consume Python execution status. No new memory engine or changed learning thresholds. Gate: focused tests pass.
4. **Real replay:** rerun the same 20 conversation sample in isolated TokenEconomizer state. Compare pairing and outcomes to the raw source audit. Gate: no invented successes, all links source-backed, no project crossover, sync idempotent.
5. **Close:** focused tests, lint, diff check, docs, and a decision log. Report verified, unknown and unlearnable signals separately.

## Rigor and risk

Moderate-high, because mislabeling a tool failure as success can teach an incorrect environment fact. All writes are to derived scratch state or the local code checkout; ABPT and live agent hooks remain unchanged. No Repowise index is configured; do not initialize without approval. Only focused tests run.

## Verified results

The 20-conversation replay matched an independent canonical-source oracle:
84 invocations, 79 paired results, 31 explicit Python execution successes,
two explicit Python kernel failures, 46 observed-only results, and five
invocations with no recorded result. Each project imported twice without
replaying unchanged state. Project-scoped context and evidence-index ownership
held; ABPT's source journal remained unchanged. No existing live
TokenEconomizer state was modified. The free learner generated no injectable
facts, as these conversations did not contain repeated verified shell-environment
repairs. The `container.exec` wrapper's `success` is still *not* treated as
shell-process success.

Rerun:

```bash
python3 scripts/verify_abpt_evidence.py --database /path/to/local-chatgpt.db --project PROJECT_A:8 --project PROJECT_B:7 --project PROJECT_C:5 --report /tmp/te-abpt-evidence-replay.json
```

## Evidence

Raw-source audit: `/tmp/te-abpt-provenance-audit.json`

Status audit: `/tmp/te-abpt-outcome-audit.json`

Run decision trail: `/tmp/te-abpt-evidence-decisions.tsv`

Target replay proof: `/tmp/te-abpt-evidence-replay.json`
