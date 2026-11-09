---
name: sandloom-docs-brief
disable-model-invocation: true
description: Agree with the owner what one Sandloom docs page must say before it is written — purpose, audience and level, job, one message, promise, the argument (the strongest pains and advantages, grouped), evidence, words, actions, reuse and what stays out. Use before writing an entry page, README or landing text, before rewriting a page from scratch, and when the owner rejects a draft that passed the checks.
---

# Brief a Sandloom docs page

A page is good when it says what the owner means to the reader who arrives. Rules cannot supply either: the meaning lives in the owner's head, and the reader's vocabulary is learned from someone who read the page. The brief collects both for one page and gets them agreed while changing them is still cheap. It applies the product's positioning ([sandloom-docs-positioning](../sandloom-docs-positioning/SKILL.md)); the page itself is written with [sandloom-docs-writing](../sandloom-docs-writing/SKILL.md).

Models: [the positioning](../../../design/docs-writing/positioning.md), [the Overview brief](../../../design/docs-writing/briefs/overview.md).

## Steps

1. **Check the positioning.** Open `design/docs-writing/positioning.md` and read the fields the page needs; the alternatives table only for a page that compares. If its status is not "approved by the owner", or this conversation questions a field the page relies on, run sandloom-docs-positioning first; its questions take the whole round, and this brief's wait for the next. Done when the fields the page needs are approved.
2. **Gather** what already says something about the page's subject, each with its source:
   - the owner's words: this conversation, the tm task and its notes, `kg_search` and `memory_retrieve`, [the owner's preferences](../../../design/docs-writing/owner-preferences.md);
   - the page in production (`git show origin/main:<path>`, or the checkout's copy where history is out of reach), and the pages that link to it;
   - the evidence: [agentic-pains.md](../../../design/docs-writing/agentic-pains.md), the research, output you capture by running the product;
   - the reader's words: issues, DX eval failures, what someone shown the page stumbled on;
   - [the concept map](../../../design/docs-writing/concept-map.md): the page's concepts, their level and home, and what the reader must know first.

   A source you cannot reach goes into the brief as "not available", and its field stays a question for the owner. Done when every field below has a candidate or is marked unknown.
3. **Draft the brief** at `design/docs-writing/briefs/<page>.md`, with these fields in this order. One message, promise and core idea come from the positioning; a page that needs them to differ raises that as a positioning question.
   - **Purpose.** Why the page exists, from the owner's side: the effect on the reader (what they think, feel or do after it); the place (where readers arrive from, and where each should go next); the sign it worked (what a reader who never saw Sandloom says or does after reading); and not its job (what it leaves to which other pages). The purpose is the owner's; ask it first, since every other field serves it.
   - **Audience and level.** The audiences, primary first, and the level, from [audience.md](../sandloom-docs-writing/audience.md); what they know, and which word stops them.
   - **Job.** The `job:` line of the page.
   - **One message.** The sentence the reader repeats to a colleague.
   - **Promise.** What the reader gets, as outcomes they would pay for.
   - **Core idea.** The phrase the page is built on, from the positioning, and whether the page uses it word for word or in the page form the owner accepted. A raw phrase is meaning, not page text. Say which half explains the reliefs and which the promise.
   - **Argument.** The positioning lists every pain and advantage; a page argues from the strongest few. Choose them in three moves:
     - rank the pains for this page's audience and level by how sharply the reader has felt them and how strong the evidence is (a measured study, then an enforced mechanism, then an inference);
     - group the top ones by a shared cause, so that one advantage answers each group: two or three groups, each a pain sentence and a relief clause;
     - keep the advantages that answer those groups, plus the ones the promise's outcomes, the fit or the cards carry without strain.
     Write the chosen groups, the advantage behind each, and the evidence. List what you left out and the deeper page that carries it. The argument is MECE: groups by one criterion, no pain in two groups. It is the owner's call; propose it as one of the round's questions.
   - **Evidence.** What may be claimed, with the link or captured output, and what may not. A time or a number nobody measured goes under "may not", including the ones already published. An outcome stated without a number may be claimed when the page names the mechanism behind it.
   - **Words.** Terms the level allows, and the plain words that replace the rest. A replacement must not already mean something else on the page.
   - **Actions.** What the reader can do from the page, for an agent and for a person, each with its own result, and the destinations of the next-step links. Destinations are the owner's choice: propose them, even when production has some.
   - **Reuse.** Blocks of the production page or the site that already work.
   - **Out.** What goes to another page, and which page; say whether that page exists. Nothing in Reuse may carry what Out sends away. Positioning the page keeps off, such as named alternatives on an entry page, goes here too.
   - **Tone.** The owner's constraints on voice and form, starting from the owner's preferences.
   - **Open.** Each question with your proposed answer.
4. **Agree it with the owner.** Show the purpose and the open questions, plus the one message and the promise when the positioning does not settle them, each with your proposal as the default, so the owner chooses or corrects instead of inventing. Ask only what changes the page, most important first, at most five per round. What the type's anatomy or the owner's preferences already settle is decided, not asked; settle the rest yourself and list it as decided. Ask with AskUserQuestion when you have it. Write each answer into its field, quoting the owner where the words matter. Repeat until nothing is open.
5. **Mark it approved.** Set the status line to "approved by the owner, <date>". Done when the owner approved the purpose and the one message, and every field is filled or marked "owner: skip". The brief ends here; the page text starts in sandloom-docs-writing.

## What the brief is for

- An agent that saw only rules wrote a compliant Overview the owner rejected three times. The page that worked came from the owner's positioning, the core idea in the owner's words, an outside reader's stumble, and blocks reused from production. Those are the fields above.
- A proposal beats a question. "Is the one message 'a Python backend framework designed for AI-driven development'?" gets an answer in one turn; "what is the one message?" does not.
