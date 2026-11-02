---
name: sandloom-docs-eval
description: Test a change to the Sandloom docs skills by re-running blind scenarios — prepare an isolated copy, run the skills on agents with the owner's recorded answers, grade the page mechanically and by three judges, compare with a baseline, log the result. Use after editing any sandloom-docs-* skill or its references, and when asked whether the docs skills work.
---

# Evaluate the Sandloom docs skills

A rule that reads well can still make pages worse. So each change to a docs skill runs against scenarios with a known good answer: a page the owner approved, written again by agents who cannot see it. Scenarios live in [design/docs-writing/evals](../../../design/docs-writing/evals), the results log in [blind-tests/README.md](../../../design/docs-writing/blind-tests/README.md). One judge's score moves by up to two points between runs of the same page, so every comparison uses three.

## Steps

1. **Pick the scenarios** whose stages use the skill you changed; when in doubt, run them all. A change no scenario exercises needs a new scenario first: copy one, point it at another approved page, list the phrases of that page under `leaks`, and write its `answers` from the owner's recorded decisions. A page type with no scenario gets one with its first approved page. Judge questions ask what a reader gets from the page, never whether it follows the anatomy. Done when each changed rule is exercised by at least one scenario.
2. **Prepare a blind copy** for each scenario, and a baseline copy with the skills before your change:
   ```bash
   python3 tools/docs/docs_skill_eval.py prepare <scenario> <scratchpad>/eval-<scenario>
   ```
   ```bash
   python3 tools/docs/docs_skill_eval.py prepare <scenario> <scratchpad>/base-<scenario> --skills-ref <before>
   ```
   `<before>` is the commit the skills had before your change: `HEAD` while the change is uncommitted, otherwise the commit you started from (`git merge-base HEAD origin/main` on a branch). With `HEAD` after committing, both copies hold the changed skill and the comparison measures nothing.
   It refuses a copy where a phrase of the approved page survives; remove the leak from the skill or scrub it in the scenario, and prepare again.
3. **Run the stages.** For each stage, start a fresh subagent in the copy with: the isolation rules (work only in the copy; never read the repository, `~/.claude`, the site or GitHub), the stage's skill, its `request` and its `stop`. When it stops with questions, answer each from the scenario's `answers` by topic and say who answers: "the operator, from the owner's recorded decisions". Never present an answer as the owner's live approval. A question no answer covers goes to the owner, and the answer joins the scenario. Run the baseline copy the same way. Done when each copy holds its page.
4. **Grade.** Run the mechanical checks on each page:
   ```bash
   python3 tools/docs/docs_skill_eval.py grade <scenario> <copy>/<page>
   ```
   Then write the reference page to a file and render it beside the change's page and the baseline's, blind:
   ```bash
   git show <reference>:<page> > <scratchpad>/reference.md
   ```
   ```bash
   uv run python tools/docs/docs_compare.py <scratchpad>/judge.html --blind change=<copy>/<page> base=<base>/<page> reference=<scratchpad>/reference.md
   ```
   Give three judge subagents, in parallel, the three pages as A, B and C in the order of the key, and the printed judge questions: a score from 1 to 10 per question with one line of evidence, and a ranking with two sentences on why. No judge sees the skill or the key. Done when every page has mechanical results and three judges' scores.
5. **Log and decide.** Add a row to the results log: the date, the scenario, what changed, the mechanical results, each judge's mean per page and the spread between judges, and what the agents said got in their way. Keep the change when it has no new mechanical failure and its mean beats the baseline's by more than the judges' spread, or at least two of three judges rank it above the baseline; otherwise revert it, or fix it and run again. What the agents report as unclear is a finding for the skill, whatever the score.
