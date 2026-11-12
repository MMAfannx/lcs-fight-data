"""Sends the notifications the owner asked for in the app's admin screen.

Nothing is sent automatically: an article is only announced when the owner
taps "Send notification" on it (articles/{id}.notifyRequested = true), and
the quiz only when he taps "Send quiz notification" (a pushRequests doc).

Each request is marked as sent BEFORE sending, so a request can never go out
twice, even if two runs overlap. Safety limits stop accidental spam.
Runs every 5 minutes from .github/workflows/push.yml.
"""

import json
import os
from datetime import datetime, timedelta, timezone

MAX_ARTICLE_PUSHES_PER_DAY = 5
QUIZ_MIN_GAP = timedelta(hours=12)
REQUEST_MAX_AGE = timedelta(hours=24)  # older requests are dropped, not sent


def _client():
    import firebase_admin
    from firebase_admin import credentials, firestore
    info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
    firebase_admin.initialize_app(credentials.Certificate(info))
    return firestore.client()


def _send(title_ar, body_ar, title_en, body_en, data):
    from firebase_admin import messaging
    apns = messaging.APNSConfig(payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")))
    for topic, title, body in (("general_ar", title_ar, body_ar), ("general_en", title_en, body_en)):
        messaging.send(messaging.Message(
            topic=topic,
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in data.items()},
            apns=apns,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(channel_id="general_alerts"),
            ),
        ))


def _short(text, limit=140):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _age_ok(ts, now):
    return ts is not None and now - ts <= REQUEST_MAX_AGE


def articles(db, now):
    from firebase_admin import firestore
    state_ref = db.collection("meta").document("pushState")
    state = state_ref.get().to_dict() or {}
    day = now.strftime("%Y-%m-%d")
    sent_today = state.get("articlesSent", 0) if state.get("day") == day else 0

    for snap in db.collection("articles").where("notifyRequested", "==", True).stream():
        d = snap.to_dict() or {}
        if d.get("notifySentAt") or d.get("notifySkipped"):
            continue
        if not _age_ok(d.get("notifyRequestedAt"), now):
            snap.reference.update({"notifySkipped": "too old"})
            print(f"  skipped (old request): {snap.id}")
            continue
        if sent_today >= MAX_ARTICLE_PUSHES_PER_DAY:
            print("  daily article limit reached; waiting until tomorrow")
            break
        # Mark first: never twice.
        snap.reference.update({"notifySentAt": firestore.SERVER_TIMESTAMP})
        sent_today += 1
        state_ref.set({"day": day, "articlesSent": sent_today}, merge=True)
        title = _short(d.get("title"), 90)
        body = _short(d.get("summary") or d.get("body"))
        try:
            _send(title, body, "New on LCS FIGHT", title, {"type": "news", "id": snap.id})
            print(f"  article notification sent: {snap.id}")
        except Exception as e:  # keep going with the others
            snap.reference.update({"notifyError": type(e).__name__})
            print(f"  article notification FAILED: {snap.id} {type(e).__name__}")


def quiz(db, now):
    from firebase_admin import firestore
    state_ref = db.collection("meta").document("pushState")
    state = state_ref.get().to_dict() or {}
    last = state.get("quizSentAt")
    for snap in db.collection("pushRequests").where("sent", "==", False).stream():
        d = snap.to_dict() or {}
        if d.get("type") != "quiz":
            snap.reference.update({"sent": True, "result": "unknown type"})
            continue
        if not _age_ok(d.get("createdAt"), now):
            snap.reference.update({"sent": True, "result": "too old"})
            continue
        if last is not None and now - last < QUIZ_MIN_GAP:
            snap.reference.update({"sent": True, "result": "skipped: quiz sent less than 12 h ago"})
            print("  quiz request skipped (sent recently)")
            continue
        snap.reference.update({"sent": True, "sentAt": firestore.SERVER_TIMESTAMP})
        state_ref.set({"quizSentAt": firestore.SERVER_TIMESTAMP}, merge=True)
        last = now
        try:
            _send("كويز اليوم جاهز", "أسئلة جديدة بانتظارك، حافظ على سلسلتك!",
                  "Today's quiz is ready", "New questions are waiting. Keep your streak going!",
                  {"type": "quiz"})
            print("  quiz notification sent")
        except Exception as e:
            snap.reference.update({"result": f"error {type(e).__name__}"})
            print(f"  quiz notification FAILED {type(e).__name__}")


def main():
    db = _client()
    now = datetime.now(timezone.utc)
    articles(db, now)
    quiz(db, now)
    print("push requests: done")


if __name__ == "__main__":
    main()
