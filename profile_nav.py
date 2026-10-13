from collections import Counter

PATH = "data/amfi/NAVAll.txt"

counts = Counter()
categories = []
odd = []

try:
    with open(PATH, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                counts["blank"] += 1
            elif s.count(";") == 7:
                counts["8-field row (includes header)"] += 1
            elif ";" not in s and s.startswith(("Open Ended", "Close Ended", "Interval")):
                counts["category line"] += 1
                categories.append(s)
            elif ";" not in s:
                counts["fund house line"] += 1
            else:
                counts["other"] += 1
                odd.append((n, s[:120]))
except UnicodeDecodeError as exc:
    print("DECODE ERROR: the file is not valid UTF-8:", exc.reason, "at byte", exc.start)
    raise SystemExit(1)

print(dict(counts))
print()
print(*categories, sep="\n")
print()
print("odd lines (first 20):")
print(*odd[:20], sep="\n")