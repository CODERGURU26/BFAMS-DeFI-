from datetime import datetime, timezone

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import MarketDataLoad, MfNav, MfScheme

SCHEME_FIELDS = (
    "isin_primary",
    "isin_reinvestment",
    "scheme_name",
    "plan",
    "option_name",
    "amc_name",
    "scheme_type",
    "scheme_category",
)
CHUNK = 2000


def _chunks(items, size=CHUNK):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def load_nav(
    session: Session,
    schemes,
    navs,
    rejected,
    source="amfi_navall",
    update_schemes=True,
):
    try:
        run = MarketDataLoad(source=source, status="running")
        session.add(run)
        session.flush()

        scheme_table = MfScheme.__table__
        existing = {
            row.scheme_code: tuple(getattr(row, f) for f in SCHEME_FIELDS)
            for row in session.execute(
                select(
                    MfScheme.scheme_code,
                    *[getattr(MfScheme, f) for f in SCHEME_FIELDS],
                )
            )
        }

        new_schemes, changed = [], []
        if schemes:
            for s in schemes:
                current = existing.get(s["scheme_code"])
                if current is None:
                    new_schemes.append(s)
                elif update_schemes and current != tuple(s[f] for f in SCHEME_FIELDS):
                    changed.append(s)

        for chunk in _chunks(new_schemes):
            session.execute(insert(scheme_table), chunk)
        for s in changed:
            session.execute(
                update(scheme_table)
                .where(scheme_table.c.scheme_code == s["scheme_code"])
                .values(
                    **{f: s[f] for f in SCHEME_FIELDS}, updated_at=func.now()
                )
            )

        id_by_code = dict(
            session.execute(select(MfScheme.scheme_code, MfScheme.id)).all()
        )
        nav_rows = []
        unmapped_navs = 0
        for n in navs:
            sid = id_by_code.get(n["scheme_code"])
            if sid is not None:
                nav_rows.append({
                    "scheme_id": sid,
                    "nav_date": n["nav_date"],
                    "nav": n["nav"],
                })
            else:
                unmapped_navs += 1

        before = session.scalar(select(func.count()).select_from(MfNav))
        stmt = pg_insert(MfNav.__table__).on_conflict_do_nothing(
            index_elements=["scheme_id", "nav_date"]
        )
        for chunk in _chunks(nav_rows):
            session.execute(stmt, chunk)
        after = session.scalar(select(func.count()).select_from(MfNav))
        navs_inserted = after - before

        total_rejected = len(rejected) + unmapped_navs
        run.rows_loaded = len(new_schemes) + navs_inserted
        run.rows_rejected = total_rejected
        run.status = "partial" if total_rejected else "completed"
        run.completed_at = datetime.now(timezone.utc)
        if total_rejected:
            reasons = []
            if rejected:
                lines = ", ".join(str(r["line"]) for r in rejected[:50])
                reasons.append(f"{len(rejected)} line(s) rejected: {lines}")
            if unmapped_navs:
                reasons.append(f"{unmapped_navs} nav(s) skipped for unknown scheme_code")
            run.error_message = "; ".join(reasons)
        session.commit()

        schemes_count = len(schemes) if schemes else 0
        return {
            "schemes_new": len(new_schemes),
            "schemes_changed": len(changed),
            "schemes_unchanged": schemes_count - len(new_schemes) - len(changed),
            "navs_inserted": navs_inserted,
            "navs_already_stored": len(nav_rows) - navs_inserted,
            "unmapped_navs": unmapped_navs,
            "rejected": total_rejected,
        }

    except Exception as exc:
        session.rollback()
        session.add(
            MarketDataLoad(
                source=source,
                status="failed",
                completed_at=datetime.now(timezone.utc),
                error_message=type(exc).__name__,
            )
        )
        session.commit()
        raise

        