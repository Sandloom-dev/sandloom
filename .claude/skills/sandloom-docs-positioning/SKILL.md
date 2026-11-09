---
name: sandloom-docs-positioning
disable-model-invocation: true
description: Agree Sandloom's positioning with the owner — category, alternatives, advantages, pains, value, best-fit readers, core idea, one message, naming — in design/docs-writing/positioning.md. Use when a docs brief finds it unapproved, when the owner names a new pain, advantage or alternative, and when asked what Sandloom is, who it is for or how it compares.
---

# Agree Sandloom's positioning

Positioning is what the product is, for whom, against what, and why it wins. It lives in the owner's head and in a few files, and every page that sells or introduces Sandloom draws on it, so it is agreed once for the product in [positioning.md](../../../design/docs-writing/positioning.md) and each page brief ([sandloom-docs-brief](../sandloom-docs-brief/SKILL.md)) applies it. The fields follow April Dunford's components, with the core idea, one message and naming that the pages need.

## Steps

1. **Read what is settled.** Open `positioning.md`. A field marked *owner* stands until the owner or this conversation questions it. Work only on the fields that are drafted, missing or questioned. Done when you can list them.
2. **Gather candidates** for those fields, each with its source:
   - the owner's words: this conversation, the tm task and its notes, `kg_search` and `memory_retrieve`;
   - published wording, as candidates only: `README.md`, `website/index.html`, the production entry page (`git show origin/main:docs/index.md`);
   - the product itself, for advantages: the CLI (`uv run sandloom --help`), the SDK (`uv run sandloom docs`), the checks and their codes (`docs/reference/error-codes.md`), the standard modules;
   - the evidence: [agentic-pains.md](../../../design/docs-writing/agentic-pains.md), output you capture by running the product.

   A source you cannot reach is written down as "not available". Done when every open field has a candidate or is marked unknown.
3. **Draft the fields:**
   - **Category.** The kind of product the reader files it under, in words they already use.
   - **Alternatives.** What the reader would do without Sandloom, from several sides (framework, tooling, architecture, platform, doing nothing), and where each falls short for them.
   - **Advantages.** An inventory: each with plain words, its proof in the code and the pain or promise it answers. The owner ranks them; the top few are the main advantages.
   - **Pains.** The pains the product closes, each with its row in `agentic-pains.md` and the advantage that answers it.
   - **Value.** What the advantages give the reader's product, as outcomes over time.
   - **Best-fit readers.** Who cares most about that value, and who does not.
   - **Core idea.** The one phrase that explains why the value follows, in the owner's words, and its page form once the owner accepts one.
   - **One message.** The sentence a reader repeats to a colleague: category and what it is designed for.
   - **Naming.** The words the product is called and described with, the words avoided, and a plain form of the core idea and of each advantage for pages at level `new`.
4. **Ask the owner.** The owner decides fields, not cells: inventory rows and evidence are facts you check, and the owner ranks and chooses. Ask at most five questions a round, each with your proposal as the default, starting with the core idea, the alternatives and the main advantages, which only the owner knows. Use AskUserQuestion when you have it. Write each answer into its field marked *owner* with the date, quoting the owner where the words matter. Done when no field is open and the status line reads "approved by the owner, <date>".
