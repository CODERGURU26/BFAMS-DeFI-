import re

isin = re.compile(r"^INF[0-9A-Z]{9}$")

with open("data/amfi/NAVAll.txt", encoding="utf-8") as f:
    for line in f:
        s = line.strip()
        if s.count(";") != 7 or s.startswith("Scheme Code"):
            continue
        r = [p.strip() for p in s.split(";")]
        for v in (r[1], r[2]):
            if v != "-" and not isin.match(v):
                print(r[0], repr(v))