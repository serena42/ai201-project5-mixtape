# Mixtape Bug Hunt — Submission

## Codebase Map

### Main files and their roles

- **`app.py`** — Flask application factory (`create_app`). Configures the SQLAlchemy DB URI (defaults to `sqlite:///mixtape.db`, stored under `instance/`), initializes the `db` extension, registers the four blueprints (`songs`, `playlists`, `users`, `feed`) under their URL prefixes, and calls `db.create_all()`. There is no `/` route — every real endpoint lives under `/songs`, `/playlists`, `/users`, or `/feed`.

- **`models.py`** — All SQLAlchemy models plus three association tables:
  - `User` — has `listening_streak` (int) and `last_listened_at` (datetime), a self-referential many-to-many `friends` relationship (via the `friendships` table, inserted bidirectionally), and one-to-many relationships to `Song` (songs they shared), `Rating`, `ListeningEvent`, `Notification`, `Playlist`.
  - `Song` — belongs to the user who shared it (`shared_by`), has a many-to-many `tags` relationship via `song_tags`.
  - `Tag` — just a name; joined to `Song` via `song_tags`.
  - `ListeningEvent` — a timestamped record that a user listened to a song. This is the raw data both the streak logic and the "listening now" feed are built from.
  - `Rating` — one row per (user, song) pair, enforced by a `UniqueConstraint`; re-rating updates the existing row rather than creating a new one.
  - `Playlist` — many-to-many to `Song` via `playlist_entries`, which (unlike `song_tags`) carries extra columns: `position` (explicit ordering), `added_by`, `added_at`. So playlist song order is NOT insertion order — it's the explicit `position` column.
  - `Notification` — a flat table with a `notification_type` string (e.g. `"song_added_to_playlist"`), a pre-rendered `body` string, and a `read` boolean. There's no polymorphism — every notification type is just a row with a different `notification_type` value and a differently-formatted body.

- **`routes/`** — one blueprint per resource (`songs.py`, `playlists.py`, `users.py`, `feed.py`). Every route does the same three things: parse the request (query args or JSON body), call exactly one service function, and translate the result (or a caught `ValueError`) into a JSON response with the right status code. No business logic lives in routes.

- **`services/`** — where all business logic and DB querying actually happens:
  - `streak_service.py` — `record_listening_event()` creates a `ListeningEvent` and calls `update_listening_streak()`, which compares `now.date()` against the user's `last_listened_at.date()` to decide whether to increment, reset, or leave the streak unchanged.
  - `feed_service.py` — `get_friends_listening_now()` finds the current user's friends, then queries `ListeningEvent` rows for those friends newer than a `RECENT_THRESHOLD` cutoff, deduplicated to the single most recent event per friend. `get_activity_feed()` is the same query without the recency filter, just capped by `limit`.
  - `search_service.py` — `search_songs()` does a case-insensitive `ilike` match on `title`/`artist`, joined against `song_tags` so tag data can be included in the result.
  - `notification_service.py` — `create_notification()` is the generic constructor used by every other notification-producing function. `add_to_playlist()` adds a song to a playlist's `songs` relationship and then calls `create_notification()` to tell the original sharer. `rate_song()` upserts a `Rating` row (update-in-place if the user already rated that song, due to the unique constraint).
  - `playlist_service.py` — `create_playlist()`, `get_playlist()` (metadata only), `get_playlist_songs()` (joins through `playlist_entries`, ordered by `position`), `get_user_playlists()`.

- **`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py`. Existing coverage per service, presumably written against intended (not necessarily current) behavior.

- **`seed_data.py`** — populates 5 users with pre-set friendships and streak values, 13 songs split deliberately into 0-tag / 1-tag / 3+-tag groups, 3 playlists, listening events split into "recent" (within 30 min, for the listening-now feed) and "older" (1–14 days back), and one pre-existing `song_added_to_playlist` notification. The comments in this file are strong hints about which conditions each bug needs to reproduce (e.g. multi-tag songs for search, the recent/old event split for the feed).

### Data flow — user adds a song to a playlist (triggers a notification)

1. Client sends `POST /playlists/<playlist_id>/songs` with `{song_id, added_by}` in the body.
2. `routes/playlists.py::add_song()` parses the body, checks both fields are present, and calls `notification_service.add_to_playlist(playlist_id, song_id, added_by)`.
3. Inside `add_to_playlist()`:
   - Loads the `Song`, the adding `User`, and the `Playlist` by ID, raising `ValueError` (→ 400 in the route) if any is missing.
   - If the song isn't already in `playlist.songs`, appends it and commits. (Note: this only adds it to the `playlist_entries` association — nothing here sets `position` explicitly beyond whatever `db.relationship.append` does by default.)
   - If the song's original sharer (`song.shared_by`) is not the same person who just added it, calls `create_notification()` with `notification_type="song_added_to_playlist"` and a pre-formatted body naming the adder, the song, and the playlist.
4. `create_notification()` just constructs and commits a `Notification` row for `song.shared_by`.
5. The route returns `{"message": "Song added to playlist"}, 201`.
6. Later, the sharer fetches `GET /users/<id>/notifications`, which calls `notification_service.get_notifications()` — a straight query on `Notification` filtered by `user_id` (and `read` if `unread_only=true`), ordered newest-first.

### Patterns noticed

- **Routes never touch the DB directly.** Every route's job is request parsing + one service call + response shaping. All querying, all business rules, all commits happen in `services/`. If behavior is wrong, the fix is virtually always in a service function, not a route.
- **`ValueError` is the app's error-signaling convention.** Services raise `ValueError` for "not found" or invalid-input conditions; routes catch it and turn it into a 404 or 400 JSON response. There's no custom exception hierarchy.
- **Association tables carry different amounts of metadata.** `song_tags` and `friendships` are pure link tables (just the two foreign keys), but `playlist_entries` also carries `position`, `added_by`, and `added_at` — meaning playlist song order is a stored, explicit value that has to be queried and sorted deliberately, not inferred from row order.
- **Notifications are not polymorphic.** Each notification-producing action (so far, just playlist-add) calls the same generic `create_notification()` helper with its own `notification_type` and hand-formatted `body` string. Any new notification-producing action needs to explicitly add its own call to that helper — there's no shared trigger or event system that fires notifications automatically off of other actions (like rating a song).
- **`to_dict()` is the uniform serialization boundary.** Every model defines its own `to_dict()`, and services always return the result of calling it (or a list of them) rather than raw model instances — routes just `jsonify()` whatever the service handed back.

---

## Bug Fixes

_(root cause analysis entries to follow — one per fixed issue)_

## AI Usage

_(to follow)_
