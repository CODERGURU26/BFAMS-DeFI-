from sqlalchemy import select
from sqlalchemy.orm import Session

from database import engine
from app.models import (
    DataProvider,
    FinancialAccount,
    FinancialConnection,
    FinancialInstitution,
    Transaction,
    User,
)

with Session(engine) as session:
    providers = session.scalars(select(DataProvider)).all()
    for p in providers:
        print(p.provider_code, p.provider_name, p.provider_type)

    users = session.scalars(select(User)).all()
    print("users:", len(users))

    institutions = session.scalars(select(FinancialInstitution)).all()
    for i in institutions:
        print(i.institution_name, i.institution_code, i.institution_type, i.country_code)

    connections = session.scalars(select(FinancialConnection)).all()
    print("connections:", len(connections))

    accounts = session.scalars(select(FinancialAccount)).all()
    print("accounts:", len(accounts))

    transactions = session.scalars(select(Transaction)).all()
    print("transactions:", len(transactions))
    