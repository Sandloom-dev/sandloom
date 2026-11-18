# Ledger delta: released marks (contract-release-marks)

A contract or exported event is a draft until `sandloom release` marks it
released; `sandloom generate` refuses an incompatible change (SANDLOOM-T004,
T005) only for a released entry, and `--new-major` copies a released one to the
next major (ADR-0017).

| Id | Disposition |
| --- | --- |
| G-COMPAT-09 | New: drafts change freely, released entries are compared and move a revision, a file with no `released` key counts as released, a malformed mark is refused, `--new-major` copies a released contract or event and refuses a draft, an unknown name and a `--check` run; enforced by `test_release_marks.py` and the release cases of `test_sdk_generators.py`. |
| G-COMPAT-01 | Retain, restated: T004 and T005 apply to released entries only (G-COMPAT-09); the tests are unchanged. |

No guarantee, enforcing assertion, or residual-risk requirement is removed.
