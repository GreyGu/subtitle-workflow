#!/usr/bin/env python3
"""Maintain compact, resumable subtitle task evidence without printing subtitle text."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

WORKFLOW_VERSION = "1.0"
STAGES = ("candidates", "structure", "mapping", "timing", "translation", "audio", "delivery")
STATUSES = ("pass", "issues", "pending", "not_required")
SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sup", ".idx", ".sub"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_state(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("episodes"), dict):
        raise ValueError("Unsupported or invalid progress file.")
    return data


def write_new(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def write_state(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        raise FileExistsError(f"Temporary state file already exists: {tmp}")
    with tmp.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    tmp.replace(path)


def key_for(episode: int, language: str) -> str:
    return f"S{episode:02d}|{language}"


def cmd_init(args) -> dict:
    path = Path(args.state).resolve()
    if path.exists():
        raise FileExistsError(f"Progress file already exists: {path}")
    languages = [x.strip() for x in args.languages.split(",") if x.strip()]
    if not languages or len(set(languages)) != len(languages):
        raise ValueError("Languages must be a non-empty, comma-separated unique list.")
    if args.episodes < 1 or args.season < 1:
        raise ValueError("Season and episode count must be positive.")
    rows = {}
    for episode in range(1, args.episodes + 1):
        for language in languages:
            rows[key_for(episode, language)] = {
                "episode": episode,
                "language": language,
                "stages": {stage: None for stage in STAGES},
            }
    data = {
        "schema_version": 1,
        "workflow_version": WORKFLOW_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": args.title,
        "season": args.season,
        "release": args.release,
        "languages": languages,
        "episode_count": args.episodes,
        "episodes": rows,
    }
    write_new(path, data)
    return {"status": "initialized", "state": str(path), "episodes": args.episodes, "languages": languages}


def cmd_index(args) -> dict:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    files = []
    groups = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in SUBTITLE_SUFFIXES:
            continue
        sha = digest(path)
        row = {"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": sha}
        match = re.search(r"S(\d{1,2})\s*E(\d{1,3})", path.name, re.I)
        if match:
            row["season"], row["episode"] = int(match.group(1)), int(match.group(2))
        groups.setdefault(sha, []).append(row["path"])
        files.append(row)
    unique = [{"sha256": sha, "paths": paths} for sha, paths in groups.items()]
    manifest_path = Path(args.manifest).resolve()
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(root),
        "file_count": len(files),
        "unique_content_count": len(unique),
        "duplicate_file_count": len(files) - len(unique),
        "files": files,
        "duplicate_groups": [group for group in unique if len(group["paths"]) > 1],
    }
    write_new(manifest_path, manifest)
    return {"status": "indexed", "manifest": str(manifest_path), "files": len(files),
            "unique_contents": len(unique), "duplicates": len(files) - len(unique),
            "duplicate_groups": sum(len(x["paths"]) > 1 for x in unique)}


def cmd_record(args) -> dict:
    state_path = Path(args.state).resolve()
    data = read_state(state_path)
    key = key_for(args.episode, args.language)
    if key not in data["episodes"]:
        raise ValueError(f"Episode/language is not in progress file: {key}")
    if args.stage not in STAGES:
        raise ValueError("Unknown stage.")
    source = Path(args.input).resolve()
    if not source.is_file():
        raise FileNotFoundError(str(source))
    source_sha = digest(source)
    config_sha = hashlib.sha256(args.config.encode("utf-8")).hexdigest()
    row = data["episodes"][key]
    stage_index = STAGES.index(args.stage)
    previous = row["stages"].get(args.stage)
    selected_tool = args.tool_version or WORKFLOW_VERSION
    changed = previous is not None and (
        previous.get("input_sha256") != source_sha
        or previous.get("config_sha256") != config_sha
        or previous.get("tool_version") != selected_tool
    )
    if changed:
        for later in STAGES[stage_index:]:
            row["stages"][later] = None
    report = Path(args.report).resolve() if args.report else None
    report_sha = digest(report) if report and report.is_file() else None
    if report and not report.is_file():
        raise FileNotFoundError(str(report))
    row["stages"][args.stage] = {
        "status": args.status,
        "input": str(source),
        "input_sha256": source_sha,
        "config_sha256": config_sha,
        "report": str(report) if report else None,
        "report_sha256": report_sha,
        "tool_version": selected_tool,
        "summary": args.summary,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data["updated_at"] = row["stages"][args.stage]["updated_at"]
    write_state(state_path, data)
    return {"status": "recorded", "episode": key, "stage": args.stage,
            "stage_status": args.status, "input_changed": changed,
            "invalidated_stages": list(STAGES[stage_index + 1:]) if changed else []}


def cmd_check(args) -> dict:
    data = read_state(Path(args.state).resolve())
    key = key_for(args.episode, args.language)
    if key not in data["episodes"]:
        raise ValueError(f"Episode/language is not in progress file: {key}")
    source = Path(args.input).resolve()
    if not source.is_file():
        raise FileNotFoundError(str(source))
    record = data["episodes"][key]["stages"].get(args.stage)
    current_sha = digest(source)
    config_sha = hashlib.sha256(args.config.encode("utf-8")).hexdigest()
    report_path = Path(record["report"]) if record and record.get("report") else None
    report_ok = bool(report_path and report_path.is_file()
                     and record.get("report_sha256") == digest(report_path))
    reusable = bool(record and record.get("input_sha256") == current_sha
                    and record.get("config_sha256") == config_sha
                    and record.get("tool_version") == (args.tool_version or WORKFLOW_VERSION)
                    and record.get("status") in {"pass", "issues", "not_required"}
                    and report_ok)
    return {"status": "reusable" if reusable else "rerun_required", "episode": key,
            "stage": args.stage, "input_sha256": current_sha,
            "recorded_status": record.get("status") if record else None}


def cmd_status(args) -> dict:
    data = read_state(Path(args.state).resolve())
    counts = {status: 0 for status in STATUSES}
    counts["unrecorded"] = 0
    rows = []
    for key, episode in data["episodes"].items():
        stages = episode["stages"]
        completed = [name for name in STAGES if stages.get(name) is not None]
        next_stage = next((name for name in STAGES if stages.get(name) is None), None)
        latest = stages[completed[-1]] if completed else None
        if latest is None:
            counts["unrecorded"] += 1
        else:
            counts[latest["status"]] += 1
        if args.details:
            rows.append({"episode": key, "last_stage": completed[-1] if completed else None,
                         "last_status": latest["status"] if latest else "unrecorded",
                         "next_stage": next_stage, "summary": latest.get("summary") if latest else None})
    result = {"status": "progress", "title": data.get("title"), "season": data.get("season"),
              "release": data.get("release"), "episode_language_pairs": len(data["episodes"]),
              "latest_stage_counts": counts}
    if args.details:
        result["items"] = rows
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a per-episode progress file; refuses overwrite.")
    init.add_argument("state")
    init.add_argument("--title", required=True)
    init.add_argument("--season", type=int, default=1)
    init.add_argument("--episodes", type=int, required=True)
    init.add_argument("--languages", required=True, help="Comma-separated language labels.")
    init.add_argument("--release", required=True, help="Exact video release name or 'no local video'.")
    init.set_defaults(func=cmd_init)
    index = sub.add_parser("index", help="Fingerprint and deduplicate subtitle files.")
    index.add_argument("directory")
    index.add_argument("--manifest", required=True)
    index.set_defaults(func=cmd_index)
    record = sub.add_parser("record", help="Save one episode/language/stage result.")
    record.add_argument("state")
    record.add_argument("--episode", type=int, required=True)
    record.add_argument("--language", required=True)
    record.add_argument("--stage", choices=STAGES, required=True)
    record.add_argument("--input", required=True, help="File whose bytes this result was based on.")
    record.add_argument("--report", help="Optional full local report path.")
    record.add_argument("--status", choices=STATUSES, required=True)
    record.add_argument("--tool-version")
    record.add_argument("--config", default="", help="Parameters that affect this stage; keep the same string for reuse.")
    record.add_argument("--summary", default="", help="Short anomaly/status summary; never paste subtitle text.")
    record.set_defaults(func=cmd_record)
    check = sub.add_parser("check", help="Check whether a saved stage can be reused.")
    check.add_argument("state")
    check.add_argument("--episode", type=int, required=True)
    check.add_argument("--language", required=True)
    check.add_argument("--stage", choices=STAGES, required=True)
    check.add_argument("--input", required=True)
    check.add_argument("--tool-version")
    check.add_argument("--config", default="", help="Same stage parameters used when recording the result.")
    check.set_defaults(func=cmd_check)
    status = sub.add_parser("status", help="Print compact progress, not report contents.")
    status.add_argument("state")
    status.add_argument("--details", action="store_true")
    status.set_defaults(func=cmd_status)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = args.func(args)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
