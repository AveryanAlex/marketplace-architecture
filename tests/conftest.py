import os

import pytest


def pytest_collection_modifyitems(items):
    if os.getenv("RUN_INTEGRATION") != "1":
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="run make test for the real Compose stack"))
