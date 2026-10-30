# Using module context and the architecture graph

**Proposed documentation — commands are not implemented in 0.7.0a3.**
Review alongside the [specification](spec.md), [delivery plan](plan.md) and
[agent skill](skills/sandloom-module-context/SKILL.md). This draft stays outside
the published website. It becomes public guides and CLI reference entries
when the matching tooling ships.

## Start with one module

Before changing a module, get its rules, public SDK signatures, contract/event
interfaces and selected source in one bounded local artifact:

```bash
uv run sandloom context notes
```

The proposed default is compact Markdown. It preserves Python code as code
blocks and avoids wrapping every line in JSON. A short header identifies the
module, framework/catalog version, selected scope, freshness and semantic digest.
The result also states which files were included or omitted. Rules and required
wire definitions cannot be silently dropped to fit the budget.

An AI agent can run the command itself and read its stdout. A person can read
or save the same result. No model service, credentials or hosted account are
required; the command does not submit content anywhere.

## Find modules and inspect connections

```bash
uv run sandloom graph --view modules
uv run sandloom graph --module module:notes --hops 1
```

Discovery returns compact JSON with stable module IDs, source availability,
counts and freshness. It is a catalog, not a graph claiming no relationships.
Choose one of those IDs for either command.

The focused graph contains typed module/contract/event/consumer/projection/
capability facts. One hop reaches neighboring modules and retains the contract
or channel explaining each connection. Required and optional dependencies remain
distinct; a missing provider is explicit. These are declared relationships from
an authoring composition. They do not prove that a call happened in production.

For a diagram a person can explore locally:

```bash
uv run sandloom graph --format html --output architecture.html
```

Open the resulting file yourself. The viewer works offline and includes search,
filters, evidence and a text table. The command does not open a browser.

## Choose a format for the caller

| Need | Proposed invocation |
| --- | --- |
| Context read directly by an agent or person | `sandloom context notes` |
| Typed context consumed by a program | `sandloom context notes --format json` |
| Compact declared relationships | `sandloom graph --module notes --hops 1` |
| Full context provenance and file hashes | `sandloom context notes --format json --detail full` |
| Full graph evidence records | `sandloom graph --detail full` |
| Offline visual inspection | `sandloom graph --format html --output architecture.html` |
| Mermaid interchange | `sandloom graph --format mermaid --module notes --hops 1` |

`--detail compact` is the default. It removes repetitive metadata, keeping
freshness, uncertainty, relation semantics, source references and omissions.
Full detail adds provenance; it does not select additional business code or
prove an unverified snapshot current. If its metadata exceeds the byte budget,
the command fails instead of evicting previously selected business content. JSON has an explicit versioned schema and
plain field names. Token savings depend on content and tokenizer; none are
claimed before equivalent fixtures are measured.

## Expand context deliberately

```bash
uv run sandloom context notes --file services.py --file tests/test_notes.py --max-bytes 131072
```

These are illustrative module-relative paths; select files that actually exist.
The default budget is 131072 **UTF-8 bytes**, including metadata and omissions,
not a token count. Optional whole sections may be omitted with a ledger. If
mandatory rules, explicitly requested files or transitive wire types exceed the
budget, the command fails and explains how much is required.

Request additional files from the selected module or increase the budget when
the task requires them. Other modules contribute public contract/event wire
interfaces, never their implementation bodies. Installed-only modules are not
editable context targets in the first version. Sensitive paths and symlink
escapes are rejected even when explicitly requested; this is not a complete
secret detector. Review source before sharing an artifact externally.

## Understand freshness

Inspection reads recorded semantic declarations and current selected source
without importing the app, loading `.env` or connecting to backends. It needs
an inspection index created by successful authoring generation. A local ignored
receipt can verify the selected input basis.

- `verified`: enumerated selected inputs match that successful generation.
- `missing_receipt`: recorded declarations can be read, but there is no local
  verification receipt, as on a fresh clone.
- `source_changed` or `dependency_changed`: the recorded verification basis differs.
- `partial`: the available basis does not cover the selected scope completely.

These states concern source evidence, not runtime health. Use
`--require-verified` when your workflow requires a verified basis; otherwise
read the explicit uncertainty before deciding what to rely on. Missing mandatory
interfaces fail even without strict verification.

A missing/stale index can require explicit `uv run sandloom generate`. Generation
writes managed files and uses the existing trusted authoring path; review its
diff and run it only in the appropriate environment. Inspection never performs
that recovery automatically. Ordinary implementation edits do not force an
unrelated generated-file gate failure just to update full-source hashes.

## Integrate with agent tools

Use `--format json` for structured success/errors; read the exit code and
versioned diagnostic kind rather than parsing an English message. Explicit
`--help` remains ordinary CLI help. All invocations are noninteractive.

| Exit | Meaning |
| --- | --- |
| 0 | Usable result; inspect warnings and omissions |
| 1 | Invalid/missing/unsafe inputs, incomplete mandatory context or I/O failure |
| 2 | CLI usage error |
| 3 | Strict verification requested but unavailable |

JSON mode emits one complete JSON response on stdout, including failures.
Markdown context failures without `--output` leave stdout empty and explain
the failure on stderr. Never treat a nonzero result as a partial successful bundle.
With `--output`, the file receives the artifact and stdout receives a small
JSON acknowledgement instead of a second copy; failures also use JSON. Replacing an existing file
requires `--force`; source and managed inputs cannot be output destinations.

The companion `sandloom-module-context` skill teaches discovery → focused
context/graph → inspect status/omissions → bounded expansion → module change →
existing gates. The release will include it in framework agent directories,
new applications and `sandloom add skills` for existing apps. Customized skill
files keep the existing preserve-unless-forced behavior.
