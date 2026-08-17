#!/usr/bin/env python3
"""
build3 Instagram DM bot - single bot, all three programs.

Env vars (set in Coolify, never in this repo):
  OPENAI_API_KEY   - required
  IG_APP_SECRET    - Instagram App Secret, used to verify webhook signatures
  IG_VERIFY_TOKEN  - arbitrary string, must match what's entered in the Meta
                     dashboard's webhook "Verify token" field
  IG_ACCESS_TOKEN  - Instagram account access token, used to send replies.
                     Leave unset during initial webhook verification; replies
                     will just be logged instead of sent until this is set.
  PORT             - defaults to 3000
"""
import http.server
import json
import os
import hmac
import hashlib
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
KB_DIR = os.path.join(ROOT, "kb")

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
IG_APP_SECRET = os.environ.get("IG_APP_SECRET", "")
IG_VERIFY_TOKEN = os.environ.get("IG_VERIFY_TOKEN", "")
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
PORT = int(os.environ.get("PORT", 3000))

UTM = "utm_source=igbot&utm_medium=juneigbotv1"
PROGRAM_LINKS = {
    "bia": f"https://build3bia.typeform.com/to/euUOBKEP?{UTM}",
    "bfb": f"https://build3bia.typeform.com/to/ECLapfzO?{UTM}",
    "ashram": f"https://binary.so/startup-ecoshrm?{UTM}",
}
PROGRAM_FILES = {
    "bia": "accelerator.md",
    "bfb": "fundraising-bootcamp.md",
    "ashram": "eco-ashram.md",
}


def load_kb(program):
    with open(os.path.join(KB_DIR, PROGRAM_FILES[program]), encoding="utf-8") as f:
        return f.read()


KB_CACHE = {p: load_kb(p) for p in PROGRAM_FILES}

SYSTEM_PROMPT = f"""You are the build3 Instagram DM assistant. build3 runs three programs -
the Bia Impact Accelerator (biA), the Fundraising Bootcamp (bFB), and the Startup Eco
Ashram. A founder has just opened a DM with you; you do not know yet which program they
mean. Read their first message and figure it out from context. If it's genuinely
ambiguous, ask one short question to find out rather than guessing or answering about the
wrong program.

Never blend facts across programs - each program below has its own prices, dates, and
guardrails, and they must not leak into each other's answers.

Follow whichever program's channel rules, voice, and guardrails apply once you know which
one you're in: no markdown, lowercase brand voice, short message bubbles (max ~3
sentences), never guess a figure or date not stated in that program's knowledge base.

When the founder asks how to apply, wants the form, says they're ready, or the
conversation reaches a natural point to move forward, send them exactly the matching link
below and nothing else in its place. Do not alter, shorten, or re-host any link. Do not
invent a different application URL.

- Bia Impact Accelerator application link: {PROGRAM_LINKS['bia']}
- Fundraising Bootcamp application link: {PROGRAM_LINKS['bfb']}
- Startup Eco Ashram link (the program's own apply flow is broken sitewide, so this
  careers/contact page is the working fallback): {PROGRAM_LINKS['ashram']}

=========================== BIA IMPACT ACCELERATOR KB ===========================
{KB_CACHE['bia']}

=========================== FUNDRAISING BOOTCAMP KB ===========================
{KB_CACHE['bfb']}

=========================== STARTUP ECO ASHRAM KB ===========================
{KB_CACHE['ashram']}
"""

# Ephemeral in-memory per-sender history. Resets on redeploy/restart - fine
# for now, upgrade to persistent storage (Supabase) before this matters.
HISTORY = {}
HISTORY_MAX_TURNS = 12


def call_openai(messages):
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "temperature": 0.4,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def send_instagram_reply(recipient_id, text):
    if not IG_ACCESS_TOKEN:
        print(f"[no IG_ACCESS_TOKEN set - would have replied to {recipient_id}]: {text}")
        return
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    req = urllib.request.Request(
        f"https://graph.instagram.com/v21.0/me/messages?access_token={IG_ACCESS_TOKEN}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print("sent reply:", resp.read().decode())
    except urllib.error.HTTPError as e:
        print("failed to send reply:", e.code, e.read().decode("utf-8", "ignore"))


def verify_signature(raw_body, signature_header):
    if not IG_APP_SECRET or not signature_header:
        return False
    expected = "sha256=" + hmac.new(IG_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/webhook":
            qs = parse_qs(parsed.query)
            mode = qs.get("hub.mode", [""])[0]
            token = qs.get("hub.verify_token", [""])[0]
            challenge = qs.get("hub.challenge", [""])[0]
            if mode == "subscribe" and token == IG_VERIFY_TOKEN:
                self._respond(200, challenge.encode(), "text/plain")
            else:
                self._respond(403, b"forbidden")
            return
        if parsed.path == "/health":
            self._respond(200, b"ok")
            return
        self._respond(404, b"not found")

    def do_POST(self):
        if self.path != "/webhook":
            self._respond(404, b"not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        sig = self.headers.get("X-Hub-Signature-256", "")
        if IG_APP_SECRET and not verify_signature(raw, sig):
            print("rejected webhook: bad signature")
            self._respond(403, b"forbidden")
            return

        # Ack immediately - Meta disables webhooks that don't 200 quickly.
        self._respond(200, b"EVENT_RECEIVED", "text/plain")

        try:
            self.handle_event(json.loads(raw))
        except Exception as e:
            print("error handling event:", e)

    def handle_event(self, body):
        for entry in body.get("entry", []):
            for msg_event in entry.get("messaging", []):
                sender_id = msg_event.get("sender", {}).get("id")
                text = msg_event.get("message", {}).get("text")
                if not sender_id or not text:
                    continue
                history = HISTORY.setdefault(sender_id, [])
                history.append({"role": "user", "content": text})
                del history[:-HISTORY_MAX_TURNS]
                try:
                    reply = call_openai(history)
                except Exception as e:
                    print("openai call failed:", e)
                    reply = "sorry, having a technical hiccup - try again in a moment."
                history.append({"role": "assistant", "content": reply})
                send_instagram_reply(sender_id, reply)

    def _respond(self, code, body, content_type="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[server]", fmt % args)


if __name__ == "__main__":
    print(f"system prompt size: {len(SYSTEM_PROMPT)} chars")
    print(f"serving on 0.0.0.0:{PORT}")
    http.server.HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
