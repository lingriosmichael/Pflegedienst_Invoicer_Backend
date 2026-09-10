import os

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:1/test_unused")


@pytest.fixture(autouse=True)
def isolated_unit_transactions(request, monkeypatch):
    if request.node.get_closest_marker("mongo"):
        return
    from app.db import transactions

    monkeypatch.setattr(transactions, "run_transaction", lambda callback: callback())
