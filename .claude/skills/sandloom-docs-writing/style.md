# Style, structure and code rules

Every page, every type. Rule ids (C1, S4, …) match [the research](../../../design/docs-writing/research.md), which holds the evidence and the disagreements; read it when a rule seems wrong for a page. Numbers are defaults you may break with a reason. Banned words and limits apply to prose; an identifier such as `start_from="latest"` is exempt.

The rules a script can check run in `tools/docs/docs_prose_lint.py` (S4, S8, S9, S11, S13, K3, the tells P6–P9 and P12, and W1, the entry word cap); a justified exception is waived on its line with `<!-- prose-ok: <rule> <reason> -->`. The rest need a reader: the writer while drafting, and the rule reviewer in [sandloom-docs-review](../sandloom-docs-review/SKILL.md).

## Style

- **S1–S3.** "You" on every page, tutorials included; "we" means the Sandloom team. Present tense, with "will" only for what really happens later, such as asynchronous delivery.
- **S4–S5.** One idea per sentence, under 26 words. Paragraphs of 3–5 sentences; the first sentence carries the point.
- **S6.** One word per concept across all pages. Where glossary and code differ (event id, envelope id), pick one and report the other.
- **S7.** Write the noun where "this", "it" or "that" could point at two things. This holds in the owner's accepted sentences too: replacing an ambiguous pronoun with its noun is not a change of wording.
- **S8.** Banned where they call the reader's task effortless or praise with nothing behind it: simply, just, easy, easily, quick, obviously, robust, powerful. An outcome the same paragraph backs with its mechanism stays. "Easy to get wrong" stays.
- **S9.** Banned in the sense of time, outside changelogs and upgrade guides: currently, now, new, soon, latest. "New to Sandloom?" stays.
- **S10.** Write about the subject, and refer to other text by link. Phrases such as "this page explains", "as above" and "below" go.
- **S11.** Headings: specific, sentence case, under 70 characters, meaningful read alone. At most three levels on a concept page.
- **S13.** 5 ± 1 items in a list or table group, and at most 6 `##` sections on a concept page. Lookup lists are exempt: Reference pages, and a list preceded by `<!-- lookup -->`.
- **S14.** Admonitions for what would change the outcome and is not needed to succeed. The first sentence is the impact. Facts the reader needs stay in body text; an entry page may put its primary path in one admonition.
- **S15.** Compare with something technical the reader knows: a `WHERE` clause, a Django signal. At most one story analogy per page, followed by the literal claim.
- **Precision by depth.** A first-screen sentence is plain and true, and its fine print lives one link deeper. A claim that needs three qualifiers is the wrong claim for that place.
- **Level.** Words above the page's `level:` are explained in place or replaced ([audience.md](audience.md)).
- **Promise what Sandloom does.** A result names what the framework guarantees or what the reader runs, never what an agent or a person will achieve: "`uv run sandloom test` passes", not "the agent hands you the part you described".
- **A1–A3.** The contract paragraph and each boundary are accurate when retrieved alone. Every other section names its subject. Use exact identifiers, verbatim messages and literal output.
- **A8.** Spend words on what a FastAPI-trained reader or agent would get wrong: the framework commits, a module never imports a sibling, a delivery can repeat.

## Structure (MECE)

Mutually exclusive, collectively exhaustive: every concept, section and list item has one place, and together they leave nothing of the subject out.

- **M1. One home per concept.** Each concept is explained on one page, its home, at the level that page declares; every other page names it and links there. The home is the glossary entry's link target. A second explanation is a defect, even when it is shorter.
- **M2. Parent before child.** A concept's home links to its parent and lists its children. A page on a sub-concept opens from its parent's problem, not its own, and does not repeat the parent's contract.
- **M3. Sections split the subject.** A page's `##` sections divide its one job with no overlap: no fact appears in two sections, and a reader who reads all of them has the whole subject at that level. A fact that belongs to no section is a missing section or another page.
- **M4. Lists by one criterion.** Items of a list or table differ along one criterion and do not overlap; two items with the same answer merge. Pains, advantages, options and cards are checked this way.
- **M5. Gaps are named.** What the page leaves out it names, with the page that covers it, so "exhaustive" holds across the docs, not by stuffing one page.

## Machine-prose tells

Ranked, with what to do about each, in [tells.md](tells.md). The mechanical ones (P6 triads, P7 repeated openers, P8 ", so" chains, P9 dashes, P12 model words) also fail the prose lint.

## Code

Every Python block is checked by `tools/docs/docs_code_check.py`: it parses (D1), its `sandloom` imports resolve (D2), its includes resolve (D3), and a block over three lines comes from a tested file or carries a waiver with its reason (D4).

- **K1–K2.** Include samples from a file a test runs: `--8<--` from `docs/snippets/` or an example app, with imports, under a fence with `title="path"`. A line range (`file.py:12:17`) must still contain what the heading promises, so read the render. A fragment is marked as one and links its full file on GitHub. Inline only one-liners.
- **K3.** A sentence before every block; expected output after it when output exists.
- **K5.** Concept samples: 10–25 lines per block, and an example may span blocks when each file needs showing. Recipe: a full module path. Longer belongs in a tutorial.
- **K6.** Where the wrong way is the common instinct (`db().commit()`), show it as a marked inline one-liner beside the code that rejects it (`SANDLOOM-*` or `C*`), and point at the test expecting that error.
