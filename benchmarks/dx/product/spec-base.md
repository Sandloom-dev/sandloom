# Academy — product spec

Academy is a multi-tenant SaaS where organizations (the existing `tenancy`
module) publish courses, learners enroll in them by paying with the
organization's credits (the existing `billing` module), learners progress
through lessons, and finished courses earn certificates.

<!-- mode: framework -->
Build it on Sandloom, in this repository, as new business modules under
`src/sandloom/`. The bundled `identity`, `tenancy` and `billing` modules
exist already; do not change them unless the spec says so.
<!-- /mode -->
<!-- mode: app -->
Build it as a Sandloom application: one business module per section below. It
uses Sandloom's bundled `identity`, `tenancy` and `billing` modules, which come
with the framework.
<!-- /mode -->

Common rules for every endpoint below:

- Every request is authenticated with the platform's access token. The
  caller's **current organization** is the token's organization; all data is
  scoped to it, and another organization's data must be invisible (`404` for
  a single item, absent from lists).
- Ids are UUIDs; request and response bodies are JSON; field names are
  exactly as written.
- Creating something answers `201` with the created object; reading answers
  `200`.
- A client may retry any POST with the same `Idempotency-Key` header; the
  retry must not repeat its effect (and must not charge twice).
- The product must keep working when its modules later run as separate
  services.

## Modules
