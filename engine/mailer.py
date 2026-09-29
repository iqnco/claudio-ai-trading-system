"""Email reports via Resend (https://resend.com). Falls back to Telegram if not configured.

secrets_local.py:  RESEND_API_KEY = "re_..."
config_local.py:   REPORT_EMAIL = "you@gmail.com"
                   REPORT_FROM  = "Claudio <claudio@your-verified-domain.com>"
                   (without a verified domain Resend only allows onboarding@resend.dev,
                    and only to the email address that owns the Resend account)

Layout rules, so every report reads well on a desktop and on a phone:
  - one column, 640px max, 16px body text; styles inline (Gmail and iOS Mail honour them)
  - any table wider than three columns is rendered as a stack of cards, never squeezed
  - numbers people act on (buy above, stop, P&L) are large; reasons are readable paragraphs
Call sites build bodies with a few class names (kpis/kpi, memo, muted, pos/neg) and
table(); page() turns those into inline-styled markup.
"""
import html
import os
import re
import sys

from . import notify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── palette & type ────────────────────────────────────────────────────────
INK, SOFT, MUTED, LINE, BG, CARD = "#15181c", "#3c434b", "#6b7280", "#e5e7eb", "#f3f4f6", "#ffffff"
POS, NEG, ACCENT, TINT = "#15803d", "#b91c1c", "#3b5bdb", "#f1f4fd"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

S = dict(
    h2=f"margin:30px 0 12px;font-size:19px;line-height:1.3;font-weight:700;color:{INK}",
    p=f"margin:0 0 14px;font-size:16px;line-height:1.55;color:{INK}",
    muted_p=f"margin:0 0 12px;font-size:14px;line-height:1.5;color:{MUTED}",
    muted=f"color:{MUTED}",
    memo=(f"margin:0 0 16px;padding:14px 16px;background:{TINT};border-left:4px solid {ACCENT};"
          f"border-radius:8px;font-size:16px;line-height:1.6;color:{INK}"),
    kpis="margin:6px 0 8px;font-size:0;line-height:0",
    kpi=(f"display:inline-block;vertical-align:top;box-sizing:border-box;width:31.3%;min-width:148px;"
         f"margin:0 2% 10px 0;padding:12px 14px;background:{BG};border-radius:10px"),
    kpi_label=(f"display:block;font-size:12px;line-height:1.3;letter-spacing:.04em;text-transform:uppercase;"
               f"color:{MUTED};margin-bottom:4px"),
    kpi_value=f"display:block;font-size:21px;line-height:1.25;font-weight:700;color:{INK}",
    pos=f"color:{POS};font-weight:600",
    neg=f"color:{NEG};font-weight:600",
    card=(f"margin:0 0 12px;padding:14px 16px;background:{CARD};border:1px solid {LINE};"
          f"border-radius:12px"),
    card_title=f"margin:0 0 8px;font-size:18px;line-height:1.3;font-weight:700;color:{INK}",
    tag=(f"display:inline-block;margin-left:8px;padding:2px 8px;border-radius:999px;background:{BG};"
         f"font-size:12px;font-weight:600;color:{SOFT};vertical-align:middle"),
    grid="width:100%;border-collapse:collapse;margin:0 0 4px",
    cell="padding:4px 10px 6px 0;vertical-align:top;width:33.3%",
    cell_label=f"display:block;font-size:12px;line-height:1.3;color:{MUTED};text-transform:uppercase;letter-spacing:.04em",
    cell_value=f"display:block;font-size:16px;line-height:1.35;font-weight:600;color:{INK}",
    long_label=f"margin:10px 0 2px;font-size:12px;color:{MUTED};text-transform:uppercase;letter-spacing:.04em",
    long_text=f"margin:0;font-size:15px;line-height:1.55;color:{SOFT}",
    table=f"width:100%;border-collapse:collapse;margin:0 0 14px;font-size:15px;line-height:1.45",
    th=(f"text-align:left;padding:8px 10px 8px 0;border-bottom:2px solid {LINE};font-size:12px;"
        f"color:{MUTED};text-transform:uppercase;letter-spacing:.04em;font-weight:600"),
    td=f"text-align:left;padding:10px 10px 10px 0;border-bottom:1px solid {LINE};vertical-align:top;color:{INK}",
)

# Columns that hold sentences, not values: shown as paragraphs under the card's numbers.
LONG = {"thesis", "why", "wrong if", "reason", "what", "invalidation", "note", "change"}


def _cfg():
    try:
        import secrets_local as s
        key = getattr(s, "RESEND_API_KEY", "") or ""
    except ImportError:
        return None
    try:
        import config_local as c
        to = getattr(c, "REPORT_EMAIL", "") or ""
        frm = getattr(c, "REPORT_FROM", "") or "Claudio <onboarding@resend.dev>"
    except ImportError:
        return None
    return (key, frm, to) if key and to else None


def configured():
    return _cfg() is not None


def _profile():
    try:
        from . import rules as R
        return R.PROFILE
    except Exception:
        return "luck"


def _label(subject):
    """Two engines share one inbox: CashMoney's mail says so up front."""
    return f"CashMoney · {subject}" if _profile() == "cash" else subject


def esc(x):
    return html.escape(str(x if x is not None else ""))


def _plain(cell):
    return re.sub(r"<[^>]+>", "", str(cell or "")).strip()


def _inline(body):
    """Turn the handful of class names call sites use into inline styles."""
    b = str(body)
    b = re.sub(r"<div class='kpi'><span class='muted'>(.*?)</span><b>(.*?)</b></div>",
               lambda m: (f"<div style=\"{S['kpi']}\"><span style=\"{S['kpi_label']}\">{m.group(1)}</span>"
                          f"<span style=\"{S['kpi_value']}\">{m.group(2)}</span></div>"), b, flags=re.S)
    reps = [("<div class='kpis'>", f"<div style=\"{S['kpis']}\">"),
            ("<div class='memo'>", f"<div style=\"{S['memo']}\">"),
            ("<p class='muted'>", f"<p style=\"{S['muted_p']}\">"),
            ("<span class='muted'>", f"<span style=\"{S['muted']}\">"),
            ("<span class='pos'>", f"<span style=\"{S['pos']}\">"),
            ("<span class='neg'>", f"<span style=\"{S['neg']}\">"),
            ("<h2>", f"<h2 style=\"{S['h2']}\">"),
            ("<p>", f"<p style=\"{S['p']}\">")]
    for a, z in reps:
        b = b.replace(a, z)
    return b


def page(title, body_html, subtitle=None):
    who = "CashMoney" if _profile() == "cash" else "Claudio · Luck"
    sub = (f"<div style=\"margin-top:4px;font-size:14px;color:{MUTED}\">{esc(subtitle)}</div>"
           if subtitle else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<style>@media (max-width:600px){{.wrap{{padding:18px 14px!important}}.outer{{padding:0!important}}}}</style>
</head><body style="margin:0;padding:0;background:{BG};-webkit-text-size-adjust:100%">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{BG}">
<tr><td class="outer" align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
 style="max-width:640px;background:{CARD};border-radius:14px;border:1px solid {LINE}">
<tr><td class="wrap" style="padding:26px 28px;font-family:{FONT};color:{INK}">
<div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:{ACCENT}">{esc(who)}</div>
<h1 style="margin:6px 0 0;font-size:24px;line-height:1.25;font-weight:800;color:{INK}">{esc(title)}</h1>{sub}
<div style="height:14px"></div>
{_inline(body_html)}
<div style="margin-top:28px;padding-top:14px;border-top:1px solid {LINE};font-size:12px;color:{MUTED}">
Sent by {esc(who)} on the host machine. Stops live at Schwab; nothing here needs a reply.</div>
</td></tr></table></td></tr></table></body></html>"""


def send(subject, body_html, text_fallback):
    """Email if configured; otherwise the plain-text version goes to Telegram."""
    title, subject = subject, _label(subject)
    cfg = _cfg()
    if not cfg:
        notify.telegram(f"{subject}\n\n{text_fallback}\n\n(Set RESEND_API_KEY in secrets_local.py and "
                        f"REPORT_EMAIL in config_local.py to get this by email.)")
        return False
    key, frm, to = cfg
    body = body_html if str(body_html).lstrip().startswith("<!doctype") else page(title, body_html)
    try:
        import requests
        r = requests.post("https://api.resend.com/emails", timeout=30,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"from": frm, "to": [to], "subject": subject,
                                "html": body, "text": text_fallback})
        if r.status_code >= 300:
            raise RuntimeError(f"Resend {r.status_code}: {r.text[:200]}")
        return True
    except Exception as e:
        notify.telegram(f"⚠️ Email failed ({notify.redact(e)}). {subject}\n\n{text_fallback[:3000]}")
        return False


def table(headers, rows):
    """Up to three columns: a clean table. Wider: one card per row, readable on a phone."""
    if len(headers) <= 3:
        head = "".join(f"<th style=\"{S['th']}\">{esc(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td style=\"{S['td']}\">{c}</td>" for c in r) + "</tr>"
                       for r in rows)
        return f"<table role=\"presentation\" style=\"{S['table']}\"><tr>{head}</tr>{body}</table>"
    return "".join(_card(headers, r) for r in rows)


def _card(headers, row):
    cols = list(zip([str(h) for h in headers], row))
    num = None
    if cols and cols[0][0] == "#":
        num, cols = _plain(cols[0][1]), cols[1:]
    ti = next((i for i, (h, _) in enumerate(cols) if h.lower() in ("ticker", "symbol")), 0)
    title = cols[ti][1]
    rest = cols[:ti] + cols[ti + 1:]
    tag = None
    for i, (h, v) in enumerate(rest):
        if h.lower() in ("setup", "decision", "action") and len(_plain(v)) <= 24:
            tag = v
            rest = rest[:i] + rest[i + 1:]
            break
    short = [(h, v) for h, v in rest if h.lower() not in LONG and len(_plain(v)) <= 40]
    long_ = [(h, v) for h, v in rest if (h, v) not in short and _plain(v)]
    head = (f"<div style=\"{S['card_title']}\">{(num + '. ') if num else ''}{title}"
            + (f"<span style=\"{S['tag']}\">{tag}</span>" if tag and _plain(tag) else "") + "</div>")
    grid = ""
    if short:
        cells = [f"<td style=\"{S['cell']}\"><span style=\"{S['cell_label']}\">{esc(h)}</span>"
                 f"<span style=\"{S['cell_value']}\">{v if _plain(v) else '–'}</span></td>" for h, v in short]
        while len(cells) % 3:
            cells.append(f"<td style=\"{S['cell']}\"></td>")
        grid = (f"<table role=\"presentation\" style=\"{S['grid']}\">"
                + "".join("<tr>" + "".join(cells[i:i + 3]) + "</tr>" for i in range(0, len(cells), 3))
                + "</table>")
    text = "".join(f"<div style=\"{S['long_label']}\">{esc(h)}</div><p style=\"{S['long_text']}\">{v}</p>"
                   for h, v in long_)
    return f"<div style=\"{S['card']}\">{head}{grid}{text}</div>"


def signed(x, fmt="{:+.2f}", suffix=""):
    if x is None:
        return ""
    cls = "pos" if x >= 0 else "neg"
    txt = fmt.format(x)
    if txt.startswith("$+") or txt.startswith("$-"):      # money reads -$135, not $-135
        txt = txt[1] + "$" + txt[2:]
    return f"<span class='{cls}'>{txt}{suffix}</span>"
