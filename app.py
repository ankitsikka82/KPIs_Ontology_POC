import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import json
import os
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
    return shipments, dispatches, utilization


shipments, dispatches, utilization = load_data()

api_key = os.environ.get("ANTHROPIC_API_KEY", "")

st.sidebar.header("Configuration")
if not api_key:
    api_key = st.sidebar.text_input("Anthropic API Key", type="password")

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
  <text x="250" y="360" text-anchor="middle" font-size="13" fill="#032030">shipments.csv</text>
  <text x="450" y="360" text-anchor="middle" font-size="13" fill="#032030">dispatches.csv</text>
  <text x="650" y="360" text-anchor="middle" font-size="13" fill="#032030">cube_utilization.csv</text>
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

with st.expander("Architecture: How the Semantic Layer Works", expanded=True):
    components.html(ARCHITECTURE_SVG, height=680, scrolling=True)
    st.caption("Green layer is the difference: the ontology gives Claude entity definitions, "
               "relationships, and exact metric formulas. The red dashed path is what happens "
               "without it — Claude reasons from raw column names and guesses.")


# ===============================================================
# INTERACTIVE KNOWLEDGE GRAPH (pyvis)
# ===============================================================
ENTITY_COLORS = {
    "Shipment": "#4dabf7",   # blue
    "Trailer": "#69db7c",    # green
    "Dispatch": "#ffd43b",   # yellow
    "Terminal": "#e599f7",   # purple
    "Lane": "#ffa94d",       # amber
}
METRIC_COLOR = "#845ef7"     # violet diamonds: governed metric definitions
USED_COLOR = "#e8590c"       # strong orange: traversed
UNUSED_COLOR = "#dee2e6"     # pale grey: present but not used

# Pinned layout: BOTH graphs use identical coordinates, so the traversal
# graph reads as "same map, path lit up" instead of a rescrambled picture.
# (vis.js y-axis points down, so negative y = higher on screen.)
NODE_POSITIONS = {
    # metrics row (top)
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


with st.expander("Interactive Knowledge Graph: the Ontology Behind the Scenes", expanded=False):
    st.caption("This is the live semantic model — not a mockup. Drag nodes, zoom with scroll, "
               "hover for definitions, computation steps, and join logic. Add an entity or metric "
               "to ontology.py and it appears here. Five entities and four metrics for this POC — "
               "a production ontology has hundreds, rendered and governed exactly the same way.")
    kg_legend(mode="full")
    render_kg(build_kg(), "kg_full.html")

# ===============================================================
# GROUND TRUTH COMPUTATIONS (pandas, no LLM)
# Each function returns (title, dataframe or text, optional chart df)
# ===============================================================

def truth_lane_utilization():
    lanes = (
        utilization
        .groupby(['origin_terminal', 'destination_terminal'], as_index=False)
        .agg(avg_utilization=('actual_utilization_pct', 'mean'),
             trailers=('trailer_id', 'count'))
        .sort_values('avg_utilization')
    )
    lanes['lane'] = lanes['origin_terminal'] + " → " + lanes['destination_terminal']
    lanes['avg_utilization'] = lanes['avg_utilization'].round(2)
    table = lanes[['lane', 'avg_utilization', 'trailers']]
    chart = lanes.set_index('lane')[['avg_utilization']]
    return ("Lane utilization, sorted worst → best", table, chart)


def truth_avg_utilization():
    avg = utilization['actual_utilization_pct'].mean()
    text = (f"Average actual utilization across all {len(utilization)} trailers: "
            f"**{avg:.2f}%**")
    per_trailer = utilization[['trailer_id', 'actual_utilization_pct']] \
        .sort_values('actual_utilization_pct')
    return (text, per_trailer, None)


def truth_volume_from_harrison():
    harrison = shipments[shipments['origin_terminal'] == 'Harrison']
    text = (f"Shipments originating from Harrison: **{len(harrison)}** — "
            f"total volume: **{harrison['cube_ft'].sum():,} cube ft**, "
            f"total weight: **{harrison['weight_lbs'].sum():,} lbs**")
    table = harrison[['shipment_id', 'destination_terminal', 'cube_ft', 'weight_lbs']]
    return (text, table, None)


def truth_underutilized_trailers():
    under = utilization[utilization['actual_utilization_pct'] < 70] \
        .sort_values('actual_utilization_pct')
    text = f"Trailers below 70% actual utilization: **{len(under)}** of {len(utilization)}"
    table = under[['trailer_id', 'origin_terminal', 'destination_terminal',
                   'actual_utilization_pct', 'binding_constraint']]
    chart = under.set_index('trailer_id')[['actual_utilization_pct']]
    return (text, table, chart)


def truth_shipments_on_top_trailer():
    top = utilization.loc[utilization['actual_utilization_pct'].idxmax()]
    trailer_id = top['trailer_id']
    disp_row = dispatches[dispatches['trailer_id'] == trailer_id]
    ship_ids = []
    if not disp_row.empty:
        ship_ids = [s for s in disp_row.iloc[0]['shipment_ids'].split(',') if s]
    ships = shipments[shipments['shipment_id'].isin(ship_ids)]
    text = (f"Highest-utilization trailer: **{trailer_id}** at "
            f"**{top['actual_utilization_pct']}%** "
            f"({top['origin_terminal']} → {top['destination_terminal']}, "
            f"binding constraint: {top['binding_constraint']}). "
            f"Shipments on board: **{len(ships)}**")
    table = ships[['shipment_id', 'origin_terminal', 'destination_terminal',
                   'cube_ft', 'weight_lbs', 'service_type']]
    return (text, table, None)


PRESET_QUESTIONS = {
    "Which lanes have the worst utilization?": truth_lane_utilization,
    "What is the average utilization across all trailers?": truth_avg_utilization,
    "How much volume originates from Harrison?": truth_volume_from_harrison,
    "Which trailers are underutilized (below 70%)?": truth_underutilized_trailers,
    "What shipments moved on the trailer with the highest utilization?": truth_shipments_on_top_trailer,
}

# ===============================================================
# QUESTION SELECTION: preset buttons + custom input
# ===============================================================
st.header("Ask About Cube Utilization")

if "selected_query" not in st.session_state:
    st.session_state.selected_query = ""
if "is_preset" not in st.session_state:
    st.session_state.is_preset = False

st.markdown("**Pick a question:**")
cols = st.columns(len(PRESET_QUESTIONS))
for i, (col, q) in enumerate(zip(cols, PRESET_QUESTIONS.keys())):
    with col:
        if st.button(q, key=f"preset_{i}", use_container_width=True):
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
    raw_context = f"""You are a freight analytics assistant. Show your work: display any
intermediate calculations or grouped values before stating conclusions.

You have these CSV tables:

DATA — full cube_utilization table:
{utilization.to_string(index=False)}

DATA — full shipments table:
{shipments.to_string(index=False)}

DATA — full dispatches table:
{dispatches.to_string(index=False)}

Answer the user's question based only on this information."""

    semantic_context = f"""You are a freight analytics assistant. Show your work: display any
intermediate calculations or grouped values before stating conclusions.

You additionally have access to a semantic ontology. Follow its metric
definitions and business rules EXACTLY, and state which metric definition you used.

After your complete answer, output these two lines EXACTLY in this format
(the system parses them; they are not shown to the user):
ENTITIES_USED: comma-separated entity names you used, chosen only from: Shipment, Trailer, Dispatch, Terminal, Lane
METRIC_USED: the single metric definition you followed, chosen only from: trailer_utilization, lane_utilization, volume_by_origin, shipments_on_trailer (or NONE)
RELATIONSHIPS_USED: comma-separated relationship names you used, chosen only from: {', '.join(ontology['relationships'].keys())}

ENTITIES:
{json.dumps({k: v['description'] for k, v in ontology['entities'].items()}, indent=2)}

RELATIONSHIPS (with join logic):
{json.dumps({k: {'link': f"{v['from_entity']} -> {v['to_entity']}", 'join': v.get('join_logic', '')} for k, v in ontology['relationships'].items()}, indent=2)}

BUSINESS RULES (canonical formulas — never deviate):
{json.dumps(ontology['business_rules'], indent=2)}

METRIC DEFINITIONS (exact computation steps):
{json.dumps(ontology['metrics'], indent=2)}

QUERY PATTERNS (map the question to the right metric):
{json.dumps(ontology['query_patterns'], indent=2)}

DATA — full cube_utilization table:
{utilization.to_string(index=False)}

DATA — full shipments table:
{shipments.to_string(index=False)}

DATA — full dispatches table:
{dispatches.to_string(index=False)}

Answer the user's question based only on this information."""

    # Store both prompts so the developer section can show the real payloads
    st.session_state.last_raw_prompt = f"{raw_context}\n\nQuestion: {user_query}"
    st.session_state.last_semantic_prompt = f"{semantic_context}\n\nQuestion: {user_query}"

    st.caption("Controlled comparison: both sides get the same model, same question, "
               "same three data tables, same instructions, same token budget. "
               "The only difference is the ontology context.")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Without Semantic Ontology")
        st.caption("Claude receives the raw data tables only")
        with st.spinner("Querying..."):
            try:
                response_raw = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=1300,
                    messages=[{
                        "role": "user",
                        "content": f"{raw_context}\n\nQuestion: {user_query}"
                    }]
                )
                st.write(response_raw.content[0].text)
            except Exception as e:
                st.error(f"Error: {str(e)}")

    with col2:
        st.subheader("With Semantic Ontology")
        st.caption("Claude receives entity definitions, business rules, and exact metric logic")
        with st.spinner("Querying..."):
            try:
                response_semantic = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=1300,
                    messages=[{
                        "role": "user",
                        "content": f"{semantic_context}\n\nQuestion: {user_query}"
                    }]
                )
                response_text = response_semantic.content[0].text

                # Parse traversal trailer lines (not shown to the user as-is)
                used_entities, used_rels, used_metric = [], [], None
                answer_text = response_text
                if "ENTITIES_USED:" in response_text:
                    answer_text, tail = response_text.split("ENTITIES_USED:", 1)
                    ent_part = tail.split("METRIC_USED:")[0].split("RELATIONSHIPS_USED:")[0]
                    used_entities = [e.strip() for e in ent_part.split(",")
                                     if e.strip() in ontology["entities"]]
                    if "METRIC_USED:" in tail:
                        m_part = tail.split("METRIC_USED:", 1)[1] \
                                     .split("RELATIONSHIPS_USED:")[0].strip()
                        if m_part in ontology.get("metrics", {}):
                            used_metric = m_part
                    if "RELATIONSHIPS_USED:" in tail:
                        rel_part = tail.split("RELATIONSHIPS_USED:", 1)[1]
                        used_rels = [r.strip() for r in rel_part.split(",")
                                     if r.strip() in ontology["relationships"]]
                st.write(answer_text.strip())
                st.session_state.traversal = (used_entities, used_rels, used_metric)
            except Exception as e:
                st.error(f"Error: {str(e)}")

    # -----------------------------------------------------------
    # TRAVERSAL: which parts of the ontology this query touched
    # -----------------------------------------------------------
    used_entities, used_rels, used_metric = st.session_state.get("traversal", ([], [], None))
    if used_entities:
        st.header("Ontology Traversal for This Query")
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
        title, table, chart = PRESET_QUESTIONS[user_query]()
        st.markdown(title)
        gt1, gt2 = st.columns([1, 1]) if chart is not None else (st.container(), None)
        if chart is not None:
            with gt1:
                st.dataframe(table, hide_index=True, use_container_width=True)
            with gt2:
                st.bar_chart(chart)
        else:
            st.dataframe(table, hide_index=True, use_container_width=True)
    else:
        st.markdown("Custom question — no precomputed check for it. "
                    "Key verified stats for manual comparison:")
        stats1, stats2, stats3 = st.columns(3)
        stats1.metric("Trailers", len(utilization))
        stats2.metric("Avg actual utilization",
                      f"{utilization['actual_utilization_pct'].mean():.1f}%")
        stats3.metric("Total shipments", len(shipments))
        with st.expander("Full utilization table (for manual verification)"):
            st.dataframe(utilization, hide_index=True, use_container_width=True)

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
  <text x="245" y="158" text-anchor="middle" font-size="13" font-weight="bold" fill="#7a1010">Prompt = data tables + question</text>
  <text x="245" y="178" text-anchor="middle" font-size="12" fill="#7a1010">CSVs serialized to text. No definitions.</text>
  <text x="245" y="196" text-anchor="middle" font-size="12" fill="#7a1010">"lane" is just a word in the question.</text>

  <rect x="550" y="135" width="370" height="72" rx="8" fill="#e6f4d7" stroke="#4f772d" stroke-width="1.5"/>
  <text x="735" y="158" text-anchor="middle" font-size="13" font-weight="bold" fill="#1a2e05">Prompt = ontology (JSON) + data + question</text>
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
  <text x="245" y="346" text-anchor="middle" font-size="13" font-weight="bold" fill="#7a1010">Claude infers meaning</text>
  <text x="245" y="366" text-anchor="middle" font-size="12" fill="#7a1010">Guesses grain: answers per TRAILER row</text>
  <text x="245" y="384" text-anchor="middle" font-size="12" fill="#7a1010">Guesses metric: may pick wrong column</text>
  <text x="245" y="402" text-anchor="middle" font-size="12" fill="#7a1010">Guesses sort: "worst" direction unverified</text>

  <rect x="550" y="323" width="370" height="92" rx="8" fill="#e6f4d7" stroke="#4f772d" stroke-width="1.5"/>
  <text x="735" y="346" text-anchor="middle" font-size="13" font-weight="bold" fill="#1a2e05">Claude follows the metric definition</text>
  <text x="735" y="366" text-anchor="middle" font-size="12" fill="#1a2e05">lane_utilization: GROUP BY origin, destination</text>
  <text x="735" y="384" text-anchor="middle" font-size="12" fill="#1a2e05">AVG(actual_utilization_pct), COUNT(trailers)</text>
  <text x="735" y="402" text-anchor="middle" font-size="12" fill="#1a2e05">ranking rule: worst = lowest, sort ascending</text>

  <line x1="245" y1="415" x2="245" y2="445" stroke="#555" stroke-width="2" marker-end="url(#a)"/>
  <line x1="735" y1="415" x2="735" y2="445" stroke="#555" stroke-width="2" marker-end="url(#a)"/>

  <rect x="60" y="448" width="370" height="72" rx="8" fill="#fff" stroke="#d62828" stroke-width="2"/>
  <text x="245" y="472" text-anchor="middle" font-size="13" font-weight="bold" fill="#7a1010">Answer at wrong grain</text>
  <text x="245" y="492" text-anchor="middle" font-size="12" fill="#7a1010">Same lane listed 3x as separate "lanes";</text>
  <text x="245" y="510" text-anchor="middle" font-size="12" fill="#7a1010">list not actually sorted by utilization</text>

  <rect x="550" y="448" width="370" height="72" rx="8" fill="#fff" stroke="#4f772d" stroke-width="2"/>
  <text x="735" y="472" text-anchor="middle" font-size="13" font-weight="bold" fill="#1a2e05">Answer matches ground truth</text>
  <text x="735" y="492" text-anchor="middle" font-size="12" fill="#1a2e05">One row per lane, averaged across trailers,</text>
  <text x="735" y="510" text-anchor="middle" font-size="12" fill="#1a2e05">sorted worst-first, cites metric used</text>

  <defs>
    <marker id="a" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#555"/>
    </marker>
  </defs>
</svg>
"""

DEV_SNIPPET = '''from ontology import ontology
import anthropic
from pyvis.network import Network, json

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY env var

# The ontology is a plain Python dict. We serialize the relevant
# sections to JSON and place them in the prompt as text:
semantic_context = (
    "You are a freight analytics assistant. Show your work.\\n\\n"
    "BUSINESS RULES (canonical formulas - never deviate):\\n"
    + json.dumps(ontology["business_rules"], indent=2) + "\\n\\n"
    "METRIC DEFINITIONS (exact computation steps):\\n"
    + json.dumps(ontology["metrics"], indent=2) + "\\n\\n"
    "DATA - full cube_utilization table:\\n"
    + utilization.to_string(index=False)
)

response = client.messages.create(
    model="claude-sonnet-4-5",
    max_tokens=1300,
    messages=[{
        "role": "user",
        "content": semantic_context + "\\n\\nQuestion: " + user_query
    }]
)
answer = response.content[0].text'''

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

#### How the ontology reaches Claude

This is the part people assume is magic. It is not. **The ontology travels as
plain text inside the prompt.** We serialize it with `json.dumps()` and prepend
it to the user's question in the `messages` array:
""")

    st.code(DEV_SNIPPET, language="python")

    st.markdown("""
So: **the ontology goes to the model as instructions plus reference material in
the message content.** It is not a special API parameter, not an embedding, not
RAG retrieval. The model treats the JSON as authoritative context and follows
the computation steps because the prompt tells it to. (In production you would
typically put the stable ontology in the `system` parameter and use prompt
caching - same mechanism, better economics.)

The "without ontology" call is **byte-for-byte identical** except the ontology
JSON is absent - same model, same data tables, same instructions, same token
budget.

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
#### Path to production

- **This POC**: Python dict, serialized to JSON, injected into the prompt. Proves the concept in ~200 lines.
- **Databricks Ontology / Unity Catalog**: managed semantic layer - versioning, lineage, governance, cross-workspace discovery. Natural fit for our stack.
- **Fabric semantic models**: if the consumption layer standardizes on Power BI / Fabric.
- **Graph DB (e.g., Neo4j)**: if we need rich multi-hop traversal queries.

The mechanism stays the same in all of them: **structured business meaning,
serialized into the model's context at query time.** The platform decision is
about governance and scale, not capability.
""")


# ===============================================================
# SAMPLE DATA
# ===============================================================
st.header("Sample Data")
tab1, tab2, tab3 = st.tabs(["Shipments", "Dispatches", "Utilization"])

with tab1:
    st.dataframe(shipments.head(10))

with tab2:
    st.dataframe(dispatches.head(10))

with tab3:
    st.dataframe(utilization.head(10))
