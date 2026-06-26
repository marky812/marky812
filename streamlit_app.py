import html
import json
from datetime import date, datetime, timedelta

import altair as alt
import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

# ----------------------------------------------------------------------------
# Page config
# ----------------------------------------------------------------------------
st.set_page_config(
    page_title="30-Day Calories + Exercise Tracker",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ----------------------------------------------------------------------------
# Google Sheets backend  (unchanged logic — only the UI around it is new)
# ----------------------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DEFAULT_SPREADSHEET_NAME = "Streamlit Calories Tracker"
DEFAULT_WORKSHEET_NAME = "daily_log"

HEADERS = ["date", "calories", "exercise", "exercise_minutes", "notes", "updated_at"]


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
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(HEADERS))
        worksheet.append_row(HEADERS)

    if worksheet.row_values(1) != HEADERS:
        worksheet.clear()
        worksheet.append_row(HEADERS)

    return worksheet, spreadsheet.url


def load_data(worksheet):
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


@st.cache_data(ttl=120, show_spinner=False)
def load_data_cached(_worksheet, version):
    # `version` busts the cache after a write; selecting a day re-uses the cache (fast).
    return load_data(_worksheet)


def find_row_number_by_date(worksheet, selected_date):
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


# ----------------------------------------------------------------------------
# Styling
# ----------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    :root{
      --cal-1:#FF8E53; --cal-2:#FF6B6B; --cal-soft:#fff1ec; --cal-ink:#e2562f;
      --ex-soft:#e9faf2; --ex-ink:#0a8f5b;
      --ink:#1f2937; --muted:#6b7280; --line:#e8eaed; --card:#ffffff;
    }

    html, body, [class*="css"], .stMarkdown, button, input, textarea, select{
      font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    }

    .block-container{ padding-top:1.4rem; padding-bottom:3rem; max-width:1180px; }
    #MainMenu, footer{ visibility:hidden; }

    /* primary buttons -> warm gradient */
    button[kind="primary"], button[kind="primaryFormSubmit"]{
      background-image:linear-gradient(135deg,var(--cal-1),var(--cal-2)) !important;
      border:0 !important; color:#fff !important; font-weight:700 !important;
      box-shadow:0 8px 18px -10px rgba(255,107,107,.7) !important;
    }
    button[kind="primary"]:hover, button[kind="primaryFormSubmit"]:hover{ filter:brightness(1.04); }

    .stTextInput input, .stNumberInput input, .stTextArea textarea{ border-radius:10px !important; }

    /* hero */
    .hero{
      display:flex; align-items:center; gap:18px;
      background:linear-gradient(120deg,#FF8E53 0%,#FF6B6B 58%,#ff5e7e 100%);
      color:#fff; padding:22px 26px; border-radius:20px;
      box-shadow:0 14px 34px -14px rgba(255,107,107,.6); margin-bottom:20px;
    }
    .hero-emoji{ font-size:42px; line-height:1; filter:drop-shadow(0 2px 6px rgba(0,0,0,.18)); }
    .hero-title{ font-size:1.75rem; font-weight:800; letter-spacing:-.02em; }
    .hero-sub{ font-size:.95rem; opacity:.93; font-weight:500; margin-top:2px; }

    /* KPI tiles */
    .kpi-grid{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:24px; }
    .kpi{
      background:var(--card); border:1px solid var(--line); border-radius:16px;
      padding:15px 18px; box-shadow:0 1px 2px rgba(16,24,40,.04);
    }
    .kpi-icon{ font-size:18px; }
    .kpi-val{ font-size:1.6rem; font-weight:800; color:var(--ink); letter-spacing:-.02em; margin-top:6px; line-height:1.1; }
    .kpi-label{ font-size:.74rem; color:var(--muted); font-weight:700; margin-top:2px; text-transform:uppercase; letter-spacing:.05em; }

    /* section title row */
    .sec-row{ display:flex; align-items:center; justify-content:space-between; margin:8px 0 14px; }
    .sec-title{ font-size:1.18rem; font-weight:800; color:var(--ink); letter-spacing:-.01em; margin:0; }
    .today-pill{
      font-size:.8rem; font-weight:700; color:var(--cal-ink); text-decoration:none;
      background:var(--cal-soft); padding:6px 13px; border-radius:999px; transition:filter .12s ease;
    }
    .today-pill:hover{ filter:brightness(.97); }

    /* calendar */
    .cal-head{ display:grid; grid-template-columns:repeat(7,1fr); gap:8px; margin-bottom:8px; }
    .cal-head span{ text-align:center; font-size:.7rem; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; }
    .cal-grid{ display:grid; grid-template-columns:repeat(7,1fr); gap:8px; }

    .day{
      display:flex; flex-direction:column; justify-content:space-between;
      min-height:88px; padding:9px 10px; border-radius:14px;
      background:var(--card); border:1px solid var(--line); color:var(--ink);
      text-decoration:none; transition:transform .12s ease, box-shadow .12s ease, border-color .12s ease;
    }
    .day:hover{ transform:translateY(-2px); box-shadow:0 10px 22px -12px rgba(16,24,40,.28); border-color:#d8dade; }
    .day-top{ display:flex; align-items:baseline; justify-content:space-between; }
    .day .dow{ font-size:.7rem; font-weight:700; color:var(--muted); }
    .day .dom{ font-size:1.05rem; font-weight:800; }
    .day-empty{ font-size:1.15rem; color:#cdd1d6; font-weight:700; align-self:center; margin-top:8px; }
    .day-stats{ display:flex; flex-wrap:wrap; gap:4px; }
    .chip{ font-size:.66rem; font-weight:700; padding:2px 6px; border-radius:999px; white-space:nowrap; }
    .chip.cal{ background:var(--cal-soft); color:var(--cal-ink); }
    .chip.ex{ background:var(--ex-soft); color:var(--ex-ink); }
    .mon-tag{ font-size:.58rem; font-weight:800; color:#fff; background:#9aa1ab; padding:1px 5px; border-radius:6px; margin-left:5px; vertical-align:middle; }

    .day.is-logged{ border-color:#ffd9cc; background:linear-gradient(180deg,#fff,#fff7f4); }
    .day.is-today{ border-color:var(--cal-2); box-shadow:0 0 0 2px rgba(255,107,107,.18); }
    .day.is-selected{
      background:linear-gradient(135deg,var(--cal-1),var(--cal-2)); border-color:transparent; color:#fff;
      box-shadow:0 12px 24px -12px rgba(255,107,107,.75);
    }
    .day.is-selected .dow, .day.is-selected .dom, .day.is-selected .day-empty{ color:#fff; }
    .day.is-selected .chip.cal, .day.is-selected .chip.ex{ background:rgba(255,255,255,.22); color:#fff; }

    /* snapshot */
    .snap-row{ display:flex; align-items:center; justify-content:space-between; padding:9px 0; border-bottom:1px dashed var(--line); }
    .snap-row:last-of-type{ border-bottom:none; }
    .snap-k{ color:var(--muted); font-weight:600; font-size:.85rem; }
    .snap-v{ color:var(--ink); font-weight:800; font-size:.95rem; }
    .snap-empty{ color:var(--muted); font-size:.9rem; text-align:center; padding:16px 6px; line-height:1.5; }

    .foot{ color:var(--muted); font-size:.78rem; text-align:center; margin-top:30px; }

    @media (max-width:760px){
      .kpi-grid{ grid-template-columns:repeat(2,1fr); }
      .day{ min-height:64px; padding:6px; }
      .day .dom{ font-size:.92rem; }
      .chip{ display:none; }
      .day-empty{ margin-top:2px; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------
# Connect + load
# ----------------------------------------------------------------------------
if "data_version" not in st.session_state:
    st.session_state.data_version = 0

with st.spinner("Connecting to Google Sheets..."):
    try:
        worksheet, sheet_url = get_or_create_worksheet()
        df = load_data_cached(worksheet, st.session_state.data_version)
    except Exception as e:
        st.exception(e)
        st.stop()

# Sidebar: status, link, optional reference line, refresh
with st.sidebar:
    st.markdown("### 🔥 Tracker")
    st.success("Connected to Google Sheets")
    st.link_button("Open Google Sheet", sheet_url, use_container_width=True)
    st.divider()
    target = st.number_input(
        "Daily calorie reference (optional)",
        min_value=0, max_value=20000, value=0, step=50,
        help="Draws a dashed line on the calories chart. Set to 0 to hide it.",
    )
    if st.button("Refresh data", use_container_width=True):
        st.session_state.data_version += 1
        st.rerun()
    st.caption("Entries are stored in your Google Sheet.")

# ----------------------------------------------------------------------------
# Window + selected day
# ----------------------------------------------------------------------------
today = date.today()
today_iso = today.isoformat()
window = [today + timedelta(days=i) for i in range(30)]
window_iso = {d.isoformat() for d in window}

df_by_date = {row["date"]: row for _, row in df.iterrows()}

# selection: URL query param -> session -> today
qp_day = st.query_params.get("day")
sel_iso = qp_day or st.session_state.get("selected_date", today_iso)
try:
    sel_date = date.fromisoformat(sel_iso)
except ValueError:
    sel_date, sel_iso = today, today_iso
st.session_state.selected_date = sel_iso
existing = df_by_date.get(sel_iso)

# ----------------------------------------------------------------------------
# Hero
# ----------------------------------------------------------------------------
st.markdown(
    f"""
    <div class="hero">
      <div class="hero-emoji">🔥</div>
      <div>
        <div class="hero-title">30-Day Tracker</div>
        <div class="hero-sub">Calories &amp; exercise · {today.strftime('%A, %B %d, %Y')}</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------
# KPI tiles (this 30-day window)
# ----------------------------------------------------------------------------
visible = df[df["date"].isin(window_iso)].copy()
logged_days = len(visible)
total_cal = int(visible["calories"].sum()) if not visible.empty else 0
avg_cal = int(round(visible["calories"].mean())) if not visible.empty else 0
total_min = int(visible["exercise_minutes"].sum()) if not visible.empty else 0

st.markdown(
    f"""
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-icon">📆</div>
        <div class="kpi-val">{logged_days}<span style="font-size:1rem;color:var(--muted);font-weight:700">/30</span></div>
        <div class="kpi-label">Days logged</div></div>
      <div class="kpi"><div class="kpi-icon">🔥</div>
        <div class="kpi-val">{avg_cal:,}</div><div class="kpi-label">Avg calories</div></div>
      <div class="kpi"><div class="kpi-icon">📊</div>
        <div class="kpi-val">{total_cal:,}</div><div class="kpi-label">Total calories</div></div>
      <div class="kpi"><div class="kpi-icon">🏃</div>
        <div class="kpi-val">{total_min:,}</div><div class="kpi-label">Exercise min</div></div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------
# Calendar (the signature element) — click a day to select it
# ----------------------------------------------------------------------------
st.markdown(
    f"""
    <div class="sec-row">
      <p class="sec-title">📅 Next 30 days</p>
      <a class="today-pill" href="?day={today_iso}" target="_self">↩︎ Jump to today</a>
    </div>
    """,
    unsafe_allow_html=True,
)

dow_header = "".join(f"<span>{d}</span>" for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])

cells = ["<div></div>"] * window[0].weekday()  # align first day to its weekday column
for d in window:
    iso = d.isoformat()
    rec = df_by_date.get(iso)
    cls = ["day"]
    if rec is not None:
        cls.append("is-logged")
    if iso == today_iso:
        cls.append("is-today")
    if iso == sel_iso:
        cls.append("is-selected")

    mon_tag = f'<span class="mon-tag">{d.strftime("%b")}</span>' if d.day == 1 else ""
    top = (
        f'<div class="day-top"><span class="dow">{d.strftime("%a")}</span>'
        f'<span class="dom">{d.day}{mon_tag}</span></div>'
    )
    if rec is not None:
        body = (
            f'<div class="day-stats">'
            f'<span class="chip cal">🔥 {int(rec.get("calories", 0))}</span>'
            f'<span class="chip ex">🏃 {int(rec.get("exercise_minutes", 0))}</span></div>'
        )
    else:
        body = '<div class="day-empty">+</div>'

    cells.append(f'<a class="{" ".join(cls)}" href="?day={iso}" target="_self">{top}{body}</a>')

st.markdown(
    f'<div class="cal-head">{dow_header}</div><div class="cal-grid">{"".join(cells)}</div>',
    unsafe_allow_html=True,
)

st.write("")

# ----------------------------------------------------------------------------
# Entry + day snapshot
# ----------------------------------------------------------------------------
left, right = st.columns([1.6, 1], gap="large")

with left:
    st.markdown(
        f'<p class="sec-title">✍️ {sel_date.strftime("%A, %b %d")}</p>',
        unsafe_allow_html=True,
    )
    with st.container(border=True):
        with st.form("entry_form", border=False):
            calories = st.number_input(
                "Calories", min_value=0, max_value=20000,
                value=int(existing["calories"]) if existing is not None else 0, step=50,
            )
            exercise = st.text_input(
                "Activity",
                value=str(existing["exercise"]) if existing is not None else "",
                placeholder="walking, lifting, tennis, yoga…",
            )
            exercise_minutes = st.number_input(
                "Exercise minutes", min_value=0, max_value=1000,
                value=int(existing["exercise_minutes"]) if existing is not None else 0, step=5,
            )
            notes = st.text_area(
                "Notes",
                value=str(existing["notes"]) if existing is not None else "",
                placeholder="Meals, mood, energy, how the workout felt…",
            )
            submitted = st.form_submit_button("Save entry", use_container_width=True, type="primary")

        a1, a2 = st.columns(2)
        delete_clicked = a1.button("Delete", use_container_width=True, disabled=existing is None)
        refresh_clicked = a2.button("Refresh data", use_container_width=True)

with right:
    st.markdown('<p class="sec-title">🧾 Day snapshot</p>', unsafe_allow_html=True)
    with st.container(border=True):
        if existing is not None:
            activity = html.escape(str(existing["exercise"]).strip()) or "—"
            rows = [
                ("🔥 Calories", f'{int(existing["calories"]):,}'),
                ("🏃 Exercise", f'{int(existing["exercise_minutes"]):,} min'),
                ("🏷️ Activity", activity),
            ]
            if target > 0:
                rows.append(("🎯 Reference", f"{int(target):,}"))
            snap = "".join(
                f'<div class="snap-row"><span class="snap-k">{k}</span>'
                f'<span class="snap-v">{v}</span></div>'
                for k, v in rows
            )
            note_val = html.escape(str(existing["notes"]).strip())
            if note_val:
                snap += (
                    '<div style="margin-top:12px;font-size:.83rem;color:var(--muted);line-height:1.5">'
                    '<span style="font-weight:700;color:var(--ink)">Notes</span><br>'
                    f"{note_val}</div>"
                )
            updated = html.escape(str(existing.get("updated_at", "")).strip())
            if updated:
                snap += (
                    '<div style="font-size:.72rem;color:var(--muted);'
                    f'margin-top:12px;text-align:right">Updated {updated}</div>'
                )
            st.markdown(snap, unsafe_allow_html=True)
        else:
            st.markdown(
                '<div class="snap-empty">Nothing logged for this day yet.<br>'
                "Fill in the form to add it. ✍️</div>",
                unsafe_allow_html=True,
            )

# Handle actions
if submitted:
    action = upsert_entry(worksheet, sel_date, calories, exercise, exercise_minutes, notes)
    st.session_state.data_version += 1
    st.toast("Saved" if action == "added" else "Updated", icon="✅")
    st.rerun()

if delete_clicked:
    if delete_entry(worksheet, sel_date):
        st.session_state.data_version += 1
        st.toast("Deleted", icon="🗑️")
        st.rerun()
    else:
        st.warning("No entry found for this date.")

if refresh_clicked:
    st.session_state.data_version += 1
    st.rerun()

# ----------------------------------------------------------------------------
# Insights
# ----------------------------------------------------------------------------
st.write("")
st.markdown('<p class="sec-title">📈 Insights</p>', unsafe_allow_html=True)


def bar_chart(data, ycol, ylabel, colors, reference=0):
    grad = alt.Gradient(
        gradient="linear",
        stops=[alt.GradientStop(color=colors[0], offset=0), alt.GradientStop(color=colors[1], offset=1)],
        x1=1, x2=1, y1=0, y2=1,
    )
    bars = (
        alt.Chart(data)
        .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6, color=grad, size=22)
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=-40)),
            y=alt.Y(f"{ycol}:Q", title=ylabel),
            tooltip=[
                alt.Tooltip("date:T", title="Date", format="%b %d"),
                alt.Tooltip(f"{ycol}:Q", title=ylabel),
            ],
        )
        .properties(height=280)
    )
    chart = bars
    if reference and reference > 0:
        rule = (
            alt.Chart(pd.DataFrame({"y": [reference]}))
            .mark_rule(color="#9aa1ab", strokeDash=[6, 4], size=1.5)
            .encode(y="y:Q")
        )
        chart = bars + rule
    return chart.configure_view(strokeWidth=0).configure_axis(
        labelColor="#6b7280", titleColor="#6b7280",
        domainColor="#e8eaed", tickColor="#e8eaed", gridColor="#eef1f4",
    )


if visible.empty:
    st.info("No entries logged in this 30-day window yet. Pick a day above and add your first one. 🙌")
else:
    chart_df = visible.copy()
    chart_df["date"] = pd.to_datetime(chart_df["date"])
    chart_df = chart_df.sort_values("date")

    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown("**Calories by day**")
        st.altair_chart(bar_chart(chart_df, "calories", "Calories", ("#FF8E53", "#FF6B6B"), target),
                        use_container_width=True)
    with c2:
        st.markdown("**Exercise minutes by day**")
        st.altair_chart(bar_chart(chart_df, "exercise_minutes", "Minutes", ("#34d399", "#10b981")),
                        use_container_width=True)

    with st.expander("📋 View all entries"):
        table = visible.sort_values("date").copy()
        table["date"] = pd.to_datetime(table["date"])
        st.dataframe(
            table,
            use_container_width=True,
            hide_index=True,
            column_config={
                "date": st.column_config.DateColumn("Date", format="MMM D, YYYY"),
                "calories": st.column_config.NumberColumn("Calories", format="%d"),
                "exercise": st.column_config.TextColumn("Activity"),
                "exercise_minutes": st.column_config.NumberColumn("Minutes", format="%d"),
                "notes": st.column_config.TextColumn("Notes", width="large"),
                "updated_at": st.column_config.TextColumn("Updated"),
            },
        )

st.markdown('<div class="foot">Built with Streamlit · data lives in your Google Sheet</div>',
            unsafe_allow_html=True)
