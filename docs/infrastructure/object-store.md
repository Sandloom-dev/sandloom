---
title: Object store
description: Store files by key using the filesystem or an S3-compatible backend.
job: Choose and configure an object store for an application.
purpose: Connect a storage backend while keeping module code on the ObjectStore interface.
audience: [developer]
level: building
type: concept
---

# Object store

An object store keeps files by key. Modules use the same `put`, `get` and
`delete` calls with either driver. Copy existing objects before switching
backends.

## What a module calls

```python
from sandloom.sdk.infra import objectstore

async def save_report(data: bytes) -> str:
    return await objectstore().put("reports/1.pdf", data)
```

`put` accepts bytes or an asynchronous byte stream and returns the key after
the upload completes. An interrupted input stream leaves the previous object
available. `get` returns bytes and raises `FileNotFoundError` when the backend
reports a missing key. `delete` succeeds if the object is already absent.
Call `objectstore(name="exports")` to select a named instance.

## Drivers

### `local`

Set `STORAGE_TYPE=local` and `STORAGE_PATH` for filesystem storage. Use
`path` and `max_upload_size_bytes` to override the defaults on a named instance.

Mount the same persistent volume in every replica that needs the files.
Production requires `local_storage_in_prod` in `SANDLOOM_ACCEPTED_RISKS`.

### `s3`

This driver requires Sandloom 0.7.0a13 or later in the 0.7 series.
Add the driver to your application.

```sh
uv add "sandloom-s3==0.7.0a13"
```

For an existing AWS S3 general-purpose bucket, set the `INFRA` environment
variable to this JSON with your bucket name and region. The `default` instance
serves calls to `objectstore()`. Keep any other entries in your existing `INFRA`.

```json
{
  "objectstore": {
    "default": {
      "driver": "s3",
      "bucket": "app-objects",
      "region": "eu-west-1"
    }
  }
}
```

The driver uses the AWS SDK credential chain. Grant `s3:ListBucket` on the
bucket for readiness and missing-key responses. Grant `s3:GetObject`,
`s3:PutObject` and `s3:DeleteObject` on the keys the application uses.

For MinIO or SeaweedFS, add `endpoint_url` and `addressing_style` to the
instance alongside `bucket` and `region`. Set them to your storage server's
URL and `"path"`, respectively. Set `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` to the credentials for that server. Both backends
run the object store conformance suite in CI.

[Declare the `metadata` feature](index.md#declare-what-you-need)
if your module needs to preserve content type
or custom metadata. Require `presigned_get` if it calls
`await objectstore().url(key)` to sign a download link. Both features are
available with the S3 driver. Treat the signed link as a credential
and use an endpoint the recipient can reach. The default lifetime is 300 seconds. Set `presign_seconds` to change it.

See the [driver package on PyPI](https://pypi.org/project/sandloom-s3/) for size
limits and the backend verification matrix. Choose an instance in [Configure backends](configure.md).
