"""Build a review-only edit decision list from local signal-level audio analysis."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from namakadhai.audio_analysis import (
    AudioAnalysisError,
    AudioAnalysisOutputExistsError,
    AudioAnalysisResult,
    analyze_audio,
)
from namakadhai.media_review import MediaReviewError, preflight_media

DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.json"
DEFAULT_PADDING_BEFORE_SECONDS = 0.25
DEFAULT_PADDING_AFTER_SECONDS = 0.25
NEAR_CLIPPING_DBFS = -1.0
LOW_MEAN_VOLUME_DBFS = -30.0


class EditDecisionError(RuntimeError):
    """Base error for review-only edit decision list creation."""


class EditDecisionOutputExistsError(EditDecisionError):
    """An edit decision list already exists and will not be overwritten."""


@dataclass(frozen=True)
class EditDecisionOutput:
    report_path: Path
    analysis: AudioAnalysisResult


def _audio_issues(analysis: AudioAnalysisResult) -> tuple[str, ...]:
    if not analysis.has_audio:
        return ("No audio stream was found.",)
    if analysis.levels is None:
        return ("Audio levels could not be measured.",)

    issues: list[str] = []
    mean = analysis.levels.mean_volume_db
    peak = analysis.levels.max_volume_db
    if mean is not None and mean <= LOW_MEAN_VOLUME_DBFS:
        issues.append(
            f"Low overall level ({mean:.2f} dBFS mean; review audibility and noise floor)."
        )
    if peak is not None and peak >= NEAR_CLIPPING_DBFS:
        issues.append(
            f"Peak is near full scale ({peak:.2f} dBFS; listen for clipping or harshness)."
        )
    return tuple(issues)


def _render_edit_decision_list(
    analysis: AudioAnalysisResult,
    *,
    file_name: str,
    duration_seconds: float | None,
    padding_before_seconds: float = DEFAULT_PADDING_BEFORE_SECONDS,
    padding_after_seconds: float = DEFAULT_PADDING_AFTER_SECONDS,
) -> str:
    duration = (
        f"{duration_seconds:.3f} seconds"
        if duration_seconds is not None
        else "unavailable"
    )
    lines = [
        "# Edit decision list",
        "",
        f"- **File:** `{file_name}`",
        f"- **Duration:** {duration}",
        f"- **Silence threshold:** {analysis.threshold_db:.2f} dBFS",
        f"- **Minimum quiet interval:** {analysis.minimum_duration_seconds:.3f} seconds",
        "",
        "Review-only proposals. No cuts, audio processing, or video rendering were "
        "applied. Signal-level silence detection cannot distinguish quiet speech "
        "from pauses; listen to every candidate and approve or reject it manually.",
        "",
        "## Proposed cuts",
        "",
    ]
    if analysis.quiet_intervals:
        lines.extend(
            (
                "| # | Detected quiet span (s) | Proposed removal (s) | Duration (s) | Decision |",
                "|---:|---:|---:|---:|---|",
            )
        )
        for index, interval in enumerate(analysis.quiet_intervals, start=1):
            cut_start = interval.start_seconds + padding_before_seconds
            cut_end = interval.end_seconds - padding_after_seconds
            if cut_end > cut_start:
                proposed = f"{cut_start:.3f}–{cut_end:.3f}"
                cut_duration = f"{cut_end - cut_start:.3f}"
            else:
                proposed = "None (too short after padding)"
                cut_duration = "0.000"
            lines.append(
                f"| {index} | {interval.start_seconds:.3f}–{interval.end_seconds:.3f} "
                f"| {proposed} | {cut_duration} | [ ] Approve [ ] Reject |"
            )
    else:
        lines.append(
            "No intervals met the configured quiet threshold and minimum duration; "
            "no cut is proposed."
        )

    lines.extend(("", "## Audio review findings", ""))
    issues = _audio_issues(analysis)
    if issues:
        lines.extend(f"- **Review:** {issue}" for issue in issues)
    elif analysis.levels is not None:
        mean = (
            f"{analysis.levels.mean_volume_db:.2f} dBFS"
            if analysis.levels.mean_volume_db is not None
            else "unavailable"
        )
        peak = (
            f"{analysis.levels.max_volume_db:.2f} dBFS"
            if analysis.levels.max_volume_db is not None
            else "unavailable"
        )
        lines.append(
            f"No threshold-based level warning. Measured mean: {mean}; peak: {peak}. "
            "Listen for noise, distortion, and inconsistent loudness."
        )
    else:
        lines.append("Audio-level findings unavailable.")
    lines.extend(
        (
            "",
            "## Approval",
            "",
            "- Approved candidate numbers:",
            "- Rejected candidate numbers:",
            "- Reviewer notes:",
            "",
            "This EDL is not an instruction to an editor and must be reviewed before "
            "any edit is applied.",
            "",
        )
    )
    return "\n".join(lines)


def create_edit_decision_list(
    media_path: str | Path,
    *,
    settings_path: str | Path | None = None,
) -> EditDecisionOutput:
    """Analyze pauses and levels, then write a separate review-only EDL."""
    metadata, review_paths = preflight_media(
        media_path,
        settings_path=settings_path,
    )
    review_directory = review_paths.project_directory / "edit_review"
    report_path = review_directory / "edit_decision_list.md"
    analysis_path = review_directory / "audio_analysis.md"
    if report_path.exists():
        raise EditDecisionOutputExistsError(
            "Edit decision list already exists; refusing to overwrite "
            f"{report_path.name}."
        )
    if analysis_path.exists():
        raise AudioAnalysisOutputExistsError(
            "Edit-review audio analysis already exists; refusing to overwrite "
            f"{analysis_path.name}."
        )

    analysis = analyze_audio(
        media_path,
        settings_path=settings_path,
        report_path=analysis_path,
    )
    report = _render_edit_decision_list(
        analysis,
        file_name=metadata.file_name,
        duration_seconds=metadata.duration_seconds,
    )
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("x", encoding="utf-8", newline="") as report_file:
            report_file.write(report)
    except FileExistsError as exc:
        raise EditDecisionOutputExistsError(
            "Edit decision list appeared during analysis; it was not overwritten."
        ) from exc
    except OSError as exc:
        raise EditDecisionError("Could not write the edit decision list.") from exc
    return EditDecisionOutput(report_path=report_path, analysis=analysis)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a review-only edit decision list from quiet intervals and "
            "audio levels. No media edits are applied."
        )
    )
    parser.add_argument("video", type=Path, help="Path to an existing video file.")
    parser.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        help="Project settings JSON.",
    )
    arguments = parser.parse_args(argv)
    try:
        result = create_edit_decision_list(
            arguments.video,
            settings_path=arguments.settings,
        )
    except (
        AudioAnalysisError,
        EditDecisionError,
        MediaReviewError,
        FileNotFoundError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Edit decision list: {result.report_path}")
    print(f"Candidate quiet intervals: {len(result.analysis.quiet_intervals)}")
    for issue in _audio_issues(result.analysis):
        print(f"Audio review: {issue}")
    print("No media changes were applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
