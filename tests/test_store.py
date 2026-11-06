"""The record rules from SPEC section 8, checked without a database."""
from datetime import UTC, datetime, timedelta

from riley import store

NOW = datetime(2026, 11, 5, 15, tzinfo=UTC)


def test_one_profile_per_person():
    assert store.phone_key('+1 (212) 555-0100') == store.phone_key('212.555.0100') == '2125550100'
    assert store.email_key(' Ana@Example.com ') == 'ana@example.com'
    assert store.license_key('ny 123-456') == 'NY123-456'
    assert store.phone_key('') is None and store.email_key(None) is None


def test_everyone_hears_within_48_hours():
    assert store.promised_by(NOW) == NOW + timedelta(hours=48)


def test_no_decision_before_the_review_window_closes():
    job = {'first_applied': NOW, 'window_hours': 48}
    assert not store.can_decide(job, NOW + timedelta(hours=47))
    assert store.can_decide(job, NOW + timedelta(hours=48))
    assert not store.can_decide({'first_applied': None}, NOW)


def test_withdrawal_undo_lasts_7_days():
    assert store.can_undo(NOW, NOW + timedelta(days=7))
    assert not store.can_undo(NOW, NOW + timedelta(days=7, seconds=1))
    assert not store.can_undo(None, NOW)


def test_resume_versions_show_changes_and_conflicts():
    changes, conflicts = store.resume_changes({'years': 4, 'acls': None}, {'years': 5, 'acls': True})
    assert changes == ['added acls: True', 'years 4 → 5']
    assert conflicts == ['years']


def test_timestamps_from_octo_parse():
    assert store.ts('2026-11-05T15:00:00Z') == NOW
