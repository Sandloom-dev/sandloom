---
name: sandloom-docs-concepts
disable-model-invocation: true
description: Build or update the map of Sandloom's concepts in design/docs-writing/concept-map.md — each concept with its parent, what it depends on, its level, its home page, and the order to learn them from simple to complex. Use before restructuring the docs or the nav, when a brief or page needs a concept's home or level, when a new concept enters the framework, and when two pages explain the same thing.
---

# Map Sandloom's concepts

One skill of the docs set: [sandloom-docs-positioning](../sandloom-docs-positioning/SKILL.md) agrees what Sandloom is, this skill maps what a reader has to learn and in which order, [sandloom-docs-brief](../sandloom-docs-brief/SKILL.md) agrees one page, [sandloom-docs-writing](../sandloom-docs-writing/SKILL.md) writes it. The map is the source the MECE rules (M1–M5 in [style.md](../sandloom-docs-writing/style.md)) check against: one home per concept, parent before child.

The map lives in [concept-map.md](../../../design/docs-writing/concept-map.md). The owner approves its order and levels; the agent finds the concepts and the evidence.

## Steps

1. **Collect the concepts from the code, not the docs.** A concept is a thing the reader must understand to use Sandloom: a name in the beginner tier of `sandloom.sdk`, a CLI command family, a guarantee, an error family, a deploy profile. Sources, in this order: the public names in `src/sandloom/sdk/` and their tiers, `uv run sandloom --help` and its subcommands, `guarantees.yml`, the error-code families, [the glossary](../../../docs/getting-started/glossary.md), the docs nav. Done when every beginner-tier name and every glossary term is a concept or is folded into one, and the map says which.
2. **Give each concept its place.** For each one write:
   - **Parent**: the one concept it refines (an event consumer refines event). Exactly one, or none for a root.
   - **Needs**: the concepts a reader must understand first, because its explanation uses them. A need is a reason: "a consumer runs in a unit, so a unit comes first".
   - **Level**: `new`, `first-app`, `building` or `operating` from [audience.md](../sandloom-docs-writing/audience.md), the earliest level at which a reader meets it in their work.
   - **Home**: the one page that explains it (M1), from the glossary's link target or the nav.
   - **Plain words**: how a `new` page says it without the term, from the positioning's Naming.

   Done when every concept has all five, and every Needs entry has its reason.
3. **Check the structure.** The parents form a tree, and the needs have no cycle. Each level's concepts need only concepts at the same or a lower level. Siblings under a parent split it by one criterion, with no overlap (M3–M4). Every concept has exactly one home: list the concepts with none and the pages that explain one twice. Done when each break is fixed in the map or listed under Gaps with the page it affects.
4. **Order the learning path.** Within each level, order the concepts so each comes after its needs and the most used come first. The order across levels is the reading order of the docs, from a first look to production. Done when a reader following it never meets a concept before its needs.
5. **Ask the owner.** Show the levels and the order, and every place you chose between two readings, as at most five questions a round. Mark the map approved with the date. The nav and the pages change through sandloom-docs-writing, one page at a time; the map lists what each change is.

## The map's format

```markdown
Status: draft | approved by the owner, <date>

## Learning path
1. `new`: <concept>, <concept>, …
2. `first-app`: …

## Concepts
| Concept | Parent | Needs (reason) | Level | Home | Plain words |

## Gaps
- <concept with no home, or two homes>: <pages>, <proposed fix>
```
