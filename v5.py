"""
FOURTEEN — Dan × Shamal
============================================================
A 14-day diet + movement pact. Two people, one number each per day.

Daily bar
  Dan     1800 NET calories
  Shamal  1800 TOTAL calories · 30 min cardio · 10 min strength

Miss a day: $50 fat tax + one embarrassing text to Armina.

Data lands in tabs ("dan_log", "shamal_log") inside the SAME Google Sheet
the old tracker uses — nothing existing is touched.

Deploy (Streamlit Community Cloud):
1. Put this file in a GitHub repo as streamlit_app.py
2. requirements.txt:  streamlit  gspread  google-auth  pandas
3. Secrets — reuse the existing:
     gcp_service_account_json = '''{ ...service account json... }'''
   Optional:
     spreadsheet_name    = "Streamlit Calories Tracker"
     challenge_start     = "2026-08-12"     # day 1
     app_password_sha256 = "<sha256 of your password>"
     dan_worksheet_name    = "dan_log"
     shamal_worksheet_name = "shamal_log"
"""

import hashlib
import hmac
import json
from datetime import date, datetime, timedelta

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(
    page_title="Fourteen Day Challenge — Dan × Shamal",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# =============================================================================
# The pact
# =============================================================================
CHALLENGE_DAYS = 14
DEFAULT_START = "2026-08-12"          # day 1 — override in secrets
CAL_CAP = 1800
CARDIO_MIN = 30
STRENGTH_MIN = 10
FINE = 50

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
DEFAULT_SPREADSHEET_NAME = "Streamlit Calories Tracker"

HEADERS = ["date", "calories", "cardio", "strength", "notes", "updated_at"]

PEOPLE = {
    "dan": {
        "name": "DAN",
        "cal_label": "NET CAL",
        "rule": f"{CAL_CAP} NET",
        "cardio": 0,            # no movement requirement — calories only
        "strength": 0,
        "ws_secret": "dan_worksheet_name",
        "ws_default": "dan_log",
    },
    "shamal": {
        "name": "SHAMAL",
        "cal_label": "TOTAL CAL",
        "rule": f"{CAL_CAP} TOTAL · {CARDIO_MIN} CARDIO · {STRENGTH_MIN} STRENGTH",
        "cardio": CARDIO_MIN,
        "strength": STRENGTH_MIN,
        "ws_secret": "shamal_worksheet_name",
        "ws_default": "shamal_log",
    },
}

# =============================================================================
# Access gate — same hash as the old app, so the same password keeps working.
# =============================================================================
PASSWORD_SHA256 = "79534a7c5a4e5d1b53b0658a9ae1781b20f41a8ce7dae38ee39b631d349ce54f"


def _password_ok(attempt: str) -> bool:
    attempt_hash = hashlib.sha256((attempt or "").encode("utf-8")).hexdigest()
    expected = st.secrets.get("app_password_sha256", PASSWORD_SHA256)
    return hmac.compare_digest(attempt_hash, expected)


def require_password():
    if st.session_state.get("auth_ok"):
        return
    st.markdown('<div class="gate">FOURTEEN DAY CHALLENGE</div>', unsafe_allow_html=True)
    with st.form("auth"):
        pw = st.text_input(
            "Password", type="password",
            label_visibility="collapsed", placeholder="PASSWORD",
        )
        ok = st.form_submit_button("ENTER", type="primary", use_container_width=True)
    if ok:
        if _password_ok(pw):
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("Wrong password. Try again.")
    st.stop()


# =============================================================================
# Helpers
# =============================================================================
def safe_int(value, default=0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def col_letter(n: int) -> str:
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _headers_are_additive(old, new) -> bool:
    return len(new) >= len(old) and new[: len(old)] == old


def start_date() -> date:
    raw = st.secrets.get("challenge_start", DEFAULT_START)
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return date.fromisoformat(DEFAULT_START)


def hit(person_key, row) -> bool:
    """A day counts only if that person's own targets are all cleared."""
    if row is None:
        return False
    cfg = PEOPLE[person_key]
    cal = safe_int(row.get("calories", 0))
    return (
        0 < cal <= CAL_CAP
        and safe_int(row.get("cardio", 0)) >= cfg["cardio"]
        and safe_int(row.get("strength", 0)) >= cfg["strength"]
    )


# =============================================================================
# Google Sheets
# =============================================================================
@st.cache_resource(show_spinner=False)
def get_gspread_client():
    if "gcp_service_account_json" not in st.secrets:
        st.error("Add `gcp_service_account_json` to Streamlit Secrets to connect the sheet.")
        st.stop()
    info = json.loads(st.secrets["gcp_service_account_json"])
    return gspread.authorize(Credentials.from_service_account_info(info, scopes=SCOPES))


@st.cache_resource(show_spinner=False)
def get_spreadsheet():
    gc = get_gspread_client()
    name = st.secrets.get("spreadsheet_name", DEFAULT_SPREADSHEET_NAME)
    try:
        return gc.open(name)
    except gspread.SpreadsheetNotFound:
        return gc.create(name)


@st.cache_resource(show_spinner=False)
def get_person_worksheet(person_key: str):
    """Get or create this person's tab. Additive schema changes upgrade in
    place; an unknown schema is left alone and a sibling tab is used."""
    ss = get_spreadsheet()
    cfg = PEOPLE[person_key]
    ws_name = st.secrets.get(cfg["ws_secret"], cfg["ws_default"])

    try:
        ws = ss.worksheet(ws_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=ws_name, rows=200, cols=len(HEADERS))
        ws.append_row(HEADERS)
        return ws

    current = ws.row_values(1)
    if not current:
        ws.append_row(HEADERS)
    elif current != HEADERS:
        if _headers_are_additive(current, HEADERS):
            if ws.col_count < len(HEADERS):
                ws.add_cols(len(HEADERS) - ws.col_count)
            ws.update(f"A1:{col_letter(len(HEADERS))}1", [HEADERS])
        else:
            safe_name = f"{ws.title}_v{int(datetime.now().timestamp())}"
            new_ws = ss.add_worksheet(title=safe_name, rows=200, cols=len(HEADERS))
            new_ws.append_row(HEADERS)
            return new_ws
    return ws


@st.cache_data(ttl=30, show_spinner=False)
def load_person_df(person_key: str, ws_title: str, cache_buster: str):
    ws = get_person_worksheet(person_key)
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=HEADERS)

    df = pd.DataFrame(records)
    for col in HEADERS:
        if col not in df.columns:
            df[col] = ""
    df = df[HEADERS].copy()
    df["date"] = df["date"].astype(str)
    for col in ("calories", "cardio", "strength"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    df["notes"] = df["notes"].astype(str)
    return df


def find_row(ws, iso):
    for idx, value in enumerate(ws.col_values(1), start=1):
        if value == iso:
            return idx
    return None


def save_entry(ws, day, calories, cardio, strength, notes):
    iso = day.isoformat()
    record = {
        "date": iso,
        "calories": safe_int(calories),
        "cardio": safe_int(cardio),
        "strength": safe_int(strength),
        "notes": str(notes).strip(),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    row = [record[c] for c in HEADERS]
    n = find_row(ws, iso)
    if n:
        ws.update(f"A{n}:{col_letter(len(HEADERS))}{n}", [row])
        return "Updated"
    ws.append_row(row)
    return "Saved"


def clear_entry(ws, day):
    n = find_row(ws, day.isoformat())
    if not n:
        return False
    ws.delete_rows(n)
    return True


# =============================================================================
# Styling — one typeface, one ink, no color. The 14-day grid does the talking.
# Everything is pinned light so phones in dark mode still read correctly.
# =============================================================================
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap');

    :root{ --ink:#111114; --mute:#93939c; --line:#e6e6ea; --paper:#ffffff; }
    html{ color-scheme:light; }

    html, body, [class*="css"], input, textarea, button, h1, h2, h3, p, table{
      font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace !important;
      color:var(--ink);
    }
    [data-testid="stApp"], [data-testid="stAppViewContainer"]{ background:var(--paper) !important; }
    header[data-testid="stHeader"]{ background:transparent; }
    #MainMenu, footer{ visibility:hidden; }
    .block-container{ max-width:660px; padding-top:2.6rem; padding-bottom:4rem; }
    hr{ border:none; border-top:1px solid var(--line); margin:1.7rem 0; }

    /* labels */
    div[data-testid="stWidgetLabel"] p{
      font-size:.6rem !important; font-weight:500 !important; letter-spacing:.14em;
      text-transform:uppercase; color:var(--mute) !important;
    }

    /* inputs */
    div[data-baseweb="input"], div[data-baseweb="textarea"]{
      background:var(--paper) !important; border-color:var(--line) !important; border-radius:0 !important;
    }
    div[data-baseweb="input"]:focus-within{ border-color:var(--ink) !important; box-shadow:none !important; }
    .stTextInput input, .stNumberInput input{
      background:var(--paper) !important; color:var(--ink) !important;
      -webkit-text-fill-color:var(--ink); caret-color:var(--ink);
      border-radius:0 !important; font-size:.9rem !important; font-weight:500;
    }
    .stTextInput input::placeholder, .stNumberInput input::placeholder{ color:var(--mute) !important; opacity:1; }
    [data-testid="stNumberInputStepUp"], [data-testid="stNumberInputStepDown"]{ display:none !important; }

    /* buttons */
    [data-testid^="stBaseButton"], [data-testid^="stBaseLinkButton"]{
      border-radius:0 !important; border:1px solid var(--line) !important;
      background:var(--paper) !important; color:var(--ink) !important;
      min-height:38px; box-shadow:none !important;
      font-size:.66rem !important; font-weight:500 !important; letter-spacing:.14em; text-transform:uppercase;
    }
    [data-testid^="stBaseButton"] p, [data-testid^="stBaseLinkButton"] p{ color:inherit !important; }
    [data-testid^="stBaseButton"]:hover, [data-testid^="stBaseLinkButton"]:hover{ border-color:var(--ink) !important; }
    button[kind="primary"], button[kind="primaryFormSubmit"],
    [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"]{
      background:var(--ink) !important; border-color:var(--ink) !important; color:#fff !important;
    }
    button[kind="primary"] p, button[kind="primaryFormSubmit"] p,
    [data-testid="stBaseButton-primary"] p, [data-testid="stBaseButton-primaryFormSubmit"] p{ color:#fff !important; }
    [data-testid^="stBaseButton"]:disabled{ opacity:.35; }

    /* alerts + toasts stay light */
    div[data-testid="stAlert"]{ background:var(--paper) !important; border:1px solid var(--line) !important; border-radius:0 !important; }
    div[data-testid="stAlert"] *{ color:var(--ink) !important; }
    div[data-testid="stToast"]{ background:var(--paper) !important; border:1px solid var(--ink); border-radius:0; }
    div[data-testid="stToast"] *{ color:var(--ink) !important; }

    /* masthead */
    .head{ display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:4px 14px; }
    .title{ font-size:.88rem; font-weight:600; letter-spacing:.17em; }
    .count{ font-size:.66rem; letter-spacing:.14em; color:var(--mute); }

    /* the grid — two rows of fourteen */
    .grid{
      display:grid; grid-template-columns:58px repeat(14,1fr);
      align-items:center; row-gap:9px; margin:26px 0 4px; min-width:330px;
    }
    .grid .rl{ font-size:.6rem; letter-spacing:.14em; color:var(--mute); }
    .grid .n{
      text-align:center; font-size:.6rem; color:var(--mute); text-decoration:none;
      padding:3px 0; letter-spacing:.02em;
    }
    .grid .n:hover{ color:var(--ink); }
    .grid .n.on{ color:var(--ink); font-weight:600; border-bottom:1px solid var(--ink); }
    .grid .n.now{ color:var(--ink); }
    .cell{ display:flex; justify-content:center; }
    .mk{ width:9px; height:9px; border-radius:50%; box-shadow:inset 0 0 0 1px var(--line); }
    .mk.done{ background:var(--ink); box-shadow:none; }
    .mk.fail{ box-shadow:inset 0 0 0 1px var(--ink); position:relative; }
    .mk.fail:after{
      content:""; position:absolute; left:-2px; top:4px; width:13px; height:1px;
      background:var(--ink); transform:rotate(-45deg);
    }
    .gridwrap{ overflow-x:auto; }

    /* dropdown — pinned light so the menu never renders dark-on-dark */
    div[data-baseweb="select"] > div{
      background:var(--paper) !important; border-color:var(--line) !important;
      border-radius:0 !important; min-height:38px;
    }
    div[data-baseweb="select"] div, div[data-baseweb="select"] span{
      color:var(--ink) !important; font-size:.66rem !important; letter-spacing:.12em;
    }
    div[data-baseweb="select"] svg{ fill:var(--ink) !important; }
    div[data-baseweb="popover"] div, div[data-baseweb="menu"], ul[role="listbox"]{
      background:var(--paper) !important; border-radius:0 !important;
    }
    li[role="option"]{ background:var(--paper) !important; }
    li[role="option"] *{ color:var(--ink) !important; font-size:.66rem !important; letter-spacing:.1em; }
    li[role="option"]:hover, li[aria-selected="true"]{ background:#f3f3f6 !important; }

    /* day chooser row */
    .daynav{ margin-top:22px; }

    /* panels */
    .who{
      border-top:1px solid var(--ink); padding-top:9px; margin-bottom:2px;
      display:flex; justify-content:space-between; align-items:baseline; gap:10px;
    }
    .who b{ font-size:.78rem; font-weight:600; letter-spacing:.18em; }
    .who span{ font-size:.55rem; letter-spacing:.1em; color:var(--mute); text-align:right; }
    .verdict{ font-size:.62rem; letter-spacing:.16em; margin:8px 0 10px; color:var(--mute); }
    .verdict b{ color:var(--ink); font-weight:600; }

    /* tally + ledger */
    .tally{ display:flex; justify-content:space-between; font-size:.66rem; letter-spacing:.1em; padding:9px 0; border-bottom:1px solid var(--line); }
    .tally span{ color:var(--mute); }
    .tally b{ font-weight:600; }
    .ledwrap{ overflow-x:auto; }
    table.led{ width:100%; min-width:352px; border-collapse:collapse; }
    .led th{
      font-size:.52rem; letter-spacing:.11em; color:var(--mute); text-transform:uppercase;
      font-weight:500; text-align:right; padding:0 0 7px 8px; white-space:nowrap;
    }
    .led th.grp{
      font-size:.6rem; letter-spacing:.16em; color:var(--ink); font-weight:600;
      text-align:left; padding-bottom:6px;
    }
    .led th.k, .led th.d{ text-align:left; padding-left:0; border-bottom:1px solid var(--line); }
    .led thead tr:last-child th{ border-bottom:1px solid var(--line); }
    .led th.g, .led td.g{ border-left:1px solid var(--line); padding-left:11px; }
    .led td{
      font-size:.68rem; padding:7px 0 7px 8px; border-bottom:1px solid var(--line);
      text-align:right; white-space:nowrap;
    }
    .led td.k, .led td.d{ text-align:left; padding-left:0; color:var(--mute); }
    .led td.mark{ width:14px; padding-left:9px; }
    .led tr.now td{ color:var(--ink); font-weight:500; }
    .led tr:last-child td{ border-bottom:none; }
    .stake{
      font-size:.86rem; font-weight:500; letter-spacing:.06em; line-height:1.5;
      color:var(--ink); text-align:center; margin-top:36px;
    }
    .gate{ font-size:.9rem; font-weight:600; letter-spacing:.17em; text-align:center; margin:24vh 0 14px; }
    </style>
    """,
    unsafe_allow_html=True,
)

require_password()

# =============================================================================
# Data
# =============================================================================
if "cache_buster" not in st.session_state:
    st.session_state.cache_buster = datetime.now().isoformat()

try:
    sheet_url = get_spreadsheet().url
    ws_map, data = {}, {}
    for key in PEOPLE:
        ws_map[key] = get_person_worksheet(key)
        data[key] = load_person_df(key, ws_map[key].title, st.session_state.cache_buster)
except Exception as e:
    st.error("Can't reach the Google Sheet. Check the service account secret and try again.")
    st.exception(e)
    st.stop()

by_date = {k: {r["date"]: r for _, r in data[k].iterrows()} for k in PEOPLE}

today = date.today()
day_one = start_date()
days = [day_one + timedelta(days=i) for i in range(CHALLENGE_DAYS)]
last_day = days[-1]

# Selected day — clamped to the challenge window, carried in ?day=
def _valid(s):
    try:
        return date.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


default_day = min(max(today, day_one), last_day)
picked = _valid(st.query_params.get("day")) or _valid(st.session_state.get("sel")) or default_day
sel_date = min(max(picked, day_one), last_day)
sel_iso = sel_date.isoformat()
st.session_state.sel = sel_iso
sel_n = (sel_date - day_one).days + 1

if today < day_one:
    counter = f"STARTS {day_one.strftime('%b %d').upper()}"
elif today > last_day:
    counter = "COMPLETE"
else:
    counter = f"DAY {(today - day_one).days + 1:02d} / {CHALLENGE_DAYS}"

st.markdown(
    f'<div class="head"><div class="title">FOURTEEN DAY CHALLENGE</div><div class="count">{counter}</div></div>',
    unsafe_allow_html=True,
)

# =============================================================================
# The grid — day numbers navigate, marks show who cleared the bar
# =============================================================================
cells = ['<div class="grid"><div class="rl"></div>']
for i, d in enumerate(days, start=1):
    iso = d.isoformat()
    cls = "n" + (" on" if iso == sel_iso else "") + (" now" if d == today else "")
    cells.append(f'<a class="{cls}" href="?day={iso}" target="_self" title="{d.strftime("%b %d")}">{i:02d}</a>')

for k in PEOPLE:
    cells.append(f'<div class="rl">{PEOPLE[k]["name"]}</div>')
    for d in days:
        row = by_date[k].get(d.isoformat())
        if row is not None:
            mark = "done" if hit(k, row) else "fail"
        elif d < today:
            mark = "fail"
        else:
            mark = ""
        cells.append(f'<div class="cell"><div class="mk {mark}"></div></div>')
cells.append("</div>")
st.markdown('<div class="gridwrap">' + "".join(cells) + "</div>", unsafe_allow_html=True)


# =============================================================================
# Check in
# =============================================================================
def go_to(d):
    st.query_params["day"] = d.isoformat()
    st.rerun()


nav_l, nav_m, nav_r = st.columns([0.62, 4, 0.62])
with nav_l:
    if st.button("‹", disabled=sel_date <= day_one, use_container_width=True, key="prev_day"):
        go_to(sel_date - timedelta(days=1))
with nav_m:
    choice = st.selectbox(
        "DAY", days, index=sel_n - 1,
        format_func=lambda d: (
            f"DAY {(d - day_one).days + 1:02d} · {d.strftime('%a %b %d').upper()}"
            + ("  · TODAY" if d == today else "")
        ),
        label_visibility="collapsed", key=f"daypick_{sel_iso}",
    )
    if choice != sel_date:
        go_to(choice)
with nav_r:
    if st.button("›", disabled=sel_date >= last_day, use_container_width=True, key="next_day"):
        go_to(sel_date + timedelta(days=1))


def panel(key):
    cfg = PEOPLE[key]
    row = by_date[key].get(sel_iso)

    st.markdown(
        f'<div class="who"><b>{cfg["name"]}</b><span>{cfg["rule"]}</span></div>',
        unsafe_allow_html=True,
    )

    moves = bool(cfg["cardio"] or cfg["strength"])
    cols = st.columns(3) if moves else [st]

    cal = cols[0].number_input(
        cfg["cal_label"], min_value=0, max_value=20000, step=10,
        value=safe_int(row["calories"]) if row is not None else 0,
        key=f"cal_{key}_{sel_iso}",
    )
    if moves:
        cardio = cols[1].number_input(
            "CARDIO", min_value=0, max_value=600, step=5,
            value=safe_int(row["cardio"]) if row is not None else 0,
            key=f"car_{key}_{sel_iso}",
        )
        strength = cols[2].number_input(
            "STRENGTH", min_value=0, max_value=600, step=5,
            value=safe_int(row["strength"]) if row is not None else 0,
            key=f"str_{key}_{sel_iso}",
        )
    else:
        # No inputs shown, so keep whatever is already on the sheet.
        cardio = safe_int(row["cardio"]) if row is not None else 0
        strength = safe_int(row["strength"]) if row is not None else 0

    note = st.text_input(
        "NOTE", value=str(row["notes"]) if row is not None else "",
        placeholder="OPTIONAL", key=f"note_{key}_{sel_iso}",
    )

    if not (cal or cardio or strength):
        state = "NOT LOGGED"
    else:
        short = []
        if cal <= 0:
            short.append("NO CALS")
        elif cal > CAL_CAP:
            short.append(f"OVER BY {cal - CAL_CAP:,}")
        if cardio < cfg["cardio"]:
            short.append(f"CARDIO {cfg['cardio'] - cardio}")
        if strength < cfg["strength"]:
            short.append(f"STRENGTH {cfg['strength'] - strength}")
        state = "<b>CLEARED</b>" if not short else "<b>SHORT</b> · " + " · ".join(short)
    st.markdown(f'<div class="verdict">{state}</div>', unsafe_allow_html=True)

    b1, b2 = st.columns([2, 1])
    if b1.button("SAVE", type="primary", use_container_width=True, key=f"save_{key}_{sel_iso}"):
        verb = save_entry(ws_map[key], sel_date, cal, cardio, strength, note)
        st.session_state.cache_buster = datetime.now().isoformat()
        st.toast(f"{cfg['name']} — {verb.upper()} DAY {sel_n:02d}")
        st.rerun()
    if b2.button("CLEAR", disabled=row is None, use_container_width=True, key=f"del_{key}_{sel_iso}"):
        clear_entry(ws_map[key], sel_date)
        st.session_state.cache_buster = datetime.now().isoformat()
        st.rerun()


left, right = st.columns(2, gap="medium")
with left:
    panel("dan")
with right:
    panel("shamal")

st.divider()

# =============================================================================
# Tally — a day is decided once it's over, or once it's logged
# =============================================================================
def decided(k):
    out = []
    for d in days:
        if d > today:
            continue
        row = by_date[k].get(d.isoformat())
        if d < today or row is not None:
            out.append(hit(k, row))
    return out


for k in PEOPLE:
    results = decided(k)
    misses = results.count(False)
    st.markdown(
        f'<div class="tally"><b>{PEOPLE[k]["name"]}</b>'
        f'<span>{results.count(True)}/{CHALLENGE_DAYS} CLEARED · '
        f'{misses} MISSED · <b>${misses * FINE}</b></span></div>',
        unsafe_allow_html=True,
    )

# =============================================================================
# Ledger
# =============================================================================
# =============================================================================
# Ledger — one labeled column per number, so nothing needs decoding
# =============================================================================
def has_moves(k) -> bool:
    return bool(PEOPLE[k]["cardio"] or PEOPLE[k]["strength"])


group_head, sub_head = "", ""
for k in PEOPLE:
    span = 4 if has_moves(k) else 2
    group_head += f'<th class="grp g" colspan="{span}">{PEOPLE[k]["name"]}</th>'
    sub_head += '<th class="g">CAL</th>'
    if has_moves(k):
        sub_head += "<th>CARDIO</th><th>STRENGTH</th>"
    sub_head += "<th></th>"

rows = []
for i, d in enumerate(days, start=1):
    iso = d.isoformat()
    tds = [f'<td class="k">{i:02d}</td>', f'<td class="d">{d.strftime("%b %d").upper()}</td>']
    for k in PEOPLE:
        row = by_date[k].get(iso)
        if row is None:
            tds.append('<td class="g">—</td>')
            if has_moves(k):
                tds.append("<td></td><td></td>")
            tds.append('<td class="mark"></td>')
        else:
            tds.append(f'<td class="g">{safe_int(row["calories"]):,}</td>')
            if has_moves(k):
                tds.append(
                    f'<td>{safe_int(row["cardio"])}</td><td>{safe_int(row["strength"])}</td>'
                )
            tds.append(f'<td class="mark">{"✓" if hit(k, row) else "✗"}</td>')
    rows.append(f'<tr class="{"now" if d == today else ""}">{"".join(tds)}</tr>')

st.markdown(
    '<div class="ledwrap"><table class="led"><thead>'
    f'<tr><th class="k" rowspan="2">#</th><th class="d" rowspan="2">DATE</th>{group_head}</tr>'
    f'<tr>{sub_head}</tr>'
    f'</thead><tbody>{"".join(rows)}</tbody></table></div>',
    unsafe_allow_html=True,
)

f1, f2 = st.columns(2)
with f1:
    st.link_button("OPEN SHEET", sheet_url, use_container_width=True)
with f2:
    if st.button("REFRESH", use_container_width=True):
        st.session_state.cache_buster = datetime.now().isoformat()
        st.rerun()

st.markdown(
    f'<div class="stake">EACH MISS is ${FINE}<br>and ONE TEXT TO ARMINA</div>',
    unsafe_allow_html=True,
)
