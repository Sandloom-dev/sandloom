---
title: Module context for agents
---

# Module context for agents

Collect the rules, public interfaces and selected source needed to change one
business module. An AI agent can invoke the command itself; people can read the
same artifact. No model service or hosted account is required.

!!! note "Package availability"
    Added after 0.7.0a3. Use the current source checkout until the next PyPI release.

From the application or framework root:

```bash
uv run sandloom graph --view modules
uv run sandloom context notes
```

Choose an actual module ID/name from discovery. The default is compact Markdown:
a short status header, applicable scoped instructions, beginner/used advanced SDK
signatures, required contract and event wire types, followed by optional owned
source/tests. Source code remains in fenced blocks. Other modules contribute
public interfaces, never their implementation bodies.

## Choose a format and scope

```bash
uv run sandloom context module:notes --format json
uv run sandloom context notes --file services.py --max-bytes 131072
uv run sandloom context notes --format json --detail full
uv run sandloom context notes --focus contract:stats.quota.v1
```

`--file` is repeatable and relative to the selected module; choose files that
exist. JSON exposes typed sections, source locators and diagnostics for programs.
Full detail adds file hashes, inclusion reasons and verification evidence. It
keeps the same business selection as compact mode; if the additional metadata
exceeds the budget it fails instead of silently evicting source.

`--focus` selects an entity/process used or owned by that module: contract, event,
consumer, projection, saga, route or module. Consumer/projection focus includes
its declared input event interfaces; infrastructure focus retains the whole module. Whole selected wire files and their transitive DTO closure remain
mandatory. Saga focus includes its declared command interfaces; route internals
are opaque, so route focus retains every declared interface. Rules and the public
SDK remain available. Owned source matches are navigation hints, not a complete
call graph: add helpers/tests explicitly with `--file`. `outside_focus` omissions
make that limit visible. Focus never authorizes editing another module.

The default limit is **131072 UTF-8 bytes**, counting the complete serialized
output, not a token count. Rules, explicitly requested files and transitive DTO
closure are mandatory. An oversized mandatory section fails with the required
size. Optional whole files can be omitted with an explicit ledger.

A project `.sandloomignore` contains project-relative glob patterns, one per line;
blank lines and `#` comments are ignored. It excludes optional context files and
blocks explicit selection of those files. It does not remove mandatory scoped
rules or public wire interfaces. Negation/re-inclusion syntax is not supported.

Sensitive paths, binaries and symlink traversal are excluded or rejected even for
explicit selections. Credential pattern checks reduce accidental exposure; they
are not a complete secret detector. Review source before sharing an artifact.
The tool writes locally and never sends context to a model automatically.

## Read freshness before relying on the result

Inspection reads `.sandloom/inspection.json`, produced by explicit
`sandloom generate`, plus current selected source. It does not load `.env`, import
the application or connect to backends. The semantic index belongs in Git.
A local verification receipt lives under ignored `.sandloom/cache/`. Receipt
entries are per module: an unavailable optional provider does not prevent an
independent module from verifying. Incomplete scopes stay explicitly unverified.

| State | Meaning |
| --- | --- |
| `verified` | Enumerated selected inputs match a successful authoring generation. |
| `missing_receipt` | Recorded declarations are readable, but no local receipt exists, e.g. a fresh clone. |
| `source_changed` | Source membership/content or scoped instructions changed. |
| `dependency_changed` | Wire/interface verification basis changed. |
| `partial` | Selected modules have different verification states. |

This is source evidence, not runtime health or a proof of arbitrary
configuration-dependent Python behavior. Default exports label uncertainty;
`--require-verified` exits 3 unless the selected basis is verified.
Missing mandatory interfaces fail regardless of that flag. A malformed or
unsupported receipt is rejected with a typed error; it can never establish
verification. Explicit generation recreates the receipt.

A missing index or changed layout requires explicit generation:

```bash
uv run sandloom generate
uv run sandloom context notes --require-verified
```

Generation uses the existing trusted application loading path and writes managed
artifacts; review its diff. Inspection never performs this recovery automatically.
Ordinary implementation-only edits do not change the semantic index merely due
to their content hashes. `generate --check` and `sandloom test` remain read-only.

## Save and automate

```bash
uv run sandloom context notes --output notes-context.md
```

The file receives the context; stdout receives one small JSON acknowledgement.
Existing outputs require `--force` and must be recognized Sandloom inspection
exports. Arbitrary existing files, project instructions, source/policy/generated
inputs and version-control/tool directories cannot be replaced. If an older file
has no recognized export marker, choose a new destination. A destination changed
by another process during export is preserved.
Without `--output`, explicit JSON returns one success/error envelope on stdout.
Default Markdown errors leave stdout empty and explain on stderr. Programs should
select JSON and branch on typed diagnostic kinds and exit codes, not English text.
See [CLI options and exit codes](../reference/cli.md#context-and-declared-architecture).

## Agent skill

`sandloom-module-context` is supplied in framework `.agents/skills` and
`.claude/skills`, new applications, and `sandloom add skills` for existing apps.
Customized skill files retain the existing preserve-unless-forced behavior.
The skill guides discovery, scoped context/graph inspection, freshness checks,
bounded expansion and the project's existing quality gates. Installed-only
providers contribute interfaces; context targets require checkout-owned source.

Continue with the [architecture graph](architecture-graph.md),
[generation workflow](generate.md) and [module tests](testing.md).
