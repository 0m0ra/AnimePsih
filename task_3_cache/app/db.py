import time
from sqlalchemy import Column, Integer, String, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    value = Column(String, nullable=False)


_engine = None
_SessionLocal = None


def init_engine(db_url: str, retries: int = 30, delay: float = 2.0):
    global _engine, _SessionLocal
    last_err = None
    for i in range(retries):
        try:
            engine = create_engine(db_url, pool_pre_ping=True, pool_size=20, max_overflow=10)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            _engine = engine
            _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
            Base.metadata.create_all(engine)
            return engine
        except OperationalError as e:
            last_err = e
            print(f"[db] waiting for postgres ({i + 1}/{retries})")
            time.sleep(delay)
    raise RuntimeError(f"failed to connect to postgres: {last_err}")


def get_session():
    if _SessionLocal is None:
        raise RuntimeError("db engine not initialized")
    return _SessionLocal()


def truncate_items():
    with get_session() as s:
        s.execute(Item.__table__.delete())
        s.commit()
