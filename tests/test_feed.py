"""
tests/test_feed.py — Mixtape

Tests for the "Friends Listening Now" feed logic.
"""

import pytest
from datetime import datetime, timedelta, timezone
from app import create_app, db
from models import User, Song, ListeningEvent, friendships
from services.feed_service import get_friends_listening_now


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def friends(app):
    """Create a user with one friend, plus a song the friend can listen to."""
    with app.app_context():
        me = User(username="me", email="me@example.com")
        friend = User(username="friend", email="friend@example.com")
        db.session.add_all([me, friend])
        db.session.flush()

        db.session.execute(friendships.insert().values(user_id=me.id, friend_id=friend.id))
        db.session.execute(friendships.insert().values(user_id=friend.id, friend_id=me.id))

        song = Song(title="Test Song", artist="Test Artist", shared_by=me.id)
        db.session.add(song)
        db.session.commit()

        yield {"me": me, "friend": friend, "song": song}


def test_listening_now_includes_song_from_a_few_minutes_ago(app, friends):
    """A friend who listened a few minutes ago should show up as listening now."""
    with app.app_context():
        event = ListeningEvent(
            user_id=friends["friend"].id,
            song_id=friends["song"].id,
            listened_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db.session.add(event)
        db.session.commit()

        results = get_friends_listening_now(friends["me"].id)
        friend_ids = [r["friend"]["id"] for r in results]
        assert friends["friend"].id in friend_ids


def test_listening_now_excludes_song_from_yesterday(app, friends):
    """
    A friend whose only listening event was ~2 hours ago (well outside the
    "listening now" window) should NOT show up in the feed.
    """
    with app.app_context():
        event = ListeningEvent(
            user_id=friends["friend"].id,
            song_id=friends["song"].id,
            listened_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db.session.add(event)
        db.session.commit()

        results = get_friends_listening_now(friends["me"].id)
        friend_ids = [r["friend"]["id"] for r in results]
        assert friends["friend"].id not in friend_ids


def test_listening_now_empty_for_no_friends(app):
    """A user with no friends gets an empty feed, not an error."""
    with app.app_context():
        me = User(username="loner", email="loner@example.com")
        db.session.add(me)
        db.session.commit()

        assert get_friends_listening_now(me.id) == []
