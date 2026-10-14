from sqlalchemy.orm import Session

from database import engine


def get_db():
    with Session(engine) as session:
        yield session


_test_engine = None


def get_mf_db():
    global _test_engine
    if _test_engine is None:
        from database import get_test_engine

        _test_engine = get_test_engine()
    with Session(_test_engine) as session:
        yield session