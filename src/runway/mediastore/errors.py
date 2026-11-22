from __future__ import annotations


class MediaStoreError(Exception):
    """Base class for storage failures. Messages never contain credentials or signed URLs."""


class InvalidMediaKey(MediaStoreError, ValueError):
    """The key is malformed or would escape the store's root."""


class InvalidUploadRequest(MediaStoreError, ValueError):
    """The size, content type, part number or part length is not acceptable."""


class UploadNotFound(MediaStoreError):
    """No upload in progress matches the plan (never started, aborted or completed)."""


class ObjectNotFound(MediaStoreError):
    pass


class ObjectExists(MediaStoreError):
    pass


class IncompleteUpload(MediaStoreError):
    """``complete_upload`` was called before every part arrived."""


class SizeMismatch(MediaStoreError):
    pass


class ChecksumMismatch(MediaStoreError):
    pass


class UnsupportedOperation(MediaStoreError):
    """The store does not offer this operation in its mode (for example API uploads on R2)."""


class StorageBackendError(MediaStoreError):
    """The storage service failed or returned an unexpected response."""

    def __init__(self, detail: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(detail)
        self.status = status
        self.retryable = retryable
