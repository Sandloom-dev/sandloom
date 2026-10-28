"""Repeated latency summaries retain spread and do not select a best window."""

from qualification.outbox_latency_ab import repeated_summary


def rows(before=(100, 100, 1000), after=(110, 120, 130)):
    return [
        {
            "rate": 200,
            "variant": v,
            "round": rnd,
            "p99_ms": p99,
            "goodput_rps": 199.9,
            "max_pending": 10,
            "errors": 0,
        }
        for v, values in [("before", before), ("after", after)]
        for rnd, p99 in enumerate(values, 1)
    ]


def test_reports_medians_instead_of_a_single_best_or_worst_window():
    result = repeated_summary(rows())[0]
    assert result["before"]["p99_ms_median"] == 100
    assert result["after"]["p99_ms_median"] == 120
    assert result["median_p99_ratio_after_before"] == 1.2
    assert result["before"]["p99_ms_min"] == 100
    assert result["before"]["p99_ms_max"] == 1000
    assert result["before"]["p99_ms_windows"] == [100, 100, 1000]
    assert result["median_paired_p99_ratio"] == 1.1
    assert [p["round"] for p in result["paired_p99"]] == [1, 2, 3]


def test_preserves_errors_instead_of_hiding_a_bad_window():
    records = rows()
    records[0]["errors"] = 1
    result = repeated_summary(records)[0]
    assert result["before"]["errors"] == 1
    assert result["before"]["windows"] == 3
