"""StreamingTranscript unit tests (streaming-cards contract §3.5/§3.7, §5)."""

from __future__ import annotations

import unittest

from kite.streaming_transcript import MAX_TRANSCRIPT_CHARS, StreamingTranscript


class AppendDeltaTests(unittest.TestCase):
    def test_appends_consecutive_offsets(self) -> None:
        transcript = StreamingTranscript()
        self.assertFalse(transcript.append_delta(0, "你好"))
        self.assertFalse(transcript.append_delta(2, "，世界"))
        self.assertEqual(transcript.full_text(), "你好，世界")
        self.assertEqual(transcript.expected_offset, 5)
        self.assertFalse(transcript.gapped)

    def test_empty_delta_is_ignored(self) -> None:
        transcript = StreamingTranscript()
        self.assertFalse(transcript.append_delta(0, ""))
        self.assertEqual(transcript.full_text(), "")

    def test_forward_offset_jump_is_a_gap_and_mutates_nothing(self) -> None:
        transcript = StreamingTranscript()
        self.assertFalse(transcript.append_delta(0, "abc"))
        self.assertTrue(transcript.append_delta(10, "xyz"))
        self.assertEqual(transcript.full_text(), "abc")
        self.assertTrue(transcript.gapped)

    def test_backward_offset_mid_step_is_a_gap(self) -> None:
        transcript = StreamingTranscript()
        self.assertFalse(transcript.append_delta(0, "abcdef"))
        # An offset inside already-accumulated text (but not a step boundary)
        # means the head of the new step was lost: gap, never guess.
        self.assertTrue(transcript.append_delta(3, "xy"))
        self.assertTrue(transcript.gapped)

    def test_offset_reset_to_zero_banks_the_finished_step(self) -> None:
        transcript = StreamingTranscript()
        self.assertFalse(transcript.append_delta(0, "第一步"))
        self.assertFalse(transcript.append_delta(3, "。"))
        # turn.step.started resets the offset upstream: the next step begins
        # at 0 and the finished step is banked.
        self.assertFalse(transcript.append_delta(0, "第二步"))
        self.assertEqual(transcript.full_text(), "第一步。\n\n第二步")
        self.assertEqual(transcript.expected_offset, 3)

    def test_gapped_latch_reports_the_gap_until_rebuilt(self) -> None:
        transcript = StreamingTranscript()
        self.assertTrue(transcript.append_delta(5, "x"))
        # While gapped every delta reports the gap without mutating, so the
        # caller triggers exactly one rebuild per gap episode.
        self.assertTrue(transcript.append_delta(6, "y"))
        self.assertEqual(transcript.full_text(), "")

        transcript.rebuild_from_snapshot("xy")
        self.assertFalse(transcript.gapped)
        self.assertEqual(transcript.expected_offset, 2)
        self.assertFalse(transcript.append_delta(2, "z"))
        self.assertEqual(transcript.full_text(), "xyz")

    def test_overflow_latches_the_gap(self) -> None:
        transcript = StreamingTranscript()
        transcript.rebuild_from_snapshot("x" * MAX_TRANSCRIPT_CHARS)
        self.assertTrue(transcript.append_delta(MAX_TRANSCRIPT_CHARS, "y"))

    def test_negative_offset_is_clamped(self) -> None:
        transcript = StreamingTranscript()
        self.assertFalse(transcript.append_delta(-1, "abc"))
        self.assertEqual(transcript.full_text(), "abc")


class RebuildFromSnapshotTests(unittest.TestCase):
    def test_reseed_replaces_text_and_clears_banked_steps(self) -> None:
        transcript = StreamingTranscript()
        transcript.append_delta(0, "step1")
        transcript.append_delta(0, "step2")
        transcript.rebuild_from_snapshot("当前步文本")
        self.assertEqual(transcript.full_text(), "当前步文本")
        self.assertEqual(transcript.expected_offset, len("当前步文本"))

    def test_reseed_with_empty_text_restarts_the_stream(self) -> None:
        transcript = StreamingTranscript()
        transcript.append_delta(0, "abc")
        transcript.rebuild_from_snapshot("")
        self.assertEqual(transcript.full_text(), "")
        self.assertFalse(transcript.append_delta(0, "d"))
        self.assertEqual(transcript.full_text(), "d")


class ReconcileTests(unittest.TestCase):
    def test_authoritative_text_replaces_the_deltas(self) -> None:
        transcript = StreamingTranscript()
        transcript.append_delta(0, "abc")
        transcript.reconcile("abcdef")
        self.assertEqual(transcript.full_text(), "abcdef")
        # The expected offset follows the reconciled length.
        self.assertFalse(transcript.append_delta(6, "g"))
        self.assertEqual(transcript.full_text(), "abcdefg")

    def test_shorter_stale_text_never_shrinks(self) -> None:
        transcript = StreamingTranscript()
        transcript.append_delta(0, "abcdef")
        transcript.reconcile("abc")
        self.assertEqual(transcript.full_text(), "abcdef")

    def test_empty_authoritative_text_is_ignored(self) -> None:
        transcript = StreamingTranscript()
        transcript.append_delta(0, "abc")
        transcript.reconcile("")
        self.assertEqual(transcript.full_text(), "abc")

    def test_equal_length_replaces(self) -> None:
        transcript = StreamingTranscript()
        transcript.append_delta(0, "abc")
        transcript.reconcile("abd")
        self.assertEqual(transcript.full_text(), "abd")


class ProjectionTests(unittest.TestCase):
    def test_under_budget_is_unchanged(self) -> None:
        transcript = StreamingTranscript()
        transcript.append_delta(0, "短文本")
        self.assertEqual(transcript.project_for_card(100), "短文本")

    def test_over_budget_keeps_the_head_with_a_notice(self) -> None:
        transcript = StreamingTranscript()
        text = "一" * 500
        transcript.append_delta(0, text)
        projected = transcript.project_for_card(120)
        self.assertEqual(len(projected), 120)
        self.assertTrue(projected.startswith("一" * 80))
        self.assertIn("回复过长", projected)

    def test_tiny_budget_uses_the_compact_notice(self) -> None:
        transcript = StreamingTranscript()
        transcript.append_delta(0, "x" * 100)
        projected = transcript.project_for_card(10)
        self.assertEqual(len(projected), 10)
        self.assertIn("回复过长", projected)

    def test_zero_budget_projects_nothing(self) -> None:
        transcript = StreamingTranscript()
        transcript.append_delta(0, "abc")
        self.assertEqual(transcript.project_for_card(0), "")

    def test_unclosed_fence_mid_stream_projects_verbatim(self) -> None:
        # Fence tolerance happens at render time (runtime markdown variant);
        # the projection must pass the raw split-token text through untouched.
        transcript = StreamingTranscript()
        transcript.append_delta(0, "看代码：\n```python\nprint(")
        projected = transcript.project_for_card(1000)
        self.assertEqual(projected, "看代码：\n```python\nprint(")


if __name__ == "__main__":
    unittest.main()
