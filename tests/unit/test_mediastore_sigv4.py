"""SigV4 against AWS's published examples.

Expected values come from the Amazon S3 API Reference, "Authenticating Requests: Using Query
Parameters (AWS Signature Version 4)" (sigv4-query-string-auth.html, example "GET
examplebucket/test.txt") and "Authenticating Requests: Using the Authorization Header"
(sig-v4-header-based-auth.html, examples "GET Object", "PUT Object", "GET Bucket Lifecycle"
and "Get Bucket (List Objects)"). All use the documented example credentials, region
us-east-1 and the time 2013-05-24T00:00:00Z.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from runway.mediastore.sigv4 import (
    EMPTY_PAYLOAD_SHA256,
    SigV4Credentials,
    canonical_request,
    presign_url,
    sha256_hex,
    sign_headers,
)

CREDENTIALS = SigV4Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
WHEN = datetime(2013, 5, 24, tzinfo=UTC)
SCOPE = "AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request"


def _sign(method: str, url: str, headers: dict[str, str], payload_hash: str) -> dict[str, str]:
    return sign_headers(
        method,
        url,
        headers,
        payload_hash=payload_hash,
        credentials=CREDENTIALS,
        region="us-east-1",
        service="s3",
        now=WHEN,
    )


def test_presigned_url_matches_the_aws_example() -> None:
    url = presign_url(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        credentials=CREDENTIALS,
        region="us-east-1",
        service="s3",
        now=WHEN,
        expires_seconds=86400,
    )
    assert url == (
        "https://examplebucket.s3.amazonaws.com/test.txt"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z"
        "&X-Amz-Expires=86400"
        "&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
    )


def test_get_object_header_signature_matches_the_aws_example() -> None:
    headers = _sign(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        {"Range": "bytes=0-9"},
        EMPTY_PAYLOAD_SHA256,
    )
    assert headers["authorization"] == (
        f"AWS4-HMAC-SHA256 Credential={SCOPE},"
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,"
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert headers["x-amz-date"] == "20130524T000000Z"
    assert headers["x-amz-content-sha256"] == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert "host" not in headers


def test_get_object_canonical_request_matches_the_aws_example() -> None:
    request, signed = canonical_request(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        {
            "host": "examplebucket.s3.amazonaws.com",
            "range": "bytes=0-9",
            "x-amz-content-sha256": EMPTY_PAYLOAD_SHA256,
            "x-amz-date": "20130524T000000Z",
        },
        EMPTY_PAYLOAD_SHA256,
    )
    assert signed == "host;range;x-amz-content-sha256;x-amz-date"
    assert sha256_hex(request.encode()) == (
        "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972"
    )


def test_put_object_header_signature_matches_the_aws_example() -> None:
    body = b"Welcome to Amazon S3."
    headers = _sign(
        "PUT",
        "https://examplebucket.s3.amazonaws.com/test%24file.text",
        {"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
        sha256_hex(body),
    )
    assert headers["x-amz-content-sha256"] == (
        "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    )
    assert headers["authorization"].endswith(
        "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class,"
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )


@pytest.mark.parametrize(
    ("url", "signature"),
    [
        (
            "https://examplebucket.s3.amazonaws.com/?lifecycle",
            "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543",
        ),
        (
            "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J",
            "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
        ),
    ],
)
def test_bucket_query_signatures_match_the_aws_examples(url: str, signature: str) -> None:
    headers = _sign("GET", url, {}, EMPTY_PAYLOAD_SHA256)
    assert headers["authorization"].endswith(f"Signature={signature}")


def test_query_order_and_extra_signed_headers_change_only_what_they_should() -> None:
    first = presign_url(
        "PUT",
        "https://example.com/b/k?uploadId=a%2Bb&partNumber=2",
        credentials=CREDENTIALS,
        region="auto",
        service="s3",
        now=WHEN,
        expires_seconds=60,
        signed_headers={"Content-Length": "5"},
    )
    reordered = presign_url(
        "PUT",
        "https://example.com/b/k?partNumber=2&uploadId=a%2Bb",
        credentials=CREDENTIALS,
        region="auto",
        service="s3",
        now=WHEN,
        expires_seconds=60,
        signed_headers={"content-length": "5"},
    )
    assert first == reordered
    assert "X-Amz-SignedHeaders=content-length%3Bhost" in first
    assert "partNumber=2&uploadId=a%2Bb" in first


@pytest.mark.parametrize("expires", [0, 604801])
def test_presign_rejects_lifetimes_sigv4_does_not_allow(expires: int) -> None:
    with pytest.raises(ValueError):
        presign_url(
            "GET",
            "https://example.com/b/k",
            credentials=CREDENTIALS,
            region="auto",
            service="s3",
            now=WHEN,
            expires_seconds=expires,
        )


def test_secret_is_not_in_the_credentials_repr() -> None:
    assert "wJalrXUtnFEMI" not in repr(CREDENTIALS)


def test_naive_time_is_refused() -> None:
    with pytest.raises(ValueError):
        sign_headers(
            "GET",
            "https://example.com/",
            {},
            payload_hash=EMPTY_PAYLOAD_SHA256,
            credentials=CREDENTIALS,
            region="auto",
            service="s3",
            now=datetime(2013, 5, 24),
        )
