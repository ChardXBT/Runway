from runway.capture.browser import BrowserCaptureService


def test_surface_growth_ignores_layout_height_jitter() -> None:
    previous = (200, 131_429, "UgkxTail")

    assert not BrowserCaptureService._surface_advanced(
        previous,
        (200, 131_612, "UgkxTail"),
    )
    assert BrowserCaptureService._surface_advanced(
        previous,
        (210, 137_200, "UgkxNewTail"),
    )
    assert BrowserCaptureService._surface_advanced(
        previous,
        (200, 131_429, "UgkxVirtualizedTail"),
    )
    assert BrowserCaptureService._surface_advanced(None, previous)
