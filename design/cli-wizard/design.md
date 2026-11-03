# `sandloom new` wizard and the Sandloom CLI look

Status: design, 2026-11-03 (tm: `wizard-design`, under `new-wizard`). The owner's request: a wizard
for a new app, thought through, and a branded CLI.

## Goal

A person types one command and answers a few questions. A few minutes later they have:

- a running app;
- the admin console, if they chose it, with their own administrator;
- the exact one-line command that would do the same again.

An agent or a script never sees a question: flags and defaults drive everything, and the output
stays plain text.

## When the wizard runs

| Situation | Behaviour |
| --- | --- |
| `sandloom new` at a terminal (stdin and stdout are TTYs, `CI` unset), any question unanswered by a flag | wizard asks only what the flags did not say |
| `sandloom new shop --with admin --agents claude` at a terminal | asks only what is left: whether to run the setup now |
| `--yes` | no questions; defaults for everything not given; setup is **not** run unless `--setup` |
| no TTY, `CI=true`, or `TERM=dumb` | no questions, today's behaviour; a missing app name is an error naming the flag |
| `--no-input` | same as no TTY (for agents that do have a pseudo-terminal) |

`app` becomes optional on the command line only at a terminal; elsewhere it stays required.

## The questions, in order

1. **Name.** The default is the current directory's name, made snake_case, when the directory is
   empty; otherwise `my_app`. It is checked live with the generator's own rule (`_NAME_RE`, not a
   Python keyword, not a stdlib module, the target directory free). A bad answer is asked again
   with the reason.
2. **Standard modules.** A checklist, built on the agent picker's `checkbox.select`, with one plain
   line each:
   - identity: users, sign-in, email verification, 2FA;
   - tenancy: organizations and members;
   - billing: plans, payments, credits;
   - admin: the web console for operators.

   Dependencies are applied and shown: ticking admin ticks identity (as the generator does), and
   so does billing if the generator requires it; check `_normalize_modules`. Nothing is ticked by
   default; the hint line says "space to add, Enter to continue".
3. **Coding agents.** The existing picker, with the installed agents already ticked.
4. **Set it up now?** `[Y/n]`, asked only when `uv` is on PATH. If Docker is unreachable, the
   wizard says so and offers "create the files only". Yes runs the setup (below).
5. **First administrator**, asked only when admin is selected and the setup runs:
   - Email; the default is `git config user.email` when it is set.
   - Password: a hidden prompt, typed twice, checked against identity's policy by
     `admin create` itself.
   - Enter on an empty email skips this step, and the summary prints the `admin create` command
     instead.

   The password goes to `sandloom admin create --password-stdin` through a pipe, never through
   argv, the environment or a file.

## The setup run

Each step is one line that updates in place: a spinner while it runs, then `✓ <step> <time>` or
`✗ <step>`.

1. `uv sync`
2. `uv run sandloom generate`
3. `uv run sandloom dev up`
4. `uv run sandloom migrate upgrade-all --apply`
5. `uv run sandloom admin create --email … --password-stdin` (admin only)

The command output is captured to `.sandloom/run/new-setup.log` (gitignored) and shown only when a
step fails: its last 20 lines, then the one-line reason, then
`Fix it and continue with: cd shop && <the remaining commands>`. The generated files always stay.

Ctrl-C:
- during the questions: nothing has been written; the wizard prints "Cancelled; nothing was
  created".
- during the setup: the current step stops, and the wizard prints the same "continue with" line.

At the end, a summary block:

```text
  ✓ shop is ready

  App       http://127.0.0.1:8000   uv run sandloom dev run
  Console   http://127.0.0.1:8000/admin/   you@example.com
  Docs      https://sandloom.dev/docs/

  Next      cd shop && uv run sandloom dev run

  Same again without questions:
  uv run --with sandloom sandloom new shop --with admin --agents claude,codex --yes --setup
```

The wizard does not start the server, because it would hold the terminal. The summary names the
port `dev run` will use, checked free at that moment. The repeat command never carries the
password or the email of the administrator.

## The Sandloom look

A small shared module, `sandloom.core.cli.ui`, standard library only. `new`, the wizard, `dev`,
`admin create` and the one-line connection errors use it. Other commands keep their output; they
can adopt it later.

**Colour.** The brand palette from `website/assets/brand.css`:

| Role | Dark terminal (default) | Light terminal | 256-colour | 16-colour |
| --- | --- | --- | --- | --- |
| accent (mark, names, prompts) | `#d8b892` | `#8b6841` | 180 / 137 | yellow |
| muted (hints, times) | dim | dim | dim | dim |
| success | green ✓ | green ✓ | green | green |
| error | red ✗ | red ✗ | red | red |

Truecolor when `COLORTERM` is `truecolor` or `24bit`, else 256 colours when `TERM` contains `256`,
else 16. Light or dark comes from `COLORFGBG` when present, else dark. `NO_COLOR` turns colour off,
`FORCE_COLOR` / `CLICOLOR_FORCE` turn it on without a TTY, and a non-TTY stdout gets no colour and
no cursor movement.

**Symbols.** `✓ ✗ › ●` and a braille spinner. ASCII `+ x > *` and `-\|/` when stdout's encoding
cannot encode them (Windows cp1252, `LANG=C`).

**Mark.** The caracal from `docs/assets/sandloom-mark.svg`, drawn once in Unicode half blocks
(`▀▄█`), at most 7 rows by 14 columns, stored as a constant in `ui.py`. It is shown beside the
heading in the wizard only, when the terminal is at least 60 columns wide and UTF-8, and it can
be turned off with `SANDLOOM_NO_BANNER=1`. Elsewhere the heading is one line:
`◆ sandloom 0.7.0a8 · new application`, with the mark's colour on `◆`.

**Lines.** Step lines are indented two spaces. Hints are muted, at most one line per question. An
error is two lines: `✗ what failed` and `→ what to do`. Output stays readable with no colour at
all, for screen readers and logs.

## Edge cases

| Case | Behaviour |
| --- | --- |
| Directory `shop` exists and is not empty | asked again for another name (wizard); error naming `--dir` (flags) |
| `uv` missing | no setup question; summary prints the install link for uv |
| Docker missing or not running | setup offers "files only"; `--setup` fails at `dev up` with its one-line reason |
| Port 8000 taken | summary names the free port `dev run --port auto` would take |
| Narrow terminal (< 60 cols) | no mark, checklist lines are cut with `…` |
| Windows | the checklist uses `msvcrt`, colours need VT mode (enable it; fall back to none), ASCII symbols under cp1252 |
| Piped stdin but TTY stdout | no questions (stdin decides) |
| `--setup` without a TTY | runs the steps with plain lines; admin step needs `--admin-email` and `--admin-invite` (prints the link), never a password flag |
| Ctrl-C in the hidden password prompt | the admin step is skipped; the summary prints the command |
| The user picks no module | the minimal app is fine |
| An agent runs `sandloom new` in a pseudo-terminal | it sees questions; `AGENTS.md` and the skill tell agents to pass `--yes` (or `--no-input`) |

## Tests

- Unit:
  - the question flow, driven by fed keys and answers (the checklist already takes `keys=`);
  - name validation;
  - the repeat command for every combination of flags;
  - symbols and colour selection for every environment variable combination;
  - the mark's width.
- The setup runner with fake commands:
  - a failure in the middle prints the tail and the "continue with" line;
  - Ctrl-C;
  - the password reaches `--password-stdin` and never argv: inspect the spawned argv and env.
- A pseudo-terminal end to end (the checkbox test's `pty` probe): `sandloom new` answered by keys,
  `--setup` against Docker in a scratch directory, then cleaned up.
- No-TTY: the existing `sandloom new` tests and `quickstart_check` stay green unchanged.

## Out of scope

Colouring `--help`, restyling `sandloom test` and the other commands, templates beyond the four
standard modules, telemetry.

## Revisions after the edge-case review (2026-11-03)

An adversarial review found two blockers and fifteen gaps. These rules replace the sections above
where they disagree.

1. **Agents never block.** One `interactive()` in `ui.py`, used by the wizard and by
   `choose_agents`. It is true only when:
   - stdin and stdout are TTYs;
   - neither `--yes` nor `--no-input` is given (`--no-input` is an alias of `--yes`);
   - `CI` is not truthy;
   - no known agent marker is set: `CLAUDECODE`, `CODEX_*`, `GEMINI_CLI`, `CURSOR_AGENT` and the
     like. Each name is verified against the agent's docs or source and listed in one place.

   Above the first question the wizard prints one muted line: "Scripts and agents: add --yes".
   `TERM=dumb` keeps questions as typed prompts (the `ask_agents` fallback), with no cursor moves.
2. **The administrator step belongs to `admin create`.** The wizard collects no password. As the
   last setup step it runs `uv run sandloom admin create --email <e>` attached to the terminal, and
   that command already asks twice, checks the policy and retries. If the account exists but the
   password was not set, the "continue with" line names `sandloom admin invite`.
3. **Repeat command:** `uvx --from "sandloom==<version>" sandloom new <name> …`, pinned like the
   docs. It carries every flag that was set (`--with`, `--agents`, `--module`, `--dir`,
   `--framework-source`/`--framework-path`) plus `--yes`. A test checks the argv round trip.
4. **CI parses "Next steps".** In plain mode nothing new is printed after that block. The summary
   box, the repeat command and the links are wizard-only.
5. **`--setup` under CI** refuses up front with one line: CI provides its own services, and
   `dev up` refuses there anyway. `--with admin --yes --setup` without `--admin-email` skips the
   admin step and prints the command; with it, the step runs `admin create --invite` and prints the
   link (no TTY, no password).
6. **The setup runner:**
   - child processes get an environment without `VIRTUAL_ENV`, `UV_PROJECT_ENVIRONMENT` and
     `UV_PROJECT`; uv is the `UV` environment variable when set, else `which("uv")`;
   - under the spinner: elapsed time and the last stderr line of the step;
   - Ctrl-C stops the current step and waits for it to exit (a second Ctrl-C kills it), then prints
     the "continue with" line, never a traceback. A partial `.venv` is fine: `uv sync` repairs it,
     and `dev up` is idempotent;
   - `docker info` gets a timeout (10 s), with "Docker is starting or not running" as the answer.
7. **`git init`** runs after generation and before setup, unless the target is already inside a git
   work tree or git is missing, so the dev state lands in the app's own repository from the start.
   It makes no commit.
8. **Name:** the default is `my_app` (`generate_project` always creates `<dir>/<app>`). The
   "not a stdlib module" check moves into `generate_project`, so flags and the wizard agree. With
   `--yes` and no name, `sandloom new` errors and names the argument.
9. **Modules:** the generator enforces only admin → identity, and the wizard says exactly that.
   Nothing is ticked by default; the checklist uses `minimum=0`.
10. **Colour:**
    - one mid-tone accent that reads on both light and dark backgrounds (about `#94714b`, checked at
      4.5:1 on white and on `#1e1e1e`; adjust if not), with bold for prompts; there is no
      light/dark switch;
    - `NO_COLOR` (non-empty) wins; `FORCE_COLOR=0` means off, and 1, 2 or 3 set the depth;
    - forced colour never turns on cursor movement or the spinner;
    - colour is decided per stream: errors go to stderr.
11. **Environment reads** (`NO_COLOR`, `FORCE_COLOR`, `TERM`, `COLORTERM`, `CI`, the agent markers)
    go through the existing process-flags seam (`dev/errors.environment()`). They are listed as an
    exception in `src/sandloom/core/AGENTS.md`. `SANDLOOM_NO_BANNER` is dropped.
12. **Windows and encodings:**
    - the checklist enables VT mode, or falls back to typed prompts;
    - its hint line uses the symbol table;
    - the fallback table covers `◆ → ↑ ↓ ·`;
    - the spinner is ASCII on Windows;
    - the ASCII failure mark is `!`, never `x`, which means "selected" in the checklist.
13. **Port:** when 8000 is taken, the "Next" line carries `--port <free port>`.
14. **Docs and agents.** These change with the code:
    - `docs/getting-started/index.md` and `installation.md`;
    - the README;
    - the generated `AGENTS.md` and skills: agents pass `--yes`;
    - `docs/reference/cli.md`;
    - CHANGELOG.
15. **The mark** is rendered in a test fixture and shown to the owner before it ships. If it does
    not read as a caracal, the heading keeps only the coloured `◆`, as rustup, gh and create-vite
    do.
16. `--plain` forces plain output at a terminal (screen readers, logs); the log file never carries
    ANSI.
