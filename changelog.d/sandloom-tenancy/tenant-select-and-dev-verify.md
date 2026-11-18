### Changed

- **Joining an organization makes the joiner's access token stale** (tenant-select-and-dev-verify). Creating an organization, accepting an invitation and `MembershipService.add_member` revoke the added user's tokens on commit, not immediately (`require_immediate=False`), so the client refreshes on `401` into a token whose `tenant_ids` lists the organization. An unreachable revocation store never refuses the creation or the join.
- **`POST /api/v1/orgs` derives `slug` from `name`** (tenant-select-and-dev-verify). `slug` is optional; a derived slug that is taken answers `409` asking for `slug`.
