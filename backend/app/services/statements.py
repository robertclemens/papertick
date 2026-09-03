"""Account statements: monthly and year-end PDFs, archived immutably.

Statements are assembled by replaying the ledger to the period boundaries
(portfolio value at start/end, cash activity, trades, dividends, options) and
rendered with ReportLab on US-letter pages with the PaperTick masthead.
"""

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    Asset,
    Contribution,
    Dividend,
    OptionPosition,
    OptionTransaction,
    OrderSide,
    Statement,
    StatementKind,
    Transaction,
    User,
    utcnow,
)
from app.services.market_data import MarketDataError, market_data

log = logging.getLogger("papertick.statements")

ZERO = Decimal("0")
CENT = Decimal("0.01")

EMERALD = colors.HexColor("#0e9f6e")
INK = colors.HexColor("#1e293b")
MUTED = colors.HexColor("#64748b")
HAIRLINE = colors.HexColor("#d8dee6")
LIGHT = colors.HexColor("#f4f6f8")


def _m(v: Decimal | float | None) -> str:
    if v is None:
        return "—"
    n = float(v)
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


# ------------------------------------------------------------- ledger replay

class _PriceBook:
    """One full-range history fetch per ticker, shared across all the period
    snapshots a statement run needs."""

    def __init__(self, start: date):
        self.start = start
        self._series: dict[str, list[tuple[date, Decimal]]] = {}

    def close(self, ticker: str, d: date) -> Decimal | None:
        if ticker not in self._series:
            try:
                candles, _ = market_data.history(ticker, self.start - timedelta(days=10), date.today())
            except MarketDataError:
                candles = []
            self._series[ticker] = candles
        best: Decimal | None = None
        for cd, px in self._series[ticker]:
            if cd <= d:
                best = px
            else:
                break
        return best


_book: _PriceBook | None = None


def _close(ticker: str, d: date) -> Decimal | None:
    if _book is not None:
        return _book.close(ticker, d)
    try:
        return market_data.close_on(ticker, d)
    except MarketDataError:
        return None


def snapshot_value(db: Session, account_ids: list[str], d: date) -> Decimal:
    """Portfolio value (cash + stock positions + option marks) at end of day d."""
    if not account_ids:
        return ZERO
    cash = ZERO
    shares: dict[str, Decimal] = defaultdict(Decimal)
    for f in db.execute(select(Contribution).where(Contribution.account_id.in_(account_ids))).scalars():
        if f.timestamp.date() <= d:
            cash += Decimal(f.amount)
    for dv in db.execute(select(Dividend).where(Dividend.account_id.in_(account_ids))).scalars():
        if dv.event_date <= d:
            cash += Decimal(dv.amount)
    for t in db.execute(select(Transaction).where(Transaction.account_id.in_(account_ids))).scalars():
        s = Decimal(t.shares_filled)
        if t.as_of <= d:
            shares[t.ticker] += s if t.side == OrderSide.BUY else -s
        # cash moves on the actual execution date for backtests (metrics semantics)
        cash_date = t.executed_at.date() if t.as_of < t.executed_at.date() else t.as_of
        if cash_date <= d:
            if t.side == OrderSide.BUY:
                cash -= Decimal(t.gross_amount) + Decimal(t.fees)
            else:
                cash += Decimal(t.gross_amount) - Decimal(t.fees)
    for ot in db.execute(select(OptionTransaction).where(OptionTransaction.account_id.in_(account_ids))).scalars():
        if ot.as_of <= d:
            cash += Decimal(ot.cash_effect)
    value = cash
    for ticker, sh in shares.items():
        if sh:
            px = _close(ticker, d)
            if px is not None:
                value += sh * px
    # open option positions valued at intrinsic on the statement date
    for pos in db.execute(select(OptionPosition).where(OptionPosition.account_id.in_(account_ids))).scalars():
        if pos.created_at.date() <= d and pos.expiry > d:
            spot = _close(pos.underlying, d)
            if spot is None:
                continue
            strike = Decimal(pos.strike)
            intrinsic = max(ZERO, (spot - strike) if pos.right.value == "CALL" else (strike - spot))
            v = intrinsic * Decimal(pos.contracts) * 100
            value += v if pos.side.value == "LONG" else -v
    return value.quantize(CENT)


def holdings_at(db: Session, account_ids: list[str], d: date) -> list[dict]:
    """Positions at end of day d with FIFO-replay basis estimates."""
    txns = list(db.execute(
        select(Transaction).where(Transaction.account_id.in_(account_ids))
        .order_by(Transaction.as_of, Transaction.executed_at)
    ).scalars())
    lots: dict[str, list[list[Decimal]]] = defaultdict(list)  # ticker -> [shares, cost_ps]
    for t in txns:
        if t.as_of > d:
            continue
        s = Decimal(t.shares_filled)
        if t.side == OrderSide.BUY:
            cost = (Decimal(t.gross_amount) + Decimal(t.fees)) / s if s else ZERO
            lots[t.ticker].append([s, cost])
        else:
            rem = s
            for lot in lots[t.ticker]:
                if rem <= 0:
                    break
                take = min(lot[0], rem)
                lot[0] -= take
                rem -= take
    out = []
    for ticker, lot_list in sorted(lots.items()):
        sh = sum(l[0] for l in lot_list)
        if sh <= Decimal("0.000001"):
            continue
        basis = sum(l[0] * l[1] for l in lot_list)
        px = _close(ticker, d)
        asset = db.get(Asset, ticker)
        out.append({
            "ticker": ticker,
            "name": asset.name if asset else ticker,
            "shares": sh,
            "price": px,
            "value": (sh * px).quantize(CENT) if px else None,
            "basis": basis.quantize(CENT),
        })
    return out


def period_data(db: Session, user: User, start: date, end: date,
                scenario_id: str | None = None) -> dict:
    accounts = list(db.execute(
        select(Account).where(Account.user_id == user.id,
                              Account.scenario_id == scenario_id)
    ).scalars())
    ids = [a.id for a in accounts]
    flows = [f for f in db.execute(select(Contribution).where(Contribution.account_id.in_(ids))).scalars()
             if start <= f.timestamp.date() <= end] if ids else []
    divs = [dv for dv in db.execute(select(Dividend).where(Dividend.account_id.in_(ids))).scalars()
            if start <= dv.event_date <= end] if ids else []
    txns = [t for t in db.execute(
        select(Transaction).where(Transaction.account_id.in_(ids))
        .order_by(Transaction.executed_at)).scalars()
        if start <= t.executed_at.date() <= end] if ids else []
    otxns = [t for t in db.execute(
        select(OptionTransaction).where(OptionTransaction.account_id.in_(ids))
        .order_by(OptionTransaction.executed_at)).scalars()
        if start <= t.as_of <= end] if ids else []

    deposits = sum((Decimal(f.amount) for f in flows if Decimal(f.amount) > 0), ZERO)
    withdrawals = sum((-Decimal(f.amount) for f in flows if Decimal(f.amount) < 0), ZERO)
    dividends = sum((Decimal(dv.amount) for dv in divs), ZERO)
    fees = sum((Decimal(t.fees) for t in txns), ZERO) + sum((Decimal(t.fees) for t in otxns), ZERO)
    realized_st = sum((Decimal(t.realized_st or 0) for t in txns), ZERO) + \
        sum((Decimal(t.realized_st or 0) for t in otxns), ZERO)
    realized_lt = sum((Decimal(t.realized_lt or 0) for t in txns), ZERO) + \
        sum((Decimal(t.realized_lt or 0) for t in otxns), ZERO)

    # Past-dated fills that land inside this period. Counted on `as_of` rather
    # than on when they were entered, because that is what makes them matter
    # here: a fill entered in August for a June date never appears in June's
    # activity list, but it moves June's beginning and ending value. A statement
    # whose totals were produced with hindsight has to say so on its face.
    backdated_fills = len([
        t for t in (db.execute(
            select(Transaction).where(Transaction.account_id.in_(ids),
                                      Transaction.backdated.is_(True))
        ).scalars() if ids else [])
        if start <= t.as_of <= end
    ])

    return {
        "accounts": accounts,
        "backdated_fills": backdated_fills,
        "beginning": snapshot_value(db, ids, start - timedelta(days=1)),
        "ending": snapshot_value(db, ids, end),
        "deposits": deposits.quantize(CENT),
        "withdrawals": withdrawals.quantize(CENT),
        "dividends": dividends.quantize(CENT),
        "fees": fees.quantize(CENT),
        "realized_st": realized_st.quantize(CENT),
        "realized_lt": realized_lt.quantize(CENT),
        "flows": flows,
        "divs": divs,
        "txns": txns,
        "otxns": otxns,
        "holdings": holdings_at(db, ids, end),
        "account_names": {a.id: a.name for a in accounts},
    }


# ------------------------------------------------------------- PDF rendering

def _header_footer(kind_title: str, period_label: str, email: str):
    def draw(canvas, doc):
        canvas.saveState()
        w, h = letter
        # masthead
        canvas.setFillColor(EMERALD)
        canvas.roundRect(0.75 * inch, h - 0.95 * inch, 0.34 * inch, 0.34 * inch, 4, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 15)
        canvas.drawCentredString(0.92 * inch, h - 0.87 * inch, "P")
        canvas.setFillColor(INK)
        canvas.setFont("Helvetica-Bold", 16)
        canvas.drawString(1.2 * inch, h - 0.88 * inch, "PaperTick")
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(w - 0.75 * inch, h - 0.72 * inch, kind_title)
        canvas.drawRightString(w - 0.75 * inch, h - 0.86 * inch, period_label)
        canvas.drawRightString(w - 0.75 * inch, h - 1.00 * inch, email)
        canvas.setStrokeColor(HAIRLINE)
        canvas.setLineWidth(0.7)
        canvas.line(0.75 * inch, h - 1.12 * inch, w - 0.75 * inch, h - 1.12 * inch)
        # footer
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(
            0.75 * inch, 0.55 * inch,
            "PaperTick is a paper-trading simulation. No real money. Not investment, tax, or legal advice.",
        )
        canvas.drawRightString(w - 0.75 * inch, 0.55 * inch, f"Page {doc.page}")
        canvas.restoreState()
    return draw


_H2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, textColor=INK, spaceAfter=6)
_BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=8.5, textColor=INK, leading=11)
_NOTE = ParagraphStyle("note", fontName="Helvetica", fontSize=8, textColor=MUTED, leading=10)

_TABLE_STYLE = TableStyle([
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
    ("TEXTCOLOR", (0, 1), (-1, -1), INK),
    ("LINEBELOW", (0, 0), (-1, 0), 0.7, HAIRLINE),
    ("LINEBELOW", (0, 1), (-1, -2), 0.3, LIGHT),
    ("TOPPADDING", (0, 0), (-1, -1), 3.5),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
])


def _table(headers: list[str], rows: list[list[str]], widths: list[float],
           numeric_from: int = 1) -> Table:
    t = Table([headers] + rows, colWidths=[w * inch for w in widths], repeatRows=1)
    style = TableStyle(_TABLE_STYLE.getCommands())
    style.add("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT")
    t.setStyle(style)
    return t


def build_pdf(user: User, data: dict, kind: StatementKind, start: date, end: date) -> bytes:
    buf = BytesIO()
    kind_title = "Year-End Statement" if kind == StatementKind.YEAR_END else "Monthly Statement"
    period_label = (
        f"January 1 – December 31, {end.year}" if kind == StatementKind.YEAR_END
        else f"{start.strftime('%B %-d')} – {end.strftime('%B %-d, %Y')}"
    )
    doc = BaseDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                          topMargin=1.35 * inch, bottomMargin=0.8 * inch,
                          title=f"PaperTick {kind_title} {period_label}")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(
        id="page", frames=[frame],
        onPage=_header_footer(kind_title, period_label, user.email),
    )])

    change = data["ending"] - data["beginning"] - data["deposits"] + data["withdrawals"]
    story = [
        Paragraph("Portfolio summary", _H2),
        _table(
            ["", "Amount"],
            [
                ["Beginning value", _m(data["beginning"])],
                ["Deposits", _m(data["deposits"])],
                ["Withdrawals", _m(-data["withdrawals"]) if data["withdrawals"] else _m(ZERO)],
                ["Dividends received", _m(data["dividends"])],
                ["Change in investment value", _m(change - data["dividends"])],
                ["Ending value", _m(data["ending"])],
            ],
            [4.5, 2.0],
        ),
        Spacer(1, 14),
    ]

    if data.get("backdated_fills"):
        n = data["backdated_fills"]
        story.append(Paragraph(
            f"This period includes {n} past-dated trade{'s' if n != 1 else ''} — "
            "order(s) entered after the date they were filled on, and therefore "
            "placed with the outcome already known. The figures above include "
            "them. They are marked \u201cas of\u201d in the activity list below.",
            _NOTE,
        ))
        story.append(Spacer(1, 12))

    if data["holdings"]:
        story.append(Paragraph(f"Holdings as of {end.strftime('%B %-d, %Y')}", _H2))
        rows = [
            [h["ticker"], h["name"][:44], f"{h['shares']:,.4f}", _m(h["price"]), _m(h["value"]), _m(h["basis"])]
            for h in data["holdings"]
        ]
        story.append(_table(["Symbol", "Name", "Shares", "Price", "Value", "Cost basis"],
                            rows, [0.8, 2.5, 0.9, 0.8, 1.0, 1.0], numeric_from=2))
        story.append(Spacer(1, 14))

    activity_rows: list[list[str]] = []
    names = data["account_names"]
    for f in data["flows"]:
        # .get, not [...]: an unmapped kind must not take a statement down.
        # A conversion writes a signed pair, so the sign says which leg this is.
        label = {
            "CONTRIBUTION": "Deposit",
            "ROLLOVER": "Rollover in",
            "OPENING_BALANCE": "Opening balance",
            "WITHDRAWAL": "Withdrawal",
            "CONVERSION": "Roth conversion in" if Decimal(f.amount) > 0 else "Roth conversion out",
        }.get(f.kind.value, f.kind.value.title())
        desc = label + (f" (tax year {f.tax_year})" if f.tax_year else "")
        activity_rows.append([f.timestamp.date().isoformat(), names.get(f.account_id, ""), desc, _m(Decimal(f.amount))])
    for t in data["txns"]:
        desc = f"{t.side.value.title()} {Decimal(t.shares_filled):,.4f} {t.ticker} @ {_m(Decimal(t.executed_price))}"
        if t.as_of != t.executed_at.date():
            desc += f" (as of {t.as_of})"
        amt = -(Decimal(t.gross_amount) + Decimal(t.fees)) if t.side == OrderSide.BUY \
            else Decimal(t.gross_amount) - Decimal(t.fees)
        activity_rows.append([t.executed_at.date().isoformat(), names.get(t.account_id, ""), desc, _m(amt)])
    for dv in data["divs"]:
        activity_rows.append([dv.event_date.isoformat(), names.get(dv.account_id, ""),
                              f"Dividend {dv.ticker} ({Decimal(dv.shares):,.2f} sh)", _m(Decimal(dv.amount))])
    for ot in data["otxns"]:
        desc = (f"{ot.action.value.replace('_', ' ').title()} {ot.contracts}x "
                f"{ot.underlying} {ot.expiry} ${Decimal(ot.strike):,.2f} {ot.right.value}")
        activity_rows.append([ot.as_of.isoformat(), names.get(ot.account_id, ""), desc, _m(Decimal(ot.cash_effect))])
    activity_rows.sort(key=lambda r: r[0])

    if activity_rows:
        story.append(Paragraph("Activity", _H2))
        story.append(_table(["Date", "Account", "Description", "Amount"],
                            activity_rows, [0.9, 1.3, 3.5, 1.0], numeric_from=3))
        story.append(Spacer(1, 14))

    if kind == StatementKind.YEAR_END:
        story.append(Paragraph(f"Tax summary — {end.year}", _H2))
        story.append(_table(
            ["", "Amount"],
            [
                ["Short-term realized gains (incl. options)", _m(data["realized_st"])],
                ["Long-term realized gains (incl. options)", _m(data["realized_lt"])],
                ["Dividend income", _m(data["dividends"])],
                ["Fees and commissions", _m(data["fees"])],
                ["Deposits", _m(data["deposits"])],
                ["Withdrawals", _m(-data["withdrawals"]) if data["withdrawals"] else _m(ZERO)],
            ],
            [4.5, 2.0],
        ))
        story.append(Spacer(1, 8))
        story.append(Paragraph(
            "Gains are computed from tax-lot records (FIFO unless another method was elected). "
            "Dividend qualified/ordinary status is not determined. See the Taxes page for the "
            "full per-year breakdown including IRA contribution designations.", _NOTE,
        ))
    else:
        story.append(Paragraph(
            f"Realized gains this period: short-term {_m(data['realized_st'])}, "
            f"long-term {_m(data['realized_lt'])}. Fees: {_m(data['fees'])}.", _NOTE,
        ))

    doc.build(story)
    return buf.getvalue()


# ------------------------------------------------------------- generation

def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    return start, end


def first_activity(db: Session, user: User, scenario_id: str | None = None) -> date | None:
    ids = [a.id for a in db.execute(
        select(Account).where(Account.user_id == user.id,
                              Account.scenario_id == scenario_id)
    ).scalars()]
    if not ids:
        return None
    dates = []
    f = db.execute(select(Contribution.timestamp).where(Contribution.account_id.in_(ids))
                   .order_by(Contribution.timestamp).limit(1)).scalar_one_or_none()
    if f:
        dates.append(f.date())
    t = db.execute(select(Transaction.as_of).where(Transaction.account_id.in_(ids))
                   .order_by(Transaction.as_of).limit(1)).scalar_one_or_none()
    if t:
        dates.append(t)
    return min(dates) if dates else None


def regenerate_from(db: Session, user: User, since: date, today: date | None = None,
                    scenario_id: str | None = None) -> int:
    """Drop and re-render every statement whose period is touched by activity on
    or after `since`, then backfill. A past-dated fill changes the balances and
    realized gains inside periods that were already issued, so the archived PDFs
    would otherwise disagree with the ledger. Caller commits."""
    q = select(Statement).where(Statement.user_id == user.id, Statement.period_end >= since)
    if scenario_id:
        q = q.where(Statement.scenario_id == scenario_id)
    stale = db.execute(q).scalars().all()
    for statement in stale:
        db.delete(statement)
    db.flush()
    created = generate_missing(db, user, today, scenario_id)
    if stale:
        log.info("regenerated %d statement(s) for %s after a %s backdated trade",
                 len(stale), user.email, since.isoformat())
    return created


def generate_missing(db: Session, user: User, today: date | None = None,
                     scenario_id: str | None = None) -> int:
    """Create any missing monthly statements (completed months) and year-end
    statements (completed years) since the user's first activity."""
    global _book
    today = today or date.today()
    start_from = first_activity(db, user, scenario_id)
    if start_from is None:
        return 0
    _book = _PriceBook(start_from)
    q = select(Statement).where(Statement.user_id == user.id)
    if scenario_id:
        q = q.where(Statement.scenario_id == scenario_id)
    existing = {(s.kind, s.period_start) for s in db.execute(q).scalars()}
    created = 0
    y, m = start_from.year, start_from.month
    while (y, m) < (today.year, today.month):
        p_start, p_end = _month_bounds(y, m)
        if (StatementKind.MONTHLY, p_start) not in existing:
            data = period_data(db, user, p_start, p_end, scenario_id)
            pdf = build_pdf(user, data, StatementKind.MONTHLY, p_start, p_end)
            db.add(Statement(user_id=user.id, scenario_id=scenario_id,
                             kind=StatementKind.MONTHLY,
                             period_start=p_start, period_end=p_end, pdf=pdf))
            created += 1
        m += 1
        if m > 12:
            y, m = y + 1, 1
    for year in range(start_from.year, today.year):
        p_start, p_end = date(year, 1, 1), date(year, 12, 31)
        if (StatementKind.YEAR_END, p_start) not in existing:
            data = period_data(db, user, p_start, p_end, scenario_id)
            pdf = build_pdf(user, data, StatementKind.YEAR_END, p_start, p_end)
            db.add(Statement(user_id=user.id, scenario_id=scenario_id,
                             kind=StatementKind.YEAR_END,
                             period_start=p_start, period_end=p_end, pdf=pdf))
            created += 1
    _book = None
    if created:
        db.commit()
        log.info("generated %d statement(s) for %s", created, user.email)
    return created
