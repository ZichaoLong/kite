"""Per-prompt volatile assistant-text transcript (streaming-cards contract).

Ported from FOCUS ``bot/execution_transcript.py`` and renamed to KITE terms
(docs/research/focus-assets-map.md §1). Volatile deltas are enhancement,
never evidence: the transcript is in-memory only, it is rebuilt from the
REST snapshot after a gap/resync/restart, and the durable path alone drives
every state transition (docs/contracts/streaming-cards.md §1).

Offset discipline (kap's in-flight turn tracker): each ``assistant.delta``
carries a pre-append offset within the turn's current step, and the offset
resets at every ``turn.step.started`` (earlier steps are banked here). An
offset that jumps forward is a gap — the missing text is never guessed; the
caller falls into the snapshot-rebuild path, exactly once per gap episode.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Card projection budget (FOCUS parity): the streamed reply body is char-
# budgeted; the terminal card enforces its own utf-8 byte budget instead.
DEFAULT_STREAM_REPLY_CHAR_LIMIT = 12000

# Fail-closed overflow cap (§4.1): a runaway stream stops being trusted and
# heals from the snapshot instead of growing the transcript without bound.
MAX_TRANSCRIPT_CHARS = DEFAULT_STREAM_REPLY_CHAR_LIMIT * 4

_TRUNCATION_NOTICE = "\n\n**[回复过长，执行卡仅显示部分内容]**"
_COMPACT_TRUNCATION_NOTICE = "[回复过长]"


@dataclass
class StreamingTranscript:
    """Accumulates one prompt's volatile assistant text with gap detection.

    State: the current step's text (its length IS the expected next offset),
    the banked earlier steps, and a gapped latch. While gapped, appends
    report the gap without mutating, so the caller rebuilds exactly once;
    ``rebuild_from_snapshot`` clears the latch and re-baselines the stream.

    Offsets are tracked in **UTF-16 code units** — upstream stamps them with
    JS ``String.length`` (`inFlightTurnTracker.ts`), so a non-BMP character
    (emoji) counts 2, and a Python ``len`` comparison would false-gap on
    every subsequent delta (audit H2).
    """

    _step_text: str = ""
    _segments: list[str] = field(default_factory=list)
    _banked_chars: int = 0
    _gapped: bool = False

    @staticmethod
    def _offset_len(text: str) -> int:
        """Length in UTF-16 code units (upstream's offset unit)."""
        return len(text.encode("utf-16-le")) // 2

    @property
    def gapped(self) -> bool:
        return self._gapped

    @property
    def expected_offset(self) -> int:
        """The pre-append offset the next delta of the current step must carry."""
        return self._offset_len(self._step_text)

    def append_delta(self, offset: int, text_delta: str) -> bool:
        """Append one delta; returns True on an offset gap (caller rebuilds).

        Nothing is mutated on a gap: guessing the missing text is worse than
        showing less (§3.5). A backward offset at 0 is an upstream step
        boundary (offsets are step-relative and reset at turn.step.started),
        so the finished step is banked; any other backward offset means the
        head of a step was lost, which is also a gap.
        """
        if not text_delta:
            return self._gapped
        if self._gapped:
            return True
        offset = max(int(offset), 0)
        expected = self._offset_len(self._step_text)
        if offset != expected:
            if offset > expected or offset != 0:
                self._gapped = True
                return True
            # Upstream step boundary: bank the finished step, start the next.
            if self._step_text:
                self._segments.append(self._step_text)
                self._banked_chars += len(self._step_text)
            self._step_text = ""
        if self._banked_chars + len(self._step_text) + len(text_delta) > MAX_TRANSCRIPT_CHARS:
            self._gapped = True
            return True
        self._step_text += text_delta
        return False

    def rebuild_from_snapshot(self, current_step_text: str) -> None:
        """Re-baseline from the snapshot's in-flight assistant text (§1.2).

        The snapshot text is step-relative — the same reference frame as the
        delta offsets — so it re-seeds both the accumulated text and the
        expected offset after a gap/resync/restart. Earlier steps are durable
        upstream (they surface on the terminal card) and are not re-fetched.
        """
        self._step_text = str(current_step_text or "")
        self._segments = []
        self._banked_chars = 0
        self._gapped = False

    def reconcile(self, authoritative_text: str) -> None:
        """Reconcile a completed turn's authoritative text over the deltas.

        Monotonic, never shrink (§3.5): a shorter or empty read — the normal
        turn-end-vs-final-flush race — must not clobber longer content the
        stream already showed.
        """
        normalized = str(authoritative_text or "")
        if not normalized:
            return
        if len(normalized) < len(self._step_text):
            return
        self._step_text = normalized

    def full_text(self) -> str:
        parts = [segment for segment in self._segments if segment]
        if self._step_text:
            parts.append(self._step_text)
        return "\n\n".join(parts)

    def project_for_card(self, char_limit: int = DEFAULT_STREAM_REPLY_CHAR_LIMIT) -> str:
        """Char-budgeted card projection with a truncation notice (§3.7)."""
        text = self.full_text()
        limit = max(int(char_limit), 0)
        if len(text) <= limit:
            return text
        if limit <= 0:
            return ""
        notice = (
            _TRUNCATION_NOTICE
            if limit > len(_COMPACT_TRUNCATION_NOTICE) + 16
            else _COMPACT_TRUNCATION_NOTICE
        )
        if len(notice) >= limit:
            return notice[:limit]
        return text[: limit - len(notice)] + notice
