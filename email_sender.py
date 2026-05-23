"""
SCOUT — Email Sender API
========================
Add this route to your existing roster_server.py
OR deploy as a standalone Flask app.

Requires one env var:
  RESEND_API_KEY   — get a free key at resend.com (3,000 emails/month free)

The front-end calls:
  POST /api/send-email
  { to, toName, school, replyTo, subject, body }
"""

import os, logging
import requests
from flask import request, jsonify

log = logging.getLogger("scout")

RESEND_API_KEY  = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL      = os.environ.get("FROM_EMAIL", "outreach@thirtysecondtimeout.com")
FROM_NAME       = os.environ.get("FROM_NAME",  "ThirtySecondTimeout Recruiting")

# Rate-limit: max emails per request batch (safety cap)
MAX_PER_CALL = 1  # send one at a time from the loop in the front-end


def send_via_resend(to, to_name, reply_to, subject, body_text):
    """Send a single email via Resend.com API."""
    if not RESEND_API_KEY:
        raise ValueError("RESEND_API_KEY not set")

    # Convert plain text body to simple HTML
    html_body = "<br>".join(
        f"<p>{line}</p>" if line.strip() else "<br>"
        for line in body_text.split("\n")
    )

    payload = {
        "from":     f"{FROM_NAME} <{FROM_EMAIL}>",
        "to":       [f"{to_name} <{to}>" if to_name else to],
        "reply_to": reply_to if reply_to else None,
        "subject":  subject,
        "text":     body_text,
        "html":     f"""
        <div style="font-family:Georgia,serif;font-size:15px;line-height:1.7;
                    max-width:600px;margin:0 auto;color:#1a1a1a;padding:20px">
          {html_body}
          <hr style="margin:32px 0;border:none;border-top:1px solid #e5e5e5">
          <p style="font-size:12px;color:#888">
            Sent via <a href="https://thirtysecondtimeout.com" style="color:#888">ThirtySecondTimeout.com</a> Coach Outreach
          </p>
        </div>""",
    }
    # remove None values
    payload = {k: v for k, v in payload.items() if v is not None}

    r = requests.post(
        "https://api.resend.com/emails",
        json=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ── Flask route — paste into roster_server.py ─────────────────────────
# from flask import request, jsonify   ← already imported in roster_server.py

def register_email_routes(app):
    """Call this from roster_server.py: register_email_routes(app)"""

    @app.route("/api/send-email", methods=["POST"])
    def send_email():
        data = request.get_json(force=True) or {}
        to       = (data.get("to")      or "").strip()
        to_name  = (data.get("toName")  or "").strip()
        school   = (data.get("school")  or "").strip()
        reply_to = (data.get("replyTo") or "").strip()
        subject  = (data.get("subject") or "").strip()
        body     = (data.get("body")    or "").strip()

        if not to or not subject or not body:
            return jsonify({"error": "to, subject, body required"}), 400
        if "@" not in to:
            return jsonify({"error": "invalid to address"}), 400

        log.info(f"Sending email to {to} ({school})")

        try:
            result = send_via_resend(to, to_name, reply_to, subject, body)
            return jsonify({"ok": True, "id": result.get("id")})
        except requests.HTTPError as e:
            log.error(f"Resend error: {e.response.text if e.response else e}")
            return jsonify({"error": str(e)}), 502
        except Exception as e:
            log.error(f"Send error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/email-status")
    def email_status():
        return jsonify({
            "resend_configured": bool(RESEND_API_KEY),
            "from": FROM_EMAIL,
        })
