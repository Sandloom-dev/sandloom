"""Qualification must reject overload even when queued requests all finish."""

from qualification.outbox_delivery_limit import sustainable


def healthy(**changes):
    return {
        "rate": 450,
        "goodput_rps": 449.9,
        "errors": 0,
        "monitor_error": None,
        "drain_s": 2.0,
        "pending_slope_per_s": 0.0,
        "pending_at_load_end": 0,
        "dead_delta": 0,
        "dlq_delta": 0,
    } | changes


def test_queued_requests_finishing_without_errors_do_not_prove_capacity():
    assert not sustainable(healthy(goodput_rps=330))
    assert sustainable(healthy())


def test_small_timing_noise_is_allowed():
    assert sustainable(healthy(goodput_rps=441))
    assert not sustainable(healthy(goodput_rps=440.9))


def test_bounded_delivery_and_error_free_requests_are_both_required():
    for change in (
        {"errors": 1},
        {"monitor_error": "sampler failed"},
        {"drain_s": None},
        {"pending_slope_per_s": 10},
        {"pending_at_load_end": 901},
        {"dead_delta": 1},
        {"dlq_delta": 1},
    ):
        assert not sustainable(healthy(**change))
