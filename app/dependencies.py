from sqlalchemy.orm import Session

from database import get_app_engine


_app_engine = None


def _selected_engine():
    global _app_engine
    if _app_engine is None:
        _app_engine = get_app_engine()
    return _app_engine


def get_db():
    with Session(_selected_engine()) as session:
        yield session


def get_mf_db():
    with Session(_selected_engine()) as session:
        yield session