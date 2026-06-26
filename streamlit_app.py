import json
from datetime import date, datetime, timedelta

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="30-Day Calories + Exercise Tracker", page_icon="🔥", layout="wide")
st.title("🔥 30-Day Calories + Exercise Tracker")
st.caption("Tracks calories and exercise using Google Sheets as the backend.")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_SPREADSHEET_NAME = "Streamlit Calories Tracker"
DEFAULT_WORKSHEET_NAME = "daily_log"
HEADERS = ["date", "calories", "exercise", "exercise_minutes", "notes", "updated_at"]


@st.cache_resource(show_spinner=False)
def get_gspread_client():
    if "gcp_service_account_json" not in st.secrets:
        st.error(
            "Missing `gcp_service_account_json` in Streamlit Secrets.\n\n"
            "Go to Streamlit Cloud → App → Settings → Secrets and paste your full JSON like this:\n\n"
            "gcp_service_account_json = '''\n{ your entire Google service account JSON here }\n'''"
        )
        st.stop()

    try:
        service_account_info = json.loads(st.secrets["gcp_service_account_json"])
    except Exception as e:
        st.error("Could not parse `gcp_service_account_json`. Make sure it is valid JSON inside triple quotes.")
        st.exception(e)
        st.stop()

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
            "It belongs to the service account. To see it in your Drive, share it with your personal Google account."
        )

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(HEADERS))
        worksheet.append_row(HEADERS)

    if worksheet.row_values(1) != HEADERS:
        worksheet.clear()
        worksheet.append_row(HEADERS)

    return worksheet, spreadsheet.url


def load_data(worksheet) -> pd.DataFrame:
    records = worksheet.get_all_records()
    if not records:
        return pd.DataFrame(columns=HEADERS)

    df = pd.DataFrame(records)
    for col in HEADERS:
        if col not in df.columns:
            df[col] = ""

    df = df[HEADERS]
    df["date"] = df["date"].astype(str)
    df["calories"] = pd.to_numeric(df["calories"], errors="coerce").fillna(0).astype(int)
    df["exercise_minutes"] = pd.to_numeric(df["exercise_minutes"], errors="coerce").fillna(0).astype(int)
    return df


def find_row_number_by_date(worksheet, selected_date: str):
    dates = worksheet.col_values(1)
    for idx, value in enumerate(dates, start=1):
        if value == selected_date:
            return idx
    return None


def upsert_entry(worksheet, selected_date, calories, exercise, exercise_minutes, notes):
    selected_date_str = selected_date.isoformat()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [selected_date_str, int(calories), exercise.strip(), int(exercise_minutes), notes.strip(), now]
    row_number = find_row_number_by_date(worksheet, selected_date_str)

    if row_number:
        worksheet.update(f"A{row_number}:F{row_number}", [row])
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


with st.spinner("Connecting to Google Sheets..."):
    worksheet, sheet_url = get_or_create_worksheet()
    df = load_data(worksheet)

st.success("Connected to Google Sheets.")
st.link_button("Open Google Sheet", sheet_url)

today = date.today()
calendar_days = [today + timedelta(days=i) for i in range(30)]
df_by_date = {row["date"]: row for _, row in df.iterrows()}

st.subheader("📅 Next 30 Days")

if "selected_date" not in st.session_state:
    st.session_state.selected_date = today.isoformat()

st.markdown(
    """
    <style>
    div[data-testid="stButton"] > button {
        width: 100%;
        min-height: 78px;
        white-space: pre-wrap;
        border-radius: 14px;
        font-size: 0.9rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

for week_start in range(0, 30, 7):
    cols = st.columns(7)
    for i, day in enumerate(calendar_days[week_start:week_start + 7]):
        day_str = day.isoformat()
        has_entry = day_str in df_by_date
        selected = st.session_state.selected_date == day_str

        try:
            label = day.strftime("%a\n%b %-d")
        except ValueError:
            label = day.strftime("%a\n%b %#d")

        if has_entry:
            calories = int(df_by_date[day_str].get("calories", 0))
            minutes = int(df_by_date[day_str].get("exercise_minutes", 0))
            label += f"\n🔥 {calories} cal\n🏃 {minutes} min"
        else:
            label += "\n—"

        if selected:
            label = "✅ " + label

        if cols[i].button(label, key=f"day_{day_str}"):
            st.session_state.selected_date = day_str
            st.rerun()

selected_date = date.fromisoformat(st.session_state.selected_date)
selected_date_str = selected_date.isoformat()
existing = df_by_date.get(selected_date_str)

st.divider()
st.subheader(f"✍️ Entry for {selected_date.strftime('%A, %B %d, %Y')}")

existing_calories = int(existing["calories"]) if existing is not None else 0
existing_exercise = str(existing["exercise"]) if existing is not None else ""
existing_minutes = int(existing["exercise_minutes"]) if existing is not None else 0
existing_notes = str(existing["notes"]) if existing is not None else ""

with st.form("entry_form"):
    calories = st.number_input("Calories", min_value=0, max_value=20000, value=existing_calories, step=50)
    exercise = st.text_input("Exercise", value=existing_exercise, placeholder="walking, lifting, tennis, yoga")
    exercise_minutes = st.number_input("Exercise minutes", min_value=0, max_value=1000, value=existing_minutes, step=5)
    notes = st.text_area("Notes", value=existing_notes, placeholder="Meals, mood, soreness, hunger, etc.")
    submitted = st.form_submit_button("Save / Update Entry")

if submitted:
    action = upsert_entry(worksheet, selected_date, calories, exercise, exercise_minutes, notes)
    st.success(f"Entry {action}.")
    st.rerun()

c1, c2 = st.columns([1, 4])
with c1:
    if st.button("🗑️ Delete Entry", disabled=existing is None):
        if delete_entry(worksheet, selected_date):
            st.success("Entry deleted.")
            st.rerun()
        else:
            st.warning("No entry found for this date.")
with c2:
    if st.button("🔄 Refresh from Google Sheets"):
        st.rerun()

st.divider()
st.subheader("📊 Summary")

if df.empty:
    st.info("No entries yet.")
else:
    visible_df = df[df["date"].isin([d.isoformat() for d in calendar_days])].copy()

    total_calories = int(visible_df["calories"].sum()) if not visible_df.empty else 0
    avg_calories = int(visible_df["calories"].mean()) if not visible_df.empty else 0
    total_minutes = int(visible_df["exercise_minutes"].sum()) if not visible_df.empty else 0
    logged_days = len(visible_df)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Logged days", logged_days)
    m2.metric("Total calories", f"{total_calories:,}")
    m3.metric("Avg calories/day", f"{avg_calories:,}")
    m4.metric("Exercise minutes", f"{total_minutes:,}")

    chart_df = visible_df.copy()
    chart_df["date"] = pd.to_datetime(chart_df["date"])
    chart_df = chart_df.sort_values("date")

    if not chart_df.empty:
        st.write("Calories by day")
        st.bar_chart(chart_df.set_index("date")["calories"])
        st.write("Exercise minutes by day")
        st.bar_chart(chart_df.set_index("date")["exercise_minutes"])

    st.write("All entries")
    st.dataframe(visible_df.sort_values("date"), use_container_width=True, hide_index=True)

with st.expander("Setup notes"):
    st.markdown(
        """
        In Streamlit Cloud → App → Settings → Secrets, paste:

        ```toml
        gcp_service_account_json = '''
        {
          "type": "service_account",
          "project_id": "...",
          "private_key_id": "...",
          "private_key": "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n",
          "client_email": "...",
          "client_id": "...",
          "auth_uri": "https://accounts.google.com/o/oauth2/auth",
          "token_uri": "https://oauth2.googleapis.com/token",
          "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
          "client_x509_cert_url": "...",
          "universe_domain": "googleapis.com"
        }
        '''

        spreadsheet_name = "Streamlit Calories Tracker"
        worksheet_name = "daily_log"
        ```

        Also share the Google Sheet with your service account email as Editor.
        If the app creates the spreadsheet, it belongs to the service account; use the Open Google Sheet button and share it with your personal Google account.
        """
    )

