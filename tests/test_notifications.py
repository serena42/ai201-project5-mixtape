"""
tests/test_notifications.py — Mixtape

Tests for notification creation logic.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, add_to_playlist, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def sharer_and_rater(app):
    """A song shared by one user, to be rated by a different user."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Test Song", artist="Test Artist", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_a_song_notifies_the_sharer(app, sharer_and_rater):
    """Rating someone else's song should create a notification for the sharer."""
    with app.app_context():
        sharer = sharer_and_rater["sharer"]
        rater = sharer_and_rater["rater"]
        song = sharer_and_rater["song"]

        rate_song(rater.id, song.id, 5)

        notifications = get_notifications(sharer.id)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "song_rated"


def test_rating_your_own_song_does_not_notify_yourself(app, sharer_and_rater):
    """Rating your own shared song should not create a self-notification."""
    with app.app_context():
        sharer = sharer_and_rater["sharer"]
        song = sharer_and_rater["song"]

        rate_song(sharer.id, song.id, 4)

        notifications = get_notifications(sharer.id)
        assert notifications == []


def test_re_rating_a_song_still_notifies_the_sharer(app, sharer_and_rater):
    """Updating an existing rating should notify the sharer again."""
    with app.app_context():
        sharer = sharer_and_rater["sharer"]
        rater = sharer_and_rater["rater"]
        song = sharer_and_rater["song"]

        rate_song(rater.id, song.id, 3)
        rate_song(rater.id, song.id, 5)

        notifications = get_notifications(sharer.id)
        assert len(notifications) == 2
