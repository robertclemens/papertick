"""A new user has to come out of signup able to open an account.

Scenarios arrived after accounts did, and for a while the only thing that
created one was a backfill at backend startup — so anyone who signed up between
two restarts had no scenario, and `POST /accounts` wrote a null scenario_id and
failed. Signup owns this now; these are the regression tests.
"""

from datetime import date

from fastapi import Response

from app.models import Scenario, User
from app.routers import auth as auth_router
from app.schemas import SignupIn
from app.services import scenarios


def _signup(db, email="new@example.com"):
    auth_router.signup(
        SignupIn(email=email, password="CorrectHorse99", date_of_birth=date(1990, 1, 2)),
        Response(),
        db,
    )
    return db.query(User).filter_by(email=email).one()


def test_signup_gives_the_new_user_a_scenario(db):
    user = _signup(db)
    tracks = db.query(Scenario).filter_by(user_id=user.id).all()
    assert len(tracks) == 1
    assert user.default_scenario_id == tracks[0].id


def test_ensure_default_adopts_an_existing_scenario_instead_of_adding_one(db, user):
    existing = Scenario(user_id=user.id, name="Only one", sort_order=0)
    db.add(existing)
    db.flush()

    assert scenarios.ensure_default(db, user).id == existing.id
    assert scenarios.ensure_default(db, user).id == existing.id      # idempotent
    assert db.query(Scenario).filter_by(user_id=user.id).count() == 1
    assert user.default_scenario_id == existing.id


def test_ensure_default_ignores_a_deleted_scenario(db, user):
    """A deleted track is frozen, so it cannot be the one a new account lands in."""
    from app.models import utcnow

    gone = Scenario(user_id=user.id, name="Deleted", sort_order=0, deleted_at=utcnow())
    db.add(gone)
    db.flush()

    fresh = scenarios.ensure_default(db, user)
    assert fresh.id != gone.id
    assert fresh.deleted_at is None
    assert user.default_scenario_id == fresh.id
