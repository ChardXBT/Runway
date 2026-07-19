from runway.discovery.safety import (
    is_known_adult_domain,
    is_known_adult_result,
    is_likely_query_match,
)
from runway.discovery.schemas import ImageSearchResult


def test_known_adult_domains_are_blocked_before_download() -> None:
    result = ImageSearchResult(
        search_query="fixture",
        result_rank=1,
        source_page_url="https://media.eporner.com/example",
        direct_image_url="https://cdn.example.test/image.jpg",
        source_domain="media.eporner.com",
    )

    assert is_known_adult_domain("www.eporner.com")
    assert is_known_adult_result(result)


def test_art_and_fan_community_domains_are_not_treated_as_nsfw() -> None:
    result = ImageSearchResult(
        search_query="fixture",
        result_rank=1,
        source_page_url="https://www.deviantart.com/example",
        direct_image_url="https://images.example.test/image.jpg",
        source_domain="www.deviantart.com",
    )

    assert not is_known_adult_result(result)


def test_obviously_unrelated_search_card_is_skipped_before_download() -> None:
    unrelated = ImageSearchResult(
        search_query='"Marge Simpson" Kwik-E-Mart groceries screencap',
        result_rank=1,
        source_page_url="https://stock.example.test/laboratory-robot",
        direct_image_url="https://cdn.example.test/robot.jpg",
        source_domain="stock.example.test",
        provider_metadata={"result_title": "Automated robotics laboratory"},
    )
    related = unrelated.model_copy(
        update={
            "source_page_url": "https://example.test/marge-simpson-kwik-e-mart",
            "provider_metadata": {"result_title": "Marge Simpson shops at the Kwik-E-Mart"},
        }
    )

    assert not is_likely_query_match(unrelated)
    assert is_likely_query_match(related)
