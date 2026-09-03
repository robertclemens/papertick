"""Reads published IRA contribution limits back from the IRS.

The seed table in `init_db.IRS_LIMITS` is typed in by hand, and
`irs.ensure_limits` carries the newest known year forward as a *projection* so
the app never has a year with no limit at all. Neither of those notices when
the IRS publishes something different. This module closes that loop: it reads
the official figures and reconciles them against what the database holds.

Sources, in the order they are tried:

  1. **COLA table** — /retirement-plans/cola-increases-for-dollar-limitations-
     on-benefits-and-contributions. A real HTML table, years across the header
     and one row each for the limit and the catch-up, covering the last four
     tax years. This is where the next year's numbers land first, in the
     October/November news release, so it is the primary.

  2. **Publication 590-A** — /publications/p590a. Prose rather than a table,
     but anchored on a sentence the publication has used unchanged for years
     ("$7,000, or $8,000 if you were age 50 or older by the end of 2025"), and
     it is the authority Pub 590-A actually *is*. It lags: 590-A for a tax
     year is published after that year ends, so it will not carry the upcoming
     year the COLA page already shows. That is exactly why it is the backup
     and not the primary.

The IRS "Retirement topics - IRA contribution limits" page is deliberately NOT
in the chain. It is official, but its worked examples are years out of date
(they still cite 2020 figures), so a parser pointed at it would confidently
return a stale number — worse than returning nothing.

**Fail closed.** Every parsed figure is bounds-checked before it is allowed
out of this module (plausible range, IRS $500 indexing step, catch-up not
exceeding the limit). Anything that does not parse cleanly is dropped rather
than guessed at, and a source that yields nothing usable is treated as a
failure so the chain moves on. A wrong limit here would silently mis-enforce
contributions for a whole tax year, which is far worse than not knowing.

**This does not poll.** The number it is looking for changes once a year, so
running year-round would be ~50 wasted requests to learn one fact. The beat
only fires weekly from November through January — the window the COLA release
has landed in every recent year, plus a tail in case the IRS is late — and
`refresh_needed` stops it even inside that window as soon as the upcoming
year's figures are recorded as official. A settled year is not re-confirmed
every week; a deployment with nothing outstanding makes zero requests.
"""

import html
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select

from app.config import get_settings

log = logging.getLogger("papertick.irs_source")

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) PaperTick/1.0"}
_TIMEOUT = 20.0

COLA_URL = ("https://www.irs.gov/retirement-plans/"
            "cola-increases-for-dollar-limitations-on-benefits-and-contributions")
PUB590A_URL = "https://www.irs.gov/publications/p590a"

# The first year this platform models. Anything the parser reports outside
# [EARLIEST_YEAR, this year + 2] is a misread, not news.
EARLIEST_YEAR = 1997
FUTURE_HORIZON = 2

# Plausibility band. The IRA limit has run $2,000 -> $7,500 across the whole
# modelled history and is indexed in $500 steps; the catch-up has been $0,
# $500, $1,000 and $1,100. A figure outside these is a parse that latched onto
# the wrong cell (a phase-out threshold, a 401(k) limit, a dollar amount in an
# example) and must not reach the database.
MIN_LIMIT = Decimal("1000")
MAX_LIMIT = Decimal("100000")
LIMIT_STEP = Decimal("500")
MAX_CATCHUP = Decimal("10000")
CATCHUP_STEP = Decimal("100")


class IrsSourceError(RuntimeError):
    """No official source could be read. Raised so the caller can retry later
    rather than treating an outage as 'the limits are fine'."""


@dataclass(frozen=True)
class FetchedLimit:
    tax_year: int
    ira_limit: Decimal
    ira_catchup: Decimal
    source_name: str
    source_url: str


# --------------------------------------------------------------- html helpers

def _text(fragment: str) -> str:
    """Tag soup to a single line of plain text."""
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(fragment)).strip()


def _money(cell: str) -> Decimal | None:
    """'$7,500' / '1,100' / '$7,500 ' -> Decimal, or None when it is not a
    plain dollar figure. Non-breaking spaces are stripped: the IRS tables use
    them, and they are not whitespace to `strip()`."""
    cleaned = cell.replace("\xa0", " ").replace("$", "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _rows(table_html: str) -> list[list[str]]:
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I):
        cells = [_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
        if cells:
            out.append(cells)
    return out


def _fetch(url: str) -> str:
    with httpx.Client(timeout=_TIMEOUT, headers=_UA, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


# -------------------------------------------------------------- sanity gate

def _plausible(year: int, limit: Decimal, catchup: Decimal, today: date) -> bool:
    if not (EARLIEST_YEAR <= year <= today.year + FUTURE_HORIZON):
        return False
    if not (MIN_LIMIT <= limit <= MAX_LIMIT) or limit % LIMIT_STEP != 0:
        return False
    if not (Decimal("0") <= catchup <= MAX_CATCHUP) or catchup % CATCHUP_STEP != 0:
        return False
    # A catch-up larger than the base limit is never a real IRS figure; it is
    # the signature of a row pair that got mismatched by column.
    return catchup <= limit


def _keep(found: dict[int, FetchedLimit], year: int, limit: Decimal, catchup: Decimal,
          name: str, url: str, today: date) -> None:
    if _plausible(year, limit, catchup, today):
        found[year] = FetchedLimit(year, limit, catchup, name, url)
    else:
        log.warning("irs_source: %s dropped implausible %d limit=%s catchup=%s",
                    name, year, limit, catchup)


# ----------------------------------------------------------------- sources

def parse_cola(page: str, today: date | None = None) -> dict[int, FetchedLimit]:
    """The COLA page's IRA table: years across the header, one row for the
    limit and one for the catch-up."""
    today = today or date.today()
    found: dict[int, FetchedLimit] = {}
    for table in re.findall(r"<table.*?</table>", page, re.S | re.I):
        rows = _rows(table)
        if not any(re.search(r"(?i)ira contribution limit", " ".join(r)) for r in rows):
            continue
        # Header: the row whose cells are mostly four-digit years. Its column
        # positions are what every later row is read against, so a row with a
        # different cell count is skipped rather than aligned by guesswork.
        years: dict[int, int] = {}
        for row in rows:
            candidate = {i: int(c) for i, c in enumerate(row)
                         if re.fullmatch(r"(19|20)\d{2}", c.strip())}
            if len(candidate) >= 2:
                years = candidate
                break
        if not years:
            continue
        width = max(years) + 1
        limits: dict[int, Decimal] = {}
        catchups: dict[int, Decimal] = {}
        for row in rows:
            if len(row) < width:
                continue
            label = row[0].lower()
            target = (limits if re.search(r"ira contribution limit", label)
                      else catchups if re.search(r"ira catch-?up", label) else None)
            if target is None:
                continue
            for col, year in years.items():
                value = _money(row[col])
                if value is not None:
                    target[year] = value
        for year, limit in limits.items():
            # A year with a limit but no catch-up cell is an incomplete read of
            # the table, not a year with a $0 catch-up. Skip it: the pre-2002
            # zero is history, and this table only covers recent years.
            if year in catchups:
                _keep(found, year, limit, catchups[year], "cola", COLA_URL, today)
    return found


def parse_pub590a(page: str, today: date | None = None) -> dict[int, FetchedLimit]:
    """Pub 590-A's canonical sentence: '$7,000, or $8,000 if you were age 50 or
    older by the end of 2025'. The second figure is limit + catch-up, so the
    catch-up is their difference."""
    today = today or date.today()
    found: dict[int, FetchedLimit] = {}
    text = _text(page)
    pattern = re.compile(
        r"\$([\d,]+),?\s*or\s*\$([\d,]+)\s*if you (?:were|are)\s*age\s*50 or older"
        r"\s*by the end of\s*((?:19|20)\d{2})",
        re.I,
    )
    for base_s, combined_s, year_s in pattern.findall(text):
        base, combined = _money(base_s), _money(combined_s)
        if base is None or combined is None or combined < base:
            continue
        _keep(found, int(year_s), base, combined - base, "pub590a", PUB590A_URL, today)
    return found


#: (name, url, parser). Order is the fallback order.
SOURCES = [
    ("cola", COLA_URL, parse_cola),
    ("pub590a", PUB590A_URL, parse_pub590a),
]


def _configured_sources():
    """IRS_LIMIT_SOURCES, when set, restricts the chain to the named sources
    (comma-separated, in order). Empty means the full chain."""
    names = [n.strip().lower() for n in get_settings().irs_limit_sources.split(",") if n.strip()]
    if not names:
        return SOURCES
    by_name = {n: s for s in SOURCES for n in (s[0],)}
    chosen = [by_name[n] for n in names if n in by_name]
    return chosen or SOURCES


def fetch_official_limits(today: date | None = None) -> dict[int, FetchedLimit]:
    """Walk the source chain and return the first usable set of figures.

    Raises IrsSourceError when every source is unreachable or unparseable, so
    an outage is retried rather than mistaken for agreement.
    """
    today = today or date.today()
    failures: list[str] = []
    for name, url, parser in _configured_sources():
        try:
            found = parser(_fetch(url), today)
        except (httpx.HTTPError, ValueError, re.error) as exc:
            failures.append(f"{name}: {type(exc).__name__}")
            log.warning("irs_source: %s unreadable (%s)", name, exc)
            continue
        if found:
            log.info("irs_source: %s returned %d year(s): %s",
                     name, len(found), sorted(found))
            return found
        failures.append(f"{name}: no usable figures")
        log.warning("irs_source: %s parsed but yielded nothing usable", name)
    raise IrsSourceError("no IRS source could be read (" + "; ".join(failures) + ")")


# ------------------------------------------------------------- when to look

#: First month the IRS could plausibly have published next year's figures. The
#: COLA release has landed in this window every recent year (21 Oct 2022 for
#: tax year 2023; 1 Nov for 2024 and 2025; 13 Nov for 2026).
PUBLICATION_MONTH = 10


def latest_publishable_year(today: date) -> int:
    """The newest tax year whose official figures could already exist.

    The IRS publishes a tax year's limits in the autumn of the year *before*
    it. So from October onward, next year is fair game; before that, the newest
    knowable year is the current one. This is the bound that lets the refresh
    ever stop: without it, `ensure_limits` keeps a projected row for the
    year after next at all times, and a job that treated any projection as work
    to do would fetch forever chasing a figure that does not exist yet.
    """
    return today.year + 1 if today.month >= PUBLICATION_MONTH else today.year


def refresh_needed(db, today: date | None = None) -> tuple[bool, str]:
    """Whether there is anything a fetch could actually settle.

    True only while a year the IRS could already have published is still
    carrying a platform projection. Once the published figures land and that
    row flips to "official", this goes False and the job stops making requests
    — it does not keep re-confirming a settled year every week.

    Returns (needed, reason) so the reason can be logged either way.
    """
    from app.models import IrsLimit

    today = today or date.today()
    horizon = latest_publishable_year(today)
    pending = db.execute(
        select(IrsLimit.tax_year)
        .where(IrsLimit.source == "projected", IrsLimit.tax_year <= horizon)
        .order_by(IrsLimit.tax_year)
    ).scalars().all()
    if pending:
        return True, f"tax year(s) {pending} still projected (publishable through {horizon})"
    return False, f"nothing projected through tax year {horizon}"


# ------------------------------------------------------------- reconciliation

#: Check rows older than this are pruned on each run — long enough to see a
#: mismatch that was noticed late, short enough not to grow without bound.
CHECK_RETENTION_DAYS = 730


def reconcile_limits(db, today: date | None = None,
                     fetched: dict[int, FetchedLimit] | None = None) -> list:
    """Compare published figures against the stored limits and record the result.

    The policy, by what the stored row claims to be:

      - **missing** -> inserted from the source as official.
      - **projected** -> replaced by the official figures. This is the whole
        point of the job: `ensure_limits` carries the last known year forward
        so no year is ever blank, and this is what retires that guess.
      - **official** -> never overwritten. Agreement just stamps `verified_at`.
        Disagreement is recorded as a MISMATCH and logged loudly, and the
        stored value stands. A scraper is the less trustworthy of the two: if
        the markup shifts under it, silently rewriting a hand-checked limit
        would mis-enforce contributions for an entire tax year before anyone
        noticed. A person resolves it by correcting whichever side is wrong.

    Returns the IrsLimitCheck rows written.
    """
    from datetime import timedelta

    from app.models import IrsLimit, IrsLimitCheck, LimitCheckOutcome, utcnow
    from app.services.irs import tax_day

    today = today or date.today()
    now = utcnow()
    if fetched is None:
        fetched = fetch_official_limits(today)

    checks: list = []

    def record(year, outcome, found=None, detail=None):
        row = IrsLimitCheck(
            tax_year=year,
            outcome=outcome,
            source_name=found.source_name if found else None,
            source_url=found.source_url if found else None,
            found_limit=found.ira_limit if found else None,
            found_catchup=found.ira_catchup if found else None,
            detail=(detail or "")[:300] or None,
        )
        db.add(row)
        checks.append(row)

    for year in sorted(fetched):
        found = fetched[year]
        row = db.get(IrsLimit, year)

        if row is None:
            db.add(IrsLimit(
                tax_year=year,
                ira_limit=found.ira_limit,
                ira_catchup=found.ira_catchup,
                designation_deadline=tax_day(year + 1),
                source="official",
                verified_at=now,
                verified_from=found.source_url,
            ))
            record(year, LimitCheckOutcome.ADDED, found,
                   f"added {year} as ${found.ira_limit} + ${found.ira_catchup} catch-up")
            log.info("irs_source: added official %d limit from %s", year, found.source_name)
            continue

        same = (Decimal(row.ira_limit) == found.ira_limit
                and Decimal(row.ira_catchup) == found.ira_catchup)

        if row.source == "projected":
            was = f"${row.ira_limit} + ${row.ira_catchup}"
            row.ira_limit = found.ira_limit
            row.ira_catchup = found.ira_catchup
            row.designation_deadline = tax_day(year + 1)
            row.source = "official"
            row.verified_at = now
            row.verified_from = found.source_url
            record(year, LimitCheckOutcome.UPDATED, found,
                   f"projection {was} replaced by ${found.ira_limit} + "
                   f"${found.ira_catchup} catch-up")
            log.info("irs_source: %d projection replaced by official figures (%s)",
                     year, found.source_name)
            continue

        if same:
            row.verified_at = now
            row.verified_from = found.source_url
            record(year, LimitCheckOutcome.CONFIRMED, found)
            continue

        detail = (f"stored ${row.ira_limit} + ${row.ira_catchup} catch-up, "
                  f"{found.source_name} says ${found.ira_limit} + "
                  f"${found.ira_catchup}; stored value kept")
        record(year, LimitCheckOutcome.MISMATCH, found, detail)
        log.error("irs_source: MISMATCH for tax year %d — %s (%s). Stored figure "
                  "left in place; a person must resolve this.",
                  year, detail, found.source_url)

    cutoff = now - timedelta(days=CHECK_RETENTION_DAYS)
    db.query(IrsLimitCheck).filter(IrsLimitCheck.ran_at < cutoff).delete(
        synchronize_session=False)
    db.commit()
    return checks
