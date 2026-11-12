# Faculty job tracker

Checks for Assistant Professor openings in sociology, anthropology, women's and
gender studies, and the wider social sciences at Indian universities — central,
state and private.

## Sources

| Source | What it covers | How it is read |
| --- | --- | --- |
| [Whose University?](https://whoseuniversity.org/) | Central universities, IITs, IIMs, IISERs, and 13 major private universities | Its public data feed (`data/current.json`) |
| [UGC CU-REC](https://curec.samarth.ac.in/index.php/search/site/search) | Central universities | The portal's own search, filtered to Assistant Professor |
| Watch list | 27 state commission and state university recruitment pages (listed in `config.json`) | Each page is scanned for faculty recruitment notices |

Whose University? data is CC BY-NC-SA 4.0: personal, non-commercial use with credit.

## Run it

```bash
python3 tracker.py
```

No installs needed (Python 3.9+). Each run writes:

- `docs/index.html` — the dashboard: counts, filters (Central / State / Private, direct matches, new, closing soon), search, and every current match
- `data/digest.md` — only the openings that are new since the last run
- `data/openings.json`, `data/seen.json` — the tracker's memory

## How matching works

A posting is kept when it is an open advertisement, is for an Assistant Professor
(or a faculty call that names no rank), and matches a keyword in `config.json`:

- **Direct match** — sociology, anthropology, women's studies, gender studies in the title or department
- **Social sciences (broad)** — e.g. "School of Humanities and Social Sciences"
- **Mentioned in ad text** — a direct keyword appears only in the body of the ad
- **Recruitment notice — check subjects** — a state page announced faculty recruitment
  without naming a subject (the subject list is usually inside the PDF)

Edit `config.json` to change keywords. They are regular expressions, so `sociolog`
matches both "Sociology" and "Sociological".

### State watch list

State pages have no common format, so the tracker reads each page's links and keeps
those that look like a faculty recruitment advertisement. It drops results, rank
lists, interview schedules, guest-faculty calls and notices that name another subject.

- The first time a page is checked, old notices are hidden unless they are dated
  within the last 90 days (or, with no date, mention the current year).
- After that, anything new on the page is shown, and drops off after 120 days.
- To watch another page, add `{"name": ..., "state": ..., "url": ...}` to `watch` in `config.json`.

## Daily checks and email (optional)

`.github/workflows/daily.yml` runs the tracker every day at 8 am India time once
this folder is pushed to a GitHub repository.

- **Page:** turn on GitHub Pages for the `docs/` folder to get a link to bookmark.
- **Email:** add these repository secrets and a digest is sent whenever something new appears:
  `SMTP_HOST`, `SMTP_PORT` (465), `SMTP_USER`, `SMTP_PASS`, `MAIL_TO`.
  For Gmail, use `smtp.gmail.com` and an app password, not the account password.

## Limits

- Most ads on Whose University? have no machine-readable deadline; the page says so. Always open the original ad.
- CU-REC has no public link per post, so its entries link to the portal's browse page.
- State coverage is a sample, not every state university. Some sites could not be read
  automatically and are not watched: Maharashtra PSC, Rajasthan PSC, Haryana PSC,
  Kerala PSC, and the universities of Calcutta, Mysore, Rajasthan and Patna.
- State notices are matched on the link text only; the tracker does not read the PDFs.
