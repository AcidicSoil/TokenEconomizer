# Trace

The one place that knows each agent's session-log format. Every mechanism reads a finished
session as an ordered list of `(tool_name, args, result_text, success)` calls through
`load_trajectory`, whichever agent wrote it.

| Module | |
|---|---|
| `calls.py` | `load_trajectory` (format sniffed per file), `call_cwds`, `tool_usage`, `strip_rtk_prefix` |
| `abpt.py` | Read-only translation of ABPT's `local-chatgpt.db` into conversation, tool-call and source-evidence JSONL; uncertain outcomes stay unknown |
| `catalog.py` | the tool list a session loaded, from whichever source the agent publishes |
| `antigravity.py` | Antigravity CLI (`agy`) transcripts: results paired with their calls, read whole by the fold and a few steps at a time by the hook; usage |
| `claude.py` | Claude Code transcripts: calls with the shell's directory, usage by message id, subagents, deferred tools |
| `codex.py` | `codex exec --json` streams; a session's rollout file, converted to that stream; usage |
| `copilot.py` | Copilot CLI event logs: calls, usage, prompt-turns, MCP servers |
| `cursor.py` | Cursor Agent hook events, normalized to a Claude-shaped trajectory; usage is unavailable |
| `droid.py` | Factory Droid sessions: one JSONL file per session, its message records unwrapped into a Claude transcript; prompts; usage from the `<session id>.settings.json` beside it |
| `grok.py` | Grok Build sessions: `updates.jsonl` (ACP updates) written as a Claude transcript; prompts and usage from its session files |
| `hermes.py` | Hermes Agent sessions: a copy of a session's `state.db` rows without the conversation's words, written as a Claude transcript; usage |
| `pi.py` | Pi's session tree: the active branch, written as a Claude transcript; usage |
| `jsonl.py` | line and record readers shared by the above |

A new agent needs a reader here and nothing else: once its log reads as `(name, args, result,
ok)` calls, every mechanism handles it.

## ABPT conversation corpus

Translate the existing archive without contacting ChatGPT or modifying ABPT:

```sh
python3 -m tokeneconomizer.trace.abpt /path/to/abpt/runs/.abpt/local-chatgpt.db translated-sessions/ --limit 10
```

Omit `--limit` to export the full corpus, or pass `--conversation-id ID` for one.
Each conversation gets its own stable, private JSONL file. Output retains user/assistant
messages, project and conversation IDs, capture status,
source evidence references, and tool activity. An `execution-output` child of a tool
message is paired with its parent call. Unpaired calls remain `unverified`; observed
output does not imply a successful execution. ABPT records without a tool name use
`chatgpt.tool` in the shared trajectory reader, without inventing shell commands,
exit codes or model token usage. Use `te corpus import --database DB --project ID`
for project-scoped historical learning, and `te corpus context --project ID`
for source-cited retrieval. See [corpus integration](../../docs/CORPUS_INTEGRATION.md).
The standalone trace export does not fold repository learning by itself.
