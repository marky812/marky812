import json
from datetime import date, datetime, timedelta

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(
    page_title="Wellness Tracker",
    page_icon="◌",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# -----------------------------
# Google Sheets setup
# -----------------------------
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
]

BOOL_COLUMNS = [
    "night_ate",
    "took_medicine",
    "meditation_listen",
    "no_food_4h_before_bed",
]

HABIT_LABELS = {
    "night_ate": "Night ate?",
    "took_medicine": "Took medicine?",
    "meditation_listen": "Meditation / Listen?",
    "no_food_4h_before_bed": "No food 4 hours before bed?",
}


# -----------------------------
# Styling
# -----------------------------
st.markdown(
    """
    <style>
        .block-container {
            padding-top: 2.2rem;
            padding-bottom: 3rem;
            max-width: 1180px;
        }

        h1, h2, h3 {
            letter-spacing: -0.045em;
        }

        .hero {
            border: 1px solid rgba(128, 128, 128, 0.22);
            border-radius: 26px;
            padding: 28px 30px;
            margin-bottom: 24px;
            background: linear-gradient(135deg, rgba(128,128,128,0.10), rgba(128,128,128,0.02));
        }

        .hero-title {
            font-size: 2.35rem;
            line-height: 1.02;
            font-weight: 760;
            letter-spacing: -0.06em;
            margin-bottom: 8px;
        }

        .hero-subtitle {
            opacity: 0.68;
            font-size: 1.02rem;
            margin-bottom: 0;
        }

        .section-label {
            font-size: 0.78rem;
            font-weight: 720;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            opacity: 0.60;
            margin: 14px 0 8px 0;
        }

        .soft-card {
            border: 1px solid rgba(128, 128, 128, 0.22);
            border-radius: 22px;
            padding: 20px;
            background: rgba(128,128,128,0.04);
            margin-bottom: 14px;
        }

        .total-card {
            border: 1px solid rgba(128, 128, 128, 0.22);
            border-radius: 22px;
            padding: 20px;
            background: rgba(128,128,128,0.05);
            text-align: center;
        }

        .total-number {
            font-size: 2.15rem;
            font-weight: 760;
            letter-spacing: -0.05em;
            line-height: 1;
        }

        .total-label {
            opacity: 0.62;
            font-size: 0.84rem;
            margin-top: 6px;
        }

        div[data-testid="stMetric"] {
            border: 1px solid rgba(128,128,128,0.22);
            border-radius: 18px;
            padding: 14px 16px;
            background: rgba(128,128,128,0.04);
        }

        div[data-testid="stButton"] > button {
            width: 100%;
            border-radius: 15px;
            border: 1px solid rgba(128,128,128,0.26);
            min-height: 44px;
            transition: all 120ms ease;
        }

        div[data-testid="stButton"] > button:hover {
            border-color: rgba(128,128,128,0.58);
            transform: translateY(-1px);
        }

        .calendar-button-note {
            font-size: 0.80rem;
            opacity: 0.62;
            margin-top: -8px;
            margin-bottom: 8px;
        }

        .food-header {
            font-size: 0.78rem;
            opacity: 0.58;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            padding-bottom: 2px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------
# Helpers
# -----------------------------
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
            cleaned.append(
                {
                    "name": name,
                    "calories": calories,
                    "protein": round(protein, 1),
                }
            )

    return cleaned


def clean_items(items):
    cleaned = []
    for item in items:
        name = str(item.get("name", "")).strip()
        calories = max(0, safe_int(item.get("calories", 0)))
        protein = max(0.0, safe_float(item.get("protein", 0.0)))

        if name or calories or protein:
            cleaned.append(
                {
                    "name": name,
                    "calories": calories,
                    "protein": round(protein, 1),
                }
            )

    return cleaned


def total_calories(items) -> int:
    return int(sum(safe_int(item.get("calories", 0)) for item in items))


def total_protein(items) -> float:
    return round(sum(safe_float(item.get("protein", 0.0)) for item in items), 1)


def format_day_label(day: date) -> str:
    try:
        return day.strftime("%a\n%b %-d")
    except ValueError:
        # Windows compatibility.
        return day.strftime("%a\n%b %#d")


@st.cache_resource(show_spinner=False)
def get_gspread_client():
    if "gcp_service_account_json" not in st.secrets:
        st.error("Missing `gcp_service_account_json` in Streamlit Secrets.")
        st.stop()

    service_account_info = json.loads(st.secrets["gcp_service_account_json"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return gspread.authorize(creds)


@st.cache_resource(show_spinner=False)
def get_or_create_worksheet():
    gc = get_gspread_client()

    spreadsheet_name = st.secrets.get("spreadsheet_name", DEFAULT_SPREADSHEET_NAME)
    worksheet_name = st.secrets.get("worksheet_name", DEFAULT_WORKSHEET_NAME)

    try:
        spreadsheet = gc.open(spreadsheet_name)
    except gspread.SpreadsheetNotFound:
        spreadsheet = gc.create(spreadsheet_name)
        st.warning(
            f"Created a new spreadsheet named `{spreadsheet_name}`. "
            "It belongs to the service account."
        )

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name,
            rows=1000,
            cols=len(HEADERS),
        )
        worksheet.append_row(HEADERS)
        return worksheet, spreadsheet.url, worksheet_name

    current_headers = worksheet.row_values(1)

    # If someone points this app at the old daily_log schema, do not clear it.
    # Instead, create/use a v2 worksheet beside it.
    if current_headers and current_headers != HEADERS:
        v2_name = f"{worksheet_name}_v2" if not worksheet_name.endswith("_v2") else worksheet_name

        if v2_name != worksheet_name:
            st.warning(
                f"`{worksheet_name}` has a different schema, so this app is using `{v2_name}` instead. "
                "Your old sheet was not changed."
            )
            try:
                worksheet = spreadsheet.worksheet(v2_name)
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(
                    title=v2_name,
                    rows=1000,
                    cols=len(HEADERS),
                )
                worksheet.append_row(HEADERS)
                return worksheet, spreadsheet.url, v2_name

            current_headers = worksheet.row_values(1)

    if not current_headers:
        worksheet.append_row(HEADERS)
    elif current_headers != HEADERS:
        # Only reached for a v2 worksheet with a bad/incomplete schema.
        worksheet.clear()
        worksheet.append_row(HEADERS)

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


def upsert_entry(
    worksheet,
    selected_date,
    night_ate,
    took_medicine,
    meditation_listen,
    no_food_4h_before_bed,
    items,
    notes,
):
    selected_date_str = selected_date.isoformat()
    cleaned_items = clean_items(items)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    row = [
        selected_date_str,
        bool(night_ate),
        bool(took_medicine),
        bool(meditation_listen),
        bool(no_food_4h_before_bed),
        json.dumps(cleaned_items),
        total_calories(cleaned_items),
        total_protein(cleaned_items),
        notes.strip(),
        now,
    ]

    row_number = find_row_number_by_date(worksheet, selected_date_str)

    if row_number:
        worksheet.update(f"A{row_number}:J{row_number}", [row])
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


def get_window_df(df, days: int):
    if df.empty:
        return df.copy()

    end = pd.Timestamp(date.today())
    start = end - pd.Timedelta(days=days - 1)

    tmp = df.copy()
    tmp["date_dt"] = pd.to_datetime(tmp["date"], errors="coerce")
    tmp = tmp[(tmp["date_dt"] >= start) & (tmp["date_dt"] <= end)]
    return tmp.sort_values("date_dt")


def habit_percent(window_df, col):
    if window_df.empty:
        return 0
    return int(round(window_df[col].mean() * 100))


def show_summary_metrics(df, days: int):
    window_df = get_window_df(df, days)
    logged_days = len(window_df)

    avg_calories = int(round(window_df["total_calories"].mean())) if logged_days else 0
    avg_protein = round(window_df["total_protein"].mean(), 1) if logged_days else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"{days}d logged", f"{logged_days}/{days}")
    c2.metric("Avg calories", f"{avg_calories:,}")
    c3.metric("Avg protein", f"{avg_protein:g}g")
    c4.metric("No food 4h", f"{habit_percent(window_df, 'no_food_4h_before_bed')}%")

    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Night ate", f"{habit_percent(window_df, 'night_ate')}%")
    h2.metric("Medicine", f"{habit_percent(window_df, 'took_medicine')}%")
    h3.metric("Meditation/listen", f"{habit_percent(window_df, 'meditation_listen')}%")
    h4.metric("Calories total", f"{int(window_df['total_calories'].sum()):,}")


# -----------------------------
# Load data
# -----------------------------
if "cache_buster" not in st.session_state:
    st.session_state.cache_buster = datetime.now().isoformat()

with st.spinner("Connecting to Google Sheets..."):
    try:
        worksheet, sheet_url, active_worksheet_name = get_or_create_worksheet()
        df = load_data_cached(active_worksheet_name, st.session_state.cache_buster)
    except Exception as e:
        st.exception(e)
        st.stop()


# -----------------------------
# Header
# -----------------------------
st.markdown(
    """
    <div class="hero">
        <div class="hero-title">Daily Wellness Tracker</div>
        <p class="hero-subtitle">Minimal habit + nutrition logging, backed by Google Sheets.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

left, right = st.columns([3, 1])
with left:
    st.caption(f"Connected worksheet: `{active_worksheet_name}`")
with right:
    st.link_button("Open Google Sheet", sheet_url, use_container_width=True)


# -----------------------------
# Date selection
# -----------------------------
today = date.today()
past_30_days = [today - timedelta(days=i) for i in range(29, -1, -1)]
past_30_day_strings = [day.isoformat() for day in past_30_days]

df_by_date = {row["date"]: row for _, row in df.iterrows()}

if "selected_date" not in st.session_state or st.session_state.selected_date not in past_30_day_strings:
    st.session_state.selected_date = today.isoformat()

st.markdown('<div class="section-label">Select day</div>', unsafe_allow_html=True)


def day_option_label(day_str):
    day = date.fromisoformat(day_str)
    label = day.strftime("%A, %B %d")
    if day == today:
        label += " · Today"
    if day_str in df_by_date:
        row = df_by_date[day_str]
        label += f" · {safe_int(row.get('total_calories', 0)):,} cal · {safe_float(row.get('total_protein', 0)):g}g protein"
    return label


selected_date_str = st.selectbox(
    "Day",
    options=list(reversed(past_30_day_strings)),
    index=list(reversed(past_30_day_strings)).index(st.session_state.selected_date),
    format_func=day_option_label,
    label_visibility="collapsed",
)

if selected_date_str != st.session_state.selected_date:
    st.session_state.selected_date = selected_date_str
    st.session_state.loaded_date = None
    st.rerun()

selected_date = date.fromisoformat(st.session_state.selected_date)
existing = df_by_date.get(st.session_state.selected_date)

if st.session_state.get("loaded_date") != st.session_state.selected_date:
    existing_items = parse_items(existing.get("items_json", "")) if existing is not None else []
    load_items_into_state(existing_items)
    st.session_state.loaded_date = st.session_state.selected_date


# -----------------------------
# Entry editor
# -----------------------------
st.markdown('<div class="section-label">Today\'s entry</div>', unsafe_allow_html=True)

entry_col, totals_col = st.columns([2.2, 1])

with entry_col:
    st.markdown('<div class="soft-card">', unsafe_allow_html=True)

    st.subheader(selected_date.strftime("%A, %B %d, %Y"))

    b1, b2 = st.columns(2)
    with b1:
        night_ate = st.checkbox(
            HABIT_LABELS["night_ate"],
            value=parse_bool(existing.get("night_ate", False)) if existing is not None else False,
            key=f"night_ate_{st.session_state.selected_date}",
        )
        took_medicine = st.checkbox(
            HABIT_LABELS["took_medicine"],
            value=parse_bool(existing.get("took_medicine", False)) if existing is not None else False,
            key=f"took_medicine_{st.session_state.selected_date}",
        )
    with b2:
        meditation_listen = st.checkbox(
            HABIT_LABELS["meditation_listen"],
            value=parse_bool(existing.get("meditation_listen", False)) if existing is not None else False,
            key=f"meditation_listen_{st.session_state.selected_date}",
        )
        no_food_4h_before_bed = st.checkbox(
            HABIT_LABELS["no_food_4h_before_bed"],
            value=parse_bool(existing.get("no_food_4h_before_bed", False)) if existing is not None else False,
            key=f"no_food_4h_{st.session_state.selected_date}",
        )

    st.markdown('<div class="section-label">Calories + protein</div>', unsafe_allow_html=True)

    header_cols = st.columns([4.8, 1.55, 1.55, 0.55])
    header_cols[0].markdown('<div class="food-header">Item</div>', unsafe_allow_html=True)
    header_cols[1].markdown('<div class="food-header">Calories</div>', unsafe_allow_html=True)
    header_cols[2].markdown('<div class="food-header">Protein</div>', unsafe_allow_html=True)
    header_cols[3].markdown('<div class="food-header">&nbsp;</div>', unsafe_allow_html=True)

    for idx, item in enumerate(st.session_state.food_items):
        cols = st.columns([4.8, 1.55, 1.55, 0.55])
        cols[0].text_input(
            "Item",
            value=item.get("name", ""),
            placeholder="Greek yogurt, Fairlife, chicken, etc.",
            label_visibility="collapsed",
            key=f"food_name_{idx}",
        )
        cols[1].number_input(
            "Calories",
            min_value=0,
            max_value=20000,
            value=safe_int(item.get("calories", 0)),
            step=25,
            label_visibility="collapsed",
            key=f"food_cal_{idx}",
        )
        cols[2].number_input(
            "Protein",
            min_value=0.0,
            max_value=1000.0,
            value=float(safe_float(item.get("protein", 0.0))),
            step=1.0,
            format="%.1f",
            label_visibility="collapsed",
            key=f"food_protein_{idx}",
        )
        if cols[3].button("×", key=f"remove_food_{idx}", help="Remove item"):
            current_items = get_items_from_widgets()
            if idx < len(current_items):
                current_items.pop(idx)
            load_items_into_state(current_items)
            st.rerun()

    add_col, spacer_col = st.columns([1, 3])
    with add_col:
        if st.button("＋ Add item", use_container_width=True):
            current_items = get_items_from_widgets()
            current_items.append({"name": "", "calories": 0, "protein": 0.0})
            load_items_into_state(current_items)
            st.rerun()

    notes = st.text_area(
        "Notes",
        value=str(existing.get("notes", "")) if existing is not None else "",
        placeholder="Optional: hunger, mood, sleep, cravings, dinner time, etc.",
        height=90,
        key=f"notes_{st.session_state.selected_date}",
    )

    save_col, delete_col, refresh_col = st.columns([1.4, 1, 1])
    with save_col:
        if st.button("Save day", type="primary", use_container_width=True):
            items = get_items_from_widgets()
            action = upsert_entry(
                worksheet=worksheet,
                selected_date=selected_date,
                night_ate=night_ate,
                took_medicine=took_medicine,
                meditation_listen=meditation_listen,
                no_food_4h_before_bed=no_food_4h_before_bed,
                items=items,
                notes=notes,
            )
            st.session_state.cache_buster = datetime.now().isoformat()
            st.success(f"Entry {action}.")
            st.rerun()

    with delete_col:
        if st.button("Delete", disabled=existing is None, use_container_width=True):
            if delete_entry(worksheet, selected_date):
                st.session_state.cache_buster = datetime.now().isoformat()
                st.success("Entry deleted.")
                st.rerun()
            else:
                st.warning("No entry found for this date.")

    with refresh_col:
        if st.button("Refresh", use_container_width=True):
            st.session_state.cache_buster = datetime.now().isoformat()
            st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)

with totals_col:
    current_items = get_items_from_widgets()
    day_calories = total_calories(current_items)
    day_protein = total_protein(current_items)
    habit_yes_count = sum(
        [night_ate, took_medicine, meditation_listen, no_food_4h_before_bed]
    )

    st.markdown(
        f"""
        <div class="total-card">
            <div class="total-number">{day_calories:,}</div>
            <div class="total-label">calories today</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="total-card">
            <div class="total-number">{day_protein:g}g</div>
            <div class="total-label">protein today</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="total-card">
            <div class="total-number">{habit_yes_count}/4</div>
            <div class="total-label">boxes checked</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# -----------------------------
# Calendar
# -----------------------------
st.divider()
st.markdown('<div class="section-label">Past 30 days</div>', unsafe_allow_html=True)
st.markdown('<div class="calendar-button-note">Click any day here or use the dropdown above.</div>', unsafe_allow_html=True)

for week_start in range(0, 30, 7):
    cols = st.columns(7)
    week_days = past_30_days[week_start:week_start + 7]

    for i, day in enumerate(week_days):
        day_str = day.isoformat()
        row = df_by_date.get(day_str)
        selected = st.session_state.selected_date == day_str

        label = format_day_label(day)
        if row is not None:
            habit_count = sum(parse_bool(row.get(col, False)) for col in BOOL_COLUMNS)
            label += (
                f"\n{safe_int(row.get('total_calories', 0)):,} cal"
                f"\n{safe_float(row.get('total_protein', 0)):g}g P"
                f"\n{habit_count}/4"
            )
        else:
            label += "\n—\n\n"

        if selected:
            label = "● " + label

        if cols[i].button(label, key=f"calendar_day_{day_str}"):
            st.session_state.selected_date = day_str
            st.session_state.loaded_date = None
            st.rerun()


# -----------------------------
# Summaries
# -----------------------------
st.divider()
st.markdown('<div class="section-label">Averages</div>', unsafe_allow_html=True)

tab7, tab30 = st.tabs(["Last 7 days", "Last 30 days"])

with tab7:
    show_summary_metrics(df, 7)

    chart_df = get_window_df(df, 7)
    if not chart_df.empty:
        st.markdown('<div class="section-label">Calories trend</div>', unsafe_allow_html=True)
        st.bar_chart(chart_df.set_index("date_dt")[["total_calories", "total_protein"]])
    else:
        st.info("No entries in the last 7 days yet.")

with tab30:
    show_summary_metrics(df, 30)

    chart_df = get_window_df(df, 30)
    if not chart_df.empty:
        st.markdown('<div class="section-label">Calories trend</div>', unsafe_allow_html=True)
        st.bar_chart(chart_df.set_index("date_dt")[["total_calories", "total_protein"]])
    else:
        st.info("No entries in the last 30 days yet.")


# -----------------------------
# Data table
# -----------------------------
st.divider()

with st.expander("All saved entries"):
    if df.empty:
        st.info("No entries yet.")
    else:
        display_df = df.copy()
        display_df["date_dt"] = pd.to_datetime(display_df["date"], errors="coerce")
        display_df = display_df.sort_values("date_dt", ascending=False).drop(columns=["date_dt"])
        display_df["items"] = display_df["items_json"].apply(
            lambda value: ", ".join(
                item["name"] for item in parse_items(value) if item.get("name")
            )
        )
        display_df = display_df[
            [
                "date",
                "total_calories",
                "total_protein",
                "night_ate",
                "took_medicine",
                "meditation_listen",
                "no_food_4h_before_bed",
                "items",
                "notes",
                "updated_at",
            ]
        ]
        st.dataframe(display_df, use_container_width=True, hide_index=True)
