"""Persistence, authorization, honest measurements and spreadsheet-safe export."""

import csv
import io

import pytest

from kbagent.trade_pilot import PilotLog, RATINGS


@pytest.fixture
def log(tmp_path):
    return PilotLog(tmp_path / "nested" / "pilot.sqlite")


def test_persistence_original_request_and_independent_measurements(log):
    request = "  first\r\n\r\nsecond\n"
    record_id = log.record("alice", request, 9, 12.5)
    row, = PilotLog(log.path).rows("alice")
    assert row["id"] == record_id
    assert row["request"] == request
    assert row["request_lines"] == 3
    assert row["positions"] == 9
    assert row["agent_seconds"] == 12.5
    assert row["manual_seconds"] is None
    assert row["rating"] is None
    log.review(record_id, "alice", "частично", 90, "Проверить цену")
    log.review(record_id, "alice", "верно", comment="Исправлено")
    row, = log.rows("alice")
    assert row["manual_seconds"] == 90
    assert row["agent_seconds"] == 12.5
    assert row["rating"] == "верно"
    assert row["comment"] == "Исправлено"
    assert row["reviewed_at"] >= row["created_at"]


def test_owner_admin_isolation_and_parameterized_inputs(log):
    alice = log.record("alice", "private", 1, 1)
    bob = log.record("bob", "secret", 1, 2)
    attacker = "' OR 1=1 --"
    log.record(attacker, "literal", 0, 0)
    assert [r["id"] for r in log.rows("alice")] == [alice]
    assert len(log.rows(attacker)) == 1
    assert log.rows("nobody") == []
    for target in (alice, "missing"):
        with pytest.raises(PermissionError):
            log.review(target, "bob", "неверно")
    with pytest.raises(PermissionError):
        log.review(alice, "bob", "неверно", is_admin="yes")
    log.review(alice, "admin", "верно", is_admin=True)
    assert len(log.rows("admin", is_admin=True)) == 3
    assert log.summary("bob")["count"] == 1
    assert log.summary("admin", is_admin=True)["count"] == 3
    assert [r["id"] for r in csv.DictReader(io.StringIO(log.export_csv("bob").decode("utf-8-sig")))] == [bob]
    assert len(list(csv.DictReader(io.StringIO(log.export_csv("admin", True).decode("utf-8-sig"))))) == 3


def test_paired_reduction_excludes_unpaired_and_quality_uses_reviews(log):
    first = log.record("a", "one", 1, 10, 100)
    second = log.record("a", "two", 2, 30, 100)
    third = log.record("a", "three", 3, 10000)
    log.record("a", "four", 4, 9999)
    log.record("b", "other user", 100, 99999, 1)
    for record_id, rating in zip((first, second, third), RATINGS):
        log.review(record_id, "a", rating)
    summary = log.summary("a")
    assert summary["count"] == 4
    assert summary["positions"] == 10
    assert summary["request_lines"] == 4
    assert summary["agent_seconds"] == 20039
    assert summary["paired_agent_seconds"] == 40
    assert summary["paired_manual_seconds"] == summary["manual_seconds"] == 200
    assert summary["paired_count"] == summary["manual_count"] == 2
    assert summary["unpaired_count"] == 2
    assert summary["time_reduction_pct"] == pytest.approx(80)
    assert summary["reviewed_count"] == 3
    assert summary["unreviewed_count"] == 1
    assert summary["rating_counts"] == dict.fromkeys(RATINGS, 1)
    assert summary["rating_rates"] == dict.fromkeys(RATINGS, 1 / 3)
    assert summary["correct_rate"] == 1 / 3


def test_empty_missing_zero_and_slower_measurements(log):
    empty = log.summary("a")
    assert empty["count"] == empty["reviewed_count"] == 0
    assert empty["time_reduction_pct"] is empty["correct_rate"] is None
    assert empty["manual_seconds"] is empty["paired_agent_seconds"] is None
    assert empty["rating_rates"] == dict.fromkeys(RATINGS, None)
    record_id = log.record("a", "", 0, 0)
    assert log.rows("a")[0]["request_lines"] == 0
    assert log.summary("a")["manual_seconds"] is None
    log.review(record_id, "a", "верно", 0)
    assert log.summary("a")["paired_count"] == 1
    assert log.summary("a")["manual_seconds"] == 0
    assert log.summary("a")["time_reduction_pct"] is None
    log.record("b", "slower", 1, 20, 10)
    assert log.summary("b")["time_reduction_pct"] == -100


@pytest.mark.parametrize("rating", ["Верно", "верно ", " верно", "да", "", None, 1, [], "correct"])
def test_exact_rating_validation_is_atomic(log, rating):
    record_id = log.record("a", "request", 1, 1)
    with pytest.raises(ValueError):
        log.review(record_id, "a", rating, 10, "comment")
    row, = log.rows("a")
    assert row["rating"] is row["manual_seconds"] is row["reviewed_at"] is None


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), -float("inf"), True, "1", 10**400])
def test_invalid_times_rejected(log, bad):
    with pytest.raises(ValueError):
        log.record("a", "x", 1, bad)
    with pytest.raises(ValueError):
        log.record("a", "x", 1, 1, bad)
    record_id = log.record("a", "x", 1, 1)
    with pytest.raises(ValueError):
        log.review(record_id, "a", "верно", bad)
    assert log.rows("a")[0]["rating"] is None


@pytest.mark.parametrize("positions", [-1, 1.5, True, "1", 2**63])
def test_invalid_positions(log, positions):
    with pytest.raises(ValueError):
        log.record("a", "x", positions, 1)


@pytest.mark.parametrize("user", ["", "  ", None, 1])
def test_invalid_user_cannot_access_or_write(log, user):
    for action in (
        lambda: log.record(user, "x", 1, 1),
        lambda: log.rows(user, True),
        lambda: log.summary(user, True),
        lambda: log.export_csv(user, True),
        lambda: log.review("missing", user, "верно", is_admin=True),
    ):
        with pytest.raises(ValueError):
            action()


@pytest.mark.parametrize("payload", ["=1+1", "+SUM(A1)", "-1+2", "@SUM(A1)", "  =1", "\t=1", "\r=1", "\n=1", "\x00 =1", "\u2003@SUM(A1)"])
def test_csv_formula_injection_and_raw_storage(log, payload):
    record_id = log.record(payload, payload, 1, 1)
    log.review(record_id, payload, "верно", comment=payload)
    exported = log.export_csv(payload)
    assert exported.startswith(b"\xef\xbb\xbf")
    row, = csv.DictReader(io.StringIO(exported.decode("utf-8-sig"), newline=""))
    for field in ("user", "request", "comment"):
        assert row[field] == "'" + payload
        assert log.rows(payload)[0][field] == payload
    assert row["manual_seconds"] == ""


def test_csv_unicode_multiline_quotes_and_database_isolation(log, tmp_path):
    request = 'Осциллограф, "АКИП"\nВторая строка'
    log.record("Сотрудник", request, 2, 1)
    row, = csv.DictReader(io.StringIO(log.export_csv("Сотрудник").decode("utf-8-sig"), newline=""))
    assert row["request"] == request
    assert PilotLog(tmp_path / "other.sqlite").rows("Сотрудник") == []
    assert len(list(csv.DictReader(io.StringIO(log.export_csv("absent").decode("utf-8-sig"))))) == 0
