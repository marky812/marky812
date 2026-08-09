"""
Shred Sheet — Abhi x Shamal
============================================================
A two-person daily diet accountability tracker.

- Left panel  = Abhi   (violet)
- Right panel = Shamal (burnt orange)
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
    page_icon="🔥",
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
        "css": "abhi",
        "hex": "#7c3aed",
        "deep": "#5b21b6",
    },
    "shamal": {
        "name": "Shamal",
        "ws_secret": "shamal_worksheet_name",
        "ws_default": "shamal_log",
        "css": "shamal",
        "hex": "#ea580c",
        "deep": "#9a3412",
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

    _, mid, _ = st.columns([1, 1.3, 1])
    with mid:
        st.markdown(
            """
            <div style="text-align:center;margin-top:7vh">
              <div style="width:58px;height:58px;margin:0 auto;border-radius:19px;
                          display:flex;align-items:center;justify-content:center;font-size:26px;
                          background:linear-gradient(135deg,#7c3aed,#0b1020);
                          box-shadow:0 14px 28px -14px rgba(11,16,32,.7)">🔒</div>
              <div style="font-family:'Space Grotesk',sans-serif;font-size:1.5rem;font-weight:700;
                          letter-spacing:-.02em;color:#101529;margin-top:12px">Shred Sheet</div>
              <div style="color:#5b667a;font-size:.9rem;margin-top:3px">
                Abhi &amp; Shamal — enter the password to continue.</div>
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
# Styling — Space Grotesk everywhere; violet (Abhi) vs burnt orange (Shamal)
# on a cool light body, with a dark split hero as the signature element.
# =============================================================================
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&display=swap');

    :root{
      --a:#7c3aed; --a-deep:#5b21b6; --a-soft:#ede9fe; --a-line:#ddd3fb;
      --b:#ea580c; --b-deep:#9a3412; --b-soft:#ffedd5; --b-line:#fcd9bd;
      --ink:#101529; --muted:#5b667a; --line:#e5e8f0; --bg:#f5f6fa; --card:#ffffff;
      --dark1:#0b1020; --dark2:#181c3a;
    }

    html, body, [class*="css"], .stMarkdown, button, input, textarea, select,
    h1, h2, h3, h4, h5, h6{
      font-family:'Space Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif !important;
    }
    [data-testid="stAppViewContainer"]{ background:var(--bg); }
    .block-container{ padding-top:1.2rem; padding-bottom:3rem; max-width:1180px; }
    #MainMenu, footer{ visibility:hidden; }

    div[data-testid="stWidgetLabel"] p{
      font-size:.72rem !important; font-weight:700 !important; color:var(--muted) !important;
      text-transform:uppercase; letter-spacing:.07em;
    }

    [data-testid^="stBaseButton"], [data-testid^="stBaseLinkButton"]{
      border-radius:14px !important; border:1.5px solid var(--line); background:var(--card);
      min-height:42px; font-weight:600 !important; transition:all .12s ease;
    }
    [data-testid^="stBaseButton"]:hover, [data-testid^="stBaseLinkButton"]:hover{
      border-color:#cfd6e4; transform:translateY(-1px);
      box-shadow:0 8px 18px -12px rgba(16,21,41,.35);
    }
    button[kind="primary"], [data-testid="stBaseButton-primary"],
    button[kind="primaryFormSubmit"], [data-testid="stBaseButton-primaryFormSubmit"]{
      background-image:linear-gradient(135deg,#312e81,var(--dark1)) !important;
      border:0 !important; color:#fff !important;
      box-shadow:0 12px 24px -14px rgba(16,21,41,.7) !important;
    }
    button[kind="primary"]:hover, [data-testid="stBaseButton-primary"]:hover{ filter:brightness(1.18); }

    .stTextInput input, .stNumberInput input, .stTextArea textarea, .stDateInput input{
      border-radius:12px !important; background:var(--card); font-weight:600;
    }

    div[data-testid="stExpander"] details{
      border:1.5px solid var(--line); border-radius:18px; background:var(--card);
    }
    div[data-testid="stExpander"] summary{ font-weight:700; }
    div[data-baseweb="tab-highlight"]{ background:linear-gradient(90deg,var(--a),var(--b)); }
    button[data-baseweb="tab"]{ font-weight:600; }
    div[data-testid="stRadio"] label{ font-weight:600; }

    /* ---- hero: dark split identity, one glow per person ---- */
    .hero{
      display:flex; align-items:stretch; justify-content:space-between; gap:16px; flex-wrap:wrap;
      color:#fff; padding:22px 24px; border-radius:26px; margin-bottom:14px;
      background:
        radial-gradient(430px 230px at 6% -25%, rgba(124,58,237,.55), transparent 62%),
        radial-gradient(430px 230px at 94% 125%, rgba(234,88,12,.5), transparent 62%),
        linear-gradient(120deg,var(--dark1),var(--dark2) 55%,var(--dark1));
      box-shadow:0 22px 48px -22px rgba(11,16,32,.7);
    }
    .hero-mid{ text-align:center; align-self:center; flex:1; min-width:220px; }
    .hero-kicker{ font-size:.66rem; font-weight:700; letter-spacing:.24em; color:#9aa3c7; text-transform:uppercase; }
    .hero-title{
      font-size:2.15rem; font-weight:700; letter-spacing:-.03em; line-height:1.05; margin-top:2px;
      background:linear-gradient(90deg,#c4b5fd,#ffffff 50%,#fdba74);
      -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
    }
    .hero-sub{ font-size:.86rem; color:#aab1d0; font-weight:500; margin-top:4px; }
    .duo{
      background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.12);
      border-radius:18px; padding:14px 18px; min-width:186px;
    }
    .duo.a{ border-color:rgba(167,139,250,.5); }
    .duo.b{ border-color:rgba(251,146,60,.5); text-align:right; }
    .duo-name{ display:flex; align-items:center; gap:8px; font-weight:700; font-size:1rem; }
    .duo.b .duo-name{ justify-content:flex-end; }
    .dnd{ width:9px; height:9px; border-radius:50%; }
    .dnd.a{ background:#a78bfa; box-shadow:0 0 12px rgba(167,139,250,.9); }
    .dnd.b{ background:#fb923c; box-shadow:0 0 12px rgba(251,146,60,.9); }
    .duo-big{ font-size:1.65rem; font-weight:700; letter-spacing:-.02em; margin-top:6px; line-height:1; }
    .duo-sub{ font-size:.7rem; color:#aab1d0; font-weight:600; margin-top:6px;
              text-transform:uppercase; letter-spacing:.06em; }

    /* accountability banner */
    .pulse{
      display:flex; align-items:center; gap:10px; padding:11px 16px; border-radius:14px; margin:2px 0 14px;
      background:linear-gradient(90deg,var(--a-soft),var(--b-soft)); border:1.5px solid #e7e0f7;
      font-weight:600; color:var(--ink); font-size:.92rem;
    }

    .eyebrow{ font-size:.7rem; font-weight:700; text-transform:uppercase; letter-spacing:.13em;
              color:var(--muted); margin:8px 0 10px; }
    .sec-row{ display:flex; align-items:center; justify-content:space-between; margin:6px 0 8px; }
    .today-pill{ font-size:.76rem; font-weight:700; color:#fff; text-decoration:none;
                 background:linear-gradient(120deg,var(--a),var(--b)); padding:6px 14px; border-radius:999px; }
    .today-pill:hover{ filter:brightness(1.06); }

    /* quick day strip — two dots per day: violet = Abhi logged, orange = Shamal */
    .strip{ display:flex; gap:8px; overflow-x:auto; padding:2px 2px 10px; }
    .pill{
      flex:0 0 auto; display:flex; flex-direction:column; align-items:center; gap:4px;
      min-width:66px; padding:10px 8px 9px; border-radius:16px;
      background:var(--card); border:1.5px solid var(--line); color:var(--ink); text-decoration:none;
      transition:transform .12s ease, box-shadow .12s ease, border-color .12s ease;
    }
    .pill:hover{ transform:translateY(-2px); box-shadow:0 10px 20px -12px rgba(16,21,41,.3); border-color:#cfd6e4; }
    .p-dow{ font-size:.62rem; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
    .p-dom{ font-size:1.15rem; font-weight:700; line-height:1; }
    .p-dots{ display:flex; gap:4px; }
    .p-dot{ width:7px; height:7px; border-radius:50%; border:1.5px solid var(--line); background:transparent; }
    .p-dot.a.on{ background:var(--a); border-color:var(--a); }
    .p-dot.b.on{ background:var(--b); border-color:var(--b); }
    .pill.is-today{ box-shadow:0 0 0 2px var(--a-soft), 0 0 0 4px var(--b-soft); }
    .pill.is-selected{
      background:linear-gradient(135deg,#312e81,var(--dark1)); border-color:transparent; color:#fff;
      box-shadow:0 12px 24px -12px rgba(11,16,32,.65);
    }
    .pill.is-selected .p-dow{ color:rgba(255,255,255,.8); }
    .pill.is-selected .p-dot{ border-color:rgba(255,255,255,.45); }
    .pill.is-selected .p-dot.a.on{ background:#c4b5fd; border-color:#c4b5fd; }
    .pill.is-selected .p-dot.b.on{ background:#fdba74; border-color:#fdba74; }

    .sel-chip{
      display:inline-flex; align-items:center; gap:8px; font-weight:700; color:var(--ink);
      background:var(--card); border:1.5px solid var(--line); padding:9px 16px;
      border-radius:999px; font-size:.95rem;
    }

    /* person panel headers */
    .phead{
      display:flex; align-items:center; justify-content:space-between; gap:10px; color:#fff;
      border-radius:18px; padding:13px 16px; margin-bottom:10px;
      box-shadow:0 14px 30px -18px rgba(16,21,41,.45);
    }
    .phead.abhi{ background:linear-gradient(120deg,var(--a),var(--a-deep)); }
    .phead.shamal{ background:linear-gradient(120deg,#f97316,#c2410c); }
    .ph-left{ display:flex; align-items:center; gap:11px; }
    .ph-av{ width:40px; height:40px; border-radius:13px; display:flex; align-items:center;
            justify-content:center; background:rgba(255,255,255,.18); font-weight:700; font-size:1.05rem; }
    .ph-name{ font-size:1.12rem; font-weight:700; letter-spacing:-.01em; line-height:1.1; }
    .ph-tag{ font-size:.66rem; font-weight:600; opacity:.85; text-transform:uppercase; letter-spacing:.08em; }
    .ph-chips{ display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
    .ph-chip{ font-size:.7rem; font-weight:700; background:rgba(255,255,255,.16);
              border:1px solid rgba(255,255,255,.22); padding:4px 10px; border-radius:999px; white-space:nowrap; }

    .avg-line{ font-size:.78rem; color:var(--muted); font-weight:600; margin:2px 0 10px; }
    .avg-line b{ color:var(--ink); }

    /* tint each bordered panel (and its Save button) in its owner's color */
    .mk-abhi, .mk-shamal{ display:none; }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mk-abhi){
      border:1.5px solid var(--a-line) !important; border-radius:20px !important;
      background:linear-gradient(180deg,#fff,#fcfbff);
      box-shadow:0 18px 38px -26px rgba(124,58,237,.55);
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mk-shamal){
      border:1.5px solid var(--b-line) !important; border-radius:20px !important;
      background:linear-gradient(180deg,#fff,#fffdfa);
      box-shadow:0 18px 38px -26px rgba(234,88,12,.5);
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mk-abhi) button[kind="primary"],
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mk-abhi) [data-testid="stBaseButton-primary"]{
      background-image:linear-gradient(135deg,var(--a),var(--a-deep)) !important;
      box-shadow:0 12px 24px -14px rgba(124,58,237,.65) !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mk-shamal) button[kind="primary"],
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mk-shamal) [data-testid="stBaseButton-primary"]{
      background-image:linear-gradient(135deg,#f97316,#c2410c) !important;
      box-shadow:0 12px 24px -14px rgba(234,88,12,.65) !important;
    }

    /* trend stat tiles */
    .sg-head{ display:flex; align-items:center; gap:8px; font-weight:700; margin:2px 0 8px; color:var(--ink); }
    .sdot{ width:9px; height:9px; border-radius:50%; }
    .sdot.abhi{ background:var(--a); } .sdot.shamal{ background:var(--b); }
    .statgrid{ display:grid; grid-template-columns:repeat(4,1fr); gap:9px; margin-bottom:8px; }
    .sg{ border:1.5px solid var(--line); border-radius:14px; padding:10px 12px; background:var(--card); }
    .sg .v{ font-weight:700; font-size:1.12rem; letter-spacing:-.02em; color:var(--ink); }
    .statgrid.abhi .sg .v{ color:var(--a-deep); }
    .statgrid.shamal .sg .v{ color:var(--b-deep); }
    .sg .l{ font-size:.62rem; color:var(--muted); font-weight:700; text-transform:uppercase;
            letter-spacing:.06em; margin-top:3px; }

    .foot{ color:var(--muted); font-size:.78rem; text-align:center; margin-top:34px; }

    @media (max-width:860px){
      .hero{ flex-direction:column; }
      .hero-mid{ order:-1; }
      .duo.b{ text-align:left; }
      .duo.b .duo-name{ justify-content:flex-start; }
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
# Hero
# =============================================================================
st.markdown(
    f"""
    <div class="hero">
      <div class="duo a">
        <div class="duo-name"><span class="dnd a"></span>Abhi</div>
        <div class="duo-big">🔥 {streaks["abhi"]}</div>
        <div class="duo-sub">day streak · {weeks["abhi"]}/7 this week</div>
      </div>
      <div class="hero-mid">
        <div class="hero-kicker">Weight-loss duo</div>
        <div class="hero-title">Shred Sheet</div>
        <div class="hero-sub">Daily diet check-in · {today.strftime('%A, %B %d')}</div>
      </div>
      <div class="duo b">
        <div class="duo-name"><span class="dnd b"></span>Shamal</div>
        <div class="duo-big">🔥 {streaks["shamal"]}</div>
        <div class="duo-sub">day streak · {weeks["shamal"]}/7 this week</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Accountability nudge — updates as each of you checks in today.
if logged_today["abhi"] and logged_today["shamal"]:
    pulse = "🔥 <b>Both of you</b> checked in today. That is how the weight comes off — same time tomorrow."
elif logged_today["abhi"]:
    pulse = "⚡ <b>Abhi</b> is on the board today. <b>Shamal</b> — the sheet is waiting."
elif logged_today["shamal"]:
    pulse = "⚡ <b>Shamal</b> is on the board today. <b>Abhi</b> — the sheet is waiting."
else:
    pulse = "👀 No check-ins yet today. First one in sets the pace."
st.markdown(f'<div class="pulse">{pulse}</div>', unsafe_allow_html=True)

meta_l, meta_m, meta_r = st.columns([3.1, 1.25, 0.9])
with meta_l:
    st.caption(f"Sheet tabs: `{ws_map['abhi'].title}` · `{ws_map['shamal'].title}` (same spreadsheet as before)")
with meta_m:
    st.link_button("Open Google Sheet", sheet_url, use_container_width=True)
with meta_r:
    if st.button("↻ Refresh", use_container_width=True):
        st.session_state.cache_buster = datetime.now().isoformat()
        st.rerun()


# =============================================================================
# Day picker — quick strip of the last 8 days + jump-to-any-date
# =============================================================================
st.markdown(
    f"""
    <div class="sec-row">
      <div class="eyebrow" style="margin:0">Pick a day</div>
      <a class="today-pill" href="?day={today_iso}" target="_self">↩ Today</a>
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
    a_mark = "✓" if a_on else "–"
    b_mark = "✓" if b_on else "–"
    title = f'{d.strftime("%a, %b %d")} — Abhi {a_mark} · Shamal {b_mark}'
    dots = (
        f'<span class="p-dots">'
        f'<span class="p-dot a{" on" if a_on else ""}"></span>'
        f'<span class="p-dot b{" on" if b_on else ""}"></span></span>'
    )
    return (
        f'<a class="{" ".join(cls)}" href="?day={iso}" target="_self" title="{title}">'
        f'<span class="p-dow">{label}</span>'
        f'<span class="p-dom">{d.day}</span>{dots}</a>'
    )


strip_days = [today + timedelta(days=i) for i in range(-7, 1)]
st.markdown(
    '<div class="strip">' + "".join(render_pill(d) for d in strip_days) + "</div>",
    unsafe_allow_html=True,
)

pick_l, pick_r = st.columns([4.3, 1.4])
with pick_l:
    st.markdown(
        f'<div class="sel-chip">🗓️ {sel_date.strftime("%A, %B %d, %Y")}</div>',
        unsafe_allow_html=True,
    )
with pick_r:
    with st.popover("📅 Any date", use_container_width=True):
        jump = st.date_input(
            "Jump to a date", value=sel_date,
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
            f'7-day avg · <b>{int(round(w7["calories"].mean())):,} cal</b>'
            f' · <b>{w7["protein"].mean():.1f}g</b> protein'
            f' · <b>{w7["fiber"].mean():.1f}g</b> fiber'
        )
    else:
        avg_html = "No entries in the last 7 days yet — today is day one."

    st.markdown(
        f"""
        <div class="phead {cfg['css']}">
          <div class="ph-left">
            <div class="ph-av">{cfg['name'][0]}</div>
            <div>
              <div class="ph-name">{cfg['name']}</div>
              <div class="ph-tag">{sel_date.strftime('%a, %b %d')}</div>
            </div>
          </div>
          <div class="ph-chips">
            <span class="ph-chip">🔥 {streaks[person_key]}-day streak</span>
            <span class="ph-chip">{weeks[person_key]}/7 this week</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        st.markdown(f'<span class="mk-{cfg["css"]}"></span>', unsafe_allow_html=True)

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
            placeholder=f"How did the day go, {cfg['name']}? Wins, slip-ups, weigh-ins, energy, cravings…",
            height=100, key=f"notes_{person_key}_{sel_iso}",
        )

        st.markdown(f'<div class="avg-line">{avg_html}</div>', unsafe_allow_html=True)

        b1, b2 = st.columns([1.6, 1])
        save_clicked = b1.button(
            f"Save · {sel_date.strftime('%b %d')}",
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
        st.toast(f"{cfg['name']} — {verb} {sel_date.strftime('%b %d')}", icon="✅")
        st.rerun()

    if delete_clicked:
        if delete_entry(ws, sel_date):
            reset_person_day_state(person_key, sel_iso)
            st.session_state.cache_buster = datetime.now().isoformat()
            st.toast(f"{cfg['name']} — entry deleted", icon="🗑️")
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
        f'<div class="sg-head"><span class="sdot {cfg["css"]}"></span>{cfg["name"]}</div>'
        f'<div class="statgrid {cfg["css"]}">'
        f'<div class="sg"><div class="v">{n}/{days}</div><div class="l">days logged</div></div>'
        f'<div class="sg"><div class="v">{avg_cal:,}</div><div class="l">avg calories</div></div>'
        f'<div class="sg"><div class="v">{avg_pro:.1f}g</div><div class="l">avg protein</div></div>'
        f'<div class="sg"><div class="v">{avg_fib:.1f}g</div><div class="l">avg fiber</div></div>'
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
            .mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5)
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
            .mark_line(point=alt.OverlayMarkDef(size=45), strokeWidth=3, interpolate="monotone")
            .encode(
                x=alt.X("date_dt:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=-40)),
                y=alt.Y(f"{ycol}:Q", title=ylabel),
                color=color, tooltip=tooltip,
            )
        )

    return (
        chart.properties(height=260)
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor="#5b667a", titleColor="#5b667a",
            domainColor="#e5e8f0", tickColor="#e5e8f0", gridColor="#eef1f6",
            labelFont="Space Grotesk", titleFont="Space Grotesk",
        )
        .configure_legend(labelFont="Space Grotesk", labelFontWeight=600, labelColor="#101529")
    )


st.divider()
with st.expander("📊 Head-to-head — trends & averages", expanded=False):
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

        st.markdown('<div class="eyebrow">Calories — head to head</div>', unsafe_allow_html=True)
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
with st.expander("🗒️ All saved entries"):
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
    '<div class="foot">Shred Sheet · built for Abhi &amp; Shamal · entries live in your Google Sheet</div>',
    unsafe_allow_html=True,
)
