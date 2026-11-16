"""Trainer assignment: which classes a member may see and book.

Mirrors fitnez_main_backend app/services/client_service/trainer_assignment.py; keep
the two in step. The gym picks gym_config.booking_settings.trainer_assignment_mode:

* ``open`` (default) - every class, as before.
* ``assigned_only`` - logged-in clients see and book only classes taught by one of
  their assigned trainers (client_trainers). Logged-out visitors still see the
  timetable (booking needs a login anyway).
* ``mixed`` - classes with audience 'everyone' for all, plus 'assigned_clients'
  classes for clients of the class's trainers. Visitors see 'everyone' classes only.

users.can_book_online = false stops a client booking from the app; staff book for them.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.client_trainer import ClientTrainer
from app.models.user import User
from app.schemas.gym_config_value import GymConfigValue

OPEN, ASSIGNED_ONLY, MIXED = "open", "assigned_only", "mixed"
EVERYONE, ASSIGNED_CLIENTS = "everyone", "assigned_clients"
CUSTOMER_TYPES = ("client", "member")

# A predicate over a class (ORM row or cached dict); None means no filtering.
Visibility = Optional[Callable[[Any], bool]]


def mode_of(cfg: Optional[GymConfigValue]) -> str:
    raw = getattr(getattr(cfg, "booking_settings", None), "trainer_assignment_mode", None)
    return raw if raw in (OPEN, ASSIGNED_ONLY, MIXED) else OPEN


def assigned_trainer_ids(db: Session, tenant_id: str, user_id) -> List[str]:
    rows = (
        db.query(ClientTrainer.trainer_id)
        .filter(ClientTrainer.tenant_id == tenant_id, ClientTrainer.client_id == user_id)
        .all()
    )
    return [str(r[0]) for r in rows]


def _is_customer(user: Optional[User]) -> bool:
    return user is not None and (getattr(user, "user_type", None) or "member") in CUSTOMER_TYPES


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def class_trainer_ids(obj: Any) -> List[str]:
    """Everyone teaching a class (ORM row or cached dict; older cache rows lack trainer_ids)."""
    ids = list(_get(obj, "trainer_ids") or [])
    if not ids:
        ids = [t.get("id") if isinstance(t, dict) else getattr(t, "id", None) for t in (_get(obj, "trainers") or [])]
    if not ids and _get(obj, "trainer_id"):
        ids = [_get(obj, "trainer_id")]
    return [str(t) for t in ids if t]


def class_audience(obj: Any) -> str:
    return _get(obj, "audience") or EVERYONE


def _open_to(mode: str, obj: Any, trainer_ids: Iterable[str]) -> bool:
    if mode == OPEN:
        return True
    if mode == MIXED and class_audience(obj) == EVERYONE:
        return True
    return bool(set(class_trainer_ids(obj)) & set(trainer_ids))


def visibility_for(db: Session, tenant_id: str, user: Optional[User], cfg: Optional[GymConfigValue]) -> Visibility:
    """What this viewer may see in class lists and details."""
    mode = mode_of(cfg)
    if mode == OPEN:
        return None
    if user is None:
        if mode == MIXED:
            return lambda obj: class_audience(obj) == EVERYONE
        return None  # assigned_only: the timetable stays public, booking needs a login
    if not _is_customer(user):
        return None
    mine = set(assigned_trainer_ids(db, tenant_id, user.id))
    return lambda obj: _open_to(mode, obj, mine)


def booking_refusal(
    db: Session, tenant_id: str, user: User, gym_class: Any, cfg: Optional[GymConfigValue]
) -> Optional[Tuple[str, str]]:
    """(check name, message) when the member may not book this class, else None."""
    if not _is_customer(user):
        return None
    if getattr(user, "can_book_online", True) is False:
        return (
            "online_booking_allowed",
            "Online booking isn't available on your account. Please contact the gym to book this class.",
        )
    mode = mode_of(cfg)
    if _open_to(mode, gym_class, assigned_trainer_ids(db, tenant_id, user.id)):
        return None
    ids = class_trainer_ids(gym_class)
    teachers = db.query(User).filter(User.id.in_(ids)).all() if ids else []
    names = ", ".join(
        f"{t.first_name or ''} {t.last_name or ''}".strip() or (t.email or "") for t in teachers
    ) or "its trainers"
    return "assigned_trainer", f"This class is only for clients of {names}."


def my_trainers(db: Session, tenant_id: str, user: User) -> List[dict]:
    rows = (
        db.query(User)
        .join(ClientTrainer, ClientTrainer.trainer_id == User.id)
        .filter(ClientTrainer.tenant_id == tenant_id, ClientTrainer.client_id == user.id)
        .order_by(ClientTrainer.created_at, User.first_name)
        .all()
    )
    return [
        {"id": str(u.id), "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or None, "avatar": u.avatar}
        for u in rows
    ]
