from database import get_test_engine
from sqlalchemy.orm import Session

from app.nav_loader import load_nav
from app.nav_parser import parse_nav_lines

engine = get_test_engine()

with open("data/amfi/NAVAll.txt", encoding="utf-8") as f:
    schemes, navs, rejected, stats = parse_nav_lines(f)

print("parsed:", len(schemes), "schemes,", len(navs), "prices,", len(rejected), "rejected")
with Session(engine) as session:
    print("load:", load_nav(session, schemes, navs, rejected))