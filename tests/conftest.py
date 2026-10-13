from __future__ import annotations

import os
import random
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from hub.api.app import create_app
from hub.clock import FrozenClock
from hub.config import Settings
from hub.db import reset_db
from hub.engine.ratelimit import TokenBucket
from hub.engine.runner import Runner
from hub.runtime import Runtime
from hub.seed import seed as seed_data
from hub.simulate import Simulator

TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/hub_test"
)
START = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        database_url=TEST_DB,
        webhook_secret="test-secret",
        webhook_secret_esign="esign-secret",
        slack_mode="sandbox",
        email_mode="sandbox",
        worker_id="test-worker",
        public_base_url="http://hub.example.com",
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(START)


@pytest.fixture
def rt(settings: Settings, clock: FrozenClock) -> Runtime:
    """A fresh database and runtime per test. Rate limiters are opened up so nothing sleeps."""
    reset_db(settings.database_url)
    runtime = Runtime.build(settings, clock)
    for adapter in runtime.adapters.by_system.values():
        adapter.bucket = TokenBucket(100_000, 100_000)
    return runtime


@pytest.fixture
def seeded(rt: Runtime) -> Runtime:
    with rt.session_factory() as session:
        seed_data(session, rt.adapters.store)
        session.commit()
    return rt


@pytest.fixture
def runner(rt: Runtime) -> Runner:
    return Runner(
        rt.session_factory, rt.catalog, rt.adapters, rt.settings, rt.clock, random.Random(1)
    )


@pytest.fixture
def sim(rt: Runtime) -> Simulator:
    return Simulator(rt, None, seed=7)


@pytest.fixture
def client(rt: Runtime) -> TestClient:
    return TestClient(create_app(rt))
