# ABPT historical corpus integration

## Scope and ownership

ABPT's existing `runs/.abpt/local-chatgpt.db` and the canonical snapshots under
`runs/archive-sync/canonical/` remain the authoritative source. TokenEconomizer
reads them using SQLite read-only access and the supplied evidence references.
No ChatGPT network capture, browser credentials, paid model runner, or parallel
conversation crawler is involved.

TokenEconomizer owns only the derived, private learning and evidence index
under `$TOKENECONOMIZER_HOME/corpus/<project-digest>/`. ABPT project IDs
are the scope boundary. A repository binding is accepted only when the caller
supplies `--repo` with an existing Git checkout.

## Usage

From the TokenEconomizer checkout:

```bash
DB=/home/user/projects/temp/ai-apps/.personal-projects/abpt/runs/.abpt/local-chatgpt.db

python3 -m tokeneconomizer.plugin.cli corpus inspect --database "$DB"
python3 -m tokeneconomizer.plugin.cli corpus inspect --database "$DB" --project PROJECT_ID
python3 -m tokeneconomizer.plugin.cli corpus import --database "$DB" --project PROJECT_ID --dry-run
python3 -m tokeneconomizer.plugin.cli corpus import --database "$DB" --project PROJECT_ID
python3 -m tokeneconomizer.plugin.cli corpus context --project PROJECT_ID --query "pytest"
python3 -m tokeneconomizer.plugin.cli corpus sync --project PROJECT_ID
python3 -m tokeneconomizer.plugin.cli corpus status --project PROJECT_ID
```

Use `--repo /path/to/repository` on import when the project is known to belong
to that Git checkout. Use `--data-root` when the canonical snapshots are not
under the database's parent `runs/` directory. A bounded exploratory import
can specify `--limit N`, producing an explicitly limited snapshot.

The ChatGPT/RDC adapter is the CLI: request `te corpus context --project ...`
through the already connected Remote Desktop Commander. This returns
historical context with source identifiers. Automatic session start hooks or
ChatGPT-side plugin registration are separate integration work.

## Data flow

1. Read one SQLite snapshot transaction for the source journal identity and
   the selected project's archived conversations, ordered by creation time.
2. Read each conversation's selected messages from `archive_messages`.
   ABPT has already selected the branch. Preserve its order and message IDs.
3. Enrich tool calls from canonical snapshots via `metadata.sourceNodeId`,
   including the actual `recipient`, structured input and the tool's output.
   Snapshots support both `payload.mapping` and `payload.conversationJson.mapping`.
   Canonical messages authored by tools are outputs, not tool invocations.
4. Classify outcomes from explicit process exit codes or structured Python
   `aggregate_result.status` (`success` or `failed_with_in_kernel_exception`).
   A ChatGPT message's `finished_successfully` status is **not** execution
   success. A `container.exec` tool-status `success` does not establish the
   shell process exit code. Other results remain observed-only.
5. Feed recognized shell commands through TokenEconomizer's existing
   `EnvFactsDistiller`, using the generic vocabulary. The original free
   learning thresholds remain authoritative; no custom learning algorithm
   or paid calls are introduced.
6. Store source-referenced excerpts in a private FTS5 evidence index.
   Assistant text is labeled `assistant_claim` and remains unverified.
   Paired tool results are labeled `tool_observation`; unpaired tool
   messages are labeled `unpaired_tool_output` and never count as calls.
7. Publish a complete revision directory, then atomically update
   `current.json`. Readers continue using the previous revision until
   the pointer changes.

## Update and recovery policy

Each snapshot has the ABPT `archive_journal_identity.journal_id` and maximum
`archive_change_log.sequence`. When those are unchanged, sync is a no-op.
When the journal changes, replay the affected project scope oldest-first into
a new derived revision. This deliberately rebuilds the scope because an older
conversation can correct a fact learned from later conversations. The
publication pointer changes only after successful replay.

A failed replay leaves the previous pointer usable. Concurrent imports for
one project are serialized by TokenEconomizer's existing `Store.locked`.
The old revision remains on disk for any concurrent reader.

The current journal check is global, so a change to another ABPT project
can cause an unnecessary replay of this project's data. It cannot merge the
projects. Optimize this only with a proven per-project change query that also
handles deletes and journal resets.

## Learning and evidence contracts

- Learn only evidence supported by an observed tool call and result.
- Preserve source conversation, message, account and project identity.
- Count explicit successes and failures separately from observed output.
- Keep proposed or assistant-reported fixes as conversation text, not facts.
- Never copy a project fact into another project's context.
- Never apply ChatGPT historical tool pruning to Claude/Codex settings.
- A historical corpus import does not alter `te history import`'s existing
  first-fold-only behavior or any live agent hook.

## Verification

Run focused regression tests and the two generated pstack verification skills:

```bash
uv run --no-project --offline --with pytest pytest -q tests/test_abpt_corpus.py tests/test_corpus_import.py
python3 .cursor/skills/verify-tokeneconomizer/scripts/verify_cli.py --evidence-dir /tmp/te-corpus-verification
python3 .cursor/skills/verify-corpus-import/scripts/check_invariants.py --evidence-dir /tmp/te-corpus-verification
```

The CLI check proves a rendered `absent_tool:pytest` fact after four
source-backed, explicit tool failures. The bounded checker covers all 27
three-action import/revision/failure sequences and catches a deliberate stale
revision mutation. These checks do not establish that an arbitrary historical
conversation has enough verified tool evidence to create a learned fact.

## Proposed workflow learning

[Evidence-backed workflow learning](WORKFLOW_LEARNING_DESIGN.md) implements
candidate-only procedure extraction over the same canonical events. Derived
`workflows.json` is stored in each atomic project revision; schema version 2
rebuilds earlier imported revisions once. The inspection commands are:

```bash
te corpus workflows --project PROJECT_ID --json
te corpus workflow show CANDIDATE_ID --project PROJECT_ID --json
```

Manual approval and rejection are available through a private, persistent
`workflow-reviews.json` ledger outside the revision directories:

```bash
te corpus workflow approve CANDIDATE_ID --project PROJECT_ID --evidence-digest DIGEST --steps-file procedure.json --reason "Checked the source"
te corpus workflow reject CANDIDATE_ID --project PROJECT_ID --evidence-digest DIGEST --reason "Unsupported procedure"
te corpus workflow reviews --project PROJECT_ID --json
```

`procedure.json` is a JSON array of reviewed step strings. The evidence digest
is returned by `te corpus workflows` and `te corpus workflow show`. The ledger
keeps every decision; `show` and `reviews` expose its history. A source update
that changes the candidate's digest marks the prior decision **stale** until
explicitly reviewed again. Candidate observations still do not prove goal
completion.

## Approved-only workflow retrieval

```bash
te corpus workflow search --project PROJECT_ID --query "pytest" --count 6 --json
```

This read-only command searches **only reviewer-approved step text** in the
named project. It requires every query keyword to match a whole word,
case-insensitively, across the approved steps. This is precise keyword search,
not semantic matching; use key terms rather than a full natural-language
question. The result includes the reviewed steps, rationale, evidence digest,
conversation IDs, call/result message IDs, and source references. Historical
assistant claims are excluded from the retrieved procedure, and the original
goal outcome remains labeled `unproven`.

Unreviewed, rejected, stale, and missing candidates never appear. An unchanged
explicit approval survives corpus replays. The command performs no tool
execution, live injection, or model calls. Existing environment facts and live
hooks are unchanged.


## Labelled relevance evaluation

Use the approved-review list and corpus status to construct an **independent**
query/relevance dataset. Labels must be human-judged: approval of a procedure
and relevance to a particular query are different decisions. Save a JSON file:

```json
{
  "project_id": "PROJECT_ID",
  "revision_dir": "CURRENT_REVISION_DIR",
  "cases": [
    {"query": "pytest", "relevant_candidate_ids": ["REVIEWED_CANDIDATE_ID"]},
    {"query": "unrelated topic", "relevant_candidate_ids": []}
  ]
}
```

```bash
te corpus status --project PROJECT_ID --json
te corpus workflow reviews --project PROJECT_ID --json
te corpus workflow evaluate --project PROJECT_ID --cases-file labels.json --count 6 --json
```

The evaluator reads the current revision and project ledger without modifying
either. It rejects changed revisions and labels pointing outside the current
approved candidate set. Each case reports retrieved, correct, missed, and
unexpected IDs. Aggregate metrics include **micro precision@k**, **micro
recall@k**, **hit rate@k** across queries with relevant labels, and
**negative-query accuracy** across queries with no relevant labels. A metric
with a zero denominator is `null`, not a fabricated perfect score.

This measures deterministic keyword filtering and the existing candidate
order, not semantic relevance ranking. The tests use clearly labelled fixture
approvals; those fixture scores are not measurements of real human-reviewed
procedures. The configured local corpus has no persistent approved-workflow
ledger yet. Human-labelled production cases are required before evaluating
real-world relevance or considering automatic session integration.
