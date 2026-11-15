"""Read-only check: prints what the app shows for events from the last 12 h
to the next 12 h (results and when they were saved). Writes nothing."""
import json
import os
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import credentials, firestore

firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])))
db = firestore.client()
now = datetime.now(timezone.utc)
q = (db.collection("events").where("start", ">", now - timedelta(hours=12))
     .where("start", "<", now + timedelta(hours=12)))
for snap in q.stream():
    d = snap.to_dict() or {}
    print(f"{d.get('name')} | main card {d.get('start')} | now {now:%H:%M} UTC")
    for f in d.get("fights") or []:
        ended = f.get("endedAt")
        ended = ended.strftime("%H:%M") if hasattr(ended, "strftime") else ""
        print(f"  {f.get('fighterA')} vs {f.get('fighterB')}: {f.get('winner') or '-'}"
              f" {('| ' + f.get('method')) if f.get('method') else ''} {('| saved ' + ended) if ended else ''}")
