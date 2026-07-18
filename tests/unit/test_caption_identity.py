from runway.captions.service import CaptionService


def test_caption_question_uses_natural_identity_reference() -> None:
    assert CaptionService._character_for_question("Homer Simpson") == "Homer"
    assert CaptionService._character_for_question("yellow-skinned animated male character") == "he"
    assert CaptionService._character_for_question("animated woman") == "she"
    assert CaptionService._character_for_question("other seated attendees") == "they"
    assert CaptionService._character_for_question("unidentified character") == "they"
