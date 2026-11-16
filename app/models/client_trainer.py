from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base


class ClientTrainer(Base):
    """A trainer assigned to a client (written by the staff backend).

    Schema: fitnez_main_backend tasks/sql/tenant.sql (trainer assignment)
    """

    __tablename__ = "client_trainers"

    tenant_id = Column(String(50), nullable=False, index=True)
    client_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    trainer_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
