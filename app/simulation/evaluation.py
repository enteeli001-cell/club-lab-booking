"""Free deterministic feedback; an optional provider contract for a future release."""
from typing import Protocol


class EvaluationProvider(Protocol):
    def evaluate(self, model: dict, result: dict) -> dict: ...


def evaluate(model, result, *, provider: EvaluationProvider | None = None, enabled=False):
    if enabled and provider is not None:
        return provider.evaluate(model, result)
    summary = result["summary"]
    advantages, shortcomings, suggestions = [], [], []
    def entry(text, **values): return {"text": text, "values": values}
    advantages.append(entry("结构在当前线性模型下可稳定求解。"))
    if not summary["overstressed"]:
        advantages.append(entry("全部杆件的组合应力比不超过 1。"))
    else:
        shortcomings.append(entry("有 {count} 根杆件超出容许应力，最大应力比为 {ratio}。", count=summary["overstressed"], ratio=f'{summary["max_stress_ratio"]:.3f}'))
        suggestions.append(entry("优先检查高亮杆件的截面、材料与荷载路径，调整后重新计算。"))
    limit = model["displacement_limit"]
    if summary["max_displacement"] <= limit:
        advantages.append(entry("最大位移未超过设定的评价限值。"))
    else:
        shortcomings.append(entry("最大位移 {value} mm，超过评价限值 {limit} mm。", value=f'{summary["max_displacement"]*1000:.3f}', limit=f'{limit*1000:.3f}'))
        suggestions.append(entry("增加有效支撑或提高受弯方向刚度，同时检查变形是否已超出小变形假设。"))
    low = summary["low_utilization"]
    if low:
        shortcomings.append(entry("有 {count} 根低应力比杆件，可能存在用料优化空间。", count=low))
        suggestions.append(entry("低应力不代表可以删除；保留稳定支撑，逐根减小截面并重新计算。"))
    if summary.get("buckling_candidates", 0):
        shortcomings.append(entry("有 {count} 根受压杆件需要进一步检查屈曲。", count=summary["buckling_candidates"]))
        suggestions.append(entry("欧拉屈曲仅为两端铰支的筛查估计，请另行核实有效长度与节点连接。"))
    suggestions.append(entry("总用料质量 {mass} kg，竖向荷重比 {ratio}；该比值对应当前输入荷载，不是极限承载力。", mass=f'{summary["weight_kg"]:.4f}', ratio=f'{summary["load_weight_ratio"]:.3f}'))
    suggestions.append(entry("本版不考虑连接破坏、材料各向异性、扭转应力及二阶效应，内部结果不能替代实物测试。"))
    return {"provider": "rules", "advantages": advantages, "shortcomings": shortcomings, "suggestions": suggestions}
