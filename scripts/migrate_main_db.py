"""Add missing application tables to the explicitly selected staging database."""

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import inspect, text

from app.models import Base
from database import engine, settings


TARGET_DATABASE = "BFAMS-DeFI"
MUTABLE_COLUMN = ("investment_holdings", "mf_scheme_id")
MUTABLE_INDEX = "idx_investment_holdings_mf_scheme"


def _column_type(column, dialect) -> str:
    return str(column.type.compile(dialect=dialect)).lower()


def migration_plan(connection) -> dict:
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    model_tables = set(Base.metadata.tables)
    missing_tables = sorted(model_tables - existing_tables)
    missing_columns = []
    incompatible_columns = []

    for table_name in sorted(model_tables & existing_tables):
        actual_columns = {
            column["name"]: column
            for column in inspector.get_columns(table_name)
        }
        model_table = Base.metadata.tables[table_name]
        for column_name, model_column in model_table.columns.items():
            actual_column = actual_columns.get(column_name)
            if actual_column is None:
                if (table_name, column_name) == MUTABLE_COLUMN:
                    missing_columns.append((table_name, column_name))
                else:
                    incompatible_columns.append(
                        f"{table_name}.{column_name} is missing"
                    )
                continue

            model_type = _column_type(model_column, connection.dialect)
            database_type = str(
                actual_column["type"].compile(dialect=connection.dialect)
            ).lower()
            if (
                model_type != database_type
                or model_column.nullable != actual_column["nullable"]
            ):
                incompatible_columns.append(
                    f"{table_name}.{column_name} differs "
                    f"(model {model_type}, database {database_type})"
                )

    if incompatible_columns:
        raise RuntimeError(
            "Existing columns differ from the application model: "
            + "; ".join(incompatible_columns)
        )

    existing_indexes = {
        index["name"]
        for index in inspector.get_indexes("investment_holdings")
    } if "investment_holdings" in existing_tables else set()
    add_scheme_index = MUTABLE_INDEX not in existing_indexes

    return {
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "add_scheme_index": add_scheme_index,
    }


def _verify_target(connection) -> None:
    database_name = connection.execute(
        text("SELECT current_database()")
    ).scalar_one()
    if settings.db_name != TARGET_DATABASE or database_name != TARGET_DATABASE:
        raise RuntimeError(
            f"Safety abort: target must be {TARGET_DATABASE!r}; "
            f"configured={settings.db_name!r}, connected={database_name!r}."
        )


def _print_plan(plan: dict) -> None:
    if plan["missing_tables"]:
        print("Tables to create: " + ", ".join(plan["missing_tables"]))
    else:
        print("Tables to create: none")
    if plan["missing_columns"]:
        print(
            "Columns to add: "
            + ", ".join(f"{table}.{column}" for table, column in plan["missing_columns"])
        )
    else:
        print("Columns to add: none")
    print(
        "MF scheme index: "
        + ("create" if plan["add_scheme_index"] else "already present")
    )


def migrate(apply: bool = False) -> int:
    with engine.connect() as connection:
        _verify_target(connection)
        plan = migration_plan(connection)

    _print_plan(plan)
    if not apply:
        print("Dry run only. Re-run with --apply to execute this additive migration.")
        return 0

    with engine.begin() as connection:
        _verify_target(connection)
        plan = migration_plan(connection)
        Base.metadata.create_all(connection)

        if MUTABLE_COLUMN in plan["missing_columns"]:
            connection.execute(
                text(
                    "ALTER TABLE investment_holdings "
                    "ADD COLUMN IF NOT EXISTS mf_scheme_id UUID "
                    "REFERENCES mf_schemes(id)"
                )
            )
        if plan["add_scheme_index"]:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS "
                    "idx_investment_holdings_mf_scheme "
                    "ON investment_holdings (mf_scheme_id) "
                    "WHERE mf_scheme_id IS NOT NULL"
                )
            )

    with engine.connect() as connection:
        _verify_target(connection)
        verified_plan = migration_plan(connection)
    if (
        verified_plan["missing_tables"]
        or verified_plan["missing_columns"]
        or verified_plan["add_scheme_index"]
    ):
        raise RuntimeError("Migration completed but schema verification failed.")

    print("Additive staging migration applied and verified. No application rows copied.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the additive schema migration; default is a read-only dry run.",
    )
    args = parser.parse_args(argv)
    return migrate(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
