# Sandloom S3 driver

Install this package alongside the matching Sandloom release. For a checkout,
run `uv pip install -e packages/sandloom -e packages/sandloom-s3` from the
repository root. Sandloom discovers `objectstore.s3` through the
`sandloom.drivers` entry-point group.

For an existing AWS S3 general-purpose bucket, set:

```sh
INFRA='{"objectstore":{"default":{"driver":"s3","bucket":"app-objects","region":"eu-west-1"}}}'
```

The driver uses the AWS credential chain by default. For MinIO or another
S3-compatible endpoint, add `endpoint_url` and, if needed, set
`addressing_style` to `path`. Supply credentials through the SDK environment
or through both `access_key_id` and `secret_access_key` in the instance
configuration. Use an HTTPS endpoint in production.

The credentials need PutObject, GetObject and DeleteObject permissions on the
object keys. Readiness probes use HeadBucket, which requires ListBucket on AWS.
The bucket must already exist. `url()` signs a GET for 300 seconds by default.
The endpoint used for signing must be reachable by the URL recipient. Treat
the signed URL as a credential until it expires.

Uploads accept bytes or a stream with an async `read(size)` method. Stream
uploads spool to temporary storage before one PutObject call.
`max_upload_size_bytes` defaults to 50 MiB and also limits downloads, which
return bytes in memory. Zero removes the configured limit. Single PUTs are
still capped at 5 GiB. Delete follows S3 semantics and succeeds for absent keys.

AWS S3 general-purpose buckets, MinIO and SeaweedFS use the same driver. Other
S3-compatible services can be exercised with the same endpoint test using a
dedicated test bucket and `SANDLOOM_S3_TEST_ENDPOINT`. Compatibility depends
on the service's support for SigV4, HeadBucket, PutObject, GetObject,
DeleteObject and signed GETs.

## Backend compatibility

| Backend | Configuration | Verification |
| --- | --- | --- |
| Amazon S3 general-purpose buckets | Bucket and AWS region, SDK credential chain | Targeted by the driver. The endpoint suite can run against a dedicated AWS test bucket. [AWS S3 API](https://docs.aws.amazon.com/AmazonS3/latest/API/Welcome.html) |
| MinIO | Endpoint URL, region, path-style addressing | Passed the endpoint suite on RELEASE.2025-04-22T22-12-26Z. The MinIO integration job uses that release. [MinIO and boto3](https://support.hpe.com/hpesc/public/docDisplay?docId=a00aie17hen_us&docLocale=en_US&page=Notebooks%2Fusing-boto3.html) |
| SeaweedFS | Endpoint URL, region, path-style addressing | Passed the endpoint suite on 4.53. The SeaweedFS integration job uses that release. [SeaweedFS S3 quick start](https://github.com/seaweedfs/seaweedfs#quick-start) |
| Cloudflare R2 | Account S3 endpoint, region `auto`, R2 API credentials | Candidate for endpoint testing. [R2 S3 compatibility](https://developers.cloudflare.com/r2/api/s3/api/) |
| Ceph RGW | Gateway endpoint, region and credentials for that gateway | Candidate for endpoint testing. [Ceph S3 API compliance](https://docs.ceph.com/en/latest/dev/radosgw/s3_compliance/) |

SeaweedFS 3.97 failed the endpoint suite during reads with `NoSuchBucket` after successful writes.

Run the endpoint suite from the repository root. It writes and removes objects
under unique `conformance/` keys in the selected bucket.

```sh
SANDLOOM_S3_TEST_BUCKET=sandloom-conformance \
SANDLOOM_S3_TEST_ENDPOINT=http://127.0.0.1:9000 \
uv run --no-sync pytest -n 2 --dist loadfile packages/sandloom-s3/tests
```

The MinIO CI job runs the endpoint suite as a service user with
`s3:ListBucket` on the test bucket and object permissions under
`conformance/*`. To run its access check on an existing test endpoint, seed
an object outside the permitted prefix as an administrator and set
`SANDLOOM_S3_TEST_DENIED_KEY` to that key. Run pytest with the service user's
credentials. The check expects a missing key under the permitted prefix to
raise `FileNotFoundError` and reads of the seeded object to return HTTP 403,
including through a signed GET URL.
