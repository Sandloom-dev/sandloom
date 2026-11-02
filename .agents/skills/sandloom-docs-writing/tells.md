# Machine-prose tells

Text that reads assembled rather than written. A model picks the choice that fits the most readers; a writer picks for one reader and one subject. Each tell below is that default choice. Ranked strongest first: tiers 1 and 2 justify a rewrite on one sighting; a tell marked *weak alone* needs another tell in the same paragraph.

Adapted from [humanizer](https://github.com/blader/humanizer) 3.1.0 by Siqi Chen (MIT License, notice below), which draws on Wikipedia's [Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing); the Sandloom-specific tells and the docs exceptions are ours. Examples come from outside Sandloom on purpose: copy the move, never the sentence.

## How to use it

Mark every tell in the page first, strongest first, at sentence and paragraph scale: three parallel examples, or the same closer after every section, are the same tell at a larger scale. Then rewrite each paragraph around its point, rather than patching phrases. A rewrite keeps every claim and adds none: a fact, number, name or guarantee the source does not hold is an error. Then search again for the tells that survive rewrites most often: P1, P2, P6, P8 and P19.

Leave a tell alone inside a quotation, an identifier, a verbatim error message, a heading that names a real thing, or a sentence that discusses the phrase. Several tells together are the signal; one weak tell is not.

## Tier 1: staging instead of stating

- **P1. Not X but Y.** "Not just a framework, but a platform"; "This does not mean X. It means Y."; a clipped tail ("…, no guessing"). The negative half names what nobody claimed. Keep a contrast only when it corrects a belief the reader holds (A8: what a FastAPI reader gets wrong), or when both halves carry facts.
- **P2. Closers and fragments.** A one-sentence paragraph that restates the paragraph before; "That is the real win."; a sentence after an example that names what it showed; a row of fragments ("Fast. Reliable. Yours."). Cut it, or merge the fragments into one claim.
- **P3. Sayings that sound deep.** "At its core", "the real question is", "X is the language of Y". Write the specific claim.
- **P4. Run-up before the point.** "Let's dive in", "Here's what you need to know", "It is worth noting that", a question the next sentence answers. Start on the point.
- **P5. Arguing with no one.** "To be clear", "This isn't about…", "A tempting approach would be…" for an option no reader weighs. Keep a wrong way only where it is the common instinct, shown as K6 does.

## Tier 2: rhythm by rule

- **P6. Forced triads.** Ideas in threes because three sounds complete: three adjectives, three parallel examples, three short facts and a lesson. Keep as many items as the meaning has; two is fine, four is fine. The lint's P6 also fires on a clause followed by "X and Y"; waive a hit that is not three parallel items, with that reason, rather than reshaping a good sentence.
- **P7. Repeated openers.** Several sentences or paragraphs in a row starting alike ("Sandloom… Sandloom…", "Each…, Each…", "Whether you…"). Merge, or begin with the action.
- **P8. Chained ", so".** Consequence after consequence joined by ", so" or "so that", sentence after sentence. Vary how cause is said, or let order carry it.
- **P9. Dashes as the universal joint.** Em and en dashes between clauses. Use a period, comma, colon or parentheses. Code, paths and ranges keep theirs.
- **P10. Stacked qualifiers.** "Could potentially", "in many cases it may". Keep a qualifier the code supports and say it once. *Weak alone.*
- **P11. Unequal series.** Items of one list in different grammar: "fast, reliability and it scales".

## Tier 3: inflation

- **P12. Model words.** Delve, crucial, pivotal, robust (figurative), seamless, leverage, unlock, empower, supercharge, elevate, journey, landscape, showcase, underscore, testament, vibrant, meticulous, additionally. Write the plain word or the fact.
- **P13. Inflated significance.** "A pivotal step", "sets the stage for", "the future looks bright", a stock "challenges and outlook" paragraph. Keep the fact, drop its importance; end on the last concrete fact.
- **P14. Shallow -ing riders.** "…, ensuring reliability", "…, highlighting the need for". Keep the fact; keep the rider only when the code backs it as a separate claim.
- **P15. Sales language.** Groundbreaking, renowned, stunning, rich, a diverse array, "commitment to". Say what the thing is and does.
- **P16. Borrowed authority.** "Experts agree", "industry reports show". Name the source and what it found, linked, or cut the claim.
- **P17. Avoiding is and has.** "Serves as", "stands as", "boasts", "features", "offers". Write is, are, has.
- **P18. Vague association.** "Tied to", "associated with", "in connection with". Name the relation: calls, owns, publishes, runs in.

## Tier 4: Sandloom docs

- **P19. Bold and labels by rule.** Bold on words with no reason; every list item with a bold label and a colon whose label adds nothing. The entry page's bold first sentence and the troubleshooting labels (E2) are slots, not decoration.
- **P20. Personified artifacts.** "The framework wants", "the check is happy", "Sandloom believes". Say what the code does.
- **P21. Spec-sheet voice.** Noun stacks and feature lists in place of what the reader does: "robust multi-tenant isolation capabilities".
- **P22. Pasted inventory.** Items of the positioning or brief turned into sentences of one shape, with no cause or consequence between them. The brief's wording is meaning; say each item as the reader would explain it.
- **P23. Writing about the page.** "This page explains", "the table below compares", "as mentioned above" (S10). Cut it, or link.
- **P24. Heading repeated as the first sentence.** "## Retries" then "Retries matter." Start on the content.
- **P25. Re-explaining what the level knows.** A `building` page that re-defines a unit, a how-to that re-tells the concept. Link down instead ([audience.md](audience.md)).
- **P26. Summary transitions.** "In short", "Overall", "To sum up" restating what the reader just read. *Weak alone* at the end of a long concept page.

## License notice

Tiers 1–3 and the method are adapted from humanizer:

> MIT License. Copyright (c) 2025 Siqi Chen. Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions: The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
