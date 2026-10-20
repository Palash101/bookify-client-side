"""
Read-only mappings for challenge configuration, reward grants and earned badges
(tenant DB). Schema: fitnez_main_backend/tasks/sql/tenant/002_event_challenges.sql.

Rows are written by the main backend (config) and fitnez_activites (grants,
badges); the client backend only reads them. Postgres enum columns are mapped as
plain strings so new enum values never break reads here.
"""
import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.core.db.session import Base


class EventCondition(Base):
    __tablename__ = "event_conditions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("event_events.id"), nullable=False)
    challenge_type = Column(String, nullable=False)  # challenge_type_enum: booking | package
    challenge_mode = Column(String, nullable=False)  # challenge_mode_enum
    conditions = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EventReward(Base):
    __tablename__ = "event_rewards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # rewards_type: package | wallet | badge
    payload = Column(JSONB, nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EventRewardRel(Base):
    __tablename__ = "event_rewards_rel"

    event_id = Column(UUID(as_uuid=True), ForeignKey("event_events.id"), primary_key=True)
    reward_id = Column(UUID(as_uuid=True), ForeignKey("event_rewards.id"), primary_key=True)
    tier = Column(Integer, primary_key=True, server_default="1")


class EventBadge(Base):
    __tablename__ = "event_badges"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    icon_path = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EventRewardGrant(Base):
    __tablename__ = "event_reward_grants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String(50), nullable=False)
    event_id = Column(UUID(as_uuid=True), ForeignKey("event_events.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    reward_id = Column(UUID(as_uuid=True), ForeignKey("event_rewards.id"), nullable=False)
    tier = Column(Integer, nullable=False)
    rank = Column(Integer, nullable=False)
    reward_type = Column(String(20), nullable=False)
    payload = Column(JSONB, nullable=False)
    status = Column(String(20), nullable=False)  # pending | granted | failed
    attempts = Column(Integer, nullable=False, server_default="0")
    last_error = Column(String, nullable=True)
    reference_type = Column(String(50), nullable=True)
    reference_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    granted_at = Column(DateTime(timezone=True), nullable=True)


class UserBadge(Base):
    __tablename__ = "user_badges"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String(50), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    badge_id = Column(UUID(as_uuid=True), ForeignKey("event_badges.id"), nullable=False)
    event_id = Column(UUID(as_uuid=True), ForeignKey("event_events.id"), nullable=True)
    grant_id = Column(UUID(as_uuid=True), ForeignKey("event_reward_grants.id"), nullable=True)
    awarded_at = Column(DateTime(timezone=True), server_default=func.now())
