import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIEWS = ROOT / "default/data/ui/views"


class DashboardViewsTest(unittest.TestCase):
    def test_every_view_is_dashboard_studio_xml_with_valid_json(self):
        view_files = sorted(VIEWS.glob("*.xml"))
        self.assertEqual(
            {path.stem for path in view_files},
            {
                "homelab_overview",
                "storage_capacity_health",
                "agent_activity",
                "agent_timeline",
                "agent_network",
                "model_residency_memory",
            },
        )

        for path in view_files:
            with self.subTest(view=path.name):
                root = ET.parse(path).getroot()
                self.assertEqual(root.tag, "dashboard")
                self.assertEqual(root.get("version"), "2")
                definition = root.findtext("definition")
                self.assertIsNotNone(definition)
                dashboard = json.loads(definition)
                data_sources = dashboard["dataSources"]
                visualizations = dashboard["visualizations"]
                for visualization in visualizations.values():
                    for source_id in visualization.get("dataSources", {}).values():
                        self.assertIn(source_id, data_sources)
                for input_definition in dashboard.get("inputs", {}).values():
                    items = input_definition.get("options", {}).get("items", {})
                    for source_id in items.get("dataSources", {}).values():
                        self.assertIn(source_id, data_sources)
                for block in dashboard["layout"]["structure"]:
                    self.assertIn(block["item"], visualizations)

    def test_navigation_lists_all_dashboards(self):
        nav = ET.parse(ROOT / "default/data/ui/nav/default.xml").getroot()
        self.assertEqual(
            {view.get("name") for view in nav.findall("view")},
            {
                "homelab_overview",
                "storage_capacity_health",
                "agent_activity",
                "agent_timeline",
                "agent_network",
                "model_residency_memory",
            },
        )

    def test_model_residency_memory_uses_declared_indexes_and_global_time(self):
        root = ET.parse(VIEWS / "model_residency_memory.xml").getroot()
        dashboard = json.loads(root.findtext("definition"))
        self.assertEqual(
            dashboard["layout"]["globalInputs"], ["input_time", "input_host"]
        )
        self.assertEqual(
            dashboard["inputs"]["input_time"]["options"]["defaultValue"],
            "-24h@h,now",
        )
        for source_id, source in dashboard["dataSources"].items():
            with self.subTest(source=source_id):
                query = source["options"]["query"]
                self.assertEqual(source["type"], "ds.search")
                self.assertRegex(query, r"^index=(llm|mac_perf|os)\b")
                self.assertNotIn("index=*", query)

    def test_model_panels_use_phys_footprint_and_normalise_model_names(self):
        dashboard = json.loads(
            ET.parse(VIEWS / "model_residency_memory.xml")
            .getroot()
            .findtext("definition")
        )
        sources = dashboard["dataSources"]
        for source_id in ("ds_model_phys", "ds_model_restarts"):
            with self.subTest(source=source_id):
                query = sources[source_id]["options"]["query"]
                self.assertIn("process_phys_footprint_bytes", query)
                self.assertNotIn("process_resident_memory_bytes", query)
                self.assertIn("models--(?<org>", query)
                self.assertIn(r"--model (?<model>\S+)", query)
        self.assertIn(
            "stats min(_time) AS first_seen max(_time) AS last_seen "
            "BY host model process_pid",
            sources["ds_model_restarts"]["options"]["query"],
        )
        self.assertEqual(
            dashboard["visualizations"]["viz_model_memory_stacked"]["type"],
            "splunk.area",
        )

    def test_swap_and_pressure_panels_use_their_source_field_names(self):
        sources = json.loads(
            ET.parse(VIEWS / "model_residency_memory.xml")
            .getroot()
            .findtext("definition")
        )["dataSources"]
        self.assertIn(
            "node_memory_swap_used_bytes=*",
            sources["ds_swap_used"]["options"]["query"],
        )
        self.assertIn(
            '"memory.pressure_percent"',
            sources["ds_macmon_pressure_pct"]["options"]["query"],
        )

    def test_macmon_panel_renames_dotted_memory_fields_and_drops_duplicates(self):
        root = ET.parse(VIEWS / "model_residency_memory.xml").getroot()
        query = json.loads(root.findtext("definition"))["dataSources"][
            "ds_macmon_memory_pct"
        ]["options"]["query"]
        rename = (
            'rename "memory.ram_usage" AS ram_used_bytes, '
            '"memory.ram_total" AS ram_total_bytes'
        )
        self.assertIn(rename, query)
        self.assertIn("dedup _time host", query)
        self.assertLess(query.index("dedup _time host"), query.index(rename))
        self.assertLess(query.index(rename), query.index("ram_total_bytes>0"))

    def test_agent_activity_uses_bounded_event_searches_and_global_time(self):
        root = ET.parse(VIEWS / "agent_activity.xml").getroot()
        dashboard = json.loads(root.findtext("definition"))
        self.assertEqual(dashboard["layout"]["globalInputs"], ["input_time"])
        parameters = dashboard["defaults"]["dataSources"]["ds.search"]["options"][
            "queryParameters"
        ]
        self.assertEqual(
            parameters, {"earliest": "$time.earliest$", "latest": "$time.latest$"}
        )
        for source in dashboard["dataSources"].values():
            with self.subTest(query=source["options"]["query"]):
                self.assertEqual(source["type"], "ds.search")
                query = source["options"]["query"]
                self.assertRegex(query, r"^\(?index=(hermes|hindsight)\b")
                self.assertNotIn("index=*", query)
                self.assertNotIn("makeresults", query)
                self.assertNotIn("fillnull", query)
                self.assertNotIn("rex ", query)

    def test_agent_timing_panels_require_explicit_units_and_measured_samples(self):
        root = ET.parse(VIEWS / "agent_activity.xml").getroot()
        dashboard = json.loads(root.findtext("definition"))
        for source_id in ("ds_gateway_latency", "ds_memory_timings"):
            with self.subTest(source=source_id):
                query = dashboard["dataSources"][source_id]["options"]["query"]
                self.assertIn("tonumber(duration_ms)", query)
                self.assertIn("1000*tonumber(duration_seconds)", query)
                self.assertIn("where isnotnull(timing_ms) AND timing_ms>=0", query)
                self.assertIn("count AS timed_events", query)
                self.assertIn("BY sourcetype source", query)

    def test_agent_cron_counts_only_terminal_events_without_inventing_outcomes(self):
        root = ET.parse(VIEWS / "agent_activity.xml").getroot()
        sources = json.loads(root.findtext("definition"))["dataSources"]
        for source_id in ("ds_cron_runs", "ds_cron_outcomes"):
            query = sources[source_id]["options"]["query"]
            self.assertIn(
                'where in(cron_event,"cron_run_completed",'
                '"cron_run_finished","cron_run_failed")',
                query,
            )
        outcomes = sources["ds_cron_outcomes"]["options"]["query"]
        self.assertIn('"failed","unreported"', outcomes)
        self.assertIn("dc(run_identifier) AS identified_runs", outcomes)
        self.assertIn("if(events_with_run_id>0,identified_runs,null())", outcomes)


if __name__ == "__main__":
    unittest.main()
