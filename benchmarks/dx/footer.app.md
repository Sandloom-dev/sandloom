
---
Working agreement for this session (from the person running it; not part of
the product request):

{start}
- The Sandloom framework is installed as a package (the `sandloom` command is on your
  PATH). `uv` is already configured to find its distributions locally, so
  `uv sync` in the application resolves them; nothing else to set up. Do not
  commit, push or open pull requests.
- The test services (PostgreSQL and Redis) are already running. Your own
  isolated test resources are exported in the environment and override
  `.env.test`: `SANDLOOM_TEST_OWNER`, `SANDLOOM_TEST_DATABASE_URL`,
  `SANDLOOM_TEST_CACHE_URL`, `SANDLOOM_TEST_BROKER_URL`. `sandloom test` is allowed,
  including its parallel steps: it claims its own Redis indexes for its
  workers. Do not run raw `pytest -n` yourself (hand-picked workers would
  collide with other sessions on this machine). Never use `pkill` /
  `killall`, and do not restart or reconfigure docker containers.
- When you are done, end with a short report: (1) what you built; (2) which
  files and docs you read to learn how to do it; (3) the mistakes you made and
  what caught each one; (4) where you got stuck or had to guess.
