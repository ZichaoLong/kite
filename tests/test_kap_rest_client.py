import unittest

import fake_kap
from kite.adapters.kap_server import (
    KapError,
    KapRestClient,
    KapTransportError,
)


class KapRestClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = fake_kap.FakeKapState()
        self.server = fake_kap.make_rest_server(self.state)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.client = KapRestClient("127.0.0.1", self.port, self.state.token)

    def test_meta_parses_normalized_fields(self) -> None:
        meta = self.client.meta()
        self.assertEqual(meta.server_version, "0.0.2-fake")
        self.assertEqual(meta.backend, "v2")
        self.assertTrue(meta.server_id)

    def test_list_sessions_maps_normalized_summary(self) -> None:
        self.state.create_session("s-1", title="alpha", cwd="/tmp/work")
        self.state.create_session("s-2", title="beta")

        sessions = {s.session_id: s for s in self.client.list_sessions()}

        self.assertEqual(set(sessions), {"s-1", "s-2"})
        self.assertEqual(sessions["s-1"].title, "alpha")
        self.assertEqual(sessions["s-1"].cwd, "/tmp/work")
        self.assertFalse(sessions["s-1"].busy)
        self.assertIsNone(sessions["s-2"].cwd)

    def test_get_prompts_reports_queue_depth(self) -> None:
        session = self.state.create_session("s-1")
        session.active_prompt = "p-active"
        session.queued_prompts = ["p-1", "p-2"]

        queue = self.client.get_prompts("s-1")

        self.assertEqual(queue.active_prompt_id, "p-active")
        self.assertEqual(queue.queued_prompt_ids, ("p-1", "p-2"))
        self.assertEqual(queue.queue_depth, 2)

    def test_get_snapshot_exposes_cursor_and_work_state(self) -> None:
        session = self.state.create_session("s-1")
        session.busy = True
        session.pending_interaction = "approval"
        self.state.append_event(session, "turn.started", {"n": 1})
        self.state.append_event(session, "tool.call.started", {"n": 2})

        snapshot = self.client.get_snapshot("s-1")

        self.assertEqual(snapshot.as_of_seq, 2)
        self.assertEqual(snapshot.epoch, session.epoch)
        self.assertTrue(snapshot.busy)
        self.assertEqual(snapshot.pending_interaction, "approval")
        self.assertEqual(snapshot.cursor.seq, 2)
        self.assertEqual(snapshot.cursor.epoch, session.epoch)

    def test_business_error_raises_kap_error_with_code(self) -> None:
        with self.assertRaises(KapError) as ctx:
            self.client.get_prompts("missing")
        self.assertEqual(ctx.exception.code, 40401)
        self.assertIn("session not found", ctx.exception.msg)

    def test_wrong_token_raises_kap_error_401(self) -> None:
        client = KapRestClient("127.0.0.1", self.port, "wrong-token")
        with self.assertRaises(KapError) as ctx:
            client.meta()
        self.assertEqual(ctx.exception.code, 40101)
        self.assertEqual(ctx.exception.http_status, 401)

    def test_transport_error_on_unreachable_server(self) -> None:
        client = KapRestClient("127.0.0.1", 1, "tok", timeout=1.0)
        with self.assertRaises(KapTransportError):
            client.meta()

    def test_shutdown_sends_no_content_type_on_empty_body(self) -> None:
        self.client.shutdown()
        self.assertTrue(self.state.shutdown_requested)
        # Spike S4 gotcha: Fastify rejects an empty body carrying Content-Type.
        self.assertIsNone(self.state.last_shutdown_content_type)

    def test_post_with_body_round_trip(self) -> None:
        data = self.client.post("/sessions", {"title": "created", "metadata": {"cwd": "/x"}})
        self.assertEqual(data["title"], "created")
        sessions = self.client.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].cwd, "/x")


if __name__ == "__main__":
    unittest.main()
