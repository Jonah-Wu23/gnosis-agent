import json
import random
from pathlib import Path

from engine.research_upstream import verify_upstreams
from engine.scoped_units import POLICY, fingerprint, save_json

ROOT = Path(__file__).resolve().parent.parent
PLAN_PATH = ROOT / "config/research_selection.json"
ARMS = ("fixed", "ace", "global", "gnosis", "repeat")
# 学习与验证合计 $22 的组间预分：两个自主修复组等额，先跑的组吃不到后跑组的份额。
ARM_LEARNING_LIMITS = {"fixed": 2.0, "ace": 3.0, "global": 8.5, "gnosis": 8.5}


def build_plan():
    from automationbench.domains import get_domain_dataset
    from automationbench.runner import compute_allowed_services, strip_none_values

    pilot = json.loads((ROOT / "config/pilot_selection.json").read_text(encoding="utf-8"))
    upstreams = verify_upstreams()
    plan = {"schema": "gnosis.research.v1", "seed": POLICY["seed"], "upstreams": upstreams,
            "search": [], "selection": [], "heldout": [], "checkpoint": [], "tasks": {},
            "selection_repeats": 2, "checkpoint_learning_fraction": 0.5,
            "arms": list(ARMS), "arm_learning_limits_usd": ARM_LEARNING_LIMITS}
    for domain, pilot_tasks in pilot["domains"].items():
        rows = list(get_domain_dataset(domain))
        by_name = {json.loads(row["info"])["task_name"]: row for row in rows}
        if len(by_name) != len(rows) or not set(pilot_tasks) <= set(by_name):
            raise ValueError("任务身份重复或先导任务缺失")
        remaining = sorted(set(by_name) - set(pilot_tasks))
        random.Random(POLICY["seed"]).shuffle(remaining)
        if len(remaining) < 6:
            raise ValueError("领域没有足够的独立选择和最终留出任务")
        plan["search"].extend(pilot_tasks)
        plan["selection"].extend(remaining[:1])
        plan["heldout"].extend(remaining[1:6])
        plan["checkpoint"].extend(pilot_tasks[:1])
        for name in [*pilot_tasks, *remaining[:6]]:
            row = by_name[name]
            info = strip_none_values(json.loads(row["info"]))
            plan["tasks"][name] = {"domain": domain, "contract": fingerprint(row),
                                    "services": compute_allowed_services(info["initial_state"], info["assertions"], info["zapier_tools"])}
    plan["identity"] = fingerprint(plan)
    return plan


def prepare_plan():
    plan = build_plan()
    if PLAN_PATH.exists() and json.loads(PLAN_PATH.read_text(encoding="utf-8")) != plan:
        raise ValueError("预定的研究任务、配对规则或上游版本发生变化")
    save_json(PLAN_PATH, plan)
    return plan
