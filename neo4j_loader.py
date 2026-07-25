"""Load the cube-utilization ontology into Neo4j as a property graph.

WHY: the ontology-as-Python-dict works for one app. A graph DATABASE makes the
same ontology multi-consumer: shared authorship, Cypher traversal queries,
visualization in Neo4j Bloom, and an API other AI use cases (classification,
document extraction, agents) can read. This is pattern 3 of the ladder.

SETUP (one time, ~10 minutes):
1. Create a free Neo4j Aura instance: https://console.neo4j.io -> "New Instance"
   -> AuraDB Free. Save the generated password when shown (it appears once).
2. Note the connection URI (looks like neo4j+s://xxxxxxxx.databases.neo4j.io).
3. pip3 install neo4j
4. Run:  python3 neo4j_loader.py --uri neo4j+s://xxxx.databases.neo4j.io \
             --user neo4j --password YOUR_PASSWORD
5. Open the instance in Neo4j Browser / Bloom and run the sample queries below.

PRODUCTION MAPPING: on your stack, the same graph loads into Databricks via a
Delta table of edges, or stays in Neo4j alongside; the point is one governed
source (ontology.py today, a governed store tomorrow) compiling to every
consumer: prompt slices, vector index, metric compiler, and this graph.
"""
import argparse
import sys

from ontology import ontology

try:
    from neo4j import GraphDatabase
except ImportError:
    sys.exit("pip3 install neo4j  (then re-run)")


def load(tx):
    # wipe prior demo load (safe on a dedicated free instance)
    tx.run("MATCH (n) DETACH DELETE n")

    # ---- Entities ----
    for name, e in ontology["entities"].items():
        tx.run(
            """MERGE (n:Entity {name: $name})
               SET n.description = $desc, n.source_table = $src""",
            name=name, desc=e.get("description", ""),
            src=e.get("source_table", ""))

    # ---- Relationships between entities ----
    for rel_name, r in ontology["relationships"].items():
        tx.run(
            """MATCH (a:Entity {name: $from_e}), (b:Entity {name: $to_e})
               MERGE (a)-[rel:RELATES {name: $name}]->(b)
               SET rel.description = $desc, rel.cardinality = $card,
                   rel.join_logic = $join""",
            from_e=r["from_entity"], to_e=r["to_entity"], name=rel_name,
            desc=r.get("description", ""), card=r.get("cardinality", ""),
            join=r.get("join_logic", ""))

    # ---- Metrics, linked to the entities they depend on ----
    for name, m in ontology.get("metrics", {}).items():
        tx.run(
            """MERGE (mt:Metric {name: $name})
               SET mt.grain = $grain, mt.steps = $steps, mt.sql = $sql""",
            name=name, grain=m.get("grain", ""),
            steps=" | ".join(m.get("steps", [])),
            sql=m.get("sql_equivalent", ""))
        for ent in m.get("entities", []):
            tx.run(
                """MATCH (mt:Metric {name: $m}), (e:Entity {name: $e})
                   MERGE (mt)-[:DEPENDS_ON]->(e)""", m=name, e=ent)

    # ---- Business rules, linked to the metrics they govern ----
    RULE_GOVERNS = {
        "column_authority_utilization": ["trailer_utilization", "lane_utilization",
                                          "utilization_trend", "reported_utilization"],
        "temporal_attribution": ["utilization_trend"],
        "lane_definition": ["lane_utilization"],
        "ranking_direction": ["lane_utilization"],
        "terminal_name_resolution": ["volume_by_origin"],
        "reported_utilization_exclusion": ["reported_utilization"],
    }
    for name, r in ontology.get("business_rules", {}).items():
        prov = r.get("provenance", {})
        tx.run(
            """MERGE (br:Rule {name: $name})
               SET br.rule = $rule, br.owner = $owner, br.policy = $policy,
                   br.effective = $eff""",
            name=name, rule=r.get("rule", ""), owner=prov.get("owner", ""),
            policy=prov.get("policy", ""), eff=prov.get("effective", ""))
        for metric in RULE_GOVERNS.get(name, []):
            tx.run(
                """MATCH (br:Rule {name: $r}), (mt:Metric {name: $m})
                   MERGE (br)-[:GOVERNS]->(mt)""", r=name, m=metric)


SAMPLE_QUERIES = """
-- See the whole semantic model
MATCH (n) RETURN n;

-- Which rules govern reported utilization? (the auditability question)
MATCH (r:Rule)-[:GOVERNS]->(m:Metric {name:'reported_utilization'})
RETURN r.name, r.rule, r.owner, r.effective;

-- Multi-hop: every entity a metric ultimately depends on
MATCH (m:Metric)-[:DEPENDS_ON]->(e:Entity)
RETURN m.name, collect(e.name) AS entities;

-- Impact analysis: if the Shipment entity changes, what is affected?
MATCH (e:Entity {name:'Shipment'})<-[:DEPENDS_ON]-(m:Metric)
OPTIONAL MATCH (r:Rule)-[:GOVERNS]->(m)
RETURN e.name, m.name AS metric, collect(r.name) AS governing_rules;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", required=True)
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    args = ap.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    with driver.session() as session:
        session.execute_write(load)
        counts = session.run(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY label"
        ).data()
    driver.close()
    print("Loaded ontology into Neo4j:")
    for row in counts:
        print(f"  {row['label']}: {row['n']}")
    print("\nTry these in Neo4j Browser:")
    print(SAMPLE_QUERIES)


if __name__ == "__main__":
    main()
