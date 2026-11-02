# Entry page

`type: entry` · Reader: a developer, a vibe coder or an agent who has never heard of Sandloom · Level: `new`

The first page a reader meets: Overview, a README, landing text. It decides whether they try Sandloom, so it starts from an approved brief ([sandloom-docs-brief](../../sandloom-docs-brief/SKILL.md)). At most 550 words of prose: body text, tab text and card lines; headings, card titles and the prompt block do not count. The prose lint counts them (W1). Over the cap, trim prose before dropping a card the brief agreed.

## Blocks

1. **Promise (no heading).** The bold first sentence is the one message: the category and what it is designed for. The rest of the paragraph says who writes the code, what the framework gives them, and what the reader's backend gets over time, as outcomes the reader wants. As many outcomes as the positioning has after merging the closest; no links or qualifiers here, because the next block carries the mechanisms.
2. **Why it is needed.** A heading that names the need in the world, not the product. First the way of working and its cost, with one linked study and its number; then the brief's argument groups, each as cause and effect in the reader's words. A claim whose fine print the reader may want links it, at most one link per paragraph. Then the relief: the core idea, and each group's relief in the same order and with the same nouns as its pain. A promise outcome no group carries gets its mechanism in one plain sentence or short paragraph after the reliefs. A pain or advantage the argument left out stays off the page, however true.
3. **Start here.** One `tip` admonition with a tab per way in. The agent tab: a prompt to paste, the prerequisites, and the check that says done. The tab for doing it yourself: the first steps, each ending in what the reader has afterwards, in product words. Below it, one line on how agents read the docs (`llms.txt`, Markdown per page).
4. **Who it fits.** Whom it fits, and whom it probably does not, in the same plain voice.
5. **Where to look next.** Cards to the destinations the brief agreed, one verb-led line each.

## Voice

- **Confidence first.** The page opens on what Sandloom is and gives; the pains follow as pains of the way of working, never as Sandloom's own.
- **Mirror.** The relief answers the pains group by group with their nouns, so the reader sees each pain closed.
- **Outcomes, then mechanisms.** Say what the backend does and keeps doing as it grows, then what makes it so. An adjective with nothing behind it goes.
- **Simple, not shallow.** Explain how, in words a complete outsider understands, with no deep technical detail; the detail lives one link deeper. A term the brief keeps off stays off everywhere on the page, card lines included.
- **Prose for the argument.** Paragraphs carry the promise, the pains and the fit; lists and cards hold only choices.
- **A result per link.** Each action says what the reader ends with, and no two promise the same result.
- **Every reader's product.** No running example from one business domain (a shop, a clinic): a visitor building something else stops to translate. Speak of the reader's own backend, its parts and features.
- **Only measured numbers.** A time or a speed-up nobody measured stays off the page.

## Not on this page

Architecture, principles, numbered workflows, reference tables, and any Sandloom term the reader has not met: they live one link deeper.

## Models

- [Overview](../../../../docs/index.md) with [its brief](../../../../design/docs-writing/briefs/overview.md): read it for what each block does, never for its sentences.
- [Supabase](https://supabase.com/docs): the agent prompt.
- [Flutter](https://flutter.dev/): the try-it action.
