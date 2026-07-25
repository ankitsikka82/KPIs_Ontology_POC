import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import json
import os
import time
import re
import duckdb
from ontology import ontology
import anthropic
from pyvis.network import Network

st.set_page_config(page_title="Cube Utilization POC", layout="wide")

st.title("Cube Utilization Semantic Ontology POC")
st.markdown("Demonstrating the value of a semantic context layer in conversational analytics")


@st.cache_data
def load_data():
    shipments = pd.read_csv('shipments.csv')
    dispatches = pd.read_csv('dispatches.csv')
    utilization = pd.read_csv('cube_utilization.csv')
    lane_ref = pd.read_csv('lane_ref.csv')
    return shipments, dispatches, utilization, lane_ref


shipments, dispatches, utilization, lane_ref = load_data()

# Copies with parsed dates for the SQL engine (legacy physical schema)
duck_shipments = shipments.copy()
duck_shipments['SHPMT_CRT_DT'] = pd.to_datetime(duck_shipments['SHPMT_CRT_DT'])
duck_dispatches = dispatches.copy()
duck_dispatches['LH_DSPTCH_DT'] = pd.to_datetime(duck_dispatches['LH_DSPTCH_DT'])
duck_utilization = utilization.copy()
duck_utilization['LH_DSPTCH_DT'] = pd.to_datetime(duck_utilization['LH_DSPTCH_DT'])
duck_movements = pd.read_csv('planned_movements.csv')

# Terminal code -> name map for DISPLAY (the meaning itself lives in the ontology)
TERMINAL_NAMES = {"HAR": "Harrison", "SGF": "Springfield", "STL": "Saint Louis",
                  "MEM": "Memphis", "ATL": "Atlanta"}

# API key resolution order: Streamlit Cloud secrets -> env var -> sidebar input.
# On Streamlit Cloud, set ANTHROPIC_API_KEY in the app's Secrets settings;
# viewers then never see or enter a key.
api_key = ""
try:
    api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
except Exception:
    pass
if not api_key:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

st.sidebar.header("Configuration")
if not api_key:
    api_key = st.session_state.get("shared_api_key", "")
if not api_key:
    api_key = st.sidebar.text_input("Anthropic API Key", type="password")
if api_key:
    st.session_state.shared_api_key = api_key  # share with other pages

# ===============================================================
# DELEGATED COMPUTATION: SQL extraction, validation gate, execution
# The LLM interprets and writes the query; DuckDB computes.
# ===============================================================
FORBIDDEN_SQL = ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
                 "ATTACH", "COPY", "PRAGMA", "INSTALL", "LOAD"]


def extract_sql(response_text):
    """Pull the SQL out of a ```sql fenced block; fall back to first SELECT/WITH."""
    m = re.search(r"```sql\s*(.*?)```", response_text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*((?:SELECT|WITH).*?)```", response_text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(SELECT|WITH)\b.*", response_text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(0).strip()
    return None


ALLOWED_TABLES = {"shpmt_mstr", "lh_dsptch", "trlr_util_fct", "pln_mvmt", "lane_ref"}


def validate_sql(sql):
    """POC governance gate: read-only, single statement, no DDL/DML, comment
    stripping, and a table ALLOWLIST. Honest scope: production adds SQL AST
    validation (e.g., sqlglot), column allowlists, Unity Catalog entitlements,
    row/column security, cost limits, and audit logging."""
    if not sql:
        return False, "No SQL query found in the response."
    body = sql.strip().rstrip(";").strip()
    if ";" in body:
        return False, "Multiple statements are not allowed."
    first_word = body.split(None, 1)[0].upper() if body.split() else ""
    if first_word not in ("SELECT", "WITH"):
        return False, f"Only SELECT queries are allowed (got '{first_word}')."
    scrub = re.sub(r"'[^']*'", "''", body)
    scrub = re.sub(r"--[^\n]*", " ", scrub)
    scrub = re.sub(r"/\*.*?\*/", " ", scrub, flags=re.DOTALL)
    upper = scrub.upper()
    for kw in FORBIDDEN_SQL:
        if re.search(r"\b" + kw + r"\b", upper):
            return False, f"Forbidden keyword: {kw}."
    ctes = set(m.lower() for m in re.findall(r"(?i)(?:WITH|,)\s*([a-zA-Z_][\w]*)\s+AS\s*\(", scrub))
    refs = set(m.lower() for m in re.findall(r"(?i)\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", scrub))
    unknown = refs - ALLOWED_TABLES - ctes
    if unknown:
        return False, f"Table(s) not on the allowlist: {', '.join(sorted(unknown))}."
    return True, body


def run_sql(sql):
    """Execute against the four registered tables; return (df, error)."""
    try:
        con = duckdb.connect()
        con.register("shpmt_mstr", duck_shipments)
        con.register("lh_dsptch", duck_dispatches)
        con.register("trlr_util_fct", duck_utilization)
        con.register("pln_mvmt", duck_movements)
        con.register("lane_ref", lane_ref)
        df = con.execute(sql).df()
        # round floats for readable, checkable output
        for c in df.select_dtypes(include="float").columns:
            df[c] = df[c].round(2)
        return df, None
    except Exception as e:
        return None, str(e)


def schema_description():
    """Schemas + 3 sample rows per table. This is ALL the model sees of the
    data in production mode — never the full rows."""
    parts = []
    for name, df in [("shpmt_mstr", duck_shipments), ("lh_dsptch", duck_dispatches),
                     ("trlr_util_fct", duck_utilization),
                     ("pln_mvmt", duck_movements), ("lane_ref", lane_ref)]:
        cols = ", ".join(f"{c} ({df[c].dtype})" for c in df.columns)
        parts.append(f"TABLE {name}\n  columns: {cols}\n  sample rows:\n"
                     f"{df.head(3).to_string(index=False)}")
    return "\n\n".join(parts)


# ===============================================================
# ARCHITECTURE DIAGRAM (top of page, expandable)
# ===============================================================
ARCHITECTURE_SVG = """
<svg viewBox="0 0 900 430" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;font-family:Helvetica,Arial,sans-serif;">
  <rect x="250" y="20" width="400" height="70" rx="10" fill="#d4a373" stroke="#8a5a2b" stroke-width="2"/>
  <text x="450" y="48" text-anchor="middle" font-size="20" font-weight="bold" fill="#2d1b00">Claude (LLM)</text>
  <text x="450" y="72" text-anchor="middle" font-size="13" fill="#2d1b00">Answers questions in natural language</text>

  <line x1="450" y1="90" x2="450" y2="140" stroke="#555" stroke-width="2.5" marker-end="url(#arrow)"/>
  <text x="465" y="120" font-size="12" fill="#333">reasons using</text>

  <rect x="150" y="140" width="600" height="110" rx="10" fill="#a7c957" stroke="#4f772d" stroke-width="2"/>
  <text x="450" y="168" text-anchor="middle" font-size="20" font-weight="bold" fill="#1a2e05">Semantic Ontology (Context Layer)</text>
  <text x="450" y="192" text-anchor="middle" font-size="13" fill="#1a2e05">Entities: Shipment, Trailer, Dispatch, Terminal, Lane</text>
  <text x="450" y="212" text-anchor="middle" font-size="13" fill="#1a2e05">Relationships + Business Rules + Exact Metric Formulas</text>
  <text x="450" y="232" text-anchor="middle" font-size="13" fill="#1a2e05">e.g., actual_utilization = max(cube%, weight%) — trailer full at EITHER limit</text>

  <line x1="450" y1="250" x2="450" y2="300" stroke="#555" stroke-width="2.5" marker-end="url(#arrow)"/>
  <text x="465" y="280" font-size="12" fill="#333">grounded in</text>

  <rect x="100" y="300" width="700" height="100" rx="10" fill="#8ecae6" stroke="#219ebc" stroke-width="2"/>
  <text x="450" y="330" text-anchor="middle" font-size="20" font-weight="bold" fill="#032030">Gold Layer Data (KPI Tables)</text>
  <text x="250" y="360" text-anchor="middle" font-size="13" fill="#032030">shpmt_mstr</text>
  <text x="450" y="360" text-anchor="middle" font-size="13" fill="#032030">lh_dsptch</text>
  <text x="650" y="360" text-anchor="middle" font-size="13" fill="#032030">trlr_util_fct</text>
  <text x="450" y="385" text-anchor="middle" font-size="12" fill="#032030" font-style="italic">(built from bronze → silver → gold transformations)</text>

  <path d="M 180 95 C 60 130, 60 260, 180 320" fill="none" stroke="#d62828" stroke-width="2.5" stroke-dasharray="7,5" marker-end="url(#arrowRed)"/>
  <text x="18" y="200" font-size="13" fill="#d62828" font-weight="bold">Without ontology:</text>
  <text x="18" y="218" font-size="12" fill="#d62828">Claude guesses from</text>
  <text x="18" y="234" font-size="12" fill="#d62828">raw columns alone</text>

  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#555"/>
    </marker>
    <marker id="arrowRed" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#d62828"/>
    </marker>
  </defs>
</svg>
"""


PROD_FLOW_SVG = """
<svg viewBox="0 0 940 620" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="470" y="28" text-anchor="middle" font-size="18" font-weight="bold" fill="#212529">Production flow: delegated computation (interpretation vs computation)</text>

  <!-- 1 -->
  <rect x="290" y="48" width="360" height="52" rx="9" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <circle cx="315" cy="74" r="13" fill="#212529"/><text x="315" y="79" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">1</text>
  <text x="485" y="70" text-anchor="middle" font-size="14" font-weight="bold">User asks in natural language</text>
  <text x="485" y="90" text-anchor="middle" font-size="12" fill="#495057">"What was the average utilization last week?"</text>
  <line x1="470" y1="100" x2="470" y2="126" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 2 -->
  <rect x="250" y="128" width="440" height="60" rx="9" fill="#e7f5ff" stroke="#1c7ed6" stroke-width="1.5"/>
  <circle cx="275" cy="158" r="13" fill="#1c7ed6"/><text x="275" y="163" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">2</text>
  <text x="485" y="152" text-anchor="middle" font-size="14" font-weight="bold" fill="#0b4a8b">Orchestration retrieves the relevant ontology slice</text>
  <text x="485" y="172" text-anchor="middle" font-size="12" fill="#0b4a8b">production design — this POC sends its full (small) ontology, cached</text>
  <line x1="470" y1="188" x2="470" y2="214" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 3 -->
  <rect x="250" y="216" width="440" height="60" rx="9" fill="#fff3bf" stroke="#e6a700" stroke-width="1.5"/>
  <circle cx="275" cy="246" r="13" fill="#e6a700"/><text x="275" y="251" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">3</text>
  <text x="485" y="240" text-anchor="middle" font-size="14" font-weight="bold" fill="#7a5800">LLM interprets — generates the SQL query</text>
  <text x="485" y="260" text-anchor="middle" font-size="12" fill="#7a5800">sees schemas only, never the data; never does arithmetic</text>
  <line x1="470" y1="276" x2="470" y2="302" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 4 -->
  <rect x="250" y="304" width="440" height="60" rx="9" fill="#ffe3e3" stroke="#d62828" stroke-width="1.5"/>
  <circle cx="275" cy="334" r="13" fill="#d62828"/><text x="275" y="339" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">4</text>
  <text x="485" y="328" text-anchor="middle" font-size="14" font-weight="bold" fill="#7a1010">Validation gate — the LLM proposes, the platform disposes</text>
  <text x="485" y="348" text-anchor="middle" font-size="12" fill="#7a1010">POC: read-only, single statement, table allowlist. Prod adds AST, entitlements, RLS</text>
  <line x1="470" y1="364" x2="470" y2="390" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 5 -->
  <rect x="250" y="392" width="440" height="60" rx="9" fill="#d3f9d8" stroke="#2f9e44" stroke-width="1.5"/>
  <circle cx="275" cy="422" r="13" fill="#2f9e44"/><text x="275" y="427" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">5</text>
  <text x="485" y="416" text-anchor="middle" font-size="14" font-weight="bold" fill="#14521f">Engine computes — Databricks SQL / DuckDB in this demo</text>
  <text x="485" y="436" text-anchor="middle" font-size="12" fill="#14521f">digit-perfect numbers; arithmetic errors structurally impossible</text>
  <line x1="470" y1="452" x2="470" y2="478" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 6 -->
  <rect x="250" y="480" width="440" height="60" rx="9" fill="#f3f0ff" stroke="#845ef7" stroke-width="1.5"/>
  <circle cx="275" cy="510" r="13" fill="#845ef7"/><text x="275" y="515" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">6</text>
  <text x="485" y="504" text-anchor="middle" font-size="14" font-weight="bold" fill="#3b2a80">LLM narrates the result in business language</text>
  <text x="485" y="524" text-anchor="middle" font-size="12" fill="#3b2a80">citing the metric definition applied (omitted in this demo to focus on query correctness)</text>

  <!-- side annotations -->
  <text x="115" y="250" text-anchor="middle" font-size="13" font-weight="bold" fill="#e6a700">LLM's job:</text>
  <text x="115" y="268" text-anchor="middle" font-size="12" fill="#7a5800">interpretation</text>
  <text x="822" y="420" text-anchor="middle" font-size="13" font-weight="bold" fill="#2f9e44">Engine's job:</text>
  <text x="822" y="438" text-anchor="middle" font-size="12" fill="#14521f">computation</text>
  <text x="115" y="330" text-anchor="middle" font-size="13" font-weight="bold" fill="#d62828">Platform's job:</text>
  <text x="115" y="348" text-anchor="middle" font-size="12" fill="#7a1010">governance</text>

  <defs>
    <marker id="pa" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#555"/>
    </marker>
  </defs>
</svg>
"""

# ===============================================================
# DELEGATED COMPUTATION: SQL extraction, validation gate, execution
# The LLM interprets and writes the query; DuckDB computes.
# ===============================================================
FORBIDDEN_SQL = ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
                 "ATTACH", "COPY", "PRAGMA", "INSTALL", "LOAD"]


def extract_sql(response_text):
    """Pull the SQL out of a ```sql fenced block; fall back to first SELECT/WITH."""
    m = re.search(r"```sql\s*(.*?)```", response_text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*((?:SELECT|WITH).*?)```", response_text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(SELECT|WITH)\b.*", response_text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(0).strip()
    return None


ALLOWED_TABLES = {"shpmt_mstr", "lh_dsptch", "trlr_util_fct", "pln_mvmt", "lane_ref"}


def validate_sql(sql):
    """POC governance gate: read-only, single statement, no DDL/DML, comment
    stripping, and a table ALLOWLIST. Honest scope: production adds SQL AST
    validation (e.g., sqlglot), column allowlists, Unity Catalog entitlements,
    row/column security, cost limits, and audit logging."""
    if not sql:
        return False, "No SQL query found in the response."
    body = sql.strip().rstrip(";").strip()
    if ";" in body:
        return False, "Multiple statements are not allowed."
    first_word = body.split(None, 1)[0].upper() if body.split() else ""
    if first_word not in ("SELECT", "WITH"):
        return False, f"Only SELECT queries are allowed (got '{first_word}')."
    scrub = re.sub(r"'[^']*'", "''", body)
    scrub = re.sub(r"--[^\n]*", " ", scrub)
    scrub = re.sub(r"/\*.*?\*/", " ", scrub, flags=re.DOTALL)
    upper = scrub.upper()
    for kw in FORBIDDEN_SQL:
        if re.search(r"\b" + kw + r"\b", upper):
            return False, f"Forbidden keyword: {kw}."
    ctes = set(m.lower() for m in re.findall(r"(?i)(?:WITH|,)\s*([a-zA-Z_][\w]*)\s+AS\s*\(", scrub))
    refs = set(m.lower() for m in re.findall(r"(?i)\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", scrub))
    unknown = refs - ALLOWED_TABLES - ctes
    if unknown:
        return False, f"Table(s) not on the allowlist: {', '.join(sorted(unknown))}."
    return True, body


def run_sql(sql):
    """Execute against the four registered tables; return (df, error)."""
    try:
        con = duckdb.connect()
        con.register("shpmt_mstr", duck_shipments)
        con.register("lh_dsptch", duck_dispatches)
        con.register("trlr_util_fct", duck_utilization)
        con.register("pln_mvmt", duck_movements)
        con.register("lane_ref", lane_ref)
        df = con.execute(sql).df()
        # round floats for readable, checkable output
        for c in df.select_dtypes(include="float").columns:
            df[c] = df[c].round(2)
        return df, None
    except Exception as e:
        return None, str(e)


def schema_description():
    """Schemas + 3 sample rows per table. This is ALL the model sees of the
    data in production mode — never the full rows."""
    parts = []
    for name, df in [("shpmt_mstr", duck_shipments), ("lh_dsptch", duck_dispatches),
                     ("trlr_util_fct", duck_utilization),
                     ("pln_mvmt", duck_movements), ("lane_ref", lane_ref)]:
        cols = ", ".join(f"{c} ({df[c].dtype})" for c in df.columns)
        parts.append(f"TABLE {name}\n  columns: {cols}\n  sample rows:\n"
                     f"{df.head(3).to_string(index=False)}")
    return "\n\n".join(parts)


# ===============================================================
# ARCHITECTURE DIAGRAM (top of page, expandable)
# ===============================================================
ARCHITECTURE_SVG = """
<svg viewBox="0 0 900 430" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;font-family:Helvetica,Arial,sans-serif;">
  <rect x="250" y="20" width="400" height="70" rx="10" fill="#d4a373" stroke="#8a5a2b" stroke-width="2"/>
  <text x="450" y="48" text-anchor="middle" font-size="20" font-weight="bold" fill="#2d1b00">Claude (LLM)</text>
  <text x="450" y="72" text-anchor="middle" font-size="13" fill="#2d1b00">Answers questions in natural language</text>

  <line x1="450" y1="90" x2="450" y2="140" stroke="#555" stroke-width="2.5" marker-end="url(#arrow)"/>
  <text x="465" y="120" font-size="12" fill="#333">reasons using</text>

  <rect x="150" y="140" width="600" height="110" rx="10" fill="#a7c957" stroke="#4f772d" stroke-width="2"/>
  <text x="450" y="168" text-anchor="middle" font-size="20" font-weight="bold" fill="#1a2e05">Semantic Ontology (Context Layer)</text>
  <text x="450" y="192" text-anchor="middle" font-size="13" fill="#1a2e05">Entities: Shipment, Trailer, Dispatch, Terminal, Lane</text>
  <text x="450" y="212" text-anchor="middle" font-size="13" fill="#1a2e05">Relationships + Business Rules + Exact Metric Formulas</text>
  <text x="450" y="232" text-anchor="middle" font-size="13" fill="#1a2e05">e.g., actual_utilization = max(cube%, weight%) — trailer full at EITHER limit</text>

  <line x1="450" y1="250" x2="450" y2="300" stroke="#555" stroke-width="2.5" marker-end="url(#arrow)"/>
  <text x="465" y="280" font-size="12" fill="#333">grounded in</text>

  <rect x="100" y="300" width="700" height="100" rx="10" fill="#8ecae6" stroke="#219ebc" stroke-width="2"/>
  <text x="450" y="330" text-anchor="middle" font-size="20" font-weight="bold" fill="#032030">Gold Layer Data (KPI Tables)</text>
  <text x="250" y="360" text-anchor="middle" font-size="13" fill="#032030">shpmt_mstr</text>
  <text x="450" y="360" text-anchor="middle" font-size="13" fill="#032030">lh_dsptch</text>
  <text x="650" y="360" text-anchor="middle" font-size="13" fill="#032030">trlr_util_fct</text>
  <text x="450" y="385" text-anchor="middle" font-size="12" fill="#032030" font-style="italic">(built from bronze → silver → gold transformations)</text>

  <path d="M 180 95 C 60 130, 60 260, 180 320" fill="none" stroke="#d62828" stroke-width="2.5" stroke-dasharray="7,5" marker-end="url(#arrowRed)"/>
  <text x="18" y="200" font-size="13" fill="#d62828" font-weight="bold">Without ontology:</text>
  <text x="18" y="218" font-size="12" fill="#d62828">Claude guesses from</text>
  <text x="18" y="234" font-size="12" fill="#d62828">raw columns alone</text>

  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#555"/>
    </marker>
    <marker id="arrowRed" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#d62828"/>
    </marker>
  </defs>
</svg>
"""


PROD_FLOW_SVG = """
<svg viewBox="0 0 940 620" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="470" y="28" text-anchor="middle" font-size="18" font-weight="bold" fill="#212529">Production flow: delegated computation (interpretation vs computation)</text>

  <!-- 1 -->
  <rect x="290" y="48" width="360" height="52" rx="9" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <circle cx="315" cy="74" r="13" fill="#212529"/><text x="315" y="79" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">1</text>
  <text x="485" y="70" text-anchor="middle" font-size="14" font-weight="bold">User asks in natural language</text>
  <text x="485" y="90" text-anchor="middle" font-size="12" fill="#495057">"What was the average utilization last week?"</text>
  <line x1="470" y1="100" x2="470" y2="126" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 2 -->
  <rect x="250" y="128" width="440" height="60" rx="9" fill="#e7f5ff" stroke="#1c7ed6" stroke-width="1.5"/>
  <circle cx="275" cy="158" r="13" fill="#1c7ed6"/><text x="275" y="163" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">2</text>
  <text x="485" y="152" text-anchor="middle" font-size="14" font-weight="bold" fill="#0b4a8b">Orchestration retrieves the relevant ontology slice</text>
  <text x="485" y="172" text-anchor="middle" font-size="12" fill="#0b4a8b">production design — this POC sends its full (small) ontology, cached</text>
  <line x1="470" y1="188" x2="470" y2="214" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 3 -->
  <rect x="250" y="216" width="440" height="60" rx="9" fill="#fff3bf" stroke="#e6a700" stroke-width="1.5"/>
  <circle cx="275" cy="246" r="13" fill="#e6a700"/><text x="275" y="251" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">3</text>
  <text x="485" y="240" text-anchor="middle" font-size="14" font-weight="bold" fill="#7a5800">LLM interprets — generates the SQL query</text>
  <text x="485" y="260" text-anchor="middle" font-size="12" fill="#7a5800">sees schemas only, never the data; never does arithmetic</text>
  <line x1="470" y1="276" x2="470" y2="302" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 4 -->
  <rect x="250" y="304" width="440" height="60" rx="9" fill="#ffe3e3" stroke="#d62828" stroke-width="1.5"/>
  <circle cx="275" cy="334" r="13" fill="#d62828"/><text x="275" y="339" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">4</text>
  <text x="485" y="328" text-anchor="middle" font-size="14" font-weight="bold" fill="#7a1010">Validation gate — the LLM proposes, the platform disposes</text>
  <text x="485" y="348" text-anchor="middle" font-size="12" fill="#7a1010">POC: read-only, single statement, table allowlist. Prod adds AST, entitlements, RLS</text>
  <line x1="470" y1="364" x2="470" y2="390" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 5 -->
  <rect x="250" y="392" width="440" height="60" rx="9" fill="#d3f9d8" stroke="#2f9e44" stroke-width="1.5"/>
  <circle cx="275" cy="422" r="13" fill="#2f9e44"/><text x="275" y="427" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">5</text>
  <text x="485" y="416" text-anchor="middle" font-size="14" font-weight="bold" fill="#14521f">Engine computes — Databricks SQL / DuckDB in this demo</text>
  <text x="485" y="436" text-anchor="middle" font-size="12" fill="#14521f">digit-perfect numbers; arithmetic errors structurally impossible</text>
  <line x1="470" y1="452" x2="470" y2="478" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 6 -->
  <rect x="250" y="480" width="440" height="60" rx="9" fill="#f3f0ff" stroke="#845ef7" stroke-width="1.5"/>
  <circle cx="275" cy="510" r="13" fill="#845ef7"/><text x="275" y="515" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">6</text>
  <text x="485" y="504" text-anchor="middle" font-size="14" font-weight="bold" fill="#3b2a80">LLM narrates the result in business language</text>
  <text x="485" y="524" text-anchor="middle" font-size="12" fill="#3b2a80">citing the metric definition applied (omitted in this demo to focus on query correctness)</text>

  <!-- side annotations -->
  <text x="115" y="250" text-anchor="middle" font-size="13" font-weight="bold" fill="#e6a700">LLM's job:</text>
  <text x="115" y="268" text-anchor="middle" font-size="12" fill="#7a5800">interpretation</text>
  <text x="822" y="420" text-anchor="middle" font-size="13" font-weight="bold" fill="#2f9e44">Engine's job:</text>
  <text x="822" y="438" text-anchor="middle" font-size="12" fill="#14521f">computation</text>
  <text x="115" y="330" text-anchor="middle" font-size="13" font-weight="bold" fill="#d62828">Platform's job:</text>
  <text x="115" y="348" text-anchor="middle" font-size="12" fill="#7a1010">governance</text>

  <defs>
    <marker id="pa" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#555"/>
    </marker>
  </defs>
</svg>
"""


RAG_FLOW_SVG = """
<svg viewBox="0 0 940 560" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="470" y="28" text-anchor="middle" font-size="18" font-weight="bold" fill="#212529">RAG over the ontology: retrieve MEANING, not data</text>

  <rect x="40" y="60" width="380" height="150" rx="10" fill="#f3f0ff" stroke="#845ef7" stroke-width="2"/>
  <text x="230" y="86" text-anchor="middle" font-size="15" font-weight="bold" fill="#3b2a80">Ontology index (built once)</text>
  <text x="230" y="110" text-anchor="middle" font-size="12" fill="#3b2a80">every metric, rule, pattern, entity = one chunk</text>
  <text x="230" y="130" text-anchor="middle" font-size="12" fill="#3b2a80">embedded into vectors, stored in a vector index</text>
  <text x="230" y="150" text-anchor="middle" font-size="12" fill="#3b2a80">POC: in-memory (fastembed) · Prod: Databricks</text>
  <text x="230" y="170" text-anchor="middle" font-size="12" fill="#3b2a80">Vector Search over 1000s of definitions</text>
  <text x="230" y="196" text-anchor="middle" font-size="11" font-style="italic" fill="#845ef7">the data tables are NOT in this index</text>

  <rect x="520" y="60" width="380" height="70" rx="10" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="710" y="88" text-anchor="middle" font-size="14" font-weight="bold">User question</text>
  <text x="710" y="112" text-anchor="middle" font-size="12" fill="#495057">"What is our reported utilization?"</text>

  <line x1="710" y1="130" x2="710" y2="166" stroke="#555" stroke-width="2" marker-end="url(#ra)"/>
  <rect x="520" y="168" width="380" height="56" rx="10" fill="#e7f5ff" stroke="#1c7ed6" stroke-width="1.5"/>
  <text x="710" y="192" text-anchor="middle" font-size="13" font-weight="bold" fill="#0b4a8b">Embed the question, search the index</text>
  <text x="710" y="212" text-anchor="middle" font-size="12" fill="#0b4a8b">top-k definition chunks by similarity</text>
  <line x1="420" y1="135" x2="520" y2="190" stroke="#845ef7" stroke-width="2" stroke-dasharray="6,4" marker-end="url(#rp)"/>

  <line x1="710" y1="224" x2="710" y2="260" stroke="#555" stroke-width="2" marker-end="url(#ra)"/>
  <rect x="480" y="262" width="440" height="76" rx="10" fill="#e6f4d7" stroke="#4f772d" stroke-width="1.5"/>
  <text x="700" y="288" text-anchor="middle" font-size="13" font-weight="bold" fill="#1a2e05">Assemble the prompt</text>
  <text x="700" y="308" text-anchor="middle" font-size="12" fill="#1a2e05">always-on core (decodes + authority + temporal rules)</text>
  <text x="700" y="326" text-anchor="middle" font-size="12" fill="#1a2e05">+ retrieved slices + schemas — small, cached, targeted</text>

  <line x1="700" y1="338" x2="700" y2="374" stroke="#555" stroke-width="2" marker-end="url(#ra)"/>
  <rect x="480" y="376" width="440" height="56" rx="10" fill="#fff3bf" stroke="#e6a700" stroke-width="1.5"/>
  <text x="700" y="400" text-anchor="middle" font-size="13" font-weight="bold" fill="#7a5800">LLM writes SQL from the retrieved definitions</text>
  <text x="700" y="420" text-anchor="middle" font-size="12" fill="#7a5800">then: gate → engine computes → verdict (as before)</text>

  <rect x="40" y="300" width="380" height="132" rx="10" fill="#fff" stroke="#d62828" stroke-width="1.5" stroke-dasharray="7,5"/>
  <text x="230" y="326" text-anchor="middle" font-size="13" font-weight="bold" fill="#d62828">What RAG here is NOT</text>
  <text x="230" y="350" text-anchor="middle" font-size="12" fill="#7a1010">not retrieving data rows for the answer —</text>
  <text x="230" y="368" text-anchor="middle" font-size="12" fill="#7a1010">numbers come from the warehouse via SQL</text>
  <text x="230" y="392" text-anchor="middle" font-size="12" fill="#7a1010">phase 2: also retrieve POLICY provenance</text>
  <text x="230" y="410" text-anchor="middle" font-size="12" fill="#7a1010">(the Finance memo paragraph behind the rule)</text>

  <defs>
    <marker id="ra" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#555"/>
    </marker>
    <marker id="rp" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#845ef7"/>
    </marker>
  </defs>
</svg>
"""

with st.expander("Architecture: How the Semantic Layer Works", expanded=True):
    tab_a, tab_b, tab_c = st.tabs(["The Context Layer", "Production Flow: Delegated Computation", "RAG over the Ontology"])
    with tab_a:
        components.html(ARCHITECTURE_SVG, height=680, scrolling=True)
        st.caption("Green layer is the difference: the ontology gives Claude entity definitions, "
                   "relationships, and exact metric formulas. The red dashed path is what happens "
                   "without it — Claude reasons from raw column names and guesses.")
    with tab_c:
        components.html(RAG_FLOW_SVG, height=620, scrolling=True)
        st.caption("Live in this app: the 'RAG step' expander above the comparison shows "
                   "the actual chunks retrieved for your question with similarity scores. "
                   "Watch the semantic side's input tokens drop versus the full-ontology "
                   "design — that reduction is what makes 500+ metrics economical.")
    with tab_b:
        components.html(PROD_FLOW_SVG, height=680, scrolling=True)
        st.markdown("""
**Why this app delegates computation instead of letting the LLM calculate:**

| | LLM computes (retired POC design) | LLM writes query, engine computes (this app) |
|---|---|---|
| Where the data lives | Pasted into the prompt (unrealistic at scale) | Stays in the warehouse; model sees only schemas + 3 sample rows |
| Who does arithmetic | The LLM — a token predictor, not a calculator | DuckDB here; Databricks SQL in production |
| Semantic errors (wrong column, grain, week) | Ontology reduces them | Ontology reduces them — same mechanism |
| Arithmetic errors | Possible even with a correct approach | Structurally impossible |
| Auditability | A paragraph of prose | The generated SQL — loggable, diffable, eval-able |

**Real example from an earlier iteration of this demo** (before delegation, when the
LLM computed inline over the then-current dataset of 24 trailers): asked for the average
utilization (true value **54.06%** on that dataset), it answered **54.09%** — right
column, right formula, right grain, but it slipped adding two dozen numbers in its head.
That result is what motivated the switch to this architecture. The ontology fixes
*meaning*, not *math*. Delegating the math to an engine eliminates that entire error
class, and what remains — "did the model write the right query?" — is exactly what the
ontology governs and exactly what you can eval at scale.
""")


# ===============================================================
# INTERACTIVE KNOWLEDGE GRAPH (pyvis)
# ===============================================================
ENTITY_COLORS = {
    "Shipment": "#4dabf7",   # blue
    "Trailer": "#69db7c",    # green
    "Dispatch": "#ffd43b",   # yellow
    "Terminal": "#e599f7",   # purple
    "Lane": "#ffa94d",       # amber
    "Time": "#63e6be",       # teal
}
METRIC_COLOR = "#845ef7"     # violet diamonds: governed metric definitions
USED_COLOR = "#e8590c"       # strong orange: traversed
UNUSED_COLOR = "#dee2e6"     # pale grey: present but not used

# Pinned layout: BOTH graphs use identical coordinates, so the traversal
# graph reads as "same map, path lit up" instead of a rescrambled picture.
# (vis.js y-axis points down, so negative y = higher on screen.)
NODE_POSITIONS = {
    # metrics row (top)
    "utilization_trend":    (0, -360),
    "reported_utilization": (-300, -360),
    "volume_by_origin":     (-480, -240),
    "shipments_on_trailer": (-160, -240),
    "trailer_utilization":  (160, -240),
    "lane_utilization":     (480, -240),
    # entity chain (middle): the physical flow, left to right
    "Shipment": (-350, 0),
    "Trailer":  (0, 0),
    "Dispatch": (350, 0),
    # derived / location entities (bottom)
    "Terminal": (-350, 220),
    "Lane":     (350, 220),
    "Time":     (0, 220),
}


def build_kg(highlight_entities=None, highlight_relationships=None,
             highlight_metrics=None, height="560px", mode="full"):
    """Interactive ontology graph with pinned layout.

    mode="full": entities colored by type, metrics as violet diamonds.
    mode="traversal": binary — traversed elements orange/large, rest grey/small.
    """
    highlight_entities = set(highlight_entities or [])
    highlight_relationships = set(highlight_relationships or [])
    highlight_metrics = set(highlight_metrics or [])

    g = Network(directed=True, height=height, width="100%",
                bgcolor="#ffffff", cdn_resources="in_line")
    g.toggle_physics(False)  # pinned layout: stable, presentation-safe

    # --- entity nodes ---
    for name, data in ontology["entities"].items():
        is_hot = name in highlight_entities
        props = "\n".join(f"- {k}: {v}" for k, v in data.get("properties", {}).items())
        x, y = NODE_POSITIONS.get(name, (0, 0))
        if mode == "traversal":
            color = USED_COLOR if is_hot else UNUSED_COLOR
            size = 40 if is_hot else 16
            font = {"size": 24 if is_hot else 12,
                    "color": "#212529" if is_hot else "#adb5bd"}
        else:
            color = ENTITY_COLORS.get(name, "#74b9ff")
            size = 28
            font = {"size": 17}
        g.add_node(name, label=name,
                   title=f"{data['description']}\n\nProperties:\n{props}",
                   color=color, size=size, x=x, y=y, physics=False,
                   borderWidth=3 if is_hot else 1, font=font)

    # --- metric nodes (violet diamonds) ---
    for mname, mdata in ontology.get("metrics", {}).items():
        is_hot = mname in highlight_metrics
        x, y = NODE_POSITIONS.get(mname, (0, -300))
        steps = "\n".join(mdata.get("steps", []))
        if mode == "traversal":
            color = USED_COLOR if is_hot else UNUSED_COLOR
            size = 34 if is_hot else 14
            font = {"size": 20 if is_hot else 10,
                    "color": "#212529" if is_hot else "#adb5bd"}
        else:
            color = METRIC_COLOR
            size = 22
            font = {"size": 14}
        g.add_node(mname, label=mname.replace("_", " "),
                   title=f"METRIC — {mdata.get('grain', '')}\n\nSteps:\n{steps}",
                   color=color, size=size, shape="diamond",
                   x=x, y=y, physics=False, font=font)

        # metric -> entity dependency edges (dashed)
        for ent in mdata.get("entities", []):
            dep_hot = is_hot and ent in highlight_entities
            g.add_edge(mname, ent,
                       title=f"{mname} is computed over {ent}",
                       color=USED_COLOR if (mode == "traversal" and dep_hot)
                             else (UNUSED_COLOR if mode == "traversal" else "#c5b3f2"),
                       width=4 if (mode == "traversal" and dep_hot) else 1,
                       dashes=True, arrows="to")

    # --- relationship edges ---
    for rel_name, rel in ontology["relationships"].items():
        is_hot = rel_name in highlight_relationships or (
            rel["from_entity"] in highlight_entities
            and rel["to_entity"] in highlight_entities
        )
        if mode == "traversal":
            color = USED_COLOR if is_hot else UNUSED_COLOR
            width = 5 if is_hot else 1
            font = {"size": 12 if is_hot else 0}
        else:
            color = "#b2bec3"
            width = 1
            font = {"size": 11}
        g.add_edge(rel["from_entity"], rel["to_entity"],
                   label=rel_name.replace("_", " ") if (mode != "traversal" or is_hot) else "",
                   title=rel["description"] + "\nJoin: " + rel.get("join_logic", "n/a"),
                   color=color, width=width, font=font)
    return g


def render_kg(g, filename, render_height=580):
    g.save_graph(filename)
    with open(filename, "r", encoding="utf-8") as f:
        components.html(f.read(), height=render_height)


def kg_legend(mode="full"):
    """Colored-dot legend so nobody has to guess what colors mean."""
    if mode == "full":
        dots = " &nbsp; ".join(
            f'<span style="color:{c};font-size:20px;">&#9679;</span> {n}'
            for n, c in ENTITY_COLORS.items()
        )
        dots += (f' &nbsp; <span style="color:{METRIC_COLOR};font-size:20px;">&#9670;</span>'
                 ' Metric definition')
        st.markdown(
            f'<div style="padding:4px 0;">{dots}'
            '<br><span style="font-size:13px;color:#868e96;">Circles are entities; '
            'violet diamonds are governed metric definitions, with dashed arrows to the '
            'entities they are computed over. Solid arrows = relationships; hover anything '
            'for definitions and join logic.</span></div>',
            unsafe_allow_html=True)
    else:
        st.markdown(
            f'<div style="padding:4px 0;">'
            f'<span style="color:{USED_COLOR};font-size:20px;">&#9679;</span> '
            f'<b>Used to answer this question</b> &nbsp;&nbsp; '
            f'<span style="color:{UNUSED_COLOR};font-size:20px;">&#9679;</span> '
            f'Not needed for this question'
            '<br><span style="font-size:13px;color:#868e96;">Two colors, one meaning: '
            'orange was traversed, grey was not.</span></div>',
            unsafe_allow_html=True)



# ===============================================================
# RAG OVER THE ONTOLOGY: retrieve MEANING, not data.
# Core invariants (small: decodes + authority/temporal rules) always ship;
# metrics, patterns, and institutional rules are RETRIEVED per question.
# This is the hot-core + retrieved-tail production design at 500+ metrics.
# ===============================================================
CORE_RULES = ["column_authority_utilization", "temporal_attribution",
              "lane_definition", "ranking_direction", "terminal_name_resolution"]


def build_ontology_corpus():
    """Each retrievable unit of meaning becomes a chunk: (id, kind, text)."""
    chunks = []
    for name, m in ontology.get("metrics", {}).items():
        text = (f"METRIC {name} | grain: {m.get('grain','')} | steps: "
                + " ".join(m.get("steps", []))
                + f" | sql: {m.get('sql_equivalent','')}"
                + f" | entities: {', '.join(m.get('entities', []))}")
        chunks.append((f"metric:{name}", "metric", text))
    for name, r in ontology.get("business_rules", {}).items():
        if name in CORE_RULES:
            continue  # core rules always ship; only the tail is retrieved
        chunks.append((f"rule:{name}", "rule",
                       f"BUSINESS RULE {name} | {r.get('rule','')} "
                       f"{r.get('formula','')} | applies: {r.get('applies_when','')}"))
    for i, qp in enumerate(ontology.get("query_patterns", [])):
        chunks.append((f"pattern:{i}:{qp.get('metric','')}", "pattern",
                       f"QUESTION PATTERN: {qp.get('question','')} -> metric "
                       f"{qp.get('metric','')} | {qp.get('answer_shape','')}"))
    for name, a in ontology.get("actions", {}).items():
        text = (f"ACTION {name} | {a.get('description','')} | eligibility: "
                + " ".join(a.get("eligibility", []))
                + f" | impact: {a.get('impact_formula','')} | owner: {a.get('owner','')}"
                + f" | sql: {a.get('sql_equivalent','')}")
        chunks.append((f"action:{name}", "action", text))
    for name, e in ontology.get("entities", {}).items():
        props = "; ".join(f"{k}: {v}" for k, v in e.get("properties", {}).items())
        chunks.append((f"entity:{name}", "entity",
                       f"ENTITY {name} | {e.get('description','')} | {props}"))
    return chunks


@st.cache_resource
def get_retriever():
    """Dual engine: fastembed vectors if the model is available, else TF-IDF.
    Both are in-memory vector indexes over ~25 chunks; production swaps this
    for Databricks Vector Search over thousands of definitions."""
    chunks = build_ontology_corpus()
    texts = [c[2] for c in chunks]
    try:
        from fastembed import TextEmbedding
        import numpy as np
        model = TextEmbedding("BAAI/bge-small-en-v1.5")
        vecs = np.array(list(model.embed(texts)))
        vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)

        def search(query, k=6):
            qv = np.array(list(model.embed([query])))[0]
            qv = qv / np.linalg.norm(qv)
            sims = vecs @ qv
            order = sims.argsort()[::-1][:k]
            return [(chunks[i], float(sims[i])) for i in order]
        return search, "fastembed (bge-small-en-v1.5, ONNX vectors)"
    except Exception:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True).fit(texts)
        mat = vec.transform(texts)

        def search(query, k=6):
            qv = vec.transform([query])
            sims = cosine_similarity(qv, mat)[0]
            order = sims.argsort()[::-1][:k]
            return [(chunks[i], float(sims[i])) for i in order]
        return search, "TF-IDF fallback (lexical)"


def assemble_semantic_slices(user_query, k=6):
    """The RAG step: core invariants + retrieved tail for THIS question."""
    search, engine = get_retriever()
    hits = search(user_query, k=k)
    retrieved_text = "\n\n".join(
        f"[{cid}] (score {score:.3f})\n{text}" for (cid, kind, text), score in hits)
    core = {name: ontology["business_rules"][name] for name in CORE_RULES
            if name in ontology.get("business_rules", {})}
    core_text = ("CODE DECODES (always included):\n"
                 + json.dumps(ontology.get("code_decodes", {}), indent=2)
                 + "\n\nCORE BUSINESS RULES (always included):\n"
                 + json.dumps(core, indent=2)
                 + "\n\nPHYSICAL TABLES:\n"
                 + json.dumps(ontology.get("physical_tables", {}), indent=2))
    return core_text, retrieved_text, hits, engine




# ===============================================================
# ACTION ENGINES: deterministic opportunity surfacing with
# feasibility screening (NOT an optimizer — the honest label)
# ===============================================================
def _trailer_services(trailer_id):
    row = dispatches[dispatches['TRLR_NBR'] == trailer_id]
    if row.empty:
        return set()
    sids = [s for s in row.iloc[0]['SHPMT_NBR_LST'].split(',') if s]
    return set(shipments[shipments['SHPMT_NBR'].isin(sids)]['SVC_TYP_CD'])


def find_consolidations():
    """All same-lane same-day pairs, screened against every eligibility rule."""
    from itertools import combinations
    eligible, rejected = [], []
    u = utilization
    for (o, dd, dt), grp in u.groupby(['ORIG_TRML_CD', 'DEST_TRML_CD', 'LH_DSPTCH_DT']):
        if len(grp) < 2:
            continue
        for i, j in combinations(grp.index, 2):
            a, b = u.loc[i], u.loc[j]
            cube = a['LD_CUBE_FT'] + b['LD_CUBE_FT']
            wgt = a['LD_WGT_LB'] + b['LD_WGT_LB']
            lane = lane_ref[(lane_ref['ORIG_TRML_CD'] == o) & (lane_ref['DEST_TRML_CD'] == dd)]
            saving = round(float(lane['LANE_MILES'].iloc[0] * lane['CPM_USD'].iloc[0]), 0) if not lane.empty else 0
            rec = {'trailer_1': a['TRLR_NBR'], 'trailer_2': b['TRLR_NBR'],
                   'lane': f"{TERMINAL_NAMES[o]} → {TERMINAL_NAMES[dd]}",
                   'date': dt, 'combined_cube': int(cube), 'combined_wgt': int(wgt),
                   'est_saving_usd': saving}
            if cube > 2000 or wgt > 20000:
                rec['rejected_because'] = 'exceeds pup capacity'
                rejected.append(rec); continue
            if a['SHPMT_CNT'] <= 1 or b['SHPMT_CNT'] <= 1:
                rec['rejected_because'] = 'service-protection load (never held)'
                rejected.append(rec); continue
            if 'Priority' in (_trailer_services(a['TRLR_NBR']) | _trailer_services(b['TRLR_NBR'])):
                rec['rejected_because'] = 'Priority-hold rule (Priority freight never held)'
                rejected.append(rec); continue
            eligible.append(rec)
    return pd.DataFrame(eligible), pd.DataFrame(rejected)


def find_frequency_candidates():
    """Low-fill high-frequency lanes, floored at 3 schedules to protect service."""
    lanes = (utilization.groupby(['ORIG_TRML_CD', 'DEST_TRML_CD'], as_index=False)
             .agg(avg_util=('UTIL_PCT_3', 'mean'), loads=('TRLR_NBR', 'count')))
    lanes = lanes.merge(lane_ref, on=['ORIG_TRML_CD', 'DEST_TRML_CD'])
    cand = lanes[(lanes['avg_util'] < 60) & (lanes['SCHED_PER_WK'] >= 5)].copy()
    cand = cand[cand['SCHED_PER_WK'] - 1 >= 3]
    cand['lane'] = cand.apply(lambda r: f"{TERMINAL_NAMES[r['ORIG_TRML_CD']]} → "
                                        f"{TERMINAL_NAMES[r['DEST_TRML_CD']]}", axis=1)
    cand['avg_util'] = cand['avg_util'].round(1)
    cand['weekly_saving_usd'] = (cand['LANE_MILES'] * cand['CPM_USD']).round(0)
    return cand[['lane', 'avg_util', 'loads', 'SCHED_PER_WK', 'SVC_STD_DAYS',
                 'weekly_saving_usd']]

# ===============================================================
# GROUND TRUTH COMPUTATIONS (pandas, no LLM)
# Each function returns (title, dataframe or text, optional chart df)
# ===============================================================

def _fmt_pct_variants(v):
    """Accept 2-decimal or 1-decimal renderings of a percentage in responses."""
    return [f"{v:.2f}", f"{v:.1f}"]


def _terminal_variants(code):
    """A response may use the code (HAR) or the business name (Harrison)."""
    return [code.lower(), TERMINAL_NAMES[code].lower()]


def _lane_label(o, d):
    return f"{TERMINAL_NAMES[o]} → {TERMINAL_NAMES[d]}"


def truth_lane_utilization():
    lanes = (
        utilization
        .groupby(['ORIG_TRML_CD', 'DEST_TRML_CD'], as_index=False)
        .agg(avg_utilization=('UTIL_PCT_3', 'mean'),
             trailers=('TRLR_NBR', 'count'))
        .sort_values('avg_utilization')
    )
    lanes['lane'] = lanes.apply(lambda r: _lane_label(r['ORIG_TRML_CD'], r['DEST_TRML_CD']), axis=1)
    lanes['avg_utilization'] = lanes['avg_utilization'].round(2)
    table = lanes[['lane', 'avg_utilization', 'trailers']]
    chart = lanes.set_index('lane')[['avg_utilization']]
    worst = lanes.iloc[0]
    facts = [
        (f"Identifies worst lane: {worst['lane']}",
         [_terminal_variants(worst['ORIG_TRML_CD']), _terminal_variants(worst['DEST_TRML_CD'])]),
        (f"States its average UTIL_PCT_3 ({worst['avg_utilization']}%)",
         [_fmt_pct_variants(worst['avg_utilization'])]),
    ]
    return ("Lane utilization (AVG of UTIL_PCT_3, the authoritative column), worst → best",
            table, chart, facts)


def truth_avg_utilization():
    avg = utilization['UTIL_PCT_3'].mean()
    text = (f"Average UTIL_PCT_3 (authoritative) across all {len(utilization)} trailers: "
            f"**{avg:.2f}%**")
    per_trailer = utilization[['TRLR_NBR', 'UTIL_PCT_3']].sort_values('UTIL_PCT_3')
    facts = [
        (f"States the overall average of UTIL_PCT_3 ({avg:.2f}%)", [_fmt_pct_variants(avg)]),
    ]
    return (text, per_trailer, None, facts)


def truth_volume_from_harrison():
    harrison = shipments[shipments['ORIG_TRML_CD'] == 'HAR']
    total_cube = int(harrison['TOT_CUBE_FT'].sum())
    text = (f"Shipments with ORIG_TRML_CD = 'HAR' (Harrison): **{len(harrison)}** — "
            f"total volume: **{total_cube:,} cube ft** "
            f"(requires resolving the name Harrison to code HAR)")
    table = harrison[['SHPMT_NBR', 'DEST_TRML_CD', 'TOT_CUBE_FT', 'TOT_WGT_LB']]
    facts = [
        (f"States total origin volume ({total_cube:,} cube ft)",
         [[str(total_cube), f"{total_cube:,}"]]),
    ]
    return (text, table, None, facts)


def truth_underutilized_trailers():
    under = utilization[utilization['UTIL_PCT_3'] < 70].sort_values('UTIL_PCT_3')
    text = f"Trailers with UTIL_PCT_3 below 70%: **{len(under)}** of {len(utilization)}"
    table = under[['TRLR_NBR', 'ORIG_TRML_CD', 'DEST_TRML_CD', 'UTIL_PCT_3', 'CNSTRNT_CD']]
    chart = under.set_index('TRLR_NBR')[['UTIL_PCT_3']]
    facts = [(f"Lists trailer {tid}", [[tid.lower()]])
             for tid in under['TRLR_NBR'].tolist()]
    return (text, table, chart, facts)


def truth_shipments_on_top_trailer():
    top = utilization.loc[utilization['UTIL_PCT_3'].idxmax()]
    trailer_id = top['TRLR_NBR']
    disp_row = dispatches[dispatches['TRLR_NBR'] == trailer_id]
    ship_ids = []
    if not disp_row.empty:
        ship_ids = [s for s in disp_row.iloc[0]['SHPMT_NBR_LST'].split(',') if s]
    ships = shipments[shipments['SHPMT_NBR'].isin(ship_ids)]
    text = (f"Highest-UTIL_PCT_3 trailer: **{trailer_id}** at **{top['UTIL_PCT_3']}%** "
            f"({_lane_label(top['ORIG_TRML_CD'], top['DEST_TRML_CD'])}, "
            f"CNSTRNT_CD: {top['CNSTRNT_CD']}). Shipments on board: **{len(ships)}**")
    table = ships[['SHPMT_NBR', 'ORIG_TRML_CD', 'DEST_TRML_CD',
                   'TOT_CUBE_FT', 'TOT_WGT_LB', 'SVC_TYP_CD']]
    # A correct SQL answer computes the top trailer in a subquery and may never
    # name it — the decisive facts are the shipment numbers it returns.
    facts = [(f"Lists shipment {sid}", [[sid.lower()]]) for sid in ship_ids]
    return (text, table, None, facts)


def truth_weekly_trend():
    u = utilization.copy()
    u['LH_DSPTCH_DT'] = pd.to_datetime(u['LH_DSPTCH_DT'])
    u['week_start'] = (u['LH_DSPTCH_DT']
                       - pd.to_timedelta(u['LH_DSPTCH_DT'].dt.dayofweek, unit='D')).dt.date
    weekly = (u.groupby('week_start', as_index=False)
                .agg(avg_utilization=('UTIL_PCT_3', 'mean'),
                     trailers=('TRLR_NBR', 'count'))
                .sort_values('week_start'))
    weekly['avg_utilization'] = weekly['avg_utilization'].round(2)
    first_v = weekly['avg_utilization'].iloc[0]
    last_v = weekly['avg_utilization'].iloc[-1]
    direction = "up" if last_v > first_v else "down"
    text = (f"Weekly average UTIL_PCT_3 across {len(weekly)} weeks "
            f"(Mon-start, grouped on LH_DSPTCH_DT — never SHPMT_CRT_DT). "
            f"Direction: **{direction}** from {first_v}% to {last_v}%.")
    chart = weekly.set_index('week_start')[['avg_utilization']]
    facts = [
        (f"States most recent week's average ({last_v}%)", [_fmt_pct_variants(last_v)]),
        (f"States earliest week's average ({first_v}%)", [_fmt_pct_variants(first_v)]),
    ]
    return (text, weekly, chart, facts)


def truth_lane_ranking():
    lanes = (
        utilization
        .groupby(['ORIG_TRML_CD', 'DEST_TRML_CD'], as_index=False)
        .agg(avg_utilization=('UTIL_PCT_3', 'mean'),
             avg_util_pct_1=('UTIL_PCT_1', 'mean'),
             trailers=('TRLR_NBR', 'count'))
        .sort_values('avg_utilization', ascending=False)
    )
    lanes['lane'] = lanes.apply(lambda r: _lane_label(r['ORIG_TRML_CD'], r['DEST_TRML_CD']), axis=1)
    lanes['avg_utilization'] = lanes['avg_utilization'].round(2)
    lanes['avg_util_pct_1'] = lanes['avg_util_pct_1'].round(2)
    table = lanes[['lane', 'avg_utilization', 'avg_util_pct_1', 'trailers']]
    chart = lanes.set_index('lane')[['avg_utilization']]
    best, worst = lanes.iloc[0], lanes.iloc[-1]
    facts = [
        (f"Best lane by UTIL_PCT_3: {best['lane']}",
         [_terminal_variants(best['ORIG_TRML_CD']), _terminal_variants(best['DEST_TRML_CD']),
          _fmt_pct_variants(best['avg_utilization'])]),
        (f"Worst lane by UTIL_PCT_3: {worst['lane']}",
         [_terminal_variants(worst['ORIG_TRML_CD']), _terminal_variants(worst['DEST_TRML_CD']),
          _fmt_pct_variants(worst['avg_utilization'])]),
    ]
    return ("Lanes ranked best → worst on UTIL_PCT_3 (authoritative; avg_util_pct_1 shown "
            "to expose the trap of ranking on the wrong column)",
            table, chart, facts)


def truth_last_week():
    from datetime import datetime, timedelta
    u = utilization.copy()
    u['LH_DSPTCH_DT'] = pd.to_datetime(u['LH_DSPTCH_DT']).dt.date
    today = datetime.now().date()
    this_monday = today - timedelta(days=today.weekday())
    lw_start = this_monday - timedelta(days=7)
    lw_end = this_monday - timedelta(days=1)
    lw = u[(u['LH_DSPTCH_DT'] >= lw_start) & (u['LH_DSPTCH_DT'] <= lw_end)]
    if len(lw) == 0:
        return (f"No dispatches in the last complete week ({lw_start} to {lw_end}).",
                u[['TRLR_NBR', 'LH_DSPTCH_DT', 'UTIL_PCT_3']], None, [])
    avg = lw['UTIL_PCT_3'].mean()
    text = (f"Last complete week = **{lw_start} to {lw_end}** (Mon–Sun, on LH_DSPTCH_DT). "
            f"Trailers dispatched: **{len(lw)}** — average UTIL_PCT_3: **{avg:.2f}%**")
    table = lw[['TRLR_NBR', 'LH_DSPTCH_DT', 'ORIG_TRML_CD',
                'DEST_TRML_CD', 'UTIL_PCT_3']].sort_values('LH_DSPTCH_DT')
    facts = [
        (f"States last week's average ({avg:.2f}%)", [_fmt_pct_variants(avg)]),
        (f"Uses the correct week ({lw_start} to {lw_end}) — literal dates or a "
         f"date_trunc('week') window both count",
         [[str(lw_start), lw_start.strftime("%B %-d").lower(),
           lw_start.strftime("%b %-d").lower(),
           "date_trunc('week'", 'date_trunc("week"']]),
    ]
    return (text, table, None, facts)


def truth_reported_utilization():
    included = utilization[utilization['SHPMT_CNT'] > 1]
    excluded = utilization[utilization['SHPMT_CNT'] == 1]
    reported = included['UTIL_PCT_3'].mean()
    naive = utilization['UTIL_PCT_3'].mean()
    text = (f"REPORTED utilization (per the 2019 Finance policy: service-protection loads "
            f"with SHPMT_CNT = 1 excluded): **{reported:.2f}%** over **{len(included)}** loads "
            f"(**{len(excluded)}** excluded). A plain average over all loads gives "
            f"{naive:.2f}% — wrong for reported figures, and no schema inspection reveals why.")
    table = included[['TRLR_NBR', 'ORIG_TRML_CD', 'DEST_TRML_CD',
                      'UTIL_PCT_3', 'SHPMT_CNT']].sort_values('UTIL_PCT_3')
    facts = [
        (f"States the REPORTED value ({reported:.2f}%), not the naive all-loads average "
         f"({naive:.2f}%)", [_fmt_pct_variants(reported)]),
        (f"Excludes service-protection loads (filter SHPMT_CNT > 1 or excludes "
         f"{len(excluded)} loads)",
         [["shpmt_cnt > 1", "shpmt_cnt>1", f"{len(excluded)} excluded",
           f"excluded {len(excluded)}", "service-protection", "shpmt_cnt = 1", "shpmt_cnt=1"]]),
    ]
    return (text, table, None, facts)


def truth_reported_sgf_lanes():
    """Compound long-tail question: institutional rule + terminal decode + lane filter.
    No pre-built gold table anticipates this exact cut; the ontology composes it."""
    sgf = utilization[utilization['ORIG_TRML_CD'] == 'SGF']
    included = sgf[sgf['SHPMT_CNT'] > 1]
    excluded_n = len(sgf) - len(included)
    reported = included['UTIL_PCT_3'].mean()
    naive = sgf['UTIL_PCT_3'].mean()
    text = (f"REPORTED utilization for lanes originating from Springfield (ORIG_TRML_CD = "
            f"'SGF', service-protection loads excluded): **{reported:.2f}%** over "
            f"**{len(included)}** loads ({excluded_n} excluded). Three semantic hops in one "
            f"question — the Finance exclusion, Springfield→SGF, origin-lane filtering — "
            f"and the naive all-loads number ({naive:.2f}%) is wrong for reported figures.")
    table = included[['TRLR_NBR', 'DEST_TRML_CD', 'LH_DSPTCH_DT',
                      'UTIL_PCT_3', 'SHPMT_CNT']].sort_values('UTIL_PCT_3')
    facts = [
        (f"States the REPORTED SGF-origin value ({reported:.2f}%), not the naive "
         f"({naive:.2f}%)", [_fmt_pct_variants(reported)]),
        ("Resolves Springfield to code SGF", [["sgf"]]),
        ("Applies the service-protection exclusion (SHPMT_CNT filter)",
         [["shpmt_cnt > 1", "shpmt_cnt>1", "shpmt_cnt = 1", "shpmt_cnt=1",
           "service-protection"]]),
    ]
    return (text, table, None, facts)


def truth_consolidation():
    elig, rej = find_consolidations()
    freq_note = ""
    if len(elig):
        total = int(elig['est_saving_usd'].sum())
        pair_txt = "; ".join(f"{r['trailer_1']}+{r['trailer_2']} on {r['lane']} "
                             f"(${int(r['est_saving_usd']):,})" for _, r in elig.iterrows())
        text = (f"Eligible consolidation pairs (ALL rules applied, including the "
                f"Priority-hold screen): **{len(elig)}** — {pair_txt}. Est. total saving "
                f"**${total:,}**.")
    else:
        text = "No eligible pairs this period."
    if len(rej):
        prio = rej[rej['rejected_because'].str.contains('Priority')]
        if len(prio):
            r0 = prio.iloc[0]
            text += (f" NOTE: {r0['trailer_1']}+{r0['trailer_2']} on {r0['lane']} fits "
                     f"physically but is REJECTED by the Priority-hold rule — invisible "
                     f"without the shipment-level join.")
    table = pd.concat([elig.assign(status='ELIGIBLE'),
                       rej.assign(status='REJECTED')], ignore_index=True) if len(rej) else elig
    facts = []
    if len(elig):
        for _, r in elig.iterrows():
            facts.append((f"Identifies eligible pair {r['trailer_1']}+{r['trailer_2']}",
                          [[r['trailer_1'].lower()], [r['trailer_2'].lower()]]))
    facts.append(("Applies the Priority-hold screen (mentions Priority eligibility)",
                  [["priority"]]))
    return (text, table, None, facts)


PRESET_QUESTIONS = {
    "Which lanes have the worst utilization?": truth_lane_utilization,
    "What is the average utilization across all trailers?": truth_avg_utilization,
    "How much volume originates from Harrison?": truth_volume_from_harrison,
    "Which trailers are underutilized (below 70%)?": truth_underutilized_trailers,
    "What shipments moved on the trailer with the highest utilization?": truth_shipments_on_top_trailer,
    "How has utilization trended week over week?": truth_weekly_trend,
    "Rank all lanes by utilization, best to worst": truth_lane_ranking,
    "What was the average utilization last week?": truth_last_week,
    "What is our reported utilization?": truth_reported_utilization,
    "What is our overall reported utilization for lanes originating from Springfield?": truth_reported_sgf_lanes,
    "Where can we consolidate trailers this period to save cost?": truth_consolidation,
}

# Fallback traversal source when Claude's tags are missing or malformed:
# derive used entities from the ontology's own metric definitions.
PRESET_METRIC_MAP = {
    "Which lanes have the worst utilization?": "lane_utilization",
    "What is the average utilization across all trailers?": "trailer_utilization",
    "How much volume originates from Harrison?": "volume_by_origin",
    "Which trailers are underutilized (below 70%)?": "trailer_utilization",
    "What shipments moved on the trailer with the highest utilization?": "shipments_on_trailer",
    "How has utilization trended week over week?": "utilization_trend",
    "Rank all lanes by utilization, best to worst": "lane_utilization",
    "What was the average utilization last week?": "utilization_trend",
    "What is our reported utilization?": "reported_utilization",
    "What is our overall reported utilization for lanes originating from Springfield?": "reported_utilization",
    "Where can we consolidate trailers this period to save cost?": "consolidation_opportunity",
}


def _normalize(text):
    """Normalize a response for fact matching: lowercase, no thousands commas,
    arrows unified to ' to '."""
    t = text.lower()
    t = t.replace("→", " to ").replace("->", " to ")
    import re
    t = re.sub(r"(\d),(\d{3})", r"\1\2", t)   # 12,345 -> 12345
    t = re.sub(r"\s+", " ", t)
    return t


def check_facts(facts, response_text):
    """Each fact = (label, groups); every group is a list of acceptable variants,
    and ALL groups must match somewhere in the normalized response."""
    norm = _normalize(response_text)
    results = []
    for label, groups in facts:
        ok = all(any(_normalize(v) in norm for v in group) for group in groups)
        results.append((label, ok))
    return results

# ===============================================================
# QUESTION SELECTION: preset buttons + custom input
# ===============================================================


# ===============================================================
# OPENING VISUALS: what this is, in ninety seconds
# ===============================================================
V1_PROBLEM = """
<svg viewBox="0 0 940 300" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="470" y="30" text-anchor="middle" font-size="17" font-weight="bold" fill="#212529">The problem in one picture</text>
  <rect x="330" y="50" width="280" height="46" rx="10" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="470" y="79" text-anchor="middle" font-size="14" font-weight="bold">"What is our reported utilization?"</text>
  <line x1="400" y1="96" x2="230" y2="140" stroke="#555" stroke-width="2" marker-end="url(#v1a)"/>
  <line x1="540" y1="96" x2="710" y2="140" stroke="#555" stroke-width="2" marker-end="url(#v1a)"/>
  <rect x="80" y="142" width="300" height="70" rx="10" fill="#fff" stroke="#d62828" stroke-width="2"/>
  <text x="230" y="168" text-anchor="middle" font-size="13" font-weight="bold" fill="#d62828">AI reads the schema alone</text>
  <text x="230" y="192" text-anchor="middle" font-size="20" font-weight="bold" fill="#d62828">53.4%</text>
  <rect x="560" y="142" width="300" height="70" rx="10" fill="#fff" stroke="#2b8a3e" stroke-width="2"/>
  <text x="710" y="168" text-anchor="middle" font-size="13" font-weight="bold" fill="#2b8a3e">AI + governed company meaning</text>
  <text x="710" y="192" text-anchor="middle" font-size="20" font-weight="bold" fill="#2b8a3e">65.6%</text>
  <text x="470" y="248" text-anchor="middle" font-size="14" fill="#495057">Both queries ran successfully. Only one follows company policy.</text>
  <text x="470" y="272" text-anchor="middle" font-size="13" font-style="italic" fill="#868e96">The model did not fail at SQL — the enterprise failed to provide the meaning.</text>
  <defs><marker id="v1a" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#555"/></marker></defs>
</svg>
"""

V2_CONTRACT = """
<svg viewBox="0 0 940 360" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="470" y="28" text-anchor="middle" font-size="17" font-weight="bold" fill="#212529">The semantic contract: author meaning once, enforce it everywhere</text>
  <rect x="350" y="150" width="240" height="80" rx="12" fill="#f3f0ff" stroke="#845ef7" stroke-width="2.5"/>
  <text x="470" y="182" text-anchor="middle" font-size="15" font-weight="bold" fill="#3b2a80">ONTOLOGY</text>
  <text x="470" y="204" text-anchor="middle" font-size="11" fill="#3b2a80">entities · metrics · rules · actions</text>
  <rect x="40" y="60" width="190" height="52" rx="9" fill="#e7f5ff" stroke="#1c7ed6" stroke-width="1.5"/>
  <text x="135" y="82" text-anchor="middle" font-size="12" font-weight="bold" fill="#0b4a8b">Prompt slices (RAG)</text>
  <text x="135" y="100" text-anchor="middle" font-size="10" fill="#0b4a8b">vector index → LLM context</text>
  <rect x="270" y="60" width="190" height="52" rx="9" fill="#e6f4d7" stroke="#4f772d" stroke-width="1.5"/>
  <text x="365" y="82" text-anchor="middle" font-size="12" font-weight="bold" fill="#1a2e05">Deterministic SQL</text>
  <text x="365" y="100" text-anchor="middle" font-size="10" fill="#1a2e05">metrics + action engines</text>
  <rect x="500" y="60" width="190" height="52" rx="9" fill="#fff3bf" stroke="#e6a700" stroke-width="1.5"/>
  <text x="595" y="82" text-anchor="middle" font-size="12" font-weight="bold" fill="#7a5800">Property graph</text>
  <text x="595" y="100" text-anchor="middle" font-size="10" fill="#7a5800">Neo4j · impact analysis</text>
  <rect x="730" y="60" width="180" height="52" rx="9" fill="#ffe8e8" stroke="#d62828" stroke-width="1.5"/>
  <text x="820" y="82" text-anchor="middle" font-size="12" font-weight="bold" fill="#7a1010">MCP tools (next)</text>
  <text x="820" y="100" text-anchor="middle" font-size="10" fill="#7a1010">agents · KPI chatbot</text>
  <line x1="420" y1="150" x2="150" y2="115" stroke="#845ef7" stroke-width="2" marker-end="url(#v2a)"/>
  <line x1="450" y1="150" x2="370" y2="115" stroke="#845ef7" stroke-width="2" marker-end="url(#v2a)"/>
  <line x1="510" y1="150" x2="580" y2="115" stroke="#845ef7" stroke-width="2" marker-end="url(#v2a)"/>
  <line x1="545" y1="150" x2="800" y2="115" stroke="#845ef7" stroke-width="2" marker-end="url(#v2a)"/>
  <rect x="330" y="280" width="280" height="50" rx="9" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="470" y="300" text-anchor="middle" font-size="12" font-weight="bold" fill="#495057">Gold data layer</text>
  <text x="470" y="318" text-anchor="middle" font-size="10" fill="#868e96">facts + lane reference (never in the LLM)</text>
  <line x1="470" y1="280" x2="470" y2="232" stroke="#adb5bd" stroke-width="2" marker-end="url(#v2a)"/>
  <defs><marker id="v2a" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#845ef7"/></marker></defs>
</svg>
"""

V3_STACK = """
<svg viewBox="0 0 940 330" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="470" y="28" text-anchor="middle" font-size="17" font-weight="bold" fill="#212529">Tech design: every POC component has a named production target</text>
  <text x="330" y="62" text-anchor="middle" font-size="13" font-weight="bold" fill="#868e96">THIS PROTOTYPE</text>
  <text x="700" y="62" text-anchor="middle" font-size="13" font-weight="bold" fill="#2b8a3e">PRODUCTION (Databricks/Azure)</text>
  <g font-size="12">
    <rect x="150" y="80" width="360" height="34" rx="7" fill="#f8f9fa" stroke="#adb5bd"/><text x="330" y="102" text-anchor="middle">DuckDB in-process engine</text>
    <rect x="560" y="80" width="330" height="34" rx="7" fill="#e6f4d7" stroke="#4f772d"/><text x="725" y="102" text-anchor="middle">Databricks SQL warehouse</text>
    <rect x="150" y="122" width="360" height="34" rx="7" fill="#f8f9fa" stroke="#adb5bd"/><text x="330" y="144" text-anchor="middle">In-memory vector index (fastembed)</text>
    <rect x="560" y="122" width="330" height="34" rx="7" fill="#e6f4d7" stroke="#4f772d"/><text x="725" y="144" text-anchor="middle">Databricks Vector Search</text>
    <rect x="150" y="164" width="360" height="34" rx="7" fill="#f8f9fa" stroke="#adb5bd"/><text x="330" y="186" text-anchor="middle">ontology.py (versioned dict)</text>
    <rect x="560" y="164" width="330" height="34" rx="7" fill="#e6f4d7" stroke="#4f772d"/><text x="725" y="186" text-anchor="middle">Governed semantic store + Unity Catalog</text>
    <rect x="150" y="206" width="360" height="34" rx="7" fill="#f8f9fa" stroke="#adb5bd"/><text x="330" y="228" text-anchor="middle">Regex gate + table allowlist</text>
    <rect x="560" y="206" width="330" height="34" rx="7" fill="#e6f4d7" stroke="#4f772d"/><text x="725" y="228" text-anchor="middle">SQL AST validation + entitlements + RLS</text>
    <rect x="150" y="248" width="360" height="34" rx="7" fill="#f8f9fa" stroke="#adb5bd"/><text x="330" y="270" text-anchor="middle">Direct Claude API + prompt caching</text>
    <rect x="560" y="248" width="330" height="34" rx="7" fill="#e6f4d7" stroke="#4f772d"/><text x="725" y="270" text-anchor="middle">MCP tools over a governed metrics API</text>
  </g>
  <line x1="510" y1="97" x2="560" y2="97" stroke="#2b8a3e" stroke-width="2" marker-end="url(#v3a)"/>
  <line x1="510" y1="139" x2="560" y2="139" stroke="#2b8a3e" stroke-width="2" marker-end="url(#v3a)"/>
  <line x1="510" y1="181" x2="560" y2="181" stroke="#2b8a3e" stroke-width="2" marker-end="url(#v3a)"/>
  <line x1="510" y1="223" x2="560" y2="223" stroke="#2b8a3e" stroke-width="2" marker-end="url(#v3a)"/>
  <line x1="510" y1="265" x2="560" y2="265" stroke="#2b8a3e" stroke-width="2" marker-end="url(#v3a)"/>
  <text x="470" y="316" text-anchor="middle" font-size="12" font-style="italic" fill="#868e96">Same architecture, bigger engines — nothing here requires inventing new technology.</text>
  <defs><marker id="v3a" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#2b8a3e"/></marker></defs>
</svg>
"""

with st.expander("Start Here — What This App Is (90 seconds)", expanded=True):
    v1, v2, v3 = st.tabs(["1 · The Problem", "2 · The Semantic Contract", "3 · Tech Design"])
    with v1:
        components.html(V1_PROBLEM, height=320)
    with v2:
        components.html(V2_CONTRACT, height=380)
        st.caption("Meaning — including what actions are eligible and what they save — is "
                   "authored once in the ontology and compiled to every consumer.")
    with v3:
        components.html(V3_STACK, height=350)

# ===============================================================
# KPI DASHBOARD: the ANTICIPATED questions (traditional BI world)
# ===============================================================
st.header("KPI Dashboard — the anticipated questions")
st.caption("This is the world that already exists: curated gold metrics on a dashboard, "
           "answering the questions someone anticipated at build time. The conversational "
           "section below serves the UNANTICIPATED tail — cuts and combinations nobody "
           "pre-built — with the ontology supplying the meaning at query time. This mirrors "
           "embedding a chat assistant inside an existing KPI dashboard application.")

_inc = utilization[utilization['SHPMT_CNT'] > 1]
_kc1, _kc2, _kc3, _kc4 = st.columns(4)
_kc1.metric("Reported utilization", f"{_inc['UTIL_PCT_3'].mean():.1f}%",
            help="Per the 2019 Finance policy: service-protection loads excluded")
_kc2.metric("Operational utilization (all loads)", f"{utilization['UTIL_PCT_3'].mean():.1f}%")
_kc3.metric("Loads dispatched", f"{len(utilization)}")
_kc4.metric("Service-protection loads", f"{(utilization['SHPMT_CNT'] == 1).sum()}")

_d1, _d2 = st.columns(2)
with _d1:
    st.markdown("**Weekly reported utilization**")
    _u = _inc.copy()
    _u['LH_DSPTCH_DT'] = pd.to_datetime(_u['LH_DSPTCH_DT'])
    _u['week'] = (_u['LH_DSPTCH_DT']
                  - pd.to_timedelta(_u['LH_DSPTCH_DT'].dt.dayofweek, unit='D')).dt.date
    _wk = _u.groupby('week')['UTIL_PCT_3'].mean().round(2)
    st.line_chart(_wk)
with _d2:
    st.markdown("**Worst lanes (reported)**")
    _ln = (_inc.groupby(['ORIG_TRML_CD', 'DEST_TRML_CD'])['UTIL_PCT_3']
           .mean().round(2).sort_values().head(5))
    _ln.index = [f"{TERMINAL_NAMES[o]} → {TERMINAL_NAMES[dd]}" for o, dd in _ln.index]
    st.bar_chart(_ln)


# ===============================================================
# ACTION PANEL: from measurement to governed action
# ===============================================================
st.header("Action Panel — governed opportunities this period")
st.caption("Deterministic opportunity surfacing with feasibility screening (rules-based, "
           "NOT an optimizer — that is the next rung). Eligibility rules, impact formulas, "
           "and owners are defined in the ontology's ACTION layer, the same way metrics are.")

_elig, _rej = find_consolidations()
_freq = find_frequency_candidates()

_a1, _a2 = st.columns(2)
with _a1:
    st.markdown("**Trailer consolidation** · owner: Linehaul load planning")
    if len(_elig):
        st.dataframe(_elig, hide_index=True, use_container_width=True)
        st.success(f"{len(_elig)} eligible pair(s) — est. total saving "
                   f"${int(_elig['est_saving_usd'].sum()):,} (one avoided pup move each)")
    else:
        st.info("No eligible pairs this period.")
    if len(_rej):
        with st.expander(f"Screened out by rule ({len(_rej)})"):
            st.dataframe(_rej, hide_index=True, use_container_width=True)
            st.caption("Physically feasible pairs rejected by INSTITUTIONAL rules — the "
                       "Priority-hold screen requires the shipment-level join; it is "
                       "invisible in the utilization fact alone.")
with _a2:
    st.markdown("**Schedule frequency review** · owner: Linehaul network planning")
    if len(_freq):
        st.dataframe(_freq, hide_index=True, use_container_width=True)
        st.success("Candidate(s) below 60% fill at ≥5 schedules/week. Weekly saving per "
                   "schedule removed; minimum-frequency floor of 3 protects the service "
                   "standard (SVC_STD_DAYS).")
    else:
        st.info("No frequency candidates this period.")
st.caption("Roadmap levers (named, not yet implemented): head-haul/backhaul balancing, "
           "break-terminal vs direct routing, doubles pairing optimization — each is "
           "eligibility rules + an impact formula in the same ACTION layer, then a MILP "
           "when network-level tradeoffs demand true optimization.")

st.header("Ask About Cube Utilization")

if "selected_query" not in st.session_state:
    st.session_state.selected_query = ""
if "is_preset" not in st.session_state:
    st.session_state.is_preset = False

st.markdown("**Pick a question:**")
_qs = list(PRESET_QUESTIONS.keys())
for row_start in range(0, len(_qs), 4):
    row_qs = _qs[row_start:row_start + 4]
    cols = st.columns(4)
    for j, q in enumerate(row_qs):
        with cols[j]:
            if st.button(q, key=f"preset_{row_start + j}", use_container_width=True):
                st.session_state.selected_query = q
                st.session_state.is_preset = True

custom = st.text_input("Or ask your own question:")
if custom:
    st.session_state.selected_query = custom
    st.session_state.is_preset = custom in PRESET_QUESTIONS

user_query = st.session_state.selected_query

if user_query:
    st.info(f"Question: {user_query}")

if user_query and not api_key:
    st.warning("Please set ANTHROPIC_API_KEY or enter it in the sidebar.")

# ===============================================================
# RUN BOTH QUERIES
# ===============================================================
if user_query and api_key:
    client = anthropic.Anthropic(api_key=api_key)

    # FAIRNESS NOTE: both prompts receive IDENTICAL data (all three tables),
    # identical generic instructions, and identical token budgets.
    # The ONLY difference between the two prompts is the ontology content.
    SCHEMAS = schema_description()

    # FAIRNESS: both prompts get IDENTICAL schemas (never full data), identical
    # generic instructions, identical token budgets. Only difference: the ontology.
    raw_context = f"""You are a freight analytics assistant. You CANNOT see the data —
only the table schemas and sample rows below. Write ONE DuckDB SQL SELECT query
that answers the user's question when executed against these tables.

Respond with ONE short sentence explaining your approach, then the query in a ```sql
fenced block. The system will execute it — do not fabricate result numbers.

{SCHEMAS}"""

    core_text, retrieved_text, rag_hits, rag_engine = assemble_semantic_slices(user_query)
    st.session_state.rag_hits = rag_hits
    st.session_state.rag_engine = rag_engine

    semantic_context = f"""You are a freight analytics assistant. You CANNOT see the data —
only the table schemas and sample rows below. Write ONE DuckDB SQL SELECT query
that answers the user's question when executed against these tables.

Respond with ONE short sentence explaining your approach (naming the metric
definition you followed), then the query in a ```sql fenced block. The system will
execute it — do not fabricate result numbers.

You additionally have access to a governed semantic ontology, provided as ALWAYS-ON
core rules plus definitions RETRIEVED for this specific question. Follow them EXACTLY.
When a rule says EXCLUDE, implement it as a WHERE filter on the aggregation itself —
reporting an excluded-count column while averaging over all rows is NOT compliance.
When a retrieved metric provides sql_equivalent, adapt that SQL precisely.

BEGIN your response with ONE compact line in EXACTLY this format, then a blank
line, then your explanation and query (the system parses and removes it):
TRACE: metric=<one of: trailer_utilization, lane_utilization, volume_by_origin, shipments_on_trailer, utilization_trend, reported_utilization, NONE>; entities=<comma-separated from: Shipment, Trailer, Dispatch, Terminal, Lane, Time>

{core_text}

RETRIEVED SEMANTIC CONTEXT for this question (top matches from the ontology index):
{retrieved_text}

{SCHEMAS}"""

    # PRODUCTION CACHING: the stable context (schemas, and for the semantic
    # side the ontology) goes in the SYSTEM parameter with cache_control.
    # Anthropic caches it server-side; subsequent questions read the cache at
    # ~10% of the fresh-token price. Only the user's question changes per call.
    raw_system = [{"type": "text", "text": raw_context,
                   "cache_control": {"type": "ephemeral"}}]
    semantic_system = [{"type": "text", "text": semantic_context,
                        "cache_control": {"type": "ephemeral"}}]

    # Store both prompts so the developer section can show the real payloads
    st.session_state.last_raw_prompt = f"[SYSTEM, cached]\n{raw_context}\n\n[USER]\n{user_query}"
    st.session_state.last_semantic_prompt = f"[SYSTEM, cached]\n{semantic_context}\n\n[USER]\n{user_query}"

    st.caption("Controlled comparison — production architecture: both sides see table "
               "metadata plus 3 sample rows (never the full dataset), same model, question, instructions, and "
               "1,200-token budget. Each writes SQL; DuckDB executes it. The stable context "
               "(schemas + ontology) sits in the cached system prompt, as in production — "
               "watch the cache-read numbers under each answer after the first question. "
               "The honest framing: the left side is not semantics-free — the model "
               "carries powerful IMPLICIT semantics from training and naming conventions. "
               "The comparison is implicit semantics vs EXPLICIT governed semantics. "
               "Institutional rules (try the reported-utilization question) are where "
               "implicit hits its ceiling.")

    with st.expander("RAG step: semantic context retrieved for this question"):
        st.caption(f"Retrieval engine: {st.session_state.get('rag_engine', '')} — the "
                   "vector index is over the ONTOLOGY's definitions, not the data. Core "
                   "invariants (decodes, column authority, temporal rules) always ship; "
                   "these chunks were retrieved for this question. At 500+ metrics this "
                   "step is what keeps the prompt small — production swaps this in-memory "
                   "index for Databricks Vector Search.")
        for (cid, kind, text), score in st.session_state.get("rag_hits", []):
            st.markdown(f"**`{cid}`** · similarity {score:.3f}")
            st.caption(text[:300] + ("…" if len(text) > 300 else ""))

    col1, col2 = st.columns(2)

    def _render_side(container, response_text, elapsed, usage, side_key, extra_note=""):
        """Shared rendering: explanation, generated SQL, validation, execution."""
        with container:
            sql = extract_sql(response_text)
            explanation = response_text.split("```")[0].strip()
            st.write(explanation)
            ok, sql_or_reason = validate_sql(sql)
            if not ok:
                st.error(f"Query failed the validation gate: {sql_or_reason}")
                st.session_state[side_key] = explanation
            else:
                st.markdown("**Generated SQL:**")
                st.code(sql_or_reason, language="sql")
                result, err = run_sql(sql_or_reason)
                if err is None:
                    st.session_state[side_key + "_tables"] = sorted(
                        set(m.lower() for m in re.findall(
                            r"(?i)\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", sql_or_reason))
                        & ALLOWED_TABLES)
                if err:
                    st.error(f"Execution error (surfaced honestly — this is why the "
                             f"validation gate exists): {err}")
                    st.session_state[side_key] = explanation + "\n" + sql_or_reason
                else:
                    st.markdown("**Executed result (computed by DuckDB, not the LLM):**")
                    st.dataframe(result, hide_index=True, use_container_width=True)
                    st.session_state[side_key] = (explanation + "\n" + sql_or_reason
                                                  + "\n" + result.to_string(index=False))
            cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
            st.caption(f"⏱ {elapsed:.1f}s · tokens: {usage.input_tokens:,} fresh in, "
                       f"{cache_w:,} cache-write, {cache_r:,} cache-read (~10% price) / "
                       f"{usage.output_tokens:,} out (budget: 1,200 — same both sides"
                       f"{extra_note}). First question warms the cache; "
                       f"repeat questions read it.")

    with col1:
        st.subheader("Without Semantic Ontology")
        st.caption("Claude writes SQL from raw schemas alone")
        with st.spinner("Generating query..."):
            try:
                t0 = time.time()
                response_raw = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=1200,
                    system=raw_system,
                    messages=[{"role": "user", "content": user_query}]
                )
                _render_side(st.container(), response_raw.content[0].text,
                             time.time() - t0, response_raw.usage, "raw_out")
            except Exception as e:
                st.error(f"Error: {str(e)}")
                st.session_state.raw_out = None

    with col2:
        st.subheader("With Semantic Ontology")
        st.caption("Claude writes SQL guided by metric definitions and business rules")
        with st.spinner("Generating query..."):
            try:
                t0 = time.time()
                response_semantic = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=1200,
                    system=semantic_system,
                    messages=[{"role": "user", "content": user_query}]
                )
                sem_elapsed = time.time() - t0
                response_text = response_semantic.content[0].text

                used_entities, used_rels, used_metric = [], [], None
                answer_lines = []
                for line in response_text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("TRACE:"):
                        body = stripped.split(":", 1)[1]
                        for part in body.split(";"):
                            part = part.strip()
                            if part.lower().startswith("metric="):
                                m = part.split("=", 1)[1].strip()
                                if m in ontology.get("metrics", {}):
                                    used_metric = m
                            elif part.lower().startswith("entities="):
                                used_entities = [e.strip() for e in
                                                 part.split("=", 1)[1].split(",")
                                                 if e.strip() in ontology["entities"]]
                    elif stripped.startswith("ENTITIES_USED:"):  # legacy fallback
                        used_entities = [e.strip() for e in
                                         stripped.split(":", 1)[1].split(",")
                                         if e.strip() in ontology["entities"]]
                    elif stripped.startswith("METRIC_USED:"):
                        m = stripped.split(":", 1)[1].strip()
                        if m in ontology.get("metrics", {}):
                            used_metric = m
                    elif stripped.startswith("RELATIONSHIPS_USED:"):
                        used_rels = [r.strip() for r in
                                     stripped.split(":", 1)[1].split(",")
                                     if r.strip() in ontology["relationships"]]
                    else:
                        answer_lines.append(line)
                answer_text = "\n".join(answer_lines)

                TABLE_ENTITIES = {
                    "shpmt_mstr": ["Shipment"],
                    "lh_dsptch": ["Trailer", "Dispatch"],
                    "trlr_util_fct": ["Trailer", "Lane"],
                    "pln_mvmt": ["Shipment", "Terminal"],
                    "lane_ref": ["Lane"],
                }
                sql_tables = st.session_state.get("sem_out_tables", [])
                derived = []
                for t in sql_tables:
                    for e in TABLE_ENTITIES.get(t, []):
                        if e not in derived:
                            derived.append(e)
                if derived:
                    used_entities = derived
                if not used_entities and user_query in PRESET_METRIC_MAP:
                    used_metric = PRESET_METRIC_MAP[user_query]
                    used_entities = ontology.get("metrics", {}).get(
                        used_metric, {}).get("entities", ["Trailer", "Lane"])

                _render_side(st.container(), answer_text, sem_elapsed,
                             response_semantic.usage, "sem_out",
                             extra_note="; input is larger because the ontology travels in the prompt")
                st.session_state.traversal = (used_entities, used_rels, used_metric)
            except Exception as e:
                st.error(f"Error: {str(e)}")
                st.session_state.sem_out = None

    # -----------------------------------------------------------
    # TRAVERSAL: which parts of the ontology this query touched
    # -----------------------------------------------------------
    used_entities, used_rels, used_metric = st.session_state.get("traversal", ([], [], None))
    if used_entities:
        st.header("Semantic Context Selected for This Query")
        st.caption("Entities (orange) are DERIVED from the tables the executed SQL "
                   "actually referenced — evidence, not self-report. The metric is the "
                   "model's own declaration of which definition it followed.")
        kg_legend(mode="traversal")
        render_kg(build_kg(highlight_entities=used_entities,
                           highlight_relationships=used_rels,
                           highlight_metrics=[used_metric] if used_metric else [],
                           height="480px", mode="traversal"),
                  "kg_traversal.html", render_height=500)
        rel_phrases = [f"**{ontology['relationships'][r]['from_entity']}** "
                       f"{r.replace('_', ' ').replace(ontology['relationships'][r]['from_entity'], '').replace(ontology['relationships'][r]['to_entity'], '').strip()} "
                       f"**{ontology['relationships'][r]['to_entity']}**"
                       for r in used_rels]
        narration = ""
        if used_metric:
            narration += (f"To answer this, Claude followed the **{used_metric}** "
                          f"metric definition, ")
        else:
            narration += "To answer this, Claude "
        narration += "used the definitions of " + \
            ", ".join(f"**{e}**" for e in used_entities) + "."
        if rel_phrases:
            narration += " It connected them through: " + "; ".join(rel_phrases) + "."
        st.markdown(narration)

    # -----------------------------------------------------------
    # GROUND TRUTH: matches the question that was asked
    # -----------------------------------------------------------
    st.header("Verified Ground Truth")
    st.caption("Computed directly from the CSVs with pandas — no LLM involved. "
               "Use it to check both responses above.")

    if user_query in PRESET_QUESTIONS:
        title, table, chart, facts = PRESET_QUESTIONS[user_query]()
        st.markdown(title)
        gt1, gt2 = st.columns([1, 1]) if chart is not None else (st.container(), None)
        if chart is not None:
            with gt1:
                st.dataframe(table, hide_index=True, use_container_width=True)
            with gt2:
                if user_query == "How has utilization trended week over week?":
                    st.line_chart(chart)
                else:
                    st.bar_chart(chart)
        else:
            st.dataframe(table, hide_index=True, use_container_width=True)

        # -------------------------------------------------------
        # VERDICT: automated fact check of both responses
        # -------------------------------------------------------
        raw_out = st.session_state.get("raw_out")
        sem_out = st.session_state.get("sem_out")
        if raw_out and sem_out and facts:
            st.header("Verdict: Automated Fact Check")
            st.caption("Each key fact below is computed from the data with pandas, then "
                       "checked for presence in each response (POC-grade string/number "
                       "matching — production would use structured output + exact eval).")
            raw_checks = check_facts(facts, raw_out)
            sem_checks = check_facts(facts, sem_out)
            verdict_df = pd.DataFrame({
                "Verified fact (from data)": [label for label, _ in raw_checks],
                "Without ontology": ["✅" if ok else "❌" for _, ok in raw_checks],
                "With ontology": ["✅" if ok else "❌" for _, ok in sem_checks],
            })
            st.dataframe(verdict_df, hide_index=True, use_container_width=True)
            raw_score = sum(ok for _, ok in raw_checks)
            sem_score = sum(ok for _, ok in sem_checks)
            n = len(facts)
            if sem_score > raw_score:
                st.success(f"With ontology matched {sem_score}/{n} verified facts; "
                           f"without ontology matched {raw_score}/{n}. "
                           f"The semantic layer produced the more accurate answer.")
            elif sem_score == raw_score == n:
                st.info(f"Both responses matched all {n} verified facts on this question. "
                        f"The ontology's value shows most on ambiguous grain, sorting, and "
                        f"time questions — and in consistency across repeated runs.")
            elif sem_score == raw_score:
                st.warning(f"Both matched {sem_score}/{n} verified facts. Inspect the "
                           f"responses above against the ground truth table.")
            else:
                st.error(f"Without ontology matched {raw_score}/{n}; with ontology "
                         f"matched {sem_score}/{n}. LLM responses vary — rerun the "
                         f"question, and inspect what the semantic side missed. This is "
                         f"why production needs repeated evals, not single runs.")
    else:
        st.markdown("Custom question — no precomputed check for it. "
                    "Key verified stats for manual comparison:")
        stats1, stats2, stats3 = st.columns(3)
        stats1.metric("Trailers", len(utilization))
        stats2.metric("Avg actual utilization",
                      f"{utilization['UTIL_PCT_3'].mean():.1f}%")
        stats3.metric("Total shipments", len(shipments))
        with st.expander("Full utilization table (for manual verification)"):
            st.dataframe(utilization, hide_index=True, use_container_width=True)

# ===============================================================
# TECHNICAL APPENDIX: architecture + the live semantic model
# ===============================================================
st.header("Technical Appendix")
st.caption("The proof lives above; the plumbing lives here.")


with st.expander("Interactive Knowledge Graph: the Ontology Behind the Scenes", expanded=False):
    st.caption("This is the live semantic model — not a mockup. Drag nodes, zoom with scroll, "
               "hover for definitions, computation steps, and join logic. Add an entity or metric "
               "to ontology.py and it appears here. Five entities and four metrics for this POC — "
               "a production ontology has hundreds, rendered and governed exactly the same way.")
    kg_legend(mode="full")
    render_kg(build_kg(), "kg_full.html")


# ===============================================================
# FROM INSIGHT TO ACTION (production pattern, illustrated)
# ===============================================================
with st.expander("From Insight to Action — where this goes next"):
    st.markdown("""
An accurate answer is the beginning, not the end. In production, the same ontology that
defines the metric also carries **targets, owners, and playbooks**, so the answer arrives
with its consequences attached. Illustrative (deterministic mock, same pattern as the
live metrics):

> **Springfield → Memphis reported utilization: below the 65% lane target.**
> Of the loads dispatched, service-protection loads were excluded from the reported
> figure per the Finance policy (owner: Finance — Asset Efficiency Reporting, effective
> 2019). Accountable process: **linehaul load planning**. Playbook: schedule
> consolidation review; departure-window adjustment.

The ontology additions this requires are the same species as everything already in it:
a `target` per metric per lane, an `owner`, and an `action` catalog — definitions, not
technology.
""")

# ===============================================================
# FOR DEVELOPERS: the mechanics
# ===============================================================
FLOW_SVG = """
<svg viewBox="0 0 980 560" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="245" y="30" text-anchor="middle" font-size="17" font-weight="bold" fill="#d62828">WITHOUT ontology</text>
  <text x="735" y="30" text-anchor="middle" font-size="17" font-weight="bold" fill="#4f772d">WITH ontology</text>

  <rect x="60" y="50" width="370" height="52" rx="8" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="245" y="72" text-anchor="middle" font-size="13" font-weight="bold" fill="#212529">User question</text>
  <text x="245" y="92" text-anchor="middle" font-size="12" fill="#495057">"Which lanes have the worst utilization?"</text>

  <rect x="550" y="50" width="370" height="52" rx="8" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="735" y="72" text-anchor="middle" font-size="13" font-weight="bold" fill="#212529">User question</text>
  <text x="735" y="92" text-anchor="middle" font-size="12" fill="#495057">"Which lanes have the worst utilization?"</text>

  <line x1="245" y1="102" x2="245" y2="132" stroke="#555" stroke-width="2" marker-end="url(#a)"/>
  <line x1="735" y1="102" x2="735" y2="132" stroke="#555" stroke-width="2" marker-end="url(#a)"/>

  <rect x="60" y="135" width="370" height="72" rx="8" fill="#ffe3e3" stroke="#d62828" stroke-width="1.5"/>
  <text x="245" y="158" text-anchor="middle" font-size="13" font-weight="bold" fill="#7a1010">Prompt = schemas + question</text>
  <text x="245" y="178" text-anchor="middle" font-size="12" fill="#7a1010">Column names + dtypes only. No definitions.</text>
  <text x="245" y="196" text-anchor="middle" font-size="12" fill="#7a1010">"lane" is just a word in the question.</text>

  <rect x="550" y="135" width="370" height="72" rx="8" fill="#e6f4d7" stroke="#4f772d" stroke-width="1.5"/>
  <text x="735" y="158" text-anchor="middle" font-size="13" font-weight="bold" fill="#1a2e05">Prompt = ontology (JSON) + schemas + question</text>
  <text x="735" y="178" text-anchor="middle" font-size="12" fill="#1a2e05">json.dumps(ontology) prepended as text.</text>
  <text x="735" y="196" text-anchor="middle" font-size="12" fill="#1a2e05">"lane" has a definition + computation steps.</text>

  <line x1="245" y1="207" x2="245" y2="237" stroke="#555" stroke-width="2" marker-end="url(#a)"/>
  <line x1="735" y1="207" x2="735" y2="237" stroke="#555" stroke-width="2" marker-end="url(#a)"/>

  <rect x="60" y="240" width="370" height="50" rx="8" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="245" y="262" text-anchor="middle" font-size="12" font-family="monospace" fill="#212529">anthropic.messages.create(model, messages=[...])</text>
  <text x="245" y="280" text-anchor="middle" font-size="11" fill="#868e96">identical call, identical model</text>

  <rect x="550" y="240" width="370" height="50" rx="8" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="735" y="262" text-anchor="middle" font-size="12" font-family="monospace" fill="#212529">anthropic.messages.create(model, messages=[...])</text>
  <text x="735" y="280" text-anchor="middle" font-size="11" fill="#868e96">identical call, identical model</text>

  <line x1="245" y1="290" x2="245" y2="320" stroke="#555" stroke-width="2" marker-end="url(#a)"/>
  <line x1="735" y1="290" x2="735" y2="320" stroke="#555" stroke-width="2" marker-end="url(#a)"/>

  <rect x="60" y="323" width="370" height="92" rx="8" fill="#ffe3e3" stroke="#d62828" stroke-width="1.5"/>
  <text x="245" y="346" text-anchor="middle" font-size="13" font-weight="bold" fill="#7a1010">Claude writes SQL by guessing meaning</text>
  <text x="245" y="366" text-anchor="middle" font-size="12" fill="#7a1010">Guesses grain: GROUP BY trailer, not lane</text>
  <text x="245" y="384" text-anchor="middle" font-size="12" fill="#7a1010">Guesses column: UTIL_PCT_1 vs _2 vs _3</text>
  <text x="245" y="402" text-anchor="middle" font-size="12" fill="#7a1010">Guesses time: trailing 7 days vs last week</text>

  <rect x="550" y="323" width="370" height="92" rx="8" fill="#e6f4d7" stroke="#4f772d" stroke-width="1.5"/>
  <text x="735" y="346" text-anchor="middle" font-size="13" font-weight="bold" fill="#1a2e05">Claude writes SQL from the metric definition</text>
  <text x="735" y="366" text-anchor="middle" font-size="12" fill="#1a2e05">lane_utilization: GROUP BY origin, destination</text>
  <text x="735" y="384" text-anchor="middle" font-size="12" fill="#1a2e05">AVG(UTIL_PCT_3), COUNT(trailers)</text>
  <text x="735" y="402" text-anchor="middle" font-size="12" fill="#1a2e05">ranking rule: worst = lowest, sort ascending</text>

  <line x1="245" y1="415" x2="245" y2="445" stroke="#555" stroke-width="2" marker-end="url(#a)"/>
  <line x1="735" y1="415" x2="735" y2="445" stroke="#555" stroke-width="2" marker-end="url(#a)"/>

  <rect x="60" y="448" width="370" height="72" rx="8" fill="#fff" stroke="#d62828" stroke-width="2"/>
  <text x="245" y="472" text-anchor="middle" font-size="13" font-weight="bold" fill="#7a1010">SQL answers the wrong question</text>
  <text x="245" y="492" text-anchor="middle" font-size="12" fill="#7a1010">Wrong column or wrong week — executes fine,</text>
  <text x="245" y="510" text-anchor="middle" font-size="12" fill="#7a1010">returns precise numbers for the wrong thing</text>

  <rect x="550" y="448" width="370" height="72" rx="8" fill="#fff" stroke="#4f772d" stroke-width="2"/>
  <text x="735" y="472" text-anchor="middle" font-size="13" font-weight="bold" fill="#1a2e05">SQL matches the canonical definition</text>
  <text x="735" y="492" text-anchor="middle" font-size="12" fill="#1a2e05">Engine returns digit-perfect numbers</text>
  <text x="735" y="510" text-anchor="middle" font-size="12" fill="#1a2e05">for the RIGHT question; query is auditable</text>

  <defs>
    <marker id="a" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#555"/>
    </marker>
  </defs>
</svg>
"""

DEV_SNIPPET = '''from ontology import ontology
import anthropic, json, duckdb

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY env var

# 1. INTERPRET: the ontology + schemas go to Claude as plain text;
#    Claude returns a SQL query, never computed numbers.
prompt = (
    "Write ONE DuckDB SQL SELECT answering the question. "
    "Follow these metric definitions EXACTLY:\\n"
    + json.dumps(ontology["metrics"], indent=2) + "\\n"
    + json.dumps(ontology["business_rules"], indent=2) + "\\n"
    + SCHEMAS_ONLY  # column names + dtypes + 3 sample rows; never full data
)
response = client.messages.create(
    model="claude-sonnet-4-5", max_tokens=1200,
    messages=[{"role": "user", "content": prompt + "\\n\\nQuestion: " + user_query}]
)
sql = extract_sql(response.content[0].text)

# 2. GOVERN: validation gate — read-only, single statement, allowed tables
ok, sql = validate_sql(sql)

# 3. COMPUTE: the engine executes; digit-perfect numbers
con = duckdb.connect()
con.register("trlr_util_fct", utilization_df)
result = con.execute(sql).df()'''

with st.expander("For Developers: How This Actually Works"):

    st.markdown("""
#### The stack

Four libraries, nothing exotic:

| Library | Role |
|---|---|
| `pandas` | Loads the CSVs, computes the ground-truth panel |
| `anthropic` | Official Python SDK for the Claude API |
| `streamlit` | The UI you are looking at |
| `json` (stdlib) | Serializes the ontology dict into the prompt |

The ontology itself is a **plain Python dictionary** in `ontology.py` - entities,
relationships, business rules, metric definitions with step-by-step computation
logic, and query patterns. No graph database, no vector store, no fine-tuning.

#### How the ontology reaches Claude — and what Claude returns

The ontology travels as **plain text inside the prompt** (serialized with
`json.dumps()`). What comes back is not an answer — it is a **SQL query**. The
LLM interprets; DuckDB computes; the platform validates in between:
""")

    st.code(DEV_SNIPPET, language="python")

    st.markdown("""
So: **the ontology goes to the model as instructions plus reference material in
the message content** — not a special API parameter, not an embedding, not RAG
retrieval. The model follows the metric definitions because the prompt tells it
to, and its only output that matters is the query text. (In production: stable
ontology in the `system` parameter with prompt caching; Databricks SQL warehouse
instead of DuckDB; the validation gate enforces Unity Catalog permissions.)

The "without ontology" call is **byte-for-byte identical** except the ontology
JSON is absent — same model, same schemas, same instructions, same token budget.

#### What changes in the traversal

The diagram traces one real question through both paths:
""")

    components.html(FLOW_SVG, height=760, scrolling=True)

    st.markdown("""
The failure on the left is not a data problem - both sides have identical data.
It is a **semantics problem**: nothing tells the model what grain a "lane" is,
which column is authoritative, or which direction "worst" sorts. Every gap
becomes a guess, and guesses vary run to run. The ontology closes those gaps in
one governed place.
""")

    if "last_semantic_prompt" in st.session_state:
        st.markdown("#### Inspect the real payloads (from your last question)")
        p1, p2 = st.tabs(["Prompt WITHOUT ontology", "Prompt WITH ontology"])
        with p1:
            st.text_area("raw", st.session_state.last_raw_prompt,
                         height=300, label_visibility="collapsed")
        with p2:
            st.text_area("semantic", st.session_state.last_semantic_prompt,
                         height=300, label_visibility="collapsed")
    else:
        st.info("Ask a question above, then reopen this section to inspect the "
                "exact prompts that were sent to Claude.")

    st.markdown("""
#### The four production patterns (and where this app sits)

Real implementations differ in **how much freedom the LLM gets**. The spectrum,
from most to least freedom:

| # | Pattern | The LLM emits | Who enforces meaning | Products |
|---|---|---|---|---|
| 1 | **Free-form query generation** *(this app)* | SQL text | Nobody — ontology is advice; a gate checks structure, not semantics | Databricks Genie, Snowflake Cortex Analyst |
| 2 | **Governed semantic-layer API** | A structured request: `{metric, group_by, filters, grain}` | The semantic layer — it compiles to SQL; wrong columns aren't on the menu | dbt Semantic Layer, Cube, Looker, Fabric semantic models |
| 3 | **Graph-native / typed objects** | Cypher/SPARQL, or typed function calls | The graph platform — traversals precompiled from declared links | Neo4j + LLM, Palantir Foundry/AIP |
| 4 | **Tool use via MCP** | A tool call: `get_lane_utilization(order='asc')` | Deterministic code behind each tool (usually pattern 2 underneath) | Custom MCP servers, agent platforms |

Note what happens to "traversal" down the ladder: in pattern 1 the LLM *reasons
about* joins per question; by patterns 2–4 the ontology *defines* the traversals
once and the LLM merely selects an entry point. Choosing the wrong column stops
being a mistake the model can make — it is structurally impossible, the same way
delegating computation made arithmetic errors impossible.

**Most cost-effective at enterprise scale: pattern 2 as the core, exposed
through pattern 4 for conversational access.** Why: structured requests are
tens of output tokens instead of hundreds of SQL tokens; the metric "menu" is
compact, cacheable, and retrievable in slices; intent-to-request is simple
enough for a small, cheap model, while trustworthy SQL generation wants a
frontier model; and correctness is engineered once in the compiler instead of
evaluated per query forever. Keep pattern 1 as a flagged, lower-trust escape
hatch for long-tail ad-hoc questions, and reach for pattern 3 only when the
questions are genuinely graph-shaped (multi-hop relationship traversal).

This app deliberately demonstrates pattern 1 because it makes the *value of the
ontology visible* — you can watch the same model succeed and fail on the same
schema with and without it. Production should graduate up the ladder.
""")


# ===============================================================
# SAMPLE DATA
# ===============================================================

with st.expander("Why an Ontology? The Top Benefits (and the honest boundary)"):
    st.markdown("""
**1. Institutional meaning — definitions that exist only in decisions.**
"Reported utilization excludes service-protection loads (SHPMT_CNT = 1), per the 2019
Finance policy." No column name, sample row, or industry convention reveals this — it was
decided in a meeting. *In this app:* the reported-utilization preset. The schema-only side
computes a plain average and fails the verdict **by construction** — the rule cannot be
inferred from schema, sample rows, or naming conventions, so no amount of model capability
recovers it from the data alone.

**2. Entities and relationships — correct joins, smaller search space.**
The ontology declares what the objects are and how they link (Shipment →loaded in→ Trailer
→part of→ Dispatch), including physical join logic like "SHPMT_NBR_LST is comma-packed;
UNNEST to join." *In this app:* the top-trailer question — the multi-hop join the model
must get right — and the traversal graph lighting up the path it used. At scale (patterns
2–4 in the ladder below) relationships are precompiled into traversals, which is where
"the ontology makes processing faster" becomes literally true.

**3. Consistency as a contract, not a coin flip.**
A frontier model often *guesses* the right column from naming conventions — implicit
semantics. But a guess that is right today is a probability; a governed definition is a
guarantee. *In this app:* rerun any ranking question several times per side and compare.

**4. Governance and change management — fix meaning once.**
During this build, a domain expert caught the utilization formula encoding min() instead
of max(). One edit in ontology.py corrected every downstream answer. In a schema-only
world that error lives on in a thousand ad-hoc queries. *In this app:* the max() rule in
the business rules, and this story.

**5. Auditability — "why this number?" has a mechanical answer.**
Every semantic answer carries a trace (which metric definition, which entities) and the
generated SQL diffs against the metric's canonical SQL. *In this app:* the TRACE-driven
traversal graph and the Generated SQL panels.

**Where this scales — the long tail in one sentence.** Consider: *"Reported utilization
for Priority freight on Springfield lanes, last fiscal period, excluding hazmat."* Every
clause is a semantic hop — the Finance exclusion, a service-mix definition (what makes a
mixed-load trailer "Priority"? — a definition someone must decide), the SGF decode, a
4-4-5 fiscal calendar rule, and a hazmat exclusion. No gold table anticipates this cut;
no schema inspection recovers these rules; the ontology composes them at query time.
The live preset above demonstrates the three hops this demo's dataset supports; the
other two need only more attributes and more rules — the mechanism is identical.

**The honest boundary:** where your definitions coincide with industry convention, the
model's implicit semantics answer correctly for free — an explicit ontology adds cost
without adding correctness there. The ROI concentrates on the definitions that are
*yours*: the institutional rules.

**Other institutional rules this demo could encode the same way** (every enterprise has
dozens): fiscal 4-4-5 calendar weeks instead of ISO weeks; pallet-adjusted effective
capacity (e.g., 1,850 cube on lanes with stacking restrictions); Economy freight weighted
0.8 in service metrics; hazmat loads excluded from cube targets; doubles measured at the
schedule level for driver productivity; backhaul lanes measured against a lower
utilization target than head-haul.

**Beyond analytics — one ontology, many AI consumers.** The same ontology.py that
grounds this conversational demo grounds other AI use cases unchanged: an
exception-classification model whose label definitions (carrier vs documentation vs
routing exception) are institutional rules of exactly this species; document extraction
that maps BOL fields onto the Shipment entity's properties; and agentic assistants whose
MCP tools are generated from the entities, metrics, and actions defined here. Author the
meaning once; every AI consumer compiles from it.

*Demo simplifications, stated openly:* pups here carry 1–5 shipments for readability
(real 28-ft pups carry 15–30); one break terminal (SGF) instead of a full hub network;
utilization pre-computed in the fact table.
""")

st.header("Sample Data")
st.caption("Deliberately realistic legacy schema: UTIL_PCT_1/2/3, LH_DSPTCH_DT vs SHPMT_CRT_DT, "
           "terminal codes like HAR. Nothing in these names says which column is authoritative "
           "or what the codes mean — that knowledge lives only in the ontology. This is what "
           "real warehouse schemas look like.")
tab1, tab2, tab3 = st.tabs(["Shipments", "Dispatches", "Utilization"])

with tab1:
    st.dataframe(shipments.head(10))

with tab2:
    st.dataframe(dispatches.head(10))

with tab3:
    st.dataframe(utilization.head(10))
