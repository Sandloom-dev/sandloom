"""Decision controls: noisy best windows and errors must not pick an image."""

from qualification.oracle_perf import python_choice


def records(official=(100, 100, 100), pbs=(104, 104, 104)):
    return [
        {
            "section": "ab",
            "variant": v,
            "route": route,
            "goodput_rps": value,
            "passed": True,
        }
        for route in ("/s5/", "/s5plan/", "/auth/me")
        for v, values in (("official", official), ("pbs", pbs))
        for value in values
    ]


def test_selects_official_below_declared_threshold():
    assert python_choice(records(pbs=(102, 102, 102)))["decision"] == "official"


def test_selects_pbs_with_consistent_material_gain():
    assert python_choice(records())["decision"] == "pbs"


def test_does_not_select_a_single_noisy_best_window():
    assert python_choice(records(pbs=(101, 102, 300)))["decision"] == "official"


def test_failed_or_missing_window_makes_decision_inconclusive():
    rows = records()
    rows[0]["passed"] = False
    assert python_choice(rows)["decision"] == "inconclusive"
    assert python_choice(records()[:-1])["decision"] == "inconclusive"
