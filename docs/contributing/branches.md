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
`main`. There is no permanent `develop`, staging or release branch.
Every PR must pass **CI required** before the owner can merge it. The check
validates the suites selected for that PR; see [CI profiles](ci.md).

## Server protection

The [branch-protection configuration](https://github.com/Sandloom-dev/sandloom/blob/main/.github/branch-protection.json)
requires a PR, restricts updates and PR merges to `Maryarty`, applies to admins,
and disallows force pushes and branch deletion. It grants no PR bypass to users,
teams or apps. The required `CI required` status accepts results only from
GitHub Actions and applies to the owner too. `CODEOWNERS` assigns every file to
`@Maryarty` for review routing.

These files define the policy; committing them does not enable protection on
GitHub. Verify the live repository settings using the procedure below. Protection
and code-owner review availability depend on the repository's GitHub plan and
visibility; see GitHub's
[branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches)
and [CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)
documentation. If GitHub refuses the protection API, the policy still applies to
contributors and agents, but server enforcement cannot be assumed.

The approval count is deliberately zero: GitHub does not let an author approve
their own PR, and the owner is currently the only maintainer. When applied on
GitHub, owner control is
enforced by the merge/update restriction, rather than by requiring a second
account's approval. A PR by somebody else still needs the owner's final review
and manual merge. With that restriction enabled, other writers cannot merge it
even if they approve it.

GitHub identifies accounts, not the person or agent using the account. An agent
authenticated as `Maryarty` has the same server permissions as the owner. It
must therefore follow the explicit rule above and wait for the owner's decision.

## Apply and verify protection

First inspect any existing rule and reconcile it with the checked-in
configuration: the PUT replaces classic protection settings. Do not
overwrite stronger protections added by another maintainer.

```bash
gh api repos/Sandloom-dev/sandloom/branches/main/protection
gh api --method PUT repos/Sandloom-dev/sandloom/branches/main/protection \
  --input .github/branch-protection.json
gh api repos/Sandloom-dev/sandloom/branches/main/protection
```

Check that PR requirements are present, `restrictions.users` contains only
`Maryarty`, `enforce_admins.enabled` is true, PR bypass lists are empty, and
force pushes/deletions are disabled. Verify `required_status_checks.checks`
contains `CI required` bound to GitHub Actions (app ID `15368`), with
`required_status_checks.strict` false. Check inherited rulesets too:

```bash
gh api repos/Sandloom-dev/sandloom/rulesets
```

Confirm the live settings after applying any change. A committed configuration
file alone is not evidence that GitHub enforces the rule.

## Publication

Merging a PR does not publish packages or deploy the website. PyPI publication
requires a release tag or an explicit manual run and owner approval in the
`pypi` environment. Documentation builds produce CI artifacts. See
[Publishing](../guides/publishing.md) for the release configuration and upload controls.
