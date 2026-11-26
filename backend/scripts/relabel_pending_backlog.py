"""Pass the existing "pending review" backlog through the direct-promotion gate.

ONE-OFF. Run only after the change is approved in vivo_control_governance
(maker/checker) and TRAINING_DIRECT_PROMOTION_ENABLED is on.

Migration 0054 moved every click-approved operator label back to
pending_review. With direct promotion on, those labels can be judged the
same way a new mark is: re-running the training-sample step for each
alert applies today's classification (two-choice False, Unclear / Bad
image) and the trust/agreement gate, updating the alert's single
training row in place. The burst guard looks at when each mark was
originally made, so historic queue-clearing bursts are held too.

Dry run by default: prints what would happen and changes nothing.

    docker compose exec -T worker-alerts python scripts/relabel_pending_backlog.py
    docker compose exec -T worker-alerts python scripts/relabel_pending_backlog.py --apply

Runs on worker-alerts (it has the datasets volume the image copy needs).
"""
from __future__ import annotations

import argparse
import collections
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the decisions (default: dry run)")
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args(argv)

    from app.config import settings
    from app.database import SessionLocal
    from app.models import Alert, TrainingImage
    from app.training.feedback_loop import absorb_label

    if args.apply and not settings.training_direct_promotion_enabled:
        print("Refusing to apply: TRAINING_DIRECT_PROMOTION_ENABLED is off. "
              "Approve the change in vivo_control_governance and switch it on first.")
        return 2

    with SessionLocal() as db:
        rows = (db.query(TrainingImage.id, TrainingImage.label_alert_id, Alert.status)
                  .join(Alert, Alert.id == TrainingImage.label_alert_id)
                  .filter(TrainingImage.review_state.in_(("pending_review", "pending")),
                          TrainingImage.label_alert_id.isnot(None),
                          TrainingImage.source_kind.in_(
                              ("operator_confirmed", "operator_dismissed")))
                  .order_by(TrainingImage.labelled_at.nullsfirst(), TrainingImage.id)
                  .limit(max(1, args.limit)).all())
        print(f"{len(rows)} pending operator labels found")
        if not args.apply:
            by_status = collections.Counter(status for _, _, status in rows)
            print("by alert status:", dict(by_status))
            print("dry run - nothing changed. Re-run with --apply after approval.")
            return 0

        outcomes: collections.Counter = collections.Counter()
        for _img_id, alert_id, status in rows:
            mode = {"confirmed": "confirm", "dismissed": "dismiss"}.get(status)
            if mode is None:
                outcomes["skipped_alert_not_marked"] += 1
                continue
            alert = db.get(Alert, alert_id)
            alert.feedback_used_for_training = False
            db.commit()
            absorb_label(db, alert_id, mode)
            path = (db.query(TrainingImage.promotion_path)
                      .filter(TrainingImage.label_alert_id == alert_id).scalar())
            outcomes[path or "unknown"] += 1
        print("outcomes:", dict(outcomes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
