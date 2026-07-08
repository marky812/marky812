import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from html import escape
from itertools import groupby
from uuid import uuid4

import altair as alt
import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(
    page_title="Wellness Tracker",
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# =============================================================================
# Google Sheets setup  (logic unchanged — only the UI around it is new)
# =============================================================================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DEFAULT_SPREADSHEET_NAME = "Streamlit Calories Tracker"
# New worksheet name so your old daily_log sheet does not get overwritten.
DEFAULT_WORKSHEET_NAME = "wellness_log_v2"

HEADERS = [
    "date",
    "night_ate",
    "took_medicine",
    "meditation_listen",
    "no_food_4h_before_bed",
    "items_json",
    "total_calories",
    "total_protein",
    "notes",
    "updated_at",
    # New habit columns — appended so existing data stays a clean prefix and
    # is preserved by the additive migration in get_or_create_worksheet().
    "brush_1",
    "brush_2",
    "harambe",
    "walk",
]

BOOL_COLUMNS = [
    "night_ate",
    "took_medicine",
    "meditation_listen",
    "no_food_4h_before_bed",
    "brush_1",
    "brush_2",
    "harambe",
    "walk",
]

HABIT_LABELS = {
    "night_ate": "No night eating?",
    "took_medicine": "Took medicine?",
    "meditation_listen": "Meditation / Listen?",
    "no_food_4h_before_bed": "No food 4 hours before bed?",
    "brush_1": "Brush #1",
    "brush_2": "Brush #2",
    "harambe": "Harambe",
    "walk": "Walk",
}

# Events calendar (bottom of the page). Events live in their OWN worksheet in
# the same spreadsheet, so the daily wellness log above is never touched.
DEFAULT_EVENTS_WORKSHEET_NAME = "events"
EVENT_HEADERS = ["id", "date", "time", "title", "notes", "created_at"]

# Trend windows the user can pick from in the Trends expander.
TREND_WINDOW_OPTIONS = [7, 14, 30, 60, 90]


# =============================================================================
# Access gate
# The plaintext password is NEVER stored here — only its SHA-256 hash is.
# (For even tighter security on a public repo, move this hash into
#  st.secrets["app_password_sha256"] instead of committing it.)
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
                          background:linear-gradient(135deg,#2563eb,#0f172a);
                          box-shadow:0 14px 28px -14px rgba(15,23,42,.65)">🔒</div>
              <div style="font-family:'Rubik',sans-serif;font-size:1.5rem;font-weight:800;
                          letter-spacing:-.02em;color:#16213a;margin-top:12px">Daily Wellness</div>
              <div style="color:#64748b;font-size:.9rem;margin-top:3px">
                Enter the password to continue.</div>
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
# Helpers  (unchanged, plus calc_streak)
# =============================================================================
def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "yes", "y", "1", "checked", "x"}


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
    """1-indexed column number -> spreadsheet column letters (e.g. 14 -> 'N')."""
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def parse_items(value):
    if value is None:
        return []

    value_str = str(value).strip()
    if not value_str:
        return []

    try:
        raw_items = json.loads(value_str)
    except json.JSONDecodeError:
        return []

    if not isinstance(raw_items, list):
        return []

    cleaned = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name", "")).strip()
        calories = safe_int(item.get("calories", 0))
        protein = safe_float(item.get("protein", 0.0))

        if name or calories or protein:
            cleaned.append({"name": name, "calories": calories, "protein": round(protein, 1)})

    return cleaned


def clean_items(items):
    cleaned = []
    for item in items:
        name = str(item.get("name", "")).strip()
        calories = max(0, safe_int(item.get("calories", 0)))
        protein = max(0.0, safe_float(item.get("protein", 0.0)))

        if name or calories or protein:
            cleaned.append({"name": name, "calories": calories, "protein": round(protein, 1)})

    return cleaned


def total_calories(items) -> int:
    return int(sum(safe_int(item.get("calories", 0)) for item in items))


def total_protein(items) -> float:
    return round(sum(safe_float(item.get("protein", 0.0)) for item in items), 1)


def calc_streak(logged_dates: set, today: date) -> int:
    """Consecutive days with an entry, counting back from today (or yesterday
    if today hasn't been logged yet, so the streak doesn't drop mid-day)."""
    d = today if today.isoformat() in logged_dates else today - timedelta(days=1)
    streak = 0
    while d.isoformat() in logged_dates:
        streak += 1
        d -= timedelta(days=1)
    return streak


@st.cache_resource(show_spinner=False)
def get_gspread_client():
    if "gcp_service_account_json" not in st.secrets:
        st.error("Missing `gcp_service_account_json` in Streamlit Secrets.")
        st.stop()

    service_account_info = json.loads(st.secrets["gcp_service_account_json"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return gspread.authorize(creds)


def _headers_are_additive(old_headers, new_headers) -> bool:
    """True when new_headers only ADDS columns to the end of old_headers."""
    return len(new_headers) >= len(old_headers) and new_headers[: len(old_headers)] == old_headers


@st.cache_resource(show_spinner=False)
def get_or_create_worksheet():
    gc = get_gspread_client()

    spreadsheet_name = st.secrets.get("spreadsheet_name", DEFAULT_SPREADSHEET_NAME)
    worksheet_name = st.secrets.get("worksheet_name", DEFAULT_WORKSHEET_NAME)

    try:
        spreadsheet = gc.open(spreadsheet_name)
    except gspread.SpreadsheetNotFound:
        # Created silently on first run; it belongs to the service account.
        spreadsheet = gc.create(spreadsheet_name)

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(HEADERS))
        worksheet.append_row(HEADERS)
        return worksheet, spreadsheet.url, worksheet_name

    current_headers = worksheet.row_values(1)

    # If someone points this app at the old daily_log schema, do not clear it.
    # Instead, create/use a v2 worksheet beside it.
    if current_headers and current_headers != HEADERS:
        v2_name = f"{worksheet_name}_v2" if not worksheet_name.endswith("_v2") else worksheet_name

        if v2_name != worksheet_name:
            try:
                worksheet = spreadsheet.worksheet(v2_name)
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(title=v2_name, rows=1000, cols=len(HEADERS))
                worksheet.append_row(HEADERS)
                return worksheet, spreadsheet.url, v2_name

            current_headers = worksheet.row_values(1)

    if not current_headers:
        worksheet.append_row(HEADERS)
    elif current_headers != HEADERS:
        if _headers_are_additive(current_headers, HEADERS):
            # Only new columns were added (e.g. the brush/harambe/walk habits).
            # Widen the grid if needed and rewrite the header row IN PLACE so
            # every existing entry is preserved.
            if worksheet.col_count < len(HEADERS):
                worksheet.add_cols(len(HEADERS) - worksheet.col_count)
            worksheet.update(f"A1:{col_letter(len(HEADERS))}1", [HEADERS])
        else:
            # Schema changed in a way we can't safely reconcile in place.
            # Preserve existing data by moving to a fresh sibling worksheet
            # rather than clearing this one.
            safe_name = f"{worksheet.title}_v{int(datetime.now().timestamp())}"
            new_ws = spreadsheet.add_worksheet(title=safe_name, rows=1000, cols=len(HEADERS))
            new_ws.append_row(HEADERS)
            return new_ws, spreadsheet.url, safe_name

    return worksheet, spreadsheet.url, worksheet.title


@st.cache_data(ttl=30, show_spinner=False)
def load_data_cached(sheet_title: str, cache_buster: str):
    # cache_buster lets us refresh after saves/deletes.
    worksheet, _, _ = get_or_create_worksheet()
    records = worksheet.get_all_records()

    if not records:
        return pd.DataFrame(columns=HEADERS)

    df = pd.DataFrame(records)

    for col in HEADERS:
        if col not in df.columns:
            df[col] = ""

    df = df[HEADERS].copy()
    df["date"] = df["date"].astype(str)

    for col in BOOL_COLUMNS:
        df[col] = df[col].apply(parse_bool)

    df["total_calories"] = pd.to_numeric(df["total_calories"], errors="coerce").fillna(0).astype(int)
    df["total_protein"] = pd.to_numeric(df["total_protein"], errors="coerce").fillna(0.0).astype(float)
    df["updated_at"] = df["updated_at"].astype(str)

    return df


def find_row_number_by_date(worksheet, selected_date_str):
    dates = worksheet.col_values(1)
    for idx, value in enumerate(dates, start=1):
        if value == selected_date_str:
            return idx
    return None


def upsert_entry(worksheet, selected_date, habit_values, items, notes):
    selected_date_str = selected_date.isoformat()
    cleaned_items = clean_items(items)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    record = {
        "date": selected_date_str,
        "items_json": json.dumps(cleaned_items),
        "total_calories": total_calories(cleaned_items),
        "total_protein": total_protein(cleaned_items),
        "notes": notes.strip(),
        "updated_at": now,
    }
    for key in BOOL_COLUMNS:
        record[key] = bool(habit_values.get(key, False))

    # Build the row in HEADERS order so column placement never matters.
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
# Events — stored in their own worksheet inside the same spreadsheet
# =============================================================================
def event_time_key(value) -> str:
    """Sortable key for free-text times ('7:30pm', '19:00', ...).
    Empty (all-day) events sort first; unparseable text sorts last."""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return pd.to_datetime(text).time().isoformat()
    except Exception:
        return "~" + text.lower()


@st.cache_resource(show_spinner=False)
def get_or_create_events_worksheet():
    gc = get_gspread_client()

    spreadsheet_name = st.secrets.get("spreadsheet_name", DEFAULT_SPREADSHEET_NAME)
    events_name = st.secrets.get("events_worksheet_name", DEFAULT_EVENTS_WORKSHEET_NAME)

    try:
        spreadsheet = gc.open(spreadsheet_name)
    except gspread.SpreadsheetNotFound:
        spreadsheet = gc.create(spreadsheet_name)

    try:
        ws = spreadsheet.worksheet(events_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=events_name, rows=1000, cols=len(EVENT_HEADERS))
        ws.append_row(EVENT_HEADERS)
        return ws

    headers = ws.row_values(1)
    if not headers:
        ws.append_row(EVENT_HEADERS)
    elif headers != EVENT_HEADERS:
        if _headers_are_additive(headers, EVENT_HEADERS):
            if ws.col_count < len(EVENT_HEADERS):
                ws.add_cols(len(EVENT_HEADERS) - ws.col_count)
            ws.update(f"A1:{col_letter(len(EVENT_HEADERS))}1", [EVENT_HEADERS])
        else:
            # A sheet with this name exists but has an unknown schema — don't
            # touch it. Use a fresh sibling worksheet instead.
            safe_name = f"{ws.title}_v{int(datetime.now().timestamp())}"
            new_ws = spreadsheet.add_worksheet(title=safe_name, rows=1000, cols=len(EVENT_HEADERS))
            new_ws.append_row(EVENT_HEADERS)
            return new_ws

    return ws


@st.cache_data(ttl=30, show_spinner=False)
def load_events_cached(events_sheet_title: str, cache_buster: str):
    ws = get_or_create_events_worksheet()
    records = ws.get_all_records()

    if not records:
        return pd.DataFrame(columns=EVENT_HEADERS)

    edf = pd.DataFrame(records)

    for col in EVENT_HEADERS:
        if col not in edf.columns:
            edf[col] = ""

    edf = edf[EVENT_HEADERS].copy()
    for col in EVENT_HEADERS:
        edf[col] = edf[col].astype(str)

    return edf


def add_event(ws, event_date, title, time_str, notes):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        uuid4().hex[:10],
        event_date.isoformat(),
        str(time_str).strip(),
        str(title).strip(),
        str(notes).strip(),
        now,
    ]
    ws.append_row(row)


def delete_event_by_id(ws, event_id) -> bool:
    ids = ws.col_values(1)
    for idx, value in enumerate(ids, start=1):
        if idx > 1 and str(value) == str(event_id):
            ws.delete_rows(idx)
            return True
    return False


def clear_food_widget_state():
    for key in list(st.session_state.keys()):
        if key.startswith("food_name_") or key.startswith("food_cal_") or key.startswith("food_protein_"):
            del st.session_state[key]


def load_items_into_state(items):
    clear_food_widget_state()
    st.session_state.food_items = items if items else [{"name": "", "calories": 0, "protein": 0.0}]


def get_items_from_widgets():
    items = []
    count = len(st.session_state.get("food_items", []))

    for idx in range(count):
        items.append(
            {
                "name": st.session_state.get(f"food_name_{idx}", ""),
                "calories": st.session_state.get(f"food_cal_{idx}", 0),
                "protein": st.session_state.get(f"food_protein_{idx}", 0.0),
            }
        )

    return clean_items(items)


def reset_day_widget_state(date_str):
    for col in BOOL_COLUMNS:
        st.session_state.pop(f"{col}_{date_str}", None)
    st.session_state.pop(f"notes_{date_str}", None)


def get_window_df(df, days: int):
    if df.empty:
        return df.copy()

    end = pd.Timestamp(date.today())
    start = end - pd.Timedelta(days=days - 1)

    tmp = df.copy()
    tmp["date_dt"] = pd.to_datetime(tmp["date"], errors="coerce")
    tmp = tmp[(tmp["date_dt"] >= start) & (tmp["date_dt"] <= end)]
    return tmp.sort_values("date_dt")


# =============================================================================
# Styling — steel blue / navy identity with amber + teal accents, Rubik
# throughout. Hierarchy comes from Rubik's weight range (400 body → 800/900
# display). Variable names kept from the old palette (--mag/--pur) so every
# selector below works unchanged — only the color VALUES changed.
# =============================================================================
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;600;700;800;900&display=swap');

    :root{
      --mag-soft:#bfdbfe; --mag:#2563eb; --mag-deep:#1e40af;
      --yel-soft:#fdf0cf; --yel:#f59e0b; --yel-deep:#d97706; --yel-ink:#92600a;
      --cyn-soft:#c7f0ea; --cyn:#2dd4bf; --cyn-deep:#0f766e;
      --red:#dc2626; --pur:#0f172a; --grn:#16a34a;
      --bg:#f7f8fa; --card:#FFFFFF;
      --ink:#16213a; --muted:#64748b; --line:#e2e8f0;
    }

    html, body, [class*="css"], .stMarkdown, button, input, textarea, select,
    h1, h2, h3, h4, h5, h6{
      font-family:'Rubik',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif !important;
    }
    [data-testid="stAppViewContainer"]{ background:var(--bg); }
    .block-container{ padding-top:1.3rem; padding-bottom:3rem; max-width:1180px; }
    #MainMenu, footer{ visibility:hidden; }

    div[data-testid="stButton"] > button{
      width:100%; border-radius:14px; border:1.5px solid var(--line); background:var(--card);
      min-height:42px; font-weight:600; transition:all .12s ease;
    }
    div[data-testid="stButton"] > button:hover{
      border-color:var(--mag-soft); color:var(--mag-deep); transform:translateY(-1px);
    }
    button[kind="primary"], button[kind="primaryFormSubmit"]{
      background-image:linear-gradient(135deg,var(--mag),var(--pur)) !important;
      border:0 !important; color:#fff !important;
      box-shadow:0 10px 22px -12px rgba(15,23,42,.65) !important;
    }
    button[kind="primary"]:hover, button[kind="primaryFormSubmit"]:hover{ filter:brightness(1.12); }
    .stTextInput input, .stNumberInput input, .stTextArea textarea{
      border-radius:12px !important; background:var(--card);
    }

    /* progress bar (habits done) */
    div[data-testid="stProgress"] > div > div > div > div,
    .stProgress > div > div > div > div{
      background-image:linear-gradient(90deg,var(--mag),var(--pur));
    }

    /* tabs + radio */
    div[data-baseweb="tab-highlight"]{ background:var(--mag); }
    button[data-baseweb="tab"]{ font-weight:600; }
    button[data-baseweb="tab"][aria-selected="true"]{ color:var(--mag-deep); }
    div[data-testid="stRadio"] label{ font-weight:600; }

    /* expander */
    div[data-testid="stExpander"] details{
      border:1.5px solid var(--line); border-radius:16px; background:var(--card);
    }

    /* hero — steel blue→navy base with subtle amber + teal glows layered on top */
    .hero{
      display:flex; align-items:center; justify-content:space-between; gap:22px; flex-wrap:wrap;
      color:#fff; padding:26px 30px; border-radius:26px; margin-bottom:18px;
      background:
        radial-gradient(560px 240px at 88% -30%, rgba(245,158,11,.22), transparent 60%),
        radial-gradient(520px 240px at -8% 130%, rgba(45,212,191,.22), transparent 60%),
        linear-gradient(120deg,var(--mag) 0%,var(--mag-deep) 55%,var(--pur) 100%);
      box-shadow:0 18px 44px -18px rgba(15,23,42,.55);
    }
    .hero-left{ display:flex; align-items:center; gap:16px; }
    .hero-mark{ font-size:30px; line-height:1; }
    .hero-title{ font-size:2rem; font-weight:800; letter-spacing:-.02em; line-height:1.05; }
    .hero-sub{ font-size:.93rem; opacity:.9; font-weight:500; margin-top:3px; }
    .hero-stats{ display:flex; gap:28px; }
    .hstat{ text-align:right; }
    .hstat .v{ font-size:1.5rem; font-weight:800; letter-spacing:-.02em; line-height:1; }
    .hstat .l{ font-size:.7rem; opacity:.85; font-weight:600; text-transform:uppercase; letter-spacing:.07em; margin-top:5px; }

    /* eyebrows + section rows */
    .eyebrow{ font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.12em; color:var(--muted); margin:8px 0 10px; }
    .sec-row{ display:flex; align-items:center; justify-content:space-between; margin:8px 0 10px; }
    .today-pill{ font-size:.78rem; font-weight:700; color:var(--mag-deep); text-decoration:none; background:var(--mag-soft); padding:6px 13px; border-radius:999px; }
    .today-pill:hover{ filter:brightness(1.03); }

    /* quick day strip — the fast way to pick a day without scrolling a calendar */
    .strip{ display:flex; gap:8px; overflow-x:auto; padding:4px 2px 12px; }
    .pill{
      flex:0 0 auto; display:flex; flex-direction:column; align-items:center; gap:4px;
      min-width:66px; padding:10px 8px; border-radius:16px;
      background:var(--card); border:1.5px solid var(--line); color:var(--ink);
      text-decoration:none; transition:transform .12s ease, box-shadow .12s ease, border-color .12s ease;
    }
    .pill:hover{ transform:translateY(-2px); box-shadow:0 10px 20px -12px rgba(15,23,42,.25); border-color:var(--mag-soft); }
    .pill .p-dow{ font-size:.64rem; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
    .pill .p-dom{ font-size:1.15rem; font-weight:800; line-height:1; }
    .p-dot{ width:7px; height:7px; border-radius:50%; border:1.5px solid var(--line); background:transparent; }
    .p-dot.on{ background:var(--mag); border-color:var(--mag); }
    .pill.is-today{ border-color:var(--cyn); box-shadow:0 0 0 2px rgba(45,212,191,.28); }
    .pill.is-selected{
      background:linear-gradient(135deg,var(--mag),var(--pur)); border-color:transparent; color:#fff;
      box-shadow:0 12px 24px -12px rgba(15,23,42,.6);
    }
    .pill.is-selected .p-dow{ color:rgba(255,255,255,.85); }
    .pill.is-selected .p-dot{ border-color:rgba(255,255,255,.55); }
    .pill.is-selected .p-dot.on{ background:#fff; border-color:#fff; }

    /* calendar (inside the expander) */
    .cal-head{ display:grid; grid-template-columns:repeat(7,1fr); gap:8px; margin-bottom:8px; }
    .cal-head span{ text-align:center; font-size:.7rem; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; }
    .cal-grid{ display:grid; grid-template-columns:repeat(7,1fr); gap:8px; }
    .day{
      display:flex; flex-direction:column; gap:6px; min-height:96px; padding:9px 10px; border-radius:14px;
      background:var(--card); border:1.5px solid var(--line); color:var(--ink);
      text-decoration:none; transition:transform .12s ease, box-shadow .12s ease, border-color .12s ease;
    }
    .day:hover{ transform:translateY(-2px); box-shadow:0 10px 22px -12px rgba(15,23,42,.25); border-color:var(--mag-soft); }
    .day-top{ display:flex; align-items:baseline; justify-content:space-between; }
    .day .dow{ font-size:.7rem; font-weight:700; color:var(--muted); }
    .day .dom{ font-size:1.05rem; font-weight:800; }
    .day-empty{ color:#dbe3f0; font-weight:700; font-size:1.05rem; margin:auto; }
    .chips{ display:flex; flex-wrap:wrap; gap:4px; }
    .chip{ font-size:.64rem; font-weight:700; padding:2px 6px; border-radius:999px; white-space:nowrap; }
    .chip.cal{ background:var(--yel-soft); color:var(--yel-ink); }
    .chip.pro{ background:var(--cyn-soft); color:var(--cyn-deep); }
    .dots{ display:flex; gap:4px; margin-top:auto; flex-wrap:wrap; }
    .dot{ width:8px; height:8px; border-radius:50%; background:transparent; border:1.5px solid var(--line); }
    .dot.on{ background:var(--mag); border-color:var(--mag); }

    .day.is-logged{ border-color:var(--mag-soft); background:linear-gradient(180deg,#fff,#f4f8ff); }
    .day.is-today{ border-color:var(--cyn); box-shadow:0 0 0 2px rgba(45,212,191,.28); }
    .day.is-selected{
      background:linear-gradient(140deg,var(--mag),var(--pur)); border-color:transparent; color:#fff;
      box-shadow:0 12px 24px -12px rgba(15,23,42,.62);
    }
    .day.is-selected .dow, .day.is-selected .dom, .day.is-selected .day-empty{ color:#fff; }
    .day.is-selected .chip.cal, .day.is-selected .chip.pro{ background:rgba(255,255,255,.22); color:#fff; }
    .day.is-selected .dot{ border-color:rgba(255,255,255,.55); }
    .day.is-selected .dot.on{ background:#fff; border-color:#fff; }
    .day.is-future .day-empty{ color:#e9eef6; }

    /* month grouping — each month is its own clearly labelled panel */
    .month-card{ border:1.5px solid var(--line); border-radius:20px; padding:14px 18px 18px; background:var(--bg); margin-bottom:16px; }
    .month-head{ display:flex; align-items:baseline; gap:10px; margin-bottom:12px; padding-bottom:10px; border-bottom:1.5px solid var(--line); }
    .month-name{ font-size:1.35rem; font-weight:800; color:var(--ink); letter-spacing:-.02em; }
    .month-name.cur{ background:linear-gradient(120deg,var(--mag),var(--pur)); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; }
    .month-year{ font-size:.92rem; font-weight:700; color:var(--muted); }
    .month-now{ margin-left:auto; font-size:.66rem; font-weight:800; text-transform:uppercase; letter-spacing:.08em; color:#fff; background:linear-gradient(120deg,var(--mag),var(--pur)); padding:4px 11px; border-radius:999px; }
    .month-card .cal-head{ margin-bottom:7px; }
    .cell-blank{ visibility:hidden; }

    /* totals cards — one per accent color: amber=calories, teal=protein, blue=habits */
    .tcard{ border:1.5px solid var(--line); border-radius:20px; padding:18px; text-align:center; background:var(--card); margin-bottom:12px; }
    .tcard .tnum{ font-size:2rem; font-weight:800; letter-spacing:-.03em; line-height:1; color:var(--ink); }
    .tcard .tlab{ color:var(--muted); font-size:.82rem; margin-top:6px; font-weight:600; }
    .tcard.cal{ background:var(--yel-soft); border-color:#f0dda6; }
    .tcard.cal .tnum{ color:var(--yel-ink); }
    .tcard.pro{ background:var(--cyn-soft); border-color:#9fdfd5; }
    .tcard.pro .tnum{ color:var(--cyn-deep); }
    .tcard.hab{ background:#e8efff; border-color:var(--mag-soft); }
    .tcard.hab .tnum{ color:var(--mag-deep); }

    /* events calendar (bottom) */
    .chip.ev{ background:#e8efff; color:var(--mag-deep); border:1px solid var(--mag-soft);
              display:inline-block; max-width:100%; overflow:hidden; text-overflow:ellipsis; }
    .day.static{ cursor:default; }
    .day.static:hover{ transform:none; box-shadow:none; border-color:var(--line); }
    .day.static.is-logged:hover{ border-color:var(--mag-soft); }
    .day.static.is-today:hover{ border-color:var(--cyn); }
    .ev-mini{ display:none; }
    .ev-when{ display:inline-block; font-size:.72rem; font-weight:800; color:var(--mag-deep);
              background:var(--mag-soft); padding:4px 10px; border-radius:999px; white-space:nowrap; }
    .ev-time{ font-size:.74rem; color:var(--muted); font-weight:600; margin-top:4px; }
    .ev-title{ font-weight:700; color:var(--ink); }
    .ev-notes{ color:var(--muted); font-size:.82rem; margin-top:1px; }

    /* metric tiles */
    div[data-testid="stMetric"]{ border:1.5px solid var(--line); border-radius:16px; padding:14px 16px; background:var(--card); }

    .food-h{ font-size:.72rem; opacity:.6; font-weight:700; text-transform:uppercase; letter-spacing:.07em; }
    .foot{ color:var(--muted); font-size:.78rem; text-align:center; margin-top:30px; }

    @media (max-width:760px){
      .hero-stats{ gap:18px; }
      .hstat{ text-align:left; }
      .day{ min-height:62px; padding:6px; gap:3px; }
      .chips, .day .dow{ display:none; }
      .dot{ width:6px; height:6px; }
      .day .dom{ font-size:.9rem; }
      .pill{ min-width:58px; }
      .ev-mini{ display:inline-flex; align-items:center; justify-content:center;
                min-width:18px; height:18px; margin:auto auto 2px; padding:0 5px;
                border-radius:999px; background:var(--mag); color:#fff;
                font-size:.62rem; font-weight:800; }
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
# Load data
# =============================================================================
if "cache_buster" not in st.session_state:
    st.session_state.cache_buster = datetime.now().isoformat()
if "ev_cache_buster" not in st.session_state:
    st.session_state.ev_cache_buster = datetime.now().isoformat()

with st.spinner("Connecting to Google Sheets..."):
    try:
        worksheet, sheet_url, active_worksheet_name = get_or_create_worksheet()
        df = load_data_cached(active_worksheet_name, st.session_state.cache_buster)
    except Exception as e:
        st.exception(e)
        st.stop()


# =============================================================================
# Date window + selection (single source of truth via the ?day= URL param)
# =============================================================================
today = date.today()
today_iso = today.isoformat()

# Calendar window: the previous 7 days, ALL of the current month, and ALL of
# next month — driven by real month boundaries, not a fixed number of days.
first_of_current = today.replace(day=1)
if today.month == 12:
    first_of_next = date(today.year + 1, 1, 1)
else:
    first_of_next = date(today.year, today.month + 1, 1)
if first_of_next.month == 12:
    first_after_next = date(first_of_next.year + 1, 1, 1)
else:
    first_after_next = date(first_of_next.year, first_of_next.month + 1, 1)
last_of_next = first_after_next - timedelta(days=1)

cal_start = min(first_of_current, today - timedelta(days=7))
cal_end = last_of_next
window_days = [cal_start + timedelta(days=i) for i in range((cal_end - cal_start).days + 1)]
window_day_strings = [d.isoformat() for d in window_days]

df_by_date = {row["date"]: row for _, row in df.iterrows()}

qp_day = st.query_params.get("day")
if qp_day in window_day_strings:
    sel_iso = qp_day
elif st.session_state.get("selected_date") in window_day_strings:
    sel_iso = st.session_state.selected_date
else:
    sel_iso = today_iso
st.session_state.selected_date = sel_iso
sel_date = date.fromisoformat(sel_iso)
existing = df_by_date.get(sel_iso)

win7 = get_window_df(df, 7)
logged_7 = len(win7)
streak = calc_streak(set(df_by_date.keys()), today)


# =============================================================================
# Header
# =============================================================================
st.markdown(
    f"""
    <div class="hero">
      <div class="hero-left">
        <div class="hero-mark">✦</div>
        <div>
          <div class="hero-title">Daily Wellness</div>
          <div class="hero-sub">Habit &amp; nutrition check-in · {today.strftime('%A, %B %d')}</div>
        </div>
      </div>
      <div class="hero-stats">
        <div class="hstat">
          <div class="v">🔥 {streak}</div>
          <div class="l">Day streak</div>
        </div>
        <div class="hstat">
          <div class="v">{logged_7}<span style="opacity:.7;font-size:1rem;font-weight:700">/7</span></div>
          <div class="l">This week</div>
        </div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

meta_left, meta_right = st.columns([3, 1])
with meta_left:
    st.caption(f"Connected worksheet: `{active_worksheet_name}`")
with meta_right:
    st.link_button("Open Google Sheet", sheet_url, use_container_width=True)


# =============================================================================
# Quick day strip — last 7 days + today + the next 2, one tap to switch.
# The full month calendars live in the expander below so the check-in form
# is always one short scroll away.
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
    row = df_by_date.get(iso)
    cls = ["pill"]
    if row is not None:
        cls.append("is-logged")
    if iso == today_iso:
        cls.append("is-today")
    if iso == sel_iso:
        cls.append("is-selected")

    dot_cls = "p-dot on" if row is not None else "p-dot"
    label = "Today" if iso == today_iso else d.strftime("%a")
    state = "logged" if row is not None else ("upcoming" if d > today else "no entry")
    title = f"{d.strftime('%a, %b %d')} — {state}"

    return (
        f'<a class="{" ".join(cls)}" href="?day={iso}" target="_self" title="{title}">'
        f'<span class="p-dow">{label}</span>'
        f'<span class="p-dom">{d.day}</span>'
        f'<span class="{dot_cls}"></span></a>'
    )


strip_days = [today + timedelta(days=i) for i in range(-7, 3)]
st.markdown(
    '<div class="strip">' + "".join(render_pill(d) for d in strip_days) + "</div>",
    unsafe_allow_html=True,
)


# =============================================================================
# Full calendar (this month + next), tucked away until needed
# =============================================================================
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
dow_header = "".join(f"<span>{d}</span>" for d in WEEKDAYS)


def render_day_cell(d):
    iso = d.isoformat()
    row = df_by_date.get(iso)
    cls = ["day"]
    if row is not None:
        cls.append("is-logged")
    if d > today:
        cls.append("is-future")
    if iso == today_iso:
        cls.append("is-today")
    if iso == sel_iso:
        cls.append("is-selected")

    top = (
        f'<div class="day-top"><span class="dow">{d.strftime("%a")}</span>'
        f'<span class="dom">{d.day}</span></div>'
    )

    if row is not None:
        cal = safe_int(row.get("total_calories", 0))
        pro = safe_float(row.get("total_protein", 0))
        dots = "".join(
            f'<span class="dot{" on" if parse_bool(row.get(c, False)) else ""}"></span>'
            for c in BOOL_COLUMNS
        )
        checked = sum(parse_bool(row.get(c, False)) for c in BOOL_COLUMNS)
        title = f"{d.strftime('%a, %b %d')} — {cal:,} cal · {pro:g}g protein · {checked}/{len(BOOL_COLUMNS)} checked"
        body = (
            f'<div class="chips"><span class="chip cal">{cal:,} cal</span>'
            f'<span class="chip pro">{pro:g}g</span></div>'
            f'<div class="dots">{dots}</div>'
        )
    else:
        state = "upcoming" if d > today else "no entry"
        title = f"{d.strftime('%a, %b %d')} — {state}"
        body = '<div class="day-empty">+</div>'

    return f'<a class="{" ".join(cls)}" href="?day={iso}" target="_self" title="{title}">{top}{body}</a>'


# Group the window into calendar months, each rendered as its own labelled panel.
calendar_html = ""
for (yr, mo), group in groupby(window_days, key=lambda d: (d.year, d.month)):
    month_days = list(group)
    is_current = yr == today.year and mo == today.month
    now_pill = '<span class="month-now">This month</span>' if is_current else ""
    header = (
        '<div class="month-head">'
        f'<span class="month-name{" cur" if is_current else ""}">{month_days[0].strftime("%B")}</span>'
        f'<span class="month-year">{yr}</span>{now_pill}</div>'
    )

    cells = ['<div class="cell-blank"></div>'] * month_days[0].weekday()
    cells.extend(render_day_cell(d) for d in month_days)

    calendar_html += (
        f'<div class="month-card">{header}'
        f'<div class="cal-head">{dow_header}</div>'
        f'<div class="cal-grid">{"".join(cells)}</div></div>'
    )

with st.expander("📅 Full calendar — this month & next"):
    st.markdown(calendar_html, unsafe_allow_html=True)

st.write("")


# =============================================================================
# Reload food-item widgets when the selected day changes  (logic unchanged)
# =============================================================================
if st.session_state.get("loaded_date") != sel_iso:
    existing_items = parse_items(existing.get("items_json", "")) if existing is not None else []
    load_items_into_state(existing_items)
    st.session_state.loaded_date = sel_iso


# =============================================================================
# Entry editor
# =============================================================================
st.markdown('<div class="eyebrow">Check in for this day</div>', unsafe_allow_html=True)
entry_col, totals_col = st.columns([2.2, 1], gap="large")

with entry_col:
    with st.container(border=True):
        st.subheader(sel_date.strftime("%A, %B %d, %Y"))

        st.markdown('<div class="eyebrow" style="margin-top:2px">Daily habits</div>', unsafe_allow_html=True)

        habit_values = {}
        half = (len(BOOL_COLUMNS) + 1) // 2
        habit_cols = st.columns(2)
        for i, key in enumerate(BOOL_COLUMNS):
            target = habit_cols[0] if i < half else habit_cols[1]
            with target:
                habit_values[key] = st.checkbox(
                    HABIT_LABELS[key],
                    value=parse_bool(existing.get(key, False)) if existing is not None else False,
                    key=f"{key}_{sel_iso}",
                )

        done_count = sum(1 for v in habit_values.values() if v)
        st.progress(
            done_count / len(BOOL_COLUMNS),
            text=f"{done_count} of {len(BOOL_COLUMNS)} habits done",
        )

        st.markdown('<div class="eyebrow" style="margin-top:14px">Food log</div>', unsafe_allow_html=True)

        head = st.columns([4.8, 1.55, 1.55, 0.55])
        head[0].markdown('<div class="food-h">Item</div>', unsafe_allow_html=True)
        head[1].markdown('<div class="food-h">Calories</div>', unsafe_allow_html=True)
        head[2].markdown('<div class="food-h">Protein</div>', unsafe_allow_html=True)
        head[3].markdown('<div class="food-h">&nbsp;</div>', unsafe_allow_html=True)

        for idx, item in enumerate(st.session_state.food_items):
            cols = st.columns([4.8, 1.55, 1.55, 0.55])
            cols[0].text_input(
                "Item", value=item.get("name", ""),
                placeholder="Greek yogurt, Fairlife, chicken, etc.",
                label_visibility="collapsed", key=f"food_name_{idx}",
            )
            cols[1].number_input(
                "Calories", min_value=0, max_value=20000,
                value=safe_int(item.get("calories", 0)), step=25,
                label_visibility="collapsed", key=f"food_cal_{idx}",
            )
            cols[2].number_input(
                "Protein", min_value=0.0, max_value=1000.0,
                value=float(safe_float(item.get("protein", 0.0))), step=1.0, format="%.1f",
                label_visibility="collapsed", key=f"food_protein_{idx}",
            )
            if cols[3].button("×", key=f"remove_food_{idx}", help="Remove item"):
                current_items = get_items_from_widgets()
                if idx < len(current_items):
                    current_items.pop(idx)
                load_items_into_state(current_items)
                st.rerun()

        add_col, copy_col, _ = st.columns([1.1, 1.35, 1.55])
        with add_col:
            if st.button("＋ Add item", key="add_food_btn", use_container_width=True):
                current_items = get_items_from_widgets()
                current_items.append({"name": "", "calories": 0, "protein": 0.0})
                load_items_into_state(current_items)
                st.rerun()
        with copy_col:
            if st.button("⧉ Copy yesterday", key="copy_yday_btn", use_container_width=True,
                         help="Fill the food log from the previous day's entry"):
                prev_row = df_by_date.get((sel_date - timedelta(days=1)).isoformat())
                prev_items = parse_items(prev_row.get("items_json", "")) if prev_row is not None else []
                if prev_items:
                    load_items_into_state(prev_items)
                    st.toast("Copied yesterday's food — remember to save", icon="⧉")
                    st.rerun()
                else:
                    st.toast("Nothing logged the day before", icon="🤷")

        notes = st.text_area(
            "Notes",
            value=str(existing.get("notes", "")) if existing is not None else "",
            placeholder="Optional: hunger, mood, sleep, dinner time — whatever you want to remember.",
            height=90, key=f"notes_{sel_iso}",
        )

        save_col, delete_col, refresh_col = st.columns([1.4, 1, 1])
        with save_col:
            save_clicked = st.button(
                f"Save · {sel_date.strftime('%b %d')}",
                type="primary", use_container_width=True, key="save_day_btn",
            )
        with delete_col:
            delete_clicked = st.button("Delete", disabled=existing is None, use_container_width=True, key="delete_day_btn")
        with refresh_col:
            refresh_clicked = st.button("Refresh", use_container_width=True, key="refresh_day_btn")

with totals_col:
    current_items = get_items_from_widgets()
    day_calories = total_calories(current_items)
    day_protein = total_protein(current_items)
    habit_yes_count = sum(1 for v in habit_values.values() if v)

    st.markdown(
        f'<div class="tcard cal"><div class="tnum">{day_calories:,}</div>'
        f'<div class="tlab">calories logged</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="tcard pro"><div class="tnum">{day_protein:g}g</div>'
        f'<div class="tlab">protein logged</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="tcard hab"><div class="tnum">{habit_yes_count}/{len(BOOL_COLUMNS)}</div>'
        f'<div class="tlab">habits checked</div></div>',
        unsafe_allow_html=True,
    )

# ---- handle editor actions ----
if save_clicked:
    items = get_items_from_widgets()
    action = upsert_entry(
        worksheet=worksheet, selected_date=sel_date,
        habit_values=habit_values, items=items, notes=notes,
    )
    st.session_state.cache_buster = datetime.now().isoformat()
    st.toast("Saved" if action == "added" else "Updated", icon="✅")
    st.rerun()

if delete_clicked:
    if delete_entry(worksheet, sel_date):
        st.session_state.cache_buster = datetime.now().isoformat()
        reset_day_widget_state(sel_iso)
        st.session_state.loaded_date = None
        st.toast("Entry deleted", icon="🗑️")
        st.rerun()
    else:
        st.warning("No entry found for this date.")

if refresh_clicked:
    st.session_state.cache_buster = datetime.now().isoformat()
    st.session_state.ev_cache_buster = datetime.now().isoformat()
    st.rerun()


# =============================================================================
# Trends — tucked into an expander, with a selectable time window instead of
# the old fixed 7-day / 30-day tabs. Calories in amber, protein in teal.
# =============================================================================
def trend_bars(data, ycol, ylabel, colors):
    grad = alt.Gradient(
        gradient="linear",
        stops=[alt.GradientStop(color=colors[0], offset=0), alt.GradientStop(color=colors[1], offset=1)],
        x1=1, x2=1, y1=0, y2=1,
    )
    return (
        alt.Chart(data)
        .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6, color=grad, size=20)
        .encode(
            x=alt.X("date_dt:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=-40)),
            y=alt.Y(f"{ycol}:Q", title=ylabel),
            tooltip=[
                alt.Tooltip("date_dt:T", title="Date", format="%b %d"),
                alt.Tooltip(f"{ycol}:Q", title=ylabel),
            ],
        )
        .properties(height=270)
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor="#64748b", titleColor="#64748b",
            domainColor="#e2e8f0", tickColor="#e2e8f0", gridColor="#eef2f7",
            labelFont="Rubik", titleFont="Rubik",
        )
    )


def render_window(df, days):
    win = get_window_df(df, days)
    logged = len(win)
    avg_cal = int(round(win["total_calories"].mean())) if logged else 0
    avg_pro = round(win["total_protein"].mean(), 1) if logged else 0.0

    m = st.columns(4)
    m[0].metric("Days logged", f"{logged}/{days}")
    m[1].metric("Avg calories", f"{avg_cal:,}")
    m[2].metric("Avg protein", f"{avg_pro:g} g")
    m[3].metric("Total calories", f"{int(win['total_calories'].sum()) if logged else 0:,}")

    # Habit completion %, shown in rows of 4 so all habits fit.
    for start in range(0, len(BOOL_COLUMNS), 4):
        chunk = BOOL_COLUMNS[start:start + 4]
        boxes = st.columns(4)
        for box, key in zip(boxes, chunk):
            pct = int(round(win[key].mean() * 100)) if logged else 0
            box.metric(HABIT_LABELS[key].rstrip("?"), f"{pct}%")

    if win.empty:
        st.info(f"No entries in the last {days} days yet — pick a day above to check in.")
        return

    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown("**Calories by day**")
        st.altair_chart(trend_bars(win, "total_calories", "Calories", ("#f59e0b", "#d97706")),
                        use_container_width=True)
    with c2:
        st.markdown("**Protein by day**")
        st.altair_chart(trend_bars(win, "total_protein", "Protein (g)", ("#2dd4bf", "#0f766e")),
                        use_container_width=True)


st.divider()
with st.expander("📊 Trends"):
    trend_days = st.radio(
        "Time window",
        TREND_WINDOW_OPTIONS,
        index=0,
        format_func=lambda d: f"Last {d} days",
        horizontal=True,
        label_visibility="collapsed",
        key="trend_window",
    )
    render_window(df, trend_days)


# =============================================================================
# All entries
# =============================================================================
st.divider()
with st.expander("All saved entries"):
    if df.empty:
        st.info("No entries yet.")
    else:
        display_df = df.copy()
        display_df["date_dt"] = pd.to_datetime(display_df["date"], errors="coerce")
        display_df = display_df.sort_values("date_dt", ascending=False)
        display_df["items"] = display_df["items_json"].apply(
            lambda value: ", ".join(item["name"] for item in parse_items(value) if item.get("name"))
        )

        st.dataframe(
            display_df[
                [
                    "date_dt", "total_calories", "total_protein",
                    "night_ate", "took_medicine", "meditation_listen", "no_food_4h_before_bed",
                    "brush_1", "brush_2", "harambe", "walk",
                    "items", "notes", "updated_at",
                ]
            ],
            use_container_width=True,
            hide_index=True,
            column_config={
                "date_dt": st.column_config.DateColumn("Date", format="MMM D, YYYY"),
                "total_calories": st.column_config.NumberColumn("Calories", format="%d"),
                "total_protein": st.column_config.NumberColumn("Protein (g)", format="%.1f"),
                "night_ate": st.column_config.CheckboxColumn("No night eating"),
                "took_medicine": st.column_config.CheckboxColumn("Medicine"),
                "meditation_listen": st.column_config.CheckboxColumn("Meditation"),
                "no_food_4h_before_bed": st.column_config.CheckboxColumn("No food 4h"),
                "brush_1": st.column_config.CheckboxColumn("Brush #1"),
                "brush_2": st.column_config.CheckboxColumn("Brush #2"),
                "harambe": st.column_config.CheckboxColumn("Harambe"),
                "walk": st.column_config.CheckboxColumn("Walk"),
                "items": st.column_config.TextColumn("Items", width="large"),
                "notes": st.column_config.TextColumn("Notes", width="large"),
                "updated_at": st.column_config.TextColumn("Updated"),
            },
        )

# =============================================================================
# Events calendar — one-off events (appointments, trips, birthdays) saved to
# an `events` worksheet in the same Google Sheet.
# =============================================================================
def month_day_list(year: int, month: int):
    first = date(year, month, 1)
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return [first + timedelta(days=i) for i in range((nxt - first).days)]


def event_label(ev) -> str:
    t = str(ev.get("time", "")).strip()
    name = str(ev.get("title", "")).strip() or "(untitled)"
    return f"{t} · {name}" if t else name


def render_event_day_cell(d, day_events):
    cls = ["day", "static"]
    if day_events:
        cls.append("is-logged")
    if d == today:
        cls.append("is-today")

    top = (
        f'<div class="day-top"><span class="dow">{d.strftime("%a")}</span>'
        f'<span class="dom">{d.day}</span></div>'
    )

    if day_events:
        chips = "".join(f'<span class="chip ev">{escape(event_label(ev))}</span>' for ev in day_events[:2])
        if len(day_events) > 2:
            chips += f'<span class="chip ev">+{len(day_events) - 2} more</span>'
        tip = escape(f"{d.strftime('%a, %b %d')} — " + " · ".join(event_label(ev) for ev in day_events))
        body = f'<div class="chips">{chips}</div><span class="ev-mini">{len(day_events)}</span>'
    else:
        tip = escape(d.strftime("%a, %b %d"))
        body = '<div class="day-empty"></div>'

    return f'<div class="{" ".join(cls)}" title="{tip}">{top}{body}</div>'


def render_event_list_row(events_ws, ev, key_suffix: str):
    d = ev["date_dt"].date()
    delta_days = (d - today).days
    if delta_days == 0:
        when = "Today"
    elif delta_days == 1:
        when = "Tomorrow"
    elif d.year == today.year:
        when = d.strftime("%a, %b %d")
    else:
        when = d.strftime("%b %d, %Y")

    time_str = str(ev.get("time", "")).strip()
    title = str(ev.get("title", "")).strip() or "(untitled)"
    notes = str(ev.get("notes", "")).strip()

    c1, c2, c3 = st.columns([1.5, 4.3, 0.55])
    time_html = f'<div class="ev-time">{escape(time_str)}</div>' if time_str else ""
    c1.markdown(f'<span class="ev-when">{escape(when)}</span>{time_html}', unsafe_allow_html=True)
    notes_html = f'<div class="ev-notes">{escape(notes)}</div>' if notes else ""
    c2.markdown(f'<div class="ev-title">{escape(title)}</div>{notes_html}', unsafe_allow_html=True)

    if c3.button("×", key=f"del_ev_{key_suffix}_{ev['id']}", help="Delete this event"):
        if delete_event_by_id(events_ws, str(ev["id"])):
            st.toast("Event deleted", icon="🗑️")
        else:
            st.toast("Couldn't find that event — try Refresh", icon="⚠️")
        st.session_state.ev_cache_buster = datetime.now().isoformat()
        st.rerun()


st.divider()
with st.expander("🗓️ Events calendar"):
    events_ws = None
    try:
        events_ws = get_or_create_events_worksheet()
        events_df = load_events_cached(events_ws.title, st.session_state.ev_cache_buster)
    except Exception as e:
        st.error(f"Couldn't load events: {e}")

    if events_ws is not None:
        st.caption(f"Saved to the `{events_ws.title}` tab of the same Google Sheet.")

        # ---- add an event ----
        with st.form("add_event_form", clear_on_submit=True):
            fc = st.columns([1.35, 1.0, 2.65])
            ev_date = fc[0].date_input("Date", value=today, format="MM/DD/YYYY")
            ev_time = fc[1].text_input("Time", placeholder="7:30pm (optional)")
            ev_title = fc[2].text_input("Event", placeholder="Dentist, dinner with Sam, flight…")
            ev_notes = st.text_input("Notes", placeholder="Optional details")
            add_ev_clicked = st.form_submit_button("＋ Add event", type="primary", use_container_width=True)

        if add_ev_clicked:
            if not str(ev_title).strip():
                st.warning("Give the event a title first.")
            else:
                add_event(events_ws, ev_date, ev_title, ev_time, ev_notes)
                st.session_state.ev_cache_buster = datetime.now().isoformat()
                st.toast(f"Added: {str(ev_title).strip()}", icon="🗓️")
                st.rerun()

        # ---- prep events ----
        evs = events_df.copy()
        evs["date_dt"] = pd.to_datetime(evs["date"], errors="coerce")
        evs = evs.dropna(subset=["date_dt"])
        evs["time_key"] = evs["time"].astype(str).apply(event_time_key)
        evs = evs.sort_values(["date_dt", "time_key", "title"])

        events_by_day = {}
        for _, ev in evs.iterrows():
            events_by_day.setdefault(ev["date_dt"].date().isoformat(), []).append(ev)

        ts_today = pd.Timestamp(today)
        upcoming = evs[evs["date_dt"] >= ts_today]
        past = evs[evs["date_dt"] < ts_today].sort_values(
            ["date_dt", "time_key"], ascending=[False, False]
        )

        # ---- month grids: this month + next, plus any later month with events ----
        base_months = {(today.year, today.month), (first_of_next.year, first_of_next.month)}
        event_months = {(d.year, d.month) for d in upcoming["date_dt"].dt.date}
        months_to_render = sorted(base_months | event_months)

        ev_cal_html = ""
        for yr, mo in months_to_render:
            mdays = month_day_list(yr, mo)
            is_cur = yr == today.year and mo == today.month
            now_pill = '<span class="month-now">This month</span>' if is_cur else ""
            header = (
                '<div class="month-head">'
                f'<span class="month-name{" cur" if is_cur else ""}">{mdays[0].strftime("%B")}</span>'
                f'<span class="month-year">{yr}</span>{now_pill}</div>'
            )
            cells = ['<div class="cell-blank"></div>'] * mdays[0].weekday()
            cells.extend(render_event_day_cell(d, events_by_day.get(d.isoformat(), [])) for d in mdays)
            ev_cal_html += (
                f'<div class="month-card">{header}'
                f'<div class="cal-head">{dow_header}</div>'
                f'<div class="cal-grid">{"".join(cells)}</div></div>'
            )
        st.markdown(ev_cal_html, unsafe_allow_html=True)

        # ---- lists ----
        up_tab, past_tab = st.tabs(["Upcoming", "Past"])
        with up_tab:
            if upcoming.empty:
                st.info("No upcoming events yet — add one above.")
            for i, (_, ev) in enumerate(upcoming.iterrows()):
                render_event_list_row(events_ws, ev, f"up_{i}")
        with past_tab:
            if past.empty:
                st.caption("No past events yet.")
            elif len(past) > 30:
                st.caption(f"Showing the 30 most recent of {len(past)} past events.")
            for i, (_, ev) in enumerate(past.head(30).iterrows()):
                render_event_list_row(events_ws, ev, f"past_{i}")


st.markdown('<div class="foot">Built with Streamlit · your entries live in your Google Sheet</div>',
            unsafe_allow_html=True)
