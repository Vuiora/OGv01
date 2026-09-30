from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _wc_l(path: Path) -> int:
    """Fast line count using system wc. Returns 0 on failure."""
    try:
        out = subprocess.check_output(["wc", "-l", str(path)], text=True).strip()
        return max(int(out.split()[0]), 0)
    except Exception:
        return 0


def _fmt_eta(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds:  # nan check
        return "?"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _parse_submit_time(s: Any) -> Optional[int]:
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp())
    except Exception:
        return None


def _year_from_ts(ts: int) -> int:
    return datetime.fromtimestamp(int(ts)).year


@dataclass
class CleanConfig:
    max_attempts: int = 20
    min_year: int = 2015
    max_year: int = 2030


def _basic_validate(obj: Dict[str, Any], cfg: CleanConfig) -> Tuple[Optional[Tuple[str, int, str, int, int]], Optional[str]]:
    """
    Return (user_id, t, pm_id, y, attempts) or drop reason.
    """
    user_id = obj.get("user_id")
    pm_id = obj.get("problem_id")
    y = obj.get("is_correct")
    attempts = obj.get("attempts")
    submit_time = obj.get("submit_time")

    if not isinstance(user_id, str) or not user_id.startswith("U_"):
        return None, "bad_user_id"
    if not isinstance(pm_id, str) or not pm_id.startswith("Pm_"):
        return None, "bad_problem_id"
    if y not in (0, 1):
        return None, "bad_is_correct"
    t = _parse_submit_time(submit_time)
    if t is None:
        return None, "bad_submit_time"
    yy = _year_from_ts(t)
    if yy < int(cfg.min_year) or yy > int(cfg.max_year):
        return None, "time_out_of_range"
    try:
        att = int(attempts) if attempts is not None else 1
    except Exception:
        att = 1
    if att <= 0:
        att = 1
    if att > int(cfg.max_attempts):
        return None, "too_many_attempts"
    return (user_id, int(t), pm_id, int(y), int(att)), None


def _run_sort(in_tsv: Path, out_tsv: Path, *, tmp_dir: Path, buffer_size: str = "40%") -> None:
    """
    External sort by (user_id asc, t asc). Input TSV columns:
      user_id \t t \t pm_id \t y \t attempts
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "sort",
        "-t",
        "\t",
        "-k1,1",
        "-k2,2n",
        "--buffer-size",
        str(buffer_size),
        "--temporary-directory",
        str(tmp_dir),
        str(in_tsv),
    ]
    with out_tsv.open("wb") as out:
        subprocess.run(cmd, check=True, stdout=out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Clean user-problem JSONL by external-sorting (user_id,t) then applying anti-cheat gap filtering."
    )
    ap.add_argument("--in-jsonl", required=True, help="Input: relations/user-problem.json (JSONL)")
    ap.add_argument("--out-jsonl", required=True, help="Output cleaned events JSONL")
    ap.add_argument("--drop-log", required=True, help="Output dropped records JSONL (reasons)")
    ap.add_argument("--max-attempts", type=int, default=20)
    ap.add_argument("--min-year", type=int, default=2015)
    ap.add_argument("--max-year", type=int, default=2030)
    ap.add_argument("--min-gap-sec", type=int, default=10, help="Drop events with consecutive gap < this seconds per user.")
    ap.add_argument("--tmp-dir", default="data/dkt/tmp_clean", help="Temp directory for external sort files")
    ap.add_argument("--buffer-size", default="40%", help="sort --buffer-size value (e.g. 40%% or 2G)")
    ap.add_argument("--total-lines", type=int, default=0, help="If >0, use as input total line count for progress.")
    ap.add_argument("--progress-every", type=int, default=1_000_000, help="Print progress every N input lines.")
    ap.add_argument("--max-lines", type=int, default=0, help="If >0, only read this many input lines (debug)")
    args = ap.parse_args()

    cfg = CleanConfig(max_attempts=int(args.max_attempts), min_year=int(args.min_year), max_year=int(args.max_year))
    in_path = Path(args.in_jsonl)
    out_path = Path(args.out_jsonl)
    drop_path = Path(args.drop_log)
    tmp_dir = Path(args.tmp_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    drop_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    stage1 = tmp_dir / "stage1.valid.tsv"
    stage2 = tmp_dir / "stage2.sorted.tsv"

    total_lines = int(args.total_lines) if int(args.total_lines) > 0 else _wc_l(in_path)
    if total_lines > 0:
        print(f"[info] input_total_lines={total_lines}")

    lines_read = 0
    json_dicts = 0
    kept_stage1 = 0
    dropped_stage1 = 0
    parse_fail = 0
    non_dict = 0
    t0 = time.time()

    # Stage 1: basic validation -> TSV
    with stage1.open("w", encoding="utf-8") as tsv, drop_path.open("w", encoding="utf-8") as dropf:
        with in_path.open("r", encoding="utf-8") as f:
            for line in f:
                lines_read += 1
                if int(args.max_lines) > 0 and lines_read > int(args.max_lines):
                    break

                if int(args.progress_every) > 0 and lines_read % int(args.progress_every) == 0:
                    dt = max(time.time() - t0, 1e-6)
                    speed = lines_read / dt
                    if total_lines > 0:
                        pct = 100.0 * (lines_read / total_lines)
                        eta = _fmt_eta((total_lines - lines_read) / max(speed, 1e-9))
                        print(
                            f"[stage1] {lines_read}/{total_lines} ({pct:.2f}%) "
                            f"kept={kept_stage1} dropped={dropped_stage1} speed={speed:,.0f} l/s eta={eta}"
                        )
                    else:
                        print(f"[stage1] lines={lines_read} kept={kept_stage1} dropped={dropped_stage1} speed={speed:,.0f} l/s")

                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    parse_fail += 1
                    continue
                if not isinstance(obj, dict):
                    non_dict += 1
                    continue
                json_dicts += 1

                rec, reason = _basic_validate(obj, cfg)
                if rec is None:
                    dropped_stage1 += 1
                    dropf.write(
                        json.dumps(
                            {
                                "kind": "drop",
                                "stage": 1,
                                "reason": reason,
                                "user_id": obj.get("user_id"),
                                "problem_id": obj.get("problem_id"),
                                "submit_time": obj.get("submit_time"),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    continue
                user_id, t, pm_id, y, attempts = rec
                kept_stage1 += 1
                tsv.write(f"{user_id}\t{t}\t{pm_id}\t{y}\t{attempts}\n")

    # Stage 2: sort
    print("[stage2] sorting...")
    _run_sort(stage1, stage2, tmp_dir=(tmp_dir / "sorttmp"), buffer_size=str(args.buffer_size))
    print("[stage2] sorting done")

    # Stage 3: anti-cheat gap filtering (per user, time-ordered)
    kept = 0
    dropped_gap = 0
    processed_stage3 = 0
    stage3_total = kept_stage1
    t3 = time.time()
    last_user: Optional[str] = None
    last_t: Optional[int] = None

    with stage2.open("r", encoding="utf-8") as tsv, out_path.open("w", encoding="utf-8") as out, drop_path.open(
        "a", encoding="utf-8"
    ) as dropf:
        for line in tsv:
            processed_stage3 += 1
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            user_id, t_s, pm_id, y_s, att_s = parts[0], parts[1], parts[2], parts[3], parts[4]
            try:
                t = int(t_s)
                y = int(y_s)
                att = int(att_s)
            except Exception:
                continue

            if last_user != user_id:
                last_user = user_id
                last_t = None

            if last_t is not None and (t - last_t) < int(args.min_gap_sec):
                dropped_gap += 1
                dropf.write(
                    json.dumps(
                        {"kind": "drop", "stage": 3, "reason": "too_fast_gap", "user_id": user_id, "t": t, "prev_t": last_t},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                last_t = t
                continue

            last_t = t
            kept += 1
            out.write(json.dumps({"user_id": user_id, "t": t, "pm_id": pm_id, "y": y, "attempts": att}, ensure_ascii=False) + "\n")
            if int(args.progress_every) > 0 and processed_stage3 % int(args.progress_every) == 0:
                dt = max(time.time() - t3, 1e-6)
                speed = processed_stage3 / dt
                if stage3_total > 0:
                    pct = 100.0 * (processed_stage3 / stage3_total)
                    eta = _fmt_eta((stage3_total - processed_stage3) / max(speed, 1e-9))
                    print(
                        f"[stage3] {processed_stage3}/{stage3_total} ({pct:.2f}%) "
                        f"kept={kept} dropped_gap={dropped_gap} speed={speed:,.0f} l/s eta={eta}"
                    )
                else:
                    print(f"[stage3] processed={processed_stage3} kept={kept} dropped_gap={dropped_gap} speed={speed:,.0f} l/s")

    print("=== cleaned(sorted) ===")
    print("in :", str(in_path))
    print("out:", str(out_path))
    print("drop:", str(drop_path))
    print(
        "stage1_lines_read:",
        lines_read,
        "stage1_json_dicts:",
        json_dicts,
        "stage1_kept:",
        kept_stage1,
        "stage1_dropped:",
        dropped_stage1,
        "stage1_parse_fail:",
        parse_fail,
        "stage1_non_dict:",
        non_dict,
    )
    print("stage3_kept:", kept, "stage3_dropped_gap:", dropped_gap)
    print("tmp_dir:", str(tmp_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


