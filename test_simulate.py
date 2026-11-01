import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import arbitre_server
import simulate


class SimulatedClientAddressTests(unittest.TestCase):
    def test_sandbox_accepts_loopback_simulated_addresses(self):
        self.assertEqual(
            arbitre_server.effective_client_ip("127.0.0.1", {"X-Rhetora-Simulated-IP": "127.10.0.2"}, True),
            "127.10.0.2",
        )

    def test_production_ignores_simulated_address_header(self):
        self.assertEqual(
            arbitre_server.effective_client_ip("127.0.0.1", {"X-Rhetora-Simulated-IP": "127.10.0.2"}),
            "127.0.0.1",
        )

    def test_sandbox_rejects_non_loopback_simulated_addresses(self):
        self.assertEqual(
            arbitre_server.effective_client_ip("127.0.0.1", {"X-Rhetora-Simulated-IP": "192.0.2.1"}, True),
            "127.0.0.1",
        )


class MetricsTests(unittest.TestCase):
    def test_expected_denial_and_unexpected_success(self):
        metrics = simulate.Metrics()
        metrics.record("denial", 403, 2, expected=(403,))
        metrics.record("unexpected", 200, 3, expected=(403,))
        self.assertEqual(metrics.summary()["denial"]["successes"], 1)
        self.assertEqual(metrics.summary()["unexpected"]["failures"], 1)
        self.assertEqual(metrics.error_count, 1)

    def test_bounded_samples_and_exact_counters(self):
        metrics = simulate.Metrics()
        for index in range(12000):
            metrics.record("load", 200, index)
        result = metrics.summary()["load"]
        self.assertEqual(metrics.request_count, 12000)
        self.assertEqual(len(metrics.requests), 100)
        self.assertEqual(result["latency_sample_count"], 10000)
        self.assertEqual(result["requests"], 12000)
        self.assertEqual(result["max_ms"], 11999)
        self.assertEqual(result["http_statuses"], {"200": 12000})

    def test_live_state_keeps_total_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "live.json"
            metrics = simulate.Metrics(path)
            for index in range(110):
                metrics.record_error("test", str(index))
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["error_count"], 110)
            self.assertEqual(len(state["errors"]), 30)
            self.assertEqual(len(metrics.errors), 100)

    def test_activity_journal_keeps_full_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "live.json"
            metrics = simulate.Metrics(path)
            metrics.set_actors(["alice", "bob"])
            metrics.set_sandbox(url="http://127.0.0.1:1", view_key="key", open=True)
            body = "x" * 1500
            metrics.actor_event("alice", "Envoie un message privé", "résumé", "bob", kind="message", content=body)
            metrics.actor_event("bob", "Publie une analyse", "titre", kind="post", post_id=7, title="Titre")
            metrics.set_phase("Lecture")
            journal = [json.loads(line) for line in (Path(temporary) / "live-activity.jsonl").read_text(encoding="utf-8").splitlines()]
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([event["id"] for event in journal], [1, 2])
            self.assertEqual(journal[0]["content"], body)
            self.assertEqual(journal[0]["persona"], "creator")
            self.assertEqual(journal[1]["post_id"], 7)
            self.assertEqual(len(state["social_activity"][0]["content"]), 600)
            self.assertTrue(state["sandbox"]["open"])

    def test_audit_failure_cannot_pass_silently(self):
        metrics = simulate.Metrics()
        simulate.audit_check(metrics, "security", "denied", lambda: simulate.require(False, "exposed"))
        self.assertEqual(metrics.checks[0]["status"], "failed")
        self.assertEqual(metrics.error_count, 1)

    def test_audit_propagates_cancellation(self):
        def canceled():
            raise simulate.SimulationCanceled()

        with self.assertRaises(simulate.SimulationCanceled):
            simulate.audit_check(simulate.Metrics(), "qa", "cancel", canceled)

    def test_recommendation_is_a_warning_not_an_error(self):
        metrics = simulate.Metrics()

        def advice():
            raise simulate.Recommendation("Ajouter une CSP.")

        simulate.audit_check(metrics, "security", "csp", advice)
        self.assertEqual(metrics.checks[0]["status"], "warning")
        self.assertEqual(metrics.error_count, 0)
        self.assertEqual(metrics.audit_summary(), {"passed": 0, "warning": 1, "failed": 0})
        self.assertEqual([event["state"] for event in metrics.agent_activity], ["running", "warning"])


class ComparisonTests(unittest.TestCase):
    @staticmethod
    def report(path, users, checks, p95, status="passed"):
        path.write_text(json.dumps({
            "status": status, "finished_at": "2026-10-31T00:00:00+00:00",
            "configuration": {"users": users, "concurrency": 5, "duration_seconds": 0, "mode": "balanced", "pace": "load",
                              "seed": 42, "ramp_seconds": 10, "signups_per_minute": 0, "visit_percent": 100},
            "checks": [{"role": "security", "name": name, "status": state} for name, state in checks.items()],
            "actions": {name: {"p95_ms": value} for name, value in p95.items()},
        }), encoding="utf-8")

    def test_baseline_prefers_same_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.report(folder / "run-20261031-010000-aaaaaa.json", 10, {"csp": "warning"}, {"feed": 100})
            self.report(folder / "run-20261031-020000-bbbbbb.json", 50, {"csp": "passed"}, {"feed": 900})
            self.report(folder / "run-20261031-030000-cccccc.json", 10, {"csp": "passed"}, {}, status="canceled")
            configuration = {"users": 10, "concurrency": 5, "duration_seconds": 0, "mode": "balanced", "pace": "load",
                             "seed": 42, "ramp_seconds": 10, "signups_per_minute": 0, "visit_percent": 100}
            baseline = simulate.load_baseline(folder, configuration)
            self.assertEqual(baseline["report"], "run-20261031-010000-aaaaaa.json")
            self.assertTrue(baseline["comparable"])
            fallback = simulate.load_baseline(folder, configuration | {"users": 25})
            self.assertEqual(fallback["report"], "run-20261031-020000-bbbbbb.json")
            self.assertFalse(fallback["comparable"])

    def test_comparison_classifies_checks_and_latency(self):
        baseline = {"report": "run.json", "finished_at": "", "status": "failed", "comparable": True,
                    "checks": {"security.csp": "warning", "security.xss": "passed", "qa.old": "passed"},
                    "p95": {"feed": 100, "publish": 1000, "security.headers": 5}}
        checks = [{"role": "security", "name": "csp", "status": "passed"},
                  {"role": "security", "name": "xss", "status": "failed"},
                  {"role": "security", "name": "sql", "status": "passed"}]
        actions = {"feed": {"p95_ms": 400}, "publish": {"p95_ms": 500}, "security.headers": {"p95_ms": 900}}
        result = simulate.compare_runs(baseline, checks, actions)
        self.assertEqual(result["summary"], {"improved": 1, "regressed": 1, "stable": 0, "new": 1, "missing": 1})
        self.assertEqual({row["action"]: row["trend"] for row in result["latency"]}, {"feed": "regressed", "publish": "improved"})
        self.assertIsNone(simulate.compare_runs(None, checks, actions))


class SimulationModelTests(unittest.TestCase):
    def test_map_prompt_returns_normalizable_map(self):
        with patch.object(arbitre_server, "SIMULATION_MODE", True):
            result = arbitre_server.llm_json("groq", {}, arbitre_server.MAP_PROMPT, "Sujet : test")

        normalized = arbitre_server.normalize_map(result)
        self.assertEqual(normalized["arguments"][0]["id"], "A1")
        self.assertEqual(normalized["arguments"][0]["supports"], ["T"])


class SchedulerTests(unittest.TestCase):
    @staticmethod
    def world(names, online=True, operations=None):
        world = simulate.World(operations=operations)
        for index, name in enumerate(names):
            actor = simulate.make_actor(name, "pw", index, "complete", contacts=[other for other in names if other != name],
                                        online=online, client=name if online else None)
            world.add_actor(actor)
        world.add_post(1, names[0], "Titre")
        return world

    def test_analysis_cancellation_is_not_an_application_error(self):
        metrics = simulate.Metrics()
        metrics.set_actors(["creator"])

        def canceled(actor):
            raise simulate.SimulationCanceled()

        world = self.world(["creator"], operations={"analyze": canceled})
        world.actors[0]["script"].append("analyze")
        stopped = simulate.run_population(world, 0.5, 1, metrics, ramp_seconds=0)
        self.assertTrue(stopped)
        self.assertEqual(metrics.error_count, 0)

    def test_parallel_preserves_order_and_records_failures(self):
        metrics = simulate.Metrics()

        def worker(value):
            if value == 2:
                raise ValueError("failure")
            return value * 10

        results = simulate.run_parallel("test", [1, 2, 3], worker, 2, metrics)
        self.assertEqual(results, [10, 30])
        self.assertEqual(metrics.error_count, 1)

    def test_cancel_does_not_launch_waiting_actors(self):
        with tempfile.TemporaryDirectory() as temporary:
            cancel_file = Path(temporary) / "cancel"
            started = []

            def worker(value):
                started.append(value)
                cancel_file.touch()
                return value

            with self.assertRaises(simulate.SimulationCanceled):
                simulate.run_parallel("cancel", list(range(100)), worker, 1, simulate.Metrics(), cancel_file)
            self.assertEqual(started, [0])

    def test_slow_actor_does_not_block_fast_actor_or_share_its_session(self):
        metrics = simulate.Metrics()
        metrics.set_actors(["slow", "fast"])
        world = self.world(["slow", "fast"])
        for actor in world.actors:
            actor["script"].extend(["profile"] * 100000)
        active = {"slow": 0, "fast": 0}
        maximum = {"slow": 0, "fast": 0}
        lock = threading.Lock()

        def request(metrics, client, *args, **kwargs):
            with lock:
                active[client] += 1
                maximum[client] = max(maximum[client], active[client])
            if client == "slow":
                time.sleep(0.1)
            with lock:
                active[client] -= 1
            return {}

        with patch("simulate.perform", side_effect=request):
            for actor in world.actors:
                actor["rng"].uniform = lambda lower, upper: 0
            simulate.run_population(world, 0.35, 2, metrics, ramp_seconds=0)
        self.assertGreater(metrics.actors["fast"]["actions"], metrics.actors["slow"]["actions"] + 2)
        self.assertEqual(maximum, {"slow": 1, "fast": 1})
        self.assertFalse(metrics.errors)

    def test_every_persona_can_use_every_action(self):
        for persona, weights in simulate.PERSONA_WEIGHTS.items():
            self.assertTrue(set(weights) <= set(simulate.ACTIONS), persona)
        world = self.world(["alice", "bob"])
        world.add_comment(1, 10, "bob", "Commentaire")
        chosen = set()
        metrics = simulate.Metrics()
        sessions = {}

        def request(metrics, client, action, method, path, payload=None, expected=(200,), header_overrides=None):
            if path.startswith("/api/community/posts/1") and method == "GET":
                return {"comments": [], "verdict": {"options": [{"id": "nul", "label": "Match nul"}]}}
            if "/comments" in path:
                return {"id": 11}
            if path in ("/api/auth/logout", "/api/auth/login"):
                sessions[client] = path.endswith("login")
            if path == "/api/me":
                return {"user": {"username": client} if sessions.get(client, True) else None}
            return {}

        actor = world.actors[0]
        actor["follows"].add("bob")
        actor["rng"].uniform = lambda lower, upper: 0
        with patch("simulate.perform", side_effect=request), patch.object(simulate, "COOLDOWNS", {}):
            for action in simulate.ACTIONS:
                if action not in ("analyze", "publish"):
                    actor["online"] = True
                    actor["script"].append(action)
                    world.actors[1]["script"].append("browse")
                    simulate.run_population(world, 0.05, 1, metrics, ramp_seconds=0)
                    chosen |= {event["kind"] for event in metrics.social_activity if event["username"] == "alice"}
        self.assertFalse(metrics.errors, list(metrics.errors))
        self.assertTrue({"follow", "reply", "report", "verdict", "notification", "search", "explore",
                         "profile", "message", "conversation", "session"} <= chosen, chosen)

    def test_loopback_addresses_are_distinct_for_ten_thousand_accounts(self):
        addresses = {simulate.loopback_address(index) for index in range(10050)}
        self.assertEqual(len(addresses), 10050)
        self.assertFalse(any(address.endswith((".0", ".255")) or address == "127.0.0.1" for address in addresses))


class QualityTests(unittest.TestCase):
    def test_latency_budget_and_database_connection_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "rhetora.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE users (role TEXT)")
                connection.execute("INSERT INTO users VALUES ('admin')")
                connection.commit()
            metrics = simulate.Metrics()
            metrics.record("browse", 200, 2500)
            simulate.run_quality_audit(directory, metrics, 1, [{}], 2000)
            checks = {item["name"]: item["status"] for item in metrics.checks}
            self.assertEqual(checks["database_integrity"], "passed")
            self.assertEqual(checks["latency_budget"], "failed")
            database.unlink()


if __name__ == "__main__":
    unittest.main()