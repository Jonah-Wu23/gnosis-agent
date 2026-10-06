import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PINS = {"StarHarness": "a56d8f6ef7d15475aae4cc302b67d0113b960153",
        "ace": "82709de050e1db6e6ef2f07bcb0393560b94992a",
        "AutomationBench": "4a8e1061254004d9dac807054eed33fad7d1ff14"}


def verify_upstreams():
    for name, expected in PINS.items():
        path = ROOT / "scratch" / name
        actual = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
        if actual != expected:
            raise ValueError("固定上游版本发生变化：" + name)
    return dict(PINS)


def starharness():
    verify_upstreams()
    root = ROOT / "scratch/StarHarness"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from benchmarks.base import BenchmarkAdapter
    import evolving_harness
    return BenchmarkAdapter, evolving_harness.decide, evolving_harness.promotion_review


def official_ace():
    verify_upstreams()
    root = ROOT / "scratch/ace"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from ace.core import Reflector, Curator
    from playbook_utils import update_bullet_counts, get_playbook_stats, get_next_global_id
    return Reflector, Curator, update_bullet_counts, get_playbook_stats, get_next_global_id
