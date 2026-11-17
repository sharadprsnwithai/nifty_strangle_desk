import sys, os
sys.path.insert(0, os.path.abspath("."))
from kite import get_kite_client

kite = get_kite_client()
token = 126774791  # SILVER26DECFUT

ranges = [
    ('2026-06-01', '2026-11-01'),
    ('2026-01-01', '2026-05-31'),
    ('2025-06-01', '2025-12-31'),
    ('2025-01-01', '2025-05-31'),
    ('2024-06-01', '2024-12-31'),
    ('2024-01-01', '2024-05-31'),
    ('2023-11-01', '2023-12-31')
]

for start, end in ranges:
    try:
        d = kite.historical_data(token, start, end, '15minute')
        min_d = d[0]['date'] if d else None
        max_d = d[-1]['date'] if d else None
        print(f"{start} to {end}: {len(d)} bars | min: {min_d} | max: {max_d}")
    except Exception as e:
        print(f"{start} to {end}: {e}")
