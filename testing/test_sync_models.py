from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_test_engine
from app.models import DataSyncRun

engine = get_test_engine()
with Session(engine) as session:
    runs = session.scalars(select(DataSyncRun)).all()
    print("sync runs:", len(runs))