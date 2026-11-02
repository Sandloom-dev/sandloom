# Brief: Overview (`docs/index.md`)

Status: **approved by the owner, 2026-11-02**, reconstructed afterwards from the owner's decisions in the session that wrote the page (79a4d26). The owner's words are quoted in translation where they decided something.

## Purpose

- **Effect.** *Owner:* "the page must make the reader want to try the framework". The reader with an agent pastes the prompt; the reader without one opens the quickstart.
- **Place.** Readers arrive from the site's home page, README, search, or an agent sent to the docs. Next: the quickstart (`footer_next`), or the agent prompt; for a reader not ready yet, How Sandloom works, the examples, the standard modules or Reference.
- **Sign it worked.** Someone who never saw Sandloom can say in one sentence what it is and what it gives, and names the next step they would take. The outside developer shown the page stumbled on "modules": no term may stop that sentence.
- **Not its job.** Architecture (How Sandloom works), concepts and their terms (Concepts), every command and setting (Reference), comparing with alternatives (no page yet).

## Reader

- A developer who has never heard of Sandloom. Shown the page, an outside developer stumbled on "modules" at once: "for a new person it is not clear what modules are; immerse gradually".
- A vibe coder who steers an agent and judges by what runs.
- A coding agent that has never seen Sandloom and acts on literal text.

## Job

When I hear about Sandloom, I want to know what it is, which problems it solves and how, so I can decide whether to look further and where.

## One message

Sandloom is a Python backend framework designed for AI-driven development.

## Promise

- You can write the code yourself, but everything is tuned for coding agents: generators, rules the agent reads, and checks that fail when a rule is broken (owner's sentence, accepted as is).
- What a backend built on it gets out of the box: speed of development, reliability, scalability, and a split from a monolith into microservices without rewriting code. One paragraph.

## Core idea, in the owner's words

"Sandloom keeps your business logic atomic and isolated from each other and from the infrastructure." The meaning, not the wording: the parts of the business logic are isolated from one another and from the infrastructure. It opens the relief paragraph.

## Pains and reliefs

Framed as pains of agentic development, not of the reader's code and not of Sandloom. Source: [agentic-pains.md](../agentic-pains.md); the owner added pains 2–4.

| Pain | Relief |
| --- | --- |
| The architecture decays: every change and new feature costs more | Checks hold the boundaries however large the backend gets |
| The agent can't design an architecture and hold it in mind | It only has to understand the part it is changing |
| Tightly coupled code does not split across subagents | Several agents work side by side |
| In a complex project the agent is overloaded and misses connected things | Same as the second: a small part to understand |
| Rules in a prompt are requests | A broken rule fails a check |

## Evidence

- He et al., MSR 2026 ([arXiv 2511.04427](https://arxiv.org/abs/2511.04427)): the gain from Cursor lasted two months; code complexity rose 41% and stayed. The owner asked for the link.
- Not to claim: times ("20 minutes" was struck), any speed-up Sandloom brings (not measured).

## Words

- Not on this page: module, gate, event, contract, driver, outbox, kernel.
- Plain replacements: "parts of the product", "checks", "business areas", "each customer's data kept separate".

## Actions

- With an agent: a prompt to paste, the prerequisites, the one check to run before it says done. "For a framework built for agents, it is strange that the main page has nothing to give an agent."
- Yourself: the quickstart (from an empty folder to a backend running on your machine, tests and checks passing), then the tutorial (a small task list whose two parts talk to each other). Each link promises a different result.
- Both in the green "Start here" block of the production page, which gets two tabs, one per path. The person's path is prose, not a numbered list.

## Reuse from production

- "Start here" and "Who Sandloom fits": moved as is first, then edited.
- "Where to look next": four cards, like "Already have an application? Pick what you need next" on the site.

## Out

- The architecture figure, principles and choosing tables: their own page, "How Sandloom works".
- "How you work with it": removed.

## Tone

- Open on what Sandloom is and gives. Problems follow, as the world's problems: "we say how great we are, and right after it problems, as if we had problems" was rejected.
- Paragraphs, not bullets, in the argument.
- One voice, "you", throughout.
- Natural sentences: an idea that was right but "rickety in language and sentence form" needed another pass.

## Open

None.
