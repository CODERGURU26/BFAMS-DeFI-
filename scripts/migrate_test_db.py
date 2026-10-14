import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text, inspect
from database import get_test_engine
from app.models import Base, SecurityDailyPrice


def migrate_test_db():
    engine = get_test_engine()
    with engine.connect() as conn:
        db_name = conn.execute(text("SELECT current_database()")).scalar()
        if db_name != "BFAMS-DeFI-test":
            print(f"ABORT: Expected 'BFAMS-DeFI-test', got '{db_name}'", file=sys.stderr)
            sys.exit(1)

        print(f"Verified connected to: {db_name}")

        # 1. Add mf_scheme_id column to investment_holdings if not exists
        conn.execute(text("""
            ALTER TABLE investment_holdings
            ADD COLUMN IF NOT EXISTS mf_scheme_id UUID REFERENCES mf_schemes(id);
        """))
        print("Column mf_scheme_id ensured on investment_holdings.")

        # 2. Add partial index on mf_scheme_id
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_investment_holdings_mf_scheme
            ON investment_holdings (mf_scheme_id)
            WHERE mf_scheme_id IS NOT NULL;
        """))
        print("Index idx_investment_holdings_mf_scheme ensured.")

        # 3. Add series column to security_daily_prices if not exists
        conn.execute(text("""
            ALTER TABLE security_daily_prices
            ADD COLUMN IF NOT EXISTS series TEXT;
        """))
        print("Column series ensured on security_daily_prices.")

        conn.commit()

    # 4. Create security_daily_prices table if not exists
    Base.metadata.create_all(engine, tables=[SecurityDailyPrice.__table__])
    print("Table security_daily_prices ensured.")

    # Verify
    inspector = inspect(engine)
    cols = [c["name"] for c in inspector.get_columns("investment_holdings")]
    has_sdp = "security_daily_prices" in inspector.get_table_names()
    print(f"Verification: investment_holdings cols: {cols}")
    print(f"Verification: security_daily_prices exists: {has_sdp}")


if __name__ == "__main__":
    migrate_test_db()
