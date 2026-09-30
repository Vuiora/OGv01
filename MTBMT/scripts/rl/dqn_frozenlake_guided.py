from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Deque, Dict, List, Optional, Tuple

from collections import deque

import numpy as np


def _try_import_gym():
    try:
        import gymnasium as gym  # type: ignore

        return gym
    except Exception as e:
        raise SystemExit(
            "未安装 gymnasium，无法运行该 RL 示例。\n"
            "请先安装：\n"
            "  py -3 -m pip install gymnasium\n"
            f"原始错误：{e}"
        )


def _ensure_utf8_console() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


@dataclass(frozen=True)
class GuidePolicy:
    best_action: Dict[int, int]  # state -> action

    def act(self, s: int, *, rng: np.random.Generator, n_actions: int) -> int:
        a = self.best_action.get(int(s), None)
        if a is None:
            return int(rng.integers(0, int(n_actions)))
        return int(a)


def _neighbors(desc: np.ndarray, r: int, c: int) -> List[Tuple[int, int, int]]:
    # 0: LEFT, 1: DOWN, 2: RIGHT, 3: UP
    H, W = desc.shape
    out: List[Tuple[int, int, int]] = []
    if c - 1 >= 0:
        out.append((r, c - 1, 0))
    if r + 1 < H:
        out.append((r + 1, c, 1))
    if c + 1 < W:
        out.append((r, c + 1, 2))
    if r - 1 >= 0:
        out.append((r - 1, c, 3))
    return out


def build_shortest_path_guide(env) -> GuidePolicy:
    """
    A simple "expert" for FrozenLake: greedy action on the shortest path to the goal,
    computed from the known map (env.unwrapped.desc).
    """
    desc = np.asarray(env.unwrapped.desc)  # bytes array shape (H,W)
    H, W = desc.shape

    goal: Optional[Tuple[int, int]] = None
    for r in range(H):
        for c in range(W):
            if desc[r, c] == b"G":
                goal = (r, c)
                break
        if goal is not None:
            break
    if goal is None:
        raise SystemExit("无法在 FrozenLake 地图中找到目标格 'G'。")

    def passable(rr: int, cc: int) -> bool:
        return desc[rr, cc] != b"H"

    q: Deque[Tuple[int, int]] = deque([goal])
    dist: Dict[Tuple[int, int], int] = {goal: 0}
    while q:
        r, c = q.popleft()
        for rr, cc, _a in _neighbors(desc, r, c):
            if not passable(rr, cc):
                continue
            if (rr, cc) in dist:
                continue
            dist[(rr, cc)] = dist[(r, c)] + 1
            q.append((rr, cc))

    best_action: Dict[int, int] = {}
    for r in range(H):
        for c in range(W):
            if not passable(r, c):
                continue
            if (r, c) not in dist or (r, c) == goal:
                continue
            best: Optional[Tuple[int, int]] = None  # (next_dist, action)
            for rr, cc, a in _neighbors(desc, r, c):
                if (rr, cc) not in dist:
                    continue
                cand = (dist[(rr, cc)], a)
                if best is None or cand < best:
                    best = cand
            if best is None:
                continue
            s = int(r * W + c)
            best_action[s] = int(best[1])
    return GuidePolicy(best_action=best_action)


def _one_hot(s: np.ndarray, nS: int) -> np.ndarray:
    s = np.asarray(s, dtype=int).reshape(-1)
    X = np.zeros((s.size, int(nS)), dtype=float)
    X[np.arange(s.size), s] = 1.0
    return X


@dataclass
class MLP:
    W1: np.ndarray
    b1: np.ndarray
    W2: np.ndarray
    b2: np.ndarray

    def forward(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        z1 = X @ self.W1 + self.b1
        h1 = np.maximum(z1, 0.0)
        q = h1 @ self.W2 + self.b2
        return q, h1


def _init_mlp(rng: np.random.Generator, nS: int, nA: int, hidden: int) -> MLP:
    # small init
    W1 = rng.normal(scale=0.1, size=(nS, hidden))
    b1 = np.zeros((hidden,), dtype=float)
    W2 = rng.normal(scale=0.1, size=(hidden, nA))
    b2 = np.zeros((nA,), dtype=float)
    return MLP(W1=W1.astype(float), b1=b1, W2=W2.astype(float), b2=b2)


def _copy_mlp(src: MLP) -> MLP:
    return MLP(W1=src.W1.copy(), b1=src.b1.copy(), W2=src.W2.copy(), b2=src.b2.copy())


def _train_step(
    net: MLP,
    target_net: MLP,
    *,
    batch_s: np.ndarray,
    batch_a: np.ndarray,
    batch_r: np.ndarray,
    batch_s2: np.ndarray,
    batch_done: np.ndarray,
    nS: int,
    gamma: float,
    lr: float,
) -> float:
    X = _one_hot(batch_s, nS)
    X2 = _one_hot(batch_s2, nS)

    q, h = net.forward(X)
    q2, _h2 = target_net.forward(X2)

    a = batch_a.astype(int)
    y = batch_r + (1.0 - batch_done) * float(gamma) * np.max(q2, axis=1)

    pred = q[np.arange(q.shape[0]), a]
    err = pred - y
    loss = float(np.mean(err * err))

    # Backprop (MSE)
    # dL/dpred = 2/N * err
    gpred = (2.0 / max(1, q.shape[0])) * err  # (B,)
    gq = np.zeros_like(q)
    gq[np.arange(q.shape[0]), a] = gpred

    # q = h @ W2 + b2
    gW2 = h.T @ gq
    gb2 = np.sum(gq, axis=0)
    gh = gq @ net.W2.T

    # ReLU
    gz1 = gh * (h > 0.0)
    gW1 = X.T @ gz1
    gb1 = np.sum(gz1, axis=0)

    # SGD update
    net.W1 -= float(lr) * gW1
    net.b1 -= float(lr) * gb1
    net.W2 -= float(lr) * gW2
    net.b2 -= float(lr) * gb2

    return loss


def evaluate_greedy(*, env, net: MLP, nS: int, episodes: int, seed: int) -> float:
    wins = 0
    for i in range(int(episodes)):
        s, _info = env.reset(seed=int(seed) + i)
        done = False
        ep_ret = 0.0
        while not done:
            q, _ = net.forward(_one_hot(np.asarray([s]), nS))
            a = int(np.argmax(q[0]))
            s, r, terminated, truncated, _info = env.step(a)
            done = bool(terminated) or bool(truncated)
            ep_ret += float(r)
        if ep_ret > 0:
            wins += 1
    return float(wins) / float(max(1, int(episodes)))


def linear_schedule(t: int, T: int, a0: float, a1: float) -> float:
    if T <= 1:
        return float(a1)
    x = float(t) / float(max(T - 1, 1))
    return float(a0 + (a1 - a0) * x)


def main(argv: Optional[List[str]] = None) -> int:
    _ensure_utf8_console()
    ap = argparse.ArgumentParser(description="DQN baseline vs DQN+guided on FrozenLake-v1 (numpy implementation).")
    ap.add_argument("--map-name", choices=["4x4", "8x8"], default="4x4")
    ap.add_argument("--random-map", action="store_true")
    ap.add_argument("--size", type=int, default=0)
    ap.add_argument("--is-slippery", action="store_true")

    ap.add_argument("--steps", type=int, default=50000, help="Training steps (environment steps).")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--hidden", type=int, default=64)
    ap.add_argument("--lr", type=float, default=0.01)
    ap.add_argument("--gamma", type=float, default=0.99)

    ap.add_argument("--epsilon0", type=float, default=1.0)
    ap.add_argument("--epsilon-final", type=float, default=0.05)
    ap.add_argument("--warmup", type=int, default=1000, help="Replay warmup steps before learning.")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--replay-size", type=int, default=20000)
    ap.add_argument("--target-update", type=int, default=1000, help="Target network update period (steps).")

    ap.add_argument("--guided", action="store_true", help="Enable guided behavior policy (shortest-path expert).")
    ap.add_argument("--alpha-guided0", type=float, default=0.7)
    ap.add_argument("--alpha-guided-final", type=float, default=0.0)

    ap.add_argument("--eval-every", type=int, default=2000)
    ap.add_argument("--eval-episodes", type=int, default=200)
    ap.add_argument("--out-csv", default="results/rl_guided/frozenlake_dqn.csv")
    ap.add_argument("--save-model", default="", help="Optional: save model weights to .npz")
    args = ap.parse_args(argv)

    gym = _try_import_gym()
    rng = np.random.default_rng(int(args.seed))

    rm = None
    make_kwargs: Dict[str, object] = {"is_slippery": bool(args.is_slippery), "render_mode": rm}
    if bool(args.random_map):
        n = int(args.size)
        if n <= 0:
            raise SystemExit("使用 --random-map 时必须提供 --size（例如 --size 12）。")
        try:
            from gymnasium.envs.toy_text.frozen_lake import generate_random_map  # type: ignore
        except Exception as e:
            raise SystemExit(f"无法导入 generate_random_map：{e}")
        make_kwargs["desc"] = generate_random_map(size=n, seed=int(args.seed))
    else:
        make_kwargs["map_name"] = str(args.map_name)

    env = gym.make("FrozenLake-v1", **make_kwargs)
    nS = int(env.observation_space.n)
    nA = int(env.action_space.n)

    guide = build_shortest_path_guide(env) if bool(args.guided) else None

    net = _init_mlp(rng, nS=nS, nA=nA, hidden=int(args.hidden))
    target_net = _copy_mlp(net)

    # Replay buffer: (s,a,r,s2,done)
    rs = int(args.replay_size)
    buf_s = np.zeros((rs,), dtype=int)
    buf_a = np.zeros((rs,), dtype=int)
    buf_r = np.zeros((rs,), dtype=float)
    buf_s2 = np.zeros((rs,), dtype=int)
    buf_d = np.zeros((rs,), dtype=float)
    buf_n = 0
    buf_i = 0

    out_csv = Path(str(args.out_csv))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    t0 = perf_counter()

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["step", "epsilon", "alpha_guided", "loss", "eval_win_rate"])

        s, _info = env.reset(seed=int(args.seed))
        for t in range(1, int(args.steps) + 1):
            eps = linear_schedule(t - 1, int(args.steps), float(args.epsilon0), float(args.epsilon_final))
            a_guided = linear_schedule(t - 1, int(args.steps), float(args.alpha_guided0), float(args.alpha_guided_final)) if guide is not None else 0.0

            # behavior policy: guided (with prob a_guided) else epsilon-greedy
            if guide is not None and float(a_guided) > 0.0 and float(rng.random()) < float(a_guided):
                a = guide.act(int(s), rng=rng, n_actions=nA)
            else:
                if float(rng.random()) < float(eps):
                    a = int(rng.integers(0, nA))
                else:
                    q, _ = net.forward(_one_hot(np.asarray([s]), nS))
                    a = int(np.argmax(q[0]))

            s2, r, terminated, truncated, _info = env.step(int(a))
            done = bool(terminated) or bool(truncated)

            # store
            buf_s[buf_i] = int(s)
            buf_a[buf_i] = int(a)
            buf_r[buf_i] = float(r)
            buf_s2[buf_i] = int(s2)
            buf_d[buf_i] = 1.0 if done else 0.0
            buf_i = (buf_i + 1) % rs
            buf_n = min(rs, buf_n + 1)

            s = s2
            if done:
                s, _info = env.reset(seed=int(args.seed) + t)

            loss = float("nan")
            if t >= int(args.warmup) and buf_n >= int(args.batch_size):
                idx = rng.integers(0, buf_n, size=int(args.batch_size))
                loss = _train_step(
                    net,
                    target_net,
                    batch_s=buf_s[idx],
                    batch_a=buf_a[idx],
                    batch_r=buf_r[idx],
                    batch_s2=buf_s2[idx],
                    batch_done=buf_d[idx],
                    nS=nS,
                    gamma=float(args.gamma),
                    lr=float(args.lr),
                )

            if int(args.target_update) > 0 and (t % int(args.target_update) == 0):
                target_net = _copy_mlp(net)

            do_eval = (t == 1) or (t % max(1, int(args.eval_every)) == 0) or (t == int(args.steps))
            win_rate = evaluate_greedy(env=env, net=net, nS=nS, episodes=int(args.eval_episodes), seed=int(args.seed) + 10_000) if do_eval else float("nan")
            w.writerow([t, eps, a_guided, loss, win_rate])

            if do_eval:
                elapsed = perf_counter() - t0
                sys.stderr.write(
                    f"\r[progress] step={t}/{args.steps} eps={eps:.3f} a_guided={a_guided:.3f} "
                    f"loss={loss if np.isfinite(loss) else -1:.4f} win_rate={win_rate if np.isfinite(win_rate) else -1:.3f} "
                    f"elapsed={elapsed:.1f}s"
                )
                sys.stderr.flush()

        sys.stderr.write("\n")
        sys.stderr.flush()

    if str(args.save_model).strip():
        p = Path(str(args.save_model))
        p.parent.mkdir(parents=True, exist_ok=True)
        np.savez(p, W1=net.W1, b1=net.b1, W2=net.W2, b2=net.b2, nS=nS, nA=nA, map_name=str(args.map_name), random_map=bool(args.random_map), size=int(args.size))
        print("saved_model:", str(p.resolve()))

    print("saved_csv:", str(out_csv.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


