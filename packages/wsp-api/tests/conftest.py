from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from wsp_api import ApiSettings, Services, create_app
from wsp_api.testing import local_client
from wsp_api.testing.fakes import sample_services


@pytest.fixture
def services() -> Services:
    return sample_services()


@pytest.fixture
def app(services: Services) -> FastAPI:
    return create_app(services, ApiSettings(sse_poll_seconds=0.01, sse_heartbeat_seconds=0.02))


@pytest.fixture
def client(app: FastAPI) -> Iterator[httpx.Client]:
    with local_client(app) as c:
        yield c
