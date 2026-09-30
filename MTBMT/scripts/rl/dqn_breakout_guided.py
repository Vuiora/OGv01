from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import List, Optional, Sequence

import numpy as np


def _try_import_gym():
    try:
        import gymnasium as gym  # type: ignore

        return gym
    except Exception as e:
        raise SystemExit(
            "未安装 gymnasium。\n"
            "请先安装：py -3 -m pip install gymnasium\n"
            f"原始错误：{e}"
        )


def _try_import_torch():
    try:
        import torch  # type: ignore
        import torch.nn as nn  # type: ignore

        return torch, nn
    except Exception as e:
        raise SystemExit(
            "未安装 torch。\n"
            "请先安装：py -3 -m pip install torch\n"
            "（建议 Python 3.11/3.12 + 对应官方 wheel）\n"
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


def linear_schedule(t: int, T: int, a0: float, a1: float) -> float:
    if T <= 1:
        return float(a1)
    x = float(t) / float(max(T - 1, 1))
    return float(a0 + (a1 - a0) * x)


def parse_guide_actions(s: str) -> List[str]:
    s = (s or "").strip()
    if not s:
        return []
    parts = []
    for p in s.split(","):
        p = p.strip().lower()
        if p:
            parts.append(p)
    return parts


def map_action_names_to_indices(action_meanings: Sequence[str], guide_names: List[str]) -> List[int]:
    # ALE meanings are typically: ["NOOP","FIRE","RIGHT","LEFT",...]
    idx_by_name = {name.strip().lower(): i for i, name in enumerate(action_meanings)}
    # aliases
    alias = {
        "noop": "noop",
        "no-op": "noop",
        "fire": "fire",
        "left": "left",
        "right": "right",
        "up": "up",
        "down": "down",
    }
    out: List[int] = []
    for g in guide_names:
        key = alias.get(g, g)
        if key not in idx_by_name:
            raise SystemExit(
                f"guide 动作 '{g}' 不在 env action_meanings 里：{list(action_meanings)}\n"
                "请使用其中的动作名字（不区分大小写）。"
            )
        out.append(int(idx_by_name[key]))
    out = sorted(set(out))
    return out


@dataclass
class ReplayBuffer:
    s: np.ndarray
    a: np.ndarray
    r: np.ndarray
    s2: np.ndarray
    d: np.ndarray
    n: int
    i: int

    @classmethod
    def create(cls, *, capacity: int, obs_dim: int) -> "ReplayBuffer":
        cap = int(capacity)
        return cls(
            s=np.zeros((cap, obs_dim), dtype=np.float32),
            a=np.zeros((cap,), dtype=np.int64),
            r=np.zeros((cap,), dtype=np.float32),
            s2=np.zeros((cap, obs_dim), dtype=np.float32),
            d=np.zeros((cap,), dtype=np.float32),
            n=0,
            i=0,
        )

    def add(self, s: np.ndarray, a: int, r: float, s2: np.ndarray, done: bool) -> None:
        self.s[self.i] = s
        self.a[self.i] = int(a)
        self.r[self.i] = float(r)
        self.s2[self.i] = s2
        self.d[self.i] = 1.0 if done else 0.0
        self.i = (self.i + 1) % self.s.shape[0]
        self.n = min(self.s.shape[0], self.n + 1)

    def sample(self, rng: np.random.Generator, batch_size: int):
        idx = rng.integers(0, self.n, size=int(batch_size))
        return self.s[idx], self.a[idx], self.r[idx], self.s2[idx], self.d[idx]


def preprocess_obs(obs: np.ndarray, *, obs_type: str) -> np.ndarray:
    if obs_type == "ram":
        # (128,) uint8
        x = np.asarray(obs, dtype=np.float32).reshape(-1)
        return x / 255.0
    elif obs_type == "rgb":
        # Minimal: flatten (H,W,C) -> float32
        x = np.asarray(obs, dtype=np.float32).reshape(-1)
        return x / 255.0
    else:
        raise ValueError(f"unknown obs_type={obs_type}")


def evaluate_greedy(
    *,
    env,
    qnet,
    torch,
    obs_type: str,
    episodes: int,
    seed: int,
    render: bool,
) -> float:
    returns: List[float] = []
    for i in range(int(episodes)):
        obs, _info = env.reset(seed=int(seed) + i)
        done = False
        ep_ret = 0.0
        while not done:
            x = preprocess_obs(obs, obs_type=obs_type)
            with torch.no_grad():
                q = qnet(torch.tensor(x[None, :], dtype=torch.float32))
                a = int(torch.argmax(q, dim=1).item())
            obs, r, terminated, truncated, _info = env.step(a)
            done = bool(terminated) or bool(truncated)
            ep_ret += float(r)
            if render:
                env.render()
        returns.append(ep_ret)
    return float(np.mean(returns)) if returns else 0.0


def main(argv: Optional[List[str]] = None) -> int:
    _ensure_utf8_console()
    ap = argparse.ArgumentParser(description="DQN baseline vs DQN+guided on ALE/Breakout (RAM by default).")
    ap.add_argument("--env-id", default="ALE/Breakout-v5")
    ap.add_argument("--obs-type", choices=["ram", "rgb"], default="ram")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--total-steps", type=int, default=200_000)
    ap.add_argument("--buffer-size", type=int, default=100_000)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--gamma", type=float, default=0.99)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--learning-starts", type=int, default=10_000)
    ap.add_argument("--train-every", type=int, default=4)
    ap.add_argument("--target-update", type=int, default=5_000)

    ap.add_argument("--epsilon0", type=float, default=1.0)
    ap.add_argument("--epsilon-final", type=float, default=0.05)
    ap.add_argument("--epsilon-decay-steps", type=int, default=100_000)

    ap.add_argument("--guided", action="store_true")
    ap.add_argument("--guide-actions", default="fire,left,right", help='Comma list, e.g. "fire,left,right,noop"')
    ap.add_argument("--alpha-guided0", type=float, default=0.7)
    ap.add_argument("--alpha-guided-final", type=float, default=0.0)

    ap.add_argument("--eval-every", type=int, default=20_000)
    ap.add_argument("--eval-episodes", type=int, default=10)
    ap.add_argument("--eval-seed", type=int, default=123)
    ap.add_argument("--eval-render", action="store_true")

    ap.add_argument("--out-csv", default="results/rl_guided/dqn_breakout.csv")
    ap.add_argument("--save-model", default="", help="Optional: save weights to .pt")
    args = ap.parse_args(argv)

    gym = _try_import_gym()
    torch, nn = _try_import_torch()
    rng = np.random.default_rng(int(args.seed))
    torch.manual_seed(int(args.seed))

    # Ensure Atari envs are registered (some installs rely on import side-effects)
    try:
        import ale_py  # type: ignore  # noqa: F401
        import gymnasium.envs.atari  # type: ignore  # noqa: F401
    except Exception:
        pass

    # NOTE: Atari + ROMs on Windows often needs:
    #   py -3 -m pip install "gymnasium[atari]" ale-py "autorom[accept-rom-license]"
    #   py -3 -m AutoROM --accept-license
    try:
        env = gym.make(
            str(args.env_id),
            obs_type=str(args.obs_type),
            frameskip=1,
            repeat_action_probability=0.0,
            render_mode="human" if bool(args.eval_render) else None,
        )
    except Exception as e:
        raise SystemExit(
            "无法创建 Atari/ALE 环境（例如 ALE/Breakout-v5）。最常见原因是未安装 Atari 依赖或 ROM。\n"
            "\n"
            "请按下面顺序检查：\n"
            "1) 使用 Python 3.11/3.12（Windows + Python 3.13/3.14 往往缺 wheels，安装会被跳过或需要源码编译）。\n"
            '2) 安装：pip install "gymnasium[atari]" ale-py "autorom[accept-rom-license]" torch\n'
            "3) 安装 ROM：AutoROM --accept-license\n"
            "   （注意是命令行可执行文件 AutoROM，不是 python -m AutoROM）\n"
            "\n"
            f"原始错误：{type(e).__name__}: {e}"
        )

    action_meanings = list(getattr(env.unwrapped, "get_action_meanings", lambda: [])())
    if not action_meanings:
        # fallback for newer gymnasium ALE wrappers
        try:
            action_meanings = list(env.unwrapped.ale.getMinimalActionSet())  # type: ignore[attr-defined]
        except Exception:
            action_meanings = []

    guide_idx: List[int] = []
    if bool(args.guided):
        if not hasattr(env.unwrapped, "get_action_meanings"):
            raise SystemExit("当前环境不支持 get_action_meanings()，无法用动作名字映射 guide-actions。")
        guide_names = parse_guide_actions(str(args.guide_actions))
        guide_idx = map_action_names_to_indices(env.unwrapped.get_action_meanings(), guide_names)  # type: ignore[attr-defined]
        if not guide_idx:
            raise SystemExit("guided 已启用，但 guide-actions 为空。")

    # infer obs dim
    obs, _info = env.reset(seed=int(args.seed))
    x0 = preprocess_obs(obs, obs_type=str(args.obs_type))
    obs_dim = int(x0.size)
    nA = int(env.action_space.n)

    class QNet(nn.Module):
        def __init__(self, obs_dim: int, nA: int):
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(obs_dim, 256),
                nn.ReLU(),
                nn.Linear(256, 256),
                nn.ReLU(),
                nn.Linear(256, nA),
            )

        def forward(self, x):
            return self.net(x)

    qnet = QNet(obs_dim, nA)
    qtarget = QNet(obs_dim, nA)
    qtarget.load_state_dict(qnet.state_dict())
    qtarget.eval()
    opt = torch.optim.Adam(qnet.parameters(), lr=float(args.lr))

    rb = ReplayBuffer.create(capacity=int(args.buffer_size), obs_dim=obs_dim)

    out_csv = Path(str(args.out_csv))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    t0 = perf_counter()
    last_eval_mean_return = float("nan")

    def epsilon(step: int) -> float:
        T = int(args.epsilon_decay_steps)
        if step >= T:
            return float(args.epsilon_final)
        return linear_schedule(step, T, float(args.epsilon0), float(args.epsilon_final))

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["step", "epsilon", "alpha_guided", "loss", "eval_mean_return"])

        obs, _info = env.reset(seed=int(args.seed))
        for step in range(1, int(args.total_steps) + 1):
            eps = epsilon(step - 1)
            a_guided = (
                linear_schedule(step - 1, int(args.total_steps), float(args.alpha_guided0), float(args.alpha_guided_final))
                if bool(args.guided)
                else 0.0
            )

            # action selection: epsilon-greedy, but exploration is guided-biased
            if float(rng.random()) < float(eps):
                if guide_idx and float(a_guided) > 0.0 and float(rng.random()) < float(a_guided):
                    a = int(rng.choice(np.asarray(guide_idx, dtype=int)))
                else:
                    a = int(rng.integers(0, nA))
            else:
                x = preprocess_obs(obs, obs_type=str(args.obs_type))
                with torch.no_grad():
                    q = qnet(torch.tensor(x[None, :], dtype=torch.float32))
                    a = int(torch.argmax(q, dim=1).item())

            obs2, r, terminated, truncated, _info = env.step(int(a))
            done = bool(terminated) or bool(truncated)

            rb.add(
                preprocess_obs(obs, obs_type=str(args.obs_type)),
                int(a),
                float(r),
                preprocess_obs(obs2, obs_type=str(args.obs_type)),
                done,
            )

            obs = obs2
            if done:
                obs, _info = env.reset(seed=int(args.seed) + step)

            loss = float("nan")
            if step >= int(args.learning_starts) and step % max(1, int(args.train_every)) == 0 and rb.n >= int(args.batch_size):
                bs = int(args.batch_size)
                s, a_b, r_b, s2, d_b = rb.sample(rng, bs)
                s_t = torch.tensor(s, dtype=torch.float32)
                a_t = torch.tensor(a_b, dtype=torch.int64)
                r_t = torch.tensor(r_b, dtype=torch.float32)
                s2_t = torch.tensor(s2, dtype=torch.float32)
                d_t = torch.tensor(d_b, dtype=torch.float32)

                q = qnet(s_t).gather(1, a_t.view(-1, 1)).squeeze(1)
                with torch.no_grad():
                    q2 = qtarget(s2_t).max(dim=1).values
                    y = r_t + (1.0 - d_t) * float(args.gamma) * q2
                td = q - y
                loss_t = (td * td).mean()

                opt.zero_grad()
                loss_t.backward()
                opt.step()
                loss = float(loss_t.item())

            if int(args.target_update) > 0 and step % int(args.target_update) == 0:
                qtarget.load_state_dict(qnet.state_dict())

            do_eval = (step == 1) or (step % max(1, int(args.eval_every)) == 0) or (step == int(args.total_steps))
            if do_eval:
                last_eval_mean_return = evaluate_greedy(
                    env=env,
                    qnet=qnet,
                    torch=torch,
                    obs_type=str(args.obs_type),
                    episodes=int(args.eval_episodes),
                    seed=int(args.eval_seed),
                    render=bool(args.eval_render),
                )

            w.writerow([step, eps, a_guided, loss, last_eval_mean_return])

            if do_eval:
                elapsed = perf_counter() - t0
                sys.stderr.write(
                    f"\r[progress] step={step}/{args.total_steps} eps={eps:.3f} a_guided={a_guided:.3f} "
                    f"loss={loss if np.isfinite(loss) else -1:.4f} eval_mean_return={last_eval_mean_return:.3f} "
                    f"elapsed={elapsed:.1f}s"
                )
                sys.stderr.flush()

        sys.stderr.write("\n")
        sys.stderr.flush()

    if str(args.save_model).strip():
        p = Path(str(args.save_model))
        p.parent.mkdir(parents=True, exist_ok=True)
        torch.save({"state_dict": qnet.state_dict(), "obs_dim": obs_dim, "nA": nA, "env_id": str(args.env_id), "obs_type": str(args.obs_type)}, p)
        print("saved_model:", str(p.resolve()))

    print("saved_csv:", str(out_csv.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


