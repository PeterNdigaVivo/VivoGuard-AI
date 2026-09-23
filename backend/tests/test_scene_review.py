"""is_noteworthy() is the whole safety valve of the scene reviewer.

Everything else in the sweep is I/O. This one function decides whether
a frame becomes an alert, and it reads free text from a model that was
asked — but cannot be forced — to answer with a single token. Getting
it wrong in one direction floods the operator feed; in the other it
silently drops real findings.
"""
from app.tasks.scene_review import is_noteworthy


def test_bare_none_is_not_noteworthy():
    assert is_noteworthy("NONE") is False


def test_none_with_punctuation_and_case():
    # Observed on live frames: the model sometimes adds a full stop, and
    # occasionally lowercases despite the prompt asking for the token.
    for reply in ("NONE.", "none", "None.", "  NONE  \n"):
        assert is_noteworthy(reply) is False, reply


def test_none_with_trailing_gloss_is_still_none():
    # "NONE - ordinary retail activity" must not be read as a finding
    # just because it carries extra words.
    assert is_noteworthy("NONE - nothing unusual in this frame") is False


def test_markdown_decorated_none():
    # The model returns markdown when it forgets the plain-text rule.
    assert is_noteworthy("**NONE**") is False
    assert is_noteworthy("- NONE") is False


def test_empty_and_missing_are_not_noteworthy():
    # A failed call must never manufacture an alert out of silence.
    assert is_noteworthy(None) is False
    assert is_noteworthy("") is False
    assert is_noteworthy("   ") is False


def test_real_description_is_noteworthy():
    assert is_noteworthy(
        "A man in a grey t-shirt is standing on a stepladder near the "
        "fitting room entrance, reaching towards the ceiling.") is True


def test_description_merely_containing_none_is_noteworthy():
    # The word appears mid-sentence; only a LEADING token means "nothing
    # to report", so this must survive as a finding.
    assert is_noteworthy(
        "Two people are at the counter and none of them appear to be "
        "staff.") is True
