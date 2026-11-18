"""Fixed entrypoint for one bounded computation; never starts a web service."""
import json
import os
import resource
import sys

for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[key] = "1"
resource.setrlimit(resource.RLIMIT_CPU, (4, 5))
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))


def main():
    from solver import solve
    from schema import SimulationError, MAX_INPUT_BYTES
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise SimulationError("size_limit", "模型数据过大。")
        payload = json.loads(data)
        result = solve(payload["model"], payload["materials"], payload["sections"])
        result["summary"]["peak_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(json.dumps({"ok": True, "result": result}, allow_nan=False))
    except SimulationError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "error": str(exc)}))
    except (MemoryError, ImportError):
        print(json.dumps({"ok": False, "code": "resource_limit", "error": "计算资源不足，请缩小模型或检查科学计算依赖。"}))
    except Exception:
        print(json.dumps({"ok": False, "code": "solver_error", "error": "计算失败，请检查模型数据及单位。"}))


if __name__ == "__main__":
    main()
