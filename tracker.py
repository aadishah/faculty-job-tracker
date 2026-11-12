#!/usr/bin/env python3
"""Faculty job tracker.

Checks public sources for Assistant Professor openings in sociology,
anthropology, women's/gender studies and the wider social sciences in India:

  * Whose University?  (whoseuniversity.org, data CC BY-NC-SA 4.0)
                       central institutions, IITs/IIMs, major private universities
  * UGC CU-REC         (curec.samarth.ac.in) central universities
  * Watch list         recruitment pages of state commissions and state
                       universities listed in config.json

Each run writes docs/index.html (the dashboard), data/openings.json and
data/digest.md (only the openings that are new in this run), and optionally
emails the digest. Standard library only; works on Python 3.9+.
"""
import concurrent.futures
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
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) faculty-job-tracker (personal use)"
WU = "https://whoseuniversity.org"
CUREC = "https://curec.samarth.ac.in"
CUREC_BROWSE = CUREC + "/index.php/search/site/search"
TODAY = dt.date.today()
HIDDEN = "hidden"  # marker in seen.json for old notices found on a watch page's first check

OTHER_RANKS = re.compile(
    r"associate professor|professor of practice|\bprofessor\b|post-?doc|"
    r"research fellow|visiting|guest faculty|librarian|registrar",
    re.I,
)
# Watch-list pages: what counts as a faculty recruitment notice, and what does not.
NOTICE = re.compile(
    r"assistant\s+professor|asst\.?\s*prof|faculty\s+(recruitment|positions?|posts?)|"
    r"teaching\s+(posts?|positions?|faculty)|recruitment\s+of\s+(faculty|teachers|teaching)|"
    r"सहायक\s*प्राध्यापक|सहायक\s*आचार्य",
    re.I,
)
NOT_AN_AD = re.compile(
    r"result|roster|admit|answer\s*key|eligib|short-?\s?list|select(ed|ion)\s+list|cancel|"
    r"non[- ]?teaching|guest|syllabus|merit|score|candidature|reject|marks|provisional|scrutiny|"
    r"seniority|promotion|career advancement|\bCAS\b|ph\.?\s?d|call letter|objection|waiting list|"
    r"recommend|rank\s?list|interview\s+(schedule|list|letter|call|dates?|program(me)?|notice)|"
    r"exam(ination)?\s+(schedule|dates?|centre)|साक्षात्कार|परिणाम|निरस्त|चयन सूची",
    re.I,
)
INVITES = re.compile(r"invit|walk-?\s?in", re.I)  # overrides NOT_AN_AD: "applications invited for rank list"
# A notice that names one department or subject (and it is not one of ours) is skipped.
SUBJECT_SPECIFIC = re.compile(
    r"(department|dept\.?|school|centre|center|institute|faculty|vertical)\s+(of|for)\s|"
    r"(professors?|posts?|positions?)\s*(\([^)]*\)\s*)?(in|for)\s+"
    r"(?!(the\s+)?(various|different|several|universit|college|govern|aided|academic|session|engagement))",
    re.I,
)
OTHER_SUBJECT = re.compile(
    r"physics|chemistry|botany|zoology|mathematic|statistic|computer|engineering|commerce|\blaw\b|"
    r"medical|medicine|hindi|english|sanskrit|urdu|music|geology|geography|pharmac|nursing|"
    r"management|physical education|agricultur|dental|ayur|homoeo|unani|veterinar|biotech|microbio|"
    r"economics|history|political|psycholog|philosophy|library|polytechnic|"
    r"भौतिक|रसायन|वनस्पति|प्राणी|गणित|वाणिज्य|विधि|भूगोल|इतिहास|अर्थशास्त्र",
    re.I,
)
SECTOR = {"PrivateUniversity": "Private", "StateUniversity": "State"}  # everything else is Central


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
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def clean(fragment):
    """HTML fragment -> plain single-line text."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return re.sub(r"\s+", " ", text.replace("‍", "").replace("‌", "")).strip()


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


# ---------- sources ----------

def source_whoseuniversity(matcher):
    feed = json.loads(fetch(WU + "/data/current.json"))
    try:
        registry = {i["id"]: i for i in json.loads(fetch(WU + "/data/institutions_registry.json"))}
    except Exception:
        registry = {}
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
        rank = matcher.rank_label(" ".join([primary, secondary, " ".join(sp.get("ranks") or [])]), ad.get("post_type"))
        if not rank:
            continue
        inst = registry.get(ad.get("institution_id") or "", {})
        out.append({
            "key": "wu:" + str(ad.get("id")),
            "source": "Whose University?",
            "sector": SECTOR.get(inst.get("type"), "Central"),
            "institution": inst.get("name") or (ad.get("institution_id") or "").replace("-", " ").title(),
            "title": ad.get("title") or "",
            "rank": rank,
            "match": level,
            "matched": words,
            "closing_date": closing.isoformat() if closing else None,
            "posts": ad.get("number_of_posts"),
            "url": ad.get("original_url") or ad.get("info_url") or WU,
            "apply_url": ad.get("apply_url"),
            "note": inst.get("state") or "",
        })
    return out, "feed dated " + str(feed.get("generated_at", ""))[:10]


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
    reply = json.loads(fetch(CUREC + "/index.php/search/default/search", opener, body, {
        "X-CSRF-Token": token, "X-Requested-With": "XMLHttpRequest", "Referer": CUREC_BROWSE,
    }))
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
            "sector": "Central",
            "institution": job.get("institution_id") or "",
            "title": "%s — %s" % (job.get("post") or rank_name, dept),
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


def page_notices(page, base):
    """Pull (text, url) pairs that look like faculty recruitment notices out of a page."""
    chunks = [m.group(2) for m in re.finditer(r"<(tr|li)\b[^>]*>(.*?)</\1>", page, re.S | re.I)]
    chunks += re.findall(r"<a\b[^>]*>.*?</a>", page, re.S | re.I)
    found, urls, texts = [], set(), set()
    for chunk in chunks:
        hrefs = re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\']', chunk, re.I)
        text = clean(chunk)
        if not hrefs or not 12 <= len(text) <= 400:
            continue
        if not NOTICE.search(text) or (NOT_AN_AD.search(text) and not INVITES.search(text)):
            continue
        href = next((h for h in hrefs if ".pdf" in h.lower()), hrefs[0])
        href = html.unescape(href).strip()
        url = base if href.startswith(("#", "javascript")) else urllib.parse.urljoin(base, href)
        if text[:100] in texts or (url != base and url in urls):
            continue
        texts.add(text[:100])
        urls.add(url)
        found.append((text, url))
    return found


def is_current(where):
    """True when a notice looks recent: a date within 90 days, or failing that, this year."""
    dates = []
    for y, m, d in re.findall(r"\b(20\d\d)[-/.](\d{1,2})[-/.](\d{1,2})\b", where):
        dates.append((y, m, d))
    for d, m, y in re.findall(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d\d)\b", where):
        dates.append((y, m, d))
    for y, m in re.findall(r"/(20\d\d)/(\d\d)/", where):
        dates.append((y, m, 28))
    parsed = []
    for y, m, d in dates:
        try:
            parsed.append(dt.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    if parsed:
        return (TODAY - max(parsed)).days <= 90
    return any(int(y) >= TODAY.year for y in re.findall(r"\b(20\d\d)\b", where))


def source_watch(matcher, cfg, seen, previous):
    """Check each watch-list page. Old notices present on a page's first check are
    recorded as hidden unless they mention the current year, so only live and
    future notices are shown."""
    entries = cfg.get("watch") or []

    def check(entry):
        try:
            return entry, page_notices(fetch(entry["url"], timeout=30), entry["url"]), None
        except Exception as exc:
            return entry, [], str(exc)[:60]

    out, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(check, entries))
    for entry, notices, error in results:
        slug = re.sub(r"[^a-z0-9]+", "-", entry["name"].lower()).strip("-")
        prefix = "watch:%s:" % slug
        if error:
            failed.append(entry["name"])
            out += [o for k, o in previous.items() if k.startswith(prefix)]
            continue
        first_check = ("baseline:" + slug) not in seen
        for text, url in notices:
            link_text = urllib.parse.unquote(url)
            level, words = matcher.discipline(text + " " + re.sub(r"[_\-+]", " ", link_text))
            if not level and (OTHER_SUBJECT.search(text) or SUBJECT_SPECIFIC.search(text)):
                continue
            key = prefix + hashlib.sha1((text + url).encode()).hexdigest()[:16]
            if first_check and not is_current(text + " " + link_text):
                seen[key] = HIDDEN
            first_seen = parse_date(seen.get(key, ""))
            expired = first_seen and (TODAY - first_seen).days > cfg.get("watch_expire_days", 120)
            if seen.get(key) == HIDDEN or expired:
                continue
            out.append({
                "key": key,
                "source": "Watch list",
                "sector": entry.get("sector", "State"),
                "institution": entry["name"],
                "title": text[:220],
                "rank": cfg["rank"] if matcher.rank.search(text) or "सहायक" in text else "Faculty (rank not stated)",
                "match": level or "notice",
                "matched": words,
                "closing_date": None,
                "posts": None,
                "url": url,
                "apply_url": None,
                "note": entry.get("state", ""),
            })
        seen["baseline:" + slug] = seen.get("baseline:" + slug) or TODAY.isoformat()
    info = "%d of %d pages checked" % (len(entries) - len(failed), len(entries))
    if failed:
        info += " · could not reach: " + ", ".join(failed)
    return out, info


# ---------- output ----------

MATCH_LABEL = {
    "strong": "Direct match",
    "broad": "Social sciences (broad)",
    "mention": "Mentioned in ad text",
    "notice": "Recruitment notice — check subjects",
}
MATCH_ORDER = {"strong": 0, "broad": 1, "mention": 2, "notice": 3}


def sort_key(o):
    return (MATCH_ORDER[o["match"]], o["closing_date"] or "9999", o["institution"])


def days_left(o):
    d = parse_date(o["closing_date"])
    return (d - TODAY).days if d else None


def deadline_text(o):
    left = days_left(o)
    if left is None:
        return "Deadline not stated — check the ad"
    when = "today" if left == 0 else "in %d day%s" % (left, "" if left == 1 else "s")
    return "Closes %s (%s)" % (parse_date(o["closing_date"]).strftime("%d %b %Y"), when)


def render_html(openings, status, new_days):
    e = html.escape
    cards, n_new, n_soon = [], 0, 0
    for o in openings:
        left = days_left(o)
        soon = left is not None and left <= 7
        is_new = (TODAY - parse_date(o["first_seen"])).days < new_days
        n_new += is_new
        n_soon += soon
        links = '<a href="%s" target="_blank" rel="noopener">Open the ad</a>' % e(o["url"])
        if o.get("apply_url"):
            links += ' · <a href="%s" target="_blank" rel="noopener">Apply</a>' % e(o["apply_url"])
        meta = " · ".join(str(x) for x in (
            o["rank"],
            "%s post%s" % (o["posts"], "" if o["posts"] == 1 else "s") if o.get("posts") else "",
            o.get("note"),
        ) if x)
        cards.append(
            '<article class="card %s" data-sector="%s" data-match="%s" data-new="%d" data-soon="%d" data-text="%s">'
            '<div class="tags">%s<span class="tag %s">%s</span><span class="tag">%s</span><span class="tag">%s</span></div>'
            '<h2>%s</h2><p class="title">%s</p><p class="meta">%s</p>'
            '<p class="deadline%s">%s</p><p class="links">%s</p></article>'
            % (
                o["match"], e(o["sector"]), o["match"], is_new, soon,
                e(" ".join([o["institution"], o["title"], o.get("note") or ""]).lower()),
                '<span class="tag new">New</span>' if is_new else "",
                o["match"], e(MATCH_LABEL[o["match"]]), e(o["sector"]), e(o["source"]),
                e(o["institution"]), e(o["title"]), e(meta),
                " urgent" if soon else "", e(deadline_text(o)), links,
            )
        )
    sectors = "".join(
        '<button class="chip" data-sector="%s" aria-pressed="false">%s <span>%d</span></button>'
        % (s, s, sum(o["sector"] == s for o in openings))
        for s in ("Central", "State", "Private")
    )
    return PAGE % {
        "total": len(openings),
        "new": n_new,
        "soon": n_soon,
        "direct": sum(o["match"] == "strong" for o in openings),
        "updated": TODAY.strftime("%d %b %Y"),
        "sectors": sectors,
        "cards": "\n".join(cards),
        "status": "".join("<li><strong>%s</strong>: %s</li>" % (e(n), e(m)) for n, m in status),
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Faculty openings tracker</title>
<style>
:root{--bg:#f6f5f1;--card:#fff;--ink:#1d1d1b;--muted:#63625c;--line:#dedcd3;--accent:#0b5d51;--on:#fff;--warn:#a3320b;--new:#8a5a00;--newbg:#fdf0c8}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#21211e;--ink:#eeece4;--muted:#a3a198;--line:#3a3934;--accent:#6fd0bd;--on:#10201d;--warn:#ff9a76;--new:#ffd978;--newbg:#3d3210}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:800px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.5rem;margin:0 0 2px}.sub{color:var(--muted);margin:0 0 16px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:0 0 16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.stat b{display:block;font-size:1.6rem;line-height:1.2;font-variant-numeric:tabular-nums}.stat span{color:var(--muted);font-size:.8rem}
@media (max-width:520px){.stats{grid-template-columns:repeat(2,1fr)}}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 10px}
.chip{font:inherit;font-size:.9rem;padding:5px 12px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
.chip span{color:var(--muted);font-size:.8rem}
.chip[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:var(--on)}.chip[aria-pressed="true"] span{color:inherit}
.sep{width:1px;height:22px;background:var(--line)}
#q{font:inherit;flex:1 1 180px;min-width:0;padding:6px 12px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
.count{color:var(--muted);font-size:.9rem;margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:8px;padding:14px 16px;margin:0 0 12px}
.card[hidden]{display:none}.card.strong{border-left-color:var(--accent)}
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
<p class="sub">Assistant Professor posts in sociology, anthropology, gender studies and the social sciences · India · updated %(updated)s</p>
<div class="stats">
<div class="stat"><b>%(total)d</b><span>openings tracked</span></div>
<div class="stat"><b>%(direct)d</b><span>direct matches</span></div>
<div class="stat"><b>%(new)d</b><span>new this week</span></div>
<div class="stat"><b>%(soon)d</b><span>closing within 7 days</span></div>
</div>
<div class="controls">
<button class="chip" data-sector="all" aria-pressed="true">All</button>%(sectors)s
<span class="sep"></span>
<button class="chip" data-flag="direct" aria-pressed="false">Direct matches</button>
<button class="chip" data-flag="fresh" aria-pressed="false">New</button>
<button class="chip" data-flag="soon" aria-pressed="false">Closing soon</button>
<input id="q" type="search" placeholder="Search university, subject or state" aria-label="Search openings">
</div>
<p class="count">Showing <span id="shown">%(total)d</span> of %(total)d · direct matches first, then by closing date</p>
%(cards)s
<p class="empty" id="none" hidden>No openings match these filters.</p>
<footer>
<p>Deadlines are read automatically and can be wrong. Always check the original advertisement before applying.
Recruitment notices from state pages are not filtered by subject unless the notice names one.</p>
<ul>%(status)s</ul>
<p>Sources: <a href="https://whoseuniversity.org/">Whose University?</a> (data CC BY-NC-SA 4.0, accessed %(updated)s), the
<a href="https://curec.samarth.ac.in/index.php/search/site/search">UGC CU-REC portal</a>, and official state commission and
university recruitment pages. Personal, non-commercial use.</p>
</footer></main>
<script>
(function(){
var cards=[].slice.call(document.querySelectorAll('.card'));
var f={sector:'all',direct:false,fresh:false,soon:false,q:''};
function apply(){
  var n=0;
  cards.forEach(function(c){
    var d=c.dataset;
    var ok=(f.sector==='all'||d.sector===f.sector)&&(!f.direct||d.match==='strong')&&(!f.fresh||d.new==='1')&&(!f.soon||d.soon==='1')&&(!f.q||d.text.indexOf(f.q)>-1);
    c.hidden=!ok; if(ok)n++;
  });
  document.getElementById('shown').textContent=n;
  document.getElementById('none').hidden=n>0;
}
[].forEach.call(document.querySelectorAll('.chip'),function(b){
  b.addEventListener('click',function(){
    if(b.dataset.sector){
      f.sector=b.dataset.sector;
      [].forEach.call(document.querySelectorAll('.chip[data-sector]'),function(x){x.setAttribute('aria-pressed',x===b?'true':'false');});
    }else{
      f[b.dataset.flag]=!f[b.dataset.flag];
      b.setAttribute('aria-pressed',f[b.dataset.flag]?'true':'false');
    }
    apply();
  });
});
document.getElementById('q').addEventListener('input',function(ev){f.q=ev.target.value.trim().toLowerCase();apply();});
apply();
})();
</script>
</body></html>
"""


def render_digest(new_items, total):
    lines = ["%d new faculty opening%s (%d being tracked in total)" % (
        len(new_items), "" if len(new_items) == 1 else "s", total), ""]
    for o in new_items:
        lines += [
            "%s — %s" % (o["institution"], o["title"]),
            "  %s · %s · %s · via %s" % (o["rank"], MATCH_LABEL[o["match"]], o["sector"], o["source"]),
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
        "watch": ("State watch list", "watch:", lambda: source_watch(matcher, cfg, seen, previous)),
    }
    openings, status, failures = [], [], 0
    for name, (label, prefix, run) in sources.items():
        if not cfg["sources"].get(name):
            continue
        try:
            items, info = run()
            openings += items
            status.append((label, "%d shown · %s" % (len(items), info)))
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
