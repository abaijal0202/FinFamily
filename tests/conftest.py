import os
import tempfile

# Must be set before app/config are imported anywhere.
_tmpdir = tempfile.mkdtemp(prefix="finfamily_test_")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmpdir, "test.db").replace("\\", "/")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["UPLOAD_FOLDER"] = os.path.join(_tmpdir, "uploads")

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from models import db, Family, User, ROLE_OWNER  # noqa: E402


@pytest.fixture()
def app():
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def family_and_user(app):
    family = Family(name="Test Family")
    db.session.add(family)
    db.session.flush()
    user = User(name="Tester", email="tester@example.com", role=ROLE_OWNER,
                family_id=family.id)
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()
    return family, user
