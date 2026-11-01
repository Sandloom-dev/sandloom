# Admin Console delivery — research

Status: **design research; no implementation**. Sources retrieved 2026-11-01.
Sandloom baseline: `5f2796aa433d8ec4623f73becd3bf7c09be09c4c` (`origin/main`). The admin guides were read with `git show origin/main:…`. Other files were read from the checkout, where another session has uncommitted edits, including `admin/README.md` and `admin/AGENTS.md`.
Source links point at each project's default branch on the retrieval date, so they can be ahead of the released versions measured below.

Text marked **Inference** or **Judgement** is mine. Everything else reports what the linked source says or what I measured.
Evidence tags: **[S]** read in source or build config, **[P]** packaging metadata or a registry manifest, **[D]** documentation only, **[M]** measured locally.
Long pages were read by targeted search, not end to end; see [What I could not verify](#what-i-could-not-verify).

## Summary

- **Recommendation: ship the console as an optional Sandloom module package.** `sandloom-admin` carries prebuilt assets in its wheel and serves them same-origin at `/admin/`. It needs neither Node nor Docker.
- **The tentative idea (image plus a new flag) fails on four counts today.** It does not fit the host-run quickstart, it still needs a per-app manifest and config mount, its default upstream port does not match a generated app, and it pairs versions across two registries.
- **The manifest is the real delivery problem.** A prebuilt UI is generic, but `admin-manifest.json` is per-app and needs the full composition. It should become a generated file, like the other outputs of `sandloom generate`.
- **The existing module machinery already fits.** A throwaway module booted, served a deep link with its own CSP, and `sandloom add deploy` generated its gateway routes. No browser login was tested.
- **Declarative extension is the right default.** The products that run plugin JavaScript pay for it with signing (Grafana), experimental status (Backstage) or shared runtime contracts (JupyterLab, Argo CD).
- **There is a blocker unrelated to packaging.** I found no supported way to create the first platform admin in a new app.

## 1. Where Sandloom stands

Every row is [S] or [M] at the baseline, unless marked.

| Finding | Evidence |
| --- | --- |
| The console is a private npm project, with a Node >=22.12 build | `admin/package.json` |
| The only image route builds from source | `deploy/docker-compose.admin.yml` has `build: context: ../admin`. No workflow pushes an image: `docker/build-push-action` in `ci.yml` has `push: false`, and `admin.yml` only runs `docker build` |
| The published wheels carry no console | [M] Installed `sandloom[standard]==0.7.0a5` from PyPI. The only web assets are the `sandloom explore` viewer under `sandloom/core/cli/inspection/` |
| `sandloom new` adds no console | [M] Generated `--with identity`: no admin overlay, and `src/sandloom/core/cli/templates/deploy/` has no admin file |
| A static-UI-in-wheel precedent already exists | That explorer viewer, `viewer.html`, `.js` and `.css` in the `sandloom` wheel |
| The manifest and the UI are separate artifacts | `sandloom admin export` compiles `admin.RESOURCES` plus OpenAPI to JSON. The browser checks `version === 1` (`admin/src/schema/manifest.ts`) and rejects unknown widgets (`admin/src/app/config.ts`) |
| The compiler needs one composition holding all shown modules | `compile_app` reads `app.state.module_summary.loaded`, and its docstring says to export the full set at build time |
| The bundled UI is coupled to three independently versioned modules | `admin/src/features/{identity,tenancy,billing}` with widgets, and `generated.ts` from their OpenAPI. README: "a backend built from the matching source revision". `packages/README.md`: "Versions are independent" |
| Session model | Bearer tokens in memory only, no auth cookies (`admin/README.md`). Same-origin is assumed: `connect-src 'self'` in `admin/nginx.conf` |
| Absolute paths are baked in | `fetch('/config.json')`, `BrowserRouter` without `basename`, `url('/fonts/…')`, no `base` in `vite.config.ts` |
| First platform admin | Guide: the console "does not create a default administrator". I found no CLI command for it in `src/sandloom/__init__.py`. `tools/smoke/admin_live_acceptance.py` inserts the rows with SQL. **Could not verify absence beyond these searches** |

### Measurements [M]

| Item | Result |
| --- | --- |
| `vite build` output (copy outside the checkout, existing `node_modules`) | 1.2 MB unpacked, 24 files before the licence step; 2.3 s wall clock without `tsc` |
| Zip of that output, including `third-party-licenses.txt` (126 KB) | **418 KB** at `zip -9`. For scale, the `sandloom` wheel is 1,631,520 bytes [P] |
| Gzip budget check (`scripts/check-bundle.mjs`) | JS 199.8 KiB (budget 230), CSS 10.3 KiB (budget 20) |
| npm lockfile | 670 packages: **95 non-dev, 575 dev**. `node_modules` is 362 MB, which is what a Python developer would otherwise install |
| Console source | 16,559 lines of non-test TS/TSX, of which `generated.ts` is 7,362. Tarball without tests or `node_modules`: 366 KB |
| Manifest export on PyPI 0.7.0a5, three standard modules | App build 2.26 s, then `compile_app` 0.10 s; 81 KB, 21 resources, 143 routes |
| Console-only module process (macOS, spike below) | 108 MB peak RSS, 1.42 s import. Not compared with nginx: I did not build the image |
| Base image `nginxinc/nginx-unprivileged:stable-alpine` | 25.6 MB compressed for amd64 [P]. That is about 60 times the console's own zip |

### Spike [M]

Throwaway code in the scratchpad, against PyPI `sandloom==0.7.0a5` rather than the checkout's HEAD, using `httpx.ASGITransport` with no lifespan, database or browser.

| Test | Result |
| --- | --- |
| `app.mount("/admin", StaticFiles(html=True))` | Works. The kernel pipeline adds `nosniff`, `X-Frame-Options: DENY`, `no-referrer` and `Cache-Control: no-store`. HTML gets no CSP. A deep link (`/admin/users/123`) returns 404, because `html=True` falls back only to `404.html` ([staticfiles.py](https://github.com/Kludex/starlette/blob/main/starlette/staticfiles.py)) [S] |
| Mount cost | `route_index.py` treats a `Mount` as a candidate for every path [S]. I did not measure the effect |
| Ordinary module: `module_router(prefix="/admin", dependencies=[public(reason=…)])`, one `/{path:path}` route returning `FileResponse` | Boots beside identity. Deep link returns `index.html` with its CSP. Hashed assets keep `immutable`, so the pipeline preserves headers a route sets. JSON gets the kernel's `default-src 'none'`. An encoded traversal (`%2e%2e`) fell back to `index.html` |
| `sandloom add deploy console --service console`, then `identity` | Wrote `location = /admin` and `location /admin/` to the new upstream, with identity's routes beside it. Compose services were added |
| `sandloom admin export` in the generated app | Worked: identity's resources plus per-module `audit_*` and `services_*` |
| Side effect | The kernel also installs platform views for the console module (`audit_console`, `/api/v1/platform/console`). This is noise to suppress |
| `vite build --base=/admin/` | Rewrites `index.html` and hashed asset URLs, but not the code literals. `fetch('/config.json')` and the router still need changing |

I did not run `sandloom add deploy --check`, `sandloom test`, compose or a login.

## 2. Delivery models in real products

**Short answer.** Static assets in a Python wheel are the norm for Python products. Separate images are common where the product is not a Python package. Several products on the "separate image" list actually embed the console in the server.

### Assets inside a Python distribution

Wheel figures are [P]: PyPI JSON plus a remote listing of the wheel's zip directory. Static means deflated bytes of web files in the wheel.

| Product (version) | Wheel | Static | Where and how it is served |
| --- | --- | --- | --- |
| Django 6.1.2 | 8.42 MB | 0.39 MB admin static | `django/contrib/admin/static`. Production needs `collectstatic` or equivalent ([deployment](https://github.com/django/django/blob/main/docs/howto/static-files/deployment.txt)) [D] |
| Flask-Admin 2.2.1 | 6.07 MB | 4.6 MB | Server-rendered, vendored Bootstrap and FontAwesome |
| SQLAdmin 0.32.0 | 1.07 MB | 0.94 MB | `self.app.mount(base_url, app=self.admin)`, default `/admin` ([application.py](https://github.com/aminalaee/sqladmin/blob/main/sqladmin/application.py)) [S] |
| Starlette-Admin 1.0.3 | 1.39 MB | 0.97 MB | `Mount("/static", StaticFiles(…))` ([base.py](https://github.com/jowilf/starlette-admin/blob/main/starlette_admin/base.py)) [S] |
| FastAPI 0.162.0 | 0.15 MB | **none** | Swagger UI defaults to `cdn.jsdelivr.net/npm/swagger-ui-dist@5`, a floating major ([docs.py](https://github.com/fastapi/fastapi/blob/master/fastapi/openapi/docs.py)) [S]. Self-hosting is a [how-to](https://github.com/fastapi/fastapi/blob/master/docs/en/docs/how-to/custom-docs-ui-assets.md) [D] |
| Flower 2.2.0 | 0.41 MB | 0.35 MB | Own distribution and process, default port 5555, `url_prefix` option. Auth is off by default ([config](https://github.com/mher/flower/blob/master/docs/config.rst)) [D] |
| apache-airflow-core 3.3.2 | 6.92 MB | 3.7 MB | Hatch `artifacts` includes `src/airflow/ui/dist/` ([pyproject](https://github.com/apache/airflow/blob/main/airflow-core/pyproject.toml)). Mount plus `/{rest_of_path:path}` SPA fallback ([app.py](https://github.com/apache/airflow/blob/main/airflow-core/src/airflow/api_fastapi/core_api/app.py)) [S] |
| dagster-webserver 1.13.28 | 12.44 MB | 8.8 MB build, including about 5.1 MB of source maps | Separate distribution, `artifacts = ["dagster_webserver/webapp/build/"]`. Rewrites `__PATH_PREFIX__` in `index.html` and sets a nonce CSP per response ([webserver.py](https://github.com/dagster-io/dagster/blob/master/python_modules/dagster-webserver/dagster_webserver/webserver.py)) [S] |
| prefect 3.9.1 | 6.99 MB | 5.0 MB | `src/prefect/server/ui` and `ui-v2` as artifacts. **Copies the UI to a static directory and rewrites a placeholder on disk** ([server.py](https://github.com/PrefectHQ/prefect/blob/main/src/prefect/server/api/server.py)) [S] |
| mlflow 3.17.2 / mlflow-skinny | 12.01 / 4.11 MB | 7.5 MB / none | `mlflow/server/js/build` only in the full distribution. Skinny is described as having no server or UI ([README](https://github.com/mlflow/mlflow/blob/master/libs/skinny/README_SKINNY.md)) [D] |
| jupyterlab 4.6.5 | 17.33 MB | About 16 MB | `jupyterlab/static` and `.data/data/share/jupyter` |
| jupyterlab-widgets 3.0.17 | 0.22 MB | 0.20 MB | A prebuilt extension as an ordinary wheel, installing under `share/jupyter` |

**Separate optional distribution.** Dagster, MLflow and Airflow all keep UI bytes out of some installable part. This is the direct precedent for `sandloom-admin`.

### Container image

Compressed amd64 size [P]: Docker Hub API, or registry manifest layer sums for Quay and GHCR.

| Product | Size | Actual relationship to the server |
| --- | --- | --- |
| [pgAdmin](https://github.com/pgadmin-org/pgadmin4/blob/master/docs/en_US/container_deployment.rst) | 160 MB | A separate client of Postgres. Also a 120.7 MB wheel [P], of which about 109 MB is documentation and images, so it ships both |
| Grafana | 503 MB | **Not separate.** The [Dockerfile](https://github.com/grafana/grafana/blob/main/Dockerfile) builds frontend and server into one product [S] |
| Hasura `graphql-engine` | 178 MB | **Not separate.** The console is served by the engine, is off by default in CE and EE, and loads assets from a CDN unless `--console-assets-dir` is set ([flags](https://github.com/hasura/graphql-engine/blob/master/docs/docs/deployment/graphql-engine-flags/reference.mdx)) [D], with the handler in [App.hs](https://github.com/hasura/graphql-engine/blob/master/server/src-lib/Hasura/Server/App.hs) [S] |
| Keycloak | 265 MB | **Not separate.** Same server, under `/admin/`. The docs recommend exposing `/admin/` only internally ([reverse proxy](https://github.com/keycloak/keycloak/blob/main/docs/guides/server/reverseproxy.adoc)), and `hostname-admin` does not restrict the admin REST API ([hostname](https://github.com/keycloak/keycloak/blob/main/docs/guides/server/hostname.adoc)) [D] |
| Temporal UI (`temporalio/ui`) | 32.5 MB | **Truly separate**, configured by `TEMPORAL_ADDRESS` and `TEMPORAL_UI_PUBLIC_PATH` ([env vars](https://github.com/temporalio/documentation/blob/main/docs/references/web-ui-environment-variables.mdx)). It also "ships with every Temporal CLI release" ([web UI](https://github.com/temporalio/documentation/blob/main/docs/web-ui.mdx)) [D] |
| Kafbat Kafka UI | 223 MB | Separate, a client of Kafka; [`docker run`](https://github.com/kafbat/kafka-ui/blob/main/README.md) [D] |
| Argo CD | 222 MB (v3.5.6) | **Not separate.** `ui.Embedded` in the API server binary, with configurable CSP and `X-Frame-Options` ([server.go](https://github.com/argoproj/argo-cd/blob/master/server/server.go)) [S] |
| Supabase Studio | 355 MB | Separate, behind the same gateway on port 8000 with HTTP basic auth ([docker guide](https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/self-hosting/docker.mdx)) [D]. Pinned by a date-and-SHA tag in [compose](https://github.com/supabase/supabase/blob/master/docker/docker-compose.yml) [S] |

**Inference.** Truly separate images belong to tools whose server is a different product or language stack. The Temporal case is instructive: it offers the image and also an embedded local experience, so a developer does not start with a container.

### npm package, or copied source

| Product | Model | Node needed by the app developer |
| --- | --- | --- |
| [Medusa](https://github.com/medusajs/medusa/blob/develop/www/apps/book/app/learn/fundamentals/admin/page.mdx) | Installed with the app, served at `/app`, built by `medusa build`. `--admin-only` plus `backendUrl` deploys it [separately](https://github.com/medusajs/medusa/blob/develop/www/apps/book/app/learn/build/page.mdx) [D] | Yes, but the whole framework is Node |
| [Strapi](https://github.com/strapi/documentation/blob/main/docusaurus/docs/cms/admin-panel-customization.md) | React SPA customized in `src/admin`, rebuilt with `npm run build` [D] | Yes, same |
| [Payload](https://github.com/payloadcms/payload/blob/main/docs/getting-started/installation.mdx) | Files copied into the app's `/app` directory, [import map](https://github.com/payloadcms/payload/blob/main/docs/custom-components/overview.mdx) generated [D] | Yes, same |
| [Refine CLI](https://github.com/refinedev/refine/blob/main/documentation/docs/packages/cli/index.md) | A library. `refine swizzle` exports a component into the project to customize [D] | Yes |
| [react-admin](https://github.com/marmelab/react-admin/blob/master/docs/ListGuesser.md) | A library. `<ListGuesser>` infers fields from a response and prints code to copy [D] | Yes |
| [shadcn/ui](https://github.com/shadcn-ui/ui/blob/main/apps/v4/content/docs/%28root%29/index.mdx) | "This is not a component library": source copied in by [CLI](https://github.com/shadcn-ui/ui/blob/main/apps/v4/content/docs/%28root%29/cli.mdx) [D] | Yes |
| [Keycloak](https://github.com/keycloak/keycloak/blob/main/js/apps/admin-ui/README.md) | Secondary: `@keycloak/keycloak-admin-ui` offers pages as reusable components for your own build [D] | Only for that route |

These fit products whose users are already Node developers, which is the opposite of Sandloom's constraint.

### Hosted

- **Supabase.** Self-hosted Studio runs as a single project and lacks branching, managed backups and the Management API ([self-hosting](https://github.com/supabase/supabase/blob/master/apps/docs/content/guides/self-hosting.mdx)) [D].
- **Temporal Cloud.** The UI is "available with" Cloud ([web UI](https://github.com/temporalio/documentation/blob/main/docs/web-ui.mdx)), and an env var switches to the Cloud variant [D].
- **Hasura.** The console is always enabled on Cloud ([flags](https://github.com/hasura/graphql-engine/blob/master/docs/docs/deployment/graphql-engine-flags/reference.mdx)) [D].

**Inference.** All three sell a hosted service. Sandloom has none, and a hosted console would have to reach operator APIs on private deployments.

## 3. How the models compare

| Concern | Wheel asset | Separate image | npm or copied source | Hosted |
| --- | --- | --- | --- | --- |
| **API compatibility** | Lockstep through the resolver: `dagster==`, `mlflow-skinny==`, `apache-airflow-core==` [P] | A floor at most: Temporal UI "requires Temporal v1.16.0 or later" ([README](https://github.com/temporalio/ui/blob/main/README.md)) [D]; or compose pinning (Supabase) | Rebuilt against the installed packages; ejected code drifts | Vendor's problem |
| **Upgrade** | `uv lock --upgrade`, with the app | A second tag to bump | Merge by hand. shadcn's FAQ limits upstream fixes to dependencies [D] | None for the user |
| **Size added** | 0.42 MB for Sandloom [M] | About 26 MB or more [P] | 362 MB `node_modules` for Sandloom [M] | None |
| **Node for the user** | No | No | Yes | No |
| **Docker for the user** | No | Yes | No | No |
| **Origin** | Same origin as the API by default | Own origin, proxying the API | Either | Cross-origin, needs CORS |
| **Network isolation** | Path rules at the proxy | Own port or network | Either | None |
| **Supply chain** | PyPI [attestations](https://github.com/pypi/warehouse/blob/main/docs/user/attestations/index.md) [D]; Sandloom's release already sets `attestations: true` [S] | [Docker attestations](https://github.com/docker/docs/blob/main/content/manuals/build/metadata/attestations/_index.md) [D]; Argo CD publishes [cosign, SLSA and SBOM](https://github.com/argoproj/argo-cd/blob/master/docs/operator-manual/signed-release-assets.md) [D] | Every user runs the lockfile's install scripts; Sandloom's `postinstall` patches build tools [S] | Trust the vendor |
| **Monolith** | Natural | Sidecar container | Natural | Remote |
| **Microservices** | One service hosts it, the gateway routes it | Natural, beside the gateway | Static host | Remote |

Version negotiation at runtime is the exception among these:

- **Prefect.** The client sends `X-PREFECT-API-VERSION` and raises on a major mismatch ([client](https://github.com/PrefectHQ/prefect/blob/main/src/prefect/client/orchestration/__init__.py)) [S]. This is client to server, not the bundled UI.
- **Grafana.** A plugin declares `grafanaDependency` as a semver range ([metadata](https://github.com/grafana/plugin-tools/blob/main/docusaurus/docs/reference/metadata.md)) [D].
- **Notebook.** Plain resolver range: `jupyterlab<4.7,>=4.6.5` [P].

**Security notes.**

- **A separate port protects less than it looks.** Keycloak warns that `hostname-admin` leaves the admin API reachable through the front-end URL. In Sandloom's generated gateway, `/api/v1/platform/*` and the module APIs are routed publicly whichever way the UI is served [S].
- **CDNs conflict with `script-src 'self'`.** FastAPI and Hasura default to one. Sandloom's nginx CSP forbids it.
- **Production defaults.** Hasura recommends disabling the console in [production](https://github.com/hasura/graphql-engine/blob/master/docs/docs/deployment/production-checklist.mdx), and Keycloak restricts its path. Flower defaults to no auth, while Django [limits](https://github.com/django/django/blob/main/docs/ref/contrib/admin/index.txt) its admin to "trusted users" in internal use [D].
- **Network middleware exists for a reason.** MLflow added [DNS rebinding and clickjacking protection](https://github.com/mlflow/mlflow/blob/master/docs/docs/self-hosting/security/network.mdx) to its UI server [D]. Sandloom's pipeline already sets the frame and sniff headers (spike).

**SBOM.** Sandloom's CI generates a backend CycloneDX SBOM from `uv.lock` and reviews the npm lockfile, but I found no console SBOM in the workflows [S]. [`npm sbom`](https://github.com/npm/cli/blob/latest/docs/lib/content/commands/npm-sbom.md) can produce one [D]; I did not run it.

## 4. Extending without a Node build

**Short answer.** Declarative is the only route with no Node for anyone. Prebuilt JS spares the installer from Node but not the author, and it needs a runtime contract.

| Mechanism | Example | Node for author / installer | Runtime contract and cost |
| --- | --- | --- | --- |
| Python class, server-rendered | Django `ModelAdmin`, [template overrides](https://github.com/django/django/blob/main/docs/ref/contrib/admin/index.txt); Flask-Admin [`ModelView`](https://github.com/pallets-eco/flask-admin/blob/master/doc/introduction.rst); [SQLAdmin templates](https://github.com/aminalaee/sqladmin/blob/main/docs/working_with_templates.md) [D] | No / no | Works because the UI is rendered on the server, which Sandloom's SPA is not |
| Declarative data, rendered by a fixed UI | **Sandloom `admin.py`** [S]; Keycloak [`UiPageProvider`](https://github.com/keycloak/keycloak/blob/main/server-spi-private/src/main/java/org/keycloak/services/ui/extend/UiPageProvider.java) with `DECLARATIVE_UI` still `EXPERIMENTAL` in [Profile.java](https://github.com/keycloak/keycloak/blob/main/common/src/main/java/org/keycloak/common/Profile.java) [S] | No / no | Limited to the language, which must grow deliberately |
| Prebuilt JS with module federation | [JupyterLab](https://github.com/jupyterlab/jupyterlab/blob/main/docs/source/extension/extension_dev.md): installing "does not require Node.js", source extensions are deprecated [D] | Yes / no | `sharedPackages`, singleton tokens, deduplication rules |
| Prebuilt JS via a global | [Argo CD](https://github.com/argoproj/argo-cd/blob/master/docs/developer-guide/extensions/ui-extensions.md): files in `/tmp/extensions`, must use the global `React` [D] | Yes / no | Runs in the UI's origin |
| Prebuilt JS by URL or iframe | [Airflow](https://github.com/apache/airflow/blob/main/airflow-core/docs/howto/custom-view-plugin.rst): `react_apps` with `bundle_url`, a bootstrap tool, `external_views` in iframes [D] | Yes / no | The iframe route isolates; the React route shares |
| Signed JS plugins | [Grafana](https://github.com/grafana/grafana/blob/main/docs/sources/administration/plugin-management/plugin-sign.md) verifies at startup. Its [frontend sandbox](https://github.com/grafana/grafana/blob/main/docs/sources/administration/plugin-management/plugin-frontend-sandbox.md) preview is deprecated [D] | Yes ([signing tool](https://github.com/grafana/plugin-tools/blob/main/docusaurus/docs/publish-a-plugin/sign-a-plugin.md) needs Node 24) / no | Signing service, marketplace, and isolation that did not last |
| Dynamic federation | [Backstage](https://github.com/backstage/backstage/blob/master/packages/frontend-dynamic-feature-loader/README.md): built on a frontend system in alpha; the [backend service](https://github.com/backstage/backstage/blob/master/packages/backend-dynamic-feature-service/README.md) is "experimental" [D] | Yes / no | Still not recommended for production by its own README |
| Rebuild with the host | [Medusa widgets](https://github.com/medusajs/medusa/blob/develop/www/apps/book/app/learn/fundamentals/admin/widgets/page.mdx) in injection zones; [Strapi](https://github.com/strapi/documentation/blob/main/docusaurus/docs/cms/plugins-development/admin-panel-api.md) `register()` and `bootstrap()` [D] | Yes / yes | Simple, but needs Node on every build |

**Inference.** Sandloom's guide states a useful property: the manifest "cannot download executable components". That makes the manifest safe to generate and mount. Any JS plugin route gives this up, and it adds a compatibility surface to the public API work.

## 5. Customization and eject

The products form a ladder, and Sandloom has the first rung.

1. **Declarations.** Exists: `admin.py`.
2. **Runtime config.** Exists in part: `config.json` toggles sections. Temporal's banner and public-path variables show the same runtime style [D].
3. **Theme or branding.** Strapi swaps logos and themes in `src/admin` with a rebuild [D]. Sandloom's tokens could take a runtime override, but this does not exist today [S].
4. **Copy one piece.** `refine swizzle`, shadcn `add` [D].
5. **Replace the bundle.** Prefect's `PREFECT_UI_STATIC_DIRECTORY` [S] and Hasura's `--console-assets-dir` [D] show a served directory that the operator can point elsewhere.
6. **Full fork.** Keycloak's npm package and Medusa's `--admin-only` deployment are partial forms [D].

**Inference.** Rung 5 is cheap and gives a complete escape hatch. A user who needs custom React forks the source, builds with Node, and points the module at the result. Rung 4 needs component APIs Sandloom has not stabilized.

## What fits Sandloom

**Everything in this section is judgement**, resting on the evidence above.

### Testing the tentative idea: an image plus a new `sandloom new` flag

| Problem | Evidence |
| --- | --- |
| The quickstart runs the app on the host with `uvicorn --reload`; Docker holds only Postgres and Redis | `QUICKSTART` in `project.py`. An image would need `host.docker.internal`, and nginx's `resolver 127.0.0.11` assumes Docker networking |
| The image's manifest and config are the framework's, not the app's | `public/config.json` lists tenancy and billing, and `loadConfig` rejects a config naming modules absent from the manifest. Each app must export and mount both files anyway |
| The default upstream does not match generated apps | Overlay and Dockerfile default to `http://app:2222`; the generated compose publishes `8000:8000` [M] |
| Two artifacts in two registries must be paired by hand | No resolver links an image tag to `uv.lock` |
| A creation-time flag decides too early, and misses existing apps | `--with` already exists and already means "add this package" |
| Installing Docker does not remove the manifest step | Export still runs on the Python side |

The image has real strengths: no Python process for static files, isolation by network, and a low memory limit. Those matter to a subset of production operators, so they justify a later, derived artifact rather than the default.

### Recommendation

| Decision | Choice |
| --- | --- |
| **Default model** | Prebuilt console in a wheel, served by the app, same-origin, at `/admin/` |
| **Packaging** | New distribution `sandloom-admin` (import `sandloom_admin`, entry point `sandloom.modules: admin`). Extra `sandloom[admin]` adds it and identity. **Not** in `sandloom[standard]`: `sandloom.main:app` composes every installed entry point, so inclusion would switch it on silently. No published image in the first step |
| **Why not inside `sandloom`** | Keeps React advisories, the JS SBOM and the release cadence out of the kernel, and leaves "no UI dependency" true. Dagster and MLflow split the same way. 0.42 MB alone would not justify it |
| **`sandloom new`** | No new flag. `--with admin` adds the dependency, implies identity, and prints `http://localhost:8000/admin/` with the first-admin command. An existing app runs `uv add sandloom-admin` and adds `admin` to `BUNDLED` in `asgi.py`, because a generated app lists its bundled modules there [M] |
| **Versioning** | Same release train as the kernel (`sandloom>=x,<0.8`, as the module packages do). The wheel also ships a `console.json`: supported manifest versions, widget names, tested ranges for identity, tenancy and billing. `sandloom admin export` and boot check it, so skew fails with a code in the terminal rather than as "Invalid admin manifest" in a browser. Python has no conditional constraint for optional packages, so this must be a runtime check |
| **Manifest** | Move it into the generated files: `sandloom generate` writes it (for example `<app>/shared/admin-manifest.json`), `sandloom test` checks it. It then ships in the app's image with the same bytes in both profiles. `config.json` becomes a value served from settings, defaulting to all modules in the manifest |
| **Monolith** | The `app` process serves it. Worker and beat are unaffected |
| **Microservices** | `sandloom add deploy admin --service identity` by default: the console already cannot work without identity, and this adds no process. `--service admin` for those who want a dedicated 108 MB process. The gateway route is generated, so the UI stays topology-free, as `/api/v1/platform/<module>` already does. Aggregation stays as it is |
| **Production exposure** | Off when `ENVIRONMENT=prod` unless `ADMIN_CONSOLE_ENABLED=true`, on in dev. `sandloom doctor` warns when it is on without a restriction. Document a proxy rule for `/admin/` and, separately, for `/api/v1/platform/`, because restricting the UI alone protects little |
| **Extension** | Declarations only for 1.0. Grow the manifest language (version 2) when real apps need more. Compiled widgets remain framework-only |
| **Eject** | `ADMIN_CONSOLE_DIST=<dir>` serves any built directory. Later, `sandloom admin eject <dir>` copies the source and lockfile (366 KB), after which that user owns Node and upgrades |

### Why a module rather than a kernel mount

- **Route policy applies.** `public(reason=…)` states the decision, and the route snapshot records it. I did not trace how B008 treats a `Mount`.
- **Discovery, splitting and checks already exist.** Entry points, `--with`, `sandloom add deploy` and the profile checks need nothing new, as the spike's gateway output shows.
- **No `Mount` on every path.** The route index stays effective.
- **The isolation rule holds, with one addition.** The module needs its manifest through a public SDK name, not `sandloom.core`. That is a reviewed change to the public surface (SANDLOOM-T008), and it also has to suppress the module's own platform views.

### Trade-offs accepted

- Python serves static files. An operator console has low traffic, and hashed assets are cached as immutable, so this should not matter. I did not load-test it.
- The console shares the API's origin unless it has its own hostname. Tokens stay in memory and the API origin serves JSON, but an app that adds HTML to that origin puts it next to the console. Operators who care should give the console its own hostname.
- A process loading the module carries the route. Production's default-off handles the unintended case.
- Release CI gains a Node build step, and the sdist must carry the built assets, because `packages/README.md` promises a self-contained sdist that rebuilds with Hatch alone.
- A generated manifest can go stale during development until `sandloom generate` runs. The `sandloom test` gate catches it. A dev-mode live compile (0.1 s) could come later.

### First shippable step

1. **First-admin command**, for example `sandloom identity grant-admin <email>`. Without it the console is a login page nobody can pass. Everything else depends on this.
2. **Make the console prefix-aware.** Set Vite `base` to `/admin/`, router `basename` from `import.meta.env.BASE_URL`, and relative bootstrap fetches. A `grep` found about 20 absolute path literals to review. Update the browser tests.
3. **`packages/sandloom-admin`.** The module route from the spike, CSP and cache policy carried over from `admin/nginx.conf`, licences, Node build in `packages.yml`, assets in wheel and sdist.
4. **`--with admin` and `sandloom[admin]`.**
5. **Manifest in `sandloom generate` and `sandloom test`, with `console.json` checks.**
6. **A quickstart gate.** From wheels built at one commit: `sandloom new --with admin`, migrate, grant admin, and a Playwright login at `/admin/`. This is what proves the design, since the spike did not cover a login.
7. **Console SBOM** with the release, then update the guides and remove the repository-source path from them. Nothing here needs compatibility shims before release.

### Rejected options

| Option | Why not |
| --- | --- |
| Published image as the default | The reasons above. Revisit as a derived artifact when an operator needs network-level separation. It is about 30 lines over the same assets, plus signing and an SBOM |
| Assets inside the `sandloom` wheel | Size is fine, but it couples the kernel's release to JS advisories and makes an optional feature mandatory |
| In `sandloom[standard]` | Entry-point composition would expose a new public route on install |
| CDN assets (FastAPI, Hasura) | Breaks `script-src 'self'`, air-gapped use and pinning |
| Build on install, or an npm dependency (Medusa, Strapi, Payload) | Breaks the no-Node constraint and pulls 575 dev packages into the path |
| Hosted console | No hosted service exists, and it needs CORS and reach to private APIs |
| JS plugins or module federation now | Removes the "no executable manifest" property and needs a stable JS API plus React sharing. Backstage and Keycloak still mark their versions alpha or experimental, and Grafana relies on signing |
| Server-rendered rewrite (Django, SQLAdmin style) | Python-side extension would be simple, but it discards a working, tested console and its API-only boundary |
| Compile the manifest at runtime in each process | Each split service sees only its own modules, so the result differs by profile |
| Per-module manifest fragments merged in the browser | Attractive for split deployments and a natural fit for the platform prefixes, but it moves the manifest behind login and needs service discovery. Revisit if build-time export proves limiting |
| Base-path placeholder rewritten on disk (Prefect) | Conflicts with the read-only runtime image. A fixed `/admin/` is simpler. Dagster's in-memory rewrite is the fallback if a configurable prefix is demanded |
| `sandloom new --admin` | Duplicates `--with` |

## What I could not verify

- **End-to-end operation.** The spike was HTTP-level, without a database, a login or a browser, and ran on the published 0.7.0a5 rather than HEAD. `sandloom add deploy --check`, `sandloom test` and compose were not run.
- **The first-admin gap.** It rests on searching the CLI parser, identity sources and docs. A mechanism elsewhere is possible.
- **Image sizes.** Registry-reported compressed sizes, not pulled images. I did not build Sandloom's image, so its 26 MB-plus figure is an estimate from the base image. Keycloak and Kafbat were measured from `latest` and `main`.
- **Python-versus-nginx serving.** No load test, and no nginx memory figure. 108 MB is macOS peak RSS, and may differ in a Linux container.
- **Docs ahead of releases.** Default-branch docs may describe unreleased behaviour, for example Grafana's sandbox deprecation or Medusa's Vite notes. Wheel listings are released versions.
- **Claims tagged [D].** Medusa, Strapi, Payload, Refine, react-admin, shadcn, Backstage, Grafana plugin behaviour, Temporal, Supabase, Keycloak paths and Flower were read in docs only. I did not trace their code.
- **`resolver 127.0.0.11`** being Docker-specific is my knowledge, not something I checked in a Docker source. I also did not test the image on Kubernetes.
- **Compatibility promises.** I found no stated compatibility policy for Kafbat, nor one for Temporal UI beyond its floor. Absence means targeted searches found none.
- **Keycloak declarative UI.** I confirmed the SPI and the `EXPERIMENTAL` flag in source, but found no current guide, so I cannot describe its full capabilities.
- **pgAdmin's and Argo CD's internal asset layout** were not inspected further than the wheel listing and the `ui.Embedded` lines cited.
