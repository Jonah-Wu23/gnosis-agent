# AppWorld + GEPA 环境搭建（WSL2）

AppWorld 官方只支持 Linux/macOS，全部安装放进 WSL2。宿主是 Windows，从 Git Bash 通过 `wsl -e bash -lc '...'` 调用。仓库路径在 WSL 内是 `/mnt/e/AI/GNOSIS Agent`（带空格，命令里要引号）。

搭建与验证完成日期：2026-10-04。

## 一、版本清单

Python：3.12.3（Ubuntu 24.04.3 LTS 自带）

四个直接依赖（`pip freeze` 实测）：

| 包 | 版本 |
| --- | --- |
| appworld | 0.1.3.post1 |
| gepa | 0.1.4 |
| openai | 3.24.0 |
| python-dotenv | 1.2.4 |

appworld 连带固定的关键间接依赖：SQLModel 0.0.10、pydantic 1.10.26、SQLAlchemy 1.4.54、fastapi 0.110.3、httpx 0.27.2。openai 3.24.0 与 pydantic 1.10.26 兼容（实测 `import openai` 正常）。

目录：

| 用途 | 路径 |
| --- | --- |
| venv | `/home/jonahwu/.venvs/gnosis` |
| 数据根目录（APPWORLD_ROOT） | `/home/jonahwu/appworld_root` |
| 数据集 | `/home/jonahwu/appworld_root/data`（194M，data version 0.1.0） |
| 解包后的测试与 app 源码 | `/home/jonahwu/.appworld`（8.4M） |
| 复现脚本与日志 | `E:\AI\GNOSIS Agent\scratch\`（gitignore） |

## 二、安装步骤与真实输出

### 1. 创建 venv

Ubuntu 24.04 默认没装 `python3.12-venv`，`python3 -m venv` 会因缺 ensurepip 失败；`sudo` 需要密码，无法 `apt install`。改用官方 `get-pip.py` 引导 pip：

```bash
wsl -e bash -lc '
mkdir -p ~/.venvs
python3 -m venv --without-pip ~/.venvs/gnosis
curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "/mnt/e/AI/GNOSIS Agent/scratch/get-pip.py"
~/.venvs/gnosis/bin/python "/mnt/e/AI/GNOSIS Agent/scratch/get-pip.py" --no-warn-script-location
'
```

真实输出摘录：

```
venv (without pip) created
Successfully installed pip-26.2.1
Python 3.12.3
pip 26.2.1 from /home/jonahwu/.venvs/gnosis/lib/python3.12/site-packages/pip
```

### 2. 安装四个包

```bash
wsl -e bash -lc '~/.venvs/gnosis/bin/pip install --no-input appworld gepa openai python-dotenv'
```

真实输出结尾：

```
Successfully installed ... appworld-0.1.3.post1 gepa-0.1.4 openai-3.24.0 python-dotenv-1.2.4
```

### 3. 解包 AppWorld 加密代码（官方 `appworld install`）

AppWorld 把 app 实现与测试放在加密的 `.bundle` 文件里，安装后必须解包。

```bash
wsl -e bash -lc 'mkdir -p ~/appworld_root && cd ~/appworld_root && ~/.venvs/gnosis/bin/appworld install'
```

真实输出：

```
Unpacked apps source code 
  - from: /home/jonahwu/.venvs/gnosis/lib/python3.12/site-packages/appworld/.source/apps.bundle
  - in  : /home/jonahwu/.venvs/gnosis/lib/python3.12/site-packages/appworld
Unpacked tests source code 
  - from: /home/jonahwu/.venvs/gnosis/lib/python3.12/site-packages/appworld/.source/tests.bundle
  - in  : /home/jonahwu/.appworld/tests
```

### 4. 下载数据（官方 `appworld download data`）

```bash
wsl -e bash -lc 'cd ~/appworld_root && APPWORLD_ROOT=/home/jonahwu/appworld_root ~/.venvs/gnosis/bin/appworld download data'
```

真实输出：

```
Downloading data. This will take around 15 seconds.
Data prepared at: ./data
```

下载后 `data/` 含 `api_docs`、`base_dbs`、`datasets`、`tasks`、`version.txt`。`load_task_ids("train")` 实测 90 个任务（阶段二复核；train.txt 行数口径以 API 为准）。

## 三、官方验证结果

### 单元测试（官方 `appworld verify tests`）

```bash
wsl -e bash -lc 'cd ~/appworld_root && ~/.venvs/gnosis/bin/appworld verify tests'
```

真实输出摘要：

```
1553 passed in 70.64s (0:01:10)
All tests passed.
...
collected 138 items
tests/package/test_common.py ...  [31%]
...
tests/package/test_appworld.py ...  [100%]
```

退出码 0。apps 测试 1553 项全过，其余模块（common、model_collection、sqlite_fts、load_task_ids、safety_guard、sqlmodel、appworld）138 项全过。

### 端到端任务验证（官方 `appworld verify tasks`）

这条命令对 train + dev 全部任务执行「ground-truth 解答 + 官方评分器」。它本身就是无 LLM 的评分链路验证。

```bash
# 注意：直接调 CLI 会丢总结行，见「坑与注意事项」第 2 条。用脚本调用 verify_tasks()：
wsl -e bash -lc 'cd ~/appworld_root && ~/.venvs/gnosis/bin/python "/mnt/e/AI/GNOSIS Agent/scratch/run_verify_tasks.py"'
```

真实输出：

```
Passed 147/147 tasks
VERIFY_TASKS_RETURN=True
```

147 = 90 个 train 任务 + 57 个 dev 任务，全部通过。

## 四、评分链路无 token 验证（已完整走通）

评分链路验证到了**官方评分器本身**，全程零模型 token。做法与官方 `src/appworld/verify.py` 中 `_verify_task` 完全一致：初始化 train 任务 → 执行 ground-truth 解答代码 → 调用官方评分器。

脚本 `scratch/verify_golden_eval.py` 的核心逻辑：

```python
from appworld import AppWorld, load_task_ids

task_id = load_task_ids("train")[0]          # 82e2fac_1
with AppWorld(task_id=task_id, experiment_name="golden_eval_check",
              ground_truth_mode="full", raise_on_failure=False) as world:
    code = world.task.ground_truth.compiled_solution_code + "\nsolution(apis, requester)"
    world.execute(code)
    tracker = world.evaluate()               # 官方评分器
```

真实输出：

```
task_id = 82e2fac_1
has ground_truth = True
has compiled_solution_code = True
--- solution execute output (tail) ---
Execution successful.
--- evaluation ---
tracker.success = True
tracker.to_dict() = {'success': True, 'difficulty': 1, 'num_tests': 2,
                     'passes': [{'requirement': 'assert answers match.', 'label': 'no_op_fail'},
                                {'requirement': 'assert no model changes.', 'label': 'no_op_pass'}],
                     'failures': []}
```

ground-truth 解答在 `82e2fac_1` 上通过官方评分器（`success=True`，2 项测试全过，0 项失败）。`appworld verify tasks` 在此基础上把同一链路跑满 train + dev 共 147 个任务，全部通过。结论：官方评分链路可用，且不消耗任何模型 token。

## 五、GEPA adapter 接口核对

```bash
wsl -e bash -lc '~/.venvs/gnosis/bin/python -c "
import gepa
from gepa.core.adapter import GEPAAdapter, EvaluationBatch
print(gepa.optimize)
print(GEPAAdapter.evaluate)
print(GEPAAdapter.make_reflective_dataset)
"'
```

真实输出摘要：

```
GEPAAdapter = <class 'gepa.core.adapter.GEPAAdapter'>
EvaluationBatch = <class 'gepa.core.adapter.EvaluationBatch'>
GEPAAdapter protocol methods present: ['evaluate', 'make_reflective_dataset']
evaluate signature: {'batch': list[~DataInst], 'candidate': dict[str, str],
                     'capture_traces': <class 'bool'>,
                     'return': gepa.core.adapter.EvaluationBatch[~Trajectory, ~RolloutOutput]}
make_reflective_dataset signature: {'candidate': dict[str, str],
                     'eval_batch': gepa.core.adapter.EvaluationBatch[~Trajectory, ~RolloutOutput],
                     'components_to_update': list[str],
                     'return': Mapping[str, Sequence[Mapping[str, Any]]]}
gepa.optimize = <function optimize at 0x...>
```

对照官方文档 https://gepa-ai.github.io/gepa/guides/adapters/ 的 `GEPAAdapter` 协议：`evaluate` 与 `make_reflective_dataset` 两个扩展方法都在，签名一致；`gepa.optimize` 可用。给 GNOSIS 写 AppWorld adapter 时，`evaluate` 里接 `AppWorld` 跑任务、用 `world.evaluate()` 的结果作为分数，`make_reflective_dataset` 里按同一接口返回 `{"组件名": [{"Inputs":..., "Generated Outputs":..., "Feedback":...}]}`。

注意：GEPA 文档示例用 `litellm`，但 `gepa` 基础包不依赖它，当前环境也没装。`gepa.optimize` 的 `reflection_lm` 走 litellm 接口时需另行安装 `litellm`，或改传自带的模型客户端。

## 六、复现步骤

从 Git Bash 执行下面几段即可重建。

```bash
# 1. venv（若已存在可跳过）
wsl -e bash -lc 'mkdir -p ~/.venvs && python3 -m venv --without-pip ~/.venvs/gnosis'
# 若缺 get-pip.py，重新下载：
wsl -e bash -lc 'curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "/mnt/e/AI/GNOSIS Agent/scratch/get-pip.py"'
wsl -e bash -lc '~/.venvs/gnosis/bin/python "/mnt/e/AI/GNOSIS Agent/scratch/get-pip.py" --no-warn-script-location'

# 2. 安装依赖（引擎要求的精确版本）
wsl -e bash -lc '~/.venvs/gnosis/bin/pip install -r "/mnt/e/AI/GNOSIS Agent/engine/requirements.txt"'

# 3. 解包 + 下载数据
wsl -e bash -lc 'mkdir -p ~/appworld_root && cd ~/appworld_root && ~/.venvs/gnosis/bin/appworld install'
wsl -e bash -lc 'cd ~/appworld_root && APPWORLD_ROOT=/home/jonahwu/appworld_root ~/.venvs/gnosis/bin/appworld download data'

# 4. 官方验证
wsl -e bash -lc 'cd ~/appworld_root && ~/.venvs/gnosis/bin/appworld verify tests'
wsl -e bash -lc 'cd ~/appworld_root && ~/.venvs/gnosis/bin/python "/mnt/e/AI/GNOSIS Agent/scratch/run_verify_tasks.py"'
```

WSL 内激活 venv：

```bash
source ~/.venvs/gnosis/bin/activate     # 之后可直接用 appworld / pip
# 或不经激活直接调用：
~/.venvs/gnosis/bin/appworld --help
~/.venvs/gnosis/bin/python your_script.py
```

任何用到 `appworld` 模块的脚本，都要在 `import appworld` 之前把 `APPWORLD_ROOT` 指向数据根目录，否则默认用当前目录，会报找不到 data：

```python
import os
os.environ["APPWORLD_ROOT"] = "/home/jonahwu/appworld_root"
from appworld import AppWorld
```

或用 `from appworld import update_root; update_root("/home/jonahwu/appworld_root")`。

## 七、坑与注意事项

1. **`python3 -m venv` 因为缺 ensurepip 失败。** Ubuntu 24.04 未预装 `python3.12-venv`，`sudo` 又需要密码。解决：`python3 -m venv --without-pip` 建 venv，再用官方 `get-pip.py` 把 pip 装进 venv。若以后能拿到 sudo 密码，`sudo apt install -y python3.12-venv` 是更直接的路径。

2. **`appworld verify tasks` 的 CLI 退出码恒为 0，且总结行会丢失。** 官方 `src/appworld/cli.py` 结尾调用 `os._exit(0)`，跳过 Python 文件缓冲刷新，rich 打印的 `Passed N/M tasks` 常常到不了终端；`os._exit(0)` 也让它永远返回 0。判断是否真的全部通过，不能看退出码。用 `scratch/run_verify_tasks.py` 直接调用 `verify_tasks()`，正常退出会刷新缓冲，能拿到 `Passed 147/147 tasks` 与布尔返回值。

3. **`APPWORLD_ROOT` 必须显式设置。** AppWorld 默认把当前目录当根，数据在 `./data`、实验输出在 `./experiments/outputs`。CLI 用 `--root` 或环境变量 `APPWORLD_ROOT`，模块用环境变量或 `update_root()`。本次统一用 `/home/jonahwu/appworld_root`。

4. **`git lfs` 只在从源码安装时需要。** 本次走 PyPI 安装（`pip install appworld`），不涉及 Git LFS。只有 `pip install git+https://...` 或 `pip install -e .` 才要先 `git lfs install`。

5. **pydantic 固定在 1.10.26。** appworld 0.1.3.post1 依赖 SQLModel 0.0.10，后者要求 pydantic 1.x。装 GEPA 或其它包时不要顺手升级 pydantic 到 2.x，会破坏 appworld。

6. **从 Git Bash 调 WSL 时路径要引号。** 仓库路径带空格，写 `/mnt/e/AI/GNOSIS Agent/...` 时必须整段加引号，否则参数被拆开。

7. **WSL 启动时的 localhost 代理告警是噪声。** 每条 `wsl -e` 命令开头会出现 `wsl: 检测到 localhost 代理配置，但未镜像到 WSL` 之类的提示，不影响命令执行与联网。

8. **appworld 要求 Python 3.11+。** 本环境 3.12.3 满足。低于 3.11 会装不上或运行报错。

9. **测试集与训练集的 ground truth 范围不同。** train、dev 有完整的 `ground_truth`（含 `compiled_solution_code`、`answer`、`evaluation_code`），test_normal、test_challenge 只有难度指标。用 `ground_truth_mode="full"` 加载 train/dev 才能拿到解答代码做无 token 验证。
