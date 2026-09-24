"""is_noteworthy() is the whole safety valve of the scene reviewer.

Everything else in the sweep is I/O. This one function decides whether
a frame becomes an alert, and it reads free text from a model that was
asked — but cannot be forced — to answer with a single token. Getting
it wrong in one direction floods the operator feed; in the other it
silently drops real findings.
"""
from app.tasks.scene_review import is_noteworthy, parse_verdict


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


# ---- parse_verdict: the JSON contract -----------------------------------

def test_json_false_returns_nothing():
    assert parse_verdict('{"noteworthy": false, "description": ""}') is None


def test_json_true_returns_type_and_description():
    assert parse_verdict(
        '{"noteworthy": true, "category": "unusual", "description": "A red '
        'ladder is leaning against the wall."}') == (
            "scene_review", "A red ladder is leaning against the wall.")


def test_phone_category_routes_to_its_own_type():
    # Must NOT become a scene_review alert: phone_usage is informational
    # and independently disableable, which only works if it routes apart.
    assert parse_verdict(
        '{"noteworthy": true, "category": "phone", "description": "A person '
        'in an aisle is looking at a phone."}') == (
            "phone_usage", "A person in an aisle is looking at a phone.")


def test_missing_category_defaults_to_scene_review():
    assert parse_verdict(
        '{"noteworthy": true, "description": "A ladder is out."}'
    ) == ("scene_review", "A ladder is out.")


def test_json_true_with_empty_description_is_nothing():
    # A flag with nothing to show an operator is not an alert.
    assert parse_verdict('{"noteworthy": true, "description": "  "}') is None


def test_json_string_boolean():
    # Small models sometimes quote the boolean.
    assert parse_verdict(
        '{"noteworthy": "true", "description": "A child is on the counter."}'
    ) == ("scene_review", "A child is on the counter.")


def test_json_true_but_describes_ordinary_scene():
    # Observed in shadow: flag set, then an all-clear sentence. The words
    # win over the boolean.
    assert parse_verdict(
        '{"noteworthy": true, "description": "The store is operating '
        'normally with staff at the counter."}') is None


# ---- prose fallback, using replies seen in the live shadow log ----------

def test_prose_all_clear_is_not_a_finding():
    # These flooded the first shadow run: the model judged the frame fine
    # and wrote a paragraph instead of the token, and every one counted
    # as an alert.
    for reply in (
        "The scene is ordinary retail activity.",
        "The store appears to be operating normally with staff working "
        "at the counter and clothes displayed on racks.",
        "There are no signs of maintenance, unattended equipment, or any "
        "unusual activity that would require immediate attention.",
    ):
        assert parse_verdict(reply) is None, reply


def test_prose_real_finding_survives_fallback():
    assert parse_verdict(
        "A red ladder is leaning against the wall in the corner of the "
        "store.") == ("scene_review",
                      "A red ladder is leaning against the wall in the "
                      "corner of the store.")


def test_prose_none_token_still_handled():
    assert parse_verdict("NONE") is None
    assert parse_verdict(None) is None
