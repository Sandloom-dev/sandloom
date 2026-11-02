# Audience and level

Every page names who reads it and how far into Sandloom they are, in front matter:

```yaml
audience: [developer, agent]   # primary first
level: building
```

The audience decides the voice and what the reader needs shown. The level decides which words the page may use without explaining them, and how deep its claims go. A page has one level; a reader below it gets a link down, a reader above it gets a link deeper.

## Audiences

| Audience | Knows | Wants | Reads by | Fails when |
| --- | --- | --- | --- | --- |
| `developer` | Python, FastAPI, SQLAlchemy, Postgres; has never met an outbox | Code that works and the reason it is shaped this way | Skimming to the code, then reading around it | The page hides the code behind prose, or the "why" behind a rule |
| `vibe-coder` | What their product should do; little backend code | A working result their agent builds and checks | The first screen and the actions | A term stops them, or the page asks them to read code to decide |
| `agent` | Nothing about Sandloom; acts on literal text | Exact commands, names, file paths and the check that says done | Every word, literally, often one page at a time | A claim is true only with context from another page, or a sample is a fragment |
| `operator` | Docker, Postgres, Redis, secrets, CI | Steps that change a running system safely, and how to undo them | The procedure, then its warnings | A step is irreversible and says so too late |
| `contributor` | The framework's own code | The kernel contract and what not to break | The rules, then the tests that hold them | The page describes intent, not the check that enforces it |

An entry page serves `developer`, `vibe-coder` and `agent` at once; any other page names its primary audience first and leaves the others a way in.

## Levels

| Level | The reader has | Words used without explanation | Typical pages |
| --- | --- | --- | --- |
| `new` | Never run Sandloom | None of Sandloom's own; plain words from the positioning's Naming | Overview, Who it fits, README |
| `first-app` | Run the quickstart | Module, check, `sandloom test`, generate, migration; each linked to the glossary on first use | Quickstart, tutorial, project structure |
| `building` | Built a feature across modules | The glossary: unit, event, contract, consumer, tenant, route policy, outbox | Concepts, how-tos, recipes |
| `operating` | Run it in production or extended it | Deploy profiles, services, keys, extraction, guarantee ids, kernel internals | Operate, security, kernel, ADRs |

A term above the page's level is explained in place or replaced with plain words; "the parts, called modules" on a `new` page brings the term back and counts as using it. Reference pages take the level of what they list and may use its terms freely.

## Choosing them

Take the audience from the job's situation (who is in that moment) and the level from what the reader must already have done to be there. Where the evidence for either is thin, it is a hypothesis, and the handover says so. A page that needs two levels is two pages, or one page with its deeper part moved one link down.
