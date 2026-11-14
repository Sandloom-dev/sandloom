"""An object store for AWS S3 and S3-compatible endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from tempfile import SpooledTemporaryFile
from typing import Any, Literal, cast

from anyio import Lock, to_thread
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)

from sandloom.driverkit import (
    HealthResult,
    InfraError,
    NotFound,
    ObjectContent,
    ObjectStoreProvider,
    PermissionDenied,
    Timeout,
    TooLarge,
    Unavailable,
    iter_object_chunks,
    validate_object_key,
)

_CHUNK_SIZE = 1024 * 1024
#: S3 error codes of a refused identity (the read is not a miss).
_DENIED = frozenset(
    {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "403"}
)
_MISSING = frozenset({"NoSuchKey", "404"})


def _mapped(exc: Exception, instance: str) -> InfraError:
    """The contract's error for a botocore failure (spec 9). The message is
    the error code or type, never the backend text (it can carry the URL)."""
    from botocore.exceptions import (
        ClientError,
        ConnectTimeoutError,
        ReadTimeoutError,
    )

    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        message = f"s3 refused the request: {code or 'ClientError'}"
        if code in _MISSING:
            return NotFound(message, capability="objectstore", instance=instance)
        if code in _DENIED:
            return PermissionDenied(
                message, capability="objectstore", instance=instance
            )
        return Unavailable(message, capability="objectstore", instance=instance)
    message = f"s3 request failed: {type(exc).__name__}"
    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError)):
        return Timeout(message, capability="objectstore", instance=instance)
    return Unavailable(message, capability="objectstore", instance=instance)


_MAX_SINGLE_PUT = 5 * 1024**3


class S3Options(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    bucket: str = Field(min_length=1)
    region: str | None = None
    endpoint_url: AnyHttpUrl | None = None
    addressing_style: Literal["auto", "path", "virtual"] = "auto"
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    presign_seconds: int = Field(default=300, ge=1, le=604800)
    max_upload_size_bytes: int = Field(default=50 * 1024**2, ge=0)

    @model_validator(mode="after")
    def validate_connection(self) -> S3Options:
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise ValueError(
                "Provide both S3 credential fields or use the AWS credential chain"
            )
        endpoint = self.endpoint_url
        if endpoint and (
            endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError(
                "S3 endpoint cannot include user info, a query or a fragment"
            )
        return self


class S3ObjectStore(ObjectStoreProvider):
    contract_version = 2
    supported_features = frozenset({"metadata", "presigned_get"})
    options_schema = S3Options
    # The SDK is imported inside the constructor path and operations, so the
    # entry point loads without it and preflight reports SANDLOOM-B037.
    required_imports = ("boto3",)
    install_hint = "pip install sandloom-s3"

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._config = S3Options.model_validate(options)
        self._client: Any = None
        self._client_lock = Lock()

    def _connect(self) -> Any:
        import boto3
        from botocore.config import Config

        options = self._config
        return boto3.session.Session().client(
            "s3",
            region_name=options.region,
            endpoint_url=str(options.endpoint_url) if options.endpoint_url else None,
            aws_access_key_id=options.access_key_id.get_secret_value()
            if options.access_key_id
            else None,
            aws_secret_access_key=options.secret_access_key.get_secret_value()
            if options.secret_access_key
            else None,
            config=Config(
                signature_version="s3v4",
                connect_timeout=2,
                read_timeout=10,
                retries={"mode": "standard", "total_max_attempts": 2},
                s3={"addressing_style": options.addressing_style},
            ),
        )

    async def aopen(self) -> None:
        # Client creation (credential resolution included) can block. Serialize
        # the first open with the same lock used by direct SDK calls.
        async with self._client_lock:
            if self._client is None:
                self._client = await to_thread.run_sync(self._connect)

    async def aclose(self) -> None:
        async with self._client_lock:
            if self._client is not None:
                client, self._client = self._client, None
                await to_thread.run_sync(client.close)

    async def _backend(self) -> Any:
        await self.aopen()
        return self._client

    async def put(
        self,
        key: str,
        data: ObjectContent,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        validate_object_key(key)
        limit = min(
            self._config.max_upload_size_bytes or _MAX_SINGLE_PUT, _MAX_SINGLE_PUT
        )
        params: dict[str, Any] = {"Bucket": self._config.bucket, "Key": key}
        if content_type is not None:
            params["ContentType"] = content_type
        if metadata is not None:
            params["Metadata"] = dict(metadata)
        from botocore.exceptions import BotoCoreError, ClientError

        client = await self._backend()
        try:
            await self._put(client, key, data, limit, params)
        except (BotoCoreError, ClientError) as exc:
            raise _mapped(exc, self.name) from exc
        return key

    async def _put(
        self,
        client: Any,
        key: str,
        data: ObjectContent,
        limit: int,
        params: dict[str, Any],
    ) -> None:
        if isinstance(data, (bytes, bytearray)):
            if len(data) > limit:
                raise TooLarge(
                    f"Upload exceeds {limit} bytes",
                    capability="objectstore",
                    instance=self.name,
                )
            await to_thread.run_sync(
                partial(client.put_object, Body=bytes(data), **params)
            )
        else:
            # A seekable body allows the SDK to sign and retry the request.
            # Spill larger inputs to disk and publish only after the whole
            # source has been read within its size limit.
            with SpooledTemporaryFile(max_size=_CHUNK_SIZE) as body:
                written = 0
                async for chunk in iter_object_chunks(data, _CHUNK_SIZE):
                    written += len(chunk)
                    if written > limit:
                        raise TooLarge(
                            f"Upload exceeds {limit} bytes",
                            capability="objectstore",
                            instance=self.name,
                        )
                    await to_thread.run_sync(body.write, chunk)
                await to_thread.run_sync(body.seek, 0)
                await to_thread.run_sync(
                    partial(client.put_object, Body=body, **params)
                )

    async def get(self, key: str) -> bytes:
        validate_object_key(key)
        client = await self._backend()

        def read() -> bytes:
            from botocore.exceptions import BotoCoreError, ClientError

            try:
                result = client.get_object(Bucket=self._config.bucket, Key=key)
                body = result["Body"]
                try:
                    limit = self._config.max_upload_size_bytes
                    if limit and result["ContentLength"] > limit:
                        raise TooLarge(
                            f"Stored object exceeds {limit} bytes",
                            capability="objectstore",
                            instance=self.name,
                        )
                    return cast(bytes, body.read())
                finally:
                    body.close()
            except (BotoCoreError, ClientError) as exc:
                raise _mapped(exc, self.name) from exc

        return await to_thread.run_sync(read)

    async def delete(self, key: str) -> None:
        validate_object_key(key)
        from botocore.exceptions import BotoCoreError, ClientError

        client = await self._backend()
        try:
            await to_thread.run_sync(
                partial(client.delete_object, Bucket=self._config.bucket, Key=key)
            )
        except (BotoCoreError, ClientError) as exc:
            raise _mapped(exc, self.name) from exc

    async def url(self, key: str) -> str:
        validate_object_key(key)
        client = await self._backend()
        return await to_thread.run_sync(
            partial(
                client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self._config.bucket, "Key": key},
                ExpiresIn=self._config.presign_seconds,
            )
        )

    async def health(self) -> HealthResult:
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
        )

        try:
            client = await self._backend()
            await to_thread.run_sync(
                partial(client.head_bucket, Bucket=self._config.bucket)
            )
        except (BotoCoreError, ClientError) as exc:
            return HealthResult(status="fail", details={"error": type(exc).__name__})
        return HealthResult(status="ok")
