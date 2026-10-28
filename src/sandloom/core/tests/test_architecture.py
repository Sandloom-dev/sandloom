def test_architecture_checker_runs_without_violations() -> None:
    # Execute the checker script and assert exit code 0
    import runpy
    from pathlib import Path

    # ../../../../ -> backend
    script = (
        Path(__file__).resolve().parents[4]
        / "tools"
        / "checks"
        / "check_architecture.py"
    )
    # runpy.run_path raises SystemExit with the exit code
    try:
        runpy.run_path(str(script))
    except SystemExit as exc:
        code = int(getattr(exc, "code", 1) or 0)
        assert code == 0, "Architecture checker reported violations"
    except Exception as exc:  # pragma: no cover
        raise AssertionError(f"Failed to run architecture checker: {exc}") from exc
