"""Cheap pin for prompts/classify.md's thin-summary title guidance (M3.5a
follow-up, reviewer IMPORTANT finding): real-world thin-summary materials
(e.g. a dropbox assignment whose instructions field was blank, so its
summary carries almost nothing) gain nothing from the metadata pass if the
existing "judge by the summary, not the title" rule still forbids the only
evidence they have -- their title naming the course's own topic vocabulary.

This is a STRING-PRESENCE check on the prompt file, not a behavioral proof.
It pins the guidance existing in the prompt text; it cannot prove the model
actually follows it (whether "Assignment 5: Databases + Basic SQL" really
gets filed under a databases/SQL topic at moderate confidence, and not
higher, and not for every material) -- that's only measurable in a live
validation run against the real backend.
"""

from __future__ import annotations

from importlib import resources


def _classify_prompt() -> str:
    return (
        resources.files("brightspace_agent.agents.prompts")
        .joinpath("classify.md")
        .read_text(encoding="utf-8")
    )


def test_classify_prompt_carries_thin_summary_title_guidance():
    prompt = _classify_prompt()
    lower = prompt.lower()

    # The carve-out exists and is named as an exception to (not a repeal of)
    # the anti-title-overreach rule.
    assert "thin" in lower
    assert "title" in lower and "key terms" in lower

    # It caps confidence rather than opening the door to a confident guess.
    assert "moderate confidence" in lower

    # The anti-title-overreach rule itself must still be intact for
    # materials that DO have a substantive summary -- this guidance is an
    # exception, not a replacement.
    assert "judge by the summary" in lower
