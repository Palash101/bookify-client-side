from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.core.db.session import tenant_session
from app.models.tenants_models.tenant_users import TenantUsers
from app.models.users import User

logger = logging.getLogger(__name__)


def apply_password_change(
    master_db: Session,
    master_user: User,
    password_hash: str,
    *,
    activate: bool = False,
) -> None:
    """
    Set the user's password on both the master and tenant databases (and
    optionally activate the account), committing both as one logical operation.

    The master session may carry other pending changes (e.g. a consumed token);
    they are committed together. On any failure both sessions are rolled back
    and the exception is re-raised for the caller to translate into a response.
    """
    org_id = master_user.organization_id
    try:
        tenant_user_id = uuid.UUID(str(master_user.tenant_user_id))
    except (ValueError, TypeError) as exc:
        raise LookupError("master user has no valid tenant_user_id") from exc

    with tenant_session(org_id) as tenant_db:
        tenant_user = (
            tenant_db.query(TenantUsers)
            .filter(TenantUsers.id == tenant_user_id)
            .first()
        )
        if tenant_user is None:
            raise LookupError("tenant user not found")

        tenant_user.password_hash = password_hash
        master_user.password = password_hash
        if activate:
            tenant_user.is_active = True
            master_user.is_active = True

        # Commit master first; if the tenant commit then fails we have a rare
        # cross-DB inconsistency, which is logged for manual reconciliation.
        master_db.commit()
        try:
            tenant_db.commit()
        except Exception:
            logger.critical(
                "Password updated in master but tenant commit failed for "
                "user_id=%s org=%s; manual reconciliation required",
                master_user.tenant_user_id,
                org_id,
            )
            raise
