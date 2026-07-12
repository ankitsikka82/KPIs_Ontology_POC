# ontology.py (v6)
# Cube Utilization Semantic Ontology over a LEGACY PHYSICAL SCHEMA.
#
# THE POINT OF THIS VERSION:
# The physical tables use legacy column codes (UTIL_PCT_3, LH_DSPTCH_DT,
# ORIG_TRML_CD...) that carry no business meaning. This ontology is the ONLY
# place where meaning lives. Without it, a model can only guess which of
# UTIL_PCT_1/2/3 is authoritative, what 'HAR' means, or which of two date
# columns to use. That is exactly the situation in a real enterprise warehouse.

ontology = {
    "name": "Cube Utilization Ontology",
    "description": "Semantic model for freight trailer cube utilization over the legacy gold-layer schema",
    "version": "6.0",

    # -------------------------------------------------------------------
    # PHYSICAL TABLES the queries run against
    # -------------------------------------------------------------------
    "physical_tables": {
        "shpmt_mstr": "Shipment master — one row per shipment",
        "lh_dsptch": "Linehaul dispatch — one row per TRAILER on a dispatch (a dispatch can move 2 trailers with 1 driver)",
        "trlr_util_fct": "Trailer utilization fact — one row per trailer per dispatch, pre-computed utilization",
        "pln_mvmt": "Planned movement — one row per routing leg per shipment"
    },

    # -------------------------------------------------------------------
    # CODE DECODES: legacy codes -> business meaning
    # -------------------------------------------------------------------
    "code_decodes": {
        "terminal_codes": {
            "HAR": "Harrison", "SGF": "Springfield", "STL": "Saint Louis",
            "MEM": "Memphis", "ATL": "Atlanta"
        },
        "CNSTRNT_CD": {
            "C": "cube-constrained (volume limit reached first)",
            "W": "weight-constrained (weight limit reached first)"
        },
        "SVC_TYP_CD": {
            "Economy": "economy service", "Standard": "standard service",
            "Priority": "priority service"
        }
    },

    # -------------------------------------------------------------------
    # ENTITIES: business concepts mapped to physical columns
    # -------------------------------------------------------------------
    "entities": {
        "Shipment": {
            "description": "A freight shipment moving from an origin to a destination",
            "source_table": "shpmt_mstr",
            "properties": {
                "SHPMT_NBR": "shipment number — unique identifier",
                "CUST_CD": "customer code",
                "ORIG_TRML_CD": "origin terminal code (see terminal_codes)",
                "DEST_TRML_CD": "destination terminal code (see terminal_codes)",
                "SVC_TYP_CD": "service type code",
                "TOT_WGT_LB": "total shipment weight, pounds",
                "TOT_CUBE_FT": "total shipment volume, cubic feet",
                "REV_AMT": "revenue, dollars",
                "SHPMT_CRT_DT": "shipment CREATION date — administrative only; NEVER use for utilization time analysis"
            }
        },
        "Trailer": {
            "description": "A 28-foot pup trailer used in LTL linehaul. Dispatches typically move two pups as a double with one driver.",
            "source_table": "lh_dsptch / trlr_util_fct (TRLR_NBR)",
            "properties": {
                "TRLR_NBR": "trailer number — unique identifier",
                "TRLR_CAP_CUBE": "pup volume capacity, cubic feet (~2000 for a 28-ft pup)",
                "WGT_LMT_LB": "PLANNING weight limit per pup, pounds (20000) — set by load planning to respect axle/GVW limits, not a per-trailer regulation"
            }
        },
        "Dispatch": {
            "description": "A linehaul movement event: one driver moving a double (typically two pups) between terminals",
            "source_table": "lh_dsptch",
            "properties": {
                "DSPTCH_NBR": "dispatch number",
                "ORIG_TRML_CD": "origin terminal code",
                "DEST_TRML_CD": "destination terminal code",
                "DRVR_ID": "driver (attribute of the dispatch, not an entity)",
                "LH_DSPTCH_DT": "dispatch departure date — the AUTHORITATIVE event time for utilization",
                "SHPMT_NBR_LST": "comma-separated shipment numbers loaded in this trailer"
            }
        },
        "Terminal": {
            "description": "A physical freight terminal, stored as a 3-letter code",
            "source_table": "ORIG_TRML_CD / DEST_TRML_CD columns; decode via terminal_codes",
            "properties": {
                "code": "HAR, SGF, STL, MEM, ATL — see code_decodes.terminal_codes"
            }
        },
        "Lane": {
            "description": "A DIRECTED origin-destination terminal pair (HAR->SGF is a different lane than SGF->HAR)",
            "source_table": "derived: (ORIG_TRML_CD, DEST_TRML_CD)",
            "properties": {
                "ORIG_TRML_CD": "lane start code",
                "DEST_TRML_CD": "lane end code"
            }
        },
        "Time": {
            "description": "Calendar time. LH_DSPTCH_DT is the AUTHORITATIVE event time for utilization (the trailer moved when it moved); SHPMT_CRT_DT is only when the shipment record was created.",
            "source_table": "LH_DSPTCH_DT in trlr_util_fct / lh_dsptch",
            "properties": {
                "LH_DSPTCH_DT": "dispatch date (authoritative for utilization)",
                "week": "ISO week, Monday through Sunday"
            }
        }
    },

    # -------------------------------------------------------------------
    # RELATIONSHIPS with join logic on physical columns
    # -------------------------------------------------------------------
    "relationships": {
        "Shipment_loaded_in_Trailer": {
            "from_entity": "Shipment",
            "to_entity": "Trailer",
            "description": "Shipments are loaded into a trailer",
            "cardinality": "many-to-one",
            "join_logic": "lh_dsptch.SHPMT_NBR_LST contains shpmt_mstr.SHPMT_NBR (comma-separated; split/UNNEST to join)"
        },
        "Trailer_part_of_Dispatch": {
            "from_entity": "Trailer",
            "to_entity": "Dispatch",
            "description": "Each lh_dsptch row is one trailer on a dispatch",
            "cardinality": "many-to-one",
            "join_logic": "lh_dsptch.TRLR_NBR -> lh_dsptch.DSPTCH_NBR; trlr_util_fct.TRLR_NBR joins to lh_dsptch.TRLR_NBR"
        },
        "Dispatch_moves_on_Lane": {
            "from_entity": "Dispatch",
            "to_entity": "Lane",
            "description": "A dispatch travels a lane",
            "cardinality": "many-to-one",
            "join_logic": "GROUP BY (ORIG_TRML_CD, DEST_TRML_CD)"
        },
        "Dispatch_occurs_in_Time": {
            "from_entity": "Dispatch",
            "to_entity": "Time",
            "description": "Time analysis of utilization is grouped on LH_DSPTCH_DT",
            "cardinality": "many-to-one",
            "join_logic": "trlr_util_fct.LH_DSPTCH_DT"
        },
        "Shipment_routes_through_Terminals": {
            "from_entity": "Shipment",
            "to_entity": "Terminal",
            "description": "A shipment's planned movement is a sequence of legs through terminals",
            "cardinality": "many-to-many",
            "join_logic": "pln_mvmt.SHPMT_NBR = shpmt_mstr.SHPMT_NBR, ordered by LEG_SEQ_NBR"
        }
    },

    # -------------------------------------------------------------------
    # BUSINESS RULES: the meaning the schema does not carry
    # -------------------------------------------------------------------
    "business_rules": {
        "column_authority_utilization": {
            "rule": "UTIL_PCT_3 is the AUTHORITATIVE utilization column. UTIL_PCT_1 is cube-only utilization; UTIL_PCT_2 is weight-only. ALWAYS use UTIL_PCT_3 for any ranking, average, or comparison unless the user explicitly asks for cube-only or weight-only.",
            "formula": "UTIL_PCT_3 = max(UTIL_PCT_1, UTIL_PCT_2)",
            "rationale": "A pup is FULL when it hits EITHER limit — cubed out (volume) or weighed out (weight) — whichever comes first. A pup at 95% weight and 40% cube is ~95% utilized; it cannot take more freight."
        },
        "temporal_attribution": {
            "rule": "Attribute utilization to LH_DSPTCH_DT, NEVER SHPMT_CRT_DT. Weeks run Monday through Sunday. 'Last week' = the most recent COMPLETE week before the current week. State the period boundaries used."
        },
        "lane_definition": {
            "rule": "A lane is the directed pair (ORIG_TRML_CD, DEST_TRML_CD). Do NOT merge A->B with B->A."
        },
        "ranking_direction": {
            "rule": "Higher utilization is BETTER. 'Worst' = LOWEST UTIL_PCT_3. 'Best' = HIGHEST. Verify sort direction before answering."
        },
        "reported_utilization_exclusion": {
            "rule": "REPORTED utilization EXCLUDES service-protection loads. A service-protection load is any trailer dispatched with a single shipment on board (SHPMT_CNT = 1) — cut to protect transit commitments regardless of fill. Per the 2019 Finance policy, these distort asset-efficiency reporting and are excluded from all REPORTED utilization figures. This rule exists ONLY here: no column, sample row, or naming convention reveals it.",
            "applies_when": "the user asks for 'reported', 'official', or 'finance' utilization"
        },
        "terminal_name_resolution": {
            "rule": "Users refer to terminals by NAME (Harrison, Springfield...); the data stores 3-letter CODES. Resolve names to codes via code_decodes.terminal_codes before filtering (e.g., Harrison -> ORIG_TRML_CD = 'HAR')."
        }
    },

    # -------------------------------------------------------------------
    # METRIC DEFINITIONS: exact computation steps on physical columns
    # -------------------------------------------------------------------
    "metrics": {
        "trailer_utilization": {
            "entities": ["Trailer"],
            "grain": "one row per trailer",
            "source": "trlr_util_fct (pre-computed)",
            "steps": [
                "1. Read trlr_util_fct",
                "2. UTIL_PCT_3 is the authoritative per-trailer utilization",
                "3. Report TRLR_NBR with UTIL_PCT_3"
            ]
        },
        "lane_utilization": {
            "entities": ["Lane", "Trailer"],
            "grain": "one row per (ORIG_TRML_CD, DEST_TRML_CD)",
            "source": "trlr_util_fct",
            "steps": [
                "1. Read trlr_util_fct",
                "2. GROUP BY ORIG_TRML_CD, DEST_TRML_CD",
                "3. AVG(UTIL_PCT_3) per group; COUNT(TRLR_NBR) as trailer count",
                "4. WORST lanes: sort ascending. BEST: sort descending."
            ],
            "sql_equivalent": "SELECT ORIG_TRML_CD, DEST_TRML_CD, AVG(UTIL_PCT_3) AS avg_util, COUNT(*) AS trailers FROM trlr_util_fct GROUP BY 1,2 ORDER BY avg_util ASC"
        },
        "volume_by_origin": {
            "entities": ["Shipment", "Terminal"],
            "grain": "one row per ORIG_TRML_CD",
            "source": "shpmt_mstr",
            "steps": [
                "1. Resolve the terminal name to its code (Harrison -> 'HAR')",
                "2. Read shpmt_mstr; filter/GROUP BY ORIG_TRML_CD",
                "3. SUM(TOT_CUBE_FT) as total volume; COUNT(SHPMT_NBR) as shipment count"
            ],
            "sql_equivalent": "SELECT ORIG_TRML_CD, SUM(TOT_CUBE_FT) AS total_cube, COUNT(*) AS shipments FROM shpmt_mstr GROUP BY 1"
        },
        "utilization_trend": {
            "entities": ["Trailer", "Time"],
            "grain": "one row per week",
            "source": "trlr_util_fct",
            "steps": [
                "1. Read trlr_util_fct; use LH_DSPTCH_DT (NEVER SHPMT_CRT_DT)",
                "2. Assign each row to its week (Monday start; date_trunc('week', LH_DSPTCH_DT))",
                "3. GROUP BY week; AVG(UTIL_PCT_3) and COUNT(TRLR_NBR)",
                "4. Sort chronologically; state the direction of change"
            ],
            "sql_equivalent": "SELECT date_trunc('week', LH_DSPTCH_DT) AS wk, AVG(UTIL_PCT_3), COUNT(*) FROM trlr_util_fct GROUP BY 1 ORDER BY 1"
        },
        "reported_utilization": {
            "entities": ["Trailer"],
            "grain": "single number (optionally by lane or week)",
            "source": "trlr_util_fct",
            "steps": [
                "1. Read trlr_util_fct",
                "2. EXCLUDE service-protection loads: filter to SHPMT_CNT > 1 (per reported_utilization_exclusion)",
                "3. AVG(UTIL_PCT_3) over the remaining rows",
                "4. State how many loads were excluded"
            ],
            "sql_equivalent": "SELECT AVG(UTIL_PCT_3) AS reported_util, COUNT(*) AS loads_included FROM trlr_util_fct WHERE SHPMT_CNT > 1"
        },
        "shipments_on_trailer": {
            "entities": ["Shipment", "Trailer", "Dispatch"],
            "grain": "list of shipments for a given trailer",
            "source": "lh_dsptch joined to shpmt_mstr",
            "steps": [
                "1. Find the trailer's row in lh_dsptch",
                "2. Split SHPMT_NBR_LST on commas (UNNEST(string_split(...)))",
                "3. Join each number to shpmt_mstr.SHPMT_NBR for details"
            ]
        }
    },

    # -------------------------------------------------------------------
    # QUERY PATTERNS: question -> metric, with the trap called out
    # -------------------------------------------------------------------
    "query_patterns": [
        {
            "question": "Rank the lanes by utilization (best to worst or worst to best)",
            "metric": "lane_utilization",
            "answer_shape": "Full ranked table of ALL lanes by AVG(UTIL_PCT_3) — NEVER UTIL_PCT_1 or UTIL_PCT_2 unless explicitly asked. Best = highest. State which column you ranked on."
        },
        {
            "question": "What was utilization last week / in a specific week?",
            "metric": "utilization_trend",
            "answer_shape": "Apply temporal_attribution: Monday-Sunday weeks on LH_DSPTCH_DT; 'last week' = most recent COMPLETE week. State the exact date range, then AVG(UTIL_PCT_3) for rows in range."
        },
        {
            "question": "What is our reported / official utilization?",
            "metric": "reported_utilization",
            "answer_shape": "Apply reported_utilization_exclusion FIRST (SHPMT_CNT > 1), then AVG(UTIL_PCT_3). State the excluded load count. A plain average over all loads is WRONG for 'reported' figures."
        },
        {
            "question": "Which lanes have the lowest/worst utilization?",
            "metric": "lane_utilization",
            "answer_shape": "Table of lanes sorted ASCENDING by AVG(UTIL_PCT_3), with trailer counts."
        },
        {
            "question": "What is the average utilization across all trailers?",
            "metric": "trailer_utilization",
            "answer_shape": "Single number: AVG(UTIL_PCT_3) over all rows of trlr_util_fct"
        },
        {
            "question": "How much volume originates from <terminal name>?",
            "metric": "volume_by_origin",
            "answer_shape": "Resolve name to code first; SUM(TOT_CUBE_FT) from shpmt_mstr where ORIG_TRML_CD = <code>"
        },
        {
            "question": "How has utilization trended over time / week over week?",
            "metric": "utilization_trend",
            "answer_shape": "Chronological weeks with AVG(UTIL_PCT_3) and trailer counts, plus trend direction"
        },
        {
            "question": "What shipments moved on trailer <id>?",
            "metric": "shipments_on_trailer",
            "answer_shape": "List of SHPMT_NBR values with origin, destination, weight, cube"
        }
    ]
}


# ------------------------- helper functions -------------------------
def get_entity(entity_name):
    return ontology["entities"].get(entity_name)


def get_relationship(relationship_name):
    return ontology["relationships"].get(relationship_name)


def get_business_rule(rule_name):
    return ontology["business_rules"].get(rule_name)


def get_metric(metric_name):
    return ontology["metrics"].get(metric_name)


def get_all_entities():
    return list(ontology["entities"].keys())


def get_all_relationships():
    return list(ontology["relationships"].keys())
