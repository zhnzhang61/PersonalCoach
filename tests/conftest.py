"""pytest config — registers --integration flag and integration marker."""


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests that make real LLM API calls.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests that hit real external APIs (deselect by default; enable with --integration)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--integration"):
        return
    import pytest as _pytest

    skip_integration = _pytest.mark.skip(reason="need --integration flag")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
