from __future__ import annotations

from datetime import datetime, time
from html import escape
import json
import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
import streamlit as st
from dotenv import load_dotenv
from psycopg2 import sql

st.set_page_config(page_title="Production Process Monitoring", page_icon="◈", layout="wide", initial_sidebar_state="collapsed")

PROCESS_ORDER = ["CUT", "SM2", "SM3", "SEW", "WAIT_FN", "PACK"]
PROCESS_LABELS = {"CUT": "CUT", "SM2": "SM2", "SM3": "SM3", "SEW": "SEW", "WAIT_FN": "WAIT FN", "PACK": "PACK"}
FACTORIES = ["ALL", "G1", "G2", "G3", "G4", "TRM", "EA"]
TRANSITIONS = [("CUT", "SM2"), ("SM2", "SM3"), ("SM3", "SEW"), ("SEW", "WAIT_FN"), ("WAIT_FN", "PACK")]
BLUE = "#1684e8"
INK = "#112642"
MUTED = "#71839a"
RED = "#eb5c6d"


load_dotenv(Path(__file__).with_name(".env"), override=True)
SOURCE_SCHEMA = os.environ.get("VCM_SOURCE_SCHEMA", "public")

GROUP_KEYS = ["plant", "so_year", "so_no", "barcode"]
PROCESS_COLUMNS = [*GROUP_KEYS, "process", "process_order", "process_date", "input_qty", "output_qty", "defect_qty", "barcode_count"]
DATABASE_CHUNK_SIZE = 200_000
SOURCE_START_DATE = pd.Timestamp("2026-07-09")
DATA_MODEL_VERSION = "2026-09-26-v13"
SNAPSHOT_TTL_SECONDS = 3600
SNAPSHOT_DATA_PATH = Path(__file__).with_name("vcm_snapshot.parquet")
SNAPSHOT_SUMMARY_PATH = Path(__file__).with_name("vcm_barcode_summary.parquet")
SNAPSHOT_META_PATH = Path(__file__).with_name("vcm_snapshot_meta.json")


def make_source_query(connection: psycopg2.extensions.connection, view: str, columns: str, where: str) -> str:
    return sql.SQL("SELECT {columns} FROM {schema}.{view} WHERE {where}").format(
        columns=sql.SQL(columns),
        schema=sql.Identifier(SOURCE_SCHEMA),
        view=sql.Identifier(view),
        where=sql.SQL(where),
    ).as_string(connection)


def make_aggregated_source_query(
    connection: psycopg2.extensions.connection,
    view: str,
    barcode_column: str,
    process: str,
    process_order: int,
    process_date: str,
    input_qty: str,
    output_qty: str,
    defect_qty: str,
    where: str,
) -> str:
    return sql.SQL(
        """
        SELECT
            plant,
            so_year,
            so_no,
            {barcode} AS barcode,
            {process}::text AS process,
            {process_order}::integer AS process_order,
            MIN({process_date}) AS process_date,
            COALESCE(SUM(COALESCE({input_qty}, 0)), 0)::double precision AS input_qty,
            COALESCE(SUM(COALESCE({output_qty}, 0)), 0)::double precision AS output_qty,
            COALESCE(SUM(COALESCE({defect_qty}, 0)), 0)::double precision AS defect_qty,
            COUNT({barcode})::bigint AS barcode_count
        FROM {schema}.{view}
        WHERE {where}
        GROUP BY plant, so_year, so_no, {barcode}
        """
    ).format(
        barcode=sql.Identifier(barcode_column),
        process=sql.Literal(process),
        process_order=sql.Literal(process_order),
        process_date=sql.SQL(process_date),
        input_qty=sql.SQL(input_qty),
        output_qty=sql.SQL(output_qty),
        defect_qty=sql.SQL(defect_qty),
        schema=sql.Identifier(SOURCE_SCHEMA),
        view=sql.Identifier(view),
        where=sql.SQL(where),
    ).as_string(connection)


def fetch_database_data() -> pd.DataFrame:
    connection = psycopg2.connect(
        host=os.environ["PG_HOST"], port=os.environ.get("PG_PORT", "5432"),
        dbname=os.environ["PG_DATABASE"], user=os.environ["PG_USER"],
        password=os.environ["PG_PASSWORD"], options=f"-c search_path={os.environ.get('PG_SCHEMA', 'public')}"
    )
    try:
        query_specs = [
            ("v_cut", "barcode_no", "CUT", 1, "CASE WHEN (cut_date IS NULL OR cut_date::time = TIME '00:00:00') AND upd_date IS NOT NULL THEN upd_date ELSE cut_date END", "cut_qty", "COALESCE(cut_qty, 0) - COALESCE(defect_qty, 0)", "defect_qty", "barcode_no IS NOT NULL"),
            ("v_sm2", "barcode", "SM2", 2, "ww_in_date", "ww_in_qty", "COALESCE(ww_in_qty, 0) - COALESCE(waste_qty, 0)", "waste_qty", "barcode IS NOT NULL AND ww_in_date >= DATE '2026-07-09'"),
            ("v_sm3", "barcode", "SM3", 3, "ww_in_date", "ww_in_qty", "COALESCE(ww_in_qty, 0) - COALESCE(waste_qty, 0)", "waste_qty", "barcode IS NOT NULL"),
            ("v_sew", "barcode_no", "SEW", 4, "loading_date", "qty", "wait_fn_qty", "defect_qty", "barcode_no IS NOT NULL"),
            ("v_sew", "barcode_no", "WAIT_FN", 5, "wait_fn_date", "wait_fn_qty", "COALESCE(wait_fn_qty, 0) - COALESCE(defect_qty, 0)", "defect_qty", "barcode_no IS NOT NULL AND wait_fn_date >= DATE '2026-07-09'"),
            ("v_pack", "barcode_no", "PACK", 6, "entry_date", "qty", "qty", "0", "barcode_no IS NOT NULL"),
        ]
        parts = []
        for spec in query_specs:
            query = make_aggregated_source_query(connection, *spec)
            for chunk in pd.read_sql_query(query, connection, chunksize=DATABASE_CHUNK_SIZE):
                chunk["process_date"] = pd.to_datetime(chunk["process_date"], errors="coerce").dt.tz_localize(None)
                parts.append(chunk[PROCESS_COLUMNS])
    finally:
        connection.close()

    result = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=PROCESS_COLUMNS)
    return result.sort_values([*GROUP_KEYS, "process_order"], kind="stable").reset_index(drop=True)


def load_snapshot(data_model_version: str) -> tuple[pd.DataFrame, pd.DataFrame, datetime] | None:
    try:
        metadata = json.loads(SNAPSHOT_META_PATH.read_text(encoding="utf-8"))
        refreshed_at = datetime.fromisoformat(metadata["refreshed_at"])
        is_current = (datetime.now() - refreshed_at).total_seconds() < SNAPSHOT_TTL_SECONDS
        if metadata.get("data_model_version") != data_model_version or not is_current:
            return None
        return pd.read_parquet(SNAPSHOT_DATA_PATH), pd.read_parquet(SNAPSHOT_SUMMARY_PATH), refreshed_at
    except (FileNotFoundError, KeyError, OSError, ValueError):
        return None


def save_snapshot(data: pd.DataFrame, summary: pd.DataFrame, refreshed_at: datetime, data_model_version: str) -> None:
    data_temp = SNAPSHOT_DATA_PATH.with_suffix(".tmp.parquet")
    summary_temp = SNAPSHOT_SUMMARY_PATH.with_suffix(".tmp.parquet")
    meta_temp = SNAPSHOT_META_PATH.with_suffix(".tmp.json")
    try:
        data.to_parquet(data_temp, index=False)
        summary.to_parquet(summary_temp, index=False)
        meta_temp.write_text(
            json.dumps({"data_model_version": data_model_version, "refreshed_at": refreshed_at.isoformat()}),
            encoding="utf-8",
        )
        os.replace(data_temp, SNAPSHOT_DATA_PATH)
        os.replace(summary_temp, SNAPSHOT_SUMMARY_PATH)
        os.replace(meta_temp, SNAPSHOT_META_PATH)
    except (ImportError, OSError, ValueError):
        for temporary_path in (data_temp, summary_temp, meta_temp):
            temporary_path.unlink(missing_ok=True)


@st.cache_resource(ttl=SNAPSHOT_TTL_SECONDS, show_spinner=False)
def get_data(data_model_version: str) -> tuple[pd.DataFrame, pd.DataFrame, datetime]:
    snapshot = load_snapshot(data_model_version)
    if snapshot is not None:
        return snapshot
    result = fetch_database_data()
    barcode_summary = make_barcode_summary(result)
    refreshed_at = datetime.now()
    save_snapshot(result, barcode_summary, refreshed_at, data_model_version)
    return result, barcode_summary, refreshed_at


def make_barcode_summary(process_rows: pd.DataFrame) -> pd.DataFrame:
    if process_rows.empty:
        output_columns = [f"{process}_output" for process in PROCESS_ORDER]
        wip_basis_columns = [f"{process}_wip_basis" for process in PROCESS_ORDER]
        barcode_count_columns = [f"{process}_barcode_count" for process in PROCESS_ORDER]
        return pd.DataFrame(columns=["barcode", "plant", "so_year", "so_no", *PROCESS_ORDER, *output_columns, *wip_basis_columns, *barcode_count_columns, "current_stage", "next_stage", "current_position", "status", "waiting_minutes", "aging_minutes", "minutes_since_cut", "total_lead_time_minutes"])
    index_columns = GROUP_KEYS
    wide = process_rows.pivot_table(index=index_columns, columns="process", values="process_date", aggfunc="min").reset_index()
    flags = process_rows.assign(has_process=1).pivot_table(index=index_columns, columns="process", values="has_process", aggfunc="max", fill_value=0).reset_index()
    outputs = process_rows.pivot_table(index=index_columns, columns="process", values="output_qty", aggfunc="sum", fill_value=0).reset_index()
    wip_bases = process_rows.pivot_table(index=index_columns, columns="process", values="input_qty", aggfunc="sum", fill_value=0).reset_index()
    barcode_counts = process_rows.pivot_table(index=index_columns, columns="process", values="barcode_count", aggfunc="sum", fill_value=0).reset_index()
    for process in PROCESS_ORDER:
        if process not in wide:
            wide[process] = pd.NaT
        if process not in flags:
            flags[process] = 0
        if process not in outputs:
            outputs[process] = 0
        if process not in wip_bases:
            wip_bases[process] = 0
        if process not in barcode_counts:
            barcode_counts[process] = 0
    wide = wide.merge(flags[[*index_columns, *PROCESS_ORDER]], on=index_columns, suffixes=("", "_has"))
    outputs = outputs[[*index_columns, *PROCESS_ORDER]].rename(columns={process: f"{process}_output" for process in PROCESS_ORDER})
    wide = wide.merge(outputs, on=index_columns)
    wip_bases = wip_bases[[*index_columns, *PROCESS_ORDER]].rename(columns={process: f"{process}_wip_basis" for process in PROCESS_ORDER})
    wide = wide.merge(wip_bases, on=index_columns)
    barcode_counts = barcode_counts[[*index_columns, *PROCESS_ORDER]].rename(columns={process: f"{process}_barcode_count" for process in PROCESS_ORDER})
    wide = wide.merge(barcode_counts, on=index_columns)
    now = datetime.now()
    latest_flags = wide[[f"{process}_has" for process in reversed(PROCESS_ORDER)]].rename(columns={f"{process}_has": process for process in PROCESS_ORDER})
    wide["current_stage"] = latest_flags.idxmax(axis=1)
    wide["next_stage"] = wide["current_stage"].map({source: target for source, target in TRANSITIONS})
    wide["current_output_qty"] = 0.0
    current_dates = pd.Series(pd.NaT, index=wide.index, dtype="datetime64[ns]")
    for process in PROCESS_ORDER:
        process_mask = wide["current_stage"] == process
        wide.loc[process_mask, "current_output_qty"] = wide.loc[process_mask, f"{process}_output"]
        current_dates.loc[process_mask] = pd.to_datetime(wide.loc[process_mask, process])
    wide["status"] = "IN PROCESS"
    wide.loc[wide["current_output_qty"] > 0, "status"] = "WAITING"
    wide.loc[wide["current_stage"] == "PACK", "status"] = "COMPLETED"
    wide["current_position"] = wide["current_stage"]
    waiting_mask = (wide["status"] == "WAITING") & wide["next_stage"].notna()
    wide.loc[waiting_mask, "current_position"] = wide.loc[waiting_mask, "current_stage"] + " → " + wide.loc[waiting_mask, "next_stage"]
    valid_aging_time = ~((wide["current_stage"] == "CUT") & current_dates.dt.time.eq(time(0, 0)))
    wide["aging_minutes"] = (now - current_dates).dt.total_seconds().div(60).round(2).where((wide["status"] != "COMPLETED") & valid_aging_time)
    wide["waiting_minutes"] = wide["aging_minutes"].where(wide["status"] == "WAITING")
    wide["minutes_since_cut"] = (now - pd.to_datetime(wide["CUT"])).dt.total_seconds().div(60).round(2)
    wide["total_lead_time_minutes"] = (pd.to_datetime(wide["PACK"]) - pd.to_datetime(wide["CUT"])).dt.total_seconds().div(60).round(2)
    return wide


def refresh_summary_times(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return summary
    refreshed = summary.copy()
    current_dates = pd.Series(pd.NaT, index=refreshed.index, dtype="datetime64[ns]")
    for process in PROCESS_ORDER:
        process_mask = refreshed["current_stage"] == process
        current_dates.loc[process_mask] = pd.to_datetime(refreshed.loc[process_mask, process])
    valid_aging_time = ~((refreshed["current_stage"] == "CUT") & current_dates.dt.time.eq(time(0, 0)))
    elapsed_minutes = (datetime.now() - current_dates).dt.total_seconds().div(60).round(2)
    refreshed["aging_minutes"] = elapsed_minutes.where((refreshed["status"] != "COMPLETED") & valid_aging_time)
    refreshed["waiting_minutes"] = refreshed["aging_minutes"].where(refreshed["status"] == "WAITING")
    refreshed["minutes_since_cut"] = (datetime.now() - pd.to_datetime(refreshed["CUT"])).dt.total_seconds().div(60).round(2)
    return refreshed


def filter_data(data: pd.DataFrame, barcode_summary: pd.DataFrame, factory: str, so_no: str, barcode: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    filtered = data
    filtered_summary = barcode_summary
    if factory != "ALL":
        filtered = filtered[filtered["plant"] == factory]
        filtered_summary = filtered_summary[filtered_summary["plant"] == factory]
    if so_no:
        so_key = so_no.strip().casefold()
        filtered = filtered[filtered["so_no"].map(display_identifier).str.strip().str.casefold().eq(so_key)]
        filtered_summary = filtered_summary[filtered_summary["so_no"].map(display_identifier).str.strip().str.casefold().eq(so_key)]
    if barcode:
        barcode_key = barcode.strip().casefold()
        filtered = filtered[filtered["barcode"].astype(str).str.strip().str.casefold().eq(barcode_key)]
        filtered_summary = filtered_summary[filtered_summary["barcode"].astype(str).str.strip().str.casefold().eq(barcode_key)]
    return filtered, filtered_summary


def flow_metrics(process_rows: pd.DataFrame, summary: pd.DataFrame) -> pd.DataFrame:
    metrics = process_rows.groupby("process", as_index=False).agg(input_qty=("input_qty", "sum"), output_qty=("output_qty", "sum"))
    flow = pd.DataFrame({"process": PROCESS_ORDER}).merge(metrics, on="process", how="left").fillna(0)
    flow["wip_qty"] = 0.0
    flow["wip_barcodes"] = 0
    for source, target in TRANSITIONS:
        waiting_mask = summary[source].notna() & summary[target].isna()
        flow.loc[flow["process"] == source, "wip_qty"] = summary.loc[waiting_mask, "CUT_wip_basis"].clip(lower=0).sum()
        # CUT is the canonical barcode source across every process.
        flow.loc[flow["process"] == source, "wip_barcodes"] = int(summary.loc[waiting_mask, "CUT_barcode_count"].sum())
    return flow.assign(process_order=lambda frame: frame.index + 1)


def elapsed_metrics(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for flow_order, (source, target) in enumerate(TRANSITIONS, start=1):
        valid_rows = summary[source].notna() & summary[target].notna()
        if source == "CUT":
            valid_rows &= ~summary[source].dt.time.eq(time(0, 0))
        raw_durations = (summary.loc[valid_rows, target] - summary.loc[valid_rows, source]).dt.total_seconds().div(60)
        invalid_count = int((raw_durations < 0).sum())
        durations = raw_durations[raw_durations >= 0]
        if len(durations) >= 4:
            q1, q3 = durations.quantile([0.25, 0.75])
            outlier_threshold = q3 + 1.5 * (q3 - q1)
            outlier_count = int((durations > outlier_threshold).sum())
        else:
            outlier_threshold = None
            outlier_count = 0
        rows.append(
            {
                "flow": f"{source} -> {target}",
                "flow_order": flow_order,
                "sample_count": len(durations),
                "invalid_count": invalid_count,
                "median_minutes": durations.median() if not durations.empty else None,
                "mode_minutes": durations.round(2).mode().iloc[0] if not durations.empty else None,
                "outlier_threshold_minutes": outlier_threshold,
                "outlier_count": outlier_count,
            }
        )
    return pd.DataFrame(rows)


def waiting_metrics(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    now = datetime.now()
    for flow_order, (source, target) in enumerate(TRANSITIONS, start=1):
        waiting = summary[summary[source].notna() & summary[target].isna()].copy()
        minutes = (now - waiting[source]).dt.total_seconds().div(60) if not waiting.empty else pd.Series(dtype=float)
        if source == "CUT" and not waiting.empty:
            minutes = minutes[~waiting[source].dt.time.eq(time(0, 0))]
        minutes = minutes[minutes >= 0]
        rows.append(
            {
                "flow": f"{source} -> {target}",
                "flow_order": flow_order,
                "waiting_barcodes": int(waiting["CUT_barcode_count"].sum()),
                "waiting_qty": waiting["CUT_wip_basis"].clip(lower=0).sum(),
                "median_waiting_minutes": minutes.median() if not minutes.empty else None,
            }
        )
    return pd.DataFrame(rows)


def metric_card(label: str, value: str, detail: str, tone: str = "blue") -> None:
    st.markdown(f'<div class="metric-card {tone}"><div class="metric-label">{label}</div><div class="metric-value">{value}</div><div class="metric-detail">{detail}</div></div>', unsafe_allow_html=True)


def compact_number(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def display_identifier(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    if isinstance(value, (int, float)) and float(value).is_integer():
        return str(int(value))
    return str(value)


def reset_factory_dependents() -> None:
    st.session_state["so_filter"] = "All"
    st.session_state["barcode_filter"] = "All"


def reset_so_dependents() -> None:
    st.session_state["barcode_filter"] = "All"


st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');
:root { --ink:#112642; --muted:#64748b; --line:#d9e3ec; --blue:#1684e8; --surface:#ffffff; --canvas:#f4f7fa; }
html, body, [class*="css"] { font-family:'DM Sans',sans-serif; color:var(--ink); }
.stApp { background:var(--canvas); color:var(--ink) !important; }
.block-container { max-width:1480px; padding:24px 28px 44px; }
[data-testid="stMainBlockContainer"], [data-testid="stAppViewBlockContainer"], div.block-container { padding-top:4px !important; }
header[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"] { display:none !important; }
.stApp [data-testid="stMarkdownContainer"], .stApp [data-testid="stWidgetLabel"] p { color:var(--ink) !important; }
h1,h2,h3 { font-family:'Space Grotesk',sans-serif; letter-spacing:0; }
.hero-content { padding:0 0 10px; }
.hero-content h1 { color:var(--ink) !important; font-size:30px; line-height:1.15; margin:0 0 5px; }
.hero-copy { color:#096bc4 !important; font-weight:700; font-size:14px; }
.hero-sub,.section-title span { color:var(--muted) !important; font-size:12px; }
.refresh-time { color:var(--muted) !important; font-size:10px; line-height:1.35; margin-top:4px; text-align:right; white-space:nowrap; }
.hero-rule { border-bottom:1px solid var(--line); margin:5px 0 14px; }
.stButton button { min-height:38px; border:1px solid #bfd0df; border-radius:7px; background:#fff; color:#294966; font-size:11px; font-weight:700; }
.metric-card { background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:12px 14px; min-height:96px; box-shadow:0 3px 12px rgba(17,38,66,.04); }
.metric-card.blue { border-top:3px solid #1682eb; }.metric-card.green { border-top:3px solid #27ae68; }.metric-card.orange { border-top:3px solid #f59e0b; }.metric-card.red { border-top:3px solid #ed5b6b; }
.metric-label { color:#48627d !important; font-size:12px; font-weight:700; }
.metric-value { color:var(--ink) !important; font-family:'Space Grotesk'; font-size:26px; line-height:1.15; font-weight:700; margin:6px 0 4px; }
.metric-detail { color:var(--muted) !important; font-size:10px; }
.section-title { display:flex; align-items:baseline; gap:10px; margin:18px 0 9px; }
.section-title h2 { color:var(--ink) !important; margin:0; font-size:21px; }
.flow-scroll { overflow-x:auto; padding:1px 1px 8px; scrollbar-width:thin; }
.flow-track { display:grid; grid-template-columns:repeat(5, minmax(155px, 1fr) minmax(92px, .48fr)) minmax(155px, 1fr); align-items:stretch; min-width:1240px; }
.flow-card { background:var(--surface); border-radius:8px; border:1px solid var(--line); padding:13px 14px; min-height:122px; box-shadow:0 3px 12px rgba(17,38,66,.035); }
.flow-name { color:#294966 !important; font-size:14px; font-weight:700; }
.flow-value { color:var(--ink); font-family:'Space Grotesk'; font-size:23px; line-height:1.1; font-weight:700; margin-top:9px; }
.flow-meta { color:var(--muted) !important; font-size:10px; line-height:1.35; margin-top:5px; white-space:normal; }
.flow-connector { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:7px; padding:0 7px; }
.flow-elapsed { width:100%; background:#f7fbff; border:1px solid #d9e8f6; border-radius:6px; color:#526b85 !important; font-size:10px; line-height:1.35; text-align:center; padding:7px 4px; }
.flow-elapsed strong { color:var(--ink) !important; display:block; font-size:11px; }
.flow-arrow { color:#187ce4; font-size:24px; line-height:.7; text-align:center; }
.panel-title { color:var(--ink) !important; font-family:'Space Grotesk'; font-weight:700; font-size:14px; }
.panel-sub { color:var(--muted) !important; font-size:11px; margin:2px 0 4px; min-height:15px; }
div[data-testid="stVerticalBlockBorderWrapper"] { background:var(--surface); border-color:var(--line); border-radius:8px; box-shadow:0 3px 12px rgba(17,38,66,.04); }
div[data-testid="stSelectbox"] label, div[data-testid="stTextInput"] label, div[data-testid="stDateInput"] label { font-size:11px; color:#48627d !important; font-weight:700; }
div[data-baseweb="select"] > div, div[data-testid="stTextInput"] input { min-height:42px; background:#fff; border-color:#cad8e5; }
.stTabs [data-baseweb="tab-list"] { gap:4px; border-bottom:1px solid var(--line); margin-top:14px; }
.stTabs [data-baseweb="tab"] { height:42px; padding:0 18px; color:#526b85; font-size:12px; font-weight:700; }
.stTabs [aria-selected="true"] { color:#096bc4 !important; }
.stTabs [data-baseweb="tab-panel"], .stTabs [role="tabpanel"] { padding-top:4px !important; }
.stTabs .section-title { margin-top:6px; }
.insight { background:var(--surface); border:1px solid var(--line); border-left:4px solid #f2ad32; border-radius:8px; padding:9px 12px; color:#573f12 !important; font-size:11px; line-height:1.45; min-height:72px; }
.insight strong { color:#8a5a06 !important; }.empty { color:#526b85 !important; padding:26px; text-align:center; background:#fff; border:1px dashed #bfd0df; border-radius:8px; font-size:12px; }
.story-summary { margin:12px 0 2px; padding:13px 16px; background:#fff; border:1px solid var(--line); border-left:4px solid #1684e8; border-radius:8px; }
.story-summary h3 { margin:0 0 8px; color:var(--ink) !important; font-size:15px; }
.story-grid { display:grid; grid-template-columns:1.2fr 1fr 1fr; gap:18px; }
.story-item { color:#526b85 !important; font-size:11px; line-height:1.5; }
.story-item strong { display:block; color:#096bc4 !important; font-size:11px; margin-bottom:2px; }
@media (max-width:900px) { [data-testid="stMainBlockContainer"], [data-testid="stAppViewBlockContainer"], div.block-container { padding:4px 14px 32px !important; }.hero-content h1 { font-size:25px; }.refresh-time { text-align:left; }.section-title { display:block; }.section-title span { display:block; margin-top:3px; } }
@media (max-width:900px) { .story-grid { grid-template-columns:1fr; gap:9px; } }
</style>
""", unsafe_allow_html=True)

data, barcode_summary, data_refreshed_at = get_data(DATA_MODEL_VERSION)
required_summary_columns = {"barcode", "plant", "so_year", "so_no", "current_stage", "current_output_qty", "aging_minutes"}
if not required_summary_columns.issubset(barcode_summary.columns):
    barcode_summary = make_barcode_summary(data)
hero_cols = st.columns([4.5, 1], vertical_alignment="bottom")
with hero_cols[0]:
    st.markdown('<div class="hero-content"><h1>Production Process Monitoring</h1><div class="hero-copy">ติดตามการไหลของงานตั้งแต่ Process CUT → PACK</div></div>', unsafe_allow_html=True)
with hero_cols[1]:
    st.markdown(f'<div class="refresh-time">Data refreshed<br><strong>{data_refreshed_at:%d/%m/%Y %H:%M:%S}</strong></div>', unsafe_allow_html=True)
st.markdown('<div class="hero-rule"></div>', unsafe_allow_html=True)

filter_cols = st.columns([1.0, 1.15, 1.15])
with filter_cols[0]:
    factory = st.selectbox("Factory", FACTORIES, index=0, key="factory_filter", on_change=reset_factory_dependents)
factory_rows = barcode_summary if factory == "ALL" else barcode_summary[barcode_summary["plant"] == factory]
so_choices = sorted({display_identifier(value) for value in factory_rows["so_no"].dropna()})
with filter_cols[1]:
    so_selection = st.selectbox("SO NO", ["All", *so_choices], index=0, accept_new_options=True, placeholder="พิมพ์หรือเลือก SO NO", key="so_filter", on_change=reset_so_dependents)
so_search = "" if not so_selection or so_selection == "All" else str(so_selection).strip()
barcode_rows = factory_rows
if so_search:
    so_key = so_search.casefold()
    barcode_rows = barcode_rows[barcode_rows["so_no"].map(display_identifier).str.strip().str.casefold().eq(so_key)]
barcode_choices = sorted(barcode_rows["barcode"].dropna().astype(str).unique().tolist())
with filter_cols[2]:
    barcode_selection = st.selectbox("Barcode", ["All", *barcode_choices], index=0, accept_new_options=True, placeholder="พิมพ์หรือเลือก Barcode", key="barcode_filter")
barcode_search = "" if not barcode_selection or barcode_selection == "All" else str(barcode_selection).strip()

filtered, summary = filter_data(data, barcode_summary, factory, so_search, barcode_search)
summary = refresh_summary_times(summary)
total_barcodes = len(summary)
completed = int((summary["status"] == "COMPLETED").sum()) if not summary.empty else 0
waiting = total_barcodes - completed
completion_rate = completed / total_barcodes * 100 if total_barcodes else 0
avg_aging = summary.loc[summary["status"] != "COMPLETED", "aging_minutes"].mean() if not summary.empty else None

st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
kpi_cols = st.columns(5)
with kpi_cols[0]:
    metric_card("Total Barcodes", f"{total_barcodes:,}", "Barcode")
with kpi_cols[1]:
    metric_card("Completed Barcodes", f"{completed:,}", f"{completion_rate:.2f}% · reached PACK", "green")
with kpi_cols[2]:
    metric_card("Open Barcodes", f"{waiting:,}", "Waiting or in process", "orange")
with kpi_cols[3]:
    metric_card("Completion Rate", f"{completion_rate:.2f}%", "Completed / Total × 100", "blue")
with kpi_cols[4]:
    metric_card("Avg. Aging (Minutes)", f"{avg_aging:,.2f}" if pd.notna(avg_aging) else "-", "One aging value per barcode", "red")

overview_tab, analysis_tab, drilldown_tab = st.tabs(["Production Flow", "Time & Waiting Analysis", "Barcode Drill-down"], key="main_tabs")

overview_tab.markdown('<div class="section-title"><h2>Production Flow</h2><span>Input / Output / WIP · CUT → PACK</span></div>', unsafe_allow_html=True)
flow = flow_metrics(filtered, summary)
flow_elapsed = elapsed_metrics(summary)
flow_parts = ['<div class="flow-scroll"><div class="flow-track">']
for index, row in flow.iterrows():
    tooltip = escape(f'WIP {row["wip_qty"]:,.0f} pcs · {int(row["wip_barcodes"]):,} Barcode', quote=True)
    flow_parts.append(f'<div class="flow-card" title="{tooltip}"><div class="flow-name">{PROCESS_LABELS[row["process"]]}</div><div class="flow-meta">Input {row["input_qty"]:,.0f}<br>Output {row["output_qty"]:,.0f}</div><div class="flow-value">{row["wip_qty"]:,.0f}</div><div class="flow-meta">WIP (pcs)</div></div>')
    if index < 5:
        elapsed_row = flow_elapsed.iloc[index]
        median_value = f"{elapsed_row['median_minutes']:.2f} min" if pd.notna(elapsed_row["median_minutes"]) else "-"
        mode_value = f"{elapsed_row['mode_minutes']:.2f} min" if pd.notna(elapsed_row["mode_minutes"]) else "-"
        flow_parts.append(f'<div class="flow-connector"><div class="flow-elapsed"><strong>Median {median_value}</strong>Mode {mode_value}</div><div class="flow-arrow">›</div></div>')
flow_parts.append('</div></div>')
overview_tab.markdown("".join(flow_parts), unsafe_allow_html=True)

elapsed = flow_elapsed
waiting_metrics_data = waiting_metrics(summary)
waiting_metrics_data["source_process"] = waiting_metrics_data["flow"].str.split(" -> ").str[0]
waiting_metrics_data = waiting_metrics_data.merge(
    flow[["process", "output_qty", "wip_qty", "wip_barcodes"]].rename(columns={"process": "source_process", "output_qty": "process_output_qty"}),
    on="source_process",
    how="left",
)
insight_elapsed = elapsed.loc[elapsed["median_minutes"].notna()].sort_values("median_minutes", ascending=False).iloc[0] if elapsed["median_minutes"].notna().any() else None
insight_waiting = waiting_metrics_data.sort_values("waiting_barcodes", ascending=False).iloc[0] if not waiting_metrics_data.empty else None
insight_outlier = elapsed.loc[elapsed["median_minutes"].notna()].sort_values(["outlier_count", "median_minutes"], ascending=False).iloc[0] if elapsed["median_minutes"].notna().any() else None
elapsed_flow_text = insight_elapsed["flow"] if insight_elapsed is not None else "-"
elapsed_minutes_text = f'{insight_elapsed["median_minutes"]:.2f} min' if insight_elapsed is not None else "-"
elapsed_mode_text = f'{insight_elapsed["mode_minutes"]:.2f} min' if insight_elapsed is not None and pd.notna(insight_elapsed["mode_minutes"]) else "-"

scope_parts = [f"Factory {factory}"]
if so_search:
    scope_parts.append(f"SO {so_search}")
if barcode_search:
    scope_parts.append(f"Barcode {barcode_search}")
scope_text = " · ".join(scope_parts)

priority_pool = waiting_metrics_data[(waiting_metrics_data["wip_qty"] > 0) | (waiting_metrics_data["wip_barcodes"] > 0)]
priority_flow = priority_pool.sort_values(["wip_qty", "wip_barcodes", "median_waiting_minutes"], ascending=False).iloc[0] if not priority_pool.empty else None
if priority_flow is not None:
    situation_text = (
        f'{priority_flow["flow"]} มี WIP {priority_flow["wip_qty"]:,.0f} ชิ้น '
        f'จาก {priority_flow["wip_barcodes"]:,.0f} Barcode'
    )
else:
    situation_text = "ไม่พบ WIP หรือ Barcode ที่กำลังรอ Process ถัดไป"

valid_flow_medians = elapsed.loc[elapsed["median_minutes"].notna(), "median_minutes"]
typical_flow_median = valid_flow_medians.median() if not valid_flow_medians.empty else None
if insight_elapsed is not None:
    longest_median = insight_elapsed["median_minutes"]
    if pd.notna(typical_flow_median) and typical_flow_median > 0:
        severity_text = f'{insight_elapsed["flow"]} ใช้เวลานานที่สุด {longest_median:,.2f} นาที หรือ {longest_median / typical_flow_median:,.1f} เท่าของ Median ทุก Flow'
    else:
        severity_text = f'{insight_elapsed["flow"]} ใช้เวลานานที่สุด {longest_median:,.2f} นาที'
else:
    severity_text = "ยังไม่มีคู่เวลา Process ที่เพียงพอสำหรับวิเคราะห์ความล่าช้า"

aged_open = summary[(summary["status"] != "COMPLETED") & summary["aging_minutes"].notna()]
oldest_barcode = aged_open.sort_values("aging_minutes", ascending=False).iloc[0] if not aged_open.empty else None
if oldest_barcode is not None:
    action_text = (
        f'ตรวจ Barcode {display_identifier(oldest_barcode["barcode"])} '
        f'(SO {display_identifier(oldest_barcode["so_no"])}) ก่อน: อยู่ที่ {oldest_barcode["current_position"]} '
        f'และมี Aging {oldest_barcode["aging_minutes"]:,.0f} นาที'
    )
elif priority_flow is not None:
    action_text = f'เริ่มตรวจสอบข้อมูล Scan และกำลังการผลิตในช่วง {priority_flow["flow"]}'
else:
    action_text = "ยังไม่มีงานเปิดที่มีเวลาเพียงพอสำหรับจัดลำดับการตรวจสอบ"

analysis_tab.markdown('<div class="section-title"><h2>Time & Waiting Analysis</h2><span>วิเคราะห์เวลาที่ใช้ และปริมาณงานรอ เพื่อระบุจุดที่ควรตรวจสอบ</span></div>', unsafe_allow_html=True)
insight_cols = analysis_tab.columns([1, 1, 1])
with insight_cols[0]:
    st.markdown(f'<div class="insight"><strong>Key Finding</strong><br>{elapsed_flow_text} ใช้เวลานานที่สุด<br><b>Median {elapsed_minutes_text} · Mode {elapsed_mode_text}</b></div>', unsafe_allow_html=True)
with insight_cols[1]:
    outlier_mode_text = f'{insight_outlier["mode_minutes"]:.2f} min' if insight_outlier is not None and pd.notna(insight_outlier["mode_minutes"]) else "-"
    outlier_detail = f'{int(insight_outlier["outlier_count"]):,} outliers  {outlier_mode_text}' if insight_outlier is not None else "-"
    st.markdown(f'<div class="insight"><strong>01 &nbsp; Abnormal elapsed time (เวลาที่ใช้ผิดปกติ)</strong><br>{insight_outlier["flow"] if insight_outlier is not None else "-"}<br><b>{outlier_detail}</b></div>', unsafe_allow_html=True)
with insight_cols[2]:
    waiting_minutes_text = f'{insight_waiting["median_waiting_minutes"]:.0f} min median' if insight_waiting is not None and pd.notna(insight_waiting["median_waiting_minutes"]) else "-"
    waiting_qty_text = f'{insight_waiting["wip_qty"]:,.0f}' if insight_waiting is not None else "-"
    st.markdown(f'<div class="insight"><strong>02 &nbsp; Highest waiting volume (ปริมาณงานรอสูงสุด)</strong><br>{insight_waiting["flow"] if insight_waiting is not None else "-"}<br><b>{compact_number(insight_waiting["waiting_barcodes"]) if insight_waiting is not None else "-"}</b> barcodes · {waiting_qty_text} pcs WIP · {waiting_minutes_text}</div>', unsafe_allow_html=True)

analysis_tab.markdown(
    f'<div class="story-summary"><h3>Operational Story · {escape(scope_text)}</h3>'
    f'<div class="story-grid">'
    f'<div class="story-item"><strong>สิ่งที่เกิดขึ้น</strong>{escape(situation_text)}</div>'
    f'<div class="story-item"><strong>ระดับความรุนแรง</strong>{escape(severity_text)}</div>'
    f'<div class="story-item"><strong>ควรตรวจสอบก่อน</strong>{escape(action_text)}</div>'
    f'</div></div>',
    unsafe_allow_html=True,
)

chart_row_1 = analysis_tab.columns(2)
with chart_row_1[0]:
    with st.container(border=True):
        st.markdown('<div class="panel-title">Process Elapsed Time (Minutes)</div><div class="panel-sub">Lead Time ของทุกช่วงที่ยืนยันเวลาต้นทางและปลายทางได้ โดยไม่นับค่าที่เวลาติดลบและ CUT เวลา 00:00:00</div>', unsafe_allow_html=True)
        chart_data = elapsed.melt(id_vars="flow", value_vars=["median_minutes", "mode_minutes"], var_name="measure", value_name="minutes")
        chart_data["measure"] = chart_data["measure"].map({"median_minutes": "Median Minutes", "mode_minutes": "Mode Minutes"})
        fig = px.bar(chart_data, x="flow", y="minutes", color="measure", barmode="group", color_discrete_map={"Median Minutes": BLUE, "Mode Minutes": "#183f7a"})
        fig.update_layout(height=270, margin=dict(l=55, r=18, t=42, b=66), font=dict(size=10, color=INK), plot_bgcolor="white", paper_bgcolor="white", legend=dict(orientation="h", y=1.2, x=0, title_text=None, font=dict(size=10, color=INK), bgcolor="rgba(255,255,255,.92)"), yaxis=dict(gridcolor="#e7eef5"), xaxis=dict(tickangle=-20))
        fig.update_xaxes(tickfont=dict(color=INK), title_font=dict(color=INK))
        fig.update_yaxes(tickfont=dict(color=INK), title_font=dict(color=INK))
        fig.update_traces(texttemplate="%{y:,.2f} min", textposition="outside", cliponaxis=False, hovertemplate="%{x}<br>%{y:,.2f} min<extra>%{fullData.name}</extra>")
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, theme=None)
with chart_row_1[1]:
    with st.container(border=True):
        st.markdown('<div class="panel-title">Waiting Barcode by Process Flow</div><div class="panel-sub">จำนวนบาร์โค้ดที่ผ่านกระบวนการต้นทางแล้ว แต่ยังไม่พบในกระบวนการถัดไป</div>', unsafe_allow_html=True)
        fig = px.bar(waiting_metrics_data, x="waiting_barcodes", y="flow", orientation="h", text="waiting_barcodes", color="waiting_barcodes", color_continuous_scale=[[0, "#1a8ced"], [1, RED]])
        fig.update_layout(height=270, margin=dict(l=112, r=88, t=18, b=50), font=dict(size=10, color=INK), plot_bgcolor="white", paper_bgcolor="white", coloraxis_showscale=False, xaxis=dict(gridcolor="#e7eef5"), yaxis=dict(title=None))
        fig.update_xaxes(tickfont=dict(color=INK), title_font=dict(color=INK))
        fig.update_yaxes(tickfont=dict(color=INK), title_font=dict(color=INK))
        fig.update_traces(texttemplate="%{x:,.0f} barcode", textposition="outside", textfont=dict(size=9), cliponaxis=False, hovertemplate="%{y}<br>%{x:,.0f} barcode<extra></extra>")
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, theme=None)

chart_row_2 = analysis_tab.columns(2)
with chart_row_2[0]:
    with st.container(border=True):
        st.markdown('<div class="panel-title">Potential Bottleneck by Process Flow</div><div class="panel-sub">X = Aging · Y = Waiting Barcode · ขนาด = WIP Pcs · สี = Median Elapsed Time</div>', unsafe_allow_html=True)
        bottleneck = waiting_metrics_data.merge(elapsed[["flow", "median_minutes"]], on="flow", how="left")
        bottleneck["bubble_size"] = bottleneck["wip_qty"].clip(lower=0).fillna(0) + 1
        bottleneck["elapsed_color"] = bottleneck["median_minutes"].fillna(0)
        fig = px.scatter(
            bottleneck,
            x="median_waiting_minutes",
            y="waiting_barcodes",
            hover_name="flow",
            size="bubble_size",
            size_max=34,
            color="elapsed_color",
            color_continuous_scale=[[0, "#1787e7"], [1, RED]],
            hover_data={"wip_qty": ":,.0f", "wip_barcodes": ":,.0f", "waiting_qty": ":,.0f", "process_output_qty": ":,.0f", "median_minutes": ":,.2f", "bubble_size": False, "elapsed_color": False},
        )
        fig.update_traces(marker=dict(line=dict(color="white", width=1)))
        fig.update_layout(height=270, margin=dict(l=72, r=20, t=18, b=54), font=dict(size=10, color=INK), plot_bgcolor="white", paper_bgcolor="white", coloraxis_colorbar=dict(title="Elapsed<br>(min)", thickness=9), xaxis=dict(title="Median aging (min)", gridcolor="#e7eef5"), yaxis=dict(title="Waiting barcode (barcode)", gridcolor="#e7eef5"))
        fig.update_xaxes(tickfont=dict(color=INK), title_font=dict(color=INK))
        fig.update_yaxes(tickfont=dict(color=INK), title_font=dict(color=INK))
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, theme=None)
with chart_row_2[1]:
    with st.container(border=True):
        st.markdown('<div class="panel-title">Process Quantity by Flow (Pcs)</div><div class="panel-sub">จำนวนชิ้นงาน Output ที่ผ่านแต่ละกระบวนการตามตัวกรองที่เลือก</div>', unsafe_allow_html=True)
        quantity_chart_data = flow[["process", "output_qty"]].copy()
        quantity_chart_data["quantity_label"] = quantity_chart_data["output_qty"].map(lambda value: f"{compact_number(value)} pcs")
        fig = px.bar(
            quantity_chart_data,
            x="process",
            y="output_qty",
            text="quantity_label",
            category_orders={"process": PROCESS_ORDER},
            color_discrete_sequence=[BLUE],
        )
        fig.update_layout(height=270, margin=dict(l=62, r=20, t=18, b=50), font=dict(size=10, color=INK), plot_bgcolor="white", paper_bgcolor="white", showlegend=False, xaxis=dict(title="process"), yaxis=dict(title="Pieces", rangemode="tozero", gridcolor="#e7eef5"))
        fig.update_xaxes(tickfont=dict(color=INK), title_font=dict(color=INK))
        fig.update_yaxes(tickfont=dict(color=INK), title_font=dict(color=INK), tickformat="~s")
        fig.update_traces(texttemplate="%{text}", textposition="outside", cliponaxis=False, hovertemplate="%{x}<br>%{y:,.0f} pcs<extra></extra>")
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, theme=None)

drilldown_tab.markdown('<div class="section-title"><h2>Barcode Drill-down</h2><span>เลือก Barcode เดียวเพื่อดูรายละเอียดและเวลาจริงของแต่ละช่วง</span></div>', unsafe_allow_html=True)
selected_barcode = barcode_search or None
selected_matches = summary[summary["barcode"].astype(str).str.casefold() == str(selected_barcode).strip().casefold()] if selected_barcode else summary.iloc[0:0]
if not selected_barcode:
    drilldown_tab.markdown('<div class="empty">เลือก Barcode จากตัวกรองด้านบนเพื่อดูสถานะและลำดับกระบวนการ</div>', unsafe_allow_html=True)
elif selected_matches.empty:
    drilldown_tab.markdown(f'<div class="empty">ไม่พบ Barcode {selected_barcode} ในข้อมูลตามตัวกรองปัจจุบัน</div>', unsafe_allow_html=True)
else:
    selected_summary = selected_matches.iloc[0]
    status_tone = "green" if selected_summary["status"] == "COMPLETED" else "orange" if selected_summary["status"] == "WAITING" else "blue"
    info_cols = drilldown_tab.columns(4)
    with info_cols[0]:
        metric_card("Current Position", selected_summary["current_position"], selected_summary["barcode"])
    with info_cols[1]:
        metric_card("Status", selected_summary["status"], f'SO NO: {display_identifier(selected_summary["so_no"])}', status_tone)
    with info_cols[2]:
        metric_card("Minutes Since CUT", f"{selected_summary['minutes_since_cut']:.2f} min" if pd.notna(selected_summary["minutes_since_cut"]) else "-", selected_summary["plant"])
    with info_cols[3]:
        metric_card("Total Lead Time", f"{selected_summary['total_lead_time_minutes']:.2f} min" if pd.notna(selected_summary["total_lead_time_minutes"]) else "-", "CUT → PACK", "blue")

    detail_cols = drilldown_tab.columns(1)
    with detail_cols[0]:
        with st.container(border=True):
            passed_processes = [process for process in PROCESS_ORDER if process != selected_summary["current_stage"] and pd.notna(selected_summary[process])]
            passed_text = ", ".join(passed_processes) if passed_processes else "-"
            cut_elapsed_text = f'{selected_summary["minutes_since_cut"]:,.2f} นาที' if pd.notna(selected_summary["minutes_since_cut"]) else "ไม่พบ CUT Scan"
            st.markdown(
                f'<div class="panel-title">Process Timeline</div><div class="panel-sub">ผ่านแล้ว: {passed_text} · ปัจจุบัน: {selected_summary["current_stage"]} · ตั้งแต่ CUT: {cut_elapsed_text}</div>',
                unsafe_allow_html=True,
            )
            timeline = go.Figure()
            timeline.add_trace(go.Scatter(x=list(range(6)), y=[1] * 6, mode="lines", line=dict(color="#b9d5ef", width=2), hoverinfo="skip", showlegend=False))
            current_label = "Completed" if selected_summary["status"] == "COMPLETED" else "Current"
            current_color = "#27ae68" if selected_summary["status"] == "COMPLETED" else "#f59e0b"
            timeline_groups = [
                ("Passed", BLUE, [process for process in PROCESS_ORDER if process != selected_summary["current_stage"] and pd.notna(selected_summary[process])]),
                (current_label, current_color, [selected_summary["current_stage"]]),
                ("Not Reached", "#c9d7e5", [process for process in PROCESS_ORDER if process != selected_summary["current_stage"] and pd.isna(selected_summary[process])]),
            ]
            for status_label, marker_color, processes in timeline_groups:
                if not processes:
                    continue
                positions = [PROCESS_ORDER.index(process) for process in processes]
                scan_times = [selected_summary[process].strftime("%d/%m/%Y %H:%M") if pd.notna(selected_summary[process]) else "-" for process in processes]
                timeline.add_trace(
                    go.Scatter(
                        x=positions,
                        y=[1] * len(processes),
                        mode="markers+text",
                        name=status_label,
                        text=processes,
                        textposition="top center",
                        customdata=scan_times,
                        marker=dict(size=12, color=marker_color, line=dict(color="white", width=2)),
                        hovertemplate="<b>%{text}</b><br>First Scan: %{customdata}<extra>%{fullData.name}</extra>",
                    )
                )
            timeline.update_layout(height=250, margin=dict(l=20, r=20, t=52, b=42), showlegend=True, legend=dict(orientation="h", x=.5, xanchor="center", y=-.08, yanchor="top", font=dict(size=10, color=INK)), xaxis=dict(showticklabels=False, showgrid=False, zeroline=False, showline=False, range=[-.3, 5.3]), yaxis=dict(showticklabels=False, showgrid=False, zeroline=False, showline=False, range=[.75, 1.25]), plot_bgcolor="white", paper_bgcolor="white")
            st.plotly_chart(timeline, width="stretch", config={"displayModeBar": False})
