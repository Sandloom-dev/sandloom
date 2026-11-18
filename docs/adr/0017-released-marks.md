---
search:
  exclude: true
title: "ADR-0017: Released marks for contracts and events"
---

# ADR-0017: Released marks for contracts and events

- **Status:** Accepted (2026-11-18, by the owner)
- **Date:** 2026-11-18
- **Amends:** [ADR-0010](0010-public-api-1.0.md) (the versioning rule of contracts and events) and
  [ADR-0013](0013-contract-ownership.md) (what the snapshot files hold)
- **Origin:** the mcphub dogfood run, friction items 1 to 3
- **Implemented by:** the tm task `contract-release-marks`

## Context

Every Sandloom module can become a separate service, so every contract and every event schema is
treated as published the moment it exists. `sandloom generate` therefore refused any breaking
change of a contract (SANDLOOM-T004) or an event (SANDLOOM-T005) against the committed snapshot,
from the first commit on. A new project that is still shaping its first module could not rename a
method or drop a field without writing a `.v2` beside it.

The baseline of that comparison is a file in the project, not a git ref or a tag. An application
may have no remote and no git at all, and a module distribution is compared with the snapshot that
ships in its wheel. Whatever fixes the friction must keep the baseline in those files.

## Decision

### 1. The strict model stays

The major version stays in the name (`.vN`). A breaking change of a released contract or event is
a new major beside the old one, both served. Runtime readiness keeps comparing the peers' describe
documents. Nothing in this ADR loosens a rule for something already released.

### 2. A released mark in the snapshot files

Each snapshot file records which of its entries are released, with a revision:

| file | key | holds |
|---|---|---|
| `<module>/contracts.snapshot.json` | `released` | `{contract name: revision}` |
| `<module>/module.snapshot.json` | `released` | `{event channel: revision}` |
| `<shared>/events/<channel>.json` (an application) | `x-sandloom-released` | the revision of that channel; `0` is a draft |

- **A draft is explicit, never inferred from absence.** Nothing that is protected becomes
  unprotected because a file is missing, moved, renamed or foreign. A draft exists only where a
  command created it (see "Where a draft comes from" below).
- **A draft is rewritten freely** by `sandloom generate`: no T004, no T005. Rewriting it moves no
  revision.
- **A released entry keeps today's checks.** A breaking change fails as before. The fix is in this
  order: restore the released `.vN` declaration, run `sandloom generate --new-major <name>`, then
  edit the copy.
- **The revision is computed, never typed.** A release writes revision 1. A later `sandloom
  generate` that finds a change of a released entry (an added method or optional field) writes
  revision N+1. The revision is shown by `sandloom docs <name>`; it counts the changes `generate`
  saw inside one major and does not replace the major. It is not verified against any history: the
  snapshot file is the only record, so a checkout that regenerates from an older file reaches an
  older number.
- **A name added to a snapshot file that exists is a draft** until the next release.

Where a draft comes from:

| source | what is a draft |
|---|---|
| `sandloom new` | the sample module (its contracts and events) |
| `sandloom add module` / `contract` / `event` (and `saga`, `projection`, which declare contracts and events) | what the command created |
| a new entry in a snapshot file that exists | that entry, until the next release |
| `sandloom generate --draft <name>` | the named contract, event or module's entries; refused for a name that is released, and for a name that is none of them |

**A snapshot file written fresh records every current entry as released** (revision 1): a deleted
file, a renamed module directory or a module copied in from elsewhere has no committed record to
tell a draft from a release, and the safe reading is the strict one. `--draft` is the way back for
a project that really wants a draft: delete the snapshot file and run `generate --draft <name>`,
which writes the named entry as a draft and every other entry of the file as released. The commands
above pass it for what they create.

### 3. `sandloom release`

`sandloom release` is `sandloom generate --release`: it runs the same checks and writes, and in
the same pass marks every current contract and event of this project released. It is a top-level
command, not `sandloom contracts release`, because it marks events too and a contracts command
would hide that. It marks only what the project owns: an installed module package carries its own
marks.

### 4. `sandloom generate --new-major <name>`

For a released contract or event, `--new-major` copies its declaration in the module's source to
the next major beside the old one: a contract class `@contract("m.x.v1")` gets a copy named for
`m.x.v2`, an event class `channel="m.x.v1"` a copy for `m.x.v2`. The old declaration stays, so both
are served. The copy is a draft. The command refuses a name that is a draft (edit it, there is
nothing to protect), a name that does not exist, a channel whose next major exists, a declaration
it cannot find in one place of the source, and a run with `--check` (as `--release` and `--draft`,
it writes).

The order matters when a breaking edit was already made to the released declaration: `--new-major`
copies the declaration as it is on disk, so the copy would carry the edit. Restore the released
`.vN` first (its snapshot file holds the released form), run `--new-major`, then edit the copy. A
`--new-major` run on a tree whose released entry is still broken copies the broken declaration and
says so: the generated files are written once the name is restored as released. A contract copy
reuses the DTO classes of the old major; give the copy its own DTO before it diverges.

### 5. No git, tag or remote dependency

The mark is data in the snapshot files. No command in this ADR runs `git`, reads a tag or contacts
a remote.

### 6. Migration

- **A snapshot file written before this ADR has no `released` key.** Its entries are treated as
  released at revision 1 on the next `sandloom generate`, which writes the key. Nothing that was
  protected is loosened by upgrading. A name the old file lacks is a draft.
- **A file with the key means exactly what it says.** A missing file is not new: it is written
  with every current entry released (see "Where a draft comes from").
- **The snapshots in this repository** (the kernel's modules, the batteries, the packages and the
  examples) were migrated this way, so they are released. `src/sandloom_admin/module.snapshot.json`
  has no events and was written with an empty `released` map by a generate that composes the
  console.
- **A new project from `sandloom new`** starts with its sample module as a draft: the scaffold
  names it with `--draft` when it writes the generated files, so its snapshot files carry an empty
  `released` map. The scaffold's own files, and so its manifest, are unchanged: the snapshots are
  written by that generate, not by the scaffold.
- **An installed module package's events always count as published.** The package ships its
  snapshot; its marks (draft or not) do not make its events drafts in an application, and the
  exported copy in the application is compared on every generate (T005). A package mark that is not
  a revision is reported, not ignored.

### 7. Where the rule lives

`core/descriptors/release_marks.py` holds the pure rules (read the marks of an old document, resolve
the legacy case, compute the next marks and revisions). `core/cli/generated.py` applies them for
module, contract and exported event files, and for `--release` and `--new-major`.
`sandloom contracts snapshot` keeps the marks of the file it rewrites. `sandloom explain` T004 and
T005 say draft against released.

## Alternatives considered

| option | why not |
|---|---|
| Compare with a git ref or tag of the last release | an application may have no git or remote, and a module wheel has no history; it adds a dependency on how the project is hosted |
| A draft is a separate flag in the source (`@contract(..., draft=True)`) | the mark would live apart from the baseline it governs, and a forgotten flag silently removes a check |
| Everything released from the first commit (the previous rule) | the friction this ADR removes: a project shaping its first module cannot change it |
| Treat files with no `released` key as drafts | loosens every existing project on upgrade with no warning |
| Treat a missing snapshot file as all drafts (the first revision of this ADR) | a deleted, moved or renamed file silently unprotects what a release protected; main refused that break |

## Consequences

- A project that adopts Sandloom edits its sample contracts freely until its first `sandloom
  release`, and gets today's guarantees from then on.
- Two writers of `contracts.snapshot.json` exist (`generate` and `contracts snapshot --write`);
  both keep the marks through the same function.
- A revision is a count of changes seen by `generate`, not a content hash. Two checkouts that
  generate the same change independently reach the same number, and reverting a change does not
  lower it. It is not recomputed from anything, so it is not a proof of what the entry held.
- A hand-made snapshot file for a module that is new to a project is released unless the project
  passes `--draft`; the creating commands do so.
- The change is behavioural for new projects only; for existing ones the first `generate` after
  the upgrade adds the key to each snapshot (see the upgrade note).
