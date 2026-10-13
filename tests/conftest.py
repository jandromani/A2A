import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    database = tmp_path / "platform.db"
    config = Settings(
        _env_file=None,
        environment="test",
        auth_mode="dev",
        database_url=f"sqlite:///{database}",
    )
    with TestClient(create_app(config)) as test_client:
        yield test_client
