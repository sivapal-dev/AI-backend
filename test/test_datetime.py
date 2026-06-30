from datetime import datetime, timezone

# Simulate stored aware ISO string
aware_str = "2026-05-13T19:00:00+00:00"
# Simulate stored naive ISO string
naive_str = "2026-05-13T19:00:00"

for s in [aware_str, naive_str]:
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is not None:
        naive = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        naive = parsed
    now = datetime.utcnow()
    expired = now >= naive
    print(f"Original: {s} -> Parsed tzinfo: {parsed.tzinfo} -> Naive: {naive} (expired={expired})")
