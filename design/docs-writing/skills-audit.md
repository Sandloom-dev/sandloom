# Audit of the docs skill set against the market

Status: **audit, 2026-11-02; nothing changed yet.** An independent agent read the five `sandloom-docs-*` skills, their references, tools, scenario and results log, and compared them with [skills-research.md](skills-research.md). It spot-checked four of that survey's claims at their sources (all held) and added one skill the survey missed, [blader/humanizer](https://github.com/blader/humanizer/blob/0ef9fe5bc3572734fd3ae6c448fe00a30cd5dcf0/SKILL.md) (63.9k stars), whose §6 "Forced triads" we verified.

## Verdict

The most complete docs-skill system published that the auditor knows of: ahead on strategy (positioning), the per-page brief, the reader model with levels, MECE structure, preference capture and outcome-based evaluation with a real blind-test record. Behind on mechanical enforcement, machine-prose detection, the statistics of its own eval, and context cost (about 10k words loaded per entry-page run). It overfits one approved page through copyable examples.

Highest-value change: replace the example-laden entry anatomy and the "Threes" rule with a step that writes two or three variants per block, picked by three blind judges. Runs 11–12 are the only clear wins in the log, and they came from merging blocks.

## By capability

| Capability | Best market example | Mark |
| --- | --- | --- |
| Positioning | Mintlify product brief | ahead |
| Per-page brief | Vercel's plan as test spec | ahead |
| Reader model and level | React docs-voice | ahead |
| Information architecture, MECE | Gemini docs-writer | ahead |
| Drafting workflow | doc-coauthoring: 5–20 options per section, user curates | behind: no variants |
| Style rules | Google style as base (Flutter, Prefect) | on par |
| Machine-prose detection | humanizer: 26 ranked tells | behind |
| Code-sample testing | Prefect: every block runs in CI | behind |
| Automated linting | Vale agent-tools: in-turn hook | on par, no hook |
| Independent review | React review-docs | on par |
| Reader testing | doc-coauthoring stage 3 | on par |
| Owner in the loop, preferences | doc-coauthoring (not persisted) | ahead |
| Evaluation of the skills | skill-creator: with/without, repeated, variance | ahead on realism, behind on statistics |
| Packaging, size, triggering | Mintlify reference index; SkillsBench "≤3 modules" | behind |
| Maintenance cost | Vercel remote rules; Vale config | behind |

## Gaps, with fixes

1. **Machine-prose detection.** The set requires a tell humanizer flags: "Threes" (`page-types.md`) and the S4 exemption for series of three. Delete both; rank the tells by strength; lint the mechanical ones (repeated openers, ", so" chains, triads per paragraph).
2. **No lint in the agent's turn.** Add a PostToolUse hook on Edit|Write for `docs/**/*.md` running `docs_prose_lint.py <file>`, as Vale agent-tools does.
3. **Eval statistics.** The eval skill still prescribes one judge and a gate of "no worse on any check, better on one", though one judge moves by two points. Three judges by default, a gate on the mean beating noise, and scenarios for a concept page and a how-to.
4. **No variants.** Writing step 5 should produce two or three variants of each argument block on entry and brief-backed pages, picked per block by blind judges or the owner.
5. **Code samples.** A check that every Python fence under `docs/` is a `--8<--` include or marked `notest` with a reason.

## Over-built

- The entry anatomy: about 25 lines fitted to one page, down to sentence counts; the scenario's judge questions restate it, so the eval measures compliance more than quality.
- The review loop: five reviewers and reader questions until "right twice in a row"; run 6's 41 findings moved the judge inside one judge's noise. Keep two reviewers: anatomy with brief, and tells with owner preferences.
- The full alternatives table loaded on every brief run, though no page may name an alternative.
- A 947-line linter for six rules, and scrub regexes tied to exact skill sentences.

## Internal problems

- Copyable examples remain and pass the leak check, which matches full phrases only: "stays quick to change as it grows, runs reliably…", "a small task list: statistics count each new task", "a backend running on your machine".
- The template "<what happens>, so <what the reader has felt>" produces the ", so" chain the set's own "Reused framing" tell forbids.
- The relief paragraph is "three or four sentences" in one place and "three to five" in another.
- The promise still demands three mechanisms and three outcomes, regardless of the brief's Argument.
- Duplicated rules: owner-preference rows that restate anatomy and style rules; job, purpose and one message defined in both brief and writing; "change words, not add" in writing and review.
- Uncheckable completion criteria: "would say every sentence aloud"; "right twice in a row" without saying who judges right.
- Steps an agent skips: the render check (the eval itself skips it); block-by-block owner acceptance in unattended runs.
- Positioning and brief descriptions both trigger on "README, landing or entry-page text".
