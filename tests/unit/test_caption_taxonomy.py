from runway.captions.taxonomy import analyze_caption


def test_noun_phrase_with_question_mark_is_not_open_ended() -> None:
    result = analyze_caption("Bart and Lisa holding hands?")

    assert result.is_question is True
    assert result.open_question is False
    assert result.structure == "yes_no_question"


def test_complete_wh_question_is_open_ended() -> None:
    result = analyze_caption("Why are Bart and Lisa holding hands?")

    assert result.is_question is True
    assert result.open_question is True
    assert result.structure == "open_question"


def test_visible_object_lookup_is_a_quiz_not_an_open_question() -> None:
    result = analyze_caption("What is Wiggum holding during the conversation?")

    assert result.is_question is True
    assert result.open_question is False
    assert result.structure == "quiz"


def test_contracted_visible_lookup_is_a_quiz_not_an_open_question() -> None:
    for text in (
        "What's on that chalkboard behind Wiggum?",
        "What’s Homer holding?",
        "Who's on the glowing screen?",
        "What does Homer hold?",
    ):
        result = analyze_caption(text)

        assert result.is_question is True
        assert result.open_question is False
        assert result.structure == "quiz"


def test_unknown_purpose_question_remains_open_ended() -> None:
    result = analyze_caption("What are those keys for?")

    assert result.is_question is True
    assert result.open_question is True
    assert result.structure == "open_question"


def test_counting_question_is_a_quiz_not_an_open_question() -> None:
    result = analyze_caption("How many microphones can reach Wiggum?")

    assert result.is_question is True
    assert result.open_question is False
    assert result.structure == "quiz"
