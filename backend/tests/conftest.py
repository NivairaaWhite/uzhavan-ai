import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolated SQLite file per test session so tests never touch a dev DB.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ["JWT_SECRET_KEY"] = "test-secret"

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: E402,F401
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def make_user(client):
    """Creates a user of any role *directly in the DB* (admins can't
    self-register through the API) and returns ready-to-use auth headers."""
    from app.auth import hash_password, normalize_phone
    from app.models import User, UserRole

    def _make(phone, role="farmer", district=None, password="secret123"):
        with SessionLocal() as db:
            db.add(User(
                full_name="Test User", phone_number=normalize_phone(phone),
                hashed_password=hash_password(password), role=UserRole(role),
                preferred_language="en", district=district,
            ))
            db.commit()
        token = client.post("/auth/login", data={"username": phone, "password": password}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _make
