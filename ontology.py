# ontology.py
# Cube Utilization Semantic Ontology (v2)
#
# WHAT CHANGED FROM v1:
# - KPI definitions now include exact, step-by-step calculation logic
#   (pandas-style and SQL-style) so the LLM follows a blueprint instead
#   of guessing.
# - Added a "query_patterns" section: worked examples mapping common
#   business questions to exact computation steps.
#
# WHY THIS EXISTS:
# An LLM given only raw table schemas has to guess what "cube utilization"
# means, which tables to join, and what business rules apply. This ontology
# makes that knowledge explicit and machine-readable. This is the "context
# layer" the POC demonstrates.

ontology = {
    "name": "Cube Utilization Ontology",
    "description": "Semantic model for freight trailer cube utilization and asset efficiency",
    "version": "2.0",

    # -------------------------------------------------------------------
    # ENTITIES: key business concepts, mapped to physical columns.
    # -------------------------------------------------------------------
    "entities": {
        "Shipment": {
            "description": "A freight shipment moving from an origin to a destination",
            "source_table": "shipments.csv",
            "properties": {
                "shipment_id": "unique identifier (e.g., SHP_00001)",
                "customer_id": "customer who tendered the shipment",
                "origin_terminal": "terminal where shipment originates",
                "destination_terminal": "terminal where shipment is delivered",
                "service_type": "Economy, Standard, or Priority",
                "weight_lbs": "shipment weight in pounds",
                "cube_ft": "shipment volume in cubic feet",
                "revenue": "revenue in dollars"
            }
        },
        "Trailer": {
            "description": "A physical trailer that carries shipments on a dispatch",
            "source_table": "dispatches.csv (one row per trailer)",
            "properties": {
                "trailer_id": "unique identifier (e.g., TRL_00001_1)",
                "capacity_cube_ft": "2000 cubic feet (standard for this demo)",
                "max_weight_lbs": "20000 pounds (regulatory/safety limit)"
            }
        },
        "Dispatch": {
            "description": "A linehaul movement event: one driver moving trailer(s) from one terminal to another",
            "source_table": "dispatches.csv",
            "properties": {
                "dispatch_id": "unique identifier",
                "origin_terminal": "starting terminal",
                "destination_terminal": "ending terminal",
                "driver_id": "driver operating the dispatch (attribute, not an entity)",
                "shipment_ids": "comma-separated list of shipments loaded in this trailer"
            }
        },
        "Terminal": {
            "description": "A physical freight terminal / hub location",
            "source_table": "derived from origin_terminal / destination_terminal columns",
            "properties": {
                "terminal_name": "Harrison, Springfield, Saint_Louis, Memphis, Atlanta"
            }
        },
        "Lane": {
            "description": "A directed origin-destination terminal pair (e.g., Harrison -> Springfield). Lanes are directional: A->B is a different lane than B->A.",
            "source_table": "derived: (origin_terminal, destination_terminal)",
            "properties": {
                "origin_terminal": "lane start",
                "destination_terminal": "lane end"
            }
        }
    },

    # -------------------------------------------------------------------
    # RELATIONSHIPS
    # -------------------------------------------------------------------
    "relationships": {
        "Shipment_loaded_in_Trailer": {
            "from_entity": "Shipment",
            "to_entity": "Trailer",
            "description": "Shipments are physically loaded into a trailer (shipment_ids column in dispatches.csv)",
            "cardinality": "many-to-one",
            "join_logic": "dispatches.shipment_ids contains shipments.shipment_id"
        },
        "Trailer_part_of_Dispatch": {
            "from_entity": "Trailer",
            "to_entity": "Dispatch",
            "description": "Each trailer row belongs to a dispatch; a dispatch can move multiple trailers with one driver",
            "cardinality": "many-to-one",
            "join_logic": "dispatches.trailer_id -> dispatches.dispatch_id"
        },
        "Dispatch_moves_on_Lane": {
            "from_entity": "Dispatch",
            "to_entity": "Lane",
            "description": "A dispatch travels a lane defined by its origin and destination terminals",
            "cardinality": "many-to-one",
            "join_logic": "GROUP BY (origin_terminal, destination_terminal)"
        },
        "Shipment_routes_through_Terminals": {
            "from_entity": "Shipment",
            "to_entity": "Terminal",
            "description": "A shipment's planned movement is a sequence of legs through terminals (planned_movements.csv)",
            "cardinality": "many-to-many",
            "join_logic": "planned_movements.shipment_id = shipments.shipment_id, ordered by leg_number"
        }
    },

    # -------------------------------------------------------------------
    # BUSINESS RULES: constraints and canonical formulas.
    # -------------------------------------------------------------------
    "business_rules": {
        "cube_utilization": {
            "formula": "cube_utilization_pct = (sum of cube_ft of shipments in trailer / 2000) * 100",
            "notes": "2000 cubic feet is the trailer capacity in this demo"
        },
        "weight_utilization": {
            "formula": "weight_utilization_pct = (sum of weight_lbs of shipments in trailer / 20000) * 100",
            "notes": "20000 lbs is the max legal/safe weight per trailer"
        },
        "actual_utilization": {
            "formula": "actual_utilization_pct = max(cube_utilization_pct, weight_utilization_pct)",
            "rationale": "A trailer is FULL when it hits EITHER limit (volume or weight), whichever comes first. So effective utilization is the MAX of the two: a trailer at 95% weight and 40% cube is ~95% utilized (weighed out) — it cannot take more freight. ALWAYS use actual_utilization_pct when ranking or comparing utilization unless the user explicitly asks for cube-only or weight-only.",
            "binding_constraint": "The binding_constraint column in cube_utilization.csv names which limit (cube or weight) is the one closer to 100%."
        },
        "lane_definition": {
            "rule": "A lane is the directed pair (origin_terminal, destination_terminal). Do NOT merge A->B with B->A.",
        },
        "ranking_direction": {
            "rule": "Higher utilization is BETTER. 'Worst' lanes/trailers = LOWEST actual_utilization_pct. 'Best' = HIGHEST. Double-check sort direction before answering."
        }
    },

    # -------------------------------------------------------------------
    # METRIC DEFINITIONS with exact computation steps.
    # The LLM must follow these steps literally.
    # -------------------------------------------------------------------
    "metrics": {
        "trailer_utilization": {
            "entities": ["Trailer"],
            "grain": "one row per trailer",
            "source": "cube_utilization.csv (pre-computed)",
            "columns": ["trailer_id", "actual_utilization_pct"],
            "steps": [
                "1. Read cube_utilization.csv",
                "2. Each row already contains actual_utilization_pct per trailer",
                "3. Report trailer_id with its actual_utilization_pct"
            ]
        },
        "lane_utilization": {
            "entities": ["Lane", "Trailer"],
            "grain": "one row per (origin_terminal, destination_terminal)",
            "source": "cube_utilization.csv",
            "steps": [
                "1. Read cube_utilization.csv",
                "2. GROUP BY origin_terminal, destination_terminal",
                "3. Compute AVG(actual_utilization_pct) per group; also report COUNT(trailer_id) as trailer count",
                "4. To find WORST lanes: sort ascending by avg utilization (lowest first)",
                "5. To find BEST lanes: sort descending (highest first)"
            ],
            "sql_equivalent": "SELECT origin_terminal, destination_terminal, AVG(actual_utilization_pct) AS avg_util, COUNT(*) AS trailers FROM cube_utilization GROUP BY 1,2 ORDER BY avg_util ASC"
        },
        "volume_by_origin": {
            "entities": ["Shipment", "Terminal"],
            "grain": "one row per origin_terminal",
            "source": "shipments.csv",
            "steps": [
                "1. Read shipments.csv",
                "2. GROUP BY origin_terminal",
                "3. Compute SUM(cube_ft) as total volume, COUNT(shipment_id) as shipment count"
            ],
            "sql_equivalent": "SELECT origin_terminal, SUM(cube_ft) AS total_cube, COUNT(*) AS shipments FROM shipments GROUP BY 1"
        },
        "shipments_on_trailer": {
            "entities": ["Shipment", "Trailer", "Dispatch"],
            "grain": "list of shipments for a given trailer",
            "source": "dispatches.csv joined to shipments.csv",
            "steps": [
                "1. Find the trailer's row in dispatches.csv",
                "2. Split its shipment_ids column on commas",
                "3. Look up each shipment_id in shipments.csv for details"
            ]
        }
    },

    # -------------------------------------------------------------------
    # QUERY PATTERNS: worked examples question -> computation.
    # -------------------------------------------------------------------
    "query_patterns": [
        {
            "question": "Which lanes have the lowest/worst utilization?",
            "metric": "lane_utilization",
            "answer_shape": "Table of lanes sorted ASCENDING by avg actual_utilization_pct, with trailer counts. Lowest number = worst."
        },
        {
            "question": "What is the average utilization across all trailers?",
            "metric": "trailer_utilization",
            "answer_shape": "Single number: AVG(actual_utilization_pct) over all rows of cube_utilization.csv"
        },
        {
            "question": "How much volume originates from <terminal>?",
            "metric": "volume_by_origin",
            "answer_shape": "SUM(cube_ft) from shipments.csv where origin_terminal = <terminal>"
        },
        {
            "question": "What shipments moved on trailer <id>?",
            "metric": "shipments_on_trailer",
            "answer_shape": "List of shipment_ids with origin, destination, weight, cube"
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
