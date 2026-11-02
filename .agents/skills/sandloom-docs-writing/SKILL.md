---
name: sandloom-docs-writing
description: Write, rewrite or review Sandloom documentation pages under docs/ — concept, tutorial, how-to, cookbook recipe, reference, troubleshooting. Use when asked to explain a concept, add a recipe, clean up Reference, or check a page against the house style.
---

# Write a Sandloom docs page

Rule ids (C1, S4, …) match [the research](../../../design/docs-writing/research.md), which holds the evidence and the disagreements behind them. Read it when a rule seems wrong for a page.

The rules catch defects; they do not make a page good. A page passes when its reader wants what it offers and can act on it, and for that you look at it, compare it with a model page and, for an entry page, ask the owner.

## Steps

1. **Write the reader's job.** Set `job:` in front matter: "When <situation>, I want <progress>, so I can <outcome>." The situation names a moment in the reader's work, with no Sandloom term; the progress is verb + object; the outcome is what they can do afterwards. A job you have no evidence for (an issue, a DX eval failure, a question someone asked) is a hypothesis, and the handover says so. Done when one sentence holds the whole page; a second job gets its own page.

   Then write the page's **one message**: the sentence a reader repeats to a colleague afterwards. Everything on the first screen serves it.

   The job then drives the page: the situation becomes the opening, the progress becomes the title, what the reader fears going wrong becomes the failure walk and the boundaries, and a section that serves none of it moves or goes.
2. **Choose the type from the job.** Does the reader act or learn; are they acquiring a skill or applying one? Set `type:` to `entry`, `concept`, `tutorial`, `how-to` or `reference`; a recipe and a troubleshooting page use the nearest one.
3. **Map what cites the page.** Search `src/`, `docs/`, `guarantees.yml` and the `AGENTS.md` files for its path and headings. A cited heading stays, or its citations change with it. Text of another type moves to its target page first, then leaves a link. Done when every removal has a destination and every citation still resolves.
4. **Read the code the page describes.** Trace each promise to its source or test, along its failure paths too: "if and only if" is a claim about every path, and "cannot", "always" and "never" are claims about every opt-out. Run what the page tells the reader to run, and the tests behind its samples. Done when each claim has a place you saw it true. A claim the page needs and no test covers gets an experiment and a follow-up task for the missing test; a claim you cannot confirm is reported, not written. Where page and code disagree, report it with both locations.
5. **Name the reader's starting point.** There are three readers. A developer knows FastAPI and SQLAlchemy and has never met an outbox. A vibe coder steers an agent, reads little code and judges by what runs. An agent has never seen Sandloom and acts on literal text. Write for the one the job names, and leave the others a way in. List each Sandloom term the page uses in your handover, with where the reader meets its definition. Use [the glossary](../../../docs/getting-started/glossary.md) names; a missing term joins the glossary in the same change.
6. **Draft from the anatomy.** Open [page-types.md](page-types.md), follow the section for your type in its order, and read its model pages first. Show, with real output you captured by running it, what an adjective would only claim.
7. **Check every rule.** Go through Style and Code below and the type's rules, line by line. Done when each rule is met or the handover states why the page breaks it.
8. **Look at the render.** Build, open it in the browser at 1280×900 and at phone width, and read the first screen alone, beside a model page. Does it say the one message, and would you try this? Done when you have seen it, not when the build passes.
9. **Run a cold read.** Give the subagent the finished page and its `job:` line, with no other context, and ask: can you now reach that outcome, what problem does this solve, what exactly is promised, what happens on a crash or a duplicate, what would you do next? Fix the page wherever an answer is wrong or missing.
10. **Run the gates.** Each of these must pass; report what they said.
   ```bash
   uv run --extra docs python tools/docs/docs_site.py build
   ```
   ```bash
   uv run python tools/docs/docs_stale_symbols.py
   ```
   ```bash
   uv run python tools/packaging/export_repository.py --check
   ```
   A new or moved page also needs its `zensical.toml` entry and its path in `tools/packaging/public-source.json`. After changing what `guarantees.yml` cites, run `uv run python tools/checks/guarantees.py render`. A page headed `GENERATED` changes through its generator.

## Style

Every page, every type. Numbers are defaults you may break with a reason. Banned words and limits apply to prose; an identifier such as `start_from="latest"` is exempt.

- **S1–S3.** "You" on every page, tutorials included; "we" means the Sandloom team. Present tense, with "will" only for what really happens later, such as asynchronous delivery.
- **S4–S5.** One idea per sentence, under 26 words. Paragraphs of 3–5 sentences; the first sentence carries the point.
- **S6.** One word per concept across all pages. Where glossary and code differ (event id, envelope id), pick one and report the other.
- **S7.** Write the noun where "this", "it" or "that" could point at two things.
- **S8.** Banned where they describe the reader's task or praise the product: simply, just, easy, easily, quick, obviously, robust, powerful. "Easy to get wrong" stays.
- **S9.** Banned in the sense of time, outside changelogs and upgrade guides: currently, now, new, soon, latest. "New to Sandloom?" stays.
- **S10.** Write about the subject, and refer to other text by link. Phrases such as "this page explains", "as above" and "below" go.
- **S11.** Headings: specific, sentence case, under 70 characters, meaningful read alone. At most three levels on a concept page.
- **S13.** 5 ± 1 items in a list or table group, and at most 6 `##` sections on a concept page. Lookup tables are exempt.
- **S14.** Admonitions for what would change the outcome and is not needed to succeed. The first sentence is the impact. Facts the reader needs stay in body text; an entry page may put its primary path in one admonition.
- **S15.** Compare with something technical the reader knows: a `WHERE` clause, a Django signal. At most one story analogy per page, followed by the literal claim.
- **Precision by depth.** A first-screen sentence is plain and true, and its fine print lives one link deeper. A claim that needs three qualifiers is the wrong claim for that place.
- **A1–A3.** The contract paragraph and each boundary are accurate when retrieved alone. Every other section names its subject. Use exact identifiers, verbatim messages and literal output.
- **A8.** Spend words on what a FastAPI-trained reader or agent would get wrong: the framework commits, a module never imports a sibling, a delivery can repeat.

## Code

- **K1–K2.** Include samples from a file a test runs: `--8<--` from `docs/snippets/` or an example app, with imports, under a fence with `title="path"`. A line range (`file.py:12:17`) must still contain what the heading promises, so read the render. A fragment is marked as one and links its full file on GitHub. Inline only one-liners.
- **K3.** A sentence before every block; expected output after it when output exists.
- **K5.** Concept samples: 10–25 lines per block, and an example may span blocks when each file needs showing. Recipe: a full module path. Longer belongs in a tutorial.
- **K6.** Where the wrong way is the common instinct (`db().commit()`), show it as a marked inline one-liner beside the code that rejects it (`SANDLOOM-*` or `C*`), and point at the test expecting that error.
