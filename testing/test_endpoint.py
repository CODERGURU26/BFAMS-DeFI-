from sqlalchemy.orm import Session
from database import get_test_engine
from app.routes.transactions import list_transactions

with Session(get_test_engine()) as session:
    print(list_transactions(db=session))