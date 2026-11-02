# Page anatomy by type

Follow your type's order. Each page holds one type; a concept may end with a short "use it" section linking to the how-to. Bold names are slots, not headings: write a heading that means something alone, such as "What breaks an event".

## Entry page

For a reader who arrived from a link and may leave in seconds: a developer, a vibe coder, or an agent that has never seen Sandloom. The page makes them want to try it and tells them how. At most three screens at 1280×900; architecture, principles and reference tables live on their own pages.

1. **The problems.** First on the page, in the reader's words, with no Sandloom term: failures they have had or fear.
2. **The promise.** One sentence, the page's one message.
3. **Try it.** On the first screen: one primary action, and a prompt the reader pastes into their agent, with exact commands, stating what they need installed.
4. **Proof.** Real output or the shortest real code that shows the promise working.
5. **What it does about each problem.** Problem and answer side by side, each linked one level deeper.
6. **Fit and next.** Flat bullets on who it suits and who it does not, then a few links.

Models: [Rails](https://rubyonrails.org/) for a promise aimed at agents, [Supabase](https://supabase.com/docs) for the agent prompt, [Flutter](https://flutter.dev/) for the promise and its try-it action, [Encore.ts](https://encore.dev/docs/ts) for "instead of …", [Svelte](https://svelte.dev/docs/svelte/overview) for sentence, code, sentence.

## Concept

For a reader learning an idea, away from the keyboard. Noun-phrase title, never Introduction or How it works (C1). Problem and contract open the page with no heading of their own, and small slots fold together to stay within 6 `##` sections.

1. **Problem (C2).** One or two sentences with a concrete failure or need, and no Sandloom term: "A service commits an order and crashes before telling billing." A page on a sub-topic whose parent page already states the problem may open on the definition.
2. **Contract (C3–C4).** One self-contained paragraph: what it is and what is promised, in checkable words (the outbox row commits with the change or not at all; delivered at least once; not ordered). It reads correctly with the title removed. Every term in it is defined there or linked. At most two new terms per sentence.
3. **Smallest example (C5).** Complete and tested, starting on the first screen after the contract.
4. **Failure walk (C6).** "What happens when …": numbered steps through that same example, with at least one crash, retry or duplicate. This is the part readers lack most.
5. **Boundaries (C7).** Flat bullets: what it is not, what it does not cover.
6. **Choose (C8).** A comparison table only for a real either/or. Event or contract links `docs/modules/between/index.md`.
7. **What you get and declare (C9).** At most 5 ± 1 rows, after the example; the rest links to Reference.
8. **What breaks it (C10).** Top error codes, plain symptom first.
9. **Go deeper (C11).** How-to, recipes, reference, ADR. Guarantee ids and internals sit here, while the promises themselves stay in the contract.

Models: [Why Temporal?](https://docs.temporal.io/evaluate/why-temporal), [React state](https://react.dev/learn/state-a-components-memory), [Rust ownership](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html).

Numbered procedures and full option lists belong on other pages; link them. A value the reader's reasoning depends on (a 30 s handler timeout) stays, linked to its setting (C12).

## Tutorial

For a beginner learning by doing, one path from start to finish.

- **T1.** First screen: what gets built, how long it takes, the final result.
- **T2.** Pinned versions, complete prerequisites, a link for when stuck.
- **T3.** Every step ends in visible output, shown literally in a fenced block.
- **T4.** One path. Explanation is a sentence and a link.
- **T5.** Step headings give full context, so a reader landing mid-page knows where they are.
- **T6.** CI runs it end to end (`tools/smoke/tutorial_check.py`, `tools/smoke/quickstart_check.py`).

## How-to

For a working reader doing one framework operation. Title is a bare infinitive: "Add an event" (H1).

- **H2.** First sentence: what the reader can do and why, with a link to the concept. The Goal / Use it / Do not use it opening in `docs/modules/between/add-event.md` is the model.
- **H3.** One task, the recommended method. About 6 steps per group, ten per phase.
- **H4.** Conditions before instructions. Explanation over a sentence moves to the concept page.
- **H5.** End in order: verify, optional blocks, next steps.
- **H6.** Each step stands alone: full command, exact label.

## Cookbook recipe

For a reader with a business problem who knows the concepts. A how-to does one framework operation; a recipe combines several. Each recipe answers one job. Recipes are independent of each other, and the index groups them by problem domain with one line each (R9).

1. **Title (R1).** The reader's task in their words: "Stop a duplicate webhook from charging twice", not "Using `@on` with durable inbox".
2. **Problem (R2).** One or two sentences with a concrete situation.
3. **Solution (R3).** One to four sentences naming the mechanisms, each linked to its concept. The concept page does the explaining.
4. **Code (R4).** One complete, tested module path from an example app, included by marker, imports shown, nothing elided.
5. **Run and verify (R5).** A test or command with literal expected output.
6. **When to use, trade-offs, pitfalls (R6).** One or two pitfalls with the `SANDLOOM-*` code that fires.
7. **Variation (R7).** Monolith versus split, only if behaviour differs.
8. **Related (R8).** At most 3–5 links.

Model: [Temporal Polling](https://docs.temporal.io/design-patterns/polling).

## Reference

For a reader looking up a fact. Opens with one line and a table.

- **F1.** Quick-reference table first.
- **F2.** The same facts in the same order per entry: type, default, required, constraints.
- **F3.** Self-identifying entry names: `sandloom.sdk.events.on.dead_letter`, not `dead_letter` under a heading.
- **F4.** Literal values for ranges, defaults and limits.
- **F5.** Everything visible, with nothing collapsed or tabbed.
- **F6.** Structure mirrors the product, in alphabetical or API order. The 5 ± 1 limit does not apply.
- **F7.** A short usage example per entry at most. Why and how are links.

## Troubleshooting

For a reader looking at a symptom. The per-code catalogue stays in `docs/reference/error-codes.md`; this type covers symptoms and multi-cause problems (E7).

- **E1.** Entry title is the verbatim message or an observable symptom.
- **E2.** Fixed order in bold labels: Cause, Fix, Verify.
- **E3.** The message in a fenced block, unparaphrased.
- **E4.** The fix is a command or code.
- **E5.** Most frequent first, with data-loss failures ahead of them. A workaround states its cost.
- **E6.** Inline until about five entries, then its own page.
