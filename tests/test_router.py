from sage.agents.router import MODE_TO_INTENT


def test_frontend_mode_ids_are_mapped() -> None:
    assert MODE_TO_INTENT["quiz"] == "quiz"
    assert MODE_TO_INTENT["roadmap"] == "roadmap"
    assert MODE_TO_INTENT["fix"] == "fix"


def test_legacy_mode_aliases_still_work() -> None:
    assert MODE_TO_INTENT["quiz me"] == "quiz"
    assert MODE_TO_INTENT["study plan"] == "roadmap"
    assert MODE_TO_INTENT["code fix"] == "fix"
