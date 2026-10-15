"""Optional: email the digest. Enabled only when the SMTP_* secrets are set.

Gmail example: SMTP_HOST=smtp.gmail.com, SMTP_PORT=465, SMTP_USER=you@gmail.com,
SMTP_PASSWORD=<16-char app password>, EMAIL_TO=careyssa@msu.edu
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from html import escape as e


def maybe_send(rec: dict, cfg: dict) -> None:
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("EMAIL_TO")
    if not (host and to):
        return
    site = os.environ.get("SITE_URL", "").rstrip("/")
    top = [p for p in rec["papers"] if p.get("summary")]
    parts = [f"<h2 style='font-family:Georgia,serif'>arXiv digest: {e(rec['date'])}</h2>",
             f"<p style='color:#666'>{rec['counts']['total']} new papers · {len(top)} summarized"
             + (f" · <a href='{site}/days/{rec['date']}.html'>open on the web</a>" if site else "") + "</p>"]
    for p in top:
        s = p["summary"]
        kr = "".join(f"<li>{e(x)}</li>" for x in s.get("key_results", []))
        parts.append(
            f"<div style='border-top:1px solid #ddd;padding:12px 0'>"
            f"<div style='font-size:12px;color:#b4451f'>{p['score']:.0f}/10 · {e(s.get('read_priority',''))} · {e(p.get('topic',''))}</div>"
            f"<h3 style='margin:4px 0;font-family:Georgia,serif'><a href='https://arxiv.org/abs/{p['id']}'>{e(p['title'])}</a></h3>"
            f"<div style='color:#666;font-size:13px'>{e(', '.join(p['authors'][:5]))}</div>"
            f"<p><b>{e(s.get('tldr'))}</b></p><p><i>Novel idea:</i> {e(s.get('novel_idea'))}</p>"
            f"<ul>{kr}</ul><p style='background:#eef3fb;padding:6px 10px'><i>Why it matters:</i> {e(s.get('why_it_matters'))}</p></div>")
    msg = EmailMessage()
    msg["Subject"] = f"arXiv digest {rec['date']}: {len(top)} papers for you"
    msg["From"] = os.environ.get("SMTP_USER", to)
    msg["To"] = to
    msg.set_content("HTML email; open in a mail client that renders HTML.")
    msg.add_alternative("".join(parts), subtype="html")
    port = int(os.environ.get("SMTP_PORT", "465"))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]); s.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]); s.send_message(msg)
        print(f"Emailed digest to {to}", file=sys.stderr)
    except Exception as ex:  # noqa: BLE001
        print(f"[email] failed: {ex}", file=sys.stderr)
