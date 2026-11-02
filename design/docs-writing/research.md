# How the best developer documentation is written — research

Status: **design research; no docs changed**. Sources retrieved 2026-11-01 and 2026-11-02.
Input for a writing skill that will drive the concept-page rewrite, a new Cookbook and the Reference clean-up.
Prior work: `Docs/design/docs-strategy-2026-10-25.md` at commit `7c8643d6` of the pre-rename repository, not carried into this one (below, "the strategy"), which covers tooling, generated reference, error pages and agent channels. This file covers craft and only cites the strategy where it bears on craft.

Text marked **Inference** is mine. Everything else reports what the linked source says or what I measured.
Long pages were read by outline and targeted search, not end to end; see [What could not be verified](#what-could-not-be-verified). Rule ids (C1, S4, …) are there so the skill can cite them.

## Summary

### Recommendations

1. **Change the concept template.** The strategy's template (§6.2.3) is what `events.md` and `units.md` follow: definition, then a "you get / you declare" table, then "How to use it". No concept page surveyed puts a table of declarations before a worked example. Use this order instead: problem, contract, smallest complete example, what happens when it fails, boundaries, what to choose instead, what breaks it, links ([anatomy](#concept)).
2. **Separate the "why" from the vocabulary.** Temporal's [Why Temporal?](https://docs.temporal.io/evaluate/why-temporal) opens on a crash mid-payment, and its [Understanding Temporal](https://docs.temporal.io/evaluate/understanding-temporal) is the vocabulary page, which points to the other for the reason. I did not find a Sandloom page that opens on a failure; the [overview](../../docs/index.md) states what the framework is and its principles.
3. **Show the failure, then the mechanism.** React runs a broken gallery before introducing state; Rust shows the compiler rejecting a moved value; Encore's outbox guide opens on non-transactional publishing. Sandloom's guarantees exist because of failures, and its pages describe the mechanism without showing one.
4. **Move lookups out of concept pages.** The wire-shape tables, `@on` options and relay settings in `events.md` and `units.md` are reference. Diátaxis, GitLab, Cloudflare, Supabase and Red Hat all separate these. Length is not the defect: `events.md` is about 3,800 words, shorter than Laravel Queues or Stripe Webhooks. Order and mix are.
5. **Write the definition paragraph so it survives alone, in checkable terms.** This is the one rule that serves both readers: Cloudflare requires it because agents quote that paragraph, and Google's course requires an opening sentence that carries the point because busy readers stop there.
6. **Model recipes on Temporal's Design Patterns plus the Rust Cookbook's rules.** Problem, Solution, Implementation, When to use, Trade-offs, Pitfalls, Related; a one-sentence task title; standalone tested code ([recipe anatomy](#cookbook-recipe)).
7. **Clean Reference to one fixed shape per entry:** quick table first, self-identifying names, literal values, nothing collapsed, no explanation ([anatomy](#reference)).
8. **Keep the subsystem-first navigation; add Cookbook as a sibling tab.** Stripe, Cloudflare, Supabase and Encore split by product area before type. Type-named top levels are the minority (Django's index still groups by layer). Do not reorganise the tree first: Diátaxis says structure follows from improving pieces.
9. **Adopt the style rules in [Style rules](#style-rules) as a checklist,** including Supabase's measurable limits (under 26 words per sentence, 5 ± 1 items per group) as defaults a writer may break with a reason.
10. **Test the rewrite on readers.** Nothing here measures comprehension; the sources report practice. Rerun the DX eval (`benchmarks/dx/`, required by `AGENTS.md` for rule and skill changes) and add a human first-read check.

### The answer in brief

The leaders agree on: one job per page, opening with what the reader gets or needs, examples early, tested code, one name per concept, sparing admonitions, no "simply". They disagree on: problem-first versus definition-first, how strictly to separate types, analogy, code comments, and page titles ([Disagreements](#disagreements)). The best-studied agent claim is narrow (an index beats a skill); almost all prose rules for agents are practice, not measurement ([Agent notes](#agent-specific-notes)).

## What the guides say

| Source | What it adds to craft |
| --- | --- |
| [Diátaxis](https://diataxis.fr/) ([compass](https://diataxis.fr/compass/)) | Two questions decide the type: action or cognition; acquisition or application. Types drift because they are neighbours, and [blurred types bleed into each other](https://diataxis.fr/map/). [Do not create empty sections](https://diataxis.fr/how-to-use-diataxis/); improve a small piece, publish, repeat. |
| [Divio](https://docs.divio.com/documentation-system/) | Same four types, an earlier statement by the same author. [Procida's colophon](https://diataxis.fr/colophon/) says he now thinks several aspects of it were wrong. **Use Diátaxis; Divio adds nothing further here.** |
| [Google style highlights](https://developers.google.com/style/highlights), [tone](https://developers.google.com/style/tone), [headings](https://developers.google.com/style/headings), [notices](https://developers.google.com/style/notices), [code samples](https://developers.google.com/style/code-samples) | Second person, active voice, present tense. Avoid figurative language, "simply", "let's". Task headings start with a bare infinitive, concept headings are noun phrases. Notes are for what the reader does not need to succeed. |
| Google courses: [audience](https://developers.google.com/tech-writing/one/audience), [words](https://developers.google.com/tech-writing/one/words), [short sentences](https://developers.google.com/tech-writing/one/short-sentences), [paragraphs](https://developers.google.com/tech-writing/one/paragraphs), [documents](https://developers.google.com/tech-writing/one/documents), [large docs](https://developers.google.com/tech-writing/two/large-docs), [illustrations](https://developers.google.com/tech-writing/two/illustrations), [sample code](https://developers.google.com/tech-writing/two/sample-code) | Curse of knowledge. State scope and non-scope. Paragraphs of 3–5 sentences; over about 7 tends to be skipped. "Why" before the task. One illustration holds about a paragraph or five bullets; write the caption first; big picture, then subsystems. Pair example with anti-example. Sample code: range from basic to advanced. |
| [Google doc best practices](https://google.github.io/styleguide/docguide/best_practices.html) | Minimum viable documentation; delete dead docs; update docs with code; link rather than duplicate. |
| [Microsoft: top 10](https://learn.microsoft.com/en-us/style-guide/top-10-tips-style-voice), [code examples](https://learn.microsoft.com/en-us/style-guide/developer-content/code-examples), [headings](https://learn.microsoft.com/en-us/style-guide/scannable-content/headings), [global writing](https://learn.microsoft.com/en-us/style-guide/global-communications/writing-tips) | Lead with what matters, cut "you can" and "there is". One word per concept. Few commas per sentence. Examples address real problems, not contrived ones, list requirements, show expected output, and handle exceptions only when intrinsic. |
| [Write the Docs principles](https://www.writethedocs.org/guide/writing/docs-principles/) | ARID (accept some repetition), skimmable, exemplary, cumulative (prerequisites first), addressable, unique scope per source. "Consider incorrect documentation to be worse than missing documentation." |
| Good Docs: [concept](https://www.thegooddocsproject.dev/template/concept), [how-to](https://www.thegooddocsproject.dev/template/how-to), [tutorial](https://www.thegooddocsproject.dev/template/tutorial), [troubleshooting](https://www.thegooddocsproject.dev/template/troubleshooting), [reference](https://www.thegooddocsproject.dev/template/reference) | Concrete templates. One concept per document, use cases as problem → guide → change, analogies only where they clarify. How-to: one task, 8–10 steps, only the recommended method. Tutorial: 15–60 minutes, at most seven primary steps. |
| GitLab: [topic types](https://docs.gitlab.com/development/documentation/topic_types/), [concept](https://docs.gitlab.com/development/documentation/topic_types/concept/), [task](https://docs.gitlab.com/development/documentation/topic_types/task/), [troubleshooting](https://docs.gitlab.com/development/documentation/topic_types/troubleshooting/), [style guide](https://docs.gitlab.com/development/documentation/styleguide/) | Each *topic* has one type, but a page may combine them, usually concept first. A concept says what the thing is and why someone would use it, in a paragraph or two. No titles like Overview or How it works. Troubleshooting titles carry the error message. |
| [Red Hat modular docs](https://redhat-documentation.github.io/modular-docs/) | Concept, procedure, reference modules; a module does not contain another. Procedures have a fixed set of optional sections and no extra subheadings. |
| Cloudflare: [content types](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/), [concept](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/), [how-to](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/how-to/), [troubleshooting](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/troubleshooting/), [reference](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/reference/) | The most specific published rules found, and each type has a "Writing for AI and agents" section. Write a concept page when readers keep needing the same explanation inside other pages. |
| Supabase style guide: [voice](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md), [elements](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/02-elements.md), [page structure](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md) | Written for humans and LLMs together. Under 26 words per sentence; 5 ± 1 items per group; one unit per page; open a guide with what the reader can do. A short concept opener, then procedures, then concept and process, then structure and fact. |
| [Next.js contribution guide](https://nextjs.org/docs/community/contribution-guide) | Overview, Convention, Examples, API tables, Next steps. Code is a minimum working example with imports and a filename. Conceptual pages use "you"; reference pages use imperative words and tend to omit it. |
| [Django writing guide](https://docs.djangoproject.com/en/dev/internals/contributing/writing-documentation/) | Tutorials, topic guides, reference, how-tos, by name. Expert speaking to a fellow developer, a human voice, no empty superlatives or minimising words. Self-contained version notes. |
| Team accounts: [Stripe on Markdoc](https://stripe.dev/blog/markdoc), [React on react.dev](https://react.dev/blog/2023/03/16/introducing-react-dev) | Stripe: keep logic out of content so writers write, and validate structure in CI. React: the old docs taught classes first, so readers "had to learn React twice"; teach the current model from the start. Diagrams were added where code and words failed. |

## Common practices, with evidence

### Introducing a hard concept

| Move | Who does it | How |
| --- | --- | --- |
| Failure scenario first | [Why Temporal?](https://docs.temporal.io/evaluate/why-temporal), [Encore outbox](https://encore.dev/docs/go/how-to/pubsub-outbox), [Laravel Queues](https://laravel.com/docs/13.x/queues) | A crash mid-payment. Publishing not transactional with writes. A CSV upload too slow for a request. |
| Broken code, then why, then the fix | [React state](https://react.dev/learn/state-a-components-memory), [React Effects](https://react.dev/learn/synchronizing-with-effects) | Runnable code that does not work; the reasons become requirements; the feature answers them. |
| Compare with what the reader knows | [Rust ownership](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html), [Durable Objects](https://developers.cloudflare.com/durable-objects/concepts/what-are-durable-objects/), [Tailwind](https://tailwindcss.com/docs/styling-with-utility-classes) | Garbage collection and manual free versus ownership. A Worker plus two additions; then the actor model. "Isn't this just inline styles?" answered directly. |
| Rules up front, tested by the machine | [Rust ownership](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html) | Three rules as a list, then examples, then a full E0382 compiler transcript. The page closes by showing the result is "too much ceremony", which motivates the next page. |
| Definition first, no problem | [Workflow Execution](https://docs.temporal.io/workflow-execution), [Encore Pub/Sub](https://encore.dev/docs/ts/primitives/pubsub) | These are second-level pages; the reader is assumed to have come through the introduction. |
| One step-through | [How Temporal works](https://docs.temporal.io/encyclopedia/architecture/how-temporal-works) | Fifteen numbered steps through one Workflow and Activity, with an interactive demo of the same sequence. |
| Boundary statement | [Understanding Temporal](https://docs.temporal.io/evaluate/understanding-temporal), [React Effects](https://react.dev/learn/synchronizing-with-effects) | "Temporal is not a no-code Workflow engine." React narrows Effects by repeatedly showing cases that do not need one. |
| A decision page | [Encore App Structure](https://encore.dev/docs/app-structure) | Start with one service; add a service for stated reasons; choose calls or events by whether the caller needs the result; a review list for a proposed boundary. The closest published page to Sandloom's subject. |
| Progressive depth | [FastAPI async](https://fastapi.tiangolo.com/async/), [React](https://react.dev/learn/state-a-components-memory), [Google illustrations](https://developers.google.com/tech-writing/two/illustrations) | TL;DR, then a story, then "Very Technical Details". Collapsible Deep Dive after the core model. Big picture, then subsystems. |

**Inference.** Sandloom's guarantees answer a failure the reader may never have seen. That makes problem-first more important here than for a utility CSS page. Temporal is the nearest comparison and splits the job over several pages; Sandloom has one page per concept today, so one page must do both, with a short problem statement followed by the contract.

### Examples and tested code

- **Early:** the first code block on [Encore Pub/Sub](https://encore.dev/docs/ts/primitives/pubsub) is the first section; [Tailwind](https://tailwindcss.com/docs/styling-with-utility-classes) opens with the worked example; [ISR](https://nextjs.org/docs/app/guides/incremental-static-regeneration) gives a minimal example then a numbered "how this example works". Temporal's concept pages are the exception, with [no code on Understanding Temporal or Workflow Execution](https://docs.temporal.io/workflow-execution) (**Inference:** they serve several SDKs).
- **Tested:** [rustdoc](https://doc.rust-lang.org/rustdoc/write-documentation/documentation-tests.html) compiles and runs examples, with `#` hiding setup, and `compile_fail` for deliberate errors. The [Rust Cookbook](https://raw.githubusercontent.com/rust-lang-nursery/rust-cookbook/master/CONTRIBUTING.md) tests with skeptic. FastAPI has [`docs_src`](https://github.com/fastapi/fastapi/tree/master/docs_src) next to [`tests/test_tutorial`](https://github.com/fastapi/fastapi/tree/master/tests/test_tutorial). Google says [test samples, and do not reuse unit tests as samples](https://developers.google.com/tech-writing/two/sample-code) because "the only goal of a sample program is to educate". Sandloom already includes example files with `--8<--`.
- **Teaching through failure:** the [Rust Book](https://doc.rust-lang.org/book/ch00-00-introduction.html) marks code that does not compile or panics; [Rust by Example](https://doc.rust-lang.org/rust-by-example/scope/move.html) leaves commented-out erroring lines to uncomment; [useState](https://react.dev/reference/react/useState) pairs wrong and fixed code.

### Common failures these sources warn about

| Failure | Source |
| --- | --- |
| Experts forget what newcomers lack | [Google audience](https://developers.google.com/tech-writing/one/audience) |
| Types bleed into each other; explanation scattered through reference harms both | [Diátaxis map](https://diataxis.fr/map/), [reference vs explanation](https://diataxis.fr/reference-explanation/) |
| How-tos that describe machine operations, not user goals | [Diátaxis how-to](https://diataxis.fr/how-to-guides/) |
| Explaining how a thing works for paragraphs before the reader can act | [Supabase structure](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md) |
| Calling something easy | [Google tone](https://developers.google.com/style/tone), [Django](https://docs.djangoproject.com/en/dev/internals/contributing/writing-documentation/), [Next.js](https://nextjs.org/docs/community/contribution-guide), [GitLab](https://docs.gitlab.com/development/documentation/styleguide/) |
| Important facts in notes; too many notices | [Google notices](https://developers.google.com/style/notices), [GitLab](https://docs.gitlab.com/development/documentation/styleguide/), [Supabase elements](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/02-elements.md) |
| Positional references to earlier text | [Cloudflare how-to](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/how-to/), [Supabase voice](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md) |
| The primary answer only in a tab or collapsible | [Cloudflare AI consumability](https://developers.cloudflare.com/style-guide/how-we-docs/ai-consumability/) |
| Genre titles (Overview, Introduction, How it works) | [GitLab concept](https://docs.gitlab.com/development/documentation/topic_types/concept/), [Cloudflare concept](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/) |
| Stale or placeholder pages; time words such as "currently" | [Supabase structure](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md), [Google best practices](https://google.github.io/styleguide/docguide/best_practices.html) |
| FAQs as the main documentation | [Write the Docs beginner's guide](https://www.writethedocs.org/guide/writing/beginners-guide-to-docs/) |
| Contrived examples, or examples with hidden requirements | [Microsoft code examples](https://learn.microsoft.com/en-us/style-guide/developer-content/code-examples) |

The leaders break these rules: Laravel's Queues introduction says easily, FastAPI's Dependencies page says very simple, and [Django's own how-to](https://docs.djangoproject.com/en/stable/howto/custom-management-commands/) has later snippets elided with `# ...`.

## Page anatomy per type

Each rule states what a writer can check. Rules without a citation in the row are **Inference**: a synthesis for Sandloom.

### Concept

Opens with the problem, then the contract. Omits steps, full option lists, internals as main text.

| # | Section | Rule | Evidence |
| --- | --- | --- | --- |
| C1 | Title | Noun phrase naming the subject; reads naturally after the word About. Not Overview, Introduction, How it works. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/), [GitLab](https://docs.gitlab.com/development/documentation/topic_types/concept/), [Google](https://developers.google.com/style/headings) |
| C2 | Problem | One or two sentences stating a concrete failure or need, with no Sandloom term in them. | [Why Temporal?](https://docs.temporal.io/evaluate/why-temporal), [Laravel](https://laravel.com/docs/13.x/queues), [Encore outbox](https://encore.dev/docs/go/how-to/pubsub-outbox) |
| C3 | Contract | One self-contained paragraph: what it is and what is promised, in checkable words (at least once, not ordered), with no reassuring adjectives and no pronoun reaching back to the title. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/) |
| C4 | New terms | Every term the contract uses is defined there, linked, or already defined on the pages a reader must have read. At most two new terms per sentence (**Inference**, the number is mine). | [Google words](https://developers.google.com/tech-writing/one/words) |
| C5 | Smallest example | Complete and tested, about 10–25 lines, within the first screen after the contract. | [Encore](https://encore.dev/docs/ts/primitives/pubsub), [Tailwind](https://tailwindcss.com/docs/styling-with-utility-classes), [Next.js](https://nextjs.org/docs/app/guides/incremental-static-regeneration) |
| C6 | What happens, including failure | A numbered step-through of that example, with at least one crash or retry case. A diagram is optional and always has a text equivalent. | [How Temporal works](https://docs.temporal.io/encyclopedia/architecture/how-temporal-works), [Rust](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/) |
| C7 | Boundaries | Flat bullets stating what it is not and what it does not cover. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/), [Google documents](https://developers.google.com/tech-writing/one/documents) |
| C8 | Choose | Comparison table only for a real either/or (`@on` or a task). For event or contract, link `between/index.md`, which already does this. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/), [Encore](https://encore.dev/docs/app-structure) |
| C9 | What you get / declare | Keep this Sandloom table, after the example. At most 5 ± 1 rows; the rest links to Reference. | [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md); the placement is **Inference** |
| C10 | What breaks it | Keep, as the error-code table the strategy proposes. Top codes only, plain symptom first. | Strategy §3.3 |
| C11 | Go deeper | Links: how-to, recipes, reference, ADR. Guarantee ids and internals go here, collapsible; the promises themselves stay in C3. | [React](https://react.dev/learn/state-a-components-memory), [Cloudflare AI consumability](https://developers.cloudflare.com/style-guide/how-we-docs/ai-consumability/) |
| C12 | Excluded | No numbered procedures; no setting names except as links; one concept per page. | [Diátaxis](https://diataxis.fr/explanation/), [Red Hat](https://redhat-documentation.github.io/modular-docs/), [Good Docs](https://www.thegooddocsproject.dev/template/concept) |

Diátaxis's explanation type is wider than this: it welcomes history, opinion and alternatives, read away from the work. **Inference:** Sandloom's ADRs and "Design rationale" links hold that part; a concept page here is closer to Cloudflare's mental-model page.

### Tutorial

Opens with the end result and time. Omits options, alternatives, and "why" beyond a sentence.

| # | Rule | Evidence |
| --- | --- | --- |
| T1 | First screen states what gets built, how long it takes and the final result. | [Encore uptime](https://encore.dev/docs/tutorials/uptime) (about 30 minutes, then a screenshot), [Diátaxis](https://diataxis.fr/tutorials/) |
| T2 | Pinned versions and complete prerequisites; a link for when stuck. | [Django part 1](https://docs.djangoproject.com/en/stable/intro/tutorial01/), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/tutorial/) |
| T3 | Every step ends in visible output, shown literally in a fenced block. | [Diátaxis](https://diataxis.fr/tutorials/), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/tutorial/) |
| T4 | One path, no alternatives; explanations are links. | [Diátaxis](https://diataxis.fr/tutorials/) |
| T5 | Each step's heading gives full context, so a reader landing mid-page knows where they are. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/tutorial/) |
| T6 | Run it end to end in CI; a tutorial that breaks is the tutorial's fault. | [Diátaxis](https://diataxis.fr/tutorials/), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/tutorial/) |

[The Rust Book](https://doc.rust-lang.org/book/ch00-00-introduction.html) alternates concept chapters with three project chapters. [Google](https://developers.google.com/tech-writing/two/large-docs) recommends alternating concepts and practice too. The Sandloom quickstart and tutorial stay tutorials; a "project" can return after the concepts land (**Inference**).

### How-to

Opens with the goal and when to use it. Omits teaching and every method but the recommended one.

| # | Rule | Evidence |
| --- | --- | --- |
| H1 | Title is verb + object, no gerund. See [Disagreements](#disagreements) for the "How to" prefix. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/how-to/), [GitLab](https://docs.gitlab.com/development/documentation/topic_types/task/), [Google](https://developers.google.com/style/headings) |
| H2 | First sentence: what the reader can do and why; a link to the concept. Sandloom's `add-event.md` Goal / Use it / Do not use it already does this. Keep it. | [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md) |
| H3 | One task. At most about 6 steps per group, about ten per phase. | [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/how-to/), [Good Docs](https://www.thegooddocsproject.dev/template/how-to) |
| H4 | Conditions before instructions. Explanation over a sentence moves to a concept page. | [Diátaxis](https://diataxis.fr/how-to-guides/), [Supabase voice](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/how-to/) |
| H5 | End in order: verify, then optional blocks, then next steps. Never end on the last numbered step. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/how-to/) |
| H6 | Each step stands alone: full command, exact label, no positional reference. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/how-to/) |

### Cookbook recipe

A recipe solves a problem the reader already has and assumes the concepts. Diátaxis uses a [recipe as its model of a how-to](https://diataxis.fr/how-to-guides/); Django calls how-tos [recipes for key tasks](https://docs.djangoproject.com/en/dev/internals/contributing/writing-documentation/). **Inference:** in Sandloom, how-tos cover one framework operation (add an event); recipes combine several to solve a business problem, which matches Temporal's [patterns](https://docs.temporal.io/design-patterns) and [AI Cookbook](https://docs.temporal.io/ai-cookbook).

What the published ones do:

| Source | Shape |
| --- | --- |
| Temporal patterns: [Polling](https://docs.temporal.io/design-patterns/polling), [Entity Workflow](https://docs.temporal.io/design-patterns/entity-workflow), [Delayed Retry](https://docs.temporal.io/design-patterns/delayed-retry) | Overview, Problem, Solution, Implementation, When to use, Benefits and trade-offs, Comparison with alternatives, Best practices, Common pitfalls, Related. The shortest omits the middle three. Indexed by problem domain. |
| [Temporal claim check recipe](https://docs.temporal.io/ai/cookbook/claim-check-pattern-python) | Problem and what the recipe includes, how it works, code, then "Run the recipe" with prerequisites and configuration. |
| [Rust Cookbook](https://rust-lang-nursery.github.io/rust-cookbook/about.html), [rules](https://raw.githubusercontent.com/rust-lang-nursery/rust-cookbook/master/CONTRIBUTING.md) | One simple sentence as title stating a task. Description of 1–4 sentences. Standalone code a beginner can copy and run, tested. Tags for crates and categories. Avoids inline comments and glob imports; propagates errors, no shortcuts. "Instant access to working code." |
| [Cloudflare Workers examples](https://developers.cloudflare.com/workers/examples/), e.g. [A/B testing](https://developers.cloudflare.com/workers/examples/ab-testing/) | Mostly imperative titles ("Return JSON"), a description, then code with comments in several languages. Almost no prose. |
| [FastAPI How To](https://fastapi.tiangolo.com/how-to/) | Independent recipes that you can skip unless they apply; beginners are sent to the tutorial. |

**Recipe rules** (**Inference**, drawn from the rows above):

- R1. The title names the reader's problem or task in their words, not a framework feature: "Stop a duplicate webhook from charging twice", not "Using `@on` with durable inbox".
- R2. Problem: 1–2 sentences with a concrete situation.
- R3. Solution: 1–4 sentences naming the mechanisms used, each linked to its concept. No re-explanation.
- R4. Code: one complete, tested module path from an example app, included by marker; imports shown; no `...`. Comments only where intent is not visible.
- R5. Run and verify: a test or command, with literal expected output.
- R6. When to use / when not to, trade-offs, and one or two pitfalls with the `SANDLOOM-*` code that fires.
- R7. Variation: only monolith versus split if behaviour differs. No menu of options.
- R8. Related: at most 3–5 links (Good Docs' limit).
- R9. Index by problem domain with a one-line summary per recipe; recipes are independent of each other.

### Reference

Opens with a one-line statement and a table. Omits explanation, how-to and opinion.

| # | Rule | Evidence |
| --- | --- | --- |
| F1 | Quick-reference table first. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/reference/), [Tailwind padding](https://tailwindcss.com/docs/padding), [Next.js](https://nextjs.org/docs/community/contribution-guide) |
| F2 | Same facts in the same order per entry: type, default, required, constraints. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/reference/), [useState](https://react.dev/reference/react/useState) |
| F3 | Self-identifying entry names: `sandloom.sdk.events.on.dead_letter`, not `dead_letter` under a heading. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/reference/) |
| F4 | Literal values for ranges, defaults and limits; no vague quantities. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/reference/) |
| F5 | Nothing collapsed or tabbed. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/reference/) |
| F6 | Structure mirrors the product; alphabetical or API order; do not chunk into fives. | [Diátaxis](https://diataxis.fr/reference/), [Next.js](https://nextjs.org/docs/community/contribution-guide), [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md) |
| F7 | A short usage example per entry is allowed; a story is not. Link out for why and how. | [Diátaxis](https://diataxis.fr/reference/), [reference vs explanation](https://diataxis.fr/reference-explanation/) |

[React's reference](https://react.dev/reference/react/useState) adds Usage and Troubleshooting to each API page; [Stripe's entries](https://docs.stripe.com/api/idempotent_requests) open with code then carry behavioural prose. See [Disagreements](#disagreements).

### Troubleshooting and error pages

| # | Rule | Evidence |
| --- | --- | --- |
| E1 | Entry title is the verbatim message, or an observable symptom in the reader's words. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/troubleshooting/), [GitLab](https://docs.gitlab.com/development/documentation/topic_types/troubleshooting/), [useState](https://react.dev/reference/react/useState), [Supabase](https://supabase.com/docs/guides/troubleshooting) |
| E2 | Fixed internal order: Cause, Fix, Verify, in bold labels. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/troubleshooting/) |
| E3 | The message in a fenced block, unparaphrased. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/troubleshooting/), [GitLab](https://docs.gitlab.com/development/documentation/topic_types/troubleshooting/) |
| E4 | The fix is a command or code, not a general instruction to check something. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/troubleshooting/) |
| E5 | Most frequent first; a data-loss failure goes ahead of it. Workarounds state their cost. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/troubleshooting/) |
| E6 | Inline until about five entries, then its own page. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/troubleshooting/), [GitLab](https://docs.gitlab.com/development/documentation/topic_types/troubleshooting/) |
| E7 | Per-code catalogue stays reference; this page covers symptoms and multi-cause problems that codes do not capture. | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/troubleshooting/) |

## Style rules

| # | Rule, checkable | Evidence |
| --- | --- | --- |
| S1 | Address the reader as "you"; "we" only for the Sandloom team, tutorials included (see Disagreements). | [Google](https://developers.google.com/style/highlights), [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md) |
| S2 | Active voice; name the actor. Passive only when the actor does not matter. | [Google voice](https://developers.google.com/style/voice), [Next.js](https://nextjs.org/docs/community/contribution-guide) |
| S3 | Present tense, including for what code does. "Will" only for something that really happens later, such as asynchronous delivery. | [Google tense](https://developers.google.com/style/tense), [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md) |
| S4 | One idea per sentence. Flag anything over 26 words; a long sentence hiding "or" becomes a list. | [Google](https://developers.google.com/tech-writing/one/short-sentences), [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md), [Microsoft](https://learn.microsoft.com/en-us/style-guide/global-communications/writing-tips) |
| S5 | Paragraphs 3–5 sentences, first sentence carries the point. Past 7, split. | [Google](https://developers.google.com/tech-writing/one/paragraphs) |
| S6 | One word per concept, across all pages. Keep a word list; check it before review. | [Google](https://developers.google.com/tech-writing/one/words), [Microsoft](https://learn.microsoft.com/en-us/style-guide/global-communications/writing-tips), [Supabase word list](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/WORD_LIST.md) |
| S7 | Replace "this", "it", "that" with the noun when another noun is near or more than five words intervene. | [Google](https://developers.google.com/tech-writing/one/words), [Next.js](https://nextjs.org/docs/community/contribution-guide) |
| S8 | Ban: simply, just, easy, easily, quick, obviously, robust, powerful. | [Google](https://developers.google.com/style/tone), [Django](https://docs.djangoproject.com/en/dev/internals/contributing/writing-documentation/), [Next.js](https://nextjs.org/docs/community/contribution-guide), [GitLab](https://docs.gitlab.com/development/documentation/styleguide/) |
| S9 | Ban time words: currently, now, new, soon, latest (changelogs and upgrade guides excepted). | [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md) |
| S10 | Do not write about the page ("This page explains"). Ban "as above", "below". | [GitLab](https://docs.gitlab.com/development/documentation/styleguide/), [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/how-to/) |
| S11 | Headings are specific, unique across the site, sentence case, at most 70 characters. A section heading works when read alone. | [Google](https://developers.google.com/style/headings), [GitLab](https://docs.gitlab.com/development/documentation/topic_types/) |
| S12 | No two headings in a row without text. Three heading levels at most on a concept page (the limit is **Inference**; GitLab allows up to H5). | [Google](https://developers.google.com/tech-writing/two/large-docs), [Microsoft](https://learn.microsoft.com/en-us/style-guide/scannable-content/headings) |
| S13 | At most 5 ± 1 items in a group, including top-level sections; lookup surfaces excepted. | [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md) |
| S14 | Admonitions: only when missing it would change the outcome and it is not needed to succeed. First sentence is the impact. Never two in a row, never restated in the body. | [Google](https://developers.google.com/style/notices), [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/02-elements.md), [GitLab](https://docs.gitlab.com/development/documentation/styleguide/) |
| S15 | Analogy: at most one per page, followed by the literal statement it stands for. Prefer a technical comparison (Supabase's `WHERE` clause) to a story. | [Good Docs](https://www.thegooddocsproject.dev/template/concept), [Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security); limit is **Inference** |
| S16 | Diagrams support prose, the takeaway is in nearby text, about one paragraph's worth of content, caption written first. | [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/02-elements.md), [Google](https://developers.google.com/tech-writing/two/illustrations) |

**Code samples**

| # | Rule | Evidence |
| --- | --- | --- |
| K1 | Included from a file a test runs. Inline only for one-line illustrations. | Strategy §3.5; [FastAPI](https://github.com/fastapi/fastapi/tree/master/docs_src) re-checked |
| K2 | Complete: imports, file name. A reader can paste and run it. | [Next.js](https://nextjs.org/docs/community/contribution-guide), [Google](https://developers.google.com/tech-writing/two/sample-code), [Mintlify](https://learn.mintlify.com/courses/agent-friendly-docs/write-for-agents) |
| K3 | Preceded by a sentence; followed by expected output when output exists. Never a block with no lead-in. | [Google](https://developers.google.com/style/code-samples), [Microsoft](https://learn.microsoft.com/en-us/style-guide/developer-content/code-examples) |
| K4 | Realistic domain values, no `foo`, not contrived. | [Microsoft](https://learn.microsoft.com/en-us/style-guide/developer-content/code-examples), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/) |
| K5 | Length: concept 10–25 lines; recipe a full module path; longer belongs in a tutorial. Lines at most 80 characters. The ranges are **Inference** from Encore's 10-line and Rust's 3–20-line samples. | [Google line length](https://developers.google.com/style/code-samples), [Rust](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html) |
| K6 | Show the wrong way when the wrong way is the common instinct (`db().commit()`), and mark it. Run it as a test that expects the error. | [Google anti-example](https://developers.google.com/tech-writing/two/sample-code), [rustdoc `compile_fail`](https://doc.rust-lang.org/rustdoc/write-documentation/documentation-tests.html), [useState](https://react.dev/reference/react/useState) |
| K7 | Show exception handling only when it is the point. | [Microsoft](https://learn.microsoft.com/en-us/style-guide/developer-content/code-examples) |

## Section structure compared

Levels count what the reader navigates, not on-page headings, and are approximate. "Index" means the structure came from the site's `llms.txt`, not the rendered sidebar.

| Site | Top-level split | Learning order | Levels | Concepts live in | Recipes live in | Seen via |
| --- | --- | --- | --- | --- | --- | --- |
| [Django](https://docs.djangoproject.com/en/stable/) | Index by layer (model, view, template…); type shown in URL (`/topics/`, `/ref/`, `/howto/`) | First steps, then getting help, then how docs are organised, then layers | 3 | Topic guides | `/howto/` | Page |
| [FastAPI](https://fastapi.tiangolo.com/) | Learn, Reference, Resources, About, Release notes | Tutorial, Advanced, Deployment, How To | 3 | Inside the linear tutorial, plus a few pages (`async`) | How To - Recipes, under Learn | `mkdocs.yml` |
| [Rust Book](https://doc.rust-lang.org/book/) | One linear book | 21 chapters plus appendix, with project chapters 2, 12, 21 | 2 | Chapters | [Rust by Example](https://doc.rust-lang.org/rust-by-example/) and the Cookbook are separate books | `SUMMARY.md` |
| [React](https://react.dev/learn) | Learn, Reference | Quick Start, Describing the UI, Adding Interactivity, Managing State, Escape Hatches | 3–4 | Learn pages | Usage on each reference page | Index |
| [Tailwind](https://tailwindcss.com/docs) | 18 flat groups: Getting started, Core concepts, Base styles, then utilities by CSS area | Short start, then lookup | 2 | Core concepts (one group) | None | Page |
| [Laravel](https://laravel.com/docs/13.x) | Prologue, Getting Started, Architecture Concepts, The Basics, Digging Deeper, Security, Database, Eloquent, AI, Testing, Packages | Basics, then deeper | 2 | Mixed into long topic pages; Architecture Concepts group | Inside topic pages | Page |
| [Next.js](https://nextjs.org/docs) | Getting Started, Guides, API Reference, Glossary, Architecture, Community | Numbered getting-started pages; Reference alphabetical | 2–3 | Getting Started, Architecture | Guides ("How to …") | Index, contribution guide |
| [Temporal](https://docs.temporal.io/) | Evaluate, core primitives, SDK guides, Production, Cloud, CLI, References, Troubleshooting, Best Practices, Design Patterns, Guides, AI Cookbook | Home page offers quickstart, developer guide, deploy; Evaluate holds the why and vocabulary | 3 | Evaluate and encyclopedia | Design Patterns, Guides, AI Cookbook, each top-level | Index |
| [Encore](https://encore.dev/docs) | Getting started, Building (TS, Go), Deploying, Observability, Operations, Billing, Guides, Reference, Community | What is it, quickstart, Understanding Encore, tutorials | 3 | Getting started, App Structure, primitives | How-to per language; Guides | Index |
| [Cloudflare Workers](https://developers.cloudflare.com/workers/) | Per product, then Overview, Get started, Examples, Tutorials, Best practices, Configuration, Runtime APIs… | Get started, then free | 3–4 | Scattered `concepts/` folders | Examples, high in the product | Index |
| [Supabase](https://supabase.com/docs) | Per product module; Reference per SDK; Troubleshooting separate | Getting started, then product | 3 | Explainers within a product | Guides; a separate troubleshooting section | Index, style guide |
| [Stripe](https://docs.stripe.com/) | Get started, Payments, Revenue, Money management, Developer resources | Get started, then product | not measured | Short pages inside products, e.g. [lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle) | Quickstarts | Home page, partial |
| Sandloom now | Intro, Getting Started, Modules, Infrastructure, Admin Console, Operate, Reference, Project | Quickstart, tutorial | 3–4 | `Modules/*`, mixed with how-tos per subsystem | None | `zensical.toml` |

**Inference.** Two patterns: framework docs with a linear path (Django, FastAPI, Rust, React, Next.js), and platform docs split by product (Stripe, Cloudflare, Supabase, Encore). Sandloom has the second's tree and needs the first's opening. Most sites surface recipes at or near the top level. A top-level Cookbook tab after Modules fits both. Put the concept order into Getting Started: a short linear path (module, unit, event, contract) ahead of the tree. `between/index.md` and the glossary already exist and need only linking into it.

## Disagreements

| Question | Positions | Recommendation (**Inference**) |
| --- | --- | --- |
| Open a concept with the problem or the definition? | Problem: [Why Temporal?](https://docs.temporal.io/evaluate/why-temporal), [Laravel](https://laravel.com/docs/13.x/queues), [React](https://react.dev/learn/state-a-components-memory), [Encore outbox](https://encore.dev/docs/go/how-to/pubsub-outbox). Definition: [GitLab](https://docs.gitlab.com/development/documentation/topic_types/concept/), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/), [Good Docs](https://www.thegooddocsproject.dev/template/concept), [Durable Objects](https://developers.cloudflare.com/durable-objects/concepts/what-are-durable-objects/). Example first: [Tailwind](https://tailwindcss.com/docs/styling-with-utility-classes). Contrast first: [Rust](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html). | The concept is hard to grasp, so problem first, in at most two sentences, then a self-contained contract paragraph. Second-level pages may open on the definition. |
| One type per page? | Strict: [Diátaxis](https://diataxis.fr/map/), [Red Hat](https://redhat-documentation.github.io/modular-docs/), [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md) (one unit, though it permits the concept opener then procedures). Mixed: [Laravel](https://laravel.com/docs/13.x/queues), [Stripe Webhooks](https://docs.stripe.com/webhooks), [Django](https://docs.djangoproject.com/en/stable/topics/db/transactions/), [Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security) (understand, secure, reference), [GitLab](https://docs.gitlab.com/development/documentation/topic_types/) (per topic). | Mixed pages work where the reader knows the concept already, as with queues. Sandloom's concepts are new; separate them. A page may keep a short "use it" section linking to the how-to. |
| Analogy | Against: [Google tone](https://developers.google.com/style/tone) (avoid figurative language). Careful: [Good Docs](https://www.thegooddocsproject.dev/template/concept). For: [Rust restaurant](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html), [FastAPI burgers](https://fastapi.tiangolo.com/async/), [Diátaxis](https://diataxis.fr/explanation/). Technical: [Supabase](https://supabase.com/docs/guides/database/postgres/row-level-security), [Durable Objects actor model](https://developers.cloudflare.com/durable-objects/concepts/what-are-durable-objects/). | Technical comparison by default. A story only where it earns a section, always followed by the literal claim. English-as-second-language readers and agents are the reason. |
| How-to title | "How to": [Diátaxis](https://diataxis.fr/how-to-guides/), [Django](https://docs.djangoproject.com/en/stable/howto/custom-management-commands/), [Next.js](https://nextjs.org/docs/app/guides/incremental-static-regeneration). Forbidden prefix: [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/how-to/). Infinitive: [Google](https://developers.google.com/style/headings), [GitLab](https://docs.gitlab.com/development/documentation/topic_types/task/), [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/03-page-structure.md). Gerund: [Red Hat](https://redhat-documentation.github.io/modular-docs/). | Bare infinitive, as Sandloom already does ("Add an event"). |
| "How it works" as a title | Forbidden: [GitLab](https://docs.gitlab.com/development/documentation/topic_types/concept/), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/). Used: [How Temporal works](https://docs.temporal.io/encyclopedia/architecture/how-temporal-works). | Use What happens when … or a noun plus lifecycle, so the heading identifies itself out of context. |
| Comments in code | Avoid: [Rust Cookbook](https://raw.githubusercontent.com/rust-lang-nursery/rust-cookbook/master/CONTRIBUTING.md). Put it in comments because readers copy them: [Google](https://developers.google.com/tech-writing/two/sample-code). Teach by comments: [Rust by Example](https://doc.rust-lang.org/rust-by-example/scope/move.html), [Cloudflare examples](https://developers.cloudflare.com/workers/examples/ab-testing/). | Comment non-obvious intent and framework behaviour a copy would lose. Explanation goes in prose. |
| Omitted code | `...` acceptable: [GitLab](https://docs.gitlab.com/development/documentation/styleguide/). Comment only, no dots: [Google](https://developers.google.com/style/code-samples). Hidden tested lines: [rustdoc](https://doc.rust-lang.org/rustdoc/write-documentation/documentation-tests.html). Complete: [Next.js](https://nextjs.org/docs/community/contribution-guide), [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/). | Complete. A short fragment is a marked fragment, with the full file linked. |
| Negative wording | Avoid "don't": [Next.js](https://nextjs.org/docs/community/contribution-guide), [Supabase "use the positive form"](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md). Use flat negatives: [Cloudflare boundaries](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/), [Temporal](https://docs.temporal.io/evaluate/understanding-temporal). | Boundaries and enforced rules stay negative; instructions stay positive. |
| Person in tutorials | "We": [Diátaxis](https://diataxis.fr/tutorials/), [Rust](https://doc.rust-lang.org/book/ch02-00-guessing-game-tutorial.html). "You": [Google](https://developers.google.com/style/highlights), [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md). | "You" everywhere, for one rule across agent and human readers. |
| Tone | Friendly: [Microsoft](https://learn.microsoft.com/en-us/style-guide/top-10-tips-style-voice) (contractions, speech-like, even very short sales phrasing), [FastAPI](https://fastapi.tiangolo.com/async/). Reserved: [Google](https://developers.google.com/style/tone) (no let's-phrasing, not frivolous), [GitLab](https://docs.gitlab.com/development/documentation/styleguide/) (contractions restricted). | Plain and warm, contractions allowed outside Reference. Microsoft's consumer-oriented examples do not carry over. |
| Exercises | [React](https://react.dev/learn/state-a-components-memory) ends concept pages with challenges; no other site surveyed does. | Skip, with a test command or `SANDLOOM-*` probe as the cheap equivalent. |
| Repeat or link | Link: [Google](https://google.github.io/styleguide/docguide/best_practices.html). Accept some repetition: [Write the Docs](https://www.writethedocs.org/guide/writing/docs-principles/). Standalone chunks: [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/troubleshooting/). | See next section. |

## Agent-specific notes

**How strong the evidence is.**

- *Measured, narrow.* [Vercel](https://vercel.com/blog/agents-md-outperforms-skills-in-our-agent-evals): baseline 53%, skill 53% (never invoked in 56% of cases), skill with explicit instruction 79%, `AGENTS.md` docs index 100%. Vendor-run, one framework, about discovery, not prose. Re-checked; these match the strategy. [Gloaguen et al.](https://arxiv.org/abs/2602.11988) abstract: context files do not generally raise success, cost rises over 20%, overviews do not help, non-standard practices do. [Anthropic](https://www.anthropic.com/engineering/contextual-retrieval): retrieval failure falls 35% (49% with BM25, 67% with reranking) when each chunk carries its context, shown by adding generated context, not by rewriting prose.
- *Practice, unmeasured.* [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/), [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md), [Fern's writer](https://www.buildwithfern.com/post/agent-friendly-docs) (iterated a quickstart with Claude Code until it worked), [Mintlify's course](https://learn.mintlify.com/courses/agent-friendly-docs/write-for-agents) (vendor, cites no evidence). I found no controlled study of prose rules for agents.

**Rules specific to agents** (each is also harmless or good for humans unless listed under conflicts):

| # | Rule | Source |
| --- | --- | --- |
| A1 | The definition or first paragraph is accurate with the title and page removed, because retrieval returns it alone. | [Cloudflare overview](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/overview/), [concept](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/); [Anthropic's mechanism](https://www.anthropic.com/engineering/contextual-retrieval) |
| A2 | Every section names its subject. Do not lean on the heading to carry meaning. | [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md), [Cloudflare reference](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/reference/) |
| A3 | Exact identifiers and literal output. Agents match on literal text. | [Cloudflare how-to](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/how-to/) |
| A4 | Boundaries as flat bullets that can be lifted whole. | [Cloudflare concept](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/) |
| A5 | A terminal path for anything done in a UI (matters for the admin console). | [Supabase elements](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/02-elements.md) |
| A6 | Show one recommended path clearly; best practice has to be the obvious route. | [Supabase voice](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md) |
| A7 | Descriptions state the task and topics, because agents use them to decide what to fetch. | [Fern](https://www.buildwithfern.com/post/agent-friendly-docs), [Mintlify](https://learn.mintlify.com/courses/agent-friendly-docs/write-for-agents) |
| A8 | State only what the reader cannot infer; non-standard behaviour earns the words. For a FastAPI-trained agent: the framework commits, you do not. | [Gloaguen et al.](https://arxiv.org/abs/2602.11988); application is **Inference** |

**Conflicts with human readers.**

| Conflict | Evidence | Resolution (**Inference**) |
| --- | --- | --- |
| Self-containment versus brevity and one source of truth | [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/troubleshooting/) versus [Google](https://google.github.io/styleguide/docguide/best_practices.html) | Repeat *names and the contract sentence*; link everything else. Never copy tables. |
| Narrative problem-first versus lead with the contract | [Why Temporal?](https://docs.temporal.io/evaluate/why-temporal) versus [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/) | Short problem, then contract, as separate paragraphs. Each retrieves on its own. |
| Progressive disclosure versus extraction | [React Deep Dive](https://react.dev/learn/state-a-components-memory), [Cloudflare forbids hiding the primary answer](https://developers.cloudflare.com/style-guide/how-we-docs/ai-consumability/) | Collapse only what a reader can skip entirely. Guarantees that change code stay open. Check what the Zensical Markdown twin does with `???` before relying on it. |
| Hidden setup lines versus complete samples | [rustdoc](https://doc.rust-lang.org/rustdoc/write-documentation/documentation-tests.html) versus [Cloudflare](https://developers.cloudflare.com/style-guide/documentation-content-strategy/content-types/concept/) | Sandloom's `--8<--` samples show what they include, so no hidden lines. |
| Analogies and diagrams versus literal text | [Rust](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html) versus [Supabase diagrams](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/02-elements.md) | Humans keep both, always with the literal statement in text. |
| Respecting choice versus directing | [Supabase](https://github.com/supabase/supabase/blob/master/apps/docs/style-guide/01-voice-and-tone.md) says be kind to humans and possibly nudge LLMs. Stripe's Markdown tells agents not to use the Payment Intents API unless asked. | One recommended path, stated plainly, serves both. Alternatives are named with when they apply. |
| Agent-only content | [Stripe](https://docs.stripe.com/payments/paymentintents/lifecycle) (a `PlaintextOnly` tag in page state; meaning inferred from the name), [Fern's `llms-only`](https://www.buildwithfern.com/post/agent-friendly-docs). Both add setup and context agents need. | Do not start here: it forks the truth and no human reviews it. Add only for a gap the DX eval finds. |

## Implications for the three Sandloom pages

Measured in this checkout: `events.md` about 3,780 words, first code at line 42, 16 headings; `units.md` about 1,710 words, first code at line 30; `index.md` about 1,030 words, a tree at line 20. For scale, in text extraction with some page chrome: Stripe Webhooks about 5,500, Rust ownership about 4,400, Encore Pub/Sub about 1,500, Laravel Queues about 22,000. All notes are **Inference**.

**`docs/modules/between/events.md`.**

- The opening defines the event correctly but uses outbox, unit, consumer, delivery, at-least-once and claim before defining any of them, and states no problem. Start on a failure: a module commits an order and crashes before telling billing.
- The contract is there, split across sentences; pull it into one paragraph: sent if and only if the change commits; delivered at least once; no ordering; effect applied once in the consumer's database. "At-least-once delivery" and "No ordering guarantee" are the core, and they sit at line 210 and after.
- Add one step-through with a crash and a duplicate delivery. The current text states each outcome but never walks one example through it.
- Move to Reference: "What an event field may be" (its two tables are the largest block), the `@on` option table, bus backends, the `from_wire` details, and most of "Changing an event" (`event-versioning.md` exists). Move "Declare and publish" and "Consume" procedure into `add-event.md` and `add-consumer.md`.
- Keep the "What breaks it" table and the Guarantees block, with promises already in the contract.

**`docs/modules/database/units.md`.**

- It already leads with a clear definition, but opens on the term, not the need. Open on the reader's instinct: in FastAPI code you decide when to commit, and a crash between the commit and the publish loses the event. K6 fits: show the rejected `db().commit()`.
- "What you get" comes before any example; put the short `create` service first.
- Step through one request, success and failure: first `db()`, flush, publish, commit, dispatch, `on_commit`. The commit order appears three times in prose and tables and can become that one list.
- Move relay settings and "Kinds of unit" out: to Reference and Operate. Split ownership, savepoints and child tasks into sections a beginner may skip, labelled by when they matter ("When you start a child task").

**`docs/modules/index.md`.**

- The closest of the three to these rules: definition, then why, in two short paragraphs. Keep the opening.
- Add what it lacks: a minimal two-module example, one publishing and one reacting, because the isolation rule is abstract until a reader sees it. Encore's [App Structure](https://encore.dev/docs/app-structure) is the model for deciding when something becomes a module.
- "What a module may import" lists every banned spelling; a short allow-list suffices here, with spellings moved to the check reference.
- "Conventions inside a module" is lookup; link it. "Inspect before changing a module" belongs in tooling or the agent contract.

## Entry pages

Added after the owner rejected an Overview that passed every check. The first pass studied concepts and recipes, not entry pages, and the entry anatomy written from it had no evidence behind it. Sources retrieved 2026-11-02; each page's opening and headings were read, not the whole page.

| Page | First thing the reader meets | Toward trying it |
| --- | --- | --- |
| [Flutter home](https://flutter.dev/) | "Build for any screen", then one sentence of benefit | "Get started" at once; each of three benefits offers "Try it in DartPad", in the browser with no install |
| [Flutter docs](https://docs.flutter.dev/) | Cards by need, then "New to Flutter?" | Set up, then a learning pathway; a list for readers coming from other platforms |
| [Flutter architecture](https://docs.flutter.dev/resources/architectural-overview) | Its own long page under Resources, not on either entry page | Not an entry point |
| [Rails](https://rubyonrails.org/) | A promise aimed at agents, "from PROMPT to IPO", code easy for agents to write and for humans to review | Companies as proof, then guides |
| [Supabase docs](https://supabase.com/docs) | One sentence, then an Agent Prompt tab beside the CLI commands: text to paste into an agent | The prompt installs, inspects the project and suggests next steps |
| [Encore.ts](https://encore.dev/docs/ts) | Title, one-line subtitle, definition, then "Instead of stitching together Terraform, YAML…" | "New to Encore?" pointing to the quickstart, before "What you get" |
| [Convex](https://docs.convex.dev/understanding/) | Definition by comparison with React, a diagram | A section "For human and AI generated code" |
| [Svelte](https://svelte.dev/docs/svelte/overview) | Sentence, code, sentence | Shortest page surveyed |
| [Next.js](https://nextjs.org/docs) | "What is Next.js?" | "Copy for LLM" on the page |
| [Temporal](https://docs.temporal.io/) | "Build applications that never fail" | SDK icons, then a definition |
| [Laravel](https://laravel.com/docs/13.x) | Meet Laravel, then "Why Laravel?", on the installation page | A "Getting Started Using AI" section, with its agent tooling |
| [Lovable](https://docs.lovable.dev/introduction/welcome) | What it lets you build, in plain words | "Start building for free", then who it is for |

- **Common:** a short promise first, an action within the first screen, depth on other pages. None of the entry pages above carries its architecture diagram with several layers on the opening screen except Convex, whose diagram is three tiers.
- **Agents:** Supabase, Next.js, Rails, Laravel and Convex now address agents directly on or near the entry page. Copyable prompts are recent practice; I found no measurement of them.
- **Disagreement with the owner's choice, recorded:** among these pages only Encore's second paragraph names the pain, and "Why Temporal?" does so on a separate page. Opening an entry page on problems is the owner's decision. **Inference:** it suits Sandloom, whose value is in failures the reader wants to avoid, and whose readers include people who have not yet met those failures in their own code.
- **Why the last version failed, Inference:** the page was compliant, long, hedged and showed no benefit. Rule lists catch defects and do not produce desire, so the skill now requires one message, captured proof, a look at the render and the owner's choice.

## Reader jobs (jobs to be done)

Added on the owner's question whether the method belongs in the skill. Sources retrieved 2026-11-02.

- [Christensen Institute](https://www.christenseninstitute.org/theory/jobs-to-be-done/): a job is the progress a person tries to make in particular circumstances; people "hire" a product for it.
- [GitLab](https://handbook.gitlab.com/handbook/product/ux/jobs-to-be-done/), drawing on Kalbach: job statements follow Verb + Object + Clarifier and come in levels, from a main job through small jobs to micro-jobs.
- [Intercom](https://www.intercom.com/blog/using-job-stories-design-features-ui-ux/): user stories tie implementation to motivation and ignore context, situations and anxieties. Job stories frame the situation first.
- [GOV.UK content guidance](https://www.gov.uk/guidance/content-design/user-needs), the closest published use for content: every piece of content meets a valid user need, written as As a / I need / So that, with optional When and Because, in the users' own words, from evidence such as analytics and support data. Content designers are not user researchers.
- [Nielsen Norman Group](https://www.nngroup.com/articles/personas-jobs-be-done/): jobs and well-made personas are compatible.
- Temporal's Markdown pages ask agents to pass a `jtb` job-to-be-done parameter to their retrieval API, so at least one docs team models the reader's job as data.

I found no documentation style guide among those surveyed that names jobs to be done as its method. Diátaxis's how-to page makes the nearest point, that how-tos serve user goals. No source measures its effect on documentation.

**Inference.** Diátaxis says what kind of page to write; a job says for whom, at what moment, and what to cut. Both are needed, and a job statement gives C2's problem, the title and the cold-read question from one line. Adopted: one job story per page, kept in front matter; jobs as a cut test; job levels loosely matching entry page and concept, how-to and recipe, reference. Not adopted: interviews, outcome scoring, forces diagrams and job canvases. Sandloom has too few users for them, so its job statements are hypotheses until issues and the DX eval support them.

## What could not be verified

- **The fetch tool's summariser invented content.** On [How Temporal works](https://docs.temporal.io/encyclopedia/architecture/how-temporal-works) it produced a money-transfer example, a crash walkthrough and a code sample that are not on the page. So I downloaded raw text or Markdown for the pages cited and checked quotations and outlines against that. Rules from Microsoft, Cloudflare, Supabase, Next.js, Temporal and Encore were read raw. Other rows were summarised first and spot-checked by search, so a detail may still be off.
- **Sampled, not read whole:** Laravel Queues (introduction and outline), GitLab style guide (targeted), Red Hat modular docs (targeted), Rust ownership, React, Django and FastAPI pages (outline and targeted), Temporal patterns (outlines of three, openings of one), the Supabase style guide (`03` in full except one example; `01` and `02` in part), Stripe Webhooks read, Stripe API reference one entry.
- **Navigation:** the sidebars of Temporal, Encore, React, Next.js, Cloudflare and Supabase were not read as rendered; the table uses their `llms.txt`. Stripe's came from home-page strings and is partial. Cloudflare's Examples Markdown shows no filter; the rendered page may have one.
- **Not opened:**
  - O'Reilly Python Cookbook returned 403, so the classic Problem / Solution / Discussion form is not cited as evidence.
  - `docs.temporal.io/design-patterns/saga` does not exist at that path; Temporal's saga guidance was not read beyond the Distributed Transaction index.
  - The Zensical proposal ZAP-009 redirects to a roadmap; its content was not readable.
  - Klement's job-story article on jtbd.info did not download and the HBR article is paywalled, so neither is cited for its content.
  - No published account by Stripe of its writing process was found beyond the Markdoc post.
- **Stale strategy claims not re-checked and not relied on:** Mintlify's 66% traffic figure, the Ahrefs llms.txt figure, the Gloaguen percentage-point deltas (only the abstract was read), and the statement that Django does not execute its doc code.
- **Sample testing:** verified only for rustdoc, the Rust Cookbook, and FastAPI's test directories (existence, not contents). I did not establish whether Stripe, Temporal, Encore, Cloudflare or Laravel test their samples.
- **Stripe's `PlaintextOnly`** tag was seen in the page's serialised state. I infer from its name that it marks agent-only text and did not confirm it in rendered HTML.
- **No reader evidence.** No source measures whether problem-first explanations improve comprehension. The recommendations rest on what respected docs do and on my reading of the three pages, not on a test with Sandloom readers. The word and line limits labelled as mine are starting defaults.

## Sources

Retrieved 2026-11-01 to 2026-11-02. Inline links are the full list; grouped here by use.

- **Guides:** [Diátaxis](https://diataxis.fr/) (explanation, how-to, tutorials, reference, compass, map, how to use, reference vs explanation, colophon), [Divio](https://docs.divio.com/documentation-system/), [Google style](https://developers.google.com/style/highlights), [Google courses](https://developers.google.com/tech-writing), [Google doc best practices](https://google.github.io/styleguide/docguide/best_practices.html), [Microsoft style](https://learn.microsoft.com/en-us/style-guide/welcome/), [Write the Docs](https://www.writethedocs.org/guide/writing/docs-principles/), [Good Docs](https://www.thegooddocsproject.dev/template), [GitLab](https://docs.gitlab.com/development/documentation/), [Red Hat](https://redhat-documentation.github.io/modular-docs/), [Cloudflare style](https://developers.cloudflare.com/style-guide/), [Supabase style](https://github.com/supabase/supabase/tree/master/apps/docs/style-guide), [Next.js](https://nextjs.org/docs/community/contribution-guide), [Django](https://docs.djangoproject.com/en/dev/internals/contributing/writing-documentation/).
- **Team accounts:** [Stripe Markdoc](https://stripe.dev/blog/markdoc), [react.dev launch](https://react.dev/blog/2023/03/16/introducing-react-dev), [Cloudflare AI consumability](https://developers.cloudflare.com/style-guide/how-we-docs/ai-consumability/), [Fern](https://www.buildwithfern.com/post/agent-friendly-docs).
- **Pages studied:**
  - Stripe: [Payment Intents lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle), [Webhooks](https://docs.stripe.com/webhooks), [idempotent requests](https://docs.stripe.com/api/idempotent_requests), [Checkout quickstart](https://docs.stripe.com/checkout/quickstart).
  - Django: [transactions](https://docs.djangoproject.com/en/stable/topics/db/transactions/), [custom commands](https://docs.djangoproject.com/en/stable/howto/custom-management-commands/), [tutorial 1](https://docs.djangoproject.com/en/stable/intro/tutorial01/).
  - FastAPI: [tutorial](https://fastapi.tiangolo.com/tutorial/), [dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/), [async](https://fastapi.tiangolo.com/async/), [how-to](https://fastapi.tiangolo.com/how-to/).
  - Rust: [introduction](https://doc.rust-lang.org/book/ch00-00-introduction.html), [ownership](https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html), [guessing game](https://doc.rust-lang.org/book/ch02-00-guessing-game-tutorial.html), [Rust by Example](https://doc.rust-lang.org/rust-by-example/scope/move.html), [Cookbook](https://rust-lang-nursery.github.io/rust-cookbook/about.html), [rustdoc tests](https://doc.rust-lang.org/rustdoc/write-documentation/documentation-tests.html).
  - React: [state](https://react.dev/learn/state-a-components-memory), [Effects](https://react.dev/learn/synchronizing-with-effects), [useState](https://react.dev/reference/react/useState).
  - Tailwind: [utility classes](https://tailwindcss.com/docs/styling-with-utility-classes), [padding](https://tailwindcss.com/docs/padding).
  - Laravel: [Queues](https://laravel.com/docs/13.x/queues).
  - Next.js: [layouts and pages](https://nextjs.org/docs/app/getting-started/layouts-and-pages), [ISR](https://nextjs.org/docs/app/guides/incremental-static-regeneration), [page.js](https://nextjs.org/docs/app/api-reference/file-conventions/page).
  - Temporal: [Why](https://docs.temporal.io/evaluate/why-temporal), [Understanding](https://docs.temporal.io/evaluate/understanding-temporal), [How it works](https://docs.temporal.io/encyclopedia/architecture/how-temporal-works), [Workflows](https://docs.temporal.io/workflows), [Workflow Execution](https://docs.temporal.io/workflow-execution), [Design Patterns](https://docs.temporal.io/design-patterns), [AI Cookbook](https://docs.temporal.io/ai-cookbook).
  - Encore: [Understanding Encore](https://encore.dev/docs/ts/concepts/benefits), [App Structure](https://encore.dev/docs/app-structure), [services](https://encore.dev/docs/ts/primitives/services), [Pub/Sub](https://encore.dev/docs/ts/primitives/pubsub), [outbox](https://encore.dev/docs/go/how-to/pubsub-outbox), [uptime tutorial](https://encore.dev/docs/tutorials/uptime).
  - Cloudflare: [Durable Objects](https://developers.cloudflare.com/durable-objects/concepts/what-are-durable-objects/), [Workers examples](https://developers.cloudflare.com/workers/examples/).
  - Supabase: [RLS](https://supabase.com/docs/guides/database/postgres/row-level-security), [troubleshooting](https://supabase.com/docs/guides/troubleshooting), [one article](https://supabase.com/docs/guides/troubleshooting/disk-size-not-shrinking-after-deleting-data).
- **Agent evidence:** [Vercel](https://vercel.com/blog/agents-md-outperforms-skills-in-our-agent-evals), [Gloaguen et al.](https://arxiv.org/abs/2602.11988), [Anthropic](https://www.anthropic.com/engineering/contextual-retrieval), [Mintlify course](https://learn.mintlify.com/courses/agent-friendly-docs/write-for-agents).
- **Sandloom:** `docs/modules/between/events.md`, `docs/modules/database/units.md`, `docs/modules/index.md`, `docs/modules/between/add-event.md`, `zensical.toml`, the strategy (`docs-strategy-2026-10-25.md`), `design/admin-delivery/research.md` for the labelling convention.
