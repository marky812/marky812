"""
Streamlit Google Sheets Health Calendar
--------------------------------------
A one-file Streamlit app for tracking calories and exercise over the next 30 days
with Google Sheets as the backend.

Deploy notes:
1. Create a Google Cloud service account and enable the Google Sheets API.
2. Create or choose a Google Sheet.
3. Share the Google Sheet with the service account email as Editor.
4. In Streamlit Cloud, add secrets in this shape:

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "your-service-account@your-project.iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
universe_domain = "googleapis.com"

[google_sheet]
spreadsheet_id = "YOUR_SPREADSHEET_ID"
worksheet_name = "health_log"

5. requirements.txt should include:
streamlit
gspread
google-auth
pandas
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import streamlit as st

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    st.error(
        "Missing dependencies. Add `gspread`, `google-auth`, and `pandas` to requirements.txt."
    )
    st.stop()


# -----------------------------
# App configuration
# -----------------------------
st.set_page_config(
    page_title="30-Day Health Calendar",
    page_icon="📅",
    layout="wide",
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

REQUIRED_HEADERS = [
    "date",
    "calories",
    "exercise",
    "notes",
    "updated_at",
]


# -----------------------------
# Google Sheets helpers
# -----------------------------
@st.cache_resource(show_spinner=False)
def get_worksheet() -> Any:
    """Connect to Google Sheets and return the configured worksheet.

    The sheet itself must already exist and be shared with the service account.
    This app will create the worksheet/tab if it does not exist.
    """
    if "gcp_service_account" not in st.secrets:
        st.error("Missing `[gcp_service_account]` in Streamlit secrets.")
        st.stop()

    if "google_sheet" not in st.secrets or "spreadsheet_id" not in st.secrets["google_sheet"]:
        st.error("Missing `[google_sheet] spreadsheet_id` in Streamlit secrets.")
        st.stop()

    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=SCOPES,
    )
    client = gspread.authorize(creds)

    spreadsheet_id = st.secrets["google_sheet"]["spreadsheet_id"]
    worksheet_name = st.secrets["google_sheet"].get("worksheet_name", "health_log")

    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name,
            rows=1000,
            cols=len(REQUIRED_HEADERS),
        )
        worksheet.append_row(REQUIRED_HEADERS)

    ensure_headers(worksheet)
    return worksheet


def ensure_headers(worksheet: Any) -> None:
    """Make sure the worksheet has the required header row."""
    existing_headers = worksheet.row_values(1)
    if existing_headers != REQUIRED_HEADERS:
        worksheet.clear()
        worksheet.append_row(REQUIRED_HEADERS)


def load_records() -> pd.DataFrame:
    """Load all records from Google Sheets into a normalized DataFrame."""
    worksheet = get_worksheet()
    records = worksheet.get_all_records()

    if not records:
        return pd.DataFrame(columns=REQUIRED_HEADERS)

    df = pd.DataFrame(records)
    for col in REQUIRED_HEADERS:
        if col not in df.columns:
            df[col] = ""

    df = df[REQUIRED_HEADERS]
    df["date"] = df["date"].astype(str)
    df["calories"] = pd.to_numeric(df["calories"], errors="coerce").fillna(0).astype(int)
    df["exercise"] = df["exercise"].astype(str)
    df["notes"] = df["notes"].astype(str)
    df["updated_at"] = df["updated_at"].astype(str)
    return df


def find_row_by_date(selected_date: date) -> int | None:
    """Return the 1-based worksheet row number for a date, or None if missing."""
    worksheet = get_worksheet()
    all_dates = worksheet.col_values(1)
    target = selected_date.isoformat()

    # Skip header row at index 0.
    for index, value in enumerate(all_dates[1:], start=2):
        if value == target:
            return index
    return None


def upsert_record(selected_date: date, calories: int, exercise: str, notes: str) -> None:
    """Insert or update a record for one date."""
    worksheet = get_worksheet()
    row_number = find_row_by_date(selected_date)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    values = [
        selected_date.isoformat(),
        int(calories),
        exercise.strip(),
        notes.strip(),
        now,
    ]

    if row_number is None:
        worksheet.append_row(values)
    else:
        worksheet.update(f"A{row_number}:E{row_number}", [values])

    st.cache_data.clear()


def delete_record(selected_date: date) -> bool:
    """Delete the record for one date. Returns True if deleted."""
    worksheet = get_worksheet()
    row_number = find_row_by_date(selected_date)

    if row_number is None:
        return False

    worksheet.delete_rows(row_number)
    st.cache_data.clear()
    return True


def get_record_for_date(df: pd.DataFrame, selected_date: date) -> dict[str, Any]:
    """Return record fields for selected date, or blank defaults."""
    target = selected_date.isoformat()
    matches = df[df["date"] == target]

    if matches.empty:
        return {
            "date": target,
            "calories": 0,
            "exercise": "",
            "notes": "",
            "updated_at": "",
        }

    return matches.iloc[0].to_dict()


# -----------------------------
# UI helpers
# -----------------------------
def next_30_days() -> list[date]:
    today = date.today()
    return [today + timedelta(days=i) for i in range(30)]


def render_calendar(days: list[date], df: pd.DataFrame) -> date:
    """Render a simple 30-day calendar grid and return selected date."""
    st.subheader("Next 30 Days")

    date_options = {day.strftime("%a %b %-d") if hasattr(day, "strftime") else str(day): day for day in days}

    # Windows compatibility for day without leading zero if needed.
    date_options = {}
    for day in days:
        label = f"{day.strftime('%a')} {day.strftime('%b')} {day.day}"
        has_entry = not df[df["date"] == day.isoformat()].empty
        if has_entry:
            label += " ✅"
        date_options[label] = day

    labels = list(date_options.keys())

    if "selected_date" not in st.session_state:
        st.session_state.selected_date = days[0].isoformat()

    selected_label = None
    current_selected = date.fromisoformat(st.session_state.selected_date)

    cols_per_row = 7
    for row_start in range(0, len(labels), cols_per_row):
        cols = st.columns(cols_per_row)
        for col, label in zip(cols, labels[row_start : row_start + cols_per_row]):
            day = date_options[label]
            is_selected = day == current_selected
            button_label = f"🔵 {label}" if is_selected else label
            if col.button(button_label, key=f"day_{day.isoformat()}", use_container_width=True):
                selected_label = label
                st.session_state.selected_date = day.isoformat()
                st.rerun()

    if selected_label is not None:
        return date_options[selected_label]

    return date.fromisoformat(st.session_state.selected_date)


def render_summary(df: pd.DataFrame, days: list[date]) -> None:
    """Render top-level summary metrics."""
    start = days[0].isoformat()
    end = days[-1].isoformat()
    period_df = df[(df["date"] >= start) & (df["date"] <= end)]

    total_days_logged = len(period_df)
    total_calories = int(period_df["calories"].sum()) if not period_df.empty else 0
    avg_calories = int(period_df["calories"].mean()) if not period_df.empty else 0

    metric_cols = st.columns(3)
    metric_cols[0].metric("Days logged", total_days_logged)
    metric_cols[1].metric("Total calories", f"{total_calories:,}")
    metric_cols[2].metric("Average calories", f"{avg_calories:,}")


# -----------------------------
# Main app
# -----------------------------
st.title("📅 30-Day Calories + Exercise Tracker")
st.caption("Google Sheets powered backend. Select a day, save your entry, update it, or delete it.")

try:
    df = load_records()
except Exception as exc:
    st.error("Could not connect to Google Sheets. Check your Streamlit secrets and sheet sharing permissions.")
    st.exception(exc)
    st.stop()

all_days = next_30_days()
render_summary(df, all_days)

st.divider()

left, right = st.columns([1.15, 1])

with left:
    selected_date = render_calendar(all_days, df)

with right:
    st.subheader("Daily Entry")
    record = get_record_for_date(df, selected_date)

    with st.form("daily_entry_form", clear_on_submit=False):
        st.write(f"**Selected date:** {selected_date.strftime('%A, %B %d, %Y')}")

        calories = st.number_input(
            "Calories",
            min_value=0,
            max_value=20000,
            value=int(record.get("calories", 0) or 0),
            step=50,
        )

        exercise = st.text_area(
            "Exercise",
            value=str(record.get("exercise", "") or ""),
            placeholder="Example: 30 min walk, push day, tennis, rest day...",
        )

        notes = st.text_area(
            "Notes",
            value=str(record.get("notes", "") or ""),
            placeholder="Optional notes about hunger, sleep, mood, etc.",
        )

        submitted = st.form_submit_button("Save / Update", use_container_width=True)

    delete_clicked = st.button("Delete this day's entry", type="secondary", use_container_width=True)

    if submitted:
        upsert_record(selected_date, calories, exercise, notes)
        st.success("Entry saved.")
        st.rerun()

    if delete_clicked:
        was_deleted = delete_record(selected_date)
        if was_deleted:
            st.success("Entry deleted.")
            st.rerun()
        else:
            st.info("No entry existed for this date.")

    if record.get("updated_at"):
        st.caption(f"Last updated: {record['updated_at']}")

st.divider()

st.subheader("Logged Entries")
visible_start = all_days[0].isoformat()
visible_end = all_days[-1].isoformat()
visible_df = df[(df["date"] >= visible_start) & (df["date"] <= visible_end)].copy()

if visible_df.empty:
    st.info("No entries yet for the next 30 days.")
else:
    visible_df = visible_df.sort_values("date")
    st.dataframe(visible_df, use_container_width=True, hide_index=True)

    csv = visible_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download visible entries as CSV",
        data=csv,
        file_name="health_calendar_entries.csv",
        mime="text/csv",
        use_container_width=True,
    )

