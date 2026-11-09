"""训练/评测/服务同题规格守卫：所有 QUESTIONS 副本必须与生产服务逐字一致。

背景（2026-11-05，CE 类索引反转事故后补的缺口）：题面文本在 6 个文件里各存一份，
此前只靠注释约定「逐字一致」，改任何一侧都会让训练/评测/推理静默漂移（题规格变了，
所有历史指标口径跟着变，且没有任何报错）。本测试用 AST 取字面量比对，
不需要 import torch / FastAPI（gateway .venv 里可直接跑）。

新增题规格副本时：把文件路径加进 FILES 即可（变量名 QUESTIONS/Q2/Q 均识别）。
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Jev-Style 运行时键名 → 生产键名（其余文件已是生产键名，映射无操作）
KEY_MAP = {"t": "type", "ins": "instructions", "crit": "criteria"}
NAMES = ("QUESTIONS", "Q2", "Q")

FILES = {
    "生产服务(源头)": "scripts/laya_l2_server.py",
    "laya 微调": "scripts/laya_finetune_local.py",
    "选型评测共享口径": "scripts/l2_candidate_common.py",
    "版本对比评测": "scripts/eval_laya_version_compare.py",
    "laya 评测公共件": "scripts/laya_eval_common.py",
    "jevstyle 微调": "scripts/jevstyle_finetune_local.py",
}


def _extract(relpath):
    """取首个 QUESTIONS/Q2/Q 赋值，归一化为 {task: {type, instructions, criteria}}。

    支持两种字面量形态：
    - dict: {"is_confidential": {"type", "instructions", "criteria"}, ...}
    - 元组列表: [("is_confidential", {"t", "ins", "crit"}), ...]（jevstyle 运行时键）
    """
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in NAMES for t in node.targets):
            val = ast.literal_eval(node.value)
            norm = lambda q: {KEY_MAP.get(k, k): v for k, v in q.items()}
            if isinstance(val, list):
                return {key: norm(q) for key, q in val}
            return {key: norm(q) for key, q in val.items()}
    raise AssertionError(f"未找到题规格赋值（{', '.join(NAMES)}）: {relpath}")


def test_questions_verbatim_consistent():
    ref = _extract(FILES["生产服务(源头)"])
    assert set(ref) == {"is_confidential", "is_injection", "is_simple", "is_complex"}, "生产题规格任务集变化，需同步更新本测试"
    for name, path in FILES.items():
        spec = _extract(path)
        assert set(spec) <= set(ref), f"{name}（{path}）出现生产没有的任务: {set(spec) - set(ref)}"
        # 子集副本允许（历史单任务口径，见 laya_eval_common.Q 注释）；共任务必须逐字一致
        drifted = [k for k in spec if spec[k] != ref[k]]
        assert not drifted, (
            f"题规格漂移：{name}（{path}）与生产服务不一致。\n"
            f"差异任务: {drifted}\n"
            f"若是有意的规格变更：先改生产服务，再同步全部副本，并重跑历史评测对齐口径。")


def test_questions_nontrivial():
    # 防退化：若某文件题面被清成空串，上面的全相等仍会通过
    ref = _extract(FILES["生产服务(源头)"])
    for task, q in ref.items():
        assert len(q["instructions"]) > 100, f"生产题面 {task} 异常短，疑似被误改"
        assert q["criteria"]["true"] and q["criteria"]["false"]
