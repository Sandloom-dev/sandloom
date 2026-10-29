---
title: Branches and owner review
---

# Branches and owner review

Every change to `main` goes through a pull request. Vasyl Cherniavskyi
([@Maryarty](https://github.com/Maryarty)) makes the final review and manually
merges it. Contributors and agents prepare a branch and PR, report validation,
and leave it open for the owner. They do not merge or enable auto-merge unless
the owner explicitly instructs them to do so for that PR.

## Branches

`main` is the only permanent development branch. Use a short-lived, descriptive
branch for each change (`codex/<topic>` for agent work). Open the PR against
`main`. No `develop`, permanent staging branch or release branch is needed now.
Existing CI runs on PRs; the owner checks its results before merging. This
initial policy does not introduce new required status checks or change CI jobs.

## Server policy and its current limitation

The repository is private and Sandloom-dev currently uses GitHub Free.
GitHub returned HTTP 403 when reading branch-protection and ruleset endpoints
on 2026-10-29. **Server enforcement is not active.** The checked-in files express
the agreed workflow; they do not themselves enable GitHub protection or automatic
code-owner review requests. Public repositories on Free and private organization
repositories on Team support these features; see GitHub's
[branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches)
and [CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)
documentation. Changing the plan or repository visibility is a separate owner
decision.

The prepared [branch-protection payload](https://github.com/Sandloom-dev/sandloom/blob/main/.github/branch-protection.json)
requires a PR, restricts updates and PR merges to `Maryarty`, applies to admins,
and disallows force pushes and branch deletion. It grants no PR bypass to users,
teams or apps. `CODEOWNERS` assigns every file to `@Maryarty` for review routing
when the plan supports it.

The approval count is deliberately zero: GitHub does not let an author approve
their own PR, and the owner is currently the only maintainer. Owner control is
enforced by the merge/update restriction, rather than by requiring a second
account's approval. A PR by somebody else still needs the owner's final review
and manual merge. Other writers cannot merge it even if they approve it.

GitHub identifies accounts, not the person or agent using the account. An agent
authenticated as `Maryarty` has the same server permissions as the owner. It
must therefore follow the explicit rule above and wait for the owner's decision.

## Enable and verify later

After the plan supports protection, first inspect any existing rule and reconcile
it with this payload: the PUT replaces classic protection settings. Do not
overwrite stronger protections added by another maintainer.

```bash
gh api repos/Sandloom-dev/sandloom/branches/main/protection
gh api --method PUT repos/Sandloom-dev/sandloom/branches/main/protection \
  --input .github/branch-protection.json
gh api repos/Sandloom-dev/sandloom/branches/main/protection
```

Check that PR requirements are present, `restrictions.users` contains only
`Maryarty`, `enforce_admins.enabled` is true, PR bypass lists are empty, and
force pushes/deletions are disabled. Check inherited rulesets too:

```bash
gh api repos/Sandloom-dev/sandloom/rulesets
```

Once enabled and verified, update this page's enforcement status. Do not describe
the rule as active based only on a committed configuration file.

## Publication

Merging a PR does not publish packages or the website. The PyPI publish step
remains disabled, and site builds remain private CI artifacts. The repository
stays private. Release automation and additional required CI gates will be
configured separately.
