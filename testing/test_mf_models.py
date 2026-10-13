from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import get_test_engine
from app.models import MarketDataLoad, MfNav, MfScheme

engine = get_test_engine()
with Session(engine) as session:
    for model in (MfScheme, MfNav, MarketDataLoad):
        count = session.scalar(select(func.count()).select_from(model))
        print(model.__tablename__, count)