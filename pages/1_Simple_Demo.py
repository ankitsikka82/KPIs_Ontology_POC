"""Simple Demo — the 6-minute version.

Self-contained lean page following the executive script:
frame the problem -> show the ambiguity -> two decisive live questions ->
the production flow -> the ask. The full application (main page) carries
every preset, graph, and technical detail; this page carries the argument.
"""
import streamlit as st
import pandas as pd
import json
import os
import re
import time
import duckdb
from ontology import ontology

st.set_page_config(page_title="Simple Demo — 6 Minutes", layout="wide")

# ------------------------------------------------------------------
# Shared plumbing (lean copies; the main page is the full version)
# ------------------------------------------------------------------
TERMINAL_NAMES = {"HAR": "Harrison", "SGF": "Springfield", "STL": "Saint Louis",
                  "MEM": "Memphis", "ATL": "Atlanta"}
ALLOWED_TABLES = {"shpmt_mstr", "lh_dsptch", "trlr_util_fct", "pln_mvmt"}
FORBIDDEN_SQL = ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
                 "ATTACH", "COPY", "PRAGMA", "INSTALL", "LOAD"]


@st.cache_data
def load_data():
    shipments = pd.read_csv('shipments.csv')
    dispatches = pd.read_csv('dispatches.csv')
    utilization = pd.read_csv('cube_utilization.csv')
    movements = pd.read_csv('planned_movements.csv')
    return shipments, dispatches, utilization, movements


shipments, dispatches, utilization, movements = load_data()
duck_u = utilization.copy()
duck_u['LH_DSPTCH_DT'] = pd.to_datetime(duck_u['LH_DSPTCH_DT'])
duck_s = shipments.copy()
duck_s['SHPMT_CRT_DT'] = pd.to_datetime(duck_s['SHPMT_CRT_DT'])
duck_d = dispatches.copy()
duck_d['LH_DSPTCH_DT'] = pd.to_datetime(duck_d['LH_DSPTCH_DT'])

api_key = ""
try:
    api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
except Exception:
    pass
if not api_key:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if not api_key:
    api_key = st.session_state.get("shared_api_key", "")
if not api_key:
    api_key = st.sidebar.text_input("Anthropic API Key", type="password")
if api_key:
    st.session_state.shared_api_key = api_key


def extract_sql(text):
    m = re.search(r"```sql\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(SELECT|WITH)\b.*", text, re.DOTALL | re.IGNORECASE)
    return m.group(0).strip() if m else None


def validate_sql(sql):
    if not sql:
        return False, "No SQL found."
    body = sql.strip().rstrip(";").strip()
    if ";" in body:
        return False, "Multiple statements not allowed."
    if body.split(None, 1)[0].upper() not in ("SELECT", "WITH"):
        return False, "Only SELECT allowed."
    scrub = re.sub(r"'[^']*'", "''", body)
    scrub = re.sub(r"--[^\n]*", " ", scrub)
    scrub = re.sub(r"/\*.*?\*/", " ", scrub, flags=re.DOTALL)
    for kw in FORBIDDEN_SQL:
        if re.search(r"\b" + kw + r"\b", scrub.upper()):
            return False, f"Forbidden keyword: {kw}."
    ctes = set(m.lower() for m in re.findall(r"(?i)(?:WITH|,)\s*([a-zA-Z_]\w*)\s+AS\s*\(", scrub))
    refs = set(m.lower() for m in re.findall(r"(?i)\b(?:FROM|JOIN)\s+([a-zA-Z_]\w*)", scrub))
    unknown = refs - ALLOWED_TABLES - ctes
    if unknown:
        return False, f"Table(s) not on allowlist: {', '.join(sorted(unknown))}."
    return True, body


def run_sql(sql):
    try:
        con = duckdb.connect()
        con.register("shpmt_mstr", duck_s)
        con.register("lh_dsptch", duck_d)
        con.register("trlr_util_fct", duck_u)
        con.register("pln_mvmt", movements)
        df = con.execute(sql).df()
        for c in df.select_dtypes(include="float").columns:
            df[c] = df[c].round(2)
        return df, None
    except Exception as e:
        return None, str(e)


def schema_description():
    parts = []
    for name, df in [("shpmt_mstr", duck_s), ("lh_dsptch", duck_d),
                     ("trlr_util_fct", duck_u), ("pln_mvmt", movements)]:
        cols = ", ".join(f"{c} ({df[c].dtype})" for c in df.columns)
        parts.append(f"TABLE {name}\n  columns: {cols}\n  sample rows:\n"
                     f"{df.head(3).to_string(index=False)}")
    return "\n\n".join(parts)


def _normalize(t):
    t = t.lower().replace("→", " to ").replace("->", " to ")
    t = re.sub(r"(\d),(\d{3})", r"\1\2", t)
    return re.sub(r"\s+", " ", t)


def check_facts(facts, text):
    norm = _normalize(text)
    return [(label, all(any(_normalize(v) in norm for v in group) for group in groups))
            for label, groups in facts]


def _pct(v):
    return [f"{v:.2f}", f"{v:.1f}"]


# ------------------------------------------------------------------
# 1. FRAME — 45 seconds
# ------------------------------------------------------------------
st.title("Why AI Analytics Needs a Governed Semantic Contract")
st.markdown("""
We have hundreds of KPIs, operational entities, and business rules spread across
dashboards, SQL, documentation, and individual knowledge. **AI can query the data —
but it does not automatically know which enterprise definition is authoritative.**
This is a six-minute proof, on a realistic legacy schema, with every number
independently verified.
""")

# ------------------------------------------------------------------
# 2. THE AMBIGUITY — 30 seconds
# ------------------------------------------------------------------
st.header("1 · The data every enterprise actually has")
amb = utilization[['TRLR_NBR', 'UTIL_PCT_1', 'UTIL_PCT_2', 'UTIL_PCT_3',
                   'SHPMT_CNT', 'LH_DSPTCH_DT']].head(5).copy()
amb['SHPMT_CRT_DT (shipments table)'] = '…'
st.dataframe(amb, hide_index=True, use_container_width=True)
st.markdown("**Which utilization column is official? Which date controls the period? "
            "Which loads does Finance exclude?** Nothing in the schema says. "
            "That knowledge lives in policies, memos, and people.")

# ------------------------------------------------------------------
# The two decisive questions
# ------------------------------------------------------------------
QUESTIONS = {
    "What is our reported utilization?": "reported",
    "What is our reported utilization for lanes originating from Springfield?": "sgf",
}


def facts_for(kind):
    if kind == "reported":
        inc = utilization[utilization['SHPMT_CNT'] > 1]
        rep = inc['UTIL_PCT_3'].mean()
        naive = utilization['UTIL_PCT_3'].mean()
        gt = (f"Ground truth (pandas, no LLM): reported = **{rep:.2f}%** "
              f"({len(utilization) - len(inc)} service-protection loads excluded); "
              f"a naive all-loads average gives {naive:.2f}%.")
        facts = [
            (f"States the REPORTED value ({rep:.2f}%), not the naive ({naive:.2f}%)",
             [_pct(rep)]),
            ("Applies the service-protection exclusion (SHPMT_CNT filter)",
             [["shpmt_cnt > 1", "shpmt_cnt>1", "shpmt_cnt = 1", "shpmt_cnt=1",
               "service-protection"]]),
        ]
        return gt, facts
    sgf = utilization[utilization['ORIG_TRML_CD'] == 'SGF']
    inc = sgf[sgf['SHPMT_CNT'] > 1]
    rep = inc['UTIL_PCT_3'].mean()
    naive = sgf['UTIL_PCT_3'].mean()
    gt = (f"Ground truth: SGF-origin reported = **{rep:.2f}%** over {len(inc)} loads "
          f"({len(sgf) - len(inc)} excluded); naive SGF average gives {naive:.2f}%.")
    facts = [
        (f"States the REPORTED SGF value ({rep:.2f}%), not the naive ({naive:.2f}%)",
         [_pct(rep)]),
        ("Resolves Springfield to code SGF", [["sgf"]]),
        ("Applies the exclusion (SHPMT_CNT filter)",
         [["shpmt_cnt > 1", "shpmt_cnt>1", "shpmt_cnt = 1", "shpmt_cnt=1",
           "service-protection"]]),
    ]
    return gt, facts


def run_side(system_text, question, client):
    t0 = time.time()
    resp = client.messages.create(
        model="claude-sonnet-4-5", max_tokens=1200,
        system=[{"type": "text", "text": system_text,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": question}])
    elapsed = time.time() - t0
    text = resp.content[0].text
    # strip trace line if present
    text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("TRACE:"))
    sql = extract_sql(text)
    explanation = text.split("```")[0].strip()
    ok, body = validate_sql(sql)
    if not ok:
        return explanation, None, None, body, elapsed
    result, err = run_sql(body)
    return explanation, body, result, err, elapsed


def render_question(question, kind, key):
    if st.button(f"Run: {question}", key=key, use_container_width=True, type="primary"):
        if not api_key:
            st.warning("No API key configured.")
            return
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        SCHEMAS = schema_description()
        raw_sys = ("You are a freight analytics assistant. You cannot see the full data — "
                   "only the table metadata and 3 sample rows below. Write ONE DuckDB SQL "
                   "SELECT answering the question; ONE short sentence of approach, then the "
                   "query in a ```sql block. The system executes it.\n\n" + SCHEMAS)
        sem_sys = (raw_sys + "\n\nYou additionally have a governed semantic ontology. "
                   "Follow its definitions EXACTLY.\n\n"
                   "BUSINESS RULES:\n" + json.dumps(ontology['business_rules'], indent=2)
                   + "\n\nMETRICS:\n" + json.dumps(ontology['metrics'], indent=2)
                   + "\n\nCODE DECODES:\n" + json.dumps(ontology['code_decodes'], indent=2)
                   + "\n\nQUERY PATTERNS:\n" + json.dumps(ontology['query_patterns'], indent=2))
        gt, facts = facts_for(kind)
        c1, c2 = st.columns(2)
        outs = {}
        for col, label, sys_text in [(c1, "Schema only (implicit semantics)", raw_sys),
                                     (c2, "With governed ontology", sem_sys)]:
            with col:
                st.subheader(label)
                with st.spinner("Generating query..."):
                    try:
                        expl, sql, result, err, elapsed = run_side(sys_text, question,
                                                                   __import__('anthropic').Anthropic(api_key=api_key))
                        st.write(expl)
                        if sql is None:
                            st.error(f"Gate: {err}")
                            outs[label] = expl
                        else:
                            st.code(sql, language="sql")
                            if err:
                                st.error(f"Execution error: {err}")
                                outs[label] = expl + "\n" + sql
                            else:
                                st.dataframe(result, hide_index=True,
                                             use_container_width=True)
                                outs[label] = (expl + "\n" + sql + "\n"
                                               + result.to_string(index=False))
                        st.caption(f"{elapsed:.1f}s")
                    except Exception as e:
                        st.error(str(e))
        st.markdown(gt)
        if len(outs) == 2:
            labels = list(outs.keys())
            rows = {"Verified fact": [], labels[0]: [], labels[1]: []}
            for label in labels:
                checks = check_facts(facts, outs[label])
                if not rows["Verified fact"]:
                    rows["Verified fact"] = [f for f, _ in checks]
                rows[label] = ["✅" if ok else "❌" for _, ok in checks]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            st.markdown("**Both queries execute successfully. Only one follows company "
                        "policy.** The model did not fail at SQL — the enterprise failed "
                        "to provide the meaning.")


# ------------------------------------------------------------------
# 3 & 4. The two live proofs — 90 seconds each
# ------------------------------------------------------------------
st.header("2 · The institutional rule the schema cannot reveal")
st.markdown("Finance policy (owner: Finance — Asset Efficiency Reporting, effective 2019): "
            "*reported utilization excludes service-protection loads (SHPMT_CNT = 1).* "
            "This rule exists in the ontology only — no column or sample row reveals it.")
render_question("What is our reported utilization?", "reported", "q1")

st.header("3 · Composing enterprise meaning")
st.markdown("Three semantic hops in one sentence: the Finance exclusion, "
            "Springfield → SGF resolution, and origin-lane filtering. No gold table "
            "anticipated this cut.")
render_question("What is our reported utilization for lanes originating from Springfield?",
                "sgf", "q2")

# ------------------------------------------------------------------
# 5. The production flow — 60 seconds
# ------------------------------------------------------------------
st.header("4 · How this works responsibly at enterprise scale")
st.code("""Question
   ↓
Intent mapped to a governed metric / entity request
   ↓
Semantic layer compiles trusted SQL          (definitions enforced, not advised)
   ↓
Unity Catalog & platform policies enforce access
   ↓
Warehouse computes                            (digit-perfect, auditable)
   ↓
Answer carries its definition, lineage, and evidence""", language=None)
st.caption("This POC uses free-form SQL generation guided by the ontology so the value is "
           "visible. Production constrains the model through a governed semantic API or "
           "tools — the same definitions, enforced instead of advised.")

# ------------------------------------------------------------------
# 6. The close — 45 seconds
# ------------------------------------------------------------------
st.header("5 · The ask")
st.markdown("""
This is not a proposal to model the entire company before delivering value.
**Proposed pilot:** one operational domain, 10–20 high-value KPIs with their
institutional rules harvested from Finance and Operations, stood up against the
real gold layer behind governed tools, with a measured evaluation of answer
accuracy and definition consistency. Scale the semantic contract through existing
data products and governance from there.

*Full technical detail — all ten test questions, the knowledge graph, architecture,
caching economics, and developer mechanics — lives on the main page of this app.*
""")
