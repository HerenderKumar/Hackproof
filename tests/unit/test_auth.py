"""Invitation tokens and signed sessions (section 12.4)."""

import uuid
from datetime import UTC, datetime, timedelta

from api import auth
from api.auth import Session, SessionCodec

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SECRET = "s" * 32


def team_session(**overrides: object) -> Session:
    values: dict[str, object] = {
        "role": "team",
        "subject_id": uuid.uuid4(),
        "event_id": uuid.uuid4(),
        "link_id": uuid.uuid4(),
        "expires_at": NOW + timedelta(hours=12),
    }
    return Session(**{**values, **overrides})  # type: ignore[arg-type]


def test_tokens_are_long_random_and_url_safe() -> None:
    first, second = auth.new_token(), auth.new_token()
    assert first != second
    assert len(first) >= 43  # 32 random bytes, base64url
    assert first.replace("-", "").replace("_", "").isalnum()


def test_a_token_is_stored_as_its_sha256() -> None:
    token = auth.new_token()
    stored = auth.hash_token(token)
    assert len(stored) == 64 and stored != token
    assert stored == auth.hash_token(token)


def test_a_session_survives_the_cookie_round_trip() -> None:
    codec = SessionCodec(SECRET)
    session = team_session()
    assert codec.decode(codec.encode(session), NOW) == session


def test_an_organiser_session_has_no_subject_or_link() -> None:
    codec = SessionCodec(SECRET)
    session = Session(role="organiser", expires_at=NOW + timedelta(hours=1))
    decoded = codec.decode(codec.encode(session), NOW)
    assert decoded is not None
    assert (decoded.subject_id, decoded.event_id, decoded.link_id) == (None, None, None)


def test_a_tampered_cookie_is_rejected() -> None:
    codec = SessionCodec(SECRET)
    cookie = codec.encode(team_session())
    payload, signature = cookie.rsplit(".", 1)
    forged = payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB") + "." + signature
    assert codec.decode(forged, NOW) is None


def test_a_cookie_signed_with_another_secret_is_rejected() -> None:
    cookie = SessionCodec("another-secret-" * 3).encode(team_session())
    assert SessionCodec(SECRET).decode(cookie, NOW) is None


def test_an_expired_session_is_rejected() -> None:
    codec = SessionCodec(SECRET)
    cookie = codec.encode(team_session(expires_at=NOW + timedelta(hours=12)))
    assert codec.decode(cookie, NOW + timedelta(hours=11, minutes=59)) is not None
    assert codec.decode(cookie, NOW + timedelta(hours=12)) is None


def test_garbage_is_rejected() -> None:
    codec = SessionCodec(SECRET)
    for cookie in ("", "not-a-cookie", "a.b.c"):
        assert codec.decode(cookie, NOW) is None


def test_a_signed_payload_of_the_wrong_shape_is_rejected() -> None:
    from itsdangerous import URLSafeSerializer

    cookie = URLSafeSerializer(SECRET, salt=auth.SESSION_SALT).dumps({"role": "admin"})
    assert SessionCodec(SECRET).decode(cookie, NOW) is None
