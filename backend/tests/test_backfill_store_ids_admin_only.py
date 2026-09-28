"""POST /analytics/admin/backfill-store-ids is admin-only."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import analytics
from app.database import get_db
from app.deps import get_current_user

URL = "/analytics/admin/backfill-store-ids"


class _Result:
    def fetchall(self):
        return [(1,), (2,)]


class _DB:
    def __init__(self):
        self.executed = 0

    def execute(self, *_a, **_k):
        self.executed += 1
        return _Result()

    def commit(self):
        pass


def _client(role: str, db: _DB) -> TestClient:
    app = FastAPI()
    app.include_router(analytics.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = (
        lambda: SimpleNamespace(id=1, role=role, is_active=True))
    return TestClient(app)


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_non_admin_is_refused_and_nothing_runs(role):
    db = _DB()
    r = _client(role, db).post(URL)
    assert r.status_code == 403
    assert db.executed == 0


def test_admin_can_run_backfill():
    db = _DB()
    r = _client("admin", db).post(URL)
    assert r.status_code == 200
    assert r.json() == {"backfilled_rows": 2}
    assert db.executed == 1
