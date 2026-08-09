"""
Shred Sheet — Abhi x Shamal
============================================================
A two-person daily diet accountability tracker.

- Left panel  = Abhi   (indigo)
- Right panel = Shamal (copper)
- Each person logs line items (name, calories, protein, carbs, fat, fiber)
  plus a notes/progress entry per day. Day totals are computed automatically.
- Every saved day is visible in-app in the "Log" section — no need to open
  the Google Sheet (the sheet stays available as backup storage).
- Data is saved to worksheet tabs ("abhi_log", "shamal_log") inside the SAME
  Google Sheet your old tracker uses — nothing existing is touched. If a tab
  already has entries from an earlier version of this app, the new columns
  are appended on the right so every old row is preserved.

No config file needed — the light theme and all colors are enforced inside
this script, so everything stays readable even on phones in dark mode.

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
from html import escape

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

# Column order looks odd on purpose: the first six columns match the earlier
# version of this app exactly, and the new ones (carbs, fat, items_json) are
# APPENDED — so the additive migration in get_person_worksheet() upgrades an
# existing tab in place without losing a single saved row.
HEADERS = [
    "date", "calories", "protein", "fiber", "notes", "updated_at",
    "carbs", "fat", "items_json",
]

MACROS = ["calories", "protein", "carbs", "fat", "fiber"]

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

# Column ratios shared by the item header row and every item row.
ITEM_COLS = [3.0, 1.15, 1.1, 1.15, 1.05, 1.1, 0.55]

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
    """1-indexed column number -> spreadsheet column letters (e.g. 9 -> 'I')."""
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
# Food items — each day is a list of line items with full macros
# =============================================================================
def empty_item():
    return {"name": "", "calories": 0, "protein": 0.0, "carbs": 0.0, "fat": 0.0, "fiber": 0.0}


def clean_items(items):
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        cal = max(0, safe_int(item.get("calories", 0)))
        pro = round(max(0.0, safe_float(item.get("protein", 0.0))), 1)
        carb = round(max(0.0, safe_float(item.get("carbs", 0.0))), 1)
        fat = round(max(0.0, safe_float(item.get("fat", 0.0))), 1)
        fib = round(max(0.0, safe_float(item.get("fiber", 0.0))), 1)
        if name or cal or pro or carb or fat or fib:
            cleaned.append(
                {"name": name, "calories": cal, "protein": pro, "carbs": carb, "fat": fat, "fiber": fib}
            )
    return cleaned


def parse_items(value):
    value_str = str(value or "").strip()
    if not value_str:
        return []
    try:
        raw = json.loads(value_str)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return clean_items(raw)


def totals_from(items):
    t = {"calories": 0, "protein": 0.0, "carbs": 0.0, "fat": 0.0, "fiber": 0.0}
    for item in items:
        t["calories"] += safe_int(item.get("calories", 0))
        t["protein"] += safe_float(item.get("protein", 0.0))
        t["carbs"] += safe_float(item.get("carbs", 0.0))
        t["fat"] += safe_float(item.get("fat", 0.0))
        t["fiber"] += safe_float(item.get("fiber", 0.0))
    for k in ("protein", "carbs", "fat", "fiber"):
        t[k] = round(t[k], 1)
    return t


# ---- per-person widget state for the item rows ----
def clear_item_widget_state(person_key):
    prefixes = tuple(f"{p}_{person_key}_" for p in ("fname", "fcal", "fpro", "fcarb", "ffat", "ffib"))
    for k in list(st.session_state.keys()):
        if k.startswith(prefixes):
            del st.session_state[k]


def load_items_into_state(person_key, items):
    clear_item_widget_state(person_key)
    st.session_state[f"food_items_{person_key}"] = list(items) if items else [empty_item()]


def raw_items_from_widgets(person_key):
    """Read the item rows exactly as typed (no cleaning), index-aligned with
    the widgets — used for add/remove so row indices never shift."""
    items = []
    count = len(st.session_state.get(f"food_items_{person_key}", []))
    for i in range(count):
        items.append(
            {
                "name": st.session_state.get(f"fname_{person_key}_{i}", ""),
                "calories": st.session_state.get(f"fcal_{person_key}_{i}", 0),
                "protein": st.session_state.get(f"fpro_{person_key}_{i}", 0.0),
                "carbs": st.session_state.get(f"fcarb_{person_key}_{i}", 0.0),
                "fat": st.session_state.get(f"ffat_{person_key}_{i}", 0.0),
                "fiber": st.session_state.get(f"ffib_{person_key}_{i}", 0.0),
            }
        )
    return items


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
    Additive schema changes upgrade the tab in place (rows preserved);
    unknown schemas are never cleared — a sibling tab is used instead."""
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
    for col in ("protein", "carbs", "fat", "fiber"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float)
    df["notes"] = df["notes"].astype(str)
    df["items_json"] = df["items_json"].astype(str)
    df["updated_at"] = df["updated_at"].astype(str)
    return df


def find_row_number_by_date(worksheet, selected_date_str):
    dates = worksheet.col_values(1)
    for idx, value in enumerate(dates, start=1):
        if value == selected_date_str:
            return idx
    return None


def upsert_entry(worksheet, selected_date, items, notes):
    selected_date_str = selected_date.isoformat()
    cleaned = clean_items(items)
    t = totals_from(cleaned)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    record = {
        "date": selected_date_str,
        "calories": t["calories"],
        "protein": t["protein"],
        "fiber": t["fiber"],
        "notes": str(notes).strip(),
        "updated_at": now,
        "carbs": t["carbs"],
        "fat": t["fat"],
        "items_json": json.dumps(cleaned),
    }
    row = [record.get(col, "") for col in HEADERS]

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
# code: indigo = Abhi, copper = Shamal — as dots, top rules and chart ink.
# Every widget surface (inputs, buttons, radios, calendar, alerts, toasts) is
# pinned to light ink-on-white in CSS, so no theme config file is needed and
# nothing can render white-on-white or dark-on-dark in device dark mode.
# =============================================================================
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

    :root{
      --a:#4f46e5;            /* Abhi — indigo  */
      --b:#c2410c;            /* Shamal — copper */
      --ink:#18181d; --muted:#5f5f68; --faint:#8b8b94;
      --line:#e7e7ea; --line-2:#d6d6dc;
      --bg:#fafafa; --card:#ffffff;
      --mono:'IBM Plex Mono',ui-monospace,'SF Mono',Menlo,monospace;
    }
    html{ color-scheme:light; }

    html, body, [class*="css"], .stMarkdown, .stMarkdown p, button, input, textarea, select,
    h1, h2, h3, h4, h5, h6{
      font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif !important;
      color:var(--ink);
    }
    .mono{ font-family:var(--mono) !important; }
    [data-testid="stApp"], [data-testid="stAppViewContainer"]{ background:var(--bg) !important; }
    header[data-testid="stHeader"]{ background:transparent; }
    .block-container{ padding-top:1.4rem; padding-bottom:3.5rem; max-width:1120px; }
    #MainMenu, footer{ visibility:hidden; }
    hr{ border:none; border-top:1px solid var(--line); margin:1.7rem 0 1.2rem; }

    /* widget labels + captions — small mono meta text */
    div[data-testid="stWidgetLabel"] p{
      font-family:var(--mono) !important; font-size:.68rem !important; font-weight:500 !important;
      text-transform:uppercase; letter-spacing:.09em; color:var(--muted) !important;
    }
    div[data-testid="stCaptionContainer"], div[data-testid="stCaptionContainer"] p{
      font-family:var(--mono) !important; font-size:.7rem !important; color:var(--muted) !important;
    }
    div[data-testid="stCaptionContainer"] code{
      font-family:var(--mono) !important; font-size:.7rem; color:var(--ink);
      background:transparent; padding:0;
    }

    /* buttons — flat, hairline, ink primary; label color pinned so it can
       never disappear against the button face */
    [data-testid^="stBaseButton"], [data-testid^="stBaseLinkButton"]{
      border-radius:10px !important; border:1px solid var(--line-2) !important;
      background:var(--card) !important; color:var(--ink) !important;
      min-height:40px; font-weight:500 !important; font-size:.88rem !important;
      box-shadow:none !important; transition:border-color .12s ease, background .12s ease;
    }
    [data-testid^="stBaseButton"] p, [data-testid^="stBaseButton"] span,
    [data-testid^="stBaseLinkButton"] p, [data-testid^="stBaseLinkButton"] span{
      color:inherit !important;
    }
    [data-testid^="stBaseButton"]:hover, [data-testid^="stBaseLinkButton"]:hover{
      border-color:var(--ink) !important;
    }
    button[kind="primary"], button[kind="primaryFormSubmit"],
    [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"]{
      background:var(--ink) !important; color:#fff !important; border:1px solid var(--ink) !important;
    }
    button[kind="primary"] p, button[kind="primaryFormSubmit"] p,
    [data-testid="stBaseButton-primary"] p, [data-testid="stBaseButton-primaryFormSubmit"] p{
      color:#fff !important;
    }
    button[kind="primary"]:hover, button[kind="primaryFormSubmit"]:hover,
    [data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover{
      background:#32323b !important; border-color:#32323b !important;
    }
    [data-testid^="stBaseButton"]:disabled{ opacity:.45; }

    /* inputs — face, text, caret, placeholder and focus all pinned readable */
    div[data-baseweb="input"], div[data-baseweb="textarea"]{
      background:var(--card) !important; border-color:var(--line-2) !important;
    }
    div[data-baseweb="input"]:focus-within, div[data-baseweb="textarea"]:focus-within{
      border-color:var(--ink) !important; box-shadow:none !important;
    }
    .stTextInput input, .stTextArea textarea, .stDateInput input, .stNumberInput input{
      border-radius:10px !important; background:var(--card) !important;
      color:var(--ink) !important; -webkit-text-fill-color:var(--ink);
      caret-color:var(--ink);
    }
    .stNumberInput input{ font-family:var(--mono) !important; font-weight:500; font-size:.86rem !important; }
    .stTextInput input::placeholder, .stTextArea textarea::placeholder,
    .stNumberInput input::placeholder{ color:var(--faint) !important; opacity:1; }
    /* hide number steppers — typing is faster, and item rows stay compact */
    [data-testid="stNumberInputStepUp"], [data-testid="stNumberInputStepDown"]{ display:none !important; }

    /* radio — replace the default red accent with ink */
    div[data-testid="stRadio"] label p{ font-size:.84rem; font-weight:500; color:var(--ink) !important; }
    div[data-testid="stRadio"] [data-baseweb="radio"] > div:first-child{
      background:var(--card) !important; border-color:var(--line-2) !important;
    }
    div[data-testid="stRadio"] [data-baseweb="radio"] > div:first-child > div{
      background:var(--ink) !important;
    }
    div[data-testid="stRadio"] [data-baseweb="radio"]:hover > div:first-child{
      border-color:var(--ink) !important;
    }

    /* popover, toast, alerts, date-picker calendar — light surfaces always */
    div[data-testid="stPopoverBody"]{
      background:var(--card) !important; border:1px solid var(--line);
      border-radius:14px;
    }
    div[data-testid="stPopoverBody"] *{ color:var(--ink); }
    div[data-testid="stToast"]{
      background:var(--card) !important; color:var(--ink) !important;
      border:1px solid var(--line-2); border-radius:12px;
    }
    div[data-testid="stToast"] p, div[data-testid="stToast"] div{ color:var(--ink) !important; }
    div[data-testid="stAlert"]{
      background:#f3f3f5 !important; border:1px solid var(--line-2) !important;
      border-radius:12px !important;
    }
    div[data-testid="stAlert"] *{ color:var(--ink) !important; }
    div[data-baseweb="calendar"], div[data-baseweb="datepicker"]{ background:var(--card) !important; }
    div[data-baseweb="calendar"] div, div[data-baseweb="calendar"] span,
    div[data-baseweb="calendar"] button{ color:var(--ink); }
    div[data-baseweb="menu"], ul[role="listbox"]{ background:var(--card) !important; }
    div[data-baseweb="menu"] *, ul[role="listbox"] *{ color:var(--ink) !important; }

    /* expander / tabs */
    div[data-testid="stExpander"] details{
      border:1px solid var(--line); border-radius:14px; background:var(--card);
    }
    div[data-testid="stExpander"] summary, div[data-testid="stExpander"] summary p{
      font-weight:600; font-size:.92rem; color:var(--ink) !important;
    }
    div[data-baseweb="tab-highlight"]{ background:var(--ink); }
    button[data-baseweb="tab"] p{ font-weight:500; color:var(--muted) !important; }
    button[data-baseweb="tab"][aria-selected="true"] p{ color:var(--ink) !important; }

    /* person dots — the only place color is allowed to live */
    .dot{
      display:inline-block; width:8px; height:8px; border-radius:50%;
      background:var(--faint); vertical-align:0; margin-right:7px;
    }
    .dot.a{ background:var(--a); }
    .dot.b{ background:var(--b); }
    .dot.off{ background:transparent; box-shadow:inset 0 0 0 1.5px var(--faint); }

    /* masthead — wordmark left, streak counters right, single ink rule below */
    .mast{
      display:flex; align-items:flex-end; justify-content:space-between; gap:20px; flex-wrap:wrap;
      padding:4px 0 16px; border-bottom:1px solid var(--ink); margin-bottom:14px;
    }
    .mast-kicker{
      font-family:var(--mono); font-size:.64rem; font-weight:500; letter-spacing:.18em;
      text-transform:uppercase; color:var(--muted);
    }
    .mast-title{ font-size:1.72rem; font-weight:700; letter-spacing:-.035em; line-height:1.05; margin-top:5px; }
    .mast-stats{ display:flex; gap:34px; }
    .mstat{ text-align:right; }
    .ms-v{ font-family:var(--mono); font-size:1.3rem; font-weight:600; letter-spacing:-.01em; line-height:1; }
    .ms-l{
      font-family:var(--mono); font-size:.62rem; font-weight:500; letter-spacing:.1em;
      text-transform:uppercase; color:var(--muted); margin-top:6px; white-space:nowrap;
    }

    /* status line under the masthead */
    .status{ display:flex; align-items:center; font-size:.9rem; color:var(--muted); margin:0 0 20px; }
    .status b{ color:var(--ink); font-weight:600; }

    /* section labels */
    .eyebrow{
      font-family:var(--mono); font-size:.64rem; font-weight:500; text-transform:uppercase;
      letter-spacing:.14em; color:var(--muted); margin:6px 0 10px;
    }
    .sec-row{ display:flex; align-items:center; justify-content:space-between; margin:4px 0 10px; }
    .today-link{
      font-family:var(--mono); font-size:.66rem; font-weight:500; letter-spacing:.1em;
      text-transform:uppercase; color:var(--ink); text-decoration:none;
      border:1px solid var(--line-2); padding:7px 14px; border-radius:999px;
      background:var(--card); transition:border-color .12s ease;
    }
    .today-link:hover{ border-color:var(--ink); }

    /* day strip — two dots per day: indigo = Abhi logged, copper = Shamal */
    .strip{ display:flex; gap:6px; overflow-x:auto; padding:2px 1px 12px; }
    .pill{
      flex:0 0 auto; display:flex; flex-direction:column; align-items:center; gap:6px;
      min-width:62px; padding:10px 6px 9px; border-radius:12px;
      background:var(--card); border:1px solid var(--line-2); color:var(--ink);
      text-decoration:none; transition:border-color .12s ease;
    }
    .pill:hover{ border-color:var(--ink); }
    .pill:focus-visible, .today-link:focus-visible{ outline:2px solid var(--ink); outline-offset:2px; }
    .p-dow{
      font-family:var(--mono); font-size:.6rem; font-weight:500; text-transform:uppercase;
      letter-spacing:.09em; color:var(--muted);
    }
    .p-dom{ font-family:var(--mono); font-size:1rem; font-weight:600; line-height:1; color:var(--ink); }
    .p-dots{ display:flex; gap:4px; }
    .p-dot{ width:7px; height:7px; border-radius:50%; box-shadow:inset 0 0 0 1.5px var(--line-2); }
    .p-dot.a.on{ background:var(--a); box-shadow:none; }
    .p-dot.b.on{ background:var(--b); box-shadow:none; }
    .pill.is-today{ border-color:var(--faint); }
    .pill.is-today .p-dow{ color:var(--ink); font-weight:600; }
    .pill.is-selected{ background:var(--ink); border-color:var(--ink); }
    .pill.is-selected .p-dow{ color:rgba(255,255,255,.75); }
    .pill.is-selected .p-dom{ color:#fff; }
    .pill.is-selected .p-dot{ box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.4); }
    .pill.is-selected .p-dot.a.on{ background:#a5b4fc; box-shadow:none; }
    .pill.is-selected .p-dot.b.on{ background:#fdba74; box-shadow:none; }

    /* selected date heading */
    .sel-date{ display:flex; align-items:baseline; gap:10px; padding-top:6px; }
    .sd-main{ font-size:1.14rem; font-weight:600; letter-spacing:-.02em; }
    .sd-year{ font-family:var(--mono); font-size:.7rem; color:var(--muted); }

    /* person panels — hairline card, 3px person rule on top */
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mk-a){
      border:1px solid var(--line-2) !important; border-top:3px solid var(--a) !important;
      border-radius:14px !important; background:var(--card);
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mk-b){
      border:1px solid var(--line-2) !important; border-top:3px solid var(--b) !important;
      border-radius:14px !important; background:var(--card);
    }
    .pname-row{ display:flex; align-items:center; justify-content:space-between; margin:0 0 12px; }
    .pname{ font-size:1.02rem; font-weight:600; letter-spacing:-.01em; color:var(--ink); }
    .pmeta{
      font-family:var(--mono); font-size:.62rem; font-weight:500; letter-spacing:.1em;
      text-transform:uppercase; color:var(--muted);
    }

    /* item table micro-headers + totals strip */
    .fh{
      font-family:var(--mono); font-size:.58rem; font-weight:500; letter-spacing:.08em;
      text-transform:uppercase; color:var(--muted); padding:2px 2px 0;
    }
    .totals{
      font-family:var(--mono); font-size:.76rem; color:var(--muted);
      border-top:1px solid var(--line-2); border-bottom:1px solid var(--line-2);
      padding:9px 2px; margin:10px 0 12px; letter-spacing:.01em;
    }
    .totals b{ color:var(--ink); font-weight:600; }
    .avg-line{ font-family:var(--mono); font-size:.7rem; color:var(--muted); margin:2px 0 12px; }
    .avg-line b{ color:var(--ink); font-weight:600; }

    /* trend stat cells — hairline top rule, mono values */
    .sg-head{ display:flex; align-items:center; font-weight:600; font-size:.94rem; margin:14px 0 12px; color:var(--ink); }
    .statgrid{ display:grid; grid-template-columns:repeat(6,1fr); gap:14px; margin-bottom:10px; }
    .sg{ border-top:1px solid var(--line-2); padding-top:9px; }
    .sg .v{ font-family:var(--mono); font-size:1.02rem; font-weight:600; letter-spacing:-.01em; color:var(--ink); }
    .sg .l{
      font-family:var(--mono); font-size:.6rem; font-weight:500; letter-spacing:.08em;
      text-transform:uppercase; color:var(--muted); margin-top:4px;
    }

    /* in-app log — scrollable ledger table, sticky header, theme-proof */
    .logwrap{
      max-height:440px; overflow:auto; border:1px solid var(--line-2);
      border-radius:14px; background:var(--card);
    }
    table.ledger{ width:100%; min-width:760px; border-collapse:collapse; background:var(--card); }
    .ledger th{
      position:sticky; top:0; z-index:1; background:var(--card);
      font-family:var(--mono); font-size:.6rem; font-weight:500; letter-spacing:.09em;
      text-transform:uppercase; color:var(--muted); text-align:left;
      padding:11px 12px 9px; border-bottom:1px solid var(--line-2);
    }
    .ledger th.num{ text-align:right; width:58px; }
    .ledger th.date{ width:112px; }
    .ledger th.items{ width:26%; }
    .ledger td{
      padding:10px 12px; border-bottom:1px solid var(--line);
      font-size:.85rem; color:var(--ink); vertical-align:top;
    }
    .ledger tr:last-child td{ border-bottom:none; }
    .ledger td.date{ font-family:var(--mono); font-size:.76rem; font-weight:600; white-space:nowrap; }
    .ledger td.num{ font-family:var(--mono); font-size:.78rem; text-align:right; white-space:nowrap; }
    .ledger td.items{ color:var(--ink); font-size:.82rem; }
    .ledger td.notes{ color:var(--muted); font-size:.8rem; }
    .ledger tr:hover td{ background:#fbfbfc; }

    .foot{
      font-family:var(--mono); font-size:.64rem; letter-spacing:.12em; text-transform:uppercase;
      color:var(--muted); text-align:center; margin-top:40px;
    }

    /* password gate */
    .gate-wrap{ text-align:center; margin-top:10vh; margin-bottom:6px; }
    .gate-kicker{
      font-family:var(--mono); font-size:.64rem; font-weight:500; letter-spacing:.2em;
      text-transform:uppercase; color:var(--muted);
    }
    .gate-title{ font-size:1.5rem; font-weight:700; letter-spacing:-.03em; margin-top:8px; }
    .gate-sub{ color:var(--muted); font-size:.86rem; margin-top:4px; }

    @media (max-width:860px){
      .mast{ align-items:flex-start; }
      .mast-stats{ gap:22px; }
      .mstat{ text-align:left; }
      .statgrid{ grid-template-columns:repeat(3,1fr); }
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
    st.caption("Every entry is saved to your Google Sheet and shown in the Log section below")
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
# Side-by-side check-in panels — Abhi left, Shamal right.
# Each day is a list of line items; totals are computed automatically.
# =============================================================================
def reset_person_day_state(person_key, date_str):
    st.session_state.pop(f"notes_{person_key}_{date_str}", None)
    clear_item_widget_state(person_key)
    st.session_state.pop(f"food_items_{person_key}", None)
    st.session_state[f"loaded_{person_key}"] = None


def fmt_g(x) -> str:
    return f"{safe_float(x):g}"


def render_panel(person_key):
    cfg = PEOPLE[person_key]
    dfp = data[person_key]
    ws = ws_map[person_key]
    existing = by_date[person_key].get(sel_iso)

    # Reload this person's item rows whenever the selected day changes.
    if st.session_state.get(f"loaded_{person_key}") != sel_iso:
        existing_items = parse_items(existing.get("items_json", "")) if existing is not None else []
        if not existing_items and existing is not None:
            # Day saved by an older version (totals only, no items):
            # carry the totals into one editable line so nothing is lost.
            seed = {
                "name": "(previous total)",
                "calories": safe_int(existing.get("calories", 0)),
                "protein": safe_float(existing.get("protein", 0.0)),
                "carbs": safe_float(existing.get("carbs", 0.0)),
                "fat": safe_float(existing.get("fat", 0.0)),
                "fiber": safe_float(existing.get("fiber", 0.0)),
            }
            if any(safe_float(seed[m]) for m in MACROS):
                existing_items = [seed]
        load_items_into_state(person_key, existing_items)
        st.session_state[f"loaded_{person_key}"] = sel_iso

    w7 = get_window_df(dfp, 7)
    if len(w7):
        avg_html = (
            f'7-day avg — <b>{int(round(w7["calories"].mean())):,} cal</b>'
            f' · P <b>{fmt_g(w7["protein"].mean())}</b>'
            f' · C <b>{fmt_g(w7["carbs"].mean())}</b>'
            f' · F <b>{fmt_g(w7["fat"].mean())}</b>'
            f' · Fib <b>{fmt_g(w7["fiber"].mean())}</b>'
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

        # ---- item rows: name · cal · protein · carbs · fat · fiber ----
        head = st.columns(ITEM_COLS, gap="small")
        for col, label in zip(head, ["Item", "Cal", "Pro", "Carb", "Fat", "Fib", ""]):
            col.markdown(f'<div class="fh">{label}</div>', unsafe_allow_html=True)

        items_state = st.session_state.get(f"food_items_{person_key}", [empty_item()])
        for i, item in enumerate(items_state):
            cols = st.columns(ITEM_COLS, gap="small")
            cols[0].text_input(
                "Item", value=str(item.get("name", "")),
                placeholder="Chicken bowl, shake, oats…",
                label_visibility="collapsed", key=f"fname_{person_key}_{i}",
            )
            cols[1].number_input(
                "Cal", min_value=0, max_value=20000,
                value=safe_int(item.get("calories", 0)),
                label_visibility="collapsed", key=f"fcal_{person_key}_{i}",
            )
            cols[2].number_input(
                "Pro", min_value=0.0, max_value=1000.0,
                value=float(safe_float(item.get("protein", 0.0))), format="%.1f",
                label_visibility="collapsed", key=f"fpro_{person_key}_{i}",
            )
            cols[3].number_input(
                "Carb", min_value=0.0, max_value=2000.0,
                value=float(safe_float(item.get("carbs", 0.0))), format="%.1f",
                label_visibility="collapsed", key=f"fcarb_{person_key}_{i}",
            )
            cols[4].number_input(
                "Fat", min_value=0.0, max_value=1000.0,
                value=float(safe_float(item.get("fat", 0.0))), format="%.1f",
                label_visibility="collapsed", key=f"ffat_{person_key}_{i}",
            )
            cols[5].number_input(
                "Fib", min_value=0.0, max_value=500.0,
                value=float(safe_float(item.get("fiber", 0.0))), format="%.1f",
                label_visibility="collapsed", key=f"ffib_{person_key}_{i}",
            )
            if cols[6].button("×", key=f"rm_{person_key}_{i}", help="Remove this item"):
                current = raw_items_from_widgets(person_key)
                if i < len(current):
                    current.pop(i)
                load_items_into_state(person_key, current)
                st.rerun()

        add_c, copy_c = st.columns([1, 1.35])
        with add_c:
            if st.button("Add item", use_container_width=True, key=f"add_{person_key}"):
                current = raw_items_from_widgets(person_key)
                current.append(empty_item())
                load_items_into_state(person_key, current)
                st.rerun()
        with copy_c:
            if st.button("Copy yesterday", use_container_width=True, key=f"copy_{person_key}",
                         help="Fill the items from this person's previous day"):
                prev = by_date[person_key].get((sel_date - timedelta(days=1)).isoformat())
                prev_items = parse_items(prev.get("items_json", "")) if prev is not None else []
                if prev_items:
                    load_items_into_state(person_key, prev_items)
                    st.toast(f"{cfg['name']} — copied yesterday, remember to save")
                    st.rerun()
                else:
                    st.toast("Nothing logged the day before")

        # ---- live totals for the rows above ----
        t = totals_from(clean_items(raw_items_from_widgets(person_key)))
        st.markdown(
            f'<div class="totals">Totals — <b>{t["calories"]:,} cal</b>'
            f' · P <b>{fmt_g(t["protein"])}</b>'
            f' · C <b>{fmt_g(t["carbs"])}</b>'
            f' · F <b>{fmt_g(t["fat"])}</b>'
            f' · Fib <b>{fmt_g(t["fiber"])}</b></div>',
            unsafe_allow_html=True,
        )

        notes = st.text_area(
            "Notes / progress",
            value=str(existing.get("notes", "")) if existing is not None else "",
            placeholder="Weigh-in, wins, slip-ups, energy — anything worth remembering.",
            height=90, key=f"notes_{person_key}_{sel_iso}",
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
        action = upsert_entry(ws, sel_date, raw_items_from_widgets(person_key), notes)
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
# Log — every saved day, right here in the app
# =============================================================================
def ledger_html(dfp):
    dd = dfp.copy()
    dd["date_dt"] = pd.to_datetime(dd["date"], errors="coerce")
    dd = dd.sort_values("date_dt", ascending=False)

    ths = "".join(
        f'<th class="{c}">{t}</th>'
        for t, c in [
            ("Date", "date"), ("Cal", "num"), ("Pro", "num"), ("Carb", "num"),
            ("Fat", "num"), ("Fib", "num"), ("Items", "items"), ("Notes", "notes"),
        ]
    )

    rows = []
    for _, r in dd.iterrows():
        if pd.notna(r["date_dt"]):
            ds = r["date_dt"].strftime("%b %d, %Y")
        else:
            ds = escape(str(r["date"]))
        names = ", ".join(i["name"] for i in parse_items(r["items_json"]) if i["name"])
        notes = str(r["notes"]).strip()
        rows.append(
            f'<tr><td class="date">{ds}</td>'
            f'<td class="num">{safe_int(r["calories"]):,}</td>'
            f'<td class="num">{fmt_g(r["protein"])}</td>'
            f'<td class="num">{fmt_g(r["carbs"])}</td>'
            f'<td class="num">{fmt_g(r["fat"])}</td>'
            f'<td class="num">{fmt_g(r["fiber"])}</td>'
            f'<td class="items">{escape(names) if names else "—"}</td>'
            f'<td class="notes">{escape(notes)}</td></tr>'
        )

    return (
        f'<div class="logwrap"><table class="ledger">'
        f'<thead><tr>{ths}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


st.divider()
st.markdown('<div class="eyebrow">Log — every saved day</div>', unsafe_allow_html=True)
log_tabs = st.tabs([PEOPLE[key]["name"] for key in PEOPLE])
for tab, key in zip(log_tabs, PEOPLE):
    with tab:
        dfp = data[key]
        if dfp.empty:
            st.info("No entries yet — save a day above and it will show up here.")
        else:
            st.markdown(ledger_html(dfp), unsafe_allow_html=True)
            st.caption(f"{len(dfp)} day{'s' if len(dfp) != 1 else ''} logged · protein, carbs, fat and fiber in grams")


# =============================================================================
# Head-to-head trends
# =============================================================================
def statgrid_html(person_key, w, days):
    cfg = PEOPLE[person_key]
    n = len(w)
    avg_cal = int(round(w["calories"].mean())) if n else 0
    avgs = {m: (w[m].mean() if n else 0.0) for m in ("protein", "carbs", "fat", "fiber")}
    return (
        f'<div class="sg-head"><span class="dot {cfg["css"]}"></span>{cfg["name"]}</div>'
        f'<div class="statgrid">'
        f'<div class="sg"><div class="v">{n}/{days}</div><div class="l">days logged</div></div>'
        f'<div class="sg"><div class="v">{avg_cal:,}</div><div class="l">avg cal</div></div>'
        f'<div class="sg"><div class="v">{fmt_g(avgs["protein"])} g</div><div class="l">avg protein</div></div>'
        f'<div class="sg"><div class="v">{fmt_g(avgs["carbs"])} g</div><div class="l">avg carbs</div></div>'
        f'<div class="sg"><div class="v">{fmt_g(avgs["fat"])} g</div><div class="l">avg fat</div></div>'
        f'<div class="sg"><div class="v">{fmt_g(avgs["fiber"])} g</div><div class="l">avg fiber</div></div>'
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
            labelColor="#5f5f68", titleColor="#5f5f68",
            domainColor="#d6d6dc", tickColor="#d6d6dc", gridColor="#ededf0",
            labelFont="IBM Plex Mono", titleFont="IBM Plex Mono",
            labelFontSize=11, titleFontSize=11,
        )
        .configure_legend(labelFont="IBM Plex Mono", labelFontSize=11, labelColor="#18181d")
    )


st.divider()
with st.expander("Trends", expanded=False):
    trend_days = st.radio(
        "Time window", TREND_WINDOW_OPTIONS, index=0,
        format_func=lambda d: f"Last {d} days",
        horizontal=True, label_visibility="collapsed", key="trend_window",
    )

    windows = {key: get_window_df(data[key], trend_days) for key in PEOPLE}

    for key in PEOPLE:
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
            st.markdown('<div class="eyebrow">Carbs (g)</div>', unsafe_allow_html=True)
            st.altair_chart(
                duo_chart(comb, "carbs", "Carbs (g)", trend_days, fmt=",.1f"),
                use_container_width=True,
            )

        c3, c4 = st.columns(2, gap="large")
        with c3:
            st.markdown('<div class="eyebrow">Fat (g)</div>', unsafe_allow_html=True)
            st.altair_chart(
                duo_chart(comb, "fat", "Fat (g)", trend_days, fmt=",.1f"),
                use_container_width=True,
            )
        with c4:
            st.markdown('<div class="eyebrow">Fiber (g)</div>', unsafe_allow_html=True)
            st.altair_chart(
                duo_chart(comb, "fiber", "Fiber (g)", trend_days, fmt=",.1f"),
                use_container_width=True,
            )

st.markdown(
    '<div class="foot">Shred Sheet · Abhi × Shamal · data lives in your Google Sheet</div>',
    unsafe_allow_html=True,
)
