# Concept

`type: concept` · Reader: learning an idea, away from the keyboard · Level: `building`

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
