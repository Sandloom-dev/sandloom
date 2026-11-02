---
name: sandloom-docs-review
description: Review a Sandloom docs page before it ships — linters, two reviewers (spec and voice), reader questions on a context-free subagent, the render, and the owner's verdict. Use after writing or editing a page under docs/, when asked to review, check or grade a page, and before handing docs work over.
---

# Review a Sandloom docs page

The writer is the worst judge of their own page: they read what they meant. So the checks a script can do run as scripts, the rules run through reviewers that never wrote the page, and understanding is tested on a reader with no context. None of them says whether the page is good; that is the owner's verdict, or a reader's who never saw Sandloom. Part of the docs set; the writing is [sandloom-docs-writing](../sandloom-docs-writing/SKILL.md).

Each step returns findings as `file:line rule — problem → fix`, problems only, no praise. The page's front matter (`job:`, `purpose:`, `audience:`, `level:`, `type:`) and, if there is one, its brief in `design/docs-writing/briefs/` are the spec every step checks against.

## Steps

1. **Lint.** Run both linters on the page alone, strictly; `<page>` is its path, such as `docs/index.md` or `README.md`:
   ```bash
   uv run python tools/docs/docs_prose_lint.py <page>
   ```
   ```bash
   uv run python tools/docs/docs_code_check.py <page>
   ```
   A hit is fixed, or waived on its line with its reason (`<!-- prose-ok: <rule> <reason> -->`, `<!-- code-ok: <rule> <reason> -->`). Done when both exit 0.
2. **Two reviewers.** Start two subagents in parallel, each with the page, its front matter and brief, and only its rules:
   - **spec**: the type's file in [page-types/](../sandloom-docs-writing/page-types/README.md), the brief's fields, structure M1–M5 against [the concept map](../../../design/docs-writing/concept-map.md), and code K1–K6 when the page has code;
   - **voice**: [tells.md](../sandloom-docs-writing/tells.md), style S1–S15 and A1–A8 ([style.md](../sandloom-docs-writing/style.md)), the level's words ([audience.md](../sandloom-docs-writing/audience.md)) and the [owner's preferences](../../../design/docs-writing/owner-preferences.md).

   The anatomy and the brief are fixed: what they require is not a finding. After fixes, rerun the linters and one reader round; rerun a reviewer only when a block's meaning changed. Done when both have answered and each finding is fixed or answered with a reason.
3. **Reader questions.** Write 5–10 questions this page must answer for its audience at its level: from the job (can they now reach the outcome), the purpose (what would they do next), the one message (what is it, what does it give), and what that audience fears (a crash, a retry, a wrong step). Give the questions and the page, nothing else, to a fresh subagent playing the primary audience; give the page alone to a second fresh subagent for every ambiguity, unstated assumption and contradiction. You grade each answer against the brief and the code; an ambiguity about something the brief's Out sends to another page is not a finding. Fix the page by changing words and ask a fresh reader again, at most three rounds. Done when one round has every answer right, or the remaining gaps are in the handover.
4. **Look at the render.** Build and open the page at 1280×900 and at phone width:
   ```bash
   uv run --extra docs python tools/docs/docs_site.py build
   ```
   Read the first screen alone, beside the type's model page: does it say the one message, and does it have the effect the purpose names? Done when you have seen it, not when the build passes.
5. **Get the verdict.** A cold read praised pages the owner rejected, so for an entry page, a brief-backed page or a page the owner asked for, show it to the owner and ask what to change, in words. Write each correction that applies beyond this page into the owner's preferences, with the date and the page, and into the type's file when it is about a block. For other pages, the verdict of step 3 stands and the handover says so.
6. **Run the gates.** Each must pass; report what they said:
   ```bash
   uv run python tools/docs/docs_prose_lint.py
   ```
   ```bash
   uv run python tools/docs/docs_code_check.py
   ```
   ```bash
   uv run python tools/docs/docs_stale_symbols.py
   ```
   ```bash
   uv run python tools/packaging/export_repository.py --check
   ```
   A new or moved page also needs its `zensical.toml` entry and its path in `tools/packaging/public-source.json`. After changing what `guarantees.yml` cites, run `uv run python tools/checks/guarantees.py render`. A page headed `GENERATED` changes through its generator.
