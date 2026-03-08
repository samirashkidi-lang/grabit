from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, Boolean, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import uuid

DATABASE_URL = "sqlite:///./grabit.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Order(Base):
    __tablename__ = "orders"

    id                = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    fb_link           = Column(String, nullable=False)
    item_title        = Column(String, nullable=False)
    item_price        = Column(Float, nullable=False)

    # Buyer info
    buyer_name        = Column(String, nullable=False)
    buyer_email       = Column(String, nullable=False)
    buyer_phone       = Column(String, nullable=True)
    delivery_address  = Column(String, nullable=False)

    # Pickup / seller side
    pickup_address    = Column(String, nullable=True)   # seller's address / FB listing location

    # Distance & fees
    distance_miles    = Column(Float, nullable=False)
    delivery_fee      = Column(Float, nullable=False)
    service_fee       = Column(Float, nullable=False)
    total             = Column(Float, nullable=False)
    runner_payout     = Column(Float, nullable=False)
    platform_profit   = Column(Float, nullable=False)

    # Heavy item option
    heavy_item        = Column(Boolean, default=False)
    runners_needed    = Column(Integer, default=1)

    # Status & assignment
    # pending → accepted → picked_up → delivered
    status            = Column(String, default="pending")
    runner_id         = Column(String, nullable=True)

    # Payment
    stripe_payment_id = Column(String, nullable=True)
    payment_status    = Column(String, default="unpaid")  # unpaid / paid

    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Runner(Base):
    __tablename__ = "runners"

    id               = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name             = Column(String, nullable=False)
    email            = Column(String, unique=True, index=True)
    phone            = Column(String, nullable=True)
    password_hash    = Column(String, nullable=True)
    bio              = Column(Text, nullable=True)
    is_active        = Column(Boolean, default=True)
    is_approved      = Column(Boolean, default=False)
    total_earnings   = Column(Float, default=0.0)
    total_deliveries = Column(Integer, default=0)
    created_at       = Column(DateTime, default=datetime.utcnow)


class RunnerSession(Base):
    __tablename__ = "runner_sessions"

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    runner_id  = Column(String, nullable=False, index=True)
    token      = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
