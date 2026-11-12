# Object store v1 contract review

The common kit runs through the SDK on the built-in filesystem provider, an
independently discovered test plugin and the separately installed S3 plugin.
It exercises byte and async stream uploads, overwrite, read, delete and
read after delete. The S3 endpoint suite also exercises a signed HTTP GET,
content type, metadata, bucket health and an oversized overwrite.

## Findings before the parity changes

| Surface | Observed behaviour | Contract follow-up |
| --- | --- | --- |
| Keys and `put()` result | Local and S3 return the logical key. The provider docstring promises a backend locator. The S3 driver requires nonempty slash-separated segments and rejects dot segments, backslashes, NUL and keys over 1024 UTF-8 bytes. Local can normalize some keys S3 rejects. | Define a portable key grammar and promise that the result can be passed back to `get()` and `delete()`. Check old keys before enforcing a grammar in the local provider. |
| Uploads and overwrite | `bytes \| Any` makes it impossible to type-check the stream interface. Local truncates an existing file before completing the upload, then removes it on failure. S3 buffers a seekable request body before PUT, so a size or stream-read failure leaves the previous object intact. | Define an async byte reader protocol and decide whether failed uploads must preserve existing content. Local would need a temporary file and atomic replacement to offer that guarantee. |
| Reads and errors | Both backends return a full byte string. Both report a missing key with `FileNotFoundError` when the backend reveals that the key is absent. AWS can report access denied instead of absence when credentials lack bucket-list permission. Size limits currently raise backend-specific `ValueError` subclasses or `ValueError`. | Specify absence and size-limit errors. Add a streaming read as a separate capability if a caller needs downloads larger than its memory budget. |
| Delete | A successful delete makes the object unavailable at that key. A repeat raises `FileNotFoundError` on local storage and succeeds on S3. Versioned S3 buckets retain older versions according to bucket policy. | Decide whether to standardize on idempotent logical deletion. The v1 kit checks one successful deletion. |
| Content type, metadata and URLs | Local ignores content type and metadata. Local has no retrieval URL. S3 forwards the headers and returns a short-lived signed GET. `OBJECTSTORE_CAPABILITY` declares no optional features, so modules cannot declare these as requirements. | Add feature declarations when a module needs to require metadata or a retrieval URL. Specify expiry and clarify that signing a GET need not check object existence. |
| Health | Local checks its root directory. S3 probes the configured bucket with HeadBucket. Bucket health does not establish access to every object key. | Keep health at the configured-instance level and document the permission needed by each implementation. |

The portable SDK signatures remain at v1. The kit captures behaviour observed
on the included providers. Its absence check requires credentials that allow
the backend to disclose that a key is missing.

## Portable contract decision

Keep the module-facing interface scoped to one configured store. S3's
[operations catalogue](https://docs.aws.amazon.com/AmazonS3/latest/API/API_Operations_Amazon_Simple_Storage_Service.html)
contains both object operations and bucket administration. The latter needs a
different permission model and belongs in deployment tooling or the native AWS
SDK. A module can require a feature through its capability declaration.

| S3 surface | Sandloom boundary |
| --- | --- |
| PutObject, GetObject, DeleteObject | Portable core. Preserve the previous object on a failed input or size refusal, return the logical key, and make removal of the current object idempotent. |
| Content type, user metadata, signed GET | The existing parameters and URL method are retained. S3 advertises `metadata` and `presigned_get`. Local accepts the content headers as hints and reports URL retrieval as unsupported. Modules depending on the hints surviving or on URL retrieval declare the relevant feature. |
| HeadObject, ranged and streamed GET, listing, copy, conditional writes | Candidates for portable features when a caller requires them. Each needs its own error, consistency and pagination semantics in conformance. |
| Multipart sessions, version IDs, delete markers, retention, tagging and archive restore | Explicit backend features where needed. Their identity, lifecycle and permission semantics depend on the service and bucket configuration. |
| Bucket creation, policies, lifecycle, replication, CORS, encryption and inventory | Deployment or operator tooling using the native S3 API. |

The portable upload type covers bytes and asynchronous sized readers. Both
drivers also accept the legacy one-shot async `read()`. The same key syntax
and limit exceptions are used in both implementations. A filesystem deployment
must additionally respect its filename length, case sensitivity and
file-versus-directory constraints. Existing files remain stored at their
original paths.
