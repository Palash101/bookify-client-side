"""No-show booking block: a member who keeps missing classes can't book for a while.

Gym config (attendance_check_in), no_show_penalty = "booking_block":
  once a member has no_show_limit no-shows within no_show_period_days, they can't book
  until booking_block_days after their last no-show.

Nothing is stored: it's worked out from the member's ``no_show`` bookings when they
book, so the block ends by itself. Staff bookings (staff backend) ignore it. The
no-shows are written by the staff backend's class sweep (gym_classes/no_show_marker.py).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models.class_booking import ClassBooking, ClassBookingStatus
from app.models.gym_class import GymClass
from app.schemas.gym_config_value import AttendanceCheckInConfig

BOOKING_BLOCK = "booking_block"


def booking_blocked_until(
    db: Session, tenant_id: str, user_id, attendance: AttendanceCheckInConfig, today: date
) -> Optional[date]:
    """The first day the member may book again, or None when they aren't blocked."""
    if (attendance.no_show_penalty or "").strip().lower() != BOOKING_BLOCK:
        return None

    limit = max(int(attendance.no_show_limit or 3), 1)
    since = today - timedelta(days=max(int(attendance.no_show_period_days or 30), 1))
    no_show_days = [
        row.class_date
        for row in db.query(GymClass.class_date)
        .join(ClassBooking, ClassBooking.class_id == GymClass.id)
        .filter(
            ClassBooking.tenant_id == tenant_id,
            ClassBooking.user_id == user_id,
            ClassBooking.status == ClassBookingStatus.no_show,
            GymClass.class_date >= since,
        )
        .order_by(GymClass.class_date.desc())
        .limit(limit)
    ]
    if len(no_show_days) < limit:
        return None

    free_from = no_show_days[0] + timedelta(days=max(int(attendance.booking_block_days or 7), 1))
    return free_from if free_from > today else None
