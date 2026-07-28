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

# Fresh sessions open at the TOP (browsers otherwise restore the previous
# scroll position on refresh, dropping returning users mid-page).
if (not st.session_state.get("chat_turns")
        and not st.session_state.get("exec_cache")
        and not st.session_state.get("_top_snapped")):
    st.session_state["_top_snapped"] = True
    components.html(
        "<script>try{window.parent.scrollTo({top:0,left:0,behavior:'instant'});}"
        "catch(e){window.parent.scrollTo(0,0);}</script>", height=0)


st.title("Cube Utilization Semantic Ontology POC")
st.markdown("How governed semantics powers trusted analytics, proactive intelligence, and operational decision support")


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
# ---- Model selection: uncomment exactly ONE default. Both sides always use
# the same model; every answer is captioned with the model id. The
# ANTHROPIC_MODEL env var / Streamlit secret overrides without code changes.
_DEFAULT_MODEL = "claude-sonnet-5"     # current Sonnet (Jun 2026): near-Opus, most agentic, intro-priced
# _DEFAULT_MODEL = "claude-sonnet-4-6" # previous Sonnet — stable fallback
# _DEFAULT_MODEL = "claude-opus-4-8"   # deeper reasoning; slower, costlier
# _DEFAULT_MODEL = "claude-fable-5"    # most capable; highest latency (thinking)
MODEL_ID = os.environ.get("ANTHROPIC_MODEL", _DEFAULT_MODEL)

# ---- Indicative API prices, USD per MILLION tokens. EDIT to match current
# rates (verify at anthropic.com/pricing — cache writes bill at ~1.25x input,
# cache reads at ~0.1x input; thinking tokens bill as output).
PRICING = {
    # sonnet-5: INTRO pricing through 2026-08-31 ($2/$10), then $3/$15 — update after!
    "claude-sonnet-5":   {"in": 2.00, "out": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-opus-4-8":   {"in": 5.00, "out": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-fable-5":    {"in": 10.00, "out": 50.00, "cache_write": 12.50, "cache_read": 1.00},
}

def side_cost_usd(u):
    """Estimated cost of one call from its usage dict, at PRICING rates."""
    p = PRICING.get(MODEL_ID, PRICING["claude-sonnet-5"])
    return (u.get("input_tokens", 0) * p["in"]
            + u.get("cache_creation_input_tokens", 0) * p["cache_write"]
            + u.get("cache_read_input_tokens", 0) * p["cache_read"]
            + u.get("output_tokens", 0) * p["out"]) / 1_000_000
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
    # SQL constructs that legally follow FROM/JOIN but are NOT tables — never
    # treat these keywords as table names (the actual source is parenthesized
    # after them). Production replaces this regex gate with AST parsing.
    SQL_NON_TABLES = {"lateral", "unnest", "values", "select", "generate_series", "range"}
    # SECURITY: DuckDB table functions that read files or external sources are
    # FORBIDDEN anywhere in the query — they can expose application files/secrets.
    FORBIDDEN_FUNCTIONS = {"read_csv", "read_csv_auto", "read_json", "read_json_auto",
                           "read_parquet", "parquet_scan", "sqlite_scan",
                           "postgres_scan", "httpfs", "glob", "read_text",
                           "read_blob", "load_extension", "install"}
    for fn in FORBIDDEN_FUNCTIONS:
        if re.search(r"(?i)\b" + fn + r"\s*\(", scrub):
            return False, f"Forbidden function: {fn} (file/external access is blocked)."
    unknown = refs - ALLOWED_TABLES - ctes - SQL_NON_TABLES
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





# ===============================================================
# DELEGATED COMPUTATION: SQL extraction, validation gate, execution
# The LLM interprets and writes the query; DuckDB computes.
# ===============================================================
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
  <text x="485" y="172" text-anchor="middle" font-size="12" fill="#0b4a8b">POC: retrieves question-relevant slices + always-on core rules from an in-memory index</text>
  <line x1="470" y1="188" x2="470" y2="214" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 3 -->
  <rect x="250" y="216" width="440" height="60" rx="9" fill="#fff3bf" stroke="#e6a700" stroke-width="1.5"/>
  <circle cx="275" cy="246" r="13" fill="#e6a700"/><text x="275" y="251" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">3</text>
  <text x="485" y="240" text-anchor="middle" font-size="14" font-weight="bold" fill="#7a5800">LLM interprets — generates the SQL query</text>
  <text x="485" y="260" text-anchor="middle" font-size="12" fill="#7a5800">sees column names/dtypes + 3 sample rows, never the full dataset; never does arithmetic</text>
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
  <text x="485" y="436" text-anchor="middle" font-size="12" fill="#14521f">deterministic arithmetic — LLM calc errors eliminated; query/semantic errors evaluated separately</text>
  <line x1="470" y1="452" x2="470" y2="478" stroke="#555" stroke-width="2" marker-end="url(#pa)"/>

  <!-- 6 -->
  <rect x="250" y="480" width="440" height="60" rx="9" fill="#f3f0ff" stroke="#845ef7" stroke-width="1.5"/>
  <circle cx="275" cy="510" r="13" fill="#845ef7"/><text x="275" y="515" text-anchor="middle" font-size="13" fill="#fff" font-weight="bold">6</text>
  <text x="485" y="504" text-anchor="middle" font-size="14" font-weight="bold" fill="#3b2a80">App presents result + definition + trace (prod: optional LLM narration)</text>
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
  <text x="230" y="392" text-anchor="middle" font-size="12" fill="#7a1010">provenance (owner/policy/date) retrieved with each rule</text>
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
    "origin_utilization": (300, -360),
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
        prov = r.get("provenance", {})
        chunks.append((f"rule:{name}", "rule",
                       f"BUSINESS RULE {name} | {r.get('rule','')} "
                       f"{r.get('formula','')} | applies: {r.get('applies_when','')} "
                       f"| parameters: {json.dumps(r.get('parameters', {}))} "
                       f"| owner: {prov.get('owner','')} | policy: {prov.get('policy','')} "
                       f"| effective: {prov.get('effective','')}"))
    for name, rel in ontology.get("relationships", {}).items():
        chunks.append((f"relationship:{name}", "relationship",
                       f"RELATIONSHIP {name} | {rel.get('from_entity','')} -> "
                       f"{rel.get('to_entity','')} | {rel.get('description','')} | "
                       f"cardinality: {rel.get('cardinality','')} | join logic: "
                       f"{rel.get('join_logic','')}"))
    for i, qp in enumerate(ontology.get("query_patterns", [])):
        chunks.append((f"pattern:{i}:{qp.get('metric','')}", "pattern",
                       f"QUESTION PATTERN: {qp.get('question','')} -> metric "
                       f"{qp.get('metric','')} | {qp.get('answer_shape','')}"))
    for name, a in ontology.get("actions", {}).items():
        text = (f"ACTION {name} | {a.get('description','')} | eligibility: "
                + " ".join(a.get("eligibility", []))
                + f" | parameters: {json.dumps(a.get('parameters', {}))}"
                + f" | impact: {a.get('impact_formula','')} | owner: {a.get('owner','')}"
                + f" | provenance: {json.dumps(a.get('provenance', {}))}"
                + f" | sql: {a.get('sql_equivalent','')}")
        chunks.append((f"action:{name}", "action", text))
    for name, p in ontology.get("playbooks", {}).items():
        text = (f"PLAYBOOK {name} | applies to: {' '.join(p.get('question_shapes', []))} "
                f"| method: " + " ".join(p.get("method", []))
                + f" | owner: {p.get('owner','')}")
        chunks.append((f"playbook:{name}", "playbook", text))
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
    # GRAPH EXPANSION: similarity finds the entry point; explicit references
    # pull mandatory dependencies (pattern -> metric/action -> governing rule)
    have = {cid for (cid, _kind, _t), _s in hits}
    all_chunks = {c[0]: c for c in build_ontology_corpus()}
    def _force(cid):
        if cid in all_chunks and cid not in have:
            hits.append((all_chunks[cid], -1.0))  # -1 marks "dependency-included"
            have.add(cid)
    _top_pattern = next((cid for (cid, _k, _t), _s in hits
                         if cid.startswith("pattern:")), None)
    if _top_pattern:
        ref = _top_pattern.split(":")[-1].replace(" (ACTION)", "")
        _force(f"metric:{ref}")
        _force(f"action:{ref}")
        if ref == "reported_utilization":
            _force("rule:reported_utilization_exclusion")
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
            P = ontology["actions"]["consolidation_opportunity"]["parameters"]
            rec = {'trailer_1': a['TRLR_NBR'], 'trailer_2': b['TRLR_NBR'],
                   'lane': f"{TERMINAL_NAMES[o]} → {TERMINAL_NAMES[dd]}",
                   'date': dt, 'combined_cube': int(cube), 'combined_wgt': int(wgt),
                   'est_saving_usd': saving}
            if cube > P["max_combined_cube"] or wgt > P["max_combined_weight_lb"]:
                rec['rejected_because'] = 'exceeds pup capacity'
                rejected.append(rec); continue
            if (P.get("require_different_dispatch")
                    and a['DSPTCH_NBR'] == b['DSPTCH_NBR']):
                rec['rejected_because'] = 'same dispatch — pups already share a driver; no move saved'
                rejected.append(rec); continue
            if (a['SHPMT_CNT'] < P["min_shipments_per_load"]
                    or b['SHPMT_CNT'] < P["min_shipments_per_load"]):
                rec['rejected_because'] = 'service-protection load (never held)'
                rejected.append(rec); continue
            if any(svc in (_trailer_services(a['TRLR_NBR']) | _trailer_services(b['TRLR_NBR']))
                   for svc in P["excluded_service_types"]):
                rec['rejected_because'] = 'Priority-hold rule (Priority freight never held)'
                rejected.append(rec); continue
            eligible.append(rec)
    return pd.DataFrame(eligible), pd.DataFrame(rejected)


def capacity_flags(u):
    """Governed capacity status (thresholds from the ontology) — distinct from
    the dominant-constraint code CNSTRNT_CD."""
    T = ontology["business_rules"]["capacity_status"]["parameters"]
    weighed_out = u['UTIL_PCT_2'] >= T["weighed_out_min_pct"]
    cubed_out = u['UTIL_PCT_1'] >= T["cubed_out_min_pct"]
    return weighed_out, cubed_out


def utilization_diagnostic(orig=None):
    """Root-cause decomposition + greedy re-pack counterfactual (planning
    estimate — not an optimizer; departure windows/doors/hours unmodeled).
    orig: optional terminal code to scope the analysis (e.g., 'HAR')."""
    u = utilization.copy()
    if orig:
        u = u[u['ORIG_TRML_CD'] == orig]
    CP = ontology["actions"]["consolidation_opportunity"]["parameters"]
    if len(u) == 0:
        return {'current': 0, 'achievable': 0, 'uplift': 0, 'moves': pd.DataFrame(),
                'total_usd': 0, 'weighed_out_n': 0, 'cubed_out_n': 0,
                'capacity_constrained_n': 0, 'weight_dominant_n': 0,
                'weighed_out_avg_cube': 0, 'service_prot_n': 0, 'total_n': 0}
    _wo, _co = capacity_flags(u)
    weighed_out = u[_wo]
    cubed_out = u[_co]
    capacity_constrained = u[_wo | _co]
    service_prot = u[u['SHPMT_CNT'] == 1]
    current_avg = u['UTIL_PCT_3'].mean()

    # greedy first-fit-decreasing re-pack per lane-day over ELIGIBLE loads
    merged_rows, moves = [], []
    for (o, dd, dt), grp in u.groupby(['ORIG_TRML_CD', 'DEST_TRML_CD', 'LH_DSPTCH_DT']):
        _gwo, _gco = capacity_flags(grp)
        elig = grp[(grp['SHPMT_CNT'] >= CP["min_shipments_per_load"])
                   & ~(_gwo | _gco)].copy()  # never add freight to capacity-constrained loads
        if len(elig):
            mask = elig['TRLR_NBR'].map(
                lambda t: not any(s in _trailer_services(t)
                                  for s in CP["excluded_service_types"])).astype(bool)
            elig = elig[mask]
        inelig = grp.drop(elig.index)
        bins = []
        for _, r in elig.sort_values('LD_CUBE_FT', ascending=False).iterrows():
            placed = False
            for b in bins:
                if (b['cube'] + r['LD_CUBE_FT'] <= CP["max_combined_cube"]
                        and b['wgt'] + r['LD_WGT_LB'] <= CP["max_combined_weight_lb"]):
                    b['cube'] += r['LD_CUBE_FT']; b['wgt'] += r['LD_WGT_LB']
                    b['members'].append(r['TRLR_NBR']); placed = True
                    break
            if not placed:
                bins.append({'cube': r['LD_CUBE_FT'], 'wgt': r['LD_WGT_LB'],
                             'members': [r['TRLR_NBR']]})
        # a merge only saves a MOVE if the merged pups came from different
        # dispatches (same-dispatch pups already share a driver)
        disp_of = dict(zip(grp['TRLR_NBR'], grp['DSPTCH_NBR']))
        saved = 0
        for b in bins:
            if len(b['members']) > 1:
                saved += len(set(disp_of[t] for t in b['members'])) - 1
        if saved > 0:
            lane = lane_ref[(lane_ref['ORIG_TRML_CD'] == o) & (lane_ref['DEST_TRML_CD'] == dd)]
            usd = round(float(lane['LANE_MILES'].iloc[0] * lane['CPM_USD'].iloc[0]) * saved, 0) if not lane.empty else 0
            moves.append({'lane': f"{TERMINAL_NAMES[o]} → {TERMINAL_NAMES[dd]}",
                          'date': dt, 'ran': len(grp), 'needed': len(bins) + len(inelig),
                          'moves_saved': saved, 'est_saving_usd': usd,
                          'merge': "; ".join("+".join(b['members']) for b in bins if len(b['members']) > 1)})
        for b in bins:
            merged_rows.append(max(round(b['cube'] / 2000 * 100, 1),
                                   round(b['wgt'] / 20000 * 100, 1)))
        for _, r in inelig.iterrows():
            merged_rows.append(r['UTIL_PCT_3'])
    achievable_avg = sum(merged_rows) / len(merged_rows) if merged_rows else current_avg
    return {
        'current': round(current_avg, 1), 'achievable': round(achievable_avg, 1),
        'uplift': round(achievable_avg - current_avg, 1),
        'moves': pd.DataFrame(moves),
        'total_usd': int(sum(m['est_saving_usd'] for m in moves)),
        'weighed_out_n': len(weighed_out), 'cubed_out_n': len(cubed_out),
        'capacity_constrained_n': len(capacity_constrained),
        'weighed_out_avg_cube': round(weighed_out['UTIL_PCT_1'].mean(), 1) if len(weighed_out) else 0,
        'weight_dominant_n': int((u['CNSTRNT_CD'] == 'W').sum()),
        'service_prot_n': len(service_prot), 'total_n': len(u),
    }


def find_frequency_candidates():
    """Low-fill high-frequency lanes, floored at 3 schedules to protect service."""
    lanes = (utilization.groupby(['ORIG_TRML_CD', 'DEST_TRML_CD'], as_index=False)
             .agg(avg_util=('UTIL_PCT_3', 'mean'), loads=('TRLR_NBR', 'count')))
    lanes = lanes.merge(lane_ref, on=['ORIG_TRML_CD', 'DEST_TRML_CD'])
    FP = ontology["actions"]["frequency_rationalization"]["parameters"]
    uw = utilization.copy()
    uw['LH_DSPTCH_DT'] = pd.to_datetime(uw['LH_DSPTCH_DT'])
    uw['wk'] = uw['LH_DSPTCH_DT'].dt.isocalendar().week
    weeks = (uw.groupby(['ORIG_TRML_CD', 'DEST_TRML_CD'])['wk']
             .nunique().reset_index(name='observed_weeks'))
    lanes = lanes.merge(weeks, on=['ORIG_TRML_CD', 'DEST_TRML_CD'])
    cand = lanes[(lanes['avg_util'] < FP["max_avg_util_pct"])
                 & (lanes['SCHED_PER_WK'] >= FP["min_sched_per_wk"])
                 & (lanes['loads'] >= FP["min_load_count"])
                 & (lanes['observed_weeks'] >= FP["min_observed_weeks"])].copy()
    cand = cand[cand['SCHED_PER_WK'] - 1 >= FP["min_frequency_floor"]]
    if cand.empty:
        return pd.DataFrame(columns=['lane', 'avg_util', 'loads', 'observed_weeks',
                                     'SCHED_PER_WK', 'SVC_STD_DAYS',
                                     'weekly_saving_usd', 'dow_evidence'])
    # DOW evidence (governed): name specific weak days only when that day's
    # sample clears min_dow_load_count; otherwise evidence is lane-grain only
    GN = ontology["business_rules"]["recommendation_granularity"]["parameters"]
    uw2 = utilization.copy()
    uw2['LH_DSPTCH_DT'] = pd.to_datetime(uw2['LH_DSPTCH_DT'])
    uw2['dow'] = uw2['LH_DSPTCH_DT'].dt.day_name()
    def _dow_evidence(row):
        g = uw2[(uw2['ORIG_TRML_CD'] == row['ORIG_TRML_CD'])
                & (uw2['DEST_TRML_CD'] == row['DEST_TRML_CD'])]
        prof = g.groupby('dow').agg(n=('TRLR_NBR', 'count'),
                                    fill=('UTIL_PCT_3', 'mean'))
        weak = prof[(prof['n'] >= GN["min_dow_load_count"])
                    & (prof['fill'] < FP["max_avg_util_pct"])]
        if len(weak):
            return "; ".join(f"{d} ({int(r['n'])} loads @ {r['fill']:.0f}%)"
                             for d, r in weak.iterrows())
        return "insufficient DOW samples — lane-grain evidence only"
    if len(cand):
        cand['dow_evidence'] = cand.apply(_dow_evidence, axis=1)
    else:
        cand['dow_evidence'] = pd.Series(dtype=str)
    cand['lane'] = cand.apply(lambda r: f"{TERMINAL_NAMES[r['ORIG_TRML_CD']]} → "
                                        f"{TERMINAL_NAMES[r['DEST_TRML_CD']]}", axis=1)
    cand['avg_util'] = cand['avg_util'].round(1)
    cand['weekly_saving_usd'] = (cand['LANE_MILES'] * cand['CPM_USD']).round(0)
    return cand[['lane', 'avg_util', 'loads', 'observed_weeks', 'SCHED_PER_WK',
                 'SVC_STD_DAYS', 'weekly_saving_usd', 'dow_evidence']]

# ===============================================================
# CHAT SESSION: Claude is stateless — the orchestration layer (this
# app, via session_state) owns conversation memory and replays the
# last turns with each request. Same responsibility split as the
# production five-layer chatbot design.
# ===============================================================
MAX_TURNS_IN_CONTEXT = 4


def response_text_of(resp):
    """Join the TEXT blocks of an API response. Thinking-capable models
    (e.g., Fable) return thinking blocks first — never index content[0]."""
    parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    return "\n".join(parts) if parts else ""


def _turn_summary(text):
    """Compact prior-turn answer: explanation + SQL, capped, TRACE stripped."""
    lines = [l for l in text.splitlines() if not l.strip().startswith("TRACE:")]
    return "\n".join(lines)[:700]


def build_messages(side, user_query):
    msgs = []
    for turn in st.session_state.get("chat_turns", [])[-MAX_TURNS_IN_CONTEXT:]:
        msgs.append({"role": "user", "content": turn["q"]})
        msgs.append({"role": "assistant", "content": turn[side]})
    msgs.append({"role": "user", "content": user_query})
    return msgs


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


def truth_worst_origin():
    """Planner entry point: WHERE is the problem — one clean, readable answer."""
    by_o = (utilization.groupby('ORIG_TRML_CD', as_index=False)
            .agg(avg_utilization=('UTIL_PCT_3', 'mean'), loads=('TRLR_NBR', 'count'))
            .sort_values('avg_utilization'))
    by_o['terminal'] = by_o['ORIG_TRML_CD'].map(TERMINAL_NAMES)
    by_o['avg_utilization'] = by_o['avg_utilization'].round(2)
    worst = by_o.iloc[0]
    text = (f"Lowest-utilization origin: **{worst['terminal']}** "
            f"({worst['ORIG_TRML_CD']}) at **{worst['avg_utilization']}%** over "
            f"{worst['loads']} loads. Next step for a planner: accept the assistant's "
            f"offer below to run the scoped improvement diagnostic for this terminal.")
    table = by_o[['terminal', 'avg_utilization', 'loads']]
    chart = by_o.set_index('terminal')[['avg_utilization']]
    facts = [
        (f"Identifies {worst['terminal']} as lowest",
         [_terminal_variants(worst['ORIG_TRML_CD'])]),
        (f"States its average UTIL_PCT_3 ({worst['avg_utilization']}%)",
         [_fmt_pct_variants(worst['avg_utilization'])]),
    ]
    return (text, table, chart, facts)


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
    "Which origin terminal has the lowest utilization?": truth_worst_origin,
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
    "Which origin terminal has the lowest utilization?": "origin_utilization",
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

FLOW_COMPARISON_SVG = """
<svg viewBox="0 0 940 620" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:980px;height:auto;display:block;margin:0 auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="470" y="26" text-anchor="middle" font-size="16" font-weight="bold" fill="#212529">How the two paths work — same question, same model, different context</text>
  <rect x="280" y="40" width="380" height="40" rx="9" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="470" y="65" text-anchor="middle" font-size="13" font-weight="bold">User asks: "What is REPORTED utilization for Atlanta-origin lanes?"</text>
  <line x1="380" y1="80" x2="235" y2="108" stroke="#555" stroke-width="2" marker-end="url(#fca)"/>
  <line x1="560" y1="80" x2="705" y2="108" stroke="#555" stroke-width="2" marker-end="url(#fca)"/>

  <text x="235" y="128" text-anchor="middle" font-size="13" font-weight="bold" fill="#c92a2a">WITHOUT governed semantics</text>
  <rect x="60" y="140" width="350" height="78" rx="9" fill="#fff5f5" stroke="#c92a2a" stroke-width="1.5"/>
  <text x="235" y="162" text-anchor="middle" font-size="12" font-weight="bold" fill="#212529">1. Model sees the raw schema only</text>
  <text x="235" y="180" text-anchor="middle" font-size="11" fill="#495057">Cryptic reality: UTIL_PCT_1 / _2 / _3, terminal code ATL,</text>
  <text x="235" y="196" text-anchor="middle" font-size="11" fill="#495057">packed lists, no definitions — plus 3 sample rows</text>
  <line x1="235" y1="218" x2="235" y2="238" stroke="#c92a2a" stroke-width="2" marker-end="url(#fca)"/>
  <rect x="60" y="240" width="350" height="72" rx="9" fill="#fff5f5" stroke="#c92a2a" stroke-width="1.5"/>
  <text x="235" y="262" text-anchor="middle" font-size="12" font-weight="bold">2. Model GUESSES the meaning</text>
  <text x="235" y="280" text-anchor="middle" font-size="11" fill="#495057">Which column is authoritative? Which loads count?</text>
  <text x="235" y="296" text-anchor="middle" font-size="11" fill="#495057">Industry conventions from training — usually right, never governed</text>
  <line x1="235" y1="312" x2="235" y2="332" stroke="#c92a2a" stroke-width="2" marker-end="url(#fca)"/>
  <rect x="60" y="334" width="350" height="66" rx="9" fill="#fff5f5" stroke="#c92a2a" stroke-width="1.5"/>
  <text x="235" y="356" text-anchor="middle" font-size="12" font-weight="bold">3. Writes SQL directly from the guess</text>
  <text x="235" y="374" text-anchor="middle" font-size="10.5" fill="#495057" font-family="monospace">AVG(UTIL_PCT_?) WHERE ... = 'ATL'  — plausible, unverifiable</text>
  <line x1="235" y1="400" x2="235" y2="420" stroke="#c92a2a" stroke-width="2" marker-end="url(#fca)"/>
  <rect x="60" y="422" width="350" height="58" rx="9" fill="#fff" stroke="#c92a2a" stroke-width="2"/>
  <text x="235" y="444" text-anchor="middle" font-size="12" font-weight="bold" fill="#c92a2a">4. A number — with no policy behind it</text>
  <text x="235" y="462" text-anchor="middle" font-size="11" fill="#495057">May ignore exclusions, wrong column, wrong date — silently</text>

  <text x="705" y="128" text-anchor="middle" font-size="13" font-weight="bold" fill="#2b8a3e">WITH governed semantics (this app)</text>
  <rect x="530" y="140" width="350" height="78" rx="9" fill="#ebfbee" stroke="#2b8a3e" stroke-width="1.5"/>
  <text x="705" y="162" text-anchor="middle" font-size="12" font-weight="bold">1. RETRIEVE the company's meaning (RAG)</text>
  <text x="705" y="180" text-anchor="middle" font-size="11" fill="#495057">The question searches a governed ontology — business meaning</text>
  <text x="705" y="196" text-anchor="middle" font-size="11" fill="#495057">stored as data (here: ontology.py; prod: semantic registry)</text>
  <line x1="705" y1="218" x2="705" y2="238" stroke="#2b8a3e" stroke-width="2" marker-end="url(#fca)"/>
  <rect x="530" y="240" width="350" height="72" rx="9" fill="#ebfbee" stroke="#2b8a3e" stroke-width="1.5"/>
  <text x="705" y="260" text-anchor="middle" font-size="12" font-weight="bold">2. Retrieval returns the governed bundle</text>
  <text x="705" y="278" text-anchor="middle" font-size="11" fill="#495057">Metric: use UTIL_PCT_3 · decode: ATL = Atlanta · Finance</text>
  <text x="705" y="294" text-anchor="middle" font-size="11" fill="#495057">exclusions · owner + policy provenance · related rules</text>
  <line x1="705" y1="312" x2="705" y2="332" stroke="#2b8a3e" stroke-width="2" marker-end="url(#fca)"/>
  <rect x="530" y="334" width="350" height="66" rx="9" fill="#ebfbee" stroke="#2b8a3e" stroke-width="1.5"/>
  <text x="705" y="354" text-anchor="middle" font-size="12" font-weight="bold">3. Model builds SQL USING that meaning</text>
  <text x="705" y="370" text-anchor="middle" font-size="10.5" fill="#495057" font-family="monospace">AVG(UTIL_PCT_3) WHERE ORIG_TRML_CD='ATL' + governed filters</text>
  <text x="705" y="386" text-anchor="middle" font-size="10.5" fill="#495057">(schema + rules ride in a cached context window)</text>
  <line x1="705" y1="400" x2="705" y2="420" stroke="#2b8a3e" stroke-width="2" marker-end="url(#fca)"/>
  <rect x="530" y="422" width="350" height="58" rx="9" fill="#fff" stroke="#2b8a3e" stroke-width="2"/>
  <text x="705" y="442" text-anchor="middle" font-size="12" font-weight="bold" fill="#2b8a3e">4. Gate checks it → database computes →</text>
  <text x="705" y="460" text-anchor="middle" font-size="11" fill="#495057">answer arrives with definition, policy, owner, and evidence</text>

  <rect x="140" y="504" width="660" height="46" rx="9" fill="#f3f0ff" stroke="#845ef7" stroke-width="1.5"/>
  <text x="470" y="523" text-anchor="middle" font-size="12.5" font-weight="bold" fill="#3b2b78">Same data, model, and question — the governed path ADDITIONALLY receives</text>
  <text x="470" y="541" text-anchor="middle" font-size="12.5" fill="#3b2b78">explicit company meaning: governed definitions, rules, and action tools, delivered at ask-time.</text>
  <text x="470" y="580" text-anchor="middle" font-size="11" font-style="italic" fill="#868e96">In both paths the database does the arithmetic — the model never calculates. The difference is whether meaning is guessed or governed.</text>
  <defs><marker id="fca" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#555"/></marker></defs>
</svg>"""


RAG_EXAMPLE_SVG = """
<svg viewBox="0 0 940 560" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:980px;height:auto;display:block;margin:0 auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="470" y="26" text-anchor="middle" font-size="16" font-weight="bold" fill="#212529">One question under the microscope — how retrieval actually works</text>

  <rect x="20" y="48" width="200" height="64" rx="9" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="120" y="72" text-anchor="middle" font-size="12" font-weight="bold">1. The question</text>
  <text x="120" y="90" text-anchor="middle" font-size="10.5" fill="#495057">"What's the cube utilization</text>
  <text x="120" y="104" text-anchor="middle" font-size="10.5" fill="#495057">for Atlanta?"</text>
  <line x1="220" y1="80" x2="258" y2="80" stroke="#555" stroke-width="2" marker-end="url(#rga)"/>

  <rect x="260" y="48" width="210" height="64" rx="9" fill="#e7f5ff" stroke="#1c7ed6" stroke-width="1.5"/>
  <text x="365" y="70" text-anchor="middle" font-size="12" font-weight="bold">2. Search the ontology index</text>
  <text x="365" y="87" text-anchor="middle" font-size="10.5" fill="#495057">Question is embedded and matched</text>
  <text x="365" y="101" text-anchor="middle" font-size="10.5" fill="#495057">against ~40 chunks of business meaning</text>
  <line x1="470" y1="80" x2="508" y2="80" stroke="#555" stroke-width="2" marker-end="url(#rga)"/>

  <rect x="510" y="40" width="410" height="200" rx="9" fill="#fff" stroke="#845ef7" stroke-width="2"/>
  <text x="715" y="62" text-anchor="middle" font-size="12" font-weight="bold" fill="#3b2b78">3. Top-matching chunks come back (real chunk ids)</text>
  <rect x="525" y="72" width="380" height="34" rx="6" fill="#f3f0ff" stroke="#845ef7"/>
  <text x="535" y="86" font-size="10" font-family="monospace" fill="#3b2b78">metric:reported_utilization</text>
  <text x="535" y="99" font-size="9.5" fill="#495057">"avg UTIL_PCT_3 over reported loads — authoritative column, governed exclusion"</text>
  <rect x="525" y="112" width="380" height="34" rx="6" fill="#f3f0ff" stroke="#845ef7"/>
  <text x="535" y="126" font-size="10" font-family="monospace" fill="#3b2b78">core:terminal_codes (always on)</text>
  <text x="535" y="139" font-size="9.5" fill="#495057">"ATL = Atlanta · HAR = Harrison · SGF = Springfield …"</text>
  <rect x="525" y="152" width="380" height="34" rx="6" fill="#fff9db" stroke="#e6a700"/>
  <text x="535" y="166" font-size="10" font-family="monospace" fill="#8a6100">rule:reported_utilization_exclusion · dependency-included</text>
  <text x="535" y="179" font-size="9.5" fill="#495057">pulled by REFERENCE, not similarity — patterns bring their rules</text>
  <rect x="525" y="192" width="380" height="34" rx="6" fill="#f3f0ff" stroke="#845ef7"/>
  <text x="535" y="206" font-size="10" font-family="monospace" fill="#3b2b78">rule:temporal_convention</text>
  <text x="535" y="219" font-size="9.5" fill="#495057">"the event date is LH_DSPTCH_DT — never SHPMT_CRT_DT"</text>

  <line x1="715" y1="240" x2="715" y2="268" stroke="#555" stroke-width="2" marker-end="url(#rga)"/>
  <rect x="510" y="270" width="410" height="70" rx="9" fill="#ebfbee" stroke="#2b8a3e" stroke-width="1.5"/>
  <text x="715" y="292" text-anchor="middle" font-size="12" font-weight="bold">4. Bundle assembled into the model's context</text>
  <text x="715" y="309" text-anchor="middle" font-size="10.5" fill="#495057">Always-on core rules + schemas (CACHED, reused every question)</text>
  <text x="715" y="324" text-anchor="middle" font-size="10.5" fill="#495057">+ these retrieved slices (fresh per question)</text>

  <line x1="510" y1="305" x2="250" y2="305" stroke="#555" stroke-width="2" marker-end="url(#rgb)"/>
  <rect x="30" y="270" width="215" height="70" rx="9" fill="#fff" stroke="#1c7ed6" stroke-width="1.5"/>
  <text x="137" y="292" text-anchor="middle" font-size="12" font-weight="bold">5. Model writes the SQL</text>
  <text x="137" y="309" text-anchor="middle" font-size="9.5" font-family="monospace" fill="#495057">AVG(UTIL_PCT_3) WHERE</text>
  <text x="137" y="323" text-anchor="middle" font-size="9.5" font-family="monospace" fill="#495057">ORIG_TRML_CD = 'ATL'</text>

  <line x1="137" y1="340" x2="137" y2="368" stroke="#555" stroke-width="2" marker-end="url(#rga)"/>
  <rect x="30" y="370" width="215" height="58" rx="9" fill="#fff" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="137" y="392" text-anchor="middle" font-size="12" font-weight="bold">6. Validation gate</text>
  <text x="137" y="409" text-anchor="middle" font-size="10" fill="#495057">read-only · single statement · table allowlist</text>

  <line x1="245" y1="399" x2="330" y2="399" stroke="#555" stroke-width="2" marker-end="url(#rga)"/>
  <rect x="332" y="370" width="230" height="58" rx="9" fill="#fff" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="447" y="392" text-anchor="middle" font-size="12" font-weight="bold">7. Database computes</text>
  <text x="447" y="409" text-anchor="middle" font-size="10" fill="#495057">DuckDB here · Databricks SQL in production</text>

  <line x1="562" y1="399" x2="648" y2="399" stroke="#555" stroke-width="2" marker-end="url(#rga)"/>
  <rect x="650" y="362" width="270" height="74" rx="9" fill="#ebfbee" stroke="#2b8a3e" stroke-width="2"/>
  <text x="785" y="386" text-anchor="middle" font-size="12" font-weight="bold" fill="#2b8a3e">8. Answer + evidence</text>
  <text x="785" y="403" text-anchor="middle" font-size="10" fill="#495057">the number, the definition it used, the rules applied,</text>
  <text x="785" y="417" text-anchor="middle" font-size="10" fill="#495057">the SQL, and the retrieved chunks — all inspectable</text>

  <text x="470" y="472" text-anchor="middle" font-size="12" font-weight="bold" fill="#3b2b78">The ontology is structured semantic METADATA: business meaning lives in a governed file (ontology.py here,</text>
  <text x="470" y="490" text-anchor="middle" font-size="12" font-weight="bold" fill="#3b2b78">a semantic registry in production) — edit the meaning once, and every answer inherits the fix.</text>
  <text x="470" y="530" text-anchor="middle" font-size="11" font-style="italic" fill="#868e96">You can watch this exact pipeline live: every answer's evidence expander shows the retrieved chunks (RAG step) for that question.</text>
  <defs>
    <marker id="rga" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#555"/></marker>
    <marker id="rgb" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#555"/></marker>
  </defs>
</svg>"""

def build_v1_problem():
    _naive = utilization['UTIL_PCT_3'].mean()
    _rep = utilization[utilization['SHPMT_CNT'] > 1]['UTIL_PCT_3'].mean()
    return """
<svg viewBox="0 0 940 300" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:980px;height:auto;display:block;margin:0 auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="470" y="30" text-anchor="middle" font-size="17" font-weight="bold" fill="#212529">The problem in one picture</text>
  <rect x="330" y="50" width="280" height="46" rx="10" fill="#f8f9fa" stroke="#adb5bd" stroke-width="1.5"/>
  <text x="470" y="79" text-anchor="middle" font-size="14" font-weight="bold">"What is our reported utilization?"</text>
  <line x1="400" y1="96" x2="230" y2="140" stroke="#555" stroke-width="2" marker-end="url(#v1a)"/>
  <line x1="540" y1="96" x2="710" y2="140" stroke="#555" stroke-width="2" marker-end="url(#v1a)"/>
  <rect x="80" y="142" width="300" height="70" rx="10" fill="#fff" stroke="#d62828" stroke-width="2"/>
  <text x="230" y="168" text-anchor="middle" font-size="13" font-weight="bold" fill="#d62828">AI reads the schema alone</text>
  <text x="230" y="192" text-anchor="middle" font-size="20" font-weight="bold" fill="#d62828">""" + f"{_naive:.1f}%" + """</text>
  <rect x="560" y="142" width="300" height="70" rx="10" fill="#fff" stroke="#2b8a3e" stroke-width="2"/>
  <text x="710" y="168" text-anchor="middle" font-size="13" font-weight="bold" fill="#2b8a3e">AI + governed company meaning</text>
  <text x="710" y="192" text-anchor="middle" font-size="20" font-weight="bold" fill="#2b8a3e">""" + f"{_rep:.1f}%" + """</text>
  <text x="470" y="248" text-anchor="middle" font-size="14" fill="#495057">Both queries ran successfully. Only one follows company policy. (Live numbers from the current data.)</text>
  <text x="470" y="272" text-anchor="middle" font-size="13" font-style="italic" fill="#868e96">The model did not fail at SQL — the enterprise failed to provide the meaning.</text>
  <defs><marker id="v1a" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#555"/></marker></defs>
</svg>"""


V2_CONTRACT = """
<svg viewBox="0 0 940 360" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:980px;height:auto;display:block;margin:0 auto;font-family:Helvetica,Arial,sans-serif;">
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
  <text x="470" y="318" text-anchor="middle" font-size="10" fill="#868e96">facts + lane ref — only metadata + samples reach the LLM</text>
  <line x1="470" y1="280" x2="470" y2="232" stroke="#adb5bd" stroke-width="2" marker-end="url(#v2a)"/>
  <defs><marker id="v2a" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#845ef7"/></marker></defs>
</svg>
"""

V3_STACK = """
<svg viewBox="0 0 940 330" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:980px;height:auto;display:block;margin:0 auto;font-family:Helvetica,Arial,sans-serif;">
  <text x="470" y="28" text-anchor="middle" font-size="17" font-weight="bold" fill="#212529">Tech design: every POC component has a named production target</text>
  <text x="330" y="62" text-anchor="middle" font-size="13" font-weight="bold" fill="#868e96">THIS PROTOTYPE</text>
  <text x="700" y="62" text-anchor="middle" font-size="13" font-weight="bold" fill="#2b8a3e">PRODUCTION (Databricks/Azure)</text>
  <g font-size="12">
    <rect x="150" y="80" width="360" height="34" rx="7" fill="#f8f9fa" stroke="#adb5bd"/><text x="330" y="102" text-anchor="middle">DuckDB in-process engine</text>
    <rect x="560" y="80" width="330" height="34" rx="7" fill="#e6f4d7" stroke="#4f772d"/><text x="725" y="102" text-anchor="middle">Databricks SQL warehouse</text>
    <rect x="150" y="122" width="360" height="34" rx="7" fill="#f8f9fa" stroke="#adb5bd"/><text x="330" y="144" text-anchor="middle">In-memory vector index (fastembed)</text>
    <rect x="560" y="122" width="330" height="34" rx="7" fill="#e6f4d7" stroke="#4f772d"/><text x="725" y="144" text-anchor="middle">Databricks Vector Search</text>
    <rect x="150" y="164" width="360" height="34" rx="7" fill="#f8f9fa" stroke="#adb5bd"/><text x="330" y="186" text-anchor="middle">ontology.py (versioned dict)</text>
    <rect x="560" y="164" width="330" height="34" rx="7" fill="#e6f4d7" stroke="#4f772d"/><text x="725" y="186" text-anchor="middle">Governed semantic registry + Unity Catalog (physical governance)</text>
    <rect x="150" y="206" width="360" height="34" rx="7" fill="#f8f9fa" stroke="#adb5bd"/><text x="330" y="228" text-anchor="middle">Regex gate + table allowlist</text>
    <rect x="560" y="206" width="330" height="34" rx="7" fill="#e6f4d7" stroke="#4f772d"/><text x="725" y="228" text-anchor="middle">SQL AST validation + entitlements + RLS</text>
    <rect x="150" y="248" width="360" height="34" rx="7" fill="#f8f9fa" stroke="#adb5bd"/><text x="330" y="270" text-anchor="middle">Direct Claude API + prompt caching</text>
    <rect x="560" y="248" width="330" height="34" rx="7" fill="#e6f4d7" stroke="#4f772d"/><text x="725" y="270" text-anchor="middle">Governed metrics/action APIs, exposed via MCP</text>
  </g>
  <line x1="510" y1="97" x2="560" y2="97" stroke="#2b8a3e" stroke-width="2" marker-end="url(#v3a)"/>
  <line x1="510" y1="139" x2="560" y2="139" stroke="#2b8a3e" stroke-width="2" marker-end="url(#v3a)"/>
  <line x1="510" y1="181" x2="560" y2="181" stroke="#2b8a3e" stroke-width="2" marker-end="url(#v3a)"/>
  <line x1="510" y1="223" x2="560" y2="223" stroke="#2b8a3e" stroke-width="2" marker-end="url(#v3a)"/>
  <line x1="510" y1="265" x2="560" y2="265" stroke="#2b8a3e" stroke-width="2" marker-end="url(#v3a)"/>
  <text x="470" y="316" text-anchor="middle" font-size="12" font-style="italic" fill="#868e96">The separation of concerns carries forward; production adds identity, policy enforcement, versioning, evaluation, observability, and operational controls.</text>
  <defs><marker id="v3a" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 Z" fill="#2b8a3e"/></marker></defs>
</svg>
"""


def reroute_whatif(orig, via, dest):
    """Deterministic simulation: move the (orig->dest) lane's freight onto
    (orig->via)+(via->dest), repacking into existing leg loads within capacity;
    overflow opens new leg loads. Planning estimate — timing/doors/handling
    unmodeled (per the governed reroute_whatif action)."""
    CP = ontology["actions"]["consolidation_opportunity"]["parameters"]
    def _lane_row(o, d):
        r = lane_ref[(lane_ref['ORIG_TRML_CD'] == o) & (lane_ref['DEST_TRML_CD'] == d)]
        return None if r.empty else r.iloc[0]
    direct = _lane_row(orig, dest)
    if via is None:
        # DIRECT BASELINE: no reroute — report the lane's current state
        u0 = utilization
        cur = u0[(u0['ORIG_TRML_CD'] == orig) & (u0['DEST_TRML_CD'] == dest)]
        if direct is None:
            return {"error": f"Lane {orig}->{dest} not in lane_ref"}
        if len(cur) == 0:
            return {"error": f"No loads on {orig}->{dest} in this window."}
        return {"mode": "direct_baseline", "loads": len(cur),
                "avg_util": round(cur['UTIL_PCT_3'].mean(), 1),
                "total_cost_usd": int(len(cur) * direct['LANE_MILES'] * direct['CPM_USD']),
                "svc_days": int(direct['SVC_STD_DAYS']),
                "note": "Current state of the direct lane — pick a via terminal to simulate a reroute against this baseline."}
    leg1, leg2 = _lane_row(orig, via), _lane_row(via, dest)
    if direct is None or leg1 is None or leg2 is None:
        missing = [f"{a}->{b}" for (a, b), r in
                   [((orig, dest), direct), ((orig, via), leg1), ((via, dest), leg2)]
                   if r is None]
        return {"error": f"Lane(s) not in lane_ref: {', '.join(missing)}"}
    u = utilization.copy()
    moved = u[(u['ORIG_TRML_CD'] == orig) & (u['DEST_TRML_CD'] == dest)]
    if len(moved) == 0:
        return {"error": f"No loads on {orig}->{dest} in this window."}
    keep = u.drop(moved.index)
    total_cube = float(moved['LD_CUBE_FT'].sum())
    total_wgt = float(moved['LD_WGT_LB'].sum())

    def _pack_leg(o, d, cube, wgt):
        """Fill existing loads on the leg up to caps; overflow -> new loads."""
        leg_loads = keep[(keep['ORIG_TRML_CD'] == o) & (keep['DEST_TRML_CD'] == d)]
        _wo, _co = capacity_flags(leg_loads) if len(leg_loads) else (None, None)
        absorbed = 0.0
        new_utils = []
        for i, r in leg_loads.iterrows():
            if _wo is not None and (_wo.loc[i] or _co.loc[i]):
                new_utils.append(r['UTIL_PCT_3'])
                continue  # never add freight to capacity-constrained loads
            room_c = CP["max_combined_cube"] - r['LD_CUBE_FT']
            room_w = CP["max_combined_weight_lb"] - r['LD_WGT_LB']
            if cube <= 0 or room_c <= 0 or room_w <= 0:
                new_utils.append(r['UTIL_PCT_3'])
                continue
            frac = min(1.0, room_c / max(cube, 1e-9), room_w / max(wgt, 1e-9) if wgt > 0 else 1.0)
            add_c, add_w = cube * frac, wgt * frac
            cube -= add_c; wgt -= add_w; absorbed += add_c
            new_utils.append(max(round((r['LD_CUBE_FT'] + add_c) / 2000 * 100, 1),
                                 round((r['LD_WGT_LB'] + add_w) / 20000 * 100, 1)))
        new_loads = 0
        while cube > 1e-6 or wgt > 1e-6:
            take_c = min(cube, CP["max_combined_cube"])
            take_w = min(wgt, CP["max_combined_weight_lb"])
            new_utils.append(max(round(take_c / 2000 * 100, 1),
                                 round(take_w / 20000 * 100, 1)))
            cube -= take_c; wgt -= take_w; new_loads += 1
        return new_loads, new_utils

    n1, utils1 = _pack_leg(orig, via, total_cube, total_wgt)
    n2, utils2 = _pack_leg(via, dest, total_cube, total_wgt)
    before_avg = round(u['UTIL_PCT_3'].mean(), 1)
    other_utils = list(keep[~(((keep['ORIG_TRML_CD'] == orig) & (keep['DEST_TRML_CD'] == via))
                              | ((keep['ORIG_TRML_CD'] == via) & (keep['DEST_TRML_CD'] == dest)))]['UTIL_PCT_3'])
    after_utils = other_utils + utils1 + utils2
    after_avg = round(sum(after_utils) / len(after_utils), 1) if after_utils else 0
    moves_removed = len(moved)
    moves_added = n1 + n2
    cost_removed = moves_removed * float(direct['LANE_MILES'] * direct['CPM_USD'])
    cost_added = (n1 * float(leg1['LANE_MILES'] * leg1['CPM_USD'])
                  + n2 * float(leg2['LANE_MILES'] * leg2['CPM_USD']))
    svc_direct = int(direct['SVC_STD_DAYS'])
    svc_path = int(leg1['SVC_STD_DAYS'] + leg2['SVC_STD_DAYS'])
    return {"moved_loads": moves_removed, "moved_cube": int(total_cube),
            "new_leg_loads": moves_added,
            "net_moves_delta": moves_added - moves_removed,
            "before_avg_util": before_avg, "after_avg_util": after_avg,
            "cost_delta_usd": int(round(cost_added - cost_removed)),
            "svc_direct_days": svc_direct, "svc_path_days": svc_path,
            "service_ok": svc_path <= svc_direct}


def param_whatif(action_or_rule, param, value):
    """Governed parameter what-if: temporarily set an ontology parameter,
    recompute the affected engine outputs, restore, and report the deltas.
    The mutation never persists — the governed contract stays authoritative."""
    home = None
    for section in ("actions", "business_rules"):
        node = ontology.get(section, {}).get(action_or_rule)
        if node and "parameters" in node and param in node["parameters"]:
            home = node["parameters"]
            break
    if home is None:
        return {"error": f"No governed parameter '{param}' on '{action_or_rule}' — "
                         f"parameters must exist in the ontology to be simulated."}
    def _snapshot():
        elig, rej = find_consolidations()
        dg = utilization_diagnostic()
        fr = find_frequency_candidates()
        return {"eligible_pairs": len(elig),
                "moves_saved": int(dg['moves']['moves_saved'].sum()) if len(dg['moves']) else 0,
                "achievable_util": dg['achievable'],
                "est_saving_usd": dg['total_usd'],
                "schedule_signals": len(fr)}
    original = home[param]
    before = _snapshot()
    try:
        home[param] = value
        after = _snapshot()
    finally:
        home[param] = original
    return {"mode": "param_whatif", "target": f"{action_or_rule}.{param}",
            "original": original, "tested": value,
            "before": before, "after": after}


def extract_tool(response_text):
    """Parse a governed tool request block: ```tool\n{json}\n``` -> dict or None."""
    m = re.search(r"```tool\s*\n(.*?)```", response_text or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1).strip())
    except Exception:
        return {"error": "Malformed tool JSON", "raw": m.group(1)[:200]}


def run_tool(tool):
    """Dispatch a governed tool request to its deterministic engine."""
    if not isinstance(tool, dict) or "tool" not in tool:
        return {"error": "Unrecognized tool request."}
    name = tool.get("tool")
    if name == "reroute_whatif":
        return reroute_whatif(tool.get("orig"), tool.get("via"), tool.get("dest"))
    if name == "param_whatif":
        return param_whatif(tool.get("action"), tool.get("param"), tool.get("value"))
    return {"error": f"Unknown tool '{name}' — governed tools: reroute_whatif, param_whatif."}


def lane_imbalance(orig=None):
    """Directional balance per lane pair: driver requirement is set by the MAX
    direction; the gap is repositioning exposure (or a rerouting opportunity)."""
    flows = (utilization.groupby(['ORIG_TRML_CD', 'DEST_TRML_CD'])
             .size().reset_index(name='loads'))
    fmap = {(r['ORIG_TRML_CD'], r['DEST_TRML_CD']): r['loads'] for _, r in flows.iterrows()}
    rows, seen = [], set()
    for (a, b) in list(fmap):
        key = tuple(sorted([a, b]))
        if key in seen:
            continue
        seen.add(key)
        f, r = fmap.get((key[0], key[1]), 0), fmap.get((key[1], key[0]), 0)
        GN = ontology["business_rules"]["recommendation_granularity"]["parameters"]
        _ud = utilization.copy()
        _ud['LH_DSPTCH_DT'] = pd.to_datetime(_ud['LH_DSPTCH_DT'])
        _ud['dow'] = _ud['LH_DSPTCH_DT'].dt.day_name()
        _pair_loads = _ud[((_ud['ORIG_TRML_CD'] == key[0]) & (_ud['DEST_TRML_CD'] == key[1]))
                          | ((_ud['ORIG_TRML_CD'] == key[1]) & (_ud['DEST_TRML_CD'] == key[0]))]
        _dprof = []
        for _d, _g in _pair_loads.groupby('dow'):
            if len(_g) >= GN["min_dow_load_count"]:
                _f2 = int((_g['ORIG_TRML_CD'] == key[0]).sum())
                _r2 = len(_g) - _f2
                if abs(_f2 - _r2) >= 2:
                    _dprof.append(f"{_d} ({_f2}v{_r2})")
        rows.append({
            'dow_concentration': "; ".join(_dprof) if _dprof
                                 else "insufficient DOW samples — pair-grain only",
            'lane_pair': f"{TERMINAL_NAMES[key[0]]} ↔ {TERMINAL_NAMES[key[1]]}",
            f'{key[0]}→{key[1]}': f, f'{key[1]}→{key[0]}': r,
            'fwd': f, 'rev': r, 'a': key[0], 'b': key[1],
            'directional_load_requirement': max(f, r),
            'directional_load_gap (potential repositioning exposure)': abs(f - r)})
    df = pd.DataFrame(rows).sort_values('directional_load_gap (potential repositioning exposure)',
                                        ascending=False)
    if orig:
        df = df[(df['a'] == orig) | (df['b'] == orig)]
    return df[['lane_pair', 'fwd', 'rev', 'directional_load_requirement',
               'directional_load_gap (potential repositioning exposure)',
               'dow_concentration']].rename(
        columns={'fwd': 'direction A→B loads', 'rev': 'direction B→A loads'})


def build_network_svg():
    """Directional map: one curved arrow PER DIRECTION with its own count —
    because driver requirements are set by the max direction, not the total."""
    POS = {"SGF": (340, 120), "STL": (600, 85), "HAR": (280, 250),
           "MEM": (520, 290), "ATL": (790, 330)}
    flows = (utilization.groupby(['ORIG_TRML_CD', 'DEST_TRML_CD'])
             .size().reset_index(name='loads'))
    fmap = {(r['ORIG_TRML_CD'], r['DEST_TRML_CD']): r['loads'] for _, r in flows.iterrows()}
    import math
    arcs, labels = [], []
    done_pairs = set()
    for (a, b), n in fmap.items():
        x1, y1 = POS[a]; x2, y2 = POS[b]
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1
        # perpendicular offset separates the two directions
        px, py = -dy / L * 26, dx / L * 26
        cx, cy = (x1 + x2) / 2 + px, (y1 + y2) / 2 + py
        rev = fmap.get((b, a), 0)
        _min_imb = ontology["actions"]["backhaul_rebalance"]["parameters"]["min_imbalance"]
        imb = abs(n - rev) >= _min_imb and rev > 0
        color = "#e6a700" if imb else "#74a2c7"
        w = 1.5 + min(n, 12) * 0.5
        arcs.append(f'<path d="M {x1} {y1} Q {cx:.0f} {cy:.0f} {x2} {y2}" fill="none" '
                    f'stroke="{color}" stroke-width="{w:.1f}" stroke-opacity="0.7" '
                    f'marker-end="url(#dirarrow)"/>')
        lx, ly = (x1 + x2) / 2 + px * 1.35, (y1 + y2) / 2 + py * 1.35
        labels.append(f'<text x="{lx:.0f}" y="{ly:.0f}" text-anchor="middle" '
                      f'font-size="11" font-weight="bold" fill="{"#8a6100" if imb else "#40607a"}">{n}</text>')
        key = tuple(sorted([a, b]))
        if imb and key not in done_pairs:
            done_pairs.add(key)
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            labels.append(f'<text x="{mx:.0f}" y="{my + 4:.0f}" text-anchor="middle" '
                          f'font-size="10" font-style="italic" fill="#b00020">Δ{abs(n - rev)}</text>')
    nodes = []
    node_tot = {t: 0 for t in POS}
    for (a, b), n in fmap.items():
        node_tot[a] += n; node_tot[b] += n
    for t, (x, y) in POS.items():
        r = 20 + min(node_tot.get(t, 0), 30) * 0.5
        hub = t == "SGF"
        nodes.append(
            f'<circle cx="{x}" cy="{y}" r="{r:.0f}" fill="{"#f3f0ff" if hub else "#fff"}" '
            f'stroke="{"#845ef7" if hub else "#1c7ed6"}" stroke-width="{3 if hub else 2}"/>'
            f'<text x="{x}" y="{y - 3}" text-anchor="middle" font-size="12" '
            f'font-weight="bold" fill="#212529">{t}</text>'
            f'<text x="{x}" y="{y + 12}" text-anchor="middle" font-size="10" '
            f'fill="#495057">{TERMINAL_NAMES[t]}</text>')
    return (
        '<svg viewBox="0 0 940 410" xmlns="http://www.w3.org/2000/svg" '
        'style="width:100%;max-width:980px;height:auto;display:block;margin:0 auto;'
        'font-family:Helvetica,Arial,sans-serif;">'
        '<text x="470" y="24" text-anchor="middle" font-size="16" font-weight="bold" '
        'fill="#212529">The network — DIRECTIONAL flows (load requirement follows the max direction)</text>'
        + "".join(arcs) + "".join(labels) + "".join(nodes) +
        '<text x="470" y="398" text-anchor="middle" font-size="11" font-style="italic" '
        'fill="#868e96">Each arrow is one direction with its own load count. Amber arcs with Δn: '
        'asymmetric directional demand — POTENTIAL repositioning exposure (a proxy; '
        'actual empty miles need schedule and cycle data). '
        'Springfield (violet) is the break terminal.</text>'
        '<defs><marker id="dirarrow" markerWidth="9" markerHeight="9" refX="7" refY="3" '
        'orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="#40607a"/></marker></defs></svg>')


_has_convo = bool(st.session_state.get("chat_turns"))
with st.expander("Start Here — What This App Is", expanded=not _has_convo):
    st.markdown("""
**The problem this app demonstrates.** Enterprise AI can query data fluently — but it
does not automatically know which definition, date, code, or policy the company
considers authoritative. That knowledge lives in memos and people, not schemas. The
result: two technically valid queries, two different numbers, one business decision.
""")
    components.html(build_v1_problem(), height=340, scrolling=True)
    components.html(FLOW_COMPARISON_SVG, height=650, scrolling=True)
    components.html(RAG_EXAMPLE_SVG, height=590, scrolling=True)
    st.caption("ILLUSTRATIVE retrieval bundle (live retrieval for each real answer appears in its evidence expander) \u2014 the retrieval step from the right-hand lane above, under the "
               "microscope: an actual question matching actual chunks from this "
               "app's ontology. The amber chunk shows dependency expansion — a "
               "retrieved pattern always brings its governing rule along, whether "
               "or not similarity found it.")
    st.caption("Read the two lanes top to bottom: the left path guesses meaning from the schema; the right path RETRIEVES governed meaning from the ontology before writing a single line of SQL. Every answer in this app runs both lanes so you can compare them live.")
    st.markdown(f"""
**What is in this app** — four components, top to bottom:

1. **KPI Dashboard** — the questions someone anticipated, computed directly from the
   data: the traditional BI world.
2. **Action Panel** — governed opportunities (trailer consolidation, schedule review)
   surfaced by deterministic rules from the ontology's action layer, priced in dollars,
   with owners.
3. **The Assistant experiment** — the heart of the app: the SAME question answered two
   ways by the same AI model, with and without the company's governed meaning, then
   fact-checked against independently computed ground truth.
4. **Technical Appendix** — architecture, the live knowledge graph, and developer
   detail, for after the proof has landed.

**The data underneath** — a realistic slice of LTL linehaul: **{len(utilization)}
trailer loads** across **{utilization['ORIG_TRML_CD'].nunique()} terminals** (flows shown DIRECTIONALLY below — the directional load requirement, a planning proxy, follows the max direction of each pair) and
{len(lane_ref)} directed lanes over ~10 weeks, in four legacy fact tables plus a lane
reference (miles, cost, schedules, service standards). The schema is deliberately
hostile — three utilization columns named UTIL_PCT_1/2/3, two competing date fields,
coded terminals — because that is what real warehouses look like. Simplifications are
disclosed in the appendix.

""")
    components.html(build_network_svg(), height=420, scrolling=True)
    st.markdown("""
**How it solves the problem.** Company meaning — definitions, institutional rules,
eligible actions — is written ONCE in a governed ontology. For each question, the
relevant slices are retrieved (RAG over the ontology, not the data), the AI writes SQL
from them, a validation gate screens it, a database engine computes the numbers, and an
automated verdict checks both answers against ground truth. The AI decides WHAT to
compute; the engine does the arithmetic; the ontology supplies the meaning.

---

**How to use this app — pick your path:**

🎯 **Executive (3 minutes).** Click the preset **"What is our reported utilization?"**
Watch the same AI produce two different numbers, read the Verdict, then glance at the
assistant's offer and its dollar figures. The whole argument is in that one screen.

🚚 **Planner (5 minutes) — see the problem, ask why, get the move.** Press the **🚚 Planner button just below** (or the first preset in the grid) — one
readable table names the problem terminal. The assistant then offers to run the improvement diagnostic scoped to
that terminal: click yes. You get the root-cause split (how much is weighed-out freight
that planning can't fix, how much is service-protection policy, what is genuinely
addressable) and the specific consolidation moves with dollar figures. Then type a
follow-up like *"which move should I do first?"* — the analysis is in the conversation.
Then try a SIMULATION in plain language: type *"what if we reroute Springfield to
Memphis through Harrison?"* — a governed engine computes the KPI deltas (including
the service check) and the model never does the arithmetic. The 🔀 what-if widget
above the input does the same with dropdowns.

🔧 **Engineer (10 minutes).** Run any preset, open the **RAG step** expander to see
which ontology slices were retrieved and why, check the 💵 cost panel (tokens, dollars, and cost-per-correct-fact) and cache economics
under each answer, then open the **Technical Appendix** for the architecture, the live
knowledge graph, and the validation gate's honest scope.
""")
    st.markdown("**▶ Start a path with one click** — each button asks that path's "
                "first question for you:")
    _p1, _p2, _p3 = st.columns(3)
    def _launch(q):
        st.session_state.selected_query = q
        st.session_state.is_preset = True
        st.session_state.force_run = True
        st.rerun()
    with _p1:
        if st.button("🎯 Executive: reported utilization", use_container_width=True,
                     key="path_exec"):
            _launch("What is our reported utilization?")
    with _p2:
        if st.button("🚚 Planner: lowest origin terminal", use_container_width=True,
                     key="path_plan"):
            _launch("Which origin terminal has the lowest utilization?")
    with _p3:
        if st.button("🔧 Engineer: run + open the evidence", use_container_width=True,
                     key="path_eng"):
            _launch("What is our reported utilization?")
    st.caption("The scenarios above tell you what to look for; these buttons take "
               "you there. The same questions are also in the preset grid below.")
# ===============================================================
# KPI DASHBOARD: the ANTICIPATED questions (traditional BI world)
# ===============================================================
st.caption("Actions in this app are CONVERSATIONAL: ask a question below, and when "
           "relevant the assistant offers the scoped improvement diagnostic — the same "
           "governed engines production would also run as scheduled scans. Roadmap "
           "levers: plan-vs-actual routing variance (actual leg events + override flag), "
           "doubles pairing, then MILP when tradeoffs go "
           "network-wide.")

st.header("Ask About Cube Utilization")
_h1, _h2 = st.columns([4, 1])
with _h1:
    _n = len(st.session_state.get("chat_turns", []))
    if _n:
        with st.expander(f"Chat session: {_n} prior turn(s) in context "
                         f"(last {MAX_TURNS_IN_CONTEXT} travel with each question)"):
            for i, t in enumerate(st.session_state.chat_turns, 1):
                st.markdown(f"**{i}. You:** {t['q']}")
                st.caption(t['sem'][:200] + "…")
    else:
        st.caption("New chat session — follow-up questions carry context "
                   "(e.g., ask about reported utilization, then 'break that down by lane').")
with _h2:
    if st.button("🔄 New chat", use_container_width=True):
        for k in ["chat_turns", "selected_query", "last_user_query", "custom_q",
                  "rag_hits", "rag_engine", "is_preset", "exec_cache", "force_run"]:
            st.session_state.pop(k, None)
        st.rerun()

if "selected_query" not in st.session_state:
    st.session_state.selected_query = ""
if "is_preset" not in st.session_state:
    st.session_state.is_preset = False

st.markdown("**Pick a question:**")
_FEATURED = ["Which origin terminal has the lowest utilization?",
             "What is our reported utilization?",
             "Where can we consolidate trailers this period to save cost?"]
_qs = _FEATURED + [q for q in PRESET_QUESTIONS if q not in _FEATURED]
for row_start in range(0, len(_qs), 4):
    row_qs = _qs[row_start:row_start + 4]
    cols = st.columns(4)
    for j, q in enumerate(row_qs):
        with cols[j]:
            if st.button(q, key=f"preset_{row_start + j}", use_container_width=True):
                st.session_state.selected_query = q
                st.session_state.is_preset = True
                st.session_state.force_run = True


user_query = st.session_state.selected_query

# prior turns render as chat history (the current turn renders as the primary
# answer below, so skip it here if already recorded)
_hist = st.session_state.get("chat_turns", [])
if _hist:
    _skip_last = bool(user_query) and _hist[-1]["q"] == user_query
    for _t in (_hist[:-1] if _skip_last else _hist):
        with st.chat_message("user"):
            st.write(_t["q"])
        with st.chat_message("assistant"):
            _disp = _t["sem"].split("```")[0].strip() or _t["sem"][:200]
            st.write(_disp[:400] + ("…" if len(_disp) > 400 else ""))


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
EXCEPTION: if the question is PURELY about prioritizing, comparing, or explaining
results ALREADY DISPLAYED in prior turns, answer directly WITHOUT any SQL block.
STRICT LIMIT: any request for NEW numbers — breakdowns by a dimension, different
grains, filters, scopes, or time windows (e.g., "break that down by lane",
"show last week instead") — REQUIRES a fresh SQL query even in a follow-up;
prior context tells you what "that" refers to, not the numbers themselves.
ACTION/IMPROVEMENT questions ("how can we improve", "any opportunities",
"what should we do"): the governed action engines compute these
deterministically — root-cause decomposition, consolidation candidates,
frequency signals, directional balance. Do NOT attempt the full multi-step
analysis in one SQL query. Give a brief scoped observation if a simple query
helps (e.g., the scope's average utilization), then state that the complete
governed diagnostic is available via the assistant's follow-up offer below
the answer.

Respond with ONE short sentence explaining your approach, then the query in a ```sql
fenced block. The system will execute it — do not fabricate result numbers.

{SCHEMAS}"""

    core_text, retrieved_text, rag_hits, rag_engine = assemble_semantic_slices(user_query)
    st.session_state.rag_hits = rag_hits
    st.session_state.rag_engine = rag_engine

    semantic_context = f"""You are a freight analytics assistant. You CANNOT see the data —
only the table schemas and sample rows below. Write ONE DuckDB SQL SELECT query
that answers the user's question when executed against these tables.
EXCEPTION: if the question is PURELY about prioritizing, comparing, or explaining
results ALREADY DISPLAYED in prior turns, answer directly WITHOUT any SQL block.
STRICT LIMIT: any request for NEW numbers — breakdowns by a dimension, different
grains, filters, scopes, or time windows (e.g., "break that down by lane",
"show last week instead") — REQUIRES a fresh SQL query even in a follow-up;
prior context tells you what "that" refers to, not the numbers themselves.
ACTION/IMPROVEMENT questions ("how can we improve", "any opportunities",
"what should we do"): the governed action engines compute these
deterministically — root-cause decomposition, consolidation candidates,
frequency signals, directional balance. Do NOT attempt the full multi-step
analysis in one SQL query. Give a brief scoped observation if a simple query
helps (e.g., the scope's average utilization), then state that the complete
governed diagnostic is available via the assistant's follow-up offer below
the answer.

Respond with ONE short sentence explaining your approach (naming the metric
definition you followed), then the query in a ```sql fenced block. The system will
execute it — do not fabricate result numbers.

You additionally have access to a governed semantic ontology, provided as ALWAYS-ON
core rules plus definitions RETRIEVED for this specific question. Follow them EXACTLY.

GOVERNED SIMULATION TOOLS: for WHAT-IF questions (rerouting freight, changing a
governed parameter like a weight/cube limit or threshold), do NOT write SQL and
do NOT invent numbers. Output a tool block and the platform's deterministic
engines will compute the answer:
```tool
{{"tool": "reroute_whatif", "orig": "SGF", "via": "HAR", "dest": "MEM"}}
```
("via": null gives the direct-lane baseline), or
```tool
{{"tool": "param_whatif", "action": "consolidation_opportunity", "param": "max_combined_weight_lb", "value": 25000}}
```
(the parameter must exist in the ontology's governed contract). One tool block
per answer; add a one-sentence framing before it.

BEGIN your response with ONE compact line in EXACTLY this format, then a blank
line, then your explanation and query (the system parses and removes it):
TRACE: metric=<one of: trailer_utilization, lane_utilization, volume_by_origin, shipments_on_trailer, utilization_trend, reported_utilization, NONE, origin_utilization>; entities=<comma-separated from: Shipment, Trailer, Dispatch, Terminal, Lane, Time>

{core_text}

{SCHEMAS}

RETRIEVED SEMANTIC CONTEXT for this question (top matches from the ontology index):
{retrieved_text}"""

    # PRODUCTION CACHING: the stable context (schemas, and for the semantic
    # side the ontology) goes in the SYSTEM parameter with cache_control.
    # Anthropic caches it server-side; subsequent questions read the cache at
    # ~10% of the fresh-token price. Only the user's question changes per call.
    raw_system = [{"type": "text", "text": raw_context,
                   "cache_control": {"type": "ephemeral"}}]
    # CACHE-CORRECT SPLIT: the stable prefix (instructions + core + schemas) is
    # cached; the per-question retrieved slices ride uncached after it, so the
    # cache key never changes between questions.
    _stable_prefix, _sep, _variable_tail = semantic_context.partition(
        "RETRIEVED SEMANTIC CONTEXT")
    semantic_system = [
        {"type": "text", "text": _stable_prefix,
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _sep + _variable_tail},
    ]

    st.session_state.last_user_query = user_query
    # Store both prompts so the developer section can show the real payloads
    st.session_state.last_raw_prompt = f"[SYSTEM, cached]\n{raw_context}\n\n[USER]\n{user_query}"
    st.session_state.last_semantic_prompt = f"[SYSTEM, cached]\n{semantic_context}\n\n[USER]\n{user_query}"

    _primary_slot = st.container()
    _evidence_open = (st.session_state.get("force_run", False)
                      or st.session_state.get("exec_cache", {}).get("q") != user_query)
    with st.expander("🔬 How this answer was produced — with/without comparison, RAG retrieval, validation gate, ground truth, verdict", expanded=_evidence_open):
        st.caption("Controlled comparison — production architecture: both sides see table "
                   "metadata plus 3 sample rows (never the full dataset), same model, question, instructions, and "
                   "3,000-token budget. Each writes SQL; DuckDB executes it. The stable context "
                   "(schemas + ontology) sits in the cached system prompt, as in production — "
                   "watch the cache-read numbers under each answer after the first question. "
                   "The honest framing: the left side is not semantics-free — the model "
                   "carries powerful IMPLICIT semantics from training and naming conventions. "
               "The governed side ALSO carries governed simulation tools (tools are "
               "part of the semantic contract), so the PRIMARY experimental "
               "difference is access to the governed semantic bundle. "
                   "The comparison is implicit semantics vs EXPLICIT governed semantics. "
                   "Institutional rules (try the reported-utilization question) are where "
                   "implicit hits its ceiling.")
        with st.expander("📖 Implicit vs Explicit Semantics — the one concept to take away"):
            st.markdown("""
    **Implicit semantics** is everything the model absorbed about freight from training —
    thousands of schemas, KPI dictionaries, and logistics docs. When it sees UTIL_PCT_1/2/3
    it doesn't *know* which is authoritative; it *guesses* from industry patterns, and it is
    a very good guesser. Free, powerful, and improving with every model generation.

    **Explicit semantics** is meaning that is *written down and governed*: your definitions,
    with owners, versions, and provenance. Not smarter — *governed and accountable*.

    | | Implicit (model priors) | Explicit (governed ontology) |
    |---|---|---|
    | Industry conventions ("the composite column", "dispatch date") | ✅ Usually guessed right | ✅ Governed contract (guides here; enforced at the compiler rung) |
    | YOUR institutional rules (Finance exclusions, frequency floors, hold policies) | ❌ **Not in any model's training — must be supplied** | ✅ The governed source |
    | Run-to-run consistency | 🎲 A coin flip that usually lands well | 📜 A written contract (fully consistent once compiled/enforced) |
    | Audit answer to "why this number?" | "The model inferred it" | Definition + owner + policy + version |

    **Why both sides sometimes tie:** on conventional questions, implicit semantics answers
    correctly for free — and this demo says so honestly. The ROI of an ontology concentrates
    on the definitions that are *yours*, because no model has ever trained on your company's
    internal policies and none ever will.

    **The strategic arrow:** implicit coverage *grows* with every model generation; your
    institutional knowledge is absent from every model unless your systems supply it.
    So the ties will get more common — and the value will concentrate *ever more* on the
    governed layer. The ontology is not a workaround for today's model weaknesses; it is
    the one part of the stack that better models can never replace.
    """)
        st.markdown("#### RAG step — semantic context retrieved for this question")
        if True:
            st.caption(f"Retrieval engine: {st.session_state.get('rag_engine', '')} — the "
                       "vector index is over the ONTOLOGY's definitions, not the data. Core "
                       "invariants (decodes, column authority, temporal rules) always ship; "
                       "these chunks were retrieved for this question. At 500+ metrics this "
                       "step is what keeps the prompt small — production swaps this in-memory "
                       "index for Databricks Vector Search.")
            for (cid, kind, text), score in st.session_state.get("rag_hits", []):
                st.markdown(f"**`{cid}`** · " + ("dependency-included"
                            if score < 0 else f"similarity {score:.3f}"))
                st.caption(text[:300] + ("…" if len(text) > 300 else ""))

        # ===== PARALLEL EXECUTION: both sides run CONCURRENTLY — wall-clock
        # equals the slower call, not the sum (~40% cut). Same prompts, model,
        # and budgets: the controlled comparison is untouched.
        _ec = st.session_state.get("exec_cache", {})
        _forced = st.session_state.pop("force_run", False)
        _stale = (_ec.get("q") != user_query or _ec.get("model") != MODEL_ID)
        _fresh = _forced or _stale or "raw_text" not in _ec
        _fresh_sem = _forced or _stale or "sem_text" not in _ec
        if _fresh or _fresh_sem:
            import concurrent.futures as _cf
            import time as _tm
            def _api_call(_sys, _side):
                return client.messages.create(
                    model=MODEL_ID, max_tokens=3000, system=_sys,
                    messages=build_messages(_side, user_query))
            _t_par = _tm.time()
            try:
              with st.spinner(f"Asking {MODEL_ID} — both sides running concurrently…"):
                with _cf.ThreadPoolExecutor(max_workers=2) as _ex:
                    _f_raw = _ex.submit(_api_call, raw_system, "raw") if _fresh else None
                    _f_sem = _ex.submit(_api_call, semantic_system, "sem") if _fresh_sem else None
                    if _stale or _forced:
                        st.session_state.exec_cache = {}
                    st.session_state.exec_cache.update({"q": user_query, "model": MODEL_ID})
                    if _f_raw is not None:
                        _rr = _f_raw.result()
                        _u = _rr.usage
                        st.session_state.exec_cache.update({
                            "raw_text": response_text_of(_rr),
                            "raw_usage": {"input_tokens": _u.input_tokens,
                                          "output_tokens": _u.output_tokens,
                                          "cache_creation_input_tokens": getattr(_u, "cache_creation_input_tokens", 0) or 0,
                                          "cache_read_input_tokens": getattr(_u, "cache_read_input_tokens", 0) or 0}})
                    if _f_sem is not None:
                        _rs = _f_sem.result()
                        _u2 = _rs.usage
                        st.session_state.exec_cache.update({
                            "sem_text": response_text_of(_rs),
                            "sem_usage": {"input_tokens": _u2.input_tokens,
                                          "output_tokens": _u2.output_tokens,
                                          "cache_creation_input_tokens": getattr(_u2, "cache_creation_input_tokens", 0) or 0,
                                          "cache_read_input_tokens": getattr(_u2, "cache_read_input_tokens", 0) or 0}})
            except Exception as _api_err:
                st.error(f"API call failed ({type(_api_err).__name__}): {_api_err} — "
                         "press the same question again; only the missing side will "
                         "re-run.")
                st.stop()
            st.session_state["_do_scroll"] = True
            _par_elapsed = _tm.time() - _t_par
            st.session_state.exec_cache["raw_elapsed"] = _par_elapsed
            st.session_state.exec_cache["sem_elapsed"] = _par_elapsed

        col1, col2 = st.columns(2)

        def _render_side(container, response_text, elapsed, usage, side_key, extra_note=""):
            """Shared rendering: explanation, generated SQL, validation, execution."""
            with container:
                sql = extract_sql(response_text)
                explanation = response_text.split("```")[0].strip()
                st.write(explanation)
                _tool_req = extract_tool(response_text) if side_key.startswith("sem") else None
                if _tool_req is not None:
                    st.code(json.dumps(_tool_req, indent=2), language="json")
                    st.caption("Governed TOOL request \u2014 routed to a deterministic "
                               "engine, not to SQL. The gate does not apply; the engine "
                               "itself enforces the ontology's eligibility rules.")
                    st.session_state[side_key] = explanation
                    ok, sql_or_reason = False, None
                    sql = None
                elif sql is None:
                    st.caption("Conversational answer — no query needed (answered from "
                               "the conversation context).")
                    st.session_state[side_key] = explanation
                    ok, sql_or_reason = False, None
                elif True:
                    ok, sql_or_reason = validate_sql(sql)
                if sql is not None and not ok:
                    st.error(f"Query failed the validation gate: {sql_or_reason}")
                    st.session_state[side_key] = explanation
                elif sql is not None:
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
                        st.markdown("**Executed result** — Claude wrote this SQL (the decision); "
                                    "the DuckDB engine ran it (the arithmetic; production: "
                                    "Databricks SQL):")
                        st.dataframe(result, hide_index=True, use_container_width=True)
                        st.session_state[side_key] = (explanation + "\n" + sql_or_reason
                                                      + "\n" + result.to_string(index=False))
                cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
                cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
                st.caption(f"⏱ {elapsed:.1f}s · {MODEL_ID} · tokens: {usage.input_tokens:,} fresh in, "
                           f"{cache_w:,} cache-write, {cache_r:,} cache-read (~10% price) / "
                           f"{usage.output_tokens:,} out (budget: 3,000 — same both sides"
                           f"{extra_note}). First question warms the cache; "
                           f"repeat questions reuse the identical stable prefix; retrieved "
                           f"slices are processed fresh each question.")

        with col1:
            st.subheader("Without Semantic Ontology")
            st.caption("Claude writes SQL from raw schemas alone")
            with st.spinner("Generating query..."):
                try:
                    t0 = time.time()
                    import types as _t2
                    _c = st.session_state.get("exec_cache", {})
                    if "raw_text" not in _c:
                        raise RuntimeError("No cached response — re-ask the question.")
                    class _RawResp: pass
                    response_raw = _RawResp()
                    response_raw.content = [_t2.SimpleNamespace(text=_c["raw_text"])]
                    response_raw.usage = _t2.SimpleNamespace(**_c["raw_usage"])
                    t0 = time.time() - _c.get("raw_elapsed", 0.0)
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
                    import types as _t3
                    _c2 = st.session_state.get("exec_cache", {})
                    if "sem_text" not in _c2:
                        raise RuntimeError("No cached semantic response — the raw side "
                                           "likely failed this run; re-ask the question.")
                    class _SemResp: pass
                    response_semantic = _SemResp()
                    response_semantic.content = [_t3.SimpleNamespace(text=_c2["sem_text"])]
                    response_semantic.usage = _t3.SimpleNamespace(**_c2["sem_usage"])
                    t0 = time.time() - _c2.get("sem_elapsed", 0.0)
                    sem_elapsed = time.time() - t0
                    response_text = response_semantic.content[0].text
                    if _fresh_sem:
                        _turns = st.session_state.setdefault("chat_turns", [])
                        _new_turn = {
                            "q": user_query,
                            "raw": _turn_summary(
                            st.session_state.get("exec_cache", {}).get(
                                "raw_text", "(baseline call unavailable)")),
                            "sem": _turn_summary(response_text),
                        }
                        if _turns and _turns[-1]["q"] == user_query:
                            _turns[-1] = _new_turn  # re-ask replaces, never duplicates
                        else:
                            _turns.append(_new_turn)
                        st.session_state["_turn_needs_result"] = True

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
                    _cur_sql = extract_sql(answer_text) or ""
                    sql_tables = sorted(set(
                        m2.lower() for m2 in re.findall(
                            r"(?i)\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", _cur_sql))
                        & ALLOWED_TABLES)
                    st.session_state.sem_physical_tables = sql_tables
                    # SEMANTIC INTENT: the metric's declared entities are primary;
                    # table-derived entities only as fallback when no metric declared
                    if used_metric and used_metric in ontology.get("metrics", {}):
                        used_entities = ontology["metrics"][used_metric].get(
                            "entities", used_entities)
                    elif not used_entities:
                        derived = []
                        for t in sql_tables:
                            for e in TABLE_ENTITIES.get(t, []):
                                if e not in derived:
                                    derived.append(e)
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
            st.caption(f"SEMANTIC INTENT (orange): the entities declared by the metric "
                       f"definition the model followed. PHYSICAL EVIDENCE: the executed SQL "
                       f"referenced table(s) "
                       f"{', '.join(st.session_state.get('sem_physical_tables', [])) or '—'}. "
                       f"Production derives dimensions/filters/joins via SQL AST parsing.")
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
    with st.expander("\u2705 Verified ground truth & automated fact check", expanded=False):
        st.header("Verified Ground Truth")
        _sem_cached = st.session_state.get("exec_cache", {}).get("sem_text", "")
        if _sem_cached and extract_tool(_sem_cached) is not None:
            st.info("This was a DETERMINISTIC SIMULATION turn \u2014 a governed engine "
                    "computed the result directly from the data; the engine IS the "
                    "ground truth, so there is no separate fact-check to run.")
        elif _sem_cached and extract_sql(_sem_cached) is None:
            st.info("This was a CONVERSATIONAL turn — the assistant answered from "
                    "prior context without executing a query, so there is no result "
                    "to fact-check. The stats below describe the underlying data for "
                    "reference; treat the conversational answer as reasoning, not as "
                    "a verified measurement.")
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

    # ---- PRIMARY CHAT ANSWER: answer first, visual when it helps, method last ----
    _pc = st.session_state.get("exec_cache", {})
    with _primary_slot:
        st.markdown("<div id='answer-anchor'></div>", unsafe_allow_html=True)
        with st.chat_message("user"):
            st.write(user_query)
        with st.chat_message("assistant"):
            _ptxt = _pc.get("sem_text", "")
            _pans = "\n".join(l for l in _ptxt.splitlines()
                              if not l.strip().startswith("TRACE:"))
            _pexpl = _pans.split("```")[0].strip()
            _ptool = extract_tool(_pans)
            if _ptool is not None:
                _tr = run_tool(_ptool)
                if _pexpl:
                    st.write(_pexpl)
                if "error" in _tr:
                    st.warning(_tr["error"])
                elif _tr.get("mode") == "direct_baseline":
                    _b1, _b2, _b3, _b4 = st.columns(4)
                    _b1.metric("Loads on lane", _tr["loads"])
                    _b2.metric("Avg utilization", f"{_tr['avg_util']}%")
                    _b3.metric("Cost basis", f"${_tr['total_cost_usd']:,}")
                    _b4.metric("Service std", f"{_tr['svc_days']}d")
                    st.caption(_tr["note"])
                elif _tr.get("mode") == "param_whatif":
                    st.markdown(f"**Governed parameter what-if:** `{_tr['target']}` "
                                f"{_tr['original']} \u2192 {_tr['tested']} (tested, then restored)")
                    _pw = pd.DataFrame([
                        dict(scenario="current (governed)", **_tr["before"]),
                        dict(scenario=f"tested ({_tr['tested']})", **_tr["after"])])
                    st.dataframe(_pw, hide_index=True, use_container_width=True)
                    st.caption("The ontology's governed value is UNCHANGED \u2014 this "
                               "tested the parameter and restored it. Changing it for "
                               "real is a governance decision by the parameter's owner.")
                else:
                    _q1, _q2, _q3, _q4 = st.columns(4)
                    _q1.metric("Network avg utilization", f"{_tr['after_avg_util']}%",
                               delta=f"{round(_tr['after_avg_util'] - _tr['before_avg_util'], 1)} pts")
                    _q2.metric("Moves", f"{_tr['new_leg_loads']} leg loads",
                               delta=f"{_tr['net_moves_delta']:+d} vs {_tr['moved_loads']} direct",
                               delta_color="inverse")
                    _q3.metric("Est. cost delta", f"${_tr['cost_delta_usd']:+,}", delta_color="off")
                    _q4.metric("Service", f"{_tr['svc_path_days']}d via",
                               delta=f"vs {_tr['svc_direct_days']}d direct", delta_color="off")
                    if not _tr.get("service_ok", True):
                        st.warning("SERVICE CHECK: via-path exceeds the direct standard.")
                st.caption("\u2699\uFE0F Deterministic simulation \u2014 a governed engine "
                           "computed this; the model only chose WHAT to simulate.")
                if st.session_state.pop("_turn_needs_result", False):
                    _t3b = st.session_state.get("chat_turns", [])
                    if _t3b and _t3b[-1]["q"] == user_query:
                        _t3b[-1]["sem"] += "\nSIMULATION RESULT: " + str(_tr)[:600]
                _psql = None
            else:
                _psql = extract_sql(_pans)
            _pres, _perr, _pok = None, None, False
            if _psql:
                _pok, _pbody = validate_sql(_psql)
                if _pok:
                    _pres, _perr = run_sql(_pbody)
                else:
                    _perr = _pbody

            def _fmtv(c, v):
                cl = c.lower()
                if isinstance(v, float):
                    if "util" in cl or "pct" in cl:
                        return f"{v:,.1f}%"
                    if "usd" in cl or "saving" in cl or "cost" in cl:
                        return f"${v:,.0f}"
                    return f"{v:,.2f}"
                if isinstance(v, int) and ("usd" in cl or "saving" in cl):
                    return f"${v:,}"
                return f"{v:,}" if isinstance(v, int) else str(v)

            if _ptool is not None:
                pass  # tool result already rendered above
            elif _psql and _perr is None and _pres is not None and len(_pres):
                if len(_pres) == 1:
                    st.markdown("### " + "  \u00b7  ".join(
                        f"{c.replace('_', ' ')}: **{_fmtv(c, _pres.iloc[0][c])}**"
                        for c in _pres.columns[:4]))
                else:
                    _lead = ", ".join(
                        f"{c.replace('_', ' ')} {_fmtv(c, _pres.iloc[0][c])}"
                        for c in _pres.columns[:3])
                    st.markdown(f"**{len(_pres)} result rows \u2014 leading: {_lead}**")
                _num = [c for c in _pres.columns
                        if pd.api.types.is_numeric_dtype(_pres[c])]
                _lab = [c for c in _pres.columns if c not in _num]
                if len(_pres) >= 3 and _num and _lab:
                    try:
                        _series = _pres.set_index(_lab[0])[_num[0]]
                        if any(k in _lab[0].lower() for k in ("week", "date", "dt")):
                            st.line_chart(_series)
                        else:
                            st.bar_chart(_series)
                    except Exception:
                        pass
                st.dataframe(_pres, hide_index=True, use_container_width=True)
            elif _psql and _perr is None and _pres is not None:
                st.caption("The query matched no rows \u2014 this can itself be the "
                           "finding (e.g., no same-day consolidation pairs in this "
                           "scope). For improvement questions, the assistant's "
                           "follow-up offer below runs the full governed diagnostic: "
                           "root cause, frequency signals, and directional balance.")
            elif _psql:
                st.error(f"{'Execution error' if _pok else 'Validation gate'}: {_perr}")
            elif _ptool is None:
                st.caption("Conversational answer \u2014 answered from the conversation "
                           "context; no query needed.")
            if _pexpl:
                st.caption("Method: " + _pexpl[:350]
                           + ("\u2026" if len(_pexpl) > 350 else ""))
            if (_pres is not None and _perr is None
                    and st.session_state.pop("_turn_needs_result", False)):
                _t2 = st.session_state.get("chat_turns", [])
                if _t2 and _t2[-1]["q"] == user_query:
                    _t2[-1]["sem"] += ("\nRESULT (top rows): "
                                       + str(_pres.head(5).to_dict("records"))[:600])
            _sg1, _sg2 = st.columns(2)
            if _sg1.button("\U0001F4CA Break that down by lane", key="sugg_lane"):
                st.session_state.selected_query = "Break that down by lane"
                st.session_state.is_preset = False
                st.session_state.force_run = True
                st.rerun()
            if _sg2.button("\U0001F4C8 Show the weekly trend", key="sugg_trend"):
                st.session_state.selected_query = "How has utilization trended week over week?"
                st.session_state.is_preset = True
                st.session_state.force_run = True
                st.rerun()
            st.caption("\U0001F447 Full evidence in the expander below: with/without "
                       "comparison, retrieval, gate, ground truth, verdict.")
    if st.session_state.pop("_do_scroll", False):
        components.html(
            "<script>try{window.parent.document.getElementById('answer-anchor')"
            ".scrollIntoView({behavior:'smooth',block:'start'});}catch(e){}</script>",
            height=0)
    # ---- COST & SPEED: plain-language economics of this answer ----
    _cu_r = st.session_state.get("exec_cache", {}).get("raw_usage")
    _cu_s = st.session_state.get("exec_cache", {}).get("sem_usage")
    if _cu_r and _cu_s:
        with st.container(border=True):
            st.markdown("**\U0001F4B5 What this answer cost — with vs without governed semantics**")
            _cost_df = pd.DataFrame([
                {"side": "Without (baseline)",
                 "time_s": round(st.session_state.exec_cache.get("raw_elapsed", 0), 1),
                 "fresh_input_tokens": _cu_r.get("input_tokens", 0),
                 "cached_input_tokens": _cu_r.get("cache_read_input_tokens", 0),
                 "cache_write_tokens": _cu_r.get("cache_creation_input_tokens", 0),
                 "output_tokens": _cu_r.get("output_tokens", 0),
                 "est_cost_usd": round(side_cost_usd(_cu_r), 4)},
                {"side": "With governed semantics",
                 "time_s": round(st.session_state.exec_cache.get("sem_elapsed", 0), 1),
                 "fresh_input_tokens": _cu_s.get("input_tokens", 0),
                 "cached_input_tokens": _cu_s.get("cache_read_input_tokens", 0),
                 "cache_write_tokens": _cu_s.get("cache_creation_input_tokens", 0),
                 "output_tokens": _cu_s.get("output_tokens", 0),
                 "est_cost_usd": round(side_cost_usd(_cu_s), 4)},
            ])
            # cost alone is half the story: attach correctness when we can verify it
            try:
                if user_query in PRESET_QUESTIONS:
                    _vf = PRESET_QUESTIONS[user_query]()[3]
                    _ec2 = st.session_state.get("exec_cache", {})
                    # evaluate the SAME rendered outputs the verdict uses (they
                    # include executed results), falling back to the raw responses
                    _rt = st.session_state.get("raw_out") or _ec2.get("raw_text", "")
                    _st_ = st.session_state.get("sem_out") or _ec2.get("sem_text", "")
                    _n_raw = sum(1 for _, ok in check_facts(_vf, _rt) if ok)
                    _n_sem = sum(1 for _, ok in check_facts(_vf, _st_) if ok)
                    _cost_df["verified_facts"] = [f"{_n_raw}/{len(_vf)}", f"{_n_sem}/{len(_vf)}"]
                    _cost_df["cost_per_correct_fact"] = [
                        (f"${_cost_df.iloc[0]['est_cost_usd'] / _n_raw:.4f}" if _n_raw else "∞ (none correct)"),
                        (f"${_cost_df.iloc[1]['est_cost_usd'] / _n_sem:.4f}" if _n_sem else "∞ (none correct)"),
                    ]
            except Exception:
                pass
            st.dataframe(_cost_df, hide_index=True, use_container_width=True)
            _delta_c = round(_cost_df.iloc[1]["est_cost_usd"] - _cost_df.iloc[0]["est_cost_usd"], 4)
            st.caption(f"The baseline is always cheaper PER CALL — it sends ~15 UNCACHED tokens (its schemas ride the cache too) and "
                       f"gets a guess; the governed side sends the company's rules and gets "
                       f"a compliant answer. The difference (${_delta_c} here, ≈ "
                       f"${_delta_c * 1_000_000:,.0f} per MILLION questions) is the "
                       f"governance premium — compare it to the cost of one wrong number "
                       f"in a finance deck. The honest metric is cost per CORRECT answer: "
                       f"on institutional questions the baseline cannot be correct at any "
                       f"price. And production runs ONLY the governed side, so the "
                       f"comparison itself is demo-only.")
            st.caption(f"Estimated at indicative {MODEL_ID} rates — edit PRICING in "
                       "app.py and verify current rates at anthropic.com/pricing. "
                       "Cached input bills at ~10% of fresh input, which is why "
                       "repeat questions get cheaper. Both sides run only in this "
                       "comparison demo — PRODUCTION runs the governed side alone, "
                       "so the right-hand row is the real production cost per question.")

# ===============================================================
# CONVERSATIONAL ACTION OFFER: the assistant proposes the next step,
# scoped to the question's context. Production: an MCP tool call
# (get_diagnostic(scope)); here, orchestration invokes the engine.
# ===============================================================
_lq = st.session_state.get("last_user_query", "")
if _lq and any(w in _lq.lower() for w in ["utilization", "cube", "volume", "trailer",
                                          "move", "consolidat", "improve", "opportun",
                                          "lane", "terminal"]):
    _NAME_TO_CODE = {v.lower(): k for k, v in TERMINAL_NAMES.items()}
    _turns = st.session_state.get("chat_turns", [])
    _scan = _lq.lower() + " " + (_turns[-1]["sem"].lower() if _turns else "")
    _found = []
    for _nm, _cd in _NAME_TO_CODE.items():
        _pos = _scan.find(_nm)
        if _pos < 0:
            for _tok in (f" {_cd.lower()} ", f"({_cd.lower()})"):
                _p2 = f" {_scan} ".find(_tok)
                if _p2 >= 0:
                    _pos = _p2
                    break
        if _pos >= 0:
            _found.append((_pos, _cd))
    _matches = [cd for _p, cd in sorted(_found)]  # ordered by first mention
    _offer_box = st.container(border=True)
    if len(_matches) > 1:
        _choice = _offer_box.radio("Multiple terminals mentioned — scope the analysis to:",
                           [TERMINAL_NAMES[c] for c in _matches] + ["Network-wide"],
                           horizontal=True, key="scope_choice")
        _scope_code = (None if _choice == "Network-wide"
                       else {v: k for k, v in TERMINAL_NAMES.items()}[_choice])
    else:
        _scope_code = _matches[0] if _matches else None
    _scope_label = f" for {TERMINAL_NAMES[_scope_code]}-origin lanes" if _scope_code else " (network-wide)"
    with _offer_box:
        st.markdown("### 💬 Assistant follow-up")
        st.markdown(f"**Want me to check for improvement opportunities{_scope_label}?** "
                    f"I'll run the governed diagnostic — root cause first, then the moves.")
    if _offer_box.button(f"🔍 Yes — analyze opportunities{_scope_label}",
                         type="primary", use_container_width=True):
        _sdg = utilization_diagnostic(orig=_scope_code)
        if _sdg['total_n'] == 0:
            st.info("No loads in that scope this period.")
        else:
            _o1, _o2, _o3, _o4 = st.columns(4)
            _o1.metric("Scope avg utilization", f"{_sdg['current']}%")
            _o2.metric("Achievable (planning est.)", f"{_sdg['achievable']}%",
                       delta=f"+{_sdg['uplift']} pts")
            _o3.metric("Moves saved",
                       int(_sdg['moves']['moves_saved'].sum()) if len(_sdg['moves']) else 0)
            _o4.metric("Est. saving", f"${_sdg['total_usd']:,}")
            st.caption(f"Scope: {_sdg['total_n']} loads — capacity-constrained "
                       f"(≥ governed thresholds): {_sdg['capacity_constrained_n']} "
                       f"({_sdg['weighed_out_n']} weighed-out, {_sdg['cubed_out_n']} "
                       f"cubed-out — effectively full; density/mix lever, not planning). "
                       f"Weight-DOMINANT but below threshold: "
                       f"{_sdg['weight_dominant_n'] - _sdg['weighed_out_n']} — "
                       f"underutilized and potentially addressable. Service-protection "
                       f"(policy): {_sdg['service_prot_n']}. Planning estimate; owner: "
                       f"Linehaul load planning.")
            if len(_sdg['moves']):
                st.dataframe(_sdg['moves'], hide_index=True, use_container_width=True)
            _fr = find_frequency_candidates()
            if _scope_code:
                _fr = _fr[_fr['lane'].str.startswith(TERMINAL_NAMES[_scope_code])]
            if len(_fr):
                st.markdown("**Preliminary schedule-review signals** — scoped to the "
                            "day-of-week grain where samples permit:")
                st.dataframe(_fr, hide_index=True, use_container_width=True)
            _imb = lane_imbalance(orig=_scope_code)
            if len(_imb):
                st.markdown("**Directional balance** — the load requirement follows "
                            "the MAX direction; the gap is a volume-imbalance proxy for "
                            "potential repositioning exposure:")
                st.dataframe(_imb, hide_index=True, use_container_width=True)
                st.caption("Per the governed directional_balance rule: an imbalanced "
                           "backhaul is STRUCTURAL — 'consolidate harder' is the wrong "
                           "lever. Governed action: backhaul_rebalance (owner: Linehaul "
                           "network planning) — rerouting/triangulation via terminals "
                           "with opposite-direction demand; impact proxy: imbalance × "
                           "lane miles × CPM in avoided empty repositioning.")
            st.session_state.setdefault("chat_turns", []).append({
                "q": f"[assistant offer accepted] Improvement opportunities{_scope_label}",
                "raw": f"Scoped diagnostic: {_sdg['current']}% current, "
                       f"{_sdg['achievable']}% achievable, ${_sdg['total_usd']:,} est.",
                "sem": f"Scoped diagnostic{_scope_label}: current {_sdg['current']}%, "
                       f"achievable {_sdg['achievable']}% (+{_sdg['uplift']} pts), "
                       f"{_sdg['weighed_out_n']} weighed-out, "
                       f"{_sdg['service_prot_n']} service-protection, "
                       f"est. saving ${_sdg['total_usd']:,}.",
            })
            st.caption("This analysis is now in the chat context — ask a follow-up "
                       "about it below.")

with st.expander("\U0001F500 What-if: reroute a lane (deterministic simulation — no LLM)", expanded=False):
    st.caption("Per the governed reroute_whatif action: freight from the direct lane "
               "repacks onto existing leg loads within capacity (never onto "
               "capacity-constrained loads); overflow opens new loads. Service is "
               "CHECKED, not assumed. Timing, doors, and via-terminal handling are "
               "not modeled — planning estimate.")
    _w1, _w2, _w3, _w4 = st.columns([2, 2, 2, 1])
    _codes = list(TERMINAL_NAMES.keys())
    _from = _w1.selectbox("From", _codes, index=_codes.index("SGF"),
                          format_func=lambda c: f"{TERMINAL_NAMES[c]} ({c})")
    _via = _w2.selectbox("Via", _codes, index=_codes.index("HAR"),
                         format_func=lambda c: f"{TERMINAL_NAMES[c]} ({c})")
    _to = _w3.selectbox("To", _codes, index=_codes.index("MEM"),
                        format_func=lambda c: f"{TERMINAL_NAMES[c]} ({c})")
    _w4.markdown("<div style='height:1.7em'></div>", unsafe_allow_html=True)
    if _w4.button("Run", type="primary", use_container_width=True):
        if len({_from, _via, _to}) < 3:
            st.warning("Pick three different terminals.")
        else:
            _rw = reroute_whatif(_from, _via, _to)
            if "error" in _rw:
                st.info(_rw["error"])
            else:
                _m1, _m2, _m3, _m4 = st.columns(4)
                _m1.metric("Network avg utilization",
                           f"{_rw['after_avg_util']}%",
                           delta=f"{round(_rw['after_avg_util'] - _rw['before_avg_util'], 1)} pts vs {_rw['before_avg_util']}%")
                _m2.metric("Linehaul moves", f"{_rw['new_leg_loads']} leg loads",
                           delta=f"{_rw['net_moves_delta']:+d} vs {_rw['moved_loads']} direct",
                           delta_color="inverse")
                _m3.metric("Est. cost delta", f"${_rw['cost_delta_usd']:+,}",
                           delta_color="off")
                _m4.metric("Service", f"{_rw['svc_path_days']}d via path",
                           delta=f"vs {_rw['svc_direct_days']}d direct",
                           delta_color="off")
                if not _rw["service_ok"]:
                    st.warning(f"\u26A0\uFE0F SERVICE CHECK: the via-path standard "
                               f"({_rw['svc_path_days']}d) exceeds the direct standard "
                               f"({_rw['svc_direct_days']}d) — this reroute would need a "
                               f"service exception or apply only to non-time-critical freight.")
                else:
                    st.success("Service check passed: the via path meets the direct standard.")
                st.session_state.setdefault("chat_turns", []).append({
                    "q": f"[what-if] Reroute {_from}->{_to} via {_via}",
                    "raw": "Deterministic reroute simulation (no LLM).",
                    "sem": (f"Reroute what-if {TERMINAL_NAMES[_from]}->{TERMINAL_NAMES[_to]} "
                            f"via {TERMINAL_NAMES[_via]}: network util "
                            f"{_rw['before_avg_util']}% -> {_rw['after_avg_util']}%, "
                            f"moves {_rw['moved_loads']} direct -> {_rw['new_leg_loads']} leg loads "
                            f"(net {_rw['net_moves_delta']:+d}), cost delta "
                            f"${_rw['cost_delta_usd']:+,}, service {_rw['svc_path_days']}d "
                            f"via vs {_rw['svc_direct_days']}d direct "
                            f"({'OK' if _rw['service_ok'] else 'FAILS service standard'}). "
                            f"Planning estimate: handling/timing unmodeled."),
                })
                st.caption("This simulation is now in the chat context — ask a "
                           "follow-up about it below.")

_chatq = st.chat_input("Ask a question or follow up — context carries automatically…")
if _chatq and _chatq.strip():
    st.session_state.selected_query = _chatq.strip()
    st.session_state.is_preset = _chatq.strip() in PRESET_QUESTIONS
    st.session_state.force_run = True
    st.rerun()  # deliberate: the run block above already executed this cycle

# ===============================================================
# TECHNICAL APPENDIX: architecture + the live semantic model
# ===============================================================
st.header("Technical Appendix")
with st.expander("📊 Network KPI snapshot & dashboard", expanded=False):
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
st.caption("The proof lives above; the plumbing lives here.")


with st.expander("Interactive Knowledge Graph: the Ontology Behind the Scenes", expanded=False):
    st.caption(f"Generated directly from this POC's semantic definitions in ontology.py — "
               f"{len(ontology['entities'])} entities, {len(ontology.get('metrics', {}))} metrics, "
               f"{len(ontology.get('actions', {}))} actions, {len(ontology.get('playbooks', {}))} playbook. "
               "Drag nodes, zoom with scroll, hover for definitions. A production implementation "
               "would store and govern these definitions in an enterprise semantic registry — "
               "the standalone Neo4j loader in this repo compiles the same file to a property "
               "graph today — and may expose selected relationships through a graph interface.")
    kg_legend(mode="full")
    render_kg(build_kg(), "kg_full.html")


with st.expander("Architecture: How the Semantic Layer Works", expanded=False):
    tab_a, tab_b, tab_c, tab_d, tab_e = st.tabs(["The Context Layer", "Production Flow: Delegated Computation", "RAG over the Ontology", "The Semantic Contract", "POC → Production Stack"])
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
    with tab_d:
        components.html(V2_CONTRACT, height=400, scrolling=True)
        st.caption("Current vs target, labeled honestly: IMPLEMENTED in this POC — RAG "
                   "prompt slices, parameter-driven action engines (eligibility thresholds "
                   "read from the ontology), and the property graph via the standalone "
                   "Neo4j loader. ROADMAP — deterministic semantic compilation of metric "
                   "SQL, and governed metrics/action APIs exposed to agents through MCP "
                   "(MCP is the interface protocol; the semantic API governs; backend "
                   "code computes).")
    with tab_e:
        components.html(V3_STACK, height=370, scrolling=True)


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
  <text x="245" y="178" text-anchor="middle" font-size="12" fill="#7a1010">Metadata + 3 sample rows. No definitions.</text>
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
  <text x="735" y="472" text-anchor="middle" font-size="13" font-weight="bold" fill="#1a2e05">Executed result matches independent ground truth</text>
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
    model=MODEL_ID, max_tokens=3000,
    messages=[{"role": "user", "content": prompt + "\\n\\nQuestion: " + user_query}]
)
sql = extract_sql(response_text_of(response))  # never content[0]: thinking blocks come first

# 2. GOVERN: validation gate — read-only, single statement, allowed tables
ok, sql = validate_sql(sql)

# 3. COMPUTE: the engine executes; digit-perfect numbers
con = duckdb.connect()
con.register("trlr_util_fct", utilization_df)
result = con.execute(sql).df()'''

with st.expander("For Developers: How This Actually Works"):

    st.markdown("""
#### The stack

Current stack (streamlit, pandas, numpy, anthropic, pyvis, duckdb, fastembed, scikit-learn):

| Library | Role |
|---|---|
| `pandas` | Loads the CSVs, computes the ground-truth panel |
| `anthropic` | Official Python SDK for the Claude API |
| `streamlit` | The UI you are looking at |
| `json` (stdlib) | Serializes the ontology dict into the prompt |

The ontology itself is a **plain Python dictionary** in `ontology.py` - entities,
relationships, business rules, metric definitions with step-by-step computation
logic, and query patterns. An in-memory vector index over the ontology (fastembed/TF-IDF); no fine-tuning.

#### How the ontology reaches Claude — and what Claude returns

The ontology travels as **plain text inside the prompt** (serialized with
`json.dumps()`). What comes back is not an answer — it is a **SQL query**. The
LLM interprets; DuckDB computes; the platform validates in between:
""")

    st.code(DEV_SNIPPET, language="python")

    st.markdown("""
So: **the ontology goes to the model as instructions plus reference material in
the message content** — as an always-on core plus RAG-RETRIEVED slices from an
in-memory vector index (fastembed embeddings, TF-IDF lexical fallback), with
reference-following so a retrieved pattern always brings its metric/action and
governing rule. The ontology GUIDES generation — the model can still misapply
it, which is exactly what the verdict evaluates and why production graduates to
a semantic compiler that ENFORCES. (In production: stable prefix cached in the
`system` parameter, retrieved tail fresh; Databricks SQL warehouse instead of
DuckDB; identity, Unity Catalog entitlements, and row/column security enforced
by the platform.)

The "without ontology" call is identical in model, question, schemas, sample
rows, generic instructions, and token budget — the only difference is the
governed semantic bundle (always-on core + retrieved slices).

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
| 1 | **Free-form query generation** *(this app)* | SQL text | LLM, GUIDED by curated metadata/instructions/examples; a gate checks structure, not semantics | Databricks Genie, Snowflake Cortex Analyst |
| 2 | **Governed semantic-layer API** | A structured request: `{metric, group_by, filters, grain}` | The semantic layer — it compiles to SQL; wrong columns aren't on the menu | dbt Semantic Layer, Cube, Looker, Fabric semantic models |
| 3 | **Graph-native / typed objects** | Cypher/SPARQL, or typed function calls | The graph platform — traversals precompiled from declared links | Neo4j + LLM, Palantir Foundry/AIP |
| 4 | **Tool use via MCP** | A tool call: `get_lane_utilization(order='asc')` | Deterministic code behind each tool (usually pattern 2 underneath) | Custom MCP servers, agent platforms |

Note what happens to "traversal" down the ladder: in pattern 1 the LLM *reasons
about* joins per question; by patterns 2–4 the ontology *defines* the traversals
once and the LLM merely selects an entry point. Choosing the wrong column stops
being a mistake the model can make — it is prevented by construction, the same way
delegating computation made arithmetic errors impossible.

**Pattern 2 is generally the preferred default for governed KPI retrieval, exposed
through pattern 4 for conversational access** — free-form SQL stays useful for
controlled exploration; graph and tool patterns serve different question and action classes. Why: structured requests are
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
of max(). One edit in ontology.py corrected the definition every consumer compiles from (answers heal on their next run). In a schema-only
world that error lives on in a thousand ad-hoc queries. *In this app:* the max() rule in
the business rules, and this story.

**5. Auditability — "why this number?" has a mechanical answer.**
Every semantic answer carries a trace (which metric definition, which entities) and the
generated SQL can be reviewed against the metric's reference SQL. *In this app:* the TRACE-driven
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

with st.expander("\U0001F4C4 Sample Data", expanded=False):
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
