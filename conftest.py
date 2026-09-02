import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import pytest

from rbh_hedge_var import http_util


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: allow real outbound HTTP (opt-in; unit tests are blocked by default)",
    )


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Hard-block all outbound HTTP in unit tests.

    Unit tests must be hermetic: no test should ever be able to touch a
    production API. Any accidental real network call (e.g. a helper like
    ``_safe_book()`` reaching the live order book) raises immediately instead
    of silently returning live data and making assertions flaky.

    Opt out with ``@pytest.mark.network`` for tests that genuinely need it.
    """
    if request.node.get_closest_marker("network"):
        return

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "network disabled in unit tests (use @pytest.mark.network to opt in)"
        )

    monkeypatch.setattr(http_util, "get_json", _blocked)
    monkeypatch.setattr(http_util, "request_json", _blocked)
    monkeypatch.setattr(http_util, "post_json", _blocked)
