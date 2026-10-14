"""is_noteworthy() is the whole safety valve of the scene reviewer.

Everything else in the sweep is I/O. This one function decides whether
a frame becomes an alert, and it reads free text from a model that was
asked â€” but cannot be forced â€” to answer with a single token. Getting
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
            "scene_review", "A red ladder is leaning against the wall.",
            "unusual")


def test_compliance_category_is_carried_through():
    # The whole point of the taxonomy: the operator sees "Working at
    # Height", not "Unusual Activity Seen", and can filter on it.
    assert parse_verdict(
        '{"noteworthy": true, "category": "height_work", "description": '
        '"A man is standing on a stepladder near the fitting rooms."}'
    ) == ("scene_review",
          "A man is standing on a stepladder near the fitting rooms.",
          "height_work")
    assert parse_verdict(
        '{"noteworthy": true, "category": "exit_blocked", "description": '
        '"Boxes are stacked in front of the fire exit."}'
    ) == ("scene_review", "Boxes are stacked in front of the fire exit.",
          "exit_blocked")


def test_invented_category_falls_back_without_losing_the_finding():
    # A label the prompt never offered must not cost us the alert — the
    # description is what the operator actually reads.
    assert parse_verdict(
        '{"noteworthy": true, "category": "weather", "description": '
        '"A ladder is propped against the shelving."}'
    ) == ("scene_review", "A ladder is propped against the shelving.",
          "unusual")


def test_phone_category_routes_to_its_own_type():
    # Must NOT become a scene_review alert: phone_usage is informational
    # and independently disableable, which only works if it routes apart.
    assert parse_verdict(
        '{"noteworthy": true, "category": "phone", "description": "A person '
        'in an aisle is looking at a phone."}') == ("phone_usage", "A person in an aisle is looking at a phone.", "phone")


def test_missing_category_defaults_to_scene_review():
    assert parse_verdict(
        '{"noteworthy": true, "description": "A ladder is out."}'
    ) == ("scene_review", "A ladder is out.", "unusual")


def test_json_true_with_empty_description_is_nothing():
    # A flag with nothing to show an operator is not an alert.
    assert parse_verdict('{"noteworthy": true, "description": "  "}') is None


def test_json_string_boolean():
    # Small models sometimes quote the boolean.
    assert parse_verdict(
        '{"noteworthy": "true", "description": "A child is on the counter."}'
    ) == ("scene_review", "A child is on the counter.", "unusual")


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
                      "corner of the store.", "unusual")


def test_prose_none_token_still_handled():
    assert parse_verdict("NONE") is None
    assert parse_verdict(None) is None


# ---- animals are excluded in code, not just in the prompt --------------

def test_animal_findings_are_dropped():
    # Both dogs that reached the dashboard, verbatim. The prompt had
    # already been told not to report animals; it reported them anyway,
    # so the rule is enforced here where it cannot be declined.
    assert parse_verdict(
        '{"noteworthy": true, "category": "unusual", "description": '
        '"A dog is running across the store floor."}') is None
    assert parse_verdict(
        '{"noteworthy": true, "category": "unusual", "description": '
        '"A dog is standing on the floor near the counter, looking '
        'towards the staff member."}') is None
    # Prose fallback path too.
    assert parse_verdict("A cat is sitting on the counter.") is None


def test_animal_words_do_not_match_inside_other_words():
    # "catalogue" contains cat, "crate" contains rat. Word boundaries
    # keep real findings alive.
    assert parse_verdict(
        '{"noteworthy": true, "category": "unusual", "description": '
        '"Catalogues are stacked on a crate blocking the fire exit."}'
    ) == ("scene_review",
          "Catalogues are stacked on a crate blocking the fire exit.",
          "unusual")
