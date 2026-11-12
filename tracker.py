#!/usr/bin/env python3
"""Faculty job tracker.

Checks two public sources for Assistant Professor openings in sociology,
anthropology, women's/gender studies and the wider social sciences:

  * Whose University?  (whoseuniversity.org, data CC BY-NC-SA 4.0)
  * UGC CU-REC         (curec.samarth.ac.in, central universities)

Each run writes docs/index.html (the page to read), data/openings.json and
data/digest.md (only the openings that are new in this run), and optionally
emails the digest. Standard library only; works on Python 3.9+.
"""
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import os
import re
import smtplib
import sys
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
UA = "Mozilla/5.0 (faculty-job-tracker; personal use)"
WU = "https://whoseuniversity.org"
CUREC = "https://curec.samarth.ac.in"
CUREC_BROWSE = CUREC + "/index.php/search/site/search"
TODAY = dt.date.today()

OTHER_RANKS = re.compile(
    r"associate professor|professor of practice|\bprofessor\b|post-?doc|"
    r"research fellow|visiting|guest faculty|librarian|registrar",
    re.I,
)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def fetch(url, opener=None, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=dict({"User-Agent": UA}, **(headers or {})))
    resp = (opener.open if opener else urllib.request.urlopen)(req, timeout=timeout)
    return resp.read().decode("utf-8", "replace")


def parse_date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class Matcher:
    """Decides whether a posting is relevant and how closely."""

    def __init__(self, cfg):
        join = lambda words: re.compile("|".join(words), re.I) if words else None
        self.strong = join(cfg["strong_keywords"])
        self.broad = join(cfg["broad_keywords"])
        self.exclude = join(cfg.get("exclude_keywords") or [])
        self.rank = re.compile(re.escape(cfg["rank"]), re.I)

    def discipline(self, primary, secondary=""):
        """Return (match_level, matched_words) or (None, [])."""
        if self.exclude and self.exclude.search(primary):
            return None, []
        for level, rx, text in (
            ("strong", self.strong, primary),
            ("broad", self.broad, primary),
            ("mention", self.strong, secondary),
        ):
            if rx and text:
                found = sorted({m.group(0).lower() for m in rx.finditer(text)})
                if found:
                    return level, found
        return None, []

    def rank_label(self, text, post_type):
        if self.rank.search(text):
            return self.rank.pattern.replace("\\", "")
        if post_type in ("Faculty", "Unknown") and not OTHER_RANKS.search(text):
            return "Faculty (rank not stated)"
        return None


def source_whoseuniversity(matcher):
    feed = json.loads(fetch(WU + "/data/current.json"))
    try:
        names = {i["id"]: i.get("name") or i["id"] for i in json.loads(fetch(WU + "/data/institutions_registry.json"))}
    except Exception:
        names = {}
    out = []
    for ad in feed.get("ads", []):
        if ad.get("status") != "active" or ad.get("document_class") not in ("advertisement", "corrigendum"):
            continue
        closing = parse_date(ad.get("closing_date"))
        if closing and closing < TODAY:
            continue
        sp = ad.get("structured_position") or {}
        primary = " | ".join(
            str(x)
            for x in (
                ad.get("title"), ad.get("department"), ad.get("discipline"),
                sp.get("department"), sp.get("discipline"), " ".join(sp.get("areas") or []),
            )
            if x
        )
        secondary = " | ".join(str(ad.get(k) or "") for k in ("raw_text_excerpt", "unit_eligibility"))
        level, words = matcher.discipline(primary, secondary)
        if not level:
            continue
        rank_text = " ".join([primary, secondary, " ".join(sp.get("ranks") or [])])
        rank = matcher.rank_label(rank_text, ad.get("post_type"))
        if not rank:
            continue
        inst = ad.get("institution_id") or ""
        out.append({
            "key": "wu:" + str(ad.get("id")),
            "source": "Whose University?",
            "institution": names.get(inst) or inst.replace("-", " ").title(),
            "title": ad.get("title") or "",
            "department": ad.get("department") or ad.get("discipline") or "",
            "rank": rank,
            "match": level,
            "matched": words,
            "closing_date": closing.isoformat() if closing else None,
            "posts": ad.get("number_of_posts"),
            "url": ad.get("original_url") or ad.get("info_url") or WU,
            "apply_url": ad.get("apply_url"),
        })
    return out, feed.get("generated_at", "")


def source_curec(matcher, rank_name):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    page = fetch(CUREC_BROWSE, opener)
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    block = re.search(r'id="designation_dropdown"[^>]*>(.*?)</select>', page, re.S).group(1)
    ids = [v for v, t in re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', block)
           if t.strip().lower() == rank_name.lower()]
    if not ids:
        raise RuntimeError("designation '%s' not offered by CU-REC" % rank_name)
    body = urllib.parse.urlencode([("_csrf", token)] + [("designation_data[]", i) for i in ids]).encode()
    raw = fetch(CUREC + "/index.php/search/default/search", opener, body, {
        "X-CSRF-Token": token, "X-Requested-With": "XMLHttpRequest", "Referer": CUREC_BROWSE,
    })
    reply = json.loads(raw)
    out = []
    for job in reply.get("data") or []:
        closing = parse_date(job.get("closeDate"))
        if not job.get("checkclosed") or (closing and closing < TODAY):
            continue
        dept = (job.get("department") or "").replace("_", " – ")
        level, words = matcher.discipline(dept)
        if not level:
            continue
        ident = "|".join(str(job.get(k) or "") for k in ("institution_id", "code", "postNo", "department"))
        out.append({
            "key": "curec:" + hashlib.sha1(ident.encode()).hexdigest()[:16],
            "source": "UGC CU-REC",
            "institution": job.get("institution_id") or "",
            "title": "%s — %s" % (job.get("post") or rank_name, dept),
            "department": dept,
            "rank": job.get("post") or rank_name,
            "match": level,
            "matched": words,
            "closing_date": closing.isoformat() if closing else None,
            "posts": job.get("totalVacancies"),
            "url": CUREC_BROWSE,
            "apply_url": None,
            "note": "Advt %s · Post %s · %s · %s" % (
                job.get("code") or "?", job.get("postNo") or "?",
                job.get("position_type") or "", job.get("state") or ""),
        })
    return out, "%d %s posts open on the portal" % (len(reply.get("data") or []), rank_name)


# ---------- output ----------

MATCH_LABEL = {"strong": "Direct match", "broad": "Social sciences (broad)", "mention": "Mentioned in ad text"}
MATCH_ORDER = {"strong": 0, "broad": 1, "mention": 2}


def sort_key(o):
    return (MATCH_ORDER[o["match"]], o["closing_date"] or "9999", o["institution"])


def deadline_text(o):
    d = parse_date(o["closing_date"])
    if not d:
        return "Deadline not stated — check the ad"
    left = (d - TODAY).days
    when = "today" if left == 0 else "in %d day%s" % (left, "" if left == 1 else "s")
    return "Closes %s (%s)" % (d.strftime("%d %b %Y"), when)


def render_html(openings, status, new_days):
    e = html.escape
    cards = []
    for o in openings:
        d = parse_date(o["closing_date"])
        urgent = d is not None and (d - TODAY).days <= 7
        is_new = (TODAY - parse_date(o["first_seen"])).days < new_days
        links = '<a href="%s">Open the ad</a>' % e(o["url"])
        if o.get("apply_url"):
            links += ' · <a href="%s">Apply</a>' % e(o["apply_url"])
        cards.append(
            '<article class="card %s">'
            '<div class="tags">%s<span class="tag %s">%s</span><span class="tag src">%s</span></div>'
            "<h2>%s</h2><p class=\"title\">%s</p>"
            '<p class="meta">%s%s%s</p>'
            '<p class="deadline%s">%s</p><p class="links">%s</p></article>'
            % (
                o["match"],
                '<span class="tag new">New</span>' if is_new else "",
                o["match"], e(MATCH_LABEL[o["match"]]), e(o["source"]),
                e(o["institution"]), e(o["title"]),
                e(o["rank"]),
                " · %s post%s" % (o["posts"], "" if o["posts"] == 1 else "s") if o.get("posts") else "",
                " · " + e(o["note"]) if o.get("note") else "",
                " urgent" if urgent else "", e(deadline_text(o)), links,
            )
        )
    status_rows = "".join(
        "<li><strong>%s</strong>: %s</li>" % (e(name), e(msg)) for name, msg in status
    )
    return PAGE % {
        "count": len(openings),
        "updated": TODAY.strftime("%d %b %Y"),
        "cards": "\n".join(cards) or '<p class="empty">No matching openings right now.</p>',
        "status": status_rows,
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Faculty openings tracker</title>
<style>
:root{--bg:#f6f5f1;--card:#fff;--ink:#1d1d1b;--muted:#63625c;--line:#dedcd3;--accent:#0b5d51;--warn:#a3320b;--new:#8a5a00;--newbg:#fdf0c8}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#21211e;--ink:#eeece4;--muted:#a3a198;--line:#3a3934;--accent:#6fd0bd;--warn:#ff9a76;--new:#ffd978;--newbg:#3d3210}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.5rem;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:8px;padding:14px 16px;margin:0 0 12px}
.card.strong{border-left-color:var(--accent)}
.card h2{font-size:1.05rem;margin:6px 0 2px}.card p{margin:2px 0}
.title{overflow-wrap:anywhere}.meta{color:var(--muted);font-size:.9rem;overflow-wrap:anywhere}
.deadline{font-weight:600}.deadline.urgent{color:var(--warn)}
.tags{display:flex;flex-wrap:wrap;gap:6px}.tag{font-size:.75rem;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.tag.new{background:var(--newbg);color:var(--new);border-color:transparent;font-weight:700}
.tag.strong{color:var(--accent);border-color:var(--accent)}
a{color:var(--accent)}footer{color:var(--muted);font-size:.85rem;margin-top:28px;border-top:1px solid var(--line);padding-top:14px}
footer ul{padding-left:18px;margin:6px 0}.empty{color:var(--muted)}
</style></head><body><main>
<h1>Faculty openings tracker</h1>
<p class="sub">%(count)d matching openings · updated %(updated)s · direct matches first, then by closing date</p>
%(cards)s
<footer>
<p>Deadlines are read automatically and can be wrong. Always check the original advertisement before applying.</p>
<ul>%(status)s</ul>
<p>Sources: <a href="https://whoseuniversity.org/">Whose University?</a> (data CC BY-NC-SA 4.0, accessed %(updated)s) and the
<a href="https://curec.samarth.ac.in/index.php/search/site/search">UGC CU-REC portal</a>. Personal, non-commercial use.</p>
</footer></main></body></html>
"""


def render_digest(new_items, total):
    lines = ["%d new faculty opening%s (%d being tracked in total)" % (
        len(new_items), "" if len(new_items) == 1 else "s", total), ""]
    for o in new_items:
        lines += [
            "%s — %s" % (o["institution"], o["title"]),
            "  %s · %s · via %s" % (o["rank"], MATCH_LABEL[o["match"]], o["source"]),
            "  " + deadline_text(o),
            "  " + o["url"],
            "",
        ]
    lines.append("Always check the original advertisement before applying.")
    return "\n".join(lines)


def send_email(subject, body):
    """Send the digest if SMTP settings are present in the environment."""
    need = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"]
    if not all(os.environ.get(k) for k in need):
        return "not configured (digest saved to data/digest.md only)"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ["SMTP_USER"], os.environ["MAIL_TO"]
    msg.set_content(body)
    with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "465"))) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        smtp.send_message(msg)
    return "sent to " + os.environ["MAIL_TO"]


def main():
    cfg = load_json(ROOT / "config.json", None)
    if not cfg:
        sys.exit("config.json is missing or invalid")
    matcher = Matcher(cfg)
    seen = load_json(DATA / "seen.json", {})
    previous = {o["key"]: o for o in load_json(DATA / "openings.json", [])}

    sources = {
        "whoseuniversity": ("Whose University?", "wu:", lambda: source_whoseuniversity(matcher)),
        "curec": ("UGC CU-REC", "curec:", lambda: source_curec(matcher, cfg["rank"])),
    }
    openings, status, failures = [], [], 0
    for name, (label, prefix, run) in sources.items():
        if not cfg["sources"].get(name):
            continue
        try:
            items, info = run()
            openings += items
            status.append((label, "%d matches · %s" % (len(items), info)))
        except Exception as exc:  # keep the last known results for a source that is down
            failures += 1
            kept = [o for k, o in previous.items() if k.startswith(prefix)]
            openings += kept
            status.append((label, "could not be checked today (%s); showing %d from the last run" % (exc, len(kept))))

    new_items = []
    for o in openings:
        if o["key"] not in seen:
            seen[o["key"]] = TODAY.isoformat()
            new_items.append(o)
        o["first_seen"] = seen[o["key"]]
    openings.sort(key=sort_key)
    new_items.sort(key=sort_key)

    DATA.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    (DATA / "seen.json").write_text(json.dumps(seen, indent=1, sort_keys=True), encoding="utf-8")
    (DATA / "openings.json").write_text(json.dumps(openings, indent=1, ensure_ascii=False), encoding="utf-8")
    (DOCS / "index.html").write_text(render_html(openings, status, cfg["new_badge_days"]), encoding="utf-8")
    digest = render_digest(new_items, len(openings)) if new_items else ""
    (DATA / "digest.md").write_text(digest, encoding="utf-8")

    for label, msg in status:
        print("%s: %s" % (label, msg))
    print("Tracking %d openings, %d new this run." % (len(openings), len(new_items)))
    if new_items:
        try:
            print("Email: " + send_email("%d new faculty opening(s)" % len(new_items), digest))
        except Exception as exc:
            print("Email failed: %s" % exc)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
