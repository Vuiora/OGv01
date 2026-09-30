from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_jsonl_line(f, obj: Dict[str, Any]) -> None:
    f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _group_from_kid(k_id: str) -> str:
    """
    默认从 k_id 后缀抽取 group_id（领域）：
      K_xxx_物理学 -> 物理学
    若没有 '_'，则返回 'unknown'。
    """
    s = str(k_id or "").strip()
    if "_" not in s:
        return "unknown"
    return s.rsplit("_", 1)[-1] or "unknown"


def _sort_file(
    in_path: Path,
    out_path: Path,
    *,
    buffer_size: str,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # sort by: group_id (1), user_id (2), t (3 numeric)
    cmd = [
        "sort",
        "-S",
        str(buffer_size),
        "-T",
        str(out_path.parent),
        "-k1,1",
        "-k2,2",
        "-k3,3n",
        str(in_path),
    ]
    with out_path.open("w", encoding="utf-8") as f:
        subprocess.run(cmd, stdout=f, check=True)


@dataclass
class _Event:
    group_id: str
    user_id: str
    t: int
    pm_id: str
    y: int
    k_id: str


def _iter_events_sorted_text(path: Path) -> Iterator[_Event]:
    """
    读取按 (group_id, user_id, t) 排序后的 TSV：
      group_id \t user_id \t t \t pm_id \t y \t k_id
    """
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 6:
                continue
            gid, uid, t_s, pm, y_s, kid = parts
            try:
                t = int(t_s)
                y = int(y_s)
            except Exception:
                continue
            yield _Event(group_id=gid, user_id=uid, t=t, pm_id=pm, y=y, k_id=kid)


def _build_one_episode(
    *,
    group_id: str,
    user_id: str,
    rows: List[_Event],
    M: int,
    episode_idx: int,
) -> Optional[Dict[str, Any]]:
    if len(rows) < (int(M) + 1):
        return None
    rows_sorted = sorted(rows, key=lambda e: (int(e.t), str(e.pm_id)))
    exam = rows_sorted[-int(M) :]
    hist = rows_sorted[: -int(M)]
    return {
        "group_id": group_id,
        "episode_id": f"{group_id}|{user_id}#{episode_idx}",
        "user_id": user_id,
        "M": int(M),
        "history": [{"t": int(e.t), "pm_id": e.pm_id, "y": int(e.y), "k_id": e.k_id} for e in hist],
        "exam_set": [{"pm_id": e.pm_id, "k_id": e.k_id} for e in exam],
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build grouped episodes (history + last M as exam_set) from cleaned events, scalable via external sort."
    )
    ap.add_argument("--events-jsonl", required=True, help="Cleaned user-problem events JSONL (user_id,t,pm_id,y,attempts...).")
    ap.add_argument("--pm2k", required=True, help="Problem->Knowledge mapping JSON (pm_id -> k_id).")
    ap.add_argument("--out-jsonl", required=True, help="Output episodes JSONL (grouped by domain).")
    ap.add_argument("--M", type=int, default=3, help="Exam set size (last M interactions).")
    ap.add_argument("--tmp-dir", default="data/dkt/tmp_episodes_domain", help="Temp directory for buckets/sort.")
    ap.add_argument("--buckets", type=int, default=256, help="How many bucket files to shard into (controls memory).")
    ap.add_argument("--buffer-size", default="40%", help="sort -S buffer size (e.g. 40%%, 2G).")
    ap.add_argument("--progress-every", type=int, default=2_000_000, help="Print a progress line every N input events.")
    ap.add_argument("--max-episodes", type=int, default=0, help="If >0, stop after writing this many episodes (debug).")
    args = ap.parse_args(argv)

    events_path = Path(args.events_jsonl)
    pm2k = _read_json(args.pm2k)
    if not isinstance(pm2k, dict):
        raise SystemExit("pm2k 必须是 JSON object: {pm_id: k_id}")

    tmp_dir = Path(args.tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Stage A: bucketize to TSV to reduce memory pressure
    bucket_paths = [tmp_dir / f"bucket-{i:04d}.tsv" for i in range(int(args.buckets))]
    bucket_files = [p.open("w", encoding="utf-8") for p in bucket_paths]

    t0 = time.time()
    n_in = 0
    n_kept = 0
    n_missing_map = 0
    with events_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_in += 1
            try:
                obj = json.loads(line)
            except Exception:
                continue
            uid = str(obj.get("user_id") or "")
            pm = str(obj.get("pm_id") or "")
            if not uid or not pm:
                continue
            try:
                t = int(obj.get("t"))
            except Exception:
                continue
            y = int(obj.get("y") or 0)
            kid = pm2k.get(pm)
            if not kid:
                n_missing_map += 1
                continue
            kid = str(kid)
            gid = _group_from_kid(kid)
            # bucket key by (gid, uid)
            b = (hash(gid) ^ hash(uid)) % int(args.buckets)
            bucket_files[b].write(f"{gid}\t{uid}\t{t}\t{pm}\t{y}\t{kid}\n")
            n_kept += 1
            if int(args.progress_every) > 0 and n_in % int(args.progress_every) == 0:
                dt = max(time.time() - t0, 1e-9)
                sys.stderr.write(
                    f"\r[stageA] in={n_in:,} kept={n_kept:,} missing_pm2k={n_missing_map:,} speed={n_in/dt:,.0f} l/s"
                )
                sys.stderr.flush()
    for bf in bucket_files:
        bf.close()
    if n_in > 0:
        sys.stderr.write("\n")
        sys.stderr.flush()

    # Stage B: sort each bucket and build episodes streaming
    out_path = Path(args.out_jsonl)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    episode_idx_by_pair: Dict[Tuple[str, str], int] = {}

    with out_path.open("w", encoding="utf-8") as out_f:
        for bi, bucket in enumerate(bucket_paths):
            if not bucket.exists() or bucket.stat().st_size == 0:
                continue
            sorted_path = tmp_dir / f"bucket-{bi:04d}.sorted.tsv"
            _sort_file(bucket, sorted_path, buffer_size=str(args.buffer_size))

            cur_gid: Optional[str] = None
            cur_uid: Optional[str] = None
            buf: List[_Event] = []

            def _flush():
                nonlocal written, buf, cur_gid, cur_uid
                if cur_gid is None or cur_uid is None:
                    buf = []
                    return
                key = (cur_gid, cur_uid)
                idx = episode_idx_by_pair.get(key, 0)
                ep = _build_one_episode(group_id=cur_gid, user_id=cur_uid, rows=buf, M=int(args.M), episode_idx=idx)
                episode_idx_by_pair[key] = idx + 1
                buf = []
                if ep is None:
                    return
                _write_jsonl_line(out_f, ep)
                written += 1

            for ev in _iter_events_sorted_text(sorted_path):
                if cur_gid is None:
                    cur_gid, cur_uid = ev.group_id, ev.user_id
                if ev.group_id != cur_gid or ev.user_id != cur_uid:
                    _flush()
                    cur_gid, cur_uid = ev.group_id, ev.user_id
                buf.append(ev)
                if int(args.max_episodes) > 0 and written >= int(args.max_episodes):
                    break
            _flush()
            if int(args.max_episodes) > 0 and written >= int(args.max_episodes):
                break

    print(
        json.dumps(
            {
                "events_in": n_in,
                "events_kept": n_kept,
                "missing_pm2k": n_missing_map,
                "episodes_out": written,
                "out": str(out_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


