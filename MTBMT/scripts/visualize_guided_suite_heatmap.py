from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class OneDataset:
    openml_data_id: int
    openml_name: str
    csv: str
    target: str
    task: str = ""


def _read_manifest(path: str | Path) -> List[OneDataset]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"manifest 不存在：{p}")
    out: List[OneDataset] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            did = int(obj.get("openml_data_id") or obj.get("openml_id") or 0)
            if did <= 0:
                continue
            out.append(
                OneDataset(
                    openml_data_id=did,
                    openml_name=str(obj.get("openml_name") or obj.get("name") or ""),
                    csv=str(obj.get("csv") or ""),
                    target=str(obj.get("target") or "__target__"),
                    task=str(obj.get("task") or ""),
                )
            )
    # unique by id (keep first)
    uniq: Dict[int, OneDataset] = {}
    for d in out:
        uniq.setdefault(int(d.openml_data_id), d)
    return list(uniq.values())


def _rgb_for_value(v: float, max_abs: float) -> str:
    """
    Map value to background color:
    - v < 0 -> red-ish (worse)
    - v = 0 -> white
    - v > 0 -> blue-ish (better)
    """
    if not np.isfinite(v) or max_abs <= 0:
        return "rgb(245,245,245)"
    t = min(abs(float(v)) / float(max_abs), 1.0)
    # white -> red / blue
    if v >= 0:
        r = int(round(255 * (1.0 - t)))
        g = int(round(255 * (1.0 - t)))
        b = 255
    else:
        r = 255
        g = int(round(255 * (1.0 - t)))
        b = int(round(255 * (1.0 - t)))
    return f"rgb({r},{g},{b})"


def _write_html_heatmap(df: pd.DataFrame, *, title: str, out_html: Path) -> None:
    vals = df.to_numpy(dtype=float)
    finite = vals[np.isfinite(vals)]
    max_abs = float(np.max(np.abs(finite))) if finite.size else 1.0

    # simple HTML table with inline styles
    lines: List[str] = []
    lines.append("<!doctype html>")
    lines.append("<html><head><meta charset='utf-8'/>")
    lines.append(f"<title>{title}</title>")
    lines.append(
        "<style>body{font-family:Arial,Helvetica,sans-serif;} "
        "table{border-collapse:collapse;} "
        "th,td{border:1px solid #ddd;padding:6px 8px;text-align:right;white-space:nowrap;} "
        "th:first-child,td:first-child{text-align:left;} "
        "caption{caption-side:top;text-align:left;font-weight:bold;margin:8px 0;}"
        "</style>"
    )
    lines.append("</head><body>")
    lines.append("<table>")
    lines.append(f"<caption>{title}（蓝=好，红=差，值为相对 baseline 的提升 Δ）</caption>")

    # header
    cols = ["dataset"] + list(df.columns)
    lines.append("<tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>")

    for idx, row in df.iterrows():
        cells = [f"<td><b>{idx}</b></td>"]
        for c in df.columns:
            v = float(row[c])
            bg = _rgb_for_value(v, max_abs)
            txt = "nan" if not np.isfinite(v) else f"{v:+.4f}"
            cells.append(f"<td style='background:{bg}'>{txt}</td>")
        lines.append("<tr>" + "".join(cells) + "</tr>")

    lines.append("</table>")
    lines.append("</body></html>")

    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text("\n".join(lines), encoding="utf-8")


def _run_combo_eval(
    *,
    csv: str,
    target: str,
    experience: str,
    out_json: Path,
    extra_args: List[str],
    force: bool,
    timeout_sec: int,
) -> bool:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    if out_json.exists() and not force:
        return True

    cmd = [
        sys.executable,
        str(Path("scripts") / "guided_combo_eval.py"),
        "--csv",
        str(csv),
        "--target",
        str(target),
        "--top-k",
        "20",
        "--out-json",
        str(out_json),
    ]
    if experience:
        cmd += ["--experience", str(experience)]
    cmd += list(extra_args)

    try:
        # We inherit stdout/stderr so user can see progress from guided_combo_eval.
        subprocess.run(cmd, check=True, timeout=(None if int(timeout_sec) <= 0 else int(timeout_sec)))
        return True
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"[warn] dataset timeout, skipped: {out_json.name}\n")
        sys.stderr.flush()
        return False
    except subprocess.CalledProcessError:
        return False


def _load_metrics(path: Path) -> Dict[str, float]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[str, float] = {}

    # Δacc for Guided-CART (vs sklearn cart accuracy)
    try:
        out["guided_cart_delta_acc"] = float(obj["guided_cart"]["guided_cart"]["delta_acc"])
    except Exception:
        out["guided_cart_delta_acc"] = float("nan")

    # Δacc for Trajectory Guidance (vs baseline dt)
    try:
        out["traj_guidance_delta_acc"] = float(obj["trajectory_guidance"]["guided"]["delta_acc"])
    except Exception:
        out["traj_guidance_delta_acc"] = float("nan")

    # Δfinal@R for Guided-ASHA (vs ASHA)
    try:
        out["guided_asha_delta_final"] = float(obj["guided_asha"]["delta_final_mean"])
    except Exception:
        out["guided_asha_delta_final"] = float("nan")

    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run guided_combo_eval over ~20 datasets and save a blue(good)/red(bad) heatmap (HTML) for our algorithms."
    )
    ap.add_argument("--manifest", default="data/openml_100_rust/manifest.jsonl", help="JSONL manifest containing csv/target/openml_data_id.")
    ap.add_argument("--n", type=int, default=20, help="How many datasets to run (successful ones).")
    ap.add_argument("--experience", default="", help="Optional experience.jsonl for meta-selector recommendation.")
    ap.add_argument("--out-dir", default="results/guided_suite", help="Output directory for per-dataset JSON and final reports.")
    ap.add_argument("--force", action="store_true", help="Re-run even if cached JSON exists.")
    ap.add_argument("--timeout-sec", type=int, default=600, help="Per-dataset timeout for guided_combo_eval (seconds).")
    ap.add_argument(
        "--extra",
        default="--seed0 42 --cart-depth 6 --tg-iters 3 --tg-traj-samples 256 --hpo-train-seeds 2 --hpo-test-seeds 2 --n0 8 --min-resource 10 --max-resource 30 --eta 3 --hpo-alpha 0.25 --label-kind uplift",
        help="Extra args passed to guided_combo_eval (string). Tune for speed/quality.",
    )
    args = ap.parse_args(argv)

    manifest = _read_manifest(args.manifest)
    if not manifest:
        raise SystemExit("manifest 为空或无法解析。")

    # If manifest provides task field, prefer classification datasets to match guided_combo_eval constraints.
    has_task = any(bool(d.task) for d in manifest)
    if has_task:
        manifest = [d for d in manifest if (d.task or "").strip().lower() == "classification"]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_dir = out_dir / "json"

    extra_args = str(args.extra).strip().split() if str(args.extra).strip() else []

    target_n = max(1, int(args.n))
    rows: List[Tuple[str, Dict[str, float]]] = []
    t0 = perf_counter()

    tried = 0
    for ds in manifest:
        if len(rows) >= target_n:
            break
        tried += 1

        if not ds.csv:
            continue
        json_path = json_dir / f"openml-{ds.openml_data_id}.json"
        label = f"{ds.openml_data_id}:{(ds.openml_name or '').strip()}" if ds.openml_name else f"{ds.openml_data_id}"
        sys.stderr.write(f"\n[running] {label}\n")
        sys.stderr.flush()
        ok = _run_combo_eval(
            csv=ds.csv,
            target=ds.target or "__target__",
            experience=str(args.experience),
            out_json=json_path,
            extra_args=extra_args,
            force=bool(args.force),
            timeout_sec=int(args.timeout_sec),
        )
        if not ok or not json_path.exists():
            continue

        m = _load_metrics(json_path)
        label = f"{ds.openml_data_id}:{(ds.openml_name or '').strip()}" if ds.openml_name else f"{ds.openml_data_id}"
        rows.append((label, m))

        # lightweight progress line
        sys.stderr.write(f"\r[progress] ok={len(rows)}/{target_n} tried={tried}")
        sys.stderr.flush()

    sys.stderr.write("\n")
    sys.stderr.flush()

    if not rows:
        raise SystemExit("没有任何数据集成功跑完（可能是任务类型不兼容/CSV缺失/脚本报错）。")

    # Build dataframe
    df = pd.DataFrame(
        {k: v for (k, v) in rows},
    ).T
    # ensure stable column order
    cols = ["guided_cart_delta_acc", "traj_guidance_delta_acc", "guided_asha_delta_final"]
    for c in cols:
        if c not in df.columns:
            df[c] = np.nan
    df = df[cols]

    # Save CSV
    out_csv = out_dir / "guided_suite_deltas.csv"
    df.to_csv(out_csv, encoding="utf-8")

    # Save HTML heatmap
    out_html = out_dir / "guided_suite_heatmap.html"
    title = f"Guided Suite Heatmap (n={len(df)})"
    _write_html_heatmap(df, title=title, out_html=out_html)

    dt = perf_counter() - t0
    print("=== Saved ===")
    print("datasets_ok:", len(df), "time_sec:", f"{dt:.2f}")
    print("csv :", str(out_csv))
    print("html:", str(out_html))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


