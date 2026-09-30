"""Generate ~2 months of fake bank alert emails for trying the pipeline without a real inbox.

    python examples/generate_samples.py examples/sample_emails
    fraudalert import-eml examples/sample_emails
"""

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.conftest import make_eml  # noqa: E402

REGULARS = [("BLUE BOTTLE COFFEE", 4, 9), ("WHOLE FOODS MKT #102", 35, 120), ("SHELL OIL 5741", 30, 60),
            ("NETFLIX.COM", 15.49, 15.49), ("CHIPOTLE 2231", 11, 18), ("AMAZON MKTPL*2K4", 12, 80)]
ODDBALLS = [
    ("You made a $1,899.00 transaction with APPLE STORE R102", "Your $1,899.00 transaction with APPLE STORE R102"),
    ("A charge of EUR 64,50 at RISTORANTE DA MARIO ROMA on your card ending in 4242. This is a foreign transaction.",
     "International transaction alert"),
    ("A charge of GBP 12.00 at PRET A MANGER LONDON on your card ending in 4242.", "Purchase alert"),
    ("You made a $2,450.00 transaction with GRAND ELECTRONICS HK on your card ending in 4242.", "Large purchase alert"),
]


def main(out: Path) -> None:
    random.seed(7)
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    n = 0
    for day in range(60, 0, -1):
        for _ in range(random.randint(0, 3)):
            merchant, lo, hi = random.choice(REGULARS)
            amt = round(random.uniform(lo, hi), 2)
            when = now - timedelta(days=day, hours=random.randint(0, 10))
            body = f"You made a ${amt:,.2f} transaction with {merchant} on your card ending in 4242."
            (out / f"{n:04d}.eml").write_bytes(make_eml(f"Your ${amt:,.2f} transaction with {merchant}", body, when))
            n += 1
    for i, (body, subject) in enumerate(ODDBALLS):
        when = now - timedelta(days=3 - i * 0.7)
        (out / f"{n:04d}.eml").write_bytes(make_eml(subject, body, when))
        n += 1
    (out / f"{n:04d}.eml").write_bytes(make_eml("Your statement is ready", "Your new balance is $1,234.00.", now))
    print(f"wrote {n + 1} emails to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "examples/sample_emails"))
