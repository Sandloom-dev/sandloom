---
name: sandloom-docs-writing
description: Write or rewrite a Sandloom documentation page under docs/ — entry, concept, tutorial, how-to, cookbook recipe, reference, troubleshooting — from its job, purpose, audience and level to a finished draft. Use when asked to write, rewrite or restructure a page, explain a concept, add a recipe or clean up Reference.
---

# Write a Sandloom docs page

One skill of the docs set: [sandloom-docs-positioning](../sandloom-docs-positioning/SKILL.md) agrees what Sandloom is, [sandloom-docs-concepts](../sandloom-docs-concepts/SKILL.md) maps what a reader learns and in which order, [sandloom-docs-brief](../sandloom-docs-brief/SKILL.md) agrees what a page must say, this skill writes it, [sandloom-docs-review](../sandloom-docs-review/SKILL.md) checks it, and [sandloom-docs-eval](../sandloom-docs-eval/SKILL.md) tests a change to any of them.

The rules catch defects; they do not make a page good. A page is good when its reader wants what it offers and can act on it, and that comes from the owner's positioning, a real reader and the owner's own corrections. So read [the owner's preferences](../../../design/docs-writing/owner-preferences.md) before you draft: each is a correction the owner made once and should not have to make again.

References beside this file: [style.md](style.md) (style, MECE structure, code), [tells.md](tells.md) (machine-prose tells, ranked), [page-types/](page-types/README.md) (one anatomy per type), [audience.md](audience.md) (audiences and levels).

## Steps

1. **Start from the brief, or write the page's frame.** An entry page, README or landing text, or a page rewritten from scratch starts from a brief the owner approved; without one, run sandloom-docs-brief first. A phrase the owner accepted as page text goes in verbatim; a phrase the owner gave as meaning, such as the core idea, is rewritten into a natural sentence and shown to the owner; any other wording in the brief yields to the voice rules. Every page carries its frame in front matter, copied from the brief or written here with the brief's definitions of the same fields: `job:`, `purpose:`, `audience:`, `level:`, and the one message. A job with no evidence behind it (an issue, a DX eval failure, a question someone asked) is a hypothesis, and the handover says so. Done when one job holds the whole page; a second job gets its own page.
2. **Choose the type** from [page-types/](page-types/README.md) by the job, and set `type:`. When no type fits, add one there first, as its README says. Done when the type's file is open and its models are read.
3. **Place the page in the concept map.** Look up each concept the page explains in [concept-map.md](../../../design/docs-writing/concept-map.md): its home, parent, needs and level. Explain only the concepts this page is home to (M1), after their needs and at their level; for the rest, name them and link the home. A draft map is usable; a gap it lists that touches this page goes into the handover. A concept missing from the map, or a page that would become its second home, goes through sandloom-docs-concepts first. Then search `src/`, `docs/`, `guarantees.yml` and the `AGENTS.md` files for the page's path and headings: a cited heading stays, or its citations change with it. Done when each concept has one home and every citation still resolves.
4. **Read the code the page describes.** Trace each promise to its source or test, along its failure paths too: "if and only if" is a claim about every path, and "cannot", "always" and "never" are claims about every opt-out. Run what the page tells the reader to run. Done when each claim has a place you saw it true. A claim the page needs and no test covers gets an experiment and a follow-up task for the missing test; a claim you cannot confirm is reported, not written. Where page and code disagree, report it with both locations.
5. **Draft in variants.** Follow your type's blocks in order; split the subject into sections by one criterion, with no overlap and nothing left out at this level (M3). On an entry page or any page with a brief, write two or three variants of each block that carries the argument (the promise, the why, the fit): variants differ in approach (which group leads, which example, what the paragraph opens on), not in synonyms, and each meets the type's limits (the word cap, S4) before it is compared. Save them as whole pages with the same block order, `<scratchpad>/<page>-a.md`, `-b.md`, `-c.md`, and render them side by side, blind:
   ```bash
   uv run python tools/docs/docs_compare.py <scratchpad>/compare.html --blocks --blind a=<page>-a.md b=<page>-b.md c=<page>-c.md
   ```
   Pick per block. When the owner is present, show the comparison and the owner picks; otherwise give the comparison, the brief and the type's file to three judge subagents in parallel, each picking the best variant of each block with one line of reason, and take the majority. Assemble the picked blocks and rewrite their joints so the page reads as one. A pick chooses an approach, not final words: steps 6 and 7 still edit it. Other pages get one draft. Show, with real output you captured by running it, what an adjective would only claim. Done when every block is picked and the assembled page is saved.
6. **Subtract.** For each block, sentence and list item, ask what the reader loses without it, and whether the purpose still holds without it. Cut what loses nothing, even where the anatomy names a slot for it. Merge list items that give the same answer (M4). Done when every remaining piece has an answer.
7. **Edit the language.** Mark every tell from [tells.md](tells.md) on the page, strongest first, and rewrite each paragraph that has one around its point. Run the linters on the page alone:
   ```bash
   uv run python tools/docs/docs_prose_lint.py docs/<page>.md
   ```
   ```bash
   uv run python tools/docs/docs_code_check.py docs/<page>.md
   ```
   Done when both exit 0 and no tier 1 or tier 2 tell remains without a waiver and its reason.
8. **Hand it to review.** Run [sandloom-docs-review](../sandloom-docs-review/SKILL.md) on the page. A finding it returns is fixed by changing words, not adding them: a gap is filled one link deeper, and a sentence you add costs one you remove, so run steps 6 and 7 again on every block you touched. Done when the review is clean or each remaining finding is in the handover with its reason.

The handover lists the page's job, purpose, audience and level, each Sandloom term it uses with its home, every claim you could not confirm, the variants and who picked them, and the review's verdicts.
