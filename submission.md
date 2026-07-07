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

## Bug Reproduction Notes

_(Milestone 2 — reproduced all five before writing any fix code. Full root cause analysis entries with fix descriptions and side-effect checks to follow per issue.)_

### Issue #1 — Listening streak resets (streak_service.py)

**How reproduced:** Directly called `update_listening_streak(user, now)` (`services/streak_service.py:42`) with a controlled `now`, rather than depending on the real system clock landing on a Sunday. Set a seeded user's `listening_streak = 5` and `last_listened_at` = Saturday 2026-07-04 12:00 UTC, then called the function with `now` = Sunday 2026-07-05 12:00 UTC (one calendar day later — a normal consecutive-day listen).

**Result:** Streak dropped to `1` instead of incrementing to `6`.

**Suspected mechanism:** `services/streak_service.py:73` — `elif days_since_last == 1 and today.weekday() != 6:` — Python's `date.weekday()` returns `6` for Sunday, so the increment branch is explicitly skipped whenever the listen happens on a Sunday, even though `days_since_last == 1` (a legitimate consecutive day). Falls through to the `else` branch, which resets to `1`.

**Side effect of the repro itself:** the test script committed the Saturday setup state to the real seeded DB before I rolled back the final step. Re-ran `python seed_data.py` afterward to restore clean fixture state (this also means all previously-noted UUIDs from earlier exploration are now stale).

### Issue #2 — Friends Listening Now shows stale entries (feed_service.py)

**How reproduced:** `GET /feed/<darius_id>/listening-now`. Darius is friends with nova, simone, kenji. Nova has no "recent" (< 30 min) listening event in the seed data — her only event is from the "older events" seed block, ~2 hours before request time.

**Result:** Nova still appeared in the response with `"listened_at": "2026-07-07T17:09:15"` while the request happened around `19:15` — roughly 2 hours stale, clearly not "listening now."

**Suspected mechanism:** `services/feed_service.py:13` — `RECENT_THRESHOLD = timedelta(hours=24)`. Anything within a full rolling day counts as "now." The seed data's own comments (`seed_data.py:111`, "Recent events (within the past 30 minutes) — should appear in listening now") imply the intended window is far shorter than 24 hours.

**Note:** darius/simone/kenji all also have genuinely-recent events that mask the bug via the dedup-to-most-recent-event-per-friend logic (`feed_service.py:48-60`) — had to specifically pick a friend (nova) whose only event was in the "older" bucket to see the stale entry surface.

### Issue #3 — Duplicate songs in search (search_service.py) — inconclusive, defensive fix planned

**Docs say:** `instructions.txt:43` calls the bug "conditional" and mentions a "second code path." `seed_data.py:73-80`'s comment claims songs with 3+ tags "expose Issue #3." `tests/test_search.py:104`'s inline comment says "Should be 1, bug causes it to be 3" for a 3-tag song — but that test currently passes against the unmodified code.

**What I tried:**
1. Called `search_songs("Crown Heights")` against the 3-tag seed song "Crown Heights Anthem" directly — got 1 result, not 3.
2. Ran the query with `SQLALCHEMY_ECHO=True` — confirmed the `LEFT OUTER JOIN song_tags` in `search_service.py:27` really does fan out to multiple raw SQL rows (one per tag), and that a *second* query is issued separately for `Song.tags` eager loading (`models.py:90`, `lazy="subquery"`) — but that second query only loads tag data for songs that already survived dedup, so it can't produce duplicate entries.
3. Ran broad queries matching 11-13 songs at once and checked for any duplicate IDs in the result set — none, at any scale.
4. Manually inserted a second, genuinely distinct `Song` row with the same title/artist as an existing song, and confirmed *that* does produce two visually-duplicate entries in search results — but there is no route in this app (`routes/songs.py` has no `POST /songs`) that would let a real user action create that state. Deleted the test row immediately after to avoid polluting seed data.

**Conclusion:** `db.session.query(Song)...all()` (SQLAlchemy's legacy `Query` API) automatically de-duplicates full-entity results by primary key, which silently absorbs the row fan-out from the join before it reaches the return value. Confirmed the installed SQLAlchemy version (2.0.51) satisfies `requirements.txt`'s `sqlalchemy>=2.0.0`, so this isn't an environment mismatch — the join-fan-out bug as literally described does not appear to be reproducible through `search_songs()` in this codebase as currently written.

**Decision:** the join is still objectively unnecessary/wrong — nothing in the `SELECT` uses tag columns from it, it exists purely to let the `WHERE` clause reach `song_tags`, and it fans out rows for no benefit. Planned fix: add `.distinct()` (or drop the unneeded join) as a defensive correctness fix regardless of whether today's SQLAlchemy version happens to mask the symptom. Will document this reasoning in the fix's root cause analysis entry rather than claim a clean reproduction.

### Issue #4 — Missing notification on song rating (notification_service.py)

**How reproduced:** Checked nova's notifications (`GET /users/<nova_id>/notifications`) — 1 existing notification (the seeded `song_added_to_playlist` one). Had darius (a friend, not the sharer) rate nova's song "Midnight Drive" via `POST /songs/<song_id>/rate` with `{"user_id": darius_id, "score": 5}`. Re-checked nova's notifications.

**Result:** Rating succeeded (`Rating` row created, 201 response), but notification count stayed at 1 — no new notification was created for the rating.

**Suspected mechanism:** `services/notification_service.py::rate_song()` (lines 73-110) never calls `create_notification()`, unlike `add_to_playlist()` (lines 35-70), which explicitly does at line 66-70. The working playlist-add path is the template; the rating path was never wired up to it.

### Issue #5 — Last playlist song missing (playlist_service.py)

**How reproduced:** Queried the DB directly for the "Late Night Vibes" playlist's `playlist_entries` rows — confirmed 7 songs at positions 1 through 7. Then called `GET /playlists/<playlist_id>/songs`.

**Result:** Only 6 songs returned; the song at position 7 was missing.

**Suspected mechanism:** `services/playlist_service.py:66` — `return [song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice unconditionally drops the last element of the ordered-by-position list, regardless of playlist length.

---

## Bug Fixes

### Issue #1 — Listening streak resets on Sundays (`services/streak_service.py`)

**Reproduction steps:** Directly called `update_listening_streak(user, now)` with a controlled `now` rather than waiting for the real clock to land on a Sunday. Set a user's `listening_streak = 5` and `last_listened_at` to Saturday 2026-07-04 12:00 UTC, then called the function with `now` = Sunday 2026-07-05 12:00 UTC — a normal, legitimate consecutive-day listen. Streak dropped to `1` instead of incrementing to `6`.

**Navigation strategy:** Started from the streak rules documented in the function's own docstring (`streak_service.py:46-50`): same day → no change, one day later → increment, more than one day → reset. Read the `if/elif/else` at lines 70-76 against those three stated rules and noticed the `elif` branch had a condition the docstring never mentioned — `and today.weekday() != 6`. That extra clause doesn't correspond to any of the three documented rules, which made it the obvious next thing to isolate. Confirmed with the controlled repro above that this exact clause was the trigger, rather than guessing from the code alone.

**Root cause explanation:** Line 73 reads `elif days_since_last == 1 and today.weekday() != 6:`. Python's `date.weekday()` returns `6` for Sunday, so whenever a user's consecutive-day listen happens to fall on a Sunday, `today.weekday() != 6` evaluates to `False`, the `elif` as a whole is `False`, and execution falls through to the `else` branch — which is supposed to be reserved for multi-day gaps — resetting the streak to `1` even though only one day actually passed. The correct behavior only depends on `days_since_last`; day-of-week is irrelevant to whether two listens are on consecutive calendar days, so the `and today.weekday() != 6` clause has no valid basis in the streak rules at all.

**Fix description:** Removed the `and today.weekday() != 6` clause, leaving `elif days_since_last == 1:` so the increment branch fires for any consecutive-day listen regardless of which weekday it lands on, matching the documented rule exactly.

**Side-effect check:** Re-ran the same controlled test for the other two branches to confirm they were untouched: (1) `days_since_last == 0` (same-day listen) still leaves the streak unchanged, and (2) a multi-day gap still resets the streak to `1`. Also re-ran the fixed increment path on a normal (non-Sunday) consecutive day to confirm the increment still works outside the buggy condition. All three behaved identically to before the fix — only the erroneous Sunday exception was removed, no other branch's logic changed.

**Regression test:** `tests/test_streaks.py::test_streak_increments_on_sunday` (lines 83-96) already existed in the repo and covers exactly this bug — it listens on a Saturday then a Sunday and asserts the streak increments to `2`. I confirmed by temporarily reverting `streak_service.py` to the pre-fix version that this test fails against the buggy code (`assert 1 == 2`, since the Sunday listen incorrectly reset the streak instead of incrementing it), and passes against the fix. No new test was needed — this one was already in place and just needed the underlying bug fixed to go green.

### Issue #2 — Friends Listening Now shows stale entries (`services/feed_service.py`)

**Reproduction steps:** Called `GET /feed/<darius_id>/listening-now`. Darius is friends with nova, simone, and kenji in the seed data. Nova's only `ListeningEvent` is from the "older events" seed block (~2 hours before request time), while simone and kenji each also have a genuinely recent (<30 min) event. Nova still appeared in the response with a `listened_at` roughly 2 hours stale — clearly not "listening now."

**Navigation strategy:** Started from the route (`routes/feed.py`) and followed the single call into `feed_service.get_friends_listening_now()`. The function's own docstring says it returns friends "who have listened to something recently," so the natural place to look was the cutoff calculation at line 32 (`cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD`) and its definition at line 13. Cross-checked that value against `seed_data.py:111`'s comment — "Recent events (within the past 30 minutes) — should appear in listening now" — which is the seed data's own stated intent for what counts as "now." The 24-hour constant was inconsistent with that 30-minute intent by two orders of magnitude, which is what made this the confirmed root cause rather than a guess. I also had to notice the dedup-to-most-recent-event-per-friend logic (lines 48-60) before I could reproduce the bug: darius's other two friends both have a masking recent event, so I had to specifically pick nova (whose only event was in the "older" bucket) to see the stale entry surface at all.

**Root cause explanation:** `RECENT_THRESHOLD = timedelta(hours=24)` (line 13) defines "listening now" as anything within a full rolling day. Any friend with even one `ListeningEvent` in the last 24 hours passes the `listened_at >= cutoff` filter (line 42), regardless of how many hours ago that actually was — so an event from 2, 10, or 23 hours ago is treated identically to one from 30 seconds ago. The seed data and feature name both imply "now" should mean genuinely current, not "sometime today," so the correct behavior requires a much narrower window that actually reflects live listening activity, not just same-day activity.

**Fix description:** Changed `RECENT_THRESHOLD` from `timedelta(hours=24)` to `timedelta(minutes=30)`, matching the window the seed data was built around.

**Side-effect check:** Checked `get_activity_feed()` (lines 65-105) in the same file — it deliberately does not use `RECENT_THRESHOLD` at all (it's capped by `limit` instead, per its own docstring: "not filtered by recency"), so changing the constant has no effect on that function. Also reran the two friends (simone, kenji) who have genuinely recent events to confirm they still appear correctly under the new 30-minute window — the dedup-per-friend logic and the friend/song lookups were untouched by this change, only the recency filter's width changed.

**Regression test:** No existing test file covered the feed service, so I added `tests/test_feed.py` with three tests: `test_listening_now_includes_song_from_a_few_minutes_ago`, `test_listening_now_excludes_song_from_yesterday`, and `test_listening_now_empty_for_no_friends`. The second one is the direct regression test for this bug — it creates a friend whose only listening event is 2 hours old and asserts they do NOT appear in the "listening now" feed. I confirmed this test fails against the pre-fix 24-hour threshold (`assert ... not in [...]` fails because the stale friend is still included) and passes against the 30-minute fix.

### Issue #3 — Duplicate songs in search (`services/search_service.py`)

**Reproduction steps:** This one didn't reproduce cleanly, and I'm documenting that honestly rather than claiming a clean repro. `instructions.txt:43` calls the bug "conditional" and `seed_data.py:73-80` claims songs with 3+ tags "expose" it, so I called `search_songs("Crown Heights")` against the seeded 3-tag song "Crown Heights Anthem" — it returned 1 result, not 3. I then ran the query with `SQLALCHEMY_ECHO=True`, ran broad queries across 11-13 songs checking for duplicate IDs at scale, and manually inserted a genuinely duplicate `Song` row to confirm what a *visible* duplicate would even look like (then deleted it immediately). None of these reproduced the literal "song appears 3 times" symptom the docs describe.

**Navigation strategy:** Traced `search_songs()` (`search_service.py:25-35`) line by line. The `SQLALCHEMY_ECHO` output showed two things: (1) the main query's `.outerjoin(song_tags, Song.id == song_tags.c.song_id)` at line 27 does fan out to one raw SQL row per tag at the database level, and (2) `db.session.query(Song)...all()` — SQLAlchemy's legacy `Query` API — automatically deduplicates full-entity results by primary key before they're ever returned to the caller. That auto-dedup is what silently absorbs the fan-out. I confirmed this wasn't an environment/version mismatch (installed SQLAlchemy 2.0.51 satisfies `requirements.txt`'s `>=2.0.0`) before concluding the bug doesn't manifest as literally described through this function today.

**Root cause explanation:** The `.outerjoin(song_tags, Song.id == song_tags.c.song_id)` at line 27 joins purely to let a `WHERE` clause reach `song_tags` — except the actual `.filter()` immediately after only tests `Song.title` and `Song.artist` (lines 28-32). The join contributes nothing to filtering and exists for no reason in the current code, yet it still multiplies the raw row count by the number of tags a matching song has. That row-multiplication is real and is a latent correctness bug even though `Query.all()`'s primary-key dedup happens to absorb it in this codebase's current form (e.g., it would resurface immediately if this were ever rewritten with SQLAlchemy 2.0-style `session.execute(select(Song)...)`, which does not auto-dedup the same way).

**Fix description:** Removed the unnecessary `.outerjoin(song_tags, ...)` call (and the now-unused `Tag`/`song_tags` imports) from `search_songs()` entirely, rather than papering over the fan-out with `.distinct()`. Since the join was never used for filtering, dropping it is a stricter fix than de-duplicating after the fact — it also improves query cost, since the database is no longer asked to produce and then discard the extra joined rows.

**Side-effect check:** Verified `Song.to_dict()` still returns a fully-populated `tags` list after the fix — tag data comes from the separate `Song.tags` relationship (`models.py:90`, `lazy="subquery"`), which issues its own independent query and was never dependent on the join I removed. Confirmed this by inspecting the actual executed SQL: the main search query now runs as a plain `SELECT ... FROM song WHERE ...` with no `song_tags` reference, while a second, legitimate query still joins `song_tags`/`tag` to populate each song's tags. Also reran all pre-existing `test_search.py` tests (matching, empty-query, and the three "no duplicates" tests) — all still pass.

**Regression test:** Added `tests/test_search.py::test_search_does_not_join_song_tags`. Since the existing `test_search_no_duplicates_multi_tag_song` test passes even against the buggy code (it only checks the final deduplicated Python list, which `Query.all()`'s primary-key dedup already cleans up), it would not have caught this bug. The new test instead listens for the actual SQL statements executed via a SQLAlchemy `before_cursor_execute` event and asserts the main Song-searching query contains no reference to `song_tags`. I confirmed this test fails against the pre-fix code (`assert 'song_tags' not in ...` fails, showing the `LEFT OUTER JOIN song_tags` in the executed SQL) and passes against the fix — it's testing the actual root cause (the unnecessary join) rather than a symptom that happens to be masked today.

## AI Usage

_(to follow)_
