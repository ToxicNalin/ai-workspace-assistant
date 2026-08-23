import uuid

from app.utils.ids import uuid7


def test_uuid7_has_correct_version_and_variant() -> None:
    value = uuid7()

    assert isinstance(value, uuid.UUID)
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_values_are_unique() -> None:
    values = {uuid7() for _ in range(1000)}
    assert len(values) == 1000


def test_uuid7_timestamp_is_monotonic_non_decreasing() -> None:
    first = uuid7()
    second = uuid7()

    first_ts = first.int >> 80
    second_ts = second.int >> 80

    assert second_ts >= first_ts
