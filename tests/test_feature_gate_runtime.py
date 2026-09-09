"""Runtime feature-gate tests for ``hub.web`` (Task 6/9 rollout switches).

The Task 6 (session) / Task 9 (supervisor) / Task 6-adoption surfaces are
strictly additive and gated OFF by default.  ``web.make_app`` exposes the
gate for a production runner via ``AGENT_FLEET_*_ENABLED`` env vars (or the
explicit kwargs), with an absent value leaving today's surface byte-identical.

These tests prove four things:

1. default (no env, no kwarg)  -> session/supervisor/adoption stay OFF; the
   adoption/supervisor/session route rules are not registered;
2. env ``AGENT_FLEET_*_ENABLED`` flips the matching gate ON and registers the
   matching route surface (a disabled flag keeps the prior surface);
3. runtime ``<FLEET_HOME>/fleet-gates.conf`` ``KEY=VALUE`` lines flip the
   matching gate when no env var is present (env beats conf; explicit kwarg
   beats both) — the vector a host uses to enable gates on a
   guardian-spawned web without adding process env vars;
4. adoption depends on the session transcript audit store. ``make_app``
   does **not** auto-enable session when adoption is on; an adoption-only
   flip is fail-closed (adoption stays off, observe still boots);
5. an explicit kwarg ``adoption_repositories_enabled=False`` overrides a
   truthy env (explicit-wins).

They reuse the auth-matrix ``_make_app``-style harness against the real
``hub.web.make_app`` entrypoint used by the production guardian.  The
gate-on derivations write their durable stores under ``<root>/var``, so the
harness redirects ``web.FLEET_HOME`` into the same temp directory the rest
of the fixture uses — the host repo is never touched.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from hub import events
from hub import state as store
from hub import task_store
from hub import web

INGEST = "test-feature-gate-ingest"
OPERATOR = "feature@example.com"

#: The three runtime gates ``web.make_app`` resolves from the environment.
_GATE_ENVS = (
    "AGENT_FLEET_SESSION_REPOSITORIES_ENABLED",
    "AGENT_FLEET_SUPERVISOR_ENABLED",
    "AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED",
    "AGENT_FLEET_APPEND_USER_TURN_ENABLED",
    "AGENT_FLEET_APPLY_LOCAL_PROFILE_ENABLED",
)


def _make_app(env=None, conf=None, **make_kwargs):
    temp_dir = Path(tempfile.mkdtemp())
    old = (store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH,
           web.FLEET_HOME)
    store.STATE_DIR = temp_dir
    events.EVENT_LOG = temp_dir / "events.jsonl"
    task_store.DB_PATH = temp_dir / "fleet.db"
    # Session/adoption stores derive under ``<FLEET_HOME>/var``; redirecting
    # the module root keeps the whole run inside the temp sandbox.
    web.FLEET_HOME = temp_dir
    task_store.init_db()

    if conf is not None:
        (temp_dir / web._FEATURE_GATE_FILE).write_text(conf)

    saved = {k: os.environ.pop(k, None) for k in _GATE_ENVS}
    for key, value in (env or {}).items():
        os.environ[key] = value

    app = None
    try:
        app = web.make_app(
            ingest_token=INGEST,
            dev_operator=OPERATOR,
            runner_credentials={"agent-e2e": "runner-secret"},
            project_whitelist={"agent-e2e": ["agent-fleet"]},
            **make_kwargs,
        )
    finally:
        for key in _GATE_ENVS:
            os.environ.pop(key, None)
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
        store.STATE_DIR, events.EVENT_LOG, task_store.DB_PATH, web.FLEET_HOME = old
    return app


def _rules(app):
    return {rule.rule for rule in app.url_map.iter_rules()}


class DefaultOffTests(unittest.TestCase):
    """No env, no kwargs — the pre-Task-6/9 surface, byte-identical."""

    def test_all_gates_are_off_by_default(self):
        app = _make_app()
        self.assertFalse(app.config["SESSION_REPOSITORIES_ENABLED"])
        self.assertFalse(app.config["ADOPTION_REPOSITORIES_ENABLED"])
        self.assertFalse(app.config["SUPERVISOR_ENABLED"])
        self.assertFalse(app.config["APPEND_USER_TURN_ENABLED"])
        self.assertFalse(app.config["APPLY_LOCAL_PROFILE_ENABLED"])
        services = app.extensions["fleet"]["services"]
        self.assertNotIn("sessions", services)
        self.assertNotIn("adoptions", services)
        self.assertNotIn("control_router", services)

    def test_new_routes_are_not_registered_by_default(self):
        app = _make_app()
        rules = _rules(app)
        self.assertNotIn("/api/adoptions", rules)
        self.assertNotIn("/api/adoption/<session_id>/source/control", rules)
        self.assertNotIn("/api/sessions", rules)
        self.assertNotIn("/api/session-events", rules)
        self.assertNotIn("/api/supervisor/poll", rules)
        self.assertNotIn("/api/supervisor/receipts", rules)

    def test_status_features_default_off(self):
        app = _make_app()
        with app.test_client() as client:
            payload = client.get("/api/status").get_json()
        self.assertEqual(payload["features"], {
            "append_user_turn": False,
            "apply_local_profile": False,
        })

    def test_status_features_flip_with_env(self):
        app = _make_app({
            "AGENT_FLEET_APPEND_USER_TURN_ENABLED": "1",
            "AGENT_FLEET_APPLY_LOCAL_PROFILE_ENABLED": "on",
        })
        self.assertTrue(app.config["APPEND_USER_TURN_ENABLED"])
        self.assertTrue(app.config["APPLY_LOCAL_PROFILE_ENABLED"])
        with app.test_client() as client:
            payload = client.get("/api/status").get_json()
        self.assertEqual(payload["features"], {
            "append_user_turn": True,
            "apply_local_profile": True,
        })


class SessionGateTests(unittest.TestCase):
    def test_env_flips_session_store_and_routes(self):
        app = _make_app({"AGENT_FLEET_SESSION_REPOSITORIES_ENABLED": "1"})
        self.assertTrue(app.config["SESSION_REPOSITORIES_ENABLED"])
        self.assertIn("sessions", app.extensions["fleet"]["services"])
        rules = _rules(app)
        self.assertIn("/api/sessions", rules)
        self.assertIn("/api/sessions/<session_id>", rules)
        self.assertIn("/api/session-events", rules)
        # Independent gates: supervisor/adoption stayed off.
        self.assertFalse(app.config["SUPERVISOR_ENABLED"])
        self.assertFalse(app.config["ADOPTION_REPOSITORIES_ENABLED"])
        self.assertNotIn("adoptions", app.extensions["fleet"]["services"])


class SupervisorGateTests(unittest.TestCase):
    def test_env_registers_supervisor_routes(self):
        app = _make_app({"AGENT_FLEET_SUPERVISOR_ENABLED": "1"})
        self.assertTrue(app.config["SUPERVISOR_ENABLED"])
        rules = _rules(app)
        self.assertIn("/api/supervisor/poll", rules)
        self.assertIn("/api/supervisor/receipts", rules)
        # Still no supervisor_poll when only session gate raised.
        self.assertNotIn("adoptions", app.extensions["fleet"]["services"])

    def test_supervisor_poll_reachable_without_any_signing_key(self):
        # No supervisor signing key/credentials anywhere is the fail-closed
        # state: routes may be up but issuance must not succeed.
        app = _make_app({"AGENT_FLEET_SUPERVISOR_ENABLED": "1"})
        with app.test_client() as client:
            resp = client.post("/api/supervisor/poll", json={})
        self.assertIn(resp.status_code, (400, 401, 403))


class AdoptionGateTests(unittest.TestCase):
    def test_adoption_only_without_session_fails_closed(self):
        """Adoption-on + session-off must not crash ``make_app``.

        ``create_app`` needs a transcript root that ``from_root`` only
        derives when the session gate is on. Production must keep observe
        up; do not auto-enable session (that would create unexpected
        durable stores).
        """
        app = _make_app({"AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED": "1"})
        self.assertFalse(app.config["ADOPTION_REPOSITORIES_ENABLED"])
        self.assertFalse(app.config["SESSION_REPOSITORIES_ENABLED"])
        self.assertNotIn("adoptions", app.extensions["fleet"]["services"])
        self.assertNotIn("sessions", app.extensions["fleet"]["services"])
        rules = _rules(app)
        self.assertNotIn("/api/adoptions", rules)
        self.assertIn("/api/status", rules)
        self.assertIn("/api/ingest", rules)

    def test_env_flips_adoption_surface(self):
        # Adoption audits to the session transcript. Both gates must be
        # on; make_app does not pull session in from adoption alone.
        app = _make_app({
            "AGENT_FLEET_SESSION_REPOSITORIES_ENABLED": "1",
            "AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED": "1",
        })
        self.assertTrue(app.config["ADOPTION_REPOSITORIES_ENABLED"])
        services = app.extensions["fleet"]["services"]
        self.assertIn("adoptions", services)
        self.assertIn("sessions", services)
        rules = _rules(app)
        self.assertIn("/api/adoptions", rules)
        self.assertIn("/api/adoptions/<session_id>", rules)
        self.assertIn("/api/adoption/<session_id>/source/control", rules)

    def test_adoption_post_reaches_handler_once_enabled(self):
        # The disabled surface answers 405 from the page catch-all; enabled it
        # must be handled by the adoption blueprint (a driver error, never the
        # absent-surface 404).
        app = _make_app({
            "AGENT_FLEET_SESSION_REPOSITORIES_ENABLED": "1",
            "AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED": "1",
        })
        with app.test_client() as client:
            resp = client.post(
                "/api/adoptions",
                headers={"Cf-Access-Authenticated-User-Email": OPERATOR},
                json={},
            )
        self.assertIn(resp.status_code, (400, 201))
        self.assertNotIn(resp.status_code, (404, 405))


class FileGateTests(unittest.TestCase):
    """``<FLEET_HOME>/fleet-gates.conf`` flips the matching gate when no env
    var is set — the vector a production host uses to enable gates on a
    guardian-spawned web without adding process env vars."""

    def test_conf_flips_gate_without_env(self):
        app = _make_app(conf="AGENT_FLEET_SESSION_REPOSITORIES_ENABLED=1\n")
        self.assertTrue(app.config["SESSION_REPOSITORIES_ENABLED"])
        self.assertIn("sessions", app.extensions["fleet"]["services"])
        rules = _rules(app)
        self.assertIn("/api/sessions", rules)
        # Independent gates unaffected.
        self.assertFalse(app.config["SUPERVISOR_ENABLED"])
        self.assertFalse(app.config["ADOPTION_REPOSITORIES_ENABLED"])

    def test_conf_supervisor_gate(self):
        app = _make_app(conf="AGENT_FLEET_SUPERVISOR_ENABLED=on\n")
        self.assertTrue(app.config["SUPERVISOR_ENABLED"])
        rules = _rules(app)
        self.assertIn("/api/supervisor/poll", rules)

    def test_conf_truthy_variants(self):
        for val in ("1", "true", "yes", "on", " ON "):
            with self.subTest(val=val):
                # Adoption audits to the session transcript, so the adoption
                # gate needs the session store on (as in the env-based test).
                app = _make_app(
                    conf=(
                        "AGENT_FLEET_SESSION_REPOSITORIES_ENABLED=1\n"
                        f"AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED={val}\n"
                    ))
                self.assertTrue(app.config["ADOPTION_REPOSITORIES_ENABLED"])

    def test_conf_missing_file_stays_off(self):
        app = _make_app(conf=None)
        self.assertFalse(app.config["ADOPTION_REPOSITORIES_ENABLED"])

    def test_env_wins_over_conf(self):
        # A falsy-but-set env var beats a truthy conf line (env is consulted
        # first); an explicit kwarg still beats both.
        app = _make_app(
            {"AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED": "0"},
            conf="AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED=1\n",
        )
        self.assertFalse(app.config["ADOPTION_REPOSITORIES_ENABLED"])

    def test_conf_is_overridden_by_explicit_kwarg(self):
        app = _make_app(
            conf="AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED=1\n",
            adoption_repositories_enabled=False,
        )
        self.assertFalse(app.config["ADOPTION_REPOSITORIES_ENABLED"])


class ExplicitWinsTests(unittest.TestCase):
    def test_explicit_false_overrides_truthy_env(self):
        app = _make_app(
            {"AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED": "1"},
            adoption_repositories_enabled=False,
        )
        self.assertFalse(app.config["ADOPTION_REPOSITORIES_ENABLED"])
        self.assertNotIn("adoptions", app.extensions["fleet"]["services"])
        rules = _rules(app)
        self.assertNotIn("/api/adoptions", rules)


if __name__ == "__main__":
    unittest.main()