import json
import os
import subprocess
import sys
import time

from clc.config import Settings
from clc.store import Store


def run_one(store: Store, settings: Settings) -> bool:
    job = store.claim(settings.lease_seconds)
    if job is None:
        return False
    env = {**os.environ, **{f"CLC_{k.upper()}": str(v) for k, v in settings.model_dump().items() if v is not None}}
    env["CLC_DATA_DIR"] = str(store.data_dir)
    process = None
    try:
        process = subprocess.Popen([sys.executable, "-m", "clc.run_job", job["id"]], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + settings.job_timeout
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("任务超时；请拆分资料或调整 CLC_JOB_TIMEOUT")
            if not store.heartbeat(job["id"], settings.lease_seconds):
                raise RuntimeError("任务已失去运行租约")
            time.sleep(min(2, settings.lease_seconds / 3))
        result_file = store.job_dir(job["id"]) / "result.json"
        if result_file.exists():
            result = json.loads(result_file.read_text(encoding="utf-8"))
            store.finish(job["id"], result.get("error") or ("处理进程异常退出" if process.returncode else None))
        else:
            store.finish(job["id"], "处理进程异常退出；检查解析依赖或内存")
    except (Exception, KeyboardInterrupt) as exc:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        store.finish(job["id"], str(exc) or "Worker 已停止")
        if isinstance(exc, KeyboardInterrupt):
            raise
    return True


def main():
    settings = Settings()
    store = Store(settings.data_dir)
    print("CLC worker ready", flush=True)
    try:
        while True:
            if not run_one(store, settings):
                time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
