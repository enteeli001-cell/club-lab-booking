"""One compute child per application database; bounded wall time and payload."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys

from .schema import SimulationError, MAX_INPUT_BYTES

WORKER_PATH = Path(__file__).with_name("worker.py")


def run(payload, lock_dir, *, timeout=8):
    data = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    if len(data.encode()) > MAX_INPUT_BYTES:
        raise SimulationError("size_limit", "模型数据过大。")
    directory = Path(lock_dir); directory.mkdir(parents=True, exist_ok=True)
    with (directory / "simulation-compute.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SimulationError("busy", "已有模拟正在计算，请稍后再试。") from None
        try:
            completed = subprocess.run([sys.executable, str(WORKER_PATH)], input=data, text=True,
                                       capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise SimulationError("timeout", "计算超时已终止，请缩小模型后重试。") from None
        if completed.returncode != 0 or len(completed.stdout) > 4_000_000:
            raise SimulationError("resource_limit", "计算进程已终止，请缩小模型或检查运行环境。")
        try:
            message = json.loads(completed.stdout)
        except (ValueError, TypeError):
            raise SimulationError("solver_error", "计算失败，请检查模型数据及单位。") from None
        if not message.get("ok"):
            raise SimulationError(message.get("code", "solver_error"), message.get("error", "计算失败，请检查模型数据及单位。"))
        return message["result"]
