from app.ai.detectors.staff_zone import (
    UNAUTHORISED_SECONDS,
    _unauthorised_ready,
)


def test_unknown_person_alerts_after_staff_zone_grace() -> None:
    assert not _unauthorised_ready("unknown", UNAUTHORISED_SECONDS - 1)
    assert _unauthorised_ready("unknown", UNAUTHORISED_SECONDS)


def test_uncertain_uniform_read_cannot_suppress_security_forever() -> None:
    assert not _unauthorised_ready("uncertain", UNAUTHORISED_SECONDS - 1)
    assert _unauthorised_ready("uncertain", UNAUTHORISED_SECONDS)


def test_identified_staff_never_enters_unauthorised_path() -> None:
    assert not _unauthorised_ready("medium", UNAUTHORISED_SECONDS * 10)
    assert not _unauthorised_ready("high", UNAUTHORISED_SECONDS * 10)
