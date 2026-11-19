"""Paying for a package the member already holds runs backfill_missing_booking_debits;
its query used a column attribute that doesn't exist (500 on payment success)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services.user_package_tracking_service import backfill_missing_booking_debits


def test_backfill_query_runs(db):
    user_package = SimpleNamespace(id=uuid.uuid4(), sale_id=uuid.uuid4())
    backfill_missing_booking_debits(db, user_package=user_package, sale=None)  # no bookings: no-op
