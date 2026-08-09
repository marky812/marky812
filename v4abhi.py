"""
Shred Sheet — Abhi x Shamal
============================================================
A two-person daily diet accountability tracker.

- Left panel  = Abhi   (indigo)
- Right panel = Shamal (copper)
- Each person logs calories, protein, fiber + a notes/progress entry per day.
- Data is saved to NEW worksheet tabs ("abhi_log", "shamal_log") inside the
  SAME Google Sheet your old tracker uses — nothing existing is touched.

Deploy (Streamlit Community Cloud):
1. Put this file in a GitHub repo as streamlit_app.py
2. Add requirements.txt with:  streamlit  gspread  google-auth  pandas  altair
3. In Streamlit "Secrets", reuse your existing:
     gcp_service_account_json = '''{ ...service account json... }'''
   Optional overrides:
     spreadsheet_name = "Streamlit Calories Tracker"
     app_password_sha256 = "<sha256 of your password>"
     abhi_worksheet_name = "abhi_log"
     shamal_worksheet_name = "shamal_log"
"""

import hashlib
import hmac
import json
from datetime import date, datetime, timedelta

import altair as alt
import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(
    page_title="Shred Sheet — Abhi × Shamal",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# =============================================================================
# Config
# =============================================================================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Same spreadsheet as your old tracker — new tabs get created inside it.
DEFAULT_SPREADSHEET_NAME = "Streamlit Calories Tracker"

HEADERS = ["date", "calories", "protein", "fiber", "notes", "updated_at"]

PEOPLE = {
    "abhi": {
        "name": "Abhi",
        "ws_secret": "abhi_worksheet_name",
        "ws_default": "abhi_log",
        "css": "a",
        "hex": "#4f46e5",
    },
    "shamal": {
        "name": "Shamal",
        "ws_secret": "shamal_worksheet_name",
        "ws_default": "shamal_log",
        "css": "b",
        "hex": "#c2410c",
    },
}

TREND_WINDOW_OPTIONS = [7, 14, 30, 60, 90]

# =============================================================================
# Access gate — same hash as the old app, so the same password keeps working.
# (Override with st.secrets["app_password_sha256"] if you want to rotate it.)
# =============================================================================
PASSWORD_SHA256 = "f16afbda6ac2d3b4a95b0d042a4d62a1b6ce2b1ada18cf5028bf3869fb5609d2"


def _password_ok(attempt: str) -> bool:
    attempt_hash = hashlib.sha256((attempt or "").encode("utf-8")).hexdigest()
    expected = st.secrets.get("app_password_sha256", PASSWORD_SHA256)
    return hmac.compare_digest(attempt_hash, expected)


def require_password():
    if st.session_state.get("auth_ok"):
        return

    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(
            """
            <div class="gate-wrap">
              <div class="gate-kicker">Abhi × Shamal</div>
              <div class="gate-title">Shred Sheet</div>
              <div class="gate-sub">Enter the password to continue.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.form("auth_form"):
            pw = st.text_input(
                "Password", type="password",
                label_visibility="collapsed", placeholder="Password",
            )
            ok = st.form_submit_button("Unlock", type="primary", use_container_width=True)
        if ok:
            if _password_ok(pw):
                st.session_state.auth_ok = True
                st.rerun()
            else:
                st.error("Incorrect password.")
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


def safe_float(value, default=0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def col_letter(n: int) -> str:
    """1-indexed column number -> spreadsheet column letters (e.g. 6 -> 'F')."""
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _headers_are_additive(old_headers, new_headers) -> bool:
    """True when new_headers only ADDS columns to the end of old_headers."""
    return len(new_headers) >= len(old_headers) and new_headers[: len(old_headers)] == old_headers


def calc_streak(logged_dates: set, today: date) -> int:
    """Consecutive days with an entry, counting back from today (or yesterday
    if today hasn't been logged yet, so the streak doesn't drop mid-day)."""
    d = today if today.isoformat() in logged_dates else today - timedelta(days=1)
    streak = 0
    while d.isoformat() in logged_dates:
        streak += 1
        d -= timedelta(days=1)
    return streak


def get_window_df(df, days: int):
    if df.empty:
        return df.assign(date_dt=pd.Series(dtype="datetime64[ns]"))

    end = pd.Timestamp(date.today())
    start = end - pd.Timedelta(days=days - 1)

    tmp = df.copy()
    tmp["date_dt"] = pd.to_datetime(tmp["date"], errors="coerce")
    tmp = tmp[(tmp["date_dt"] >= start) & (tmp["date_dt"] <= end)]
    return tmp.sort_values("date_dt")


# =============================================================================
# Google Sheets
# =============================================================================
@st.cache_resource(show_spinner=False)
def get_gspread_client():
    if "gcp_service_account_json" not in st.secrets:
        st.error("Missing `gcp_service_account_json` in Streamlit Secrets.")
        st.stop()

    service_account_info = json.loads(st.secrets["gcp_service_account_json"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return gspread.authorize(creds)


@st.cache_resource(show_spinner=False)
def get_spreadsheet():
    gc = get_gspread_client()
    spreadsheet_name = st.secrets.get("spreadsheet_name", DEFAULT_SPREADSHEET_NAME)
    try:
        return gc.open(spreadsheet_name)
    except gspread.SpreadsheetNotFound:
        # Created silently on first run; it belongs to the service account.
        return gc.create(spreadsheet_name)


@st.cache_resource(show_spinner=False)
def get_person_worksheet(person_key: str):
    """Get (or create) this person's tab in the shared spreadsheet.
    Existing tabs with other schemas are never cleared — same safety rules
    as the old tracker."""
    spreadsheet = get_spreadsheet()
    cfg = PEOPLE[person_key]
    ws_name = st.secrets.get(cfg["ws_secret"], cfg["ws_default"])

    try:
        ws = spreadsheet.worksheet(ws_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=ws_name, rows=1000, cols=len(HEADERS))
        ws.append_row(HEADERS)
        return ws

    current_headers = ws.row_values(1)
    if not current_headers:
        ws.append_row(HEADERS)
    elif current_headers != HEADERS:
        if _headers_are_additive(current_headers, HEADERS):
            if ws.col_count < len(HEADERS):
                ws.add_cols(len(HEADERS) - ws.col_count)
            ws.update(f"A1:{col_letter(len(HEADERS))}1", [HEADERS])
        else:
            # Unknown schema under this name — leave it alone, use a sibling.
            safe_name = f"{ws.title}_v{int(datetime.now().timestamp())}"
            new_ws = spreadsheet.add_worksheet(title=safe_name, rows=1000, cols=len(HEADERS))
            new_ws.append_row(HEADERS)
            return new_ws

    return ws


@st.cache_data(ttl=30, show_spinner=False)
def load_person_df(person_key: str, ws_title: str, cache_buster: str):
    # ws_title + cache_buster keep the cache honest across saves/deletes.
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
    df["calories"] = pd.to_numeric(df["calories"], errors="coerce").fillna(0).astype(int)
    df["protein"] = pd.to_numeric(df["protein"], errors="coerce").fillna(0.0).astype(float)
    df["fiber"] = pd.to_numeric(df["fiber"], errors="coerce").fillna(0.0).astype(float)
    df["notes"] = df["notes"].astype(str)
    df["updated_at"] = df["updated_at"].astype(str)
    return df


def find_row_number_by_date(worksheet, selected_date_str):
    dates = worksheet.col_values(1)
    for idx, value in enumerate(dates, start=1):
        if value == selected_date_str:
            return idx
    return None


def upsert_entry(worksheet, selected_date, calories, protein, fiber, notes):
    selected_date_str = selected_date.isoformat()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    row = [
        selected_date_str,
        max(0, safe_int(calories)),
        round(max(0.0, safe_float(protein)), 1),
        round(max(0.0, safe_float(fiber)), 1),
        str(notes).strip(),
        now,
    ]

    row_number = find_row_number_by_date(worksheet, selected_date_str)
    end = col_letter(len(HEADERS))

    if row_number:
        worksheet.update(f"A{row_number}:{end}{row_number}", [row])
        return "updated"

    worksheet.append_row(row)
    return "added"


def delete_entry(worksheet, selected_date):
    selected_date_str = selected_date.isoformat()
    row_number = find_row_number_by_date(worksheet, selected_date_str)
    if not row_number:
        return False
    worksheet.delete_rows(row_number)
    return True


# =============================================================================
# Styling — quiet, editorial, monochrome. Instrument Sans for text, IBM Plex
# Mono for every number, date and label. Color appears ONLY as the two-person
# code: indigo = Abhi, copper = Shamal — as 6px dots, 2px rules and chart ink.
# =============================================================================
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

    :root{
      --a:#4f46e5;            /* Abhi — indigo  */
      --b:#c2410c;            /* Shamal — copper */
      --ink:#18181d; --muted:#73737c; --faint:#a6a6ae;
      --line:#e7e7ea; --line-2:#d9d9df;
      --bg:#fafafa; --card:#ffffff;
      --mono:'IBM Plex Mono',ui-monospace,'SF Mono',Menlo,monospace;
    }

    html, body, [class*="css"], .stMarkdown, button, input, textarea, select,
    h1, h2, h3, h4, h5, h6{
      font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif !important;
      color:var(--ink);
    }
    .mono{ font-family:var(--mono) !important; }
    [data-testid="stAppViewContainer"]{ background:var(--bg); }
    header[data-testid="stHeader"]{ background:transparent; }
    .block-container{ padding-top:1.4rem; padding-bottom:3.5rem; max-width:1120px; }
    #MainMenu, footer{ visibility:hidden; }
    hr{ border:none; border-top:1px solid var(--line); margin:1.7rem 0 1.2rem; }

    /* widget labels + captions — small mono meta text */
    div[data-testid="stWidgetLabel"] p{
      font-family:var(--mono) !important; font-size:.63rem !important; font-weight:500 !important;
      text-transform:uppercase; letter-spacing:.1em; color:var(--muted) !important;
    }
    div[data-testid="stCaptionContainer"], div[data-testid="stCaptionContainer"] p{
      font-family:var(--mono) !important; font-size:.66rem !important; color:var(--faint) !important;
    }
    div[data-testid="stCaptionContainer"] code{
      font-family:var(--mono) !important; font-size:.66rem; color:var(--muted);
      background:transparent; padding:0;
    }

    /* buttons — flat, hairline, ink primary */
    [data-testid^="stBaseButton"], [data-testid^="stBaseLinkButton"]{
      border-radius:10px !important; border:1px solid var(--line-2) !important;
      background:var(--card) !important; color:var(--ink) !important;
      min-height:40px; font-weight:500 !important; font-size:.88rem !important;
      box-shadow:none !important; transition:border-color .12s ease, background .12s ease;
    }
    [data-testid^="stBaseButton"]:hover, [data-testid^="stBaseLinkButton"]:hover{
      border-color:var(--ink) !important;
    }
    button[kind="primary"], button[kind="primaryFormSubmit"],
    [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"]{
      background:var(--ink) !important; color:#fff !important; border:1px solid var(--ink) !important;
    }
    button[kind="primary"]:hover, button[kind="primaryFormSubmit"]:hover,
    [data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover{
      background:#303039 !important; border-color:#303039 !important;
    }
    [data-testid^="stBaseButton"]:disabled{ opacity:.4; }

    /* inputs */
    .stTextInput input, .stTextArea textarea, .stDateInput input{
      border-radius:10px !important; background:var(--card);
    }
    .stNumberInput input{
      border-radius:10px !important; background:var(--card);
      font-family:var(--mono) !important; font-weight:500;
    }

    /* expander / tabs / radio */
    div[data-testid="stExpander"] details{
      border:1px solid var(--line); border-radius:14px; background:var(--card);
    }
    div[data-testid="stExpander"] summary{ font-weight:600; font-size:.92rem; }
    div[data-baseweb="tab-highlight"]{ background:var(--ink); }
    button[data-baseweb="tab"]{ font-weight:500; }
    div[data-testid="stRadio"] label p{ font-size:.84rem; font-weight:500; }

    /* person dots — the only place color is allowed to live */
    .dot{
      display:inline-block; width:6px; height:6px; border-radius:50%;
      background:var(--faint); vertical-align:1px; margin-right:7px;
    }
    .dot.a{ background:var(--a); }
    .dot.b{ background:var(--b); }
    .dot.off{ background:transparent; box-shadow:inset 0 0 0 1px var(--faint); }

    /* masthead — wordmark left, streak counters right, single ink rule below */
    .mast{
      display:flex; align-items:flex-end; justify-content:space-between; gap:20px; flex-wrap:wrap;
      padding:4px 0 16px; border-bottom:1px solid var(--ink); margin-bottom:14px;
    }
    .mast-kicker{
      font-family:var(--mono); font-size:.6rem; font-weight:500; letter-spacing:.2em;
      text-transform:uppercase; color:var(--muted);
    }
    .mast-title{ font-size:1.72rem; font-weight:700; letter-spacing:-.035em; line-height:1.05; margin-top:5px; }
    .mast-stats{ display:flex; gap:34px; }
    .mstat{ text-align:right; }
    .ms-v{ font-family:var(--mono); font-size:1.3rem; font-weight:600; letter-spacing:-.01em; line-height:1; }
    .ms-l{
      font-family:var(--mono); font-size:.58rem; font-weight:500; letter-spacing:.12em;
      text-transform:uppercase; color:var(--muted); margin-top:6px; white-space:nowrap;
    }

    /* status line under the masthead */
    .status{ display:flex; align-items:center; font-size:.87rem; color:var(--muted); margin:0 0 20px; }
    .status b{ color:var(--ink); font-weight:600; }

    /* section labels */
    .eyebrow{
      font-family:var(--mono); font-size:.6rem; font-weight:500; text-transform:uppercase;
      letter-spacing:.16em; color:var(--muted); margin:6px 0 10px;
    }
    .sec-row{ display:flex; align-items:center; justify-content:space-between; margin:4px 0 10px; }
    .today-link{
      font-family:var(--mono); font-size:.62rem; font-weight:500; letter-spacing:.12em;
      text-transform:uppercase; color:var(--ink); text-decoration:none;
      border:1px solid var(--line-2); padding:7px 14px; border-radius:999px;
      transition:border-color .12s ease;
    }
    .today-link:hover{ border-color:var(--ink); }

    /* day strip — two dots per day: indigo = Abhi logged, copper = Shamal */
    .strip{ display:flex; gap:6px; overflow-x:auto; padding:2px 1px 12px; }
    .pill{
      flex:0 0 auto; display:flex; flex-direction:column; align-items:center; gap:6px;
      min-width:62px; padding:10px 6px 9px; border-radius:12px;
      background:var(--card); border:1px solid var(--line); color:var(--ink);
      text-decoration:none; transition:border-color .12s ease;
    }
    .pill:hover{ border-color:var(--ink); }
    .pill:focus-visible, .today-link:focus-visible{ outline:2px solid var(--ink); outline-offset:2px; }
    .p-dow{
      font-family:var(--mono); font-size:.55rem; font-weight:500; text-transform:uppercase;
      letter-spacing:.1em; color:var(--faint);
    }
    .p-dom{ font-family:var(--mono); font-size:1rem; font-weight:600; line-height:1; }
    .p-dots{ display:flex; gap:4px; }
    .p-dot{ width:6px; height:6px; border-radius:50%; box-shadow:inset 0 0 0 1px var(--line-2); }
    .p-dot.a.on{ background:var(--a); box-shadow:none; }
    .p-dot.b.on{ background:var(--b); box-shadow:none; }
    .pill.is-today{ border-color:var(--line-2); }
    .pill.is-today .p-dow{ color:var(--ink); }
    .pill.is-selected{ background:var(--ink); border-color:var(--ink); color:#fff; }
    .pill.is-selected .p-dow{ color:rgba(255,255,255,.55); }
    .pill.is-selected .p-dot{ box-shadow:inset 0 0 0 1px rgba(255,255,255,.35); }
    .pill.is-selected .p-dot.a.on{ background:#a5b4fc; box-shadow:none; }
    .pill.is-selected .p-dot.b.on{ background:#fdba74; box-shadow:none; }

    /* selected date heading */
    .sel-date{ display:flex; align-items:baseline; gap:10px; padding-top:6px; }
    .sd-main{ font-size:1.14rem; font-weight:600; letter-spacing:-.02em; }
    .sd-year{ font-family:var(--mono); font-size:.66rem; color:var(--faint); }

    /* person panels — hairline card, 2px person rule on top */
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mk-a){
      border:1px solid var(--line) !important; border-top:2px solid var(--a) !important;
      border-radius:14px !important; background:var(--card);
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mk-b){
      border:1px solid var(--line) !important; border-top:2px solid var(--b) !important;
      border-radius:14px !important; background:var(--card);
    }
    .pname-row{ display:flex; align-items:center; justify-content:space-between; margin:0 0 12px; }
    .pname{ font-size:1rem; font-weight:600; letter-spacing:-.01em; }
    .pmeta{
      font-family:var(--mono); font-size:.58rem; font-weight:500; letter-spacing:.12em;
      text-transform:uppercase; color:var(--muted);
    }
    .avg-line{ font-family:var(--mono); font-size:.66rem; color:var(--muted); margin:2px 0 12px; }
    .avg-line b{ color:var(--ink); font-weight:600; }

    /* trend stat cells — hairline top rule, mono values */
    .sg-head{ display:flex; align-items:center; font-weight:600; font-size:.92rem; margin:6px 0 12px; }
    .statgrid{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:10px; }
    .sg{ border-top:1px solid var(--line-2); padding-top:9px; }
    .sg .v{ font-family:var(--mono); font-size:1.02rem; font-weight:600; letter-spacing:-.01em; }
    .sg .l{
      font-family:var(--mono); font-size:.56rem; font-weight:500; letter-spacing:.1em;
      text-transform:uppercase; color:var(--faint); margin-top:4px;
    }

    .foot{
      font-family:var(--mono); font-size:.6rem; letter-spacing:.14em; text-transform:uppercase;
      color:var(--faint); text-align:center; margin-top:40px;
    }

    /* password gate */
    .gate-wrap{ text-align:center; margin-top:10vh; margin-bottom:6px; }
    .gate-kicker{
      font-family:var(--mono); font-size:.6rem; font-weight:500; letter-spacing:.22em;
      text-transform:uppercase; color:var(--muted);
    }
    .gate-title{ font-size:1.5rem; font-weight:700; letter-spacing:-.03em; margin-top:8px; }
    .gate-sub{ color:var(--muted); font-size:.85rem; margin-top:4px; }

    @media (max-width:860px){
      .mast{ align-items:flex-start; }
      .mast-stats{ gap:22px; }
      .mstat{ text-align:left; }
      .statgrid{ grid-template-columns:repeat(2,1fr); }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# =============================================================================
# Gate everything below this line behind the password.
# =============================================================================
require_password()

# =============================================================================
# Load data for both people
# =============================================================================
if "cache_buster" not in st.session_state:
    st.session_state.cache_buster = datetime.now().isoformat()

with st.spinner("Connecting to Google Sheets..."):
    try:
        sheet_url = get_spreadsheet().url
        ws_map, data = {}, {}
        for key in PEOPLE:
            ws = get_person_worksheet(key)
            ws_map[key] = ws
            data[key] = load_person_df(key, ws.title, st.session_state.cache_buster)
    except Exception as e:
        st.exception(e)
        st.stop()

by_date = {key: {row["date"]: row for _, row in data[key].iterrows()} for key in PEOPLE}

today = date.today()
today_iso = today.isoformat()

streaks = {key: calc_streak(set(by_date[key].keys()), today) for key in PEOPLE}
weeks = {key: len(get_window_df(data[key], 7)) for key in PEOPLE}
logged_today = {key: today_iso in by_date[key] for key in PEOPLE}


# =============================================================================
# Selected day (single source of truth via the ?day= URL param)
# =============================================================================
def _valid_iso(s) -> bool:
    try:
        date.fromisoformat(str(s))
        return True
    except (TypeError, ValueError):
        return False


qp_day = st.query_params.get("day")
if qp_day and _valid_iso(qp_day):
    sel_iso = str(qp_day)
elif _valid_iso(st.session_state.get("selected_date")):
    sel_iso = st.session_state.selected_date
else:
    sel_iso = today_iso
st.session_state.selected_date = sel_iso
sel_date = date.fromisoformat(sel_iso)


# =============================================================================
# Masthead
# =============================================================================
st.markdown(
    f"""
    <div class="mast">
      <div>
        <div class="mast-kicker">Daily diet log — {today.strftime('%a, %b %d').upper()}</div>
        <div class="mast-title">Shred Sheet</div>
      </div>
      <div class="mast-stats">
        <div class="mstat">
          <div class="ms-v">{streaks['abhi']}</div>
          <div class="ms-l"><span class="dot a"></span>Abhi — streak · {weeks['abhi']}/7 wk</div>
        </div>
        <div class="mstat">
          <div class="ms-v">{streaks['shamal']}</div>
          <div class="ms-l"><span class="dot b"></span>Shamal — streak · {weeks['shamal']}/7 wk</div>
        </div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Accountability line — updates as each of you checks in today.
if logged_today["abhi"] and logged_today["shamal"]:
    status = ('<span class="dot a"></span><span class="dot b"></span>'
              "<span><b>Both logged today.</b> Streak intact — same again tomorrow.</span>")
elif logged_today["abhi"]:
    status = ('<span class="dot a"></span>'
              "<span><b>Abhi</b> is in for today. <b>Shamal</b> — the sheet is open.</span>")
elif logged_today["shamal"]:
    status = ('<span class="dot b"></span>'
              "<span><b>Shamal</b> is in for today. <b>Abhi</b> — the sheet is open.</span>")
else:
    status = ('<span class="dot off"></span>'
              "<span>No entries yet today. First one in sets the pace.</span>")
st.markdown(f'<div class="status">{status}</div>', unsafe_allow_html=True)

meta_l, meta_m, meta_r = st.columns([3.2, 1.15, 0.85])
with meta_l:
    st.caption(f"Tabs `{ws_map['abhi'].title}` and `{ws_map['shamal'].title}` in the same spreadsheet as before")
with meta_m:
    st.link_button("Open sheet", sheet_url, use_container_width=True)
with meta_r:
    if st.button("Refresh", use_container_width=True):
        st.session_state.cache_buster = datetime.now().isoformat()
        st.rerun()


# =============================================================================
# Day picker — quick strip of the last 8 days + jump-to-any-date
# =============================================================================
st.markdown(
    f"""
    <div class="sec-row">
      <div class="eyebrow" style="margin:0">Pick a day</div>
      <a class="today-link" href="?day={today_iso}" target="_self">Today</a>
    </div>
    """,
    unsafe_allow_html=True,
)


def render_pill(d):
    iso = d.isoformat()
    a_on = iso in by_date["abhi"]
    b_on = iso in by_date["shamal"]

    cls = ["pill"]
    if iso == today_iso:
        cls.append("is-today")
    if iso == sel_iso:
        cls.append("is-selected")

    label = "Today" if iso == today_iso else d.strftime("%a")
    a_state = "logged" if a_on else "—"
    b_state = "logged" if b_on else "—"
    title = f'{d.strftime("%a, %b %d")} — Abhi {a_state} · Shamal {b_state}'
    dots = (
        f'<span class="p-dots">'
        f'<span class="p-dot a{" on" if a_on else ""}"></span>'
        f'<span class="p-dot b{" on" if b_on else ""}"></span></span>'
    )
    return (
        f'<a class="{" ".join(cls)}" href="?day={iso}" target="_self" title="{title}">'
        f'<span class="p-dow">{label}</span>'
        f'<span class="p-dom">{d.day:02d}</span>{dots}</a>'
    )


strip_days = [today + timedelta(days=i) for i in range(-7, 1)]
st.markdown(
    '<div class="strip">' + "".join(render_pill(d) for d in strip_days) + "</div>",
    unsafe_allow_html=True,
)

pick_l, pick_r = st.columns([4.4, 1.3])
with pick_l:
    st.markdown(
        f"""
        <div class="sel-date">
          <span class="sd-main">{sel_date.strftime('%A, %B %d')}</span>
          <span class="sd-year">{sel_date.year}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
with pick_r:
    with st.popover("Jump to date", use_container_width=True):
        jump = st.date_input(
            "Date", value=sel_date,
            key=f"jump_{sel_iso}", format="MM/DD/YYYY",
        )
        if jump != sel_date:
            st.query_params["day"] = jump.isoformat()
            st.rerun()

st.write("")


# =============================================================================
# Side-by-side check-in panels — Abhi left, Shamal right
# =============================================================================
def reset_person_day_state(person_key, date_str):
    for prefix in ("cal", "pro", "fib", "notes"):
        st.session_state.pop(f"{prefix}_{person_key}_{date_str}", None)


def render_panel(person_key):
    cfg = PEOPLE[person_key]
    dfp = data[person_key]
    ws = ws_map[person_key]
    existing = by_date[person_key].get(sel_iso)

    w7 = get_window_df(dfp, 7)
    if len(w7):
        avg_html = (
            f'7-day avg — <b>{int(round(w7["calories"].mean())):,} cal</b>'
            f' · <b>{w7["protein"].mean():.1f} g</b> protein'
            f' · <b>{w7["fiber"].mean():.1f} g</b> fiber'
        )
    else:
        avg_html = "No entries in the last 7 days yet — today is day one."

    with st.container(border=True):
        st.markdown(
            f'<div class="pname-row mk-{cfg["css"]}">'
            f'<span class="pname"><span class="dot {cfg["css"]}"></span>{cfg["name"]}</span>'
            f'<span class="pmeta">streak {streaks[person_key]} · wk {weeks[person_key]}/7</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        n1, n2, n3 = st.columns(3)
        calories = n1.number_input(
            "Calories", min_value=0, max_value=20000,
            value=safe_int(existing.get("calories", 0)) if existing is not None else 0,
            step=25, key=f"cal_{person_key}_{sel_iso}",
        )
        protein = n2.number_input(
            "Protein (g)", min_value=0.0, max_value=1000.0,
            value=float(safe_float(existing.get("protein", 0.0))) if existing is not None else 0.0,
            step=1.0, format="%.1f", key=f"pro_{person_key}_{sel_iso}",
        )
        fiber = n3.number_input(
            "Fiber (g)", min_value=0.0, max_value=300.0,
            value=float(safe_float(existing.get("fiber", 0.0))) if existing is not None else 0.0,
            step=1.0, format="%.1f", key=f"fib_{person_key}_{sel_iso}",
        )

        notes = st.text_area(
            "Notes / progress",
            value=str(existing.get("notes", "")) if existing is not None else "",
            placeholder="Weigh-in, wins, slip-ups, energy — anything worth remembering.",
            height=100, key=f"notes_{person_key}_{sel_iso}",
        )

        st.markdown(f'<div class="avg-line">{avg_html}</div>', unsafe_allow_html=True)

        b1, b2 = st.columns([1.6, 1])
        save_clicked = b1.button(
            f"Save {sel_date.strftime('%b %d')}",
            type="primary", use_container_width=True, key=f"save_{person_key}",
        )
        delete_clicked = b2.button(
            "Delete", disabled=existing is None,
            use_container_width=True, key=f"del_{person_key}",
        )

    if save_clicked:
        action = upsert_entry(ws, sel_date, calories, protein, fiber, notes)
        st.session_state.cache_buster = datetime.now().isoformat()
        verb = "saved" if action == "added" else "updated"
        st.toast(f"{cfg['name']} — {verb} {sel_date.strftime('%b %d')}")
        st.rerun()

    if delete_clicked:
        if delete_entry(ws, sel_date):
            reset_person_day_state(person_key, sel_iso)
            st.session_state.cache_buster = datetime.now().isoformat()
            st.toast(f"{cfg['name']} — entry deleted")
            st.rerun()
        else:
            st.warning("No entry found for this date.")


col_abhi, col_shamal = st.columns(2, gap="large")
with col_abhi:
    render_panel("abhi")
with col_shamal:
    render_panel("shamal")


# =============================================================================
# Head-to-head trends
# =============================================================================
def statgrid_html(person_key, w, days):
    cfg = PEOPLE[person_key]
    n = len(w)
    avg_cal = int(round(w["calories"].mean())) if n else 0
    avg_pro = w["protein"].mean() if n else 0.0
    avg_fib = w["fiber"].mean() if n else 0.0
    return (
        f'<div class="sg-head"><span class="dot {cfg["css"]}"></span>{cfg["name"]}</div>'
        f'<div class="statgrid">'
        f'<div class="sg"><div class="v">{n}/{days}</div><div class="l">days logged</div></div>'
        f'<div class="sg"><div class="v">{avg_cal:,}</div><div class="l">avg calories</div></div>'
        f'<div class="sg"><div class="v">{avg_pro:.1f} g</div><div class="l">avg protein</div></div>'
        f'<div class="sg"><div class="v">{avg_fib:.1f} g</div><div class="l">avg fiber</div></div>'
        f'</div>'
    )


def duo_chart(chart_df, ycol, ylabel, days, fmt=",.0f"):
    names = [PEOPLE[k]["name"] for k in PEOPLE]
    hexes = [PEOPLE[k]["hex"] for k in PEOPLE]
    color = alt.Color(
        "person:N", title=None,
        scale=alt.Scale(domain=names, range=hexes),
        legend=alt.Legend(orient="top", symbolType="circle"),
    )
    tooltip = [
        alt.Tooltip("date_dt:T", title="Date", format="%b %d"),
        alt.Tooltip("person:N", title="Who"),
        alt.Tooltip(f"{ycol}:Q", title=ylabel, format=fmt),
    ]

    if days <= 21:
        d = chart_df.copy()
        d["day"] = d["date_dt"].dt.strftime("%b %d")
        order = [pd.Timestamp(t).strftime("%b %d") for t in sorted(d["date_dt"].unique())]
        chart = (
            alt.Chart(d)
            .mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2)
            .encode(
                x=alt.X("day:N", sort=order, title=None, axis=alt.Axis(labelAngle=-40)),
                xOffset=alt.XOffset("person:N"),
                y=alt.Y(f"{ycol}:Q", title=ylabel),
                color=color, tooltip=tooltip,
            )
        )
    else:
        chart = (
            alt.Chart(chart_df)
            .mark_line(point=alt.OverlayMarkDef(size=34), strokeWidth=2)
            .encode(
                x=alt.X("date_dt:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=-40)),
                y=alt.Y(f"{ycol}:Q", title=ylabel),
                color=color, tooltip=tooltip,
            )
        )

    return (
        chart.properties(height=250)
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor="#8b8b94", titleColor="#8b8b94",
            domainColor="#e7e7ea", tickColor="#e7e7ea", gridColor="#f0f0f3",
            labelFont="IBM Plex Mono", titleFont="IBM Plex Mono",
            labelFontSize=10, titleFontSize=10,
        )
        .configure_legend(labelFont="IBM Plex Mono", labelFontSize=10, labelColor="#18181d")
    )


st.divider()
with st.expander("Trends", expanded=False):
    trend_days = st.radio(
        "Time window", TREND_WINDOW_OPTIONS, index=0,
        format_func=lambda d: f"Last {d} days",
        horizontal=True, label_visibility="collapsed", key="trend_window",
    )

    windows = {key: get_window_df(data[key], trend_days) for key in PEOPLE}

    s1, s2 = st.columns(2, gap="large")
    for col, key in ((s1, "abhi"), (s2, "shamal")):
        with col:
            st.markdown(statgrid_html(key, windows[key], trend_days), unsafe_allow_html=True)

    frames = [
        windows[key].assign(person=PEOPLE[key]["name"])
        for key in PEOPLE if not windows[key].empty
    ]
    if not frames:
        st.info(f"No entries in the last {trend_days} days yet — pick a day above and check in.")
    else:
        comb = pd.concat(frames, ignore_index=True)

        st.markdown('<div class="eyebrow">Calories</div>', unsafe_allow_html=True)
        st.altair_chart(duo_chart(comb, "calories", "Calories", trend_days), use_container_width=True)

        c1, c2 = st.columns(2, gap="large")
        with c1:
            st.markdown('<div class="eyebrow">Protein (g)</div>', unsafe_allow_html=True)
            st.altair_chart(
                duo_chart(comb, "protein", "Protein (g)", trend_days, fmt=",.1f"),
                use_container_width=True,
            )
        with c2:
            st.markdown('<div class="eyebrow">Fiber (g)</div>', unsafe_allow_html=True)
            st.altair_chart(
                duo_chart(comb, "fiber", "Fiber (g)", trend_days, fmt=",.1f"),
                use_container_width=True,
            )


# =============================================================================
# All saved entries
# =============================================================================
st.divider()
with st.expander("All entries"):
    tabs = st.tabs([PEOPLE[key]["name"] for key in PEOPLE])
    for tab, key in zip(tabs, PEOPLE):
        with tab:
            dfp = data[key]
            if dfp.empty:
                st.info("No entries yet.")
                continue

            display_df = dfp.copy()
            display_df["date_dt"] = pd.to_datetime(display_df["date"], errors="coerce")
            display_df = display_df.sort_values("date_dt", ascending=False)

            st.dataframe(
                display_df[["date_dt", "calories", "protein", "fiber", "notes", "updated_at"]],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "date_dt": st.column_config.DateColumn("Date", format="MMM D, YYYY"),
                    "calories": st.column_config.NumberColumn("Calories", format="%d"),
                    "protein": st.column_config.NumberColumn("Protein (g)", format="%.1f"),
                    "fiber": st.column_config.NumberColumn("Fiber (g)", format="%.1f"),
                    "notes": st.column_config.TextColumn("Notes / progress", width="large"),
                    "updated_at": st.column_config.TextColumn("Updated"),
                },
            )

st.markdown(
    '<div class="foot">Shred Sheet · Abhi × Shamal · data lives in your Google Sheet</div>',
    unsafe_allow_html=True,
)
