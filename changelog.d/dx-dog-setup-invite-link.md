### Fixed

- **`sandloom new --yes --setup --admin-email` prints the administrator's set-password link** (dx-dog-setup-invite-link). Without a terminal the setup runs `admin create --invite` as a captured step, and its output went only to `.sandloom/run/new-setup.log`: the terminal showed a tick and "Next steps" and nothing said how to sign in. The step's output (the single-use link, valid 15 minutes, and the `uv run sandloom admin invite --email ...` command for a new one) is now written before "Next steps". The "Next steps" block itself is unchanged.
