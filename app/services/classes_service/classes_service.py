from datetime import date, datetime
from typing import Optional, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session, joinedload

from app.models.gym_class import GymClass
from app.models.class_schedule import ClassSchedule
from app.models.user import User
from app.models.tenant import Tenant

# Map common DB abbreviations to IANA timezone names (zoneinfo does not accept "IST" etc.)
COMMON_TZ_ABBREVS = {
    "IST": "Asia/Kolkata",
    "GST": "Asia/Dubai",
    "QAT": "Asia/Qatar",
    "AST": "Asia/Riyadh",
    "PKT": "Asia/Karachi",
    "UTC": "UTC",
}


class ClassesService:
    @staticmethod
    def list_classes(
        db: Session,
        tenant_id,
        start_date: date,
        end_date: date,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
    ) -> List[GymClass]:
        """
        List classes for a tenant in a date range, with optional search and sorting.
        Rules:
          - Only classes whose schedule belongs to this tenant (via created_by user).
          - Always include schedules with status = 'published'.
          - For status = 'draft', include only when publish_at <= tenant's current time.
        """
        # Resolve tenant timezone (DB may store "IST" etc.; zoneinfo needs IANA e.g. "Asia/Kolkata")
        tenant: Optional[Tenant] = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        tz_name = (tenant.timezone or "UTC").strip() if tenant else "UTC"
        tz_key = tz_name.upper()
        if tz_key in COMMON_TZ_ABBREVS:
            tz_name = COMMON_TZ_ABBREVS[tz_key]
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        tenant_now: datetime = datetime.now(tz)

        query = (
            db.query(GymClass)
            .join(ClassSchedule, GymClass.schedule_id == ClassSchedule.id)
            .join(User, ClassSchedule.created_by == User.id)
            .options(joinedload(GymClass.schedule))
            .filter(
                User.tenant_id == tenant_id,
                GymClass.class_date >= start_date,
                GymClass.class_date <= end_date,
                ClassSchedule.status.in_(["published", "draft"]),
            )
        )

        # Search by title
        if search:
            like = f"%{search}%"
            query = query.filter(GymClass.title.ilike(like))

        # Sorting
        sort_column = None
        if sort_by == "date":
            sort_column = GymClass.class_date
        elif sort_by == "start_time":
            sort_column = GymClass.start_time
        elif sort_by == "title":
            sort_column = GymClass.title

        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order.lower() == "asc" else sort_column.desc()
            )
        else:
            # Default ordering
            query = query.order_by(GymClass.class_date, GymClass.start_time)

        all_classes: List[GymClass] = query.all()

        # Post-filter draft schedules by publish_at vs tenant current time
        result: List[GymClass] = []
        for gym_class in all_classes:
            schedule: Optional[ClassSchedule] = gym_class.schedule
            if not schedule:
                continue

            if schedule.status == "published":
                result.append(gym_class)
            elif schedule.status == "draft" and schedule.publish_at is not None:
                # publish_at assumed stored in tenant's timezone
                if schedule.publish_at <= tenant_now:
                    result.append(gym_class)

        return result

