import sys, os
sys.path.insert(0, os.path.abspath("."))
from kite import get_kite_client

kite = get_kite_client()
for sym, tok in [
    ('SILVER26DECFUT', 126774791),
    ('SILVER27MARFUT', 144542471),
    ('SILVERM26NOVFUT', 123668487),
    ('SILVERM27FEBFUT', 144542727),
    ('SILVERMIC26NOVFUT', 143886855),
]:
    found = False
    for start, end in [
        ('2023-11-01', '2024-03-01'),
        ('2024-03-01', '2024-09-01'),
        ('2024-09-01', '2025-03-01'),
        ('2025-03-01', '2025-09-01'),
        ('2025-09-01', '2026-01-01'),
    ]:
        try:
            d = kite.historical_data(tok, start, end, '15minute')
            if d:
                print(f"{sym} ({tok}): starts at {d[0]['date']}")
                found = True
                break
        except Exception as e:
            pass
    if not found:
        print(f"{sym} ({tok}): not found before 2026")
