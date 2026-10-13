
from sqlalchemy import text
from database import engine

with engine.connect() as connection:
    result = connection.execute(
        text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
    )

    for row in result:
        print(row[0])
