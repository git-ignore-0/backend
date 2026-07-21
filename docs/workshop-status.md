# Workshop status contract

The Django content backend is the source of truth for workshop status. Clients must display the
API value and must not derive status from the browser clock.

## Status boundaries

All comparisons use timezone-aware instants:

- `upcoming`: `now < start_at`;
- `ongoing`: `start_at <= now < end_at`;
- `completed`: `now >= end_at`.

`start_at` and `end_at` are stored as absolute instants. `event_timezone` is a valid IANA timezone,
such as `Asia/Ho_Chi_Minh`, and is retained for editor input and localized display. It does not
change status comparison semantics.

The public list keeps the existing `period=upcoming|past` query contract. The `upcoming` period
contains both `upcoming` and `ongoing` workshops; the `past` period contains `completed` workshops.
List filtering and serialization use the same captured request time so an item cannot cross a
status boundary between those operations.

## Client behavior

- The public website renders the localized status on workshop cards and detail pages.
- Registration is available only when status is `upcoming` and a registration URL exists.
- The admin workshop list renders the same three status values from the admin API.
- Schema.org has no in-progress event status, so public JSON-LD maps `ongoing` to
  `EventScheduled` and maps `completed` to `EventCompleted`.

Public workshop fetches can be cached for up to five minutes in production. A time-based status
transition may therefore appear after that cache window unless content revalidation happens for
another reason.

## Deployment and verification

Deploy the public and admin frontends before or together with the backend so every consumer knows
the `ongoing` value before the API can emit it. No database migration is required.

Verify the API with workshops whose schedules cover all three boundaries, then check the public
card/detail labels and the admin status column. Backend regression coverage lives in
`tests/test_content_api.py`; public rendering coverage lives in the workshop card and detail tests.
