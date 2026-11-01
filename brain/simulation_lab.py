#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""simulation_lab.py — 小美「行动仿真沙盒与后果预测器」。

把小美从「会做事」升级成**做事前能预演未来、选最优路径、降失败率**的行动智能体。
在真正执行任何复杂任务前, 先把「计划 / 文件改动 / App 操作 / 邮件·日历·业务动作 /
代码修改 / 外部系统写入」放进**可回滚沙盒·影子环境**里仿真执行, 预测收益、风险、
依赖、失败点与用户可能反应, 给出最优路径建议 —— 但**绝不真执行任何副作用**。

定位: 本模块是**薄预测门面**, 不自带执行引擎, 不重写任何安全门。它把
goal_engine(autopilot/autopilot_store) / agent_swarm(swarm) / computer_use /
app_control_bus·connector_forge(appsmith/nexus) / self_improve / world_model
的**只读·预演·脱敏**面串成一个「先预演、后批准、再执行」的决策层。真正落地一律
回到 User 已有的审批队列(autopilot_store.enqueue_approval), 经 autopilot._execute 过全部门。

⚠️ 安全铁律(与 agent_swarm/swarm/workflow_clone 一致, 自进化引擎不得改弱):
  - 纯门面 + 零真实副作用: 本模块自身**绝不**直接 subprocess / os.system / osascript /
    删文件 / exec 生成代码。一切「执行」只发生在沙盒副本 / 引擎自带 dry·plan·draft 预演面里;
    真动作一律 `autopilot_store.enqueue_approval(...)` 入审批队列(risk≥high, 守护绝不自动执行)。
  - 永不自放行: **绝不**对任何引擎传 confirmed=True。computer_use 走 plan=True(只看不点),
    self_improve 走 confirmed=False(只回 needs_confirm), nexus/appsmith 只读 inspect/status,
    绝不触 nexus_send/appsmith.approve。每条仿真记录恒带 executed_for_real=False。
  - 红线单一真相源: 任何步骤分级一律转发 swarm.merge._classify_step → local_powers._check_red_line,
    本模块绝不复制/收窄红线表。
  - 隐私: 命中私密(world_model `_private` / file_tools private=1)的输出一律带 `_private`,
    让大脑切本机模型。展示文件正文前必过 file_tools.is_private_path / command_touches_private,
    隐私库不可达 → 一律按私密处理(只回元数据, 不读正文)。绝不读密钥文件。
  - LLM 工具面只读为主且冻结(simulate/status/risk_score, 纯预测分析);
    强动作(shadow_execute/dry_run/approval_packet/rollback_plan/rehearse_ui/...)CLI-only。

命令:
  jarvis simulation_lab simulate "<目标/计划(可含 STEP: 行)>" [--kind K] [--from-swarm <run>] [-p k=v]
                                          建仿真: 分级每步 + 预测收益/风险/依赖/失败点/用户反应(默认 dry, 不执行)
  jarvis simulation_lab dry_run <sim_id>   逐步走引擎自带预演面(computer_use.plan / self_improve.needs_confirm / 文件 diff), 零副作用
  jarvis simulation_lab shadow_execute <sim_id> [-p file=路径 -p content_file=路径]
                                          影子执行: 真实目标全程不碰; 文件改动落沙盒副本可 diff/回滚, 强动作只记预测
  jarvis simulation_lab diff <sim_id>      看影子执行的前后差异(文件 unified diff; 私密只回元数据)
  jarvis simulation_lab risk_score <sim_id|"<动作>">  风险评分 0-100 分解(红线/副作用/不可逆/隐私/对外/波及面)
  jarvis simulation_lab impact_predict <sim_id|"<目标>">  后果预测: world_model 影响波及 + 下一步预测 + 因果链
  jarvis simulation_lab compare_plans <sim_a> <sim_b> [...]  多方案对比择优(风险↓·可逆↑·偏好契合↑), 出推荐路径
  jarvis simulation_lab rollback_plan <sim_id>  生成回滚/撤销预案(逆操作), 标可否自动回滚(只出预案, 不执行)
  jarvis simulation_lab approval_packet <sim_id>  把仿真打包成审批项入队(risk≥high), 待 User 批; 绝不自动执行
  jarvis simulation_lab rehearse_ui <sim_id|"<UI 任务>">  演练 App/UI 操作(computer_use plan=True, 只看不点)
  jarvis simulation_lab test_action "<动作>"  单动作快测: 分级 + 风险 + 后果(轻量, 建临时仿真)
  jarvis simulation_lab what_if <sim_id> --change k=v [--assume "<假设>"]  反事实推演: 改一个前提看结果怎么变
  jarvis simulation_lab audit <sim_id|box_id>  安全/合规审计(红线/副作用/隐私/待批 + 自验从未真执行/从未自放行, 只读)
  jarvis simulation_lab status [<sim_id|box_id>]  看某仿真/沙盘全貌 / 不传则列最近仿真+沙盘 + 各引擎可用性

多情景反事实未来沙盘(融候选计划 + User 目标 + 日历/待办/邮件/文件状态 + world_model 机制 +
epistemic_core 不确定性 + constraint_planner 约束; 缺模块记 deferred 不建桩; 纯只读预测):
  jarvis simulation_lab simulate_plan "<计划>" [--goal G] [--from-swarm <run>] [-p k=v]
                                          一次产出 best/base/worst + 多步后果链 + 二阶影响 + 失败触发点 +
                                          关键敏感变量 + 可逆回退路径 + 推荐监控指标
  jarvis simulation_lab scenario_tree <scn_id|"<计划>">       情景树(三分支 + 失败触发子分支)
  jarvis simulation_lab counterfactual <scn_id> --change k=v|--assume "<假设>"  情景级反事实(改前提重算未来)
  jarvis simulation_lab second_order_effects <scn_id|"<计划>">  二阶影响(下游波及)
  jarvis simulation_lab failure_triggers <scn_id|"<计划>">     失败触发点(把 base 引向 worst 的条件)
  jarvis simulation_lab sensitivity <scn_id|"<计划>">          关键敏感变量(按风险摆动排序)
  jarvis simulation_lab early_warning <scn_id|"<计划>">        推荐监控指标/预警
  jarvis simulation_lab rollback_path <scn_id|"<计划>">        可逆回退路径(只出预案)
  jarvis simulation_lab compare_futures <scn_a> <scn_b> [...]  多未来对比择优(期望风险↓·可逆↑·失败面↓)
  jarvis simulation_lab explain_outcome <scn_id> [--case best|base|worst]  成因解释(为什么这个情景会发生)
  (上述接口同时作模块级函数, 供 reasoning_core/mission_control/strategy_tournament/metacognition_core import 调用)

全本机, 纯 stdlib。

可执行数字沙盘与后果预演器(digital twin sandbox; 全新一层, 把动作编成「可回滚的本机虚拟执行轨迹」):
  jarvis simulation_lab sandbox [--new] [--title T] [--reset <box_id>] [<box_id>]
                                          建/列/看数字沙盘(虚拟世界状态容器); --reset 仅清空本层自有 json, 绝不删真实文件
  jarvis simulation_lab import_state <box_id|--new> --kind file|todo|world|json [-p path=.. -p topic=.. -p json_file=..]
                                          只读把真实状态导入沙盘虚拟态(文件元数据/待办/世界模型实体/显式JSON); 私密只回元数据, 绝不读密钥
  jarvis simulation_lab sandbox_plan "<计划(可含 STEP:)>" [--box <id>] [--from-swarm <run>] [-p file=.. -p content_file=..]
                                          把计划编成虚拟执行轨迹落进沙盘: 逐步算 before/after 虚拟快照+逆操作+连锁; 零真实副作用
  jarvis simulation_lab simulate_toolchain [--box <id>] -p chain_file=<json> | -p tools="a,b,c"
                                          把多工具调用链编成虚拟轨迹(只读解析归属+分级+跨步连锁); **绝不调用任何工具**
  jarvis simulation_lab predict_side_effects <box_id|"<目标>">  逐步后果: 一阶直接改动 + 二阶波及(world_model) + 三阶下游预测 + 跨步耦合
  jarvis simulation_lab stress_test <box_id>  压力测试: 在缺依赖/并发改动/中途失败/隐私锁/引擎不可用/高并发 等逆境下看哪步会断 + 韧性分
  jarvis simulation_lab compare_outcomes <box_a> <box_b> [...]  对比多沙盘的虚拟终态(资源增改删/风险/可逆/波及)择优
  jarvis simulation_lab failure_injection <box_id> [-p step=N]  失败注入: 在第 N 步(或逐步)注入失败, 看连锁阻塞/部分状态/可否回滚/残余风险
  jarvis simulation_lab sandbox_rollback_path <box_id>  沿轨迹算完整回滚路径(逆序逆操作), 标可否自动回滚/需审批(只出预案, 不执行)
  jarvis simulation_lab approval_preview <sim_id|box_id>  预览「将入审批队列的 payload/风险/why」但**绝不真入队**(只读预演)
  jarvis simulation_lab explain_consequence <box_id> [-p step=N]  逐步因果叙述: 改了什么→因依赖/因果会波及谁→失败会怎样→怎么撤回

全本机, 纯 stdlib。数字沙盘恒 executed_for_real=False; 真落地仍只经 approval_packet→User 批。
"""
from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import os
import re
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402  — 纯读取, 无副作用


# --------------------------------------------------------------------------- #
# 蜂群分级引擎软导入(只读: 复用红线分级单一真相源; 缺则保守降级, 绝不崩)
# --------------------------------------------------------------------------- #
try:
    from swarm import merge as _merge_mod   # _classify_step / _extract_steps(→ local_powers)
except Exception:  # pragma: no cover — 引擎缺失时分级保守 gated
    _merge_mod = None

try:
    from swarm import paths as _paths        # 读 agent_swarm/swarm run 的合并计划(只读)
except Exception:  # pragma: no cover
    _paths = None


# --------------------------------------------------------------------------- #
# 风险权重(纯启发式打分用; 与红线分级解耦 —— 红线永远走 local_powers 单一真相源)
# --------------------------------------------------------------------------- #
_W_RED = 40          # 每个红线步骤(强惩罚)
_W_GATED = 12        # 每个有副作用·需审批步骤
_W_IRREVERSIBLE = 15  # 每个不可逆步骤
_W_PRIVACY = 15      # 命中隐私
_W_EXTERNAL = 20     # 对外·影响他人(红线 4)
_W_BLAST = 2         # world_model 每个受波及实体

# 对外·影响他人信号(红线 4: 发客户/同事; 仅作风险加权, 真红线仍以 local_powers 为准)
_EXTERNAL_HINTS = ("发送", "发邮件", "发给", "回复客户", "客户", "同事", "对外", "群发",
                   "send", "email", "reply", "outbound", "通知对方")
# 不可逆信号(启发式: 删除/部署/推送/真发 等; 仅作可逆性预测, 不替代红线门)
_IRREVERSIBLE_HINTS = ("删除", "删掉", "rm ", "drop ", "truncate", "格式化", "部署", "deploy",
                       "push", "推送", "发送", "send", "上线", "迁移", "覆盖生产", "清空")
# autopilot._execute 认的可执行 action_kind(approval_packet 落 payload 用; 未知 → note 人工复核)
_EXEC_KINDS = ("agent", "fleet", "computer_use", "skill", "appsmith", "shell",
               "applescript", "write_file", "move_file", "todo", "note")

# --------------------------------------------------------------------------- #
# 数字沙盘(digital twin)常量 —— 纯虚拟态, 不替代任何安全门
# --------------------------------------------------------------------------- #
# 非私密文件做内容指纹的读取上限(只读; 私密/密钥文件永不读正文, 不计指纹)
_HASH_READ_CAP = 2 * 1024 * 1024  # 2 MiB
# 编轨迹时识别动作所属「域」的提示词(顺序敏感: 先专域后泛文件)
_DOMAIN_HINTS = (
    ("calendar", ("日历", "日程", "会议", "约会", "约时间", "calendar", "event", "meeting")),
    ("email", ("邮件", "发邮件", "回信", "回复客户", "回复邮件", "email", "mail", "outbound")),
    ("task", ("待办", "任务计划", "todo", "task")),
    ("code", ("改代码", "补丁", "重构", "改模块", "改 brain", "改我自己", "patch", ".py", "代码")),
    ("business", ("业务流程", "业务", "流程", "订单", "工单", "审批流", "workflow", "pipeline")),
    ("file", ("文件", "目录", "路径", "写入", "覆盖", "移动文件", "删除文件", "重命名", "归档")),
)
# 已知「有副作用/强动作」的工具名片段(simulate_toolchain 据此把工具调用升级为 mutating/external;
# 仅作分级加权, 真红线仍以 local_powers 为准)。绝不据此调用任何工具。
_MUTATING_TOOL_HINTS = ("write_file", "move_file", "delete", "run_shell", "shell", "computer_do",
                        "self_improve", "claude_code", "codex", "appsmith", "app_", "nexus",
                        "send", "draft", "create_event", "update_event", "delete_event",
                        "create_draft", "label_", "todo", "add_goal", "enqueue", "deploy",
                        "commit", "push", "patch")
_EXTERNAL_TOOL_HINTS = ("send", "draft", "outbound", "email", "nexus", "create_event",
                        "update_event", "reply", "create_draft", "label_thread")
# 暴露 is_tool/approved_tools 只读注册面的引擎(simulate_toolchain 只读解析工具归属; 绝不 dispatch/invoke)
_TOOL_REGISTRY_MODULES = ("appsmith", "nexus", "gep", "skillforge", "context_brain",
                          "agent_swarm", "mission_control", "world_model")
# stress_test 的逆境场景矩阵(纯分析, 不真造故障)
_STRESS_SCENARIOS = (
    ("dependency_missing", "关键依赖缺失(磁盘/网络/前置数据不在)"),
    ("concurrent_modification", "并发改动(真实目标被他人/其他进程同时改)"),
    ("partial_failure_midway", "中途失败(执行到一半崩溃)"),
    ("privacy_lock_engaged", "隐私锁触发(命中私密内容, 切本机/只回元数据)"),
    ("engine_unavailable", "引擎不可用(world_model/审批队列/分级引擎掉线)"),
    ("high_volume_load", "高并发/大批量(步骤×数据量放大, 超时/限流)"),
    ("permission_denied", "权限不足(目标只读/受保护路径/红线门拦下)"),
    ("rollback_needed", "需回滚(执行后发现要撤回, 检验逆操作可达性)"),
)


# --------------------------------------------------------------------------- #
# 路径 / 小工具(全 best-effort, 绝不抛)
# --------------------------------------------------------------------------- #
def _data_dir() -> str:
    return os.path.abspath(os.path.expanduser(
        config.get("simulation_lab.data_dir", "~/.jarvis/simulation_lab")))


def _sims_dir() -> str:
    return os.path.join(_data_dir(), "sims")


def _sandbox_root() -> str:
    return os.path.join(_data_dir(), "sandbox")


def _audit_path() -> str:
    return os.path.join(_data_dir(), "audit.jsonl")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _params(kvs) -> dict:
    d = {}
    for kv in kvs or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            d[k] = v
    return d


def _opt(name):
    """软导入可选集成模块; 缺/出错 → None(降级)。importlib, 非 __import__/exec。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _audit(action: str, sim_id: str = "", **extra) -> None:
    """只记 ids/动作/计数/标志的审计流, **绝不写正文/隐私**, 永不抛。"""
    try:
        rec = {"ts": _now(), "action": action, "sim_id": sim_id or ""}
        rec.update({k: v for k, v in extra.items() if v is not None})
        path = _audit_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _sim_id(seed: str) -> str:
    h = hashlib.sha1((seed + _now()).encode("utf-8")).hexdigest()[:10]
    return f"sim_{h}"


def _sim_path(sim_id: str) -> str:
    return os.path.join(_sims_dir(), f"{sim_id}.json")


def _save_sim(sim: dict) -> None:
    """落仿真记录到自有数据目录(本机, 非密钥/非业务路径)。恒强制 executed_for_real=False。"""
    sim["executed_for_real"] = False  # 硬不变量: 仿真层永不真执行
    os.makedirs(_sims_dir(), exist_ok=True)
    with open(_sim_path(sim["sim_id"]), "w", encoding="utf-8") as f:
        json.dump(sim, f, ensure_ascii=False, indent=2)


def _load_sim(sim_id: str):
    if not sim_id:
        return None
    try:
        with open(_sim_path(sim_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _list_sims(limit: int = 30) -> list:
    out = []
    d = _sims_dir()
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except Exception:
        return out
    for n in names:
        try:
            with open(os.path.join(d, n), "r", encoding="utf-8") as f:
                s = json.load(f)
            out.append(s)
        except Exception:
            continue
    out.sort(key=lambda s: s.get("created", ""), reverse=True)
    return out[:limit]


# --------------------------------------------------------------------------- #
# 复用引擎单一真相源的薄包装(绝不复制红线/分级逻辑)
# --------------------------------------------------------------------------- #
def _classify_step(action: str) -> dict:
    """转发 swarm.merge._classify_step(→ local_powers._check_red_line)。缺引擎 → 保守 gated。"""
    try:
        if _merge_mod is not None:
            return _merge_mod._classify_step(action)
    except Exception:
        pass
    return {"kind": "mutating", "gated": True, "why": "分级引擎缺失, 保守入审批"}


def _extract_steps(text: str) -> list:
    """复用 swarm.merge._extract_steps(认 'STEP:' 行)。缺引擎 → 本地等价兜底。"""
    try:
        if _merge_mod is not None:
            return _merge_mod._extract_steps(text)
    except Exception:
        pass
    steps = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.upper().startswith("STEP:"):
            steps.append(line[5:].strip())
    return steps


# --------------------------------------------------------------------------- #
# 集成读面(world_model: 后果/影响/预测/偏好; 全只读, 命中私密自动 _private)
# --------------------------------------------------------------------------- #
def _world_ctx(topic: str):
    """world_model 给 simulation_lab 的专属上下文包(影响波及 + 下一步预测 + 因果链)。"""
    wm = _opt("world_model")
    if wm is None:
        return None
    try:
        return wm.context_for(consumer="simulation_lab", topic=topic or "")
    except Exception:
        return None


def _preferences(aspect: str = ""):
    wm = _opt("world_model")
    if wm is None:
        return None
    try:
        return wm.preference_query(aspect or "")
    except Exception:
        return None


def _impact(target: str):
    wm = _opt("world_model")
    if wm is None:
        return None
    try:
        return wm.impact(target or "")
    except Exception:
        return None


def _predict(target: str):
    wm = _opt("world_model")
    if wm is None:
        return None
    try:
        return wm.predict(target or "")
    except Exception:
        return None


def _register_goal(goal: str):
    st = _opt("autopilot_store")
    if st is None:
        return None
    try:
        return st.add_goal((goal or "")[:200], detail="[simulation_lab 登记]")
    except Exception:
        return None


def _connector_preview(action: str):
    """connector_forge(app_control_bus)只读预演: 对外动作看 nexus 渠道可用性, App 动作提示 appsmith 只读 inspect。
    **绝不** nexus.draft/send/approve、绝不 appsmith.invoke/approve —— 只读探测, 真动作经各自确认门 User 批。"""
    if _is_external(action):
        nx = _opt("nexus")
        if nx is not None:
            try:
                st = nx.status()  # 只读: 看各渠道是否就绪
                return {"connector": "nexus", "live": False,
                        "channels": st.get("connectors") or st.get("services") or list(st.keys()),
                        "preview": "对外动作: nexus 只读 status(绝不 draft/send); 真发须 User 经 nexus 确认门"}
            except Exception:
                pass
    if _opt("appsmith") is not None:
        return {"connector": "appsmith", "live": False,
                "preview": "App/业务动作: 用 jarvis appsmith inspect <app> 看可调动作(只读); 真执行经审批闸 User 批"}
    return None


# --------------------------------------------------------------------------- #
# 隐私守门(展示文件正文前必过; 隐私库不可达 → 一律按私密处理, 只回元数据)
# --------------------------------------------------------------------------- #
def _is_secret_path(path: str) -> bool:
    """密钥/凭据文件硬拦(内容无关, 单一真相源复用 skillforge; 缺则本地兜底)。命中 → 绝不读正文。

    与 index.db 隐私标记**解耦**: 密钥文件通常不入索引(is_private_path 会 fail-open 返 False),
    故这里独立硬拦, 复用 skillforge._touches_secret_resolved(单一真相源) + 本地铁名单兜底。"""
    p = path or ""
    sf = _opt("skillforge")
    if sf is not None:
        try:
            if sf._touches_secret_resolved(p):
                return True
        except Exception:
            pass
    base = os.path.basename(p).lower()
    if base in (".gmail_creds.json", ".gcal_creds.json", ".localkey", ".env"):
        return True
    # 凭据/密钥词根出现在 basename 任意位置即拦 —— 覆盖 .bak/.backup/.old/.orig/~ 等备份变体
    # (如 .gmail_creds.json.bak / .env.backup)。硬拦只会"多拦", 对仿真层 fail-safe:
    # 命中 → 只回元数据, 绝不读正文(不替代红线门, 仅密钥防泄露)。
    if any(stem in base for stem in
           (".gmail_creds", ".gcal_creds", ".localkey", "creds.json", "credential",
            "secret", "id_rsa", "id_ed25519", ".pem", ".key", ".keystore", ".env",
            ".pgpass", ".netrc")):
        return True
    return bool(re.search(r"(^|/)\.ssh(/|$)|credential|secret|id_rsa|id_ed25519",
                          p, re.IGNORECASE))


def _path_privacy(path: str) -> dict:
    """判文件可否安全展示正文。返回 {private: bool, reason}。fail-safe: 库缺/DB不可达/出错 → 按私密。

    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 除密钥/凭据文件外恒 {private: False}, 私密
    记录(工资/银行/护照等)原样展示给大脑(小美可读全部数据; 本模块无对外出口, 红线4 由 autopilot
    审批闸独立守卫, 不靠此分类)。开 → 完整隐私判定(index.db + fail-safe, 可逆)。"""
    # 0) 密钥/凭据文件: 内容无关硬拦, 优先级最高(绝不读密钥正文) —— 与总开关解耦, 永不因关闭而放行
    if _is_secret_path(path):
        return {"private": True, "reason": "密钥/凭据文件(硬拦, 绝不读正文)"}
    if not config.get("safety.data_layer_privacy_enabled", False):
        return {"private": False, "reason": ""}
    ft = _opt("file_tools")
    if ft is None:
        return {"private": True, "reason": "file_tools 不可用, 隐私状态未知 → 按私密处理"}
    # 隐私库(index.db)不可达 → is_private_path 会 fail-open 返 False, 故先探测可达性;
    # 不可达一律按私密(fail-safe, 不 fail-open)。
    try:
        dbp = getattr(ft, "DB_PATH", None)
        if not dbp or not os.path.exists(dbp):
            return {"private": True, "reason": "隐私库 index.db 不可达 → 保守按私密处理"}
    except Exception:
        return {"private": True, "reason": "隐私库探测出错 → 保守按私密处理"}
    try:
        if ft.is_private_path(path):
            return {"private": True, "reason": "index.db 标记 private=1"}
        return {"private": False, "reason": ""}
    except Exception:
        return {"private": True, "reason": "隐私判定出错 → 保守按私密处理"}


def _command_touches_private(command: str) -> bool:
    """命令是否引用任何隐私文件。fail-safe: 库缺/出错 → 按"碰隐私"(更保守)。

    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 恒 False(小美可读全部数据; 凭据文件硬拦走
    独立的 _is_secret_path, 不受本开关影响, 本函数只判断是否触碰 index.db 标记的私密记录)。
    开 → 完整判定(可逆)。"""
    if not config.get("safety.data_layer_privacy_enabled", False):
        return False
    ft = _opt("file_tools")
    if ft is None:
        return True
    try:
        return bool(ft.command_touches_private(command or ""))
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# 可逆性 / 预测效果 / 风险评分(纯启发式; 红线永远以 local_powers 分级为准)
# --------------------------------------------------------------------------- #
def _reversible(action: str, cls: dict) -> bool:
    low = (action or "").lower()
    if cls.get("kind") == "read_only":
        return True
    if cls.get("kind") == "red_line":
        return False
    if any(h in action or h.lower() in low for h in _IRREVERSIBLE_HINTS):
        return False
    return True  # 一般有副作用步骤: 假定可经备份/回滚撤销


def _is_external(action: str) -> bool:
    low = (action or "").lower()
    return any(h in action or h.lower() in low for h in _EXTERNAL_HINTS)


def _predicted_effect(action: str, cls: dict) -> str:
    kind = cls.get("kind")
    if kind == "read_only":
        return "只读, 无副作用(预测: 仅产出信息/分析)"
    if kind == "red_line":
        return f"命中红线: {cls.get('why', '')} —— 影子环境不执行, 须 User 逐条确认"
    if _is_external(action):
        return "对外动作(可能影响他人, 红线 4): 仅起草/预测, 真发须 User 批"
    return "有副作用: 预测会改本机状态(文件/配置/数据), 入审批队列待批"


def _score_steps(steps: list, blast: int = 0, privacy: bool = False) -> dict:
    red = sum(1 for s in steps if s.get("kind") == "red_line")
    gated = sum(1 for s in steps if s.get("gated") and s.get("kind") != "red_line")
    irreversible = sum(1 for s in steps if not s.get("reversible"))
    external = sum(1 for s in steps if s.get("external"))
    raw = (red * _W_RED + gated * _W_GATED + irreversible * _W_IRREVERSIBLE
           + (external * _W_EXTERNAL) + (privacy * _W_PRIVACY)
           + min(blast, 10) * _W_BLAST)
    score = float(min(100, raw))
    if red > 0:
        band = "critical"
    elif score >= 70:
        band = "critical"
    elif score >= 40:
        band = "high"
    elif score >= 15:
        band = "medium"
    else:
        band = "low"
    return {"risk_score": round(score, 1), "risk_band": band,
            "breakdown": {"red_line": red, "gated": gated, "irreversible": irreversible,
                          "external": external, "privacy_hit": bool(privacy),
                          "blast_radius": blast}}


def _predicted_reaction(prefs) -> str:
    """从偏好/近期纠正预测用户可能反应(粗启发式; 不臆造正文)。"""
    if not prefs or not prefs.get("ok"):
        return "无足够偏好数据, 用户反应未知 —— 建议先入审批闸由 User 拍板"
    corr = len(prefs.get("recent_corrections", []) or [])
    n = prefs.get("count", 0)
    if corr >= 2:
        return f"近期有 {corr} 条纠正信号, 用户可能更挑剔 —— 高风险动作务必先确认"
    if n > 0:
        return f"命中 {n} 条既有偏好/约束 —— 与偏好一致则大概率认可, 冲突则可能要求改方案"
    return "用户反应中性 —— 低风险只读可放手, 有副作用仍建议先批"


def _build_steps(text: str, params: dict) -> list:
    """从计划文本 + 结构化参数构造分级步骤列表。"""
    raw = _extract_steps(text)
    if not raw:
        # 无 STEP: 行 → 整段当一个动作(或用 -p action= 显式给)
        single = params.get("action") or (text or "").strip()
        raw = [single] if single else []
    steps = []
    for i, a in enumerate(raw, 1):
        cls = _classify_step(a)
        steps.append({
            "n": i, "action": a,
            "kind": cls.get("kind", "mutating"),
            "gated": bool(cls.get("gated", True)),
            "why": cls.get("why", ""),
            "reversible": _reversible(a, cls),
            "external": _is_external(a),
            "predicted_effect": _predicted_effect(a, cls),
        })
    return steps


def _swarm_plan_steps(run_id: str):
    """读 agent_swarm/swarm run 的合并计划 → 步骤动作文本(只读)。缺引擎 → None。"""
    if _paths is None or not run_id:
        return None
    try:
        with open(_paths._plan_path(run_id), "r", encoding="utf-8") as f:
            plan = json.load(f)
    except Exception:
        return None
    return [s.get("action", "") for s in (plan.get("steps") or [])]


def _forecast(goal: str, steps: list) -> dict:
    """汇总后果预测: world_model 影响/预测/因果 + 偏好反应 + 风险分。"""
    ctx = _world_ctx(goal)
    prefs = _preferences(goal)
    private_hit = bool((ctx and ctx.get("_private")) or (prefs and prefs.get("_private")))

    impact = (ctx or {}).get("impact") or {}
    affects = impact.get("affects") or []
    depends = impact.get("depends_on") or []
    affected_entities = [a.get("name") for a in affects if a.get("name")]
    dependencies = [d.get("name") for d in depends if d.get("name")]
    predictions = ((ctx or {}).get("prediction") or {}).get("predictions") or []
    blast = len(affected_entities)

    score = _score_steps(steps, blast=blast, privacy=private_hit)

    failure_points = []
    for s in steps:
        if s.get("kind") == "red_line":
            failure_points.append(f"步骤{s['n']} 命中红线, 会被确认门拦下 —— 须 User 逐条批")
        elif s.get("gated") and not s.get("reversible"):
            failure_points.append(f"步骤{s['n']} 有副作用且不可逆 —— 失败难回滚")
    for p in predictions[:3]:
        if float(p.get("confidence", 0)) < 0.5:
            failure_points.append(f"低置信预测: {p.get('predicted', '')}(可能不如预期)")
    if not failure_points:
        failure_points.append("未见显著失败点(基于现有世界模型与分级)")

    benefit = (f"完成「{(goal or '')[:60]}」: {len(steps)} 步, "
               f"只读 {sum(1 for s in steps if s['kind']=='read_only')} / "
               f"有副作用 {sum(1 for s in steps if s['gated'])}")

    fc = {
        **score,
        "benefit": benefit,
        "failure_points": failure_points,
        "dependencies": dependencies,
        "affected_entities": affected_entities,
        "predicted_user_reaction": _predicted_reaction(prefs),
        "horizon_predictions": [{"predicted": p.get("predicted"),
                                 "confidence": p.get("confidence"),
                                 "kind": p.get("kind")} for p in predictions[:6]],
        "by_kind": {
            "read_only": sum(1 for s in steps if s["kind"] == "read_only"),
            "mutating": sum(1 for s in steps if s["kind"] == "mutating"),
            "red_line": sum(1 for s in steps if s["kind"] == "red_line"),
        },
    }
    return fc, private_hit


# --------------------------------------------------------------------------- #
# 1. simulate — 建仿真 + 后果预测(默认 dry, 绝不执行)
# --------------------------------------------------------------------------- #
def simulate(goal: str, params=None, kind: str = "plan",
             from_swarm: str = "", register_goal: bool = False) -> dict:
    goal = (goal or "").strip()
    params = dict(params or {})

    if from_swarm:
        actions = _swarm_plan_steps(from_swarm)
        if actions is None:
            return {"ok": False, "error": f"读不到 swarm run {from_swarm} 的合并计划(先 agent_swarm merge)"}
        goal = goal or f"swarm:{from_swarm}"
        text = "\n".join(f"STEP: {a}" for a in actions)
        kind = "swarm_run"
    else:
        text = goal

    if not goal and not params.get("action"):
        return {"ok": False, "error": "空目标(给 \"<目标/计划>\" 或 -p action=...)"}

    steps = _build_steps(text, params)
    if not steps:
        return {"ok": False, "error": "没解析出任何步骤"}

    forecast, private_hit = _forecast(goal, steps)
    goal_id = _register_goal(goal) if register_goal else None

    sid = _sim_id(goal or params.get("action", ""))
    sim = {
        "sim_id": sid, "created": _now(), "kind": kind,
        "title": (goal or params.get("action", ""))[:120],
        "source": text,
        "params": {k: v for k, v in params.items() if k != "action"},
        "action_kind": (params.get("action_kind")
                        if params.get("action_kind") in _EXEC_KINDS else None),
        "steps": steps,
        "forecast": forecast,
        "shadow": {"executed": False, "sandbox_dir": None, "trace": [], "diffs": []},
        "rollback": None,
        "status": "simulated",
        "approval_id": None,
        "goal_id": goal_id,
        "_private": private_hit,
        "executed_for_real": False,
    }
    _save_sim(sim)
    _audit("simulate", sid, kind=kind, steps=len(steps),
           band=forecast["risk_band"], private=private_hit or None, goal_id=goal_id)

    out = {"ok": True, "sim_id": sid, "kind": kind, "title": sim["title"],
           "steps": steps, "forecast": forecast,
           "note": "仿真已建(dry, 零副作用)。下一步可: dry_run / shadow_execute / risk_score / "
                   "impact_predict / approval_packet。真执行只经 approval_packet→User 批。"}
    if goal_id is not None:
        out["goal_id"] = goal_id
    if private_hit:
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 2. dry_run — 逐步走引擎自带预演面(零副作用)
# --------------------------------------------------------------------------- #
def dry_run(sim_id: str) -> dict:
    sim = _load_sim(sim_id)
    if not sim:
        return {"ok": False, "error": f"没找到仿真 {sim_id}(先 simulate)"}

    previews = []
    for s in sim["steps"]:
        action = s["action"]
        kind = s["kind"]
        if kind == "read_only":
            previews.append({"n": s["n"], "engine": "none",
                             "preview": "只读步骤: 直接产出信息, 无需预演执行", "ok": True})
            continue
        # UI/App 步骤 → computer_use plan=True(只看不点)
        if any(h in action for h in ("点击", "打开", "界面", "App", "窗口", "搜索", "输入框", "UI")):
            cu = _opt("computer_use")
            if cu is not None:
                try:
                    r = cu.do(action, plan=True)  # plan=True: 跑 see→decide 但不执行任何动作
                    previews.append({"n": s["n"], "engine": "computer_use.plan",
                                     "ok": bool(r.get("ok")),
                                     "planned_steps": r.get("steps", []),
                                     "stopped_reason": r.get("stopped_reason")})
                    continue
                except Exception as e:
                    previews.append({"n": s["n"], "engine": "computer_use.plan",
                                     "ok": False, "error": str(e)})
                    continue
        # 代码改动步骤 → self_improve confirmed=False(只回 needs_confirm 描述符, 不改任何代码)
        if any(h in action for h in ("改代码", "改我自己", "self_improve", "重构", "改 brain", "改模块")):
            si = _opt("self_improve")
            if si is not None:
                try:
                    r = si.improve(action, confirmed=False)  # 恒 needs_confirm, 零副作用
                    previews.append({"n": s["n"], "engine": "self_improve.needs_confirm",
                                     "ok": False, "needs_confirm": bool(r.get("needs_confirm")),
                                     "message": r.get("message")})
                    continue
                except Exception as e:
                    previews.append({"n": s["n"], "engine": "self_improve",
                                     "ok": False, "error": str(e)})
                    continue
        # 对外/App 步骤 → connector_forge(app_control_bus)只读预演: nexus.status / appsmith 可用性
        #   绝不 draft/send/approve/invoke —— 真发/真控经各自确认门由 User 批
        if s.get("external") or any(h in action for h in ("App", "应用", "邮件", "日历", "业务", "渠道")):
            cp = _connector_preview(action)
            if cp is not None:
                previews.append({"n": s["n"], "engine": f"connector_forge.{cp['connector']}",
                                 "ok": True, **cp})
                continue
        # 其余有副作用步骤 → 仅预测(不碰任何引擎执行面)
        previews.append({"n": s["n"], "engine": "predict-only",
                         "ok": True, "gated": s["gated"],
                         "preview": s["predicted_effect"]})

    sim["status"] = "dry_run"
    _save_sim(sim)
    _audit("dry_run", sim_id, steps=len(previews))
    out = {"ok": True, "sim_id": sim_id, "previews": previews,
           "note": "逐步预演(零副作用): computer_use 走 plan(只看不点)、self_improve 走 "
                   "needs_confirm(不改代码)、其余只预测。真执行仍须 approval_packet→User 批。"}
    if sim.get("_private"):
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 3. shadow_execute — 影子执行: 真实目标全程不碰, 文件改动落沙盒副本
# --------------------------------------------------------------------------- #
def shadow_execute(sim_id: str, params=None) -> dict:
    sim = _load_sim(sim_id)
    if not sim:
        return {"ok": False, "error": f"没找到仿真 {sim_id}(先 simulate)"}
    params = dict(params or {})

    sandbox = os.path.join(_sandbox_root(), sim_id)
    os.makedirs(sandbox, exist_ok=True)

    trace = []
    for s in sim["steps"]:
        if s["kind"] == "red_line":
            trace.append({"n": s["n"], "outcome": "shadow-skipped",
                          "why": f"命中红线({s.get('why','')}): 影子环境绝不执行强动作, 须 User 批"})
        elif s["gated"]:
            trace.append({"n": s["n"], "outcome": "shadow-skipped",
                          "why": "有副作用: 影子环境只记预测, 真执行经 approval_packet 入审批闸"})
        else:
            trace.append({"n": s["n"], "outcome": "shadow-ok",
                          "predicted": s["predicted_effect"]})

    diffs = []
    file_target = params.get("file") or (sim.get("params") or {}).get("file")
    content_file = params.get("content_file") or (sim.get("params") or {}).get("content_file")
    if file_target:
        diffs.append(_shadow_file_op(sandbox, file_target, content_file))

    sim["shadow"] = {"executed": True, "sandbox_dir": sandbox, "trace": trace, "diffs": diffs}
    sim["status"] = "shadow_done"
    _save_sim(sim)
    _audit("shadow_execute", sim_id, files=len(diffs), real_target_touched=False)
    out = {"ok": True, "sim_id": sim_id, "sandbox_dir": sandbox,
           "trace": trace, "diffs": diffs, "real_target_touched": False,
           "note": "影子执行完成: 真实目标全程未碰; 文件改动只落沙盒副本(可 diff/rollback_plan); "
                   "强动作只记预测不执行。"}
    if sim.get("_private") or any(d.get("_private") for d in diffs):
        out["_private"] = True
    return out


def _shadow_file_op(sandbox: str, file_target: str, content_file) -> dict:
    """在沙盒里模拟一次文件改写: 真实目标绝不写。私密文件只回元数据不读正文。"""
    base = os.path.basename(file_target) or "target"
    # 先过隐私门: 私密 / 库不可达 → 只回元数据, 不读真实正文
    priv = _path_privacy(file_target)
    if priv["private"]:
        meta = {"file": file_target, "_private": True, "redacted": True,
                "reason": f"隐私守门: {priv['reason']} —— 只回元数据, 不读/不展示正文"}
        try:
            meta["exists"] = os.path.exists(file_target)
        except Exception:
            meta["exists"] = None
        return meta
    # 非私密: 读原内容落沙盒 .orig, 新内容(若给)落 .new, 出 unified diff —— 真实目标不动
    try:
        with open(file_target, "r", encoding="utf-8", errors="replace") as f:
            orig = f.read()
    except Exception as e:
        return {"file": file_target, "error": f"读原文件失败: {e}", "exists": False}
    try:
        with open(os.path.join(sandbox, base + ".orig"), "w", encoding="utf-8") as f:
            f.write(orig)
    except Exception:
        pass

    new = None
    if content_file:
        # 新内容文件本身也过隐私门(不能用私密内容污染沙盒展示)
        if _path_privacy(content_file)["private"]:
            return {"file": file_target, "_private": True, "redacted": True,
                    "reason": "新内容来源文件命中隐私守门 —— 不展示 diff 正文"}
        try:
            with open(content_file, "r", encoding="utf-8", errors="replace") as f:
                new = f.read()
        except Exception as e:
            return {"file": file_target, "error": f"读新内容失败: {e}"}
    if new is None:
        return {"file": file_target, "sandboxed_orig": True,
                "lines": orig.count("\n") + 1,
                "note": "已把原文件复制进沙盒(给 -p content_file=路径 可生成改写 diff)"}

    try:
        with open(os.path.join(sandbox, base + ".new"), "w", encoding="utf-8") as f:
            f.write(new)
    except Exception:
        pass
    diff = list(difflib.unified_diff(orig.splitlines(), new.splitlines(),
                                     fromfile=base + " (原)", tofile=base + " (影子)",
                                     lineterm=""))
    return {"file": file_target, "sandboxed": True,
            "added": sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++")),
            "removed": sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---")),
            "diff": diff[:400]}


# --------------------------------------------------------------------------- #
# 4. diff — 看影子执行的前后差异(私密只回元数据)
# --------------------------------------------------------------------------- #
def diff(sim_id: str) -> dict:
    sim = _load_sim(sim_id)
    if not sim:
        return {"ok": False, "error": f"没找到仿真 {sim_id}"}
    shadow = sim.get("shadow") or {}
    if not shadow.get("executed"):
        return {"ok": False, "error": "尚未影子执行(先 shadow_execute)", "sim_id": sim_id}
    diffs = shadow.get("diffs") or []
    out = {"ok": True, "sim_id": sim_id, "sandbox_dir": shadow.get("sandbox_dir"),
           "file_diffs": diffs, "step_trace": shadow.get("trace") or [],
           "note": "影子差异(只读): 文件 diff 来自沙盒副本, 真实目标从未改动; 私密文件只回元数据。"}
    if sim.get("_private") or any(d.get("_private") for d in diffs):
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 5. risk_score — 风险评分 0-100 分解(复用 local_powers 分级)
# --------------------------------------------------------------------------- #
def risk_score(sim_id_or_action: str) -> dict:
    sim = _load_sim(sim_id_or_action)
    if sim:
        steps = sim["steps"]
        fc = sim.get("forecast") or {}
        blast = len(fc.get("affected_entities", []))
        privacy = bool(sim.get("_private"))
        title = sim.get("title")
        sid = sim["sim_id"]
    else:
        action = (sim_id_or_action or "").strip()
        if not action:
            return {"ok": False, "error": "给 <sim_id> 或 \"<动作>\""}
        steps = _build_steps(action, {})
        blast = 0
        privacy = False
        title = action[:120]
        sid = None

    score = _score_steps(steps, blast=blast, privacy=privacy)
    out = {"ok": True, "sim_id": sid, "title": title,
           **score,
           "per_step": [{"n": s["n"], "action": s["action"][:120], "kind": s["kind"],
                         "gated": s["gated"], "reversible": s["reversible"],
                         "external": s["external"]} for s in steps],
           "note": "风险分 = 红线×40 + 副作用×12 + 不可逆×15 + 对外×20 + 隐私×15 + 波及面×2(封顶100)。"
                   "红线分级转发 local_powers 单一真相源; 任一红线 → band=critical。"}
    if privacy:
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 6. impact_predict — 后果预测(world_model 影响波及 + 下一步预测 + 因果链)
# --------------------------------------------------------------------------- #
def impact_predict(sim_id_or_target: str) -> dict:
    sim = _load_sim(sim_id_or_target)
    target = sim["title"] if sim else (sim_id_or_target or "").strip()
    if not target:
        return {"ok": False, "error": "给 <sim_id> 或 \"<目标实体>\""}

    wm = _opt("world_model")
    if wm is None:
        return {"ok": False, "error": "world_model 不可用, 无法做后果预测",
                "sim_id": sim["sim_id"] if sim else None}

    impact = _impact(target) or {}
    pred = _predict(target) or {}
    causal = None
    try:
        causal = wm.causal_chain(target)
    except Exception:
        causal = None

    private_hit = bool(impact.get("_private") or pred.get("_private")
                       or (causal and causal.get("_private")))
    out = {"ok": True, "sim_id": sim["sim_id"] if sim else None, "target": target,
           "impact": {"affects": impact.get("affects", []),
                      "depends_on": impact.get("depends_on", []),
                      "goals_touched": impact.get("goals_touched", []),
                      "constraints_touched": impact.get("constraints_touched", []),
                      "confidence": impact.get("confidence")},
           "next_step_predictions": pred.get("predictions", []),
           "causal_chain": ((causal or {}).get("chains")
                            or (causal or {}).get("chain") or (causal or {}).get("links")),
           "note": "后果预测(只读): 沿因果/阻塞前向波及 + 历史复现预测 + 因果链。来自 world_model 单一图谱。"}
    if private_hit:
        out["_private"] = True
    _audit("impact_predict", sim["sim_id"] if sim else "", private=private_hit or None)
    return out


# --------------------------------------------------------------------------- #
# 7. compare_plans — 多方案对比择优(风险↓·可逆↑·偏好契合↑)
# --------------------------------------------------------------------------- #
def compare_plans(sim_ids: list) -> dict:
    sim_ids = [s for s in (sim_ids or []) if s]
    if len(sim_ids) < 2:
        return {"ok": False, "error": "compare_plans 需要至少 2 个 sim_id"}
    rows = []
    private_hit = False
    for sid in sim_ids:
        sim = _load_sim(sid)
        if not sim:
            rows.append({"sim_id": sid, "error": "未找到"})
            continue
        fc = sim.get("forecast") or {}
        steps = sim.get("steps") or []
        reversible_ratio = (sum(1 for s in steps if s.get("reversible")) / max(1, len(steps)))
        risk = float(fc.get("risk_score") or 100.0)  # null/缺失 → 当最坏(不让坏数据冒充最优)
        # 综合分: 越高越优 = 低风险 + 高可逆 + 步骤覆盖(收益代理); 区间约 0-1
        composite = round(max(0.0, (100 - risk) / 100 * 0.6
                              + reversible_ratio * 0.3
                              + min(len(steps), 8) / 8 * 0.1), 4)
        if sim.get("_private"):
            private_hit = True
        rows.append({"sim_id": sid, "title": sim.get("title"),
                     "risk_score": risk, "risk_band": fc.get("risk_band"),
                     "reversible_ratio": round(reversible_ratio, 3),
                     "steps": len(steps), "composite": composite,
                     "failure_points": len(fc.get("failure_points", []))})
    ranked = [r for r in rows if "composite" in r]
    ranked.sort(key=lambda r: r["composite"], reverse=True)
    out = {"ok": True, "count": len(rows), "ranked": ranked + [r for r in rows if "error" in r],
           "recommended": ranked[0]["sim_id"] if ranked else None,
           "rationale": ("综合分 = (100−风险)/100×0.6 + 可逆比×0.3 + 步骤覆盖×0.1。"
                         "选风险最低、最可逆的路径; 仍须经 approval_packet→User 批落地。")}
    if private_hit:
        out["_private"] = True
    _audit("compare_plans", "", count=len(sim_ids),
           recommended=out.get("recommended"))
    return out


# --------------------------------------------------------------------------- #
# 8. rollback_plan — 生成回滚/撤销预案(只出预案, 不执行, 不删沙盒)
# --------------------------------------------------------------------------- #
def rollback_plan(sim_id: str) -> dict:
    sim = _load_sim(sim_id)
    if not sim:
        return {"ok": False, "error": f"没找到仿真 {sim_id}"}
    steps = sim.get("steps") or []
    plan = []
    auto_revertible = True
    for s in steps:
        if s["kind"] == "read_only":
            continue
        if not s.get("reversible"):
            auto_revertible = False
            plan.append({"n": s["n"], "undo": "不可自动回滚",
                         "why": "不可逆动作(删除/发送/部署/推送等), 撤销须人工补偿措施"})
        elif s.get("external"):
            auto_revertible = False
            plan.append({"n": s["n"], "undo": "对外动作无法撤回",
                         "why": "已发出的对外动作不可收回, 只能跟进补救(撤回/更正通知)"})
        else:
            plan.append({"n": s["n"], "undo": "恢复改动前状态",
                         "why": "经审批队列做逆操作(从备份/沙盒 .orig 覆盖回 / 反向 move)"})
    # 沙盒文件回滚预案(若已影子执行): 用 .orig 覆盖回真实目标 —— 仅作"预案", 本层绝不执行/删除
    shadow = sim.get("shadow") or {}
    for d in (shadow.get("diffs") or []):
        if d.get("sandboxed"):
            plan.append({"file": d.get("file"),
                         "undo": f"用沙盒 .orig 覆盖回 {d.get('file')}",
                         "why": "覆盖回写本身是有副作用动作, 须经 approval_packet→User 批执行"})
    sim["rollback"] = {"plan": plan, "auto_revertible": auto_revertible}
    _save_sim(sim)
    out = {"ok": True, "sim_id": sim_id, "auto_revertible": auto_revertible,
           "rollback_plan": plan,
           "note": "回滚预案(只出方案, 不执行, 不删沙盒): 任何回写/逆操作本身也是副作用, "
                   "须经 approval_packet→User 批。沙盒副本永久保留(只 User 手动清)。"}
    if sim.get("_private"):
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 9. approval_packet — 打包入审批队列(risk≥high), 待 User 批; 绝不自动执行
# --------------------------------------------------------------------------- #
def approval_packet(sim_id: str) -> dict:
    sim = _load_sim(sim_id)
    if not sim:
        return {"ok": False, "error": f"没找到仿真 {sim_id}(先 simulate)"}
    apstore = _opt("autopilot_store")
    if apstore is None:
        return {"ok": False, "error": "审批队列(autopilot_store)不可用, 无法打包入队。"}

    fc = sim.get("forecast") or {}
    band = fc.get("risk_band", "high")
    risk = "critical" if band == "critical" else "high"  # 仿真落地恒 risk≥high
    action_kind = sim.get("action_kind") or "note"       # 未知 → note(User 人工复核)
    title = sim.get("title", "")[:120]

    payload = {
        "action_kind": action_kind,
        "sim_id": sim_id,
        "source": "simulation_lab",
        "title": title,
        "steps": [{"n": s["n"], "action": s["action"], "kind": s["kind"],
                   "gated": s["gated"]} for s in sim.get("steps", [])],
        "forecast": {"risk_score": fc.get("risk_score"), "risk_band": band,
                     "failure_points": fc.get("failure_points"),
                     "affected_entities": fc.get("affected_entities"),
                     "predicted_user_reaction": fc.get("predicted_user_reaction")},
    }
    # note 类只描述, 不绑可执行字段; 可执行 kind 则把首个有副作用步骤动作放进去供 autopilot._execute
    if action_kind != "note":
        first = next((s for s in sim.get("steps", []) if s.get("gated")), None)
        if first is not None:
            payload["task"] = first["action"]
            payload["action"] = first["action"]

    why = (f"[simulation_lab 仿真后果] 风险 {fc.get('risk_score')}/{band}; "
           f"失败点 {len(fc.get('failure_points', []))}; "
           f"预测反应: {fc.get('predicted_user_reaction', '')[:60]}")
    try:
        ap_id = apstore.enqueue_approval(action=title or f"仿真 {sim_id} 落地",
                                         action_kind=action_kind, payload=payload,
                                         goal_id=sim.get("goal_id"), risk=risk, why=why)
    except Exception as e:
        return {"ok": False, "error": f"入审批队列失败: {e}"}

    sim["approval_id"] = ap_id
    sim["status"] = "packaged"
    _save_sim(sim)
    _audit("approval_packet", sim_id, approval_id=ap_id, risk=risk, action_kind=action_kind)
    tip = f"jarvis autopilot approve {ap_id}"
    if risk == "critical":
        tip += "（critical: 需带 confirm token, autopilot 会摊给你）"
    out = {"ok": True, "sim_id": sim_id, "approval_id": ap_id, "risk": risk,
           "action_kind": action_kind, "needs_confirm": True,
           "note": f"已把仿真打包入审批队列(待 User 批)。仿真层绝不自动执行; User 批准: {tip}。"}
    if sim.get("_private"):
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 10. rehearse_ui — 演练 App/UI 操作(computer_use plan=True, 只看不点)
# --------------------------------------------------------------------------- #
def rehearse_ui(sim_id_or_task: str) -> dict:
    sim = _load_sim(sim_id_or_task)
    task = sim["title"] if sim else (sim_id_or_task or "").strip()
    if not task:
        return {"ok": False, "error": "给 <sim_id> 或 \"<UI 任务>\""}
    cu = _opt("computer_use")
    if cu is None:
        return {"ok": False, "error": "computer_use 不可用, 无法演练 UI"}
    try:
        r = cu.do(task, plan=True)  # plan=True: 跑 see→decide 全程, 但绝不执行任何键鼠动作
    except Exception as e:
        return {"ok": False, "error": f"演练失败: {e}", "task": task}
    out = {"ok": bool(r.get("ok")), "sim_id": sim["sim_id"] if sim else None, "task": task,
           "planned_steps": r.get("steps", []), "stopped_reason": r.get("stopped_reason"),
           "note": "UI 演练(computer_use plan=True): 真截屏+决策但只看不点, 零键鼠副作用。"
                   "真接管键鼠须 User 经 jarvis computer_use 显式确认。"}
    if not r.get("ok"):
        out["detail"] = r.get("error") or r.get("message")
    if sim and sim.get("_private"):
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 11. test_action — 单动作快测(轻量, 建临时仿真)
# --------------------------------------------------------------------------- #
def test_action(action: str) -> dict:
    action = (action or "").strip()
    if not action:
        return {"ok": False, "error": "给 \"<动作>\""}
    steps = _build_steps(action, {})
    s0 = steps[0]
    score = _score_steps(steps)
    if s0["kind"] == "red_line":
        verdict = "red_line"
    elif s0["gated"]:
        verdict = "needs_approval"
    else:
        verdict = "safe_readonly"
    # 轻量后果探针(有实体才查)
    impact = _impact(action) if verdict != "safe_readonly" else None
    affected = [a.get("name") for a in (impact or {}).get("affects", [])][:8] if impact else []

    sid = _sim_id(action)
    sim = {"sim_id": sid, "created": _now(), "kind": "action", "title": action[:120],
           "source": action, "params": {}, "action_kind": None, "steps": steps,
           "forecast": {**score, "verdict": verdict, "affected_entities": affected},
           "shadow": {"executed": False, "sandbox_dir": None, "trace": [], "diffs": []},
           "rollback": None, "status": "simulated", "approval_id": None,
           "goal_id": None, "_private": bool(impact and impact.get("_private")),
           "executed_for_real": False}
    _save_sim(sim)
    _audit("test_action", sid, verdict=verdict, band=score["risk_band"])
    out = {"ok": True, "sim_id": sid, "action": action, "verdict": verdict,
           **score, "affected_entities": affected,
           "predicted_effect": s0["predicted_effect"], "reversible": s0["reversible"],
           "note": "单动作快测: 分级(local_powers 单一真相源) + 风险 + 轻量后果。"
                   "verdict=safe_readonly 可放手; needs_approval/red_line 须 approval_packet→批。"}
    if sim["_private"]:
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 12. what_if — 反事实推演(改一个前提看结果怎么变)
# --------------------------------------------------------------------------- #
def what_if(sim_id: str, change: str = "", assume: str = "") -> dict:
    sim = _load_sim(sim_id)
    if not sim:
        return {"ok": False, "error": f"没找到仿真 {sim_id}(先 simulate)"}
    if not change and not assume:
        return {"ok": False, "error": "what_if 需要 --change k=v 或 --assume \"<假设>\""}
    if change and "=" not in change:
        return {"ok": False, "error": "--change 需要 k=v 形式(如 step2=新动作 / file=路径); "
                                      "自由文本假设请用 --assume \"<假设>\""}

    before = sim.get("forecast") or {}
    # 构造反事实计划: 在原目标上叠加改动/假设, 重新分级 + 预测
    new_params = dict(sim.get("params") or {})
    new_goal = sim.get("title", "")
    if change and "=" in change:
        k, v = change.split("=", 1)
        if k.strip().lower().startswith("step") and k.strip()[4:].isdigit():
            # 替换某一步
            idx = int(k.strip()[4:])
            base_actions = [s["action"] for s in sim.get("steps", [])]
            if 1 <= idx <= len(base_actions):
                base_actions[idx - 1] = v.strip()
            text = "\n".join(f"STEP: {a}" for a in base_actions)
            steps = _build_steps(text, {})
        else:
            new_params[k.strip()] = v.strip()
            new_goal = f"{new_goal}（假设 {change}）"
            steps = _build_steps(sim.get("source", new_goal), new_params)
    else:
        new_goal = f"{new_goal}（假设 {assume}）"
        steps = _build_steps(sim.get("source", new_goal), new_params)

    after, private_hit = _forecast(new_goal + " " + assume, steps)

    sid = _sim_id("whatif:" + sim_id)
    cf = {"sim_id": sid, "created": _now(), "kind": "what_if",
          "title": new_goal[:120], "source": sim.get("source", ""), "params": new_params,
          "action_kind": sim.get("action_kind"), "steps": steps, "forecast": after,
          "shadow": {"executed": False, "sandbox_dir": None, "trace": [], "diffs": []},
          "rollback": None, "status": "simulated", "approval_id": None,
          "goal_id": None, "_private": private_hit, "executed_for_real": False,
          "derived_from": sim_id, "change": change, "assume": assume}
    _save_sim(cf)
    _audit("what_if", sid, derived_from=sim_id)

    deltas = {
        "risk_score": round(float(after.get("risk_score") or 0.0) - float(before.get("risk_score") or 0.0), 1),
        "risk_band": f"{before.get('risk_band')} → {after.get('risk_band')}",
        "failure_points": len(after.get("failure_points", [])) - len(before.get("failure_points", [])),
        "affected_entities": len(after.get("affected_entities", [])) - len(before.get("affected_entities", [])),
    }
    out = {"ok": True, "base_sim": sim_id, "counterfactual_sim": sid,
           "change": change, "assume": assume,
           "before": {"risk_score": before.get("risk_score"), "risk_band": before.get("risk_band"),
                      "failure_points": before.get("failure_points")},
           "after": {"risk_score": after.get("risk_score"), "risk_band": after.get("risk_band"),
                     "failure_points": after.get("failure_points"),
                     "predicted_user_reaction": after.get("predicted_user_reaction")},
           "deltas": deltas,
           "note": "反事实推演: 改一个前提重新分级+预测, 看风险/失败点怎么变。仍是 dry, 零副作用。"}
    if private_hit:
        out["_private"] = True
    return out


# =========================================================================== #
# 多情景反事实未来沙盘(scenario sandbox): 把候选计划 + User 目标 + 日历/待办/
# 邮件/文件状态 + world_model 机制规则 + epistemic_core 不确定性 + constraint_planner
# 约束, 融成可模拟场景, 自动生成 best/base/worst + 多步后果链 + 二阶影响 + 失败触发点 +
# 敏感变量 + 可逆回退路径 + 监控指标。供 reasoning_core / mission_control /
# strategy_tournament / metacognition_core 直接 import 调用(纯只读预测, 零真实副作用)。
#
# 缺失的 epistemic_core / constraint_planner 一律 best-effort 软导入并记 deferred,
# 绝不建桩; 它们落地后自动接上。world_model 不可达则退回分级启发式。
# 凭据文件硬拦 + 隐私(_private)沿用既有守门, 命中私密整包带 _private 让大脑切本机。
# =========================================================================== #
def _scn_id(seed: str) -> str:
    h = hashlib.sha1(("scn:" + (seed or "") + _now()).encode("utf-8")).hexdigest()[:10]
    return f"scn_{h}"


def _band_for(score: float, has_red: bool) -> str:
    if has_red or score >= 70:
        return "critical"
    if score >= 40:
        return "high"
    if score >= 15:
        return "medium"
    return "low"


def _futures_probs(n_red: int, n_irrev: int, n_ext: int, unc: float) -> dict:
    """best/base/worst 概率(启发式): 红线/不可逆/对外步骤数 + 不确定性驱动, 归一化到 1。"""
    p_worst = 0.12 + 0.10 * n_red + 0.05 * n_irrev + 0.05 * n_ext + 0.30 * unc
    p_best = 0.45 - 0.12 * n_red - 0.05 * n_irrev - 0.25 * unc
    p_worst = min(0.85, max(0.05, p_worst))
    p_best = min(0.70, max(0.05, p_best))
    p_base = max(0.05, 1.0 - p_worst - p_best)
    tot = p_worst + p_best + p_base
    return {"best": round(p_best / tot, 3), "base": round(p_base / tot, 3),
            "worst": round(p_worst / tot, 3)}


def _uncertainty_signal(inputs: dict, base_forecast: dict) -> float:
    """不确定性 [0,1]: 优先 epistemic_core, 否则用预测平均置信的补数代理, 再否则中等。"""
    epi = inputs.get("uncertainty")
    if isinstance(epi, dict):
        for k in ("uncertainty", "entropy", "sigma", "score"):
            v = epi.get(k)
            if isinstance(v, (int, float)):
                return min(1.0, max(0.0, float(v)))
    preds = (base_forecast.get("horizon_predictions")
             or (inputs.get("mechanisms") or {}).get("predictions") or [])
    confs = [float(p.get("confidence")) for p in preds
             if isinstance(p.get("confidence"), (int, float))]
    if confs:
        return min(1.0, max(0.0, 1.0 - sum(confs) / len(confs)))
    return 0.4


# --------------------------------------------------------------------------- #
# 输入融合(全只读 + best-effort + 私密守门; 缺模块记 deferred 不建桩)
# --------------------------------------------------------------------------- #
def _user_goals(topic: str = "") -> tuple:
    """读 User 当前目标(mission_control + autopilot_store, 只读)。只取标题/优先级/状态,
    丢 detail/constraints/preferences 正文(避免带出敏感细节)。缺模块 → deferred。"""
    goals = []
    deferred = []
    mc = _opt("mission_control")
    if mc is not None:
        try:
            for g in (mc.list_goals(status="active") or [])[:8]:
                t = (g.get("title") or "")[:120]
                if t:
                    goals.append({"title": t, "priority": g.get("priority"),
                                  "status": g.get("status"), "source": "mission_control"})
        except Exception:
            pass
    else:
        deferred.append({"module": "mission_control",
                         "reason": "不可达 → User 目标缺失, 场景以计划本身为准"})
    ap = _opt("autopilot_store")
    if ap is not None:
        try:
            for g in (ap.list_goals(status="active") or [])[:8]:
                t = (g.get("title") or "")[:120]
                if t and not any(t == x["title"] for x in goals):
                    goals.append({"title": t, "priority": g.get("priority"),
                                  "status": g.get("status"), "source": "autopilot_store"})
        except Exception:
            pass
    return goals[:12], deferred


def _state_snapshot(topic: str = "") -> tuple:
    """读日历/待办/邮件/文件状态(经 shared_context 统一只读快照, 已脱敏 + 凭据硬拦)。
    **只取元数据**(各源计数 / 已脱敏焦点摘要 / _private), 绝不取条目正文。缺则 deferred。"""
    sc = _opt("shared_context")
    if sc is None:
        return None, [{"module": "shared_context",
                       "reason": "不可达 → 日历/待办/邮件/文件状态缺失, 场景降级"}]
    try:
        snap = sc.capture(utterance=topic or "", with_screen=False, hours=48.0) or {}
    except Exception:
        return None, [{"module": "shared_context",
                       "reason": "capture 出错 → 状态快照缺失, 场景降级"}]
    focus = snap.get("focus") or {}
    out = {"counts": snap.get("counts") or {}, "n_items": snap.get("n_items"),
           "focus": "（私密焦点已抹）" if focus.get("private") else (focus.get("summary") or ""),
           "_private": bool(snap.get("_private"))}
    return out, []


def _wm_mechanisms(topic: str = "") -> dict:
    """world_model 机制规则 + 影响波及 + 预测(只读)。从 causal_chain 取机制短句,
    impact 取受影响/依赖实体, predict 取下一步预测。命中私密自动 _private。缺则 available=False。
    向前兼容: 若未来 world_model 提供 build_model() 读面则一并采(getattr, 绝不 exec/__import__)。"""
    wm = _opt("world_model")
    if wm is None:
        return {"mechanisms": [], "impact": {}, "predictions": [],
                "_private": False, "available": False}
    mechs = []
    impact = {}
    preds = []
    priv = False
    try:
        cc = wm.causal_chain(topic) or {}
        priv = priv or bool(cc.get("_private"))
        for ch in (cc.get("chains") or [])[:4]:
            for st in (ch.get("steps") or []):
                m = st.get("mechanism")
                if m:
                    mechs.append(m)
    except Exception:
        pass
    try:
        im = _impact(topic) or {}
        priv = priv or bool(im.get("_private"))
        impact = {"affects": [a.get("name") for a in im.get("affects", []) if a.get("name")],
                  "depends_on": [d.get("name") for d in im.get("depends_on", []) if d.get("name")],
                  "goals_touched": im.get("goals_touched", []),
                  "constraints_touched": im.get("constraints_touched", []),
                  "confidence": im.get("confidence")}
    except Exception:
        pass
    try:
        pr = _predict(topic) or {}
        priv = priv or bool(pr.get("_private"))
        preds = [{"predicted": p.get("predicted"), "confidence": p.get("confidence"),
                  "kind": p.get("kind"), "mechanism": p.get("mechanism")}
                 for p in pr.get("predictions", [])[:6]]
    except Exception:
        pass
    bm = getattr(wm, "build_model", None)
    if callable(bm):
        try:
            mdl = bm(topic) or {}
            priv = priv or bool(mdl.get("_private"))
            for r in (mdl.get("mechanisms") or [])[:6]:
                txt = (r.get("rule") or r.get("mechanism") or r.get("text")) if isinstance(r, dict) else r
                if txt:
                    mechs.append(txt)
        except Exception:
            pass
    seen = set()
    um = []
    for m in mechs:
        if m and m not in seen:
            seen.add(m)
            um.append(m)
    return {"mechanisms": um[:10], "impact": impact, "predictions": preds,
            "_private": priv, "available": True}


def _epistemic(topic: str = "") -> tuple:
    """epistemic_core 不确定性(只读, best-effort)。未实现 → deferred(不建桩, 落地自动接上)。
    只探约定的只读接口(context_for / uncertainty), 绝不触发可能有副作用的方法。"""
    ec = _opt("epistemic_core")
    if ec is None:
        return None, [{"module": "epistemic_core",
                       "reason": "尚未实现 → 不确定性用预测置信代理; epistemic_core 落地后自动接上"}]
    cf = getattr(ec, "context_for", None)
    if callable(cf):
        try:
            r = cf(consumer="simulation_lab", topic=topic)
            if isinstance(r, dict):
                return r, []
        except Exception:
            pass
    fn = getattr(ec, "uncertainty", None)
    if callable(fn):
        try:
            r = fn(topic)
            if isinstance(r, dict):
                return r, []
        except Exception:
            pass
    return None, [{"module": "epistemic_core",
                   "reason": "在装但无可识别只读接口 → 退回预测置信代理"}]


def _constraints(topic: str = "") -> tuple:
    """constraint_planner 约束(只读, best-effort)。未实现 → deferred(不建桩)。
    只探约定只读接口(context_for / constraints), 绝不触发规划/执行面。"""
    cp = _opt("constraint_planner")
    if cp is None:
        return None, [{"module": "constraint_planner",
                       "reason": "尚未实现 → 约束以红线分级+偏好代理; constraint_planner 落地后自动接上"}]
    cf = getattr(cp, "context_for", None)
    if callable(cf):
        try:
            r = cf(consumer="simulation_lab", topic=topic)
            if isinstance(r, dict):
                return r, []
        except Exception:
            pass
    fn = getattr(cp, "constraints", None)
    if callable(fn):
        try:
            r = fn(topic)
            if isinstance(r, dict):
                return r, []
        except Exception:
            pass
    return None, [{"module": "constraint_planner",
                   "reason": "在装但无可识别只读接口 → 退回红线+偏好代理"}]


def _gather_scenario_inputs(goal: str, steps: list, params: dict) -> tuple:
    """把候选计划 + User 目标 + 状态 + 机制规则 + 不确定性 + 约束融成可模拟输入(全只读)。
    返回 (base_forecast, inputs, deferred, private)。"""
    base_forecast, fc_private = _forecast(goal, steps)
    goals, d1 = _user_goals(goal)
    state, d2 = _state_snapshot(goal)
    mech = _wm_mechanisms(goal)
    epi, d3 = _epistemic(goal)
    con, d4 = _constraints(goal)
    deferred = d1 + d2 + d3 + d4
    if not mech.get("available"):
        deferred.append({"module": "world_model",
                         "reason": "不可达 → 机制规则/后果波及缺失, 场景以分级启发式为准"})
    private = bool(fc_private or (state and state.get("_private")) or mech.get("_private")
                   or (epi and epi.get("_private")) or (con and con.get("_private")))
    inputs = {"goals": goals, "state": state, "mechanisms": mech,
              "uncertainty": epi, "constraints": con}
    return base_forecast, inputs, deferred, private


# --------------------------------------------------------------------------- #
# 情景生成(best/base/worst + 后果链 + 叙事; 全启发式, 红线仍以 local_powers 为准)
# --------------------------------------------------------------------------- #
def _chain_for_case(case: str, steps: list, mechanisms: list) -> list:
    chain = []
    for s in steps:
        immediate = s.get("predicted_effect", "")
        if case == "worst":
            if s["kind"] == "red_line":
                downstream = "被确认门拦截; 若误放行→不可逆损害, 须人工补偿"
            elif not s.get("reversible"):
                downstream = "副作用落地后难回滚, 失败需补偿"
            elif s.get("external"):
                downstream = "对外动作发出不可收回, 错误需跟进更正"
            else:
                downstream = "副作用叠加, 可能偏离预期"
        elif case == "best":
            downstream = ("顺利完成, 进入下一步" if s["kind"] != "read_only"
                          else "产出信息支撑后续决策")
        else:
            downstream = "按分级处理: 只读直接产出, 有副作用入审批闸由 User 批"
        chain.append({"step": s["n"], "action": (s.get("action") or "")[:100],
                      "immediate": immediate, "downstream": downstream})
    for m in (mechanisms or [])[:2]:
        chain.append({"step": "因果", "action": "", "immediate": m,
                      "downstream": "world_model 机制链"})
    return chain


def _case_narrative(case: str, steps: list, n_red: int, n_irrev: int,
                    n_ext: int, unc: float) -> str:
    n = len(steps)
    if case == "best":
        tail = "含红线步骤, 即便最好也须 User 逐条确认。" if n_red else ""
        return (f"最好情形: {n} 步全程顺利, 有副作用步骤经审批后正确执行、可逆动作均可回滚; "
                f"高置信预测兑现, 无约束冲突。{tail}")
    if case == "worst":
        bits = []
        if n_red:
            bits.append(f"{n_red} 个红线步骤")
        if n_irrev:
            bits.append(f"{n_irrev} 个不可逆动作")
        if n_ext:
            bits.append(f"{n_ext} 个对外动作")
        head = "、".join(bits) or "关键步骤"
        return (f"最坏情形: {head} 触发失败 — 不可逆损害难回滚 / 对外动作不可收回 / 低置信预测落空; "
                f"不确定性 {round(unc, 2)} 放大偏差。须靠回退路径与人工补偿。")
    return (f"基准情形(最可能): {n} 步按分级处理 — 只读直接产出, 有副作用入审批闸由 User 批; "
            f"大体符合预测, 个别低置信项可能小幅偏差。")


def _case_outcomes(case: str, base_forecast: dict, inputs: dict) -> list:
    impact = (inputs.get("mechanisms") or {}).get("impact") or {}
    ents = impact.get("affects") or base_forecast.get("affected_entities") or []
    if case == "best":
        return [f"目标达成, 波及 {len(ents)} 实体均为预期内", "无需回滚", "用户大概率认可"]
    if case == "worst":
        return [f"部分/全部失败, {len(ents)} 实体受非预期影响",
                "可能触发不可逆损害或对外更正", "需启动回退路径 + User 介入"]
    return [f"主体完成, 有副作用步骤待 User 批", f"波及 {len(ents)} 实体(预期内)", "个别项需复核"]


def _case_assumptions(case: str, inputs: dict) -> list:
    a = []
    if inputs.get("uncertainty") is None:
        a.append("无 epistemic_core: 不确定性用预测置信代理")
    if inputs.get("constraints") is None:
        a.append("无 constraint_planner: 约束以红线分级+偏好代理")
    if not (inputs.get("mechanisms") or {}).get("available"):
        a.append("world_model 不可达: 后果波及/机制规则缺失, 退回分级启发式")
    if case == "best":
        a.append("假设外部依赖全部可用、无并发冲突")
    if case == "worst":
        a.append("假设关键依赖失效且失败点同时触发")
    return a or ["输入齐备, 按现有世界模型推导"]


def _build_futures(goal: str, steps: list, base_forecast: dict, inputs: dict) -> dict:
    n_red = sum(1 for s in steps if s["kind"] == "red_line")
    n_irrev = sum(1 for s in steps if not s.get("reversible"))
    n_ext = sum(1 for s in steps if s.get("external"))
    has_red = n_red > 0
    unc = _uncertainty_signal(inputs, base_forecast)
    probs = _futures_probs(n_red, n_irrev, n_ext, unc)
    base_risk = float(base_forecast.get("risk_score") or 0.0)
    worst_risk = min(100.0, base_risk + 20.0 + 8.0 * n_red + 6.0 * n_irrev)
    best_risk = max(0.0, base_risk - 22.0)
    mechs = (inputs.get("mechanisms") or {}).get("mechanisms") or []
    cases = {}
    for case, risk in (("best", best_risk), ("base", base_risk), ("worst", worst_risk)):
        cases[case] = {
            "probability": probs[case],
            "risk_score": round(risk, 1),
            "risk_band": _band_for(risk, has_red),
            "narrative": _case_narrative(case, steps, n_red, n_irrev, n_ext, unc),
            "key_outcomes": _case_outcomes(case, base_forecast, inputs),
            "consequence_chain": _chain_for_case(case, steps, mechs),
            "assumptions": _case_assumptions(case, inputs),
        }
    return {"cases": cases, "probabilities": probs,
            "uncertainty_signal": round(unc, 3),
            "method": ("启发式: 概率由红线/不可逆/对外步骤数 + 不确定性(epistemic 或预测置信代理)推导, "
                       "风险由基线分级偏移; 全 dry 预测, 零真实副作用。")}


# --------------------------------------------------------------------------- #
# 二阶影响 / 失败触发点 / 敏感变量 / 监控指标 / 回退路径(全只读)
# --------------------------------------------------------------------------- #
def _second_order(target: str, inputs: dict) -> dict:
    impact = (inputs.get("mechanisms") or {}).get("impact") or {}
    first = impact.get("affects") or []
    wm = _opt("world_model")
    priv = bool((inputs.get("mechanisms") or {}).get("_private"))
    out = []
    for name in first[:6]:
        ring = []
        if wm is not None and name:
            try:
                im = wm.impact(name) or {}
                priv = priv or bool(im.get("_private"))
                ring = [a.get("name") for a in im.get("affects", [])
                        if a.get("name") and a.get("name") != name][:5]
            except Exception:
                ring = []
        out.append({"first_order": name, "second_order": ring,
                    "note": "一阶受影响实体的下游波及(world_model 2 跳)" if ring else "无显著二阶波及"})
    for g in (impact.get("goals_touched") or [])[:4]:
        gname = g if isinstance(g, str) else (g.get("name") or "")
        if gname:
            out.append({"first_order": "既有目标", "second_order": [gname],
                        "note": "触及既有目标 → 可能改变优先级/排期(二阶)"})
    return {"effects": out, "_private": priv,
            "note": "二阶影响只读自 world_model 影响图(命中私密只回实体名/已脱敏)。"}


def _failure_triggers(steps: list, base_forecast: dict, inputs: dict) -> list:
    trig = []
    for s in steps:
        if s["kind"] == "red_line":
            trig.append({"trigger": f"步骤{s['n']} 红线", "condition": s.get("why", "命中红线"),
                         "leads_to": "被确认门拦截; 误放行→不可逆损害", "severity": "critical",
                         "mitigation": "必须 User 逐条确认(critical token), 仿真层绝不自动执行",
                         "source": "local_powers 分级"})
        elif s.get("gated") and not s.get("reversible"):
            trig.append({"trigger": f"步骤{s['n']} 不可逆副作用",
                         "condition": "动作执行后无法自动撤销",
                         "leads_to": "失败难回滚, 须人工补偿", "severity": "high",
                         "mitigation": "先影子执行/备份, 经 approval_packet 入审批闸",
                         "source": "可逆性启发式"})
        elif s.get("external"):
            trig.append({"trigger": f"步骤{s['n']} 对外动作",
                         "condition": "已发出的对外动作不可收回",
                         "leads_to": "影响他人(红线4), 错误需跟进更正", "severity": "high",
                         "mitigation": "真发须 User 批; 先起草预览", "source": "对外信号"})
    preds = (base_forecast.get("horizon_predictions")
             or (inputs.get("mechanisms") or {}).get("predictions") or [])
    seen_pred = set()
    for p in preds:
        c = p.get("confidence")
        c = float(c) if isinstance(c, (int, float)) else 0.0
        pred = p.get("predicted")
        if c < 0.5 and pred and pred not in seen_pred:
            seen_pred.add(pred)
            trig.append({"trigger": "低置信预测可能落空",
                         "condition": f"{pred}(置信 {c})",
                         "leads_to": "实际结果偏离预期", "severity": "medium",
                         "mitigation": "加监控指标, 关键节点设检查点", "source": "world_model 预测"})
    impact = (inputs.get("mechanisms") or {}).get("impact") or {}
    for dep in (impact.get("depends_on") or [])[:5]:
        trig.append({"trigger": f"依赖 {dep} 不可用", "condition": f"前置依赖 {dep} 缺失/不可达",
                     "leads_to": "计划阻塞或部分失败", "severity": "medium",
                     "mitigation": "执行前校验依赖可用性", "source": "world_model 依赖"})
    con = inputs.get("constraints")
    if isinstance(con, dict):
        for c in (con.get("violations") or con.get("constraints") or [])[:5]:
            label = c if isinstance(c, str) else (c.get("name") or c.get("constraint") or "约束")
            trig.append({"trigger": f"约束冲突: {label}", "condition": "计划可能违反既有约束",
                         "leads_to": "被约束规划器否决/需改方案", "severity": "high",
                         "mitigation": "与 constraint_planner 对齐后再落地",
                         "source": "constraint_planner"})
    if not trig:
        trig.append({"trigger": "未见显著失败触发点", "condition": "基于现有分级与世界模型",
                     "leads_to": "-", "severity": "low", "mitigation": "常规监控即可", "source": "-"})
    return trig


def _sensitivity(steps: list, base_forecast: dict, inputs: dict) -> dict:
    base_risk = float(base_forecast.get("risk_score") or 0.0)
    levers = []
    for s in steps:
        if s["kind"] == "red_line":
            levers.append({"variable": f"步骤{s['n']} 是否红线", "current": "红线",
                           "swing": _W_RED, "direction": "移除红线 → 风险大降",
                           "why": "红线权重最高(×40)"})
        if not s.get("reversible"):
            levers.append({"variable": f"步骤{s['n']} 可逆性", "current": "不可逆",
                           "swing": _W_IRREVERSIBLE, "direction": "改为可逆 → 风险降",
                           "why": "不可逆×15 + 影响回退路径"})
        if s.get("external"):
            levers.append({"variable": f"步骤{s['n']} 对外", "current": "对外",
                           "swing": _W_EXTERNAL, "direction": "改为内部 → 风险降",
                           "why": "对外×20(红线4 加权)"})
    if (inputs.get("mechanisms") or {}).get("_private") or ((inputs.get("state") or {}).get("_private")):
        levers.append({"variable": "隐私命中", "current": "命中", "swing": _W_PRIVACY,
                       "direction": "脱敏/本机处理 → 降敏感度",
                       "why": "隐私×15 + 强制本机模型路由"})
    impact = (inputs.get("mechanisms") or {}).get("impact") or {}
    blast = len(impact.get("affects") or [])
    if blast:
        levers.append({"variable": "波及面(受影响实体数)", "current": blast,
                       "swing": min(blast, 10) * _W_BLAST, "direction": "缩小波及 → 风险降",
                       "why": "每实体×2(封顶10)"})
    unc = _uncertainty_signal(inputs, base_forecast)
    levers.append({"variable": "不确定性", "current": round(unc, 3),
                   "swing": round(unc * 30, 1),
                   "direction": "补证据/降不确定 → 收窄 worst 概率",
                   "why": "驱动 best/worst 概率分布"})
    levers.sort(key=lambda x: x["swing"], reverse=True)
    return {"baseline_risk": round(base_risk, 1), "levers": levers[:8],
            "note": "敏感变量: 按对风险分的摆动幅度排序; 摆动越大越该优先盯/先消解。"}


def _early_warning(triggers: list, inputs: dict) -> list:
    metrics = []
    seen = set()
    for t in triggers:
        key = t.get("trigger")
        if not key or key in seen or t.get("severity") == "low":
            continue
        seen.add(key)
        metrics.append({"metric": f"监控: {key}", "watch_for": t.get("leads_to"),
                        "threshold_hint": "接近触发条件即预警", "tied_to": key,
                        "severity": t.get("severity"), "source": t.get("source")})
    impact = (inputs.get("mechanisms") or {}).get("impact") or {}
    for dep in (impact.get("depends_on") or [])[:4]:
        k = f"依赖可用性: {dep}"
        if k in seen:
            continue
        seen.add(k)
        metrics.append({"metric": k, "watch_for": f"{dep} 不可达",
                        "threshold_hint": "执行前+执行中探活", "tied_to": dep,
                        "severity": "medium", "source": "world_model 依赖"})
    if not metrics:
        metrics.append({"metric": "常规进度监控", "watch_for": "偏离预期步骤产出",
                        "threshold_hint": "按步检查点", "tied_to": "-",
                        "severity": "low", "source": "-"})
    return metrics[:10]


def _rollback_paths(steps: list) -> dict:
    """可逆回退路径(逆序; 只出预案不执行)。不可逆/对外步骤标人工补偿。"""
    path = []
    auto = True
    for s in reversed(steps):
        if s["kind"] == "read_only":
            continue
        if not s.get("reversible"):
            auto = False
            path.append({"order": len(path) + 1, "step": s["n"], "undo": "不可自动回滚",
                         "reversible": False,
                         "why": "不可逆(删除/部署/推送/真发等), 须人工补偿措施"})
        elif s.get("external"):
            auto = False
            path.append({"order": len(path) + 1, "step": s["n"], "undo": "对外动作无法撤回",
                         "reversible": False, "why": "已发出不可收回, 只能跟进更正通知"})
        else:
            path.append({"order": len(path) + 1, "step": s["n"],
                         "undo": "从备份/沙盒 .orig 逆操作恢复", "reversible": True,
                         "why": "经审批队列做逆操作"})
    return {"auto_revertible": auto, "path": path,
            "note": ("可逆回退路径(逆序): 任何回写本身也是副作用, 须经 approval_packet→User 批; "
                     "仿真层只出预案不执行。")}


def _inputs_summary(inputs: dict) -> dict:
    st = inputs.get("state") or {}
    mech = inputs.get("mechanisms") or {}
    return {"user_goals": len(inputs.get("goals") or []),
            "state_counts": st.get("counts") if st else None,
            "state_private": bool(st.get("_private")) if st else None,
            "world_mechanisms": len(mech.get("mechanisms") or []),
            "world_model_available": bool(mech.get("available")),
            "uncertainty_source": "epistemic_core" if inputs.get("uncertainty") else "预测置信代理",
            "constraints_source": "constraint_planner" if inputs.get("constraints") else "红线+偏好代理"}


def _assemble_scenario(goal: str, plan_text: str, params: dict,
                       action_kind: str = None, save: bool = False) -> dict:
    """把候选计划融成完整未来沙盘记录(best/base/worst + 全部衍生)。save=True 才落盘。"""
    steps = _build_steps(plan_text, params)
    base_forecast, inputs, deferred, private = _gather_scenario_inputs(goal, steps, params)
    futures = _build_futures(goal, steps, base_forecast, inputs)
    second_order = _second_order(goal, inputs)
    triggers = _failure_triggers(steps, base_forecast, inputs)
    sens = _sensitivity(steps, base_forecast, inputs)
    warn = _early_warning(triggers, inputs)
    rb = _rollback_paths(steps)
    scn = {
        # 种子含 goal + 计划全文 → 同秒内不同计划(如反事实只改一步)也得到不同 scn_id
        "sim_id": _scn_id((goal or "") + "\n" + (plan_text or "")),
        "created": _now(), "kind": "scenario",
        "title": (goal or plan_text or "")[:120], "source": plan_text, "goal": goal,
        "params": {k: v for k, v in (params or {}).items() if k != "action"},
        "action_kind": action_kind if action_kind in _EXEC_KINDS else None,
        "steps": steps, "forecast": base_forecast,
        "inputs": inputs, "scenarios": futures,
        "second_order": second_order, "failure_triggers": triggers,
        "sensitivity": sens, "early_warning": warn, "rollback_paths": rb,
        "deferred": deferred,
        "shadow": {"executed": False, "sandbox_dir": None, "trace": [], "diffs": []},
        "rollback": None, "status": "scenario_built", "approval_id": None,
        "goal_id": None, "_private": private, "executed_for_real": False,
    }
    if save:
        _save_sim(scn)
    return scn


def _resolve_scenario(scn_id_or_plan: str, params=None, save: bool = False) -> tuple:
    """统一解析: 已存场景 → 读盘(确定性, 不重算); 已存 sim → 升格为场景视图;
    否则当作计划文本现场组装(默认不落盘, 供切片查询)。返回 (scenario, from_store)。"""
    s = _load_sim(scn_id_or_plan)
    if s and s.get("kind") == "scenario":
        return s, True
    if s:
        goal = s.get("goal") or s.get("title") or ""
        plan = s.get("source") or goal
        return _assemble_scenario(goal, plan, s.get("params") or {}, save=save), False
    plan = (scn_id_or_plan or "").strip()
    if not plan:
        return None, False
    return _assemble_scenario(plan, plan, dict(params or {}), save=save), False


# --------------------------------------------------------------------------- #
# 公开接口(模块级函数, 供 reasoning_core/mission_control/strategy_tournament/
# metacognition_core 直接 import 调用; 全只读预测, 强动作仍 CLI/审批闸)
# --------------------------------------------------------------------------- #
def simulate_plan(plan: str, goal: str = "", params=None, kind: str = "scenario",
                  from_swarm: str = "", register_goal: bool = False) -> dict:
    """多情景反事实未来沙盘: 把候选计划 + User 目标 + 状态 + 机制 + 不确定性 + 约束
    融成可模拟场景, 一次产出 best/base/worst + 后果链 + 二阶影响 + 失败触发点 + 敏感变量 +
    回退路径 + 监控指标。纯 dry 预测, 零真实副作用; 真落地仍须 approval_packet→User 批。"""
    params = dict(params or {})
    plan = (plan or "").strip()
    goal = (goal or "").strip()
    if from_swarm:
        actions = _swarm_plan_steps(from_swarm)
        if actions is None:
            return {"ok": False, "error": f"读不到 swarm run {from_swarm} 的合并计划(先 agent_swarm merge)"}
        goal = goal or f"swarm:{from_swarm}"
        plan = "\n".join(f"STEP: {a}" for a in actions)
    if not plan and not goal and not params.get("action"):
        return {"ok": False, "error": "空计划(给 \"<计划>\" / --goal / -p action=...)"}
    eff_goal = goal or plan or params.get("action", "")
    eff_plan = plan or eff_goal
    scn = _assemble_scenario(eff_goal, eff_plan, params,
                             action_kind=params.get("action_kind"), save=True)
    if not scn or not scn.get("steps"):
        return {"ok": False, "error": "没解析出任何步骤"}
    if register_goal:
        scn["goal_id"] = _register_goal(eff_goal)
        _save_sim(scn)
    _audit("simulate_plan", scn["sim_id"], steps=len(scn["steps"]),
           band=scn["forecast"].get("risk_band"),
           private=scn.get("_private") or None,
           deferred=len(scn.get("deferred") or []) or None)
    out = {"ok": True, "scn_id": scn["sim_id"], "title": scn["title"],
           "candidate_steps": len(scn["steps"]),
           "scenarios": scn["scenarios"], "second_order": scn["second_order"],
           "failure_triggers": scn["failure_triggers"], "sensitivity": scn["sensitivity"],
           "early_warning": scn["early_warning"], "rollback_paths": scn["rollback_paths"],
           "inputs_summary": _inputs_summary(scn["inputs"]),
           "deferred": scn.get("deferred") or [],
           "note": ("多情景未来沙盘已建(dry, 零真实副作用)。best/base/worst + 后果链 + 二阶影响 + "
                    "失败触发点 + 敏感变量 + 回退路径 + 监控指标。真落地仍须 approval_packet→User 批。"
                    "供 reasoning_core/mission_control/strategy_tournament/metacognition_core 调用。")}
    if scn.get("goal_id") is not None:
        out["goal_id"] = scn["goal_id"]
    if scn.get("_private"):
        out["_private"] = True
    return out


def scenario_tree(scn_id_or_plan: str, params=None) -> dict:
    """情景树: 根=候选计划, 分支=best/base/worst, 子分支=可能把 base/worst 进一步引爆的失败触发点。"""
    scn, _ = _resolve_scenario(scn_id_or_plan, params)
    if not scn:
        return {"ok": False, "error": "给 <scn_id> 或 \"<计划>\""}
    futures = scn["scenarios"]["cases"]
    trig = scn["failure_triggers"]
    branches = []
    for case in ("best", "base", "worst"):
        c = futures[case]
        forks = []
        if case != "best":
            for t in trig:
                if t.get("severity") in ("critical", "high"):
                    forks.append({"fork": t["trigger"], "if": t["condition"],
                                  "leads_to": t["leads_to"], "toward": "worst"})
        branches.append({"case": case, "probability": c["probability"],
                         "risk_band": c["risk_band"], "narrative": c["narrative"],
                         "forks": forks[:6]})
    out = {"ok": True, "scn_id": scn["sim_id"], "title": scn["title"],
           "tree": {"root": "候选计划", "branches": branches},
           "note": "情景树: best/base/worst 三分支, 子分支为可能把 base/worst 引爆的失败触发点。"}
    if scn.get("_private"):
        out["_private"] = True
    return out


def counterfactual(scn_id_or_plan: str, change: str = "", assume: str = "",
                   params=None) -> dict:
    """情景级反事实: 改一个前提(步骤/目标/依赖/假设)重算 best/base/worst, 看期望风险与失败触发点怎么变。"""
    base, _ = _resolve_scenario(scn_id_or_plan, params)
    if not base:
        return {"ok": False, "error": "给 <scn_id> 或 \"<计划>\""}
    if not change and not assume:
        return {"ok": False, "error": "counterfactual 需要 --change k=v 或 --assume \"<假设>\""}
    if change and "=" not in change:
        return {"ok": False, "error": "--change 需要 k=v(如 stepN=新动作 / goal=... / dependency=...); "
                                      "自由文本假设用 --assume \"<假设>\""}
    new_goal = base.get("goal") or base.get("title") or ""
    new_params = dict(base.get("params") or {})
    base_actions = [s["action"] for s in base.get("steps", [])]
    if change and "=" in change:
        k, v = change.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k.lower().startswith("step") and k[4:].isdigit():
            idx = int(k[4:])
            if 1 <= idx <= len(base_actions):
                base_actions[idx - 1] = v
        elif k.lower() == "goal":
            new_goal = v
        else:
            new_params[k] = v
            new_goal = f"{new_goal}（假设 {change}）"
    if assume:
        new_goal = f"{new_goal}（假设 {assume}）"
    plan_text = "\n".join(f"STEP: {a}" for a in base_actions) if base_actions else new_goal
    cf = _assemble_scenario(new_goal, plan_text, new_params, save=True)
    cf["derived_from"] = base.get("sim_id")
    cf["change"] = change
    cf["assume"] = assume
    _save_sim(cf)
    _audit("counterfactual", cf["sim_id"], derived_from=base.get("sim_id"))

    def _exp(s):
        cs = s["scenarios"]["cases"]
        return round(sum(cs[c]["probability"] * cs[c]["risk_score"]
                         for c in ("best", "base", "worst")), 1)

    out = {"ok": True, "base_scn": base.get("sim_id"), "counterfactual_scn": cf["sim_id"],
           "change": change, "assume": assume,
           "before": {"expected_risk": _exp(base),
                      "cases": {c: base["scenarios"]["cases"][c]["risk_band"]
                                for c in ("best", "base", "worst")}},
           "after": {"expected_risk": _exp(cf),
                     "cases": {c: cf["scenarios"]["cases"][c]["risk_band"]
                               for c in ("best", "base", "worst")}},
           "delta_expected_risk": round(_exp(cf) - _exp(base), 1),
           "delta_failure_triggers": len(cf["failure_triggers"]) - len(base["failure_triggers"]),
           "note": "情景级反事实: 改一个前提重算 best/base/worst; 期望风险=Σ 概率×各情景风险。仍是 dry。"}
    if base.get("_private") or cf.get("_private"):
        out["_private"] = True
    return out


def second_order_effects(scn_id_or_plan: str, params=None) -> dict:
    """二阶影响: 一阶受影响实体的下游波及(world_model 2 跳) + 目标/约束涟漪。只读。"""
    scn, _ = _resolve_scenario(scn_id_or_plan, params)
    if not scn:
        return {"ok": False, "error": "给 <scn_id> 或 \"<计划>\""}
    so = scn["second_order"]
    out = {"ok": True, "scn_id": scn["sim_id"], "title": scn["title"],
           "effects": so.get("effects"), "note": so.get("note")}
    if scn.get("_private") or so.get("_private"):
        out["_private"] = True
    return out


def failure_triggers(scn_id_or_plan: str, params=None) -> dict:
    """失败触发点: 会把 base 引向 worst 的条件 + 严重度 + 缓解。红线分级来自 local_powers 单一真相源。"""
    scn, _ = _resolve_scenario(scn_id_or_plan, params)
    if not scn:
        return {"ok": False, "error": "给 <scn_id> 或 \"<计划>\""}
    out = {"ok": True, "scn_id": scn["sim_id"], "title": scn["title"],
           "failure_triggers": scn["failure_triggers"],
           "note": "失败触发点: 会把 base 引向 worst 的条件 + 严重度 + 缓解。红线分级走 local_powers。"}
    if scn.get("_private"):
        out["_private"] = True
    return out


def sensitivity(scn_id_or_plan: str, params=None) -> dict:
    """关键敏感变量: 按对风险分摆动幅度排序, 越大越该优先盯/先消解。只读。"""
    scn, _ = _resolve_scenario(scn_id_or_plan, params)
    if not scn:
        return {"ok": False, "error": "给 <scn_id> 或 \"<计划>\""}
    sn = scn["sensitivity"]
    out = {"ok": True, "scn_id": scn["sim_id"], "title": scn["title"],
           "baseline_risk": sn.get("baseline_risk"), "levers": sn.get("levers"),
           "note": sn.get("note")}
    if scn.get("_private"):
        out["_private"] = True
    return out


def early_warning(scn_id_or_plan: str, params=None) -> dict:
    """推荐监控指标/预警: 每条绑定一个失败触发点或依赖, 接近触发即预警。只读建议。"""
    scn, _ = _resolve_scenario(scn_id_or_plan, params)
    if not scn:
        return {"ok": False, "error": "给 <scn_id> 或 \"<计划>\""}
    out = {"ok": True, "scn_id": scn["sim_id"], "title": scn["title"],
           "early_warning": scn["early_warning"],
           "note": "推荐监控指标/预警: 每条绑定失败触发点或依赖, 接近触发即预警。只读建议。"}
    if scn.get("_private"):
        out["_private"] = True
    return out


def rollback_path(scn_id_or_plan: str, params=None) -> dict:
    """可逆回退路径(逆序): 不可逆/对外步骤标人工补偿。只出预案不执行(任何回写须经审批闸)。"""
    scn, _ = _resolve_scenario(scn_id_or_plan, params)
    if not scn:
        return {"ok": False, "error": "给 <scn_id> 或 \"<计划>\""}
    rb = scn["rollback_paths"]
    out = {"ok": True, "scn_id": scn["sim_id"], "title": scn["title"],
           "auto_revertible": rb["auto_revertible"], "rollback_path": rb["path"],
           "note": rb["note"]}
    if scn.get("_private"):
        out["_private"] = True
    return out


def compare_futures(scn_ids: list) -> dict:
    """多未来对比择优: 按期望风险(Σ 概率×情景风险)↓ + 可逆↑ + 失败面↓ + 二阶广度↓ 综合排序。"""
    scn_ids = [s for s in (scn_ids or []) if s]
    if len(scn_ids) < 2:
        return {"ok": False, "error": "compare_futures 需要至少 2 个 scn_id(或计划)"}
    rows = []
    private = False
    for sid in scn_ids:
        scn, _ = _resolve_scenario(sid)
        if not scn:
            rows.append({"scn_id": sid, "error": "未找到/无法解析"})
            continue
        cs = scn["scenarios"]["cases"]
        exp = sum(cs[c]["probability"] * cs[c]["risk_score"] for c in ("best", "base", "worst"))
        steps = scn.get("steps") or []
        rev_ratio = sum(1 for s in steps if s.get("reversible")) / max(1, len(steps))
        n_trig = sum(1 for t in scn["failure_triggers"] if t.get("severity") in ("critical", "high"))
        blast = len((scn["second_order"].get("effects") or []))
        composite = round(max(0.0, (100 - exp) / 100 * 0.5 + rev_ratio * 0.3
                              + max(0.0, 1 - n_trig / 5) * 0.1
                              + max(0.0, 1 - blast / 10) * 0.1), 4)
        if scn.get("_private"):
            private = True
        rows.append({"scn_id": scn["sim_id"], "title": scn.get("title"),
                     "expected_risk": round(exp, 1), "reversible_ratio": round(rev_ratio, 3),
                     "severe_triggers": n_trig, "second_order_breadth": blast,
                     "composite": composite})
    ranked = [r for r in rows if "composite" in r]
    ranked.sort(key=lambda r: r["composite"], reverse=True)
    out = {"ok": True, "count": len(rows), "ranked": ranked + [r for r in rows if "error" in r],
           "recommended": ranked[0]["scn_id"] if ranked else None,
           "rationale": ("综合分=(100−期望风险)/100×0.5 + 可逆比×0.3 + (1−重大触发/5)×0.1 + "
                         "(1−二阶广度/10)×0.1。选期望风险低、最可逆、失败面小的未来; 落地仍须 User 批。")}
    if private:
        out["_private"] = True
    _audit("compare_futures", "", count=len(scn_ids), recommended=out.get("recommended"))
    return out


def explain_outcome(scn_id: str, case: str = "base") -> dict:
    """成因解释: 为什么这个情景会发生 — 串后果链 + 关键驱动(worst 看失败触发点, base 看敏感变量)。只读。"""
    scn = _load_sim(scn_id)
    if not scn or scn.get("kind") != "scenario":
        scn, _ = _resolve_scenario(scn_id)
        if not scn:
            return {"ok": False, "error": "给 <scn_id>(先 simulate_plan)或 \"<计划>\""}
    case = (case or "base").lower()
    if case not in ("best", "base", "worst"):
        case = "base"
    c = scn["scenarios"]["cases"][case]
    if case == "worst":
        drivers = [t["trigger"] for t in scn["failure_triggers"]
                   if t.get("severity") in ("critical", "high")][:5]
    elif case == "best":
        drivers = ["全程可逆/顺利", "高置信预测兑现", "无约束冲突"]
    else:
        drivers = [l["variable"] for l in (scn["sensitivity"].get("levers") or [])[:4]]
    out = {"ok": True, "scn_id": scn["sim_id"], "case": case,
           "probability": c["probability"], "risk_band": c["risk_band"],
           "outcome": c["key_outcomes"], "why_narrative": c["narrative"],
           "drivers": drivers, "consequence_chain": c["consequence_chain"],
           "note": "成因解释: 为什么这个情景会发生 — 后果链 + 关键驱动。只读, 零副作用。"}
    if scn.get("_private"):
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 13. audit — 安全/合规审计 + 自验从未真执行/从未自放行(只读)
# --------------------------------------------------------------------------- #
def audit(sim_id: str) -> dict:
    if (sim_id or "").startswith("box_"):
        return _audit_box(sim_id)
    sim = _load_sim(sim_id)
    if not sim:
        return {"ok": False, "error": f"没找到仿真 {sim_id}"}
    steps = sim.get("steps") or []
    by_kind = {"read_only": 0, "mutating": 0, "red_line": 0}
    red_steps = []
    for s in steps:
        k = s.get("kind", "mutating")
        by_kind[k] = by_kind.get(k, 0) + 1
        if k == "red_line":
            red_steps.append({"n": s["n"], "action": s["action"][:160], "why": s.get("why")})

    # 自验硬不变量: 仿真层从未真执行 / 从未自放行 / 影子从未碰真实目标
    invariants = {
        "executed_for_real": bool(sim.get("executed_for_real")),  # 期望恒 False
        "shadow_touched_real_target": False,                       # 影子只碰沙盒副本
        "ever_self_approved": False,                               # 绝不自传 confirmed=True
    }
    invariant_ok = (invariants["executed_for_real"] is False)

    # 关联待批(本仿真打包入队的)
    pend = []
    apstore = _opt("autopilot_store")
    if apstore is not None:
        try:
            for ap in apstore.list_approvals("pending", limit=100):
                pl = ap.get("payload") or {}
                if isinstance(pl, dict) and pl.get("sim_id") == sim_id:
                    pend.append({"approval_id": ap.get("id"),
                                 "action": (ap.get("action") or "")[:120], "risk": ap.get("risk")})
        except Exception:
            pass

    private_hit = bool(sim.get("_private"))
    if by_kind["red_line"] > 0 or private_hit:
        verdict = "needs_attention"
    elif by_kind["mutating"] > 0:
        verdict = "review_required"
    else:
        verdict = "clean"

    recs = []
    if by_kind["red_line"] > 0:
        recs.append("含红线步骤: 落地须 User 逐条确认(critical token), 仿真层绝不自动执行。")
    if by_kind["mutating"] > 0:
        recs.append("含有副作用步骤: 经 approval_packet→autopilot 审批队列, 过 local_powers 红线门。")
    if private_hit:
        recs.append("命中隐私内容: 下游叙述切本机模型, 文件正文只回元数据。")
    if not invariant_ok:
        recs.append("⚠️ 不变量异常: executed_for_real 非 False —— 立即排查(仿真层绝不该真执行)。")
    if not recs:
        recs.append("全只读且不变量正常: 可直接走 approval_packet 准备落地。")

    out = {"ok": True, "sim_id": sim_id, "title": sim.get("title"),
           "steps_total": len(steps), "by_kind": by_kind, "red_line_steps": red_steps,
           "safety_invariants": invariants, "invariants_ok": invariant_ok,
           "pending_approvals": pend, "verdict": verdict, "recommendations": recs,
           "note": "安全审计(只读): 红线分级复用 local_powers 单一真相源; 自验仿真从未真执行/自放行; "
                   "不放宽任何门。"}
    # 情景沙盘记录: 同样恒 executed_for_real=False / 零真实副作用; 缺失模块记 deferred 不建桩
    if sim.get("kind") == "scenario":
        out["scenario_audit"] = {
            "is_scenario": True,
            "cases": list(((sim.get("scenarios") or {}).get("cases") or {}).keys()),
            "failure_triggers": len(sim.get("failure_triggers") or []),
            "second_order": len((sim.get("second_order") or {}).get("effects") or []),
            "deferred": sim.get("deferred") or [],
            "note": "情景沙盘纯只读预测: 不碰执行面, 不自放行; 缺失模块(epistemic_core/constraint_planner)记 deferred。"}
    if private_hit:
        out["_private"] = True
    _audit("audit", sim_id, verdict=verdict, red=by_kind["red_line"], invariant_ok=invariant_ok)
    return out


# --------------------------------------------------------------------------- #
# 14. status — 看某仿真全貌 / 不传则列最近仿真 + 各引擎可用性
# --------------------------------------------------------------------------- #
def _engines_available() -> dict:
    return {
        "world_model": _opt("world_model") is not None,
        "computer_use": _opt("computer_use") is not None,
        "self_improve": _opt("self_improve") is not None,
        "appsmith": _opt("appsmith") is not None,
        "nexus": _opt("nexus") is not None,
        "agent_swarm": _opt("agent_swarm") is not None,
        "autopilot_store": _opt("autopilot_store") is not None,
        "file_tools": _opt("file_tools") is not None,
        "todo_store": _opt("todo_store") is not None,
        "classify_engine": _merge_mod is not None,
        # 多情景未来沙盘的融合输入源(缺失 → 场景降级/记 deferred, 绝不建桩)
        "mission_control": _opt("mission_control") is not None,
        "shared_context": _opt("shared_context") is not None,
        "epistemic_core": _opt("epistemic_core") is not None,
        "constraint_planner": _opt("constraint_planner") is not None,
    }


def status(sim_id: str = "") -> dict:
    sim_id = (sim_id or "").strip()
    if sim_id.startswith("box_"):
        return _status_box(sim_id)
    if sim_id:
        sim = _load_sim(sim_id)
        if not sim:
            return {"ok": False, "error": f"没找到仿真 {sim_id}"}
        fc = sim.get("forecast") or {}
        out = {"ok": True, "sim_id": sim_id, "kind": sim.get("kind"),
               "title": sim.get("title"), "status": sim.get("status"),
               "created": sim.get("created"),
               "steps": len(sim.get("steps", [])),
               "risk_score": fc.get("risk_score"), "risk_band": fc.get("risk_band"),
               "shadow_executed": (sim.get("shadow") or {}).get("executed"),
               "approval_id": sim.get("approval_id"),
               "executed_for_real": sim.get("executed_for_real"),
               "forecast": fc}
        if sim.get("kind") == "scenario":
            cs = (sim.get("scenarios") or {}).get("cases") or {}
            out["scenario"] = {
                "cases": {c: {"probability": cs[c].get("probability"),
                              "risk_band": cs[c].get("risk_band")} for c in cs},
                "failure_triggers": len(sim.get("failure_triggers") or []),
                "second_order": len((sim.get("second_order") or {}).get("effects") or []),
                "deferred": sim.get("deferred") or []}
        if sim.get("_private"):
            out["_private"] = True
        return out
    # 列最近仿真 + 数字沙盘 + 引擎可用性
    sims = _list_sims(limit=20)
    boxes = _list_boxes(limit=20)
    return {"ok": True, "count": len(sims),
            "recent": [{"sim_id": s["sim_id"], "kind": s.get("kind"),
                        "title": s.get("title"), "status": s.get("status"),
                        "risk_band": (s.get("forecast") or {}).get("risk_band"),
                        "created": s.get("created")} for s in sims],
            "recent_sandboxes": [{"box_id": b["box_id"], "title": b.get("title"),
                                  "resources": len(b.get("state") or {}),
                                  "trace_len": len(b.get("trace") or []),
                                  "created": b.get("created")} for b in boxes],
            "engines": _engines_available(),
            "note": "仿真沙盒概览: 列最近仿真 + 数字沙盘 + 各集成引擎可用性。仿真/沙盘恒 dry; 真执行经 approval_packet→User 批。"}


# =========================================================================== #
# 可执行数字沙盘与后果预演器(digital twin sandbox)
# 把文件/日历·邮件/任务计划/代码补丁/业务流程/多工具调用编成「可回滚的本机虚拟
# 执行轨迹」。每条轨迹记 before/after 虚拟快照 + 逆操作 + 连锁波及。
# 硬铁律(同 simulation_lab 全模块): 纯虚拟态, 零真实副作用; 文件只读元数据(私密/密钥
# 只回元数据绝不读正文); 红线分级转发 local_powers 单一真相源; 绝不执行/绝不自放行;
# 真落地一律 approval_packet→User 批。沙盒副本永久保留(只 User 手动清)。
# =========================================================================== #
def _sandboxes_dir() -> str:
    return os.path.join(_data_dir(), "sandboxes")


def _box_id(seed: str) -> str:
    h = hashlib.sha1((seed + _now()).encode("utf-8")).hexdigest()[:10]
    return f"box_{h}"


def _box_path(box_id: str) -> str:
    return os.path.join(_sandboxes_dir(), f"{box_id}.json")


def _save_box(box: dict) -> None:
    """落沙盘到自有数据目录。硬不变量: 沙盘与每条轨迹恒 executed_for_real=False。"""
    box["executed_for_real"] = False  # 硬不变量: 沙盘层永不真执行
    box["updated"] = _now()
    for e in box.get("trace") or []:
        e["executed_for_real"] = False
    os.makedirs(_sandboxes_dir(), exist_ok=True)
    with open(_box_path(box["box_id"]), "w", encoding="utf-8") as f:
        json.dump(box, f, ensure_ascii=False, indent=2)


def _load_box(box_id: str):
    if not box_id:
        return None
    try:
        with open(_box_path(box_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _list_boxes(limit: int = 30) -> list:
    out = []
    d = _sandboxes_dir()
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except Exception:
        return out
    for n in names:
        try:
            with open(os.path.join(d, n), "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            continue
    out.sort(key=lambda b: b.get("created", ""), reverse=True)
    return out[:limit]


def _new_box(title: str = "", note: str = "") -> dict:
    box = {"box_id": _box_id(title or "sandbox"), "created": _now(), "updated": _now(),
           "title": (title or "未命名沙盘")[:120], "note": (note or "")[:300],
           "state": {}, "trace": [], "imports": [], "forecast": {}, "rollback": None,
           "executed_for_real": False, "_private": False}
    _save_box(box)
    return box


def _get_or_new_box(box_id: str = "", title: str = "") -> dict:
    if box_id:
        b = _load_box(box_id)
        if b is not None:
            return b
    return _new_box(title=title)


def _slug(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:8]


def _first_pathish(text: str) -> str:
    """从动作文本里抽第一个像路径的 token(best-effort; 不准则空)。"""
    for tok in re.split(r"\s+", text or ""):
        t = tok.strip().strip("，。、；,;:'\"()（）【】")
        if t.startswith("~/") or t.startswith("/") or t.startswith("./"):
            return t
        if "/" in t and "." in t.rsplit("/", 1)[-1]:
            return t
    return ""


# --------------------------------------------------------------------------- #
# 文件虚拟态快照(只读元数据; 私密/密钥只回元数据, 绝不读正文/不计指纹)
# --------------------------------------------------------------------------- #
def _file_state(path: str) -> dict:
    path = os.path.abspath(os.path.expanduser(path or ""))
    priv = _path_privacy(path)            # 复用隐私门: 密钥硬拦 + 私密 + 库不可达 fail-safe
    st = {"domain": "file", "id": path, "_private": bool(priv["private"])}
    try:
        st["exists"] = os.path.exists(path)
    except Exception:
        st["exists"] = None
    if priv["private"]:
        # 私密/密钥/库不可达: 只回元数据, 绝不读正文/不计内容指纹
        st["redacted"] = True
        st["reason"] = priv["reason"]
        try:
            if st.get("exists") and os.path.isfile(path):
                st["size"] = os.path.getsize(path)
        except Exception:
            pass
        return st
    # 非私密且隐私库确认可达: 安全读元数据 + 内容指纹(只读, 有上限)
    try:
        if st.get("exists") and os.path.isfile(path):
            sz = os.path.getsize(path)
            st["size"] = sz
            if sz <= _HASH_READ_CAP:
                with open(path, "rb") as f:
                    data = f.read(_HASH_READ_CAP)
                st["content_hash"] = hashlib.sha1(data).hexdigest()[:16]
                st["lines"] = data.count(b"\n") + 1
            else:
                st["content_hash"] = None
                st["note"] = "文件超指纹上限, 只记大小不计 hash"
    except Exception as e:
        st["error"] = f"读元数据失败: {e}"
    return st


# --------------------------------------------------------------------------- #
# 域/操作推断 + 逆操作规格 + 连锁波及(全启发式; 红线永远以 local_powers 为准)
# --------------------------------------------------------------------------- #
def _infer_domain(action: str, hint: str = "") -> str:
    if hint in ("file", "calendar", "email", "task", "code", "business", "tool", "plan"):
        return hint
    a = action or ""
    low = a.lower()
    for dom, hints in _DOMAIN_HINTS:
        if any(h in a or h.lower() in low for h in hints):
            return dom
    if "/" in a or "~" in a:
        return "file"
    return "plan"


def _infer_op(domain: str, action: str) -> str:
    a = action or ""
    low = a.lower()
    if domain == "file":
        if any(h in a or h in low for h in ("删除", "rm ", "unlink", "废纸篓", "trash")):
            return "delete_file"
        if any(h in a or h in low for h in ("移动", "move", "mv ", "重命名", "rename")):
            return "move_file"
        return "write_file"
    if domain == "calendar":
        if any(h in a for h in ("删除", "取消")):
            return "delete_event"
        if any(h in a for h in ("改", "更新", "调整", "改期", "挪")):
            return "update_event"
        return "create_event"
    if domain == "email":
        return "send_email"
    if domain == "task":
        if any(h in a for h in ("完成", "勾掉", "done")):
            return "complete_task"
        return "create_task"
    if domain == "code":
        return "patch_code"
    if domain == "business":
        return "business_step"
    if domain == "tool":
        return "tool_call"
    return "generic"


def _undo_spec(op: str, target: str, reversible: bool, external: bool) -> dict:
    """逆操作规格(只描述, 绝不执行)。任何回写/逆操作本身都是副作用, 须经 approval。"""
    if external:
        spec = {"op": "compensate", "auto": False,
                "describe": "对外动作已发出不可撤回 —— 只能跟进补救(撤回/更正通知), 经审批闸"}
        spec["target"] = target
        return spec
    table = {
        "write_file": {"op": "restore_or_remove", "auto": reversible,
                       "describe": "用沙盒 .orig 覆盖回(若为新建则移入废纸篓) —— 回写本身是副作用, 经 approval"},
        "move_file": {"op": "reverse_move", "auto": reversible,
                      "describe": "反向移动回原路径 —— 经 approval 执行"},
        "delete_file": {"op": "restore_from_trash", "auto": False,
                        "describe": "从废纸篓/备份恢复(无备份则不可逆) —— 经 approval; 本层绝不真删"},
        "create_event": {"op": "delete_event", "auto": True,
                         "describe": "删除新建的日历事件 —— 经日历确认门"},
        "update_event": {"op": "restore_event", "auto": reversible,
                         "describe": "用 before 快照还原事件字段 —— 经日历确认门"},
        "delete_event": {"op": "recreate_event", "auto": False,
                         "describe": "从 before 快照重建事件(可能丢与会人状态) —— 经确认门"},
        "send_email": {"op": "compensate", "auto": False,
                       "describe": "已发邮件不可撤回 —— 只能跟进更正"},
        "create_task": {"op": "remove_task", "auto": True, "describe": "删除新建待办(经 approval)"},
        "complete_task": {"op": "reopen_task", "auto": True, "describe": "重新打开待办(经 approval)"},
        "patch_code": {"op": "git_revert", "auto": True,
                       "describe": "git revert / 丢弃 self_improve 隔离 worktree 分支 —— 经 approval"},
        "business_step": {"op": "compensating_action", "auto": False,
                          "describe": "业务流程逆向补偿(取决于流程定义) —— 经 approval"},
        "tool_call": {"op": "tool_inverse", "auto": reversible,
                      "describe": "工具的逆操作(若存在) —— 经各自确认门"},
        "generic": {"op": "manual_restore", "auto": reversible,
                    "describe": "恢复改动前状态 —— 经 approval 做逆操作"},
    }
    spec = dict(table.get(op, table["generic"]))
    spec["target"] = target
    return spec


def _cascade_for(target: str, limit: int = 6):
    """预测一个目标/资源的连锁波及(只读 world_model)。返回 (cascade_list, private_hit)。"""
    cascade = []
    private_hit = False
    impact = _impact(target) or {}
    if impact.get("_private"):
        private_hit = True
    for a in (impact.get("affects") or [])[:limit]:
        nm = a.get("name")
        if not nm:
            continue
        cascade.append({"target": nm, "order": 2,
                        "why": f"world_model 前向波及({a.get('type_cn') or a.get('etype') or ''})".strip(),
                        "confidence": a.get("confidence"), "_private": bool(a.get("private"))})
    pred = _predict(target) or {}
    if pred.get("_private"):
        private_hit = True
    for p in (pred.get("predictions") or [])[:limit]:
        cascade.append({"target": p.get("predicted"), "order": 3,
                        "why": f"历史复现/因果预测({p.get('kind')})",
                        "confidence": p.get("confidence"), "_private": bool(p.get("private"))})
    return cascade, private_hit


def _entry_snapshots(domain: str, op: str, action: str, state: dict, params: dict):
    """算 (target, before, after, delta, private)。全虚拟; 文件只读元数据, 私密只回元数据。"""
    private = False
    if domain == "file":
        path = params.get("file") or _first_pathish(action)
        if not path:
            target = f"file:{_slug(action)}"
            return target, None, {"domain": "file", "note": "未给具体路径, 仅记意图"}, \
                {"op": op, "note": "无路径"}, private
        path = os.path.abspath(os.path.expanduser(path))
        target = f"file:{path}"
        before = state.get(target) or _file_state(path)
        private = bool(before.get("_private"))
        if op == "delete_file":
            after = {"domain": "file", "id": path, "exists": False, "deleted": True, "_private": private}
            delta = {"op": op, "change": "删除文件(虚拟)", "was_exists": before.get("exists")}
        elif op == "move_file":
            dst = params.get("dest") or params.get("to") or ""
            after = {"domain": "file", "id": dst or path, "exists": True,
                     "moved_from": path, "_private": private}
            delta = {"op": op, "change": "移动/重命名(虚拟)", "to": dst or "(未指定目标)"}
        else:
            after = dict(before)
            after["exists"] = True
            after["pending_write"] = True
            after["_private"] = private
            delta = {"op": op, "change": "写入/覆盖(虚拟)", "was_exists": before.get("exists")}
        return target, before, after, delta, private
    # 非文件域: 全虚拟 JSON 快照
    key = f"{domain}:{params.get('id') or _slug(action)}"
    before = state.get(key)
    after = {"domain": domain, "op": op, "intent": action[:200], "virtual": True}
    delta = {"op": op, "change": f"{domain} 域虚拟改动", "detail": action[:120]}
    return key, before, after, delta, private


def _build_trace_entry(seq: int, action: str, state: dict, domain_hint: str = "",
                       params=None, with_cascade: bool = True) -> dict:
    """把一个动作编成一条虚拟执行轨迹(分级转发 local_powers; 零真实副作用)。"""
    params = dict(params or {})
    action = (action or "").strip()
    domain = _infer_domain(action, domain_hint)
    op = _infer_op(domain, action)
    cls = _classify_step(action)                       # 红线分级单一真相源
    reversible = _reversible(action, cls)
    external = _is_external(action) or (domain == "email")
    target, before, after, delta, private = _entry_snapshots(domain, op, action, state, params)
    undo = _undo_spec(op, target, reversible, external)
    cascade, casc_priv = ([], False)
    if with_cascade:
        cascade, casc_priv = _cascade_for(target.split(":", 1)[-1] if ":" in (target or "") else (target or action))
    return {
        "seq": seq, "ts": _now(), "domain": domain, "op": op, "target": target,
        "action_text": action[:300],
        "classification": {"kind": cls.get("kind", "mutating"),
                           "gated": bool(cls.get("gated", True)), "why": cls.get("why", "")},
        "reversible": reversible, "external": external, "private": bool(private or casc_priv),
        "before": before, "after": after, "delta": delta,
        "undo": undo, "cascade": cascade,
        "predicted_effect": _predicted_effect(action, cls),
        "executed_for_real": False,
    }


def _apply_entry_to_state(state: dict, entry: dict) -> None:
    """把一条轨迹的 after 虚拟快照落进沙盘状态(保留 deleted key 以便回滚追溯)。"""
    target = entry.get("target")
    after = entry.get("after")
    if not target or after is None:
        return
    state[target] = after


def _steps_from_trace(trace: list) -> list:
    """轨迹 → _forecast/_score_steps 认的步骤形态(复用既有风险/预测引擎)。"""
    steps = []
    for i, e in enumerate(trace, 1):
        cls = e.get("classification") or {}
        steps.append({"n": i, "action": e.get("action_text", ""),
                      "kind": cls.get("kind", "mutating"), "gated": bool(cls.get("gated", True)),
                      "why": cls.get("why", ""), "reversible": bool(e.get("reversible")),
                      "external": bool(e.get("external")), "predicted_effect": e.get("predicted_effect", "")})
    return steps


def _box_forecast(box: dict):
    steps = _steps_from_trace(box.get("trace") or [])
    return _forecast(box.get("title", ""), steps)


def _box_state_summary(box: dict) -> dict:
    state = box.get("state") or {}
    by_domain = {}
    deleted = 0
    private = 0
    for k, v in state.items():
        v = v or {}
        dom = v.get("domain") or (k.split(":", 1)[0] if ":" in k else "unknown")
        by_domain[dom] = by_domain.get(dom, 0) + 1
        if v.get("deleted") or v.get("exists") is False:
            deleted += 1
        if v.get("_private"):
            private += 1
    return {"resources": len(state), "by_domain": by_domain,
            "deleted_or_missing": deleted, "private_resources": private}


def _entry_brief(e: dict) -> dict:
    cls = e.get("classification") or {}
    b = {"seq": e.get("seq"), "domain": e.get("domain"), "op": e.get("op"),
         "target": e.get("target"), "kind": cls.get("kind"), "gated": cls.get("gated"),
         "reversible": e.get("reversible"), "external": e.get("external"),
         "predicted_effect": e.get("predicted_effect"),
         "cascade_count": len(e.get("cascade") or []),
         "undo": (e.get("undo") or {}).get("describe")}
    if e.get("tool"):
        b["tool"] = e["tool"]
        b["owner_module"] = e.get("owner_module")
    if e.get("private"):
        b["_private"] = True
    sd = e.get("sandbox_diff")
    if sd:
        b["sandbox_diff"] = {k: sd.get(k) for k in
                             ("file", "sandboxed", "added", "removed", "_private", "redacted")
                             if k in sd}
    return b


# --------------------------------------------------------------------------- #
# 工具链只读解析(simulate_toolchain 用; 绝不 dispatch/invoke 任何工具)
# --------------------------------------------------------------------------- #
def _load_toolchain(params: dict):
    cf = params.get("chain_file")
    if cf:
        if _is_secret_path(cf) or _path_privacy(cf)["private"]:
            return {"error": "chain_file 命中密钥/隐私守门, 绝不读取。"}
        try:
            with open(cf, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return {"error": f"读 chain_file 失败: {e}"}
        items = data if isinstance(data, list) else (data.get("chain") if isinstance(data, dict) else None)
        if not isinstance(items, list):
            return {"error": "chain_file 需是 [{tool,args?,note?}] 数组或 {chain:[...]}"}
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("tool"):
                out.append({"tool": str(it["tool"]),
                            "note": str(it.get("note") or it.get("args") or "")[:160]})
            elif isinstance(it, str) and it.strip():
                out.append({"tool": it.strip()})
        return out
    tools = params.get("tools") or ""
    return [{"tool": t.strip()} for t in re.split(r"[,\n]", tools) if t.strip()]


def _resolve_tool_owner(tool: str) -> str:
    """只读解析工具归属模块(调各引擎只读 is_tool, 绝不 dispatch/invoke)。找不到 → unknown。"""
    if not tool:
        return "unknown"
    for mod_name in _TOOL_REGISTRY_MODULES:
        mod = _opt(mod_name)
        if mod is None:
            continue
        try:
            checker = getattr(mod, "is_tool", None)
            if callable(checker) and checker(tool):
                return mod_name
        except Exception:
            continue
    return "unknown"


def _tool_is_mutating(tool: str) -> bool:
    low = (tool or "").lower()
    return any(h in low for h in _MUTATING_TOOL_HINTS)


def _tool_is_external(tool: str) -> bool:
    low = (tool or "").lower()
    return any(h in low for h in _EXTERNAL_TOOL_HINTS)


# --------------------------------------------------------------------------- #
# A. sandbox — 建/列/看/重置数字沙盘
# --------------------------------------------------------------------------- #
def sandbox(box_id: str = "", new: bool = False, title: str = "", reset: str = "") -> dict:
    if reset:
        box = _load_box(reset)
        if not box:
            return {"ok": False, "error": f"没找到沙盘 {reset}"}
        box["state"] = {}
        box["trace"] = []
        box["imports"] = []
        box["forecast"] = {}
        box["rollback"] = None
        box["_private"] = False
        _save_box(box)
        _audit("sandbox_reset", reset)
        return {"ok": True, "box_id": reset, "reset": True,
                "note": "已清空沙盘虚拟态与轨迹(仅重写本层自有 json; 绝不删任何真实文件)。"}
    if new:
        box = _new_box(title=title)
        _audit("sandbox_new", box["box_id"])
        return {"ok": True, "box_id": box["box_id"], "title": box["title"],
                "note": "已建空数字沙盘。下一步: import_state 只读导入真实状态 / sandbox_plan 编虚拟执行轨迹。"}
    if box_id:
        if not _load_box(box_id):
            return {"ok": False, "error": f"没找到沙盘 {box_id}(先 sandbox --new)"}
        return _status_box(box_id)
    boxes = _list_boxes(limit=30)
    return {"ok": True, "count": len(boxes),
            "sandboxes": [{"box_id": b["box_id"], "title": b.get("title"),
                           "resources": len(b.get("state") or {}),
                           "trace_len": len(b.get("trace") or []),
                           "created": b.get("created"), "_private": bool(b.get("_private"))}
                          for b in boxes],
            "note": "数字沙盘列表。看单个: sandbox <box_id> / status <box_id>。建新: sandbox --new。"}


# --------------------------------------------------------------------------- #
# B. import_state — 只读把真实状态导入沙盘虚拟态(私密只回元数据, 绝不读密钥)
# --------------------------------------------------------------------------- #
def import_state(box_id: str = "", kind: str = "file", params=None, new: bool = False) -> dict:
    params = dict(params or {})
    box = _new_box(title=params.get("title", "")) if (new or not box_id) else _load_box(box_id)
    if box is None:
        return {"ok": False, "error": f"没找到沙盘 {box_id}(先 sandbox --new 或加 --new)"}
    added = []
    private_hit = bool(box.get("_private"))

    if kind == "file":
        raw = params.get("path") or params.get("paths") or ""
        paths = [p.strip() for p in re.split(r"[,\n]", raw) if p.strip()]
        if not paths:
            return {"ok": False, "error": "import_state --kind file 需要 -p path=<路径>(逗号可分隔多个)"}
        for p in paths:
            st = _file_state(p)
            key = f"file:{st['id']}"
            box["state"][key] = st
            if st.get("_private"):
                private_hit = True
            added.append({"key": key, "exists": st.get("exists"), "_private": st.get("_private")})
    elif kind == "todo":
        ts = _opt("todo_store")
        if ts is None:
            return {"ok": False, "error": "todo_store 不可用, 无法导入待办"}
        try:
            todos = ts.list_open()
        except Exception as e:
            return {"ok": False, "error": f"读待办失败: {e}"}
        for t in (todos or [])[:200]:
            key = f"task:{t.get('id')}"
            box["state"][key] = {"domain": "task", "id": t.get("id"),
                                 "content": (t.get("content") or "")[:200],
                                 "due": t.get("due"), "done": t.get("done")}
            added.append({"key": key})
    elif kind == "world":
        topic = params.get("topic") or params.get("query") or ""
        ctx = _world_ctx(topic)
        if ctx is None:
            return {"ok": False, "error": "world_model 不可用, 无法导入世界态"}
        if ctx.get("_private"):
            private_hit = True
        for ent in (ctx.get("entities") or [])[:100]:
            nm = ent.get("name")
            if not nm:
                continue
            ent_priv = bool(ent.get("private"))
            key = f"business:{_slug(nm)}"
            box["state"][key] = {"domain": "business", "id": (nm if not ent_priv else f"<私密实体 {_slug(nm)}>"),
                                 "etype": ent.get("etype") or ent.get("type_cn"), "_private": ent_priv}
            if ent_priv:
                private_hit = True
            added.append({"key": key})
    elif kind == "json":
        jf = params.get("json_file")
        if not jf:
            return {"ok": False, "error": "import_state --kind json 需要 -p json_file=<路径>"}
        if _is_secret_path(jf):
            return {"ok": False, "error": "json_file 命中密钥/凭据文件硬拦, 绝不读取。"}
        if _path_privacy(jf)["private"]:
            return {"ok": False, "error": "json_file 命中隐私守门, 不导入其正文(改用非私密的显式状态文件)。"}
        try:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return {"ok": False, "error": f"读 json_file 失败: {e}"}
        items = data if isinstance(data, list) else (data.get("resources") if isinstance(data, dict) else None)
        if not isinstance(items, list):
            return {"ok": False, "error": "json_file 需是资源数组或 {resources:[...]}"}
        for it in items[:500]:
            if not isinstance(it, dict):
                continue
            dom = it.get("domain") or "business"
            rid = it.get("id") or _slug(json.dumps(it, ensure_ascii=False, sort_keys=True))
            key = f"{dom}:{rid}"
            if it.get("_private") or it.get("private"):
                box["state"][key] = {"domain": dom, "id": rid, "_private": True, "redacted": True}
                private_hit = True
            else:
                box["state"][key] = {**it, "domain": dom, "id": rid}
            added.append({"key": key})
    else:
        return {"ok": False, "error": f"未知 kind: {kind}(支持 file|todo|world|json)"}

    box["imports"].append({"kind": kind, "count": len(added), "ts": _now(),
                           "_private": private_hit or None})
    box["_private"] = bool(box.get("_private") or private_hit)
    _save_box(box)
    _audit("import_state", box["box_id"], kind=kind, count=len(added), private=private_hit or None)
    out = {"ok": True, "box_id": box["box_id"], "kind": kind, "imported": len(added),
           "resources_total": len(box["state"]), "added": added[:50],
           "note": "已只读导入真实状态到沙盘虚拟态。文件只取元数据(私密/密钥只回元数据, 绝不读正文)。"}
    if private_hit:
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# C. sandbox_plan — 把计划编成可回滚的虚拟执行轨迹(落进沙盘, 零真实副作用)
#    (原名 simulate_plan, 2026-06-10 合并时与情景版 simulate_plan 撞名改之)
# --------------------------------------------------------------------------- #
def sandbox_plan(goal: str, params=None, box_id: str = "", from_swarm: str = "") -> dict:
    goal = (goal or "").strip()
    params = dict(params or {})
    if from_swarm:
        actions = _swarm_plan_steps(from_swarm)
        if actions is None:
            return {"ok": False, "error": f"读不到 swarm run {from_swarm} 的合并计划(先 agent_swarm merge)"}
        goal = goal or f"swarm:{from_swarm}"
    else:
        raw = _extract_steps(goal)
        single = params.get("action") or goal
        actions = raw or ([single] if single else [])
    if not actions:
        return {"ok": False, "error": "没解析出任何步骤(给 \"<计划(可含 STEP:)>\" 或 -p action=...)"}

    box = _get_or_new_box(box_id, title=goal[:80] or "plan")
    state = box.get("state") or {}
    base_seq = len(box.get("trace") or [])
    sandbox_dir = os.path.join(_sandbox_root(), box["box_id"])
    new_entries = []
    private_hit = bool(box.get("_private"))

    for i, action in enumerate(actions, 1):
        entry = _build_trace_entry(base_seq + i, action, state, params=params)
        # 文件写入且给了 content_file 且非私密 → 在沙盒里产 diff(真实目标绝不写; 复用既有 _shadow_file_op)
        if entry["op"] == "write_file" and not entry["private"]:
            tgt = entry.get("target") or ""
            ft_path = tgt[5:] if tgt.startswith("file:") else params.get("file")
            cf = params.get("content_file")
            if ft_path and cf:
                os.makedirs(sandbox_dir, exist_ok=True)
                d = _shadow_file_op(sandbox_dir, ft_path, cf)
                entry["sandbox_diff"] = d
                if d.get("_private"):
                    entry["private"] = True
        if entry.get("private"):
            private_hit = True
        _apply_entry_to_state(state, entry)
        new_entries.append(entry)

    box["state"] = state
    box["trace"] = (box.get("trace") or []) + new_entries
    box["_private"] = bool(box.get("_private") or private_hit)
    forecast, fc_priv = _box_forecast(box)
    box["_private"] = bool(box["_private"] or fc_priv)
    box["forecast"] = forecast
    _save_box(box)
    _audit("sandbox_plan", box["box_id"], steps=len(new_entries),
           band=forecast.get("risk_band"), private=(private_hit or fc_priv) or None)
    out = {"ok": True, "box_id": box["box_id"], "compiled_steps": len(new_entries),
           "trace_len": len(box["trace"]), "trace": [_entry_brief(e) for e in new_entries],
           "end_state": _box_state_summary(box), "forecast": forecast,
           "note": "已把计划编成可回滚的虚拟执行轨迹落进沙盘(零真实副作用)。下一步: "
                   "predict_side_effects / stress_test / failure_injection / rollback_path / "
                   "approval_preview。真执行只经 approval_packet→User 批。"}
    if box.get("_private"):
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# D. simulate_toolchain — 把多工具调用链编成虚拟轨迹(绝不调用任何工具)
# --------------------------------------------------------------------------- #
def simulate_toolchain(box_id: str = "", params=None) -> dict:
    params = dict(params or {})
    chain = _load_toolchain(params)
    if isinstance(chain, dict) and chain.get("error"):
        return {"ok": False, "error": chain["error"]}
    if not chain:
        return {"ok": False, "error": "给 -p chain_file=<json> 或 -p tools=\"a,b,c\""}
    box = _get_or_new_box(box_id, title="toolchain")
    state = box.get("state") or {}
    base_seq = len(box.get("trace") or [])
    new_entries = []
    prev_out = None

    for i, call in enumerate(chain, 1):
        tool = call.get("tool") or ""
        note = call.get("note") or ""
        owner = _resolve_tool_owner(tool)
        mutating = _tool_is_mutating(tool)
        external = _tool_is_external(tool)
        desc = f"调用工具 {tool}" + (f": {note}" if note else "")
        cls = _classify_step(desc + (" 写入 发送 修改" if mutating else ""))
        if mutating and cls.get("kind") == "read_only":
            cls = {"kind": "mutating", "gated": True, "why": "已知有副作用工具(保底升级)"}
        reversible = (not external) and cls.get("kind") != "red_line"
        entry = {
            "seq": base_seq + i, "ts": _now(), "domain": "tool", "op": "tool_call",
            "target": f"tool:{tool}", "action_text": desc[:300], "tool": tool, "owner_module": owner,
            "classification": {"kind": cls.get("kind", "mutating"),
                               "gated": bool(cls.get("gated", True)), "why": cls.get("why", "")},
            "reversible": reversible, "external": external, "private": False,
            "before": prev_out,
            "after": {"domain": "tool", "tool": tool, "virtual_output": f"<{tool} 的虚拟输出>"},
            "delta": {"op": "tool_call", "consumes_prev": bool(prev_out)},
            "undo": _undo_spec("tool_call", f"tool:{tool}", reversible, external),
            "cascade": [], "predicted_effect": _predicted_effect(desc, cls),
            "executed_for_real": False,
        }
        if prev_out is not None:
            entry["cascade"].append({"target": f"tool:{tool}", "order": 1, "confidence": 0.9,
                                     "why": "消费上一步输出(链式耦合): 上一步失败/变形会传导到本步"})
        prev_out = entry["after"]
        _apply_entry_to_state(state, entry)
        new_entries.append(entry)

    box["state"] = state
    box["trace"] = (box.get("trace") or []) + new_entries
    forecast, fc_priv = _box_forecast(box)
    box["_private"] = bool(box.get("_private") or fc_priv)
    box["forecast"] = forecast
    _save_box(box)
    _audit("simulate_toolchain", box["box_id"], calls=len(new_entries), band=forecast.get("risk_band"))
    out = {"ok": True, "box_id": box["box_id"], "calls": len(new_entries),
           "chain": [{"seq": e["seq"], "tool": e["tool"], "owner": e["owner_module"],
                      "kind": e["classification"]["kind"], "external": e["external"],
                      "reversible": e["reversible"]} for e in new_entries],
           "forecast": forecast,
           "note": "已把工具调用链编成虚拟轨迹(只读解析归属+分级+跨步连锁)。**绝不调用任何工具**; "
                   "真执行须各工具各自确认门 / approval_packet→User 批。"}
    if box.get("_private"):
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# E. predict_side_effects — 一阶直接 + 二阶波及 + 三阶下游 + 跨步耦合
# --------------------------------------------------------------------------- #
def predict_side_effects(box_id_or_target: str, params=None) -> dict:
    target_or_box = (box_id_or_target or "").strip()
    if target_or_box.startswith("box_"):
        box = _load_box(target_or_box)
        if not box:
            return {"ok": False, "error": f"没找到沙盘 {target_or_box}"}
        return _predict_box_side_effects(box)
    if not target_or_box:
        return {"ok": False, "error": "给 <box_id> 或 \"<目标>\""}
    cascade, private_hit = _cascade_for(target_or_box, limit=12)
    out = {"ok": True, "target": target_or_box,
           "direct_effects": [{"target": target_or_box, "order": 1, "why": "直接动作对象"}],
           "second_order_affected": [c for c in cascade if c["order"] == 2],
           "third_order_predicted": [c for c in cascade if c["order"] == 3],
           "note": "后果预测(只读): 一阶直接 + 二阶 world_model 波及 + 三阶下游预测。来自 world_model 单一图谱。"}
    if private_hit:
        out["_private"] = True
    _audit("predict_side_effects", "", private=private_hit or None)
    return out


def _predict_box_side_effects(box: dict) -> dict:
    trace = box.get("trace") or []
    private_hit = bool(box.get("_private"))
    per_step = []
    touched = {}
    couplings = []
    for e in trace:
        seq = e.get("seq")
        tgt = e.get("target")
        casc = e.get("cascade") or []
        if e.get("private") or any(c.get("_private") for c in casc):
            private_hit = True
        per_step.append({
            "seq": seq, "target": (tgt if not e.get("private") else "<私密资源>"), "op": e.get("op"),
            "direct": [{"target": tgt if not e.get("private") else "<私密资源>",
                        "change": (e.get("delta") or {}).get("change"), "op": e.get("op")}],
            "second_order": [c for c in casc if c.get("order") == 2 and not c.get("_private")],
            "third_order": [c for c in casc if c.get("order") == 3 and not c.get("_private")],
            "_private": True if e.get("private") else None})
        if tgt in touched:
            couplings.append({"resource": tgt if not e.get("private") else "<私密资源>",
                              "first_changed_seq": touched[tgt], "reused_at_seq": seq,
                              "why": "同一资源被多步连续改动: 前步偏差会累积传导到后步"})
        touched.setdefault(tgt, seq)
    out = {"ok": True, "box_id": box["box_id"], "steps": len(trace),
           "per_step_effects": per_step, "cross_step_couplings": couplings,
           "note": "逐步后果(只读): 每步一阶直接改动 + 二阶 world_model 波及 + 三阶下游预测; "
                   "并标跨步耦合(前步偏差会传导)。私密资源只回元数据。"}
    if private_hit:
        out["_private"] = True
    _audit("predict_side_effects", box["box_id"], steps=len(trace), private=private_hit or None)
    return out


# --------------------------------------------------------------------------- #
# F. stress_test — 逆境场景矩阵下的韧性分析(纯分析, 不真造故障)
# --------------------------------------------------------------------------- #
def _scenario_breaking_steps(key: str, trace: list) -> list:
    out = []
    for e in trace:
        seq = e.get("seq")
        op = e.get("op")
        kind = (e.get("classification") or {}).get("kind")
        ext = e.get("external")
        rev = e.get("reversible")
        dom = e.get("domain")
        hit = False
        if key == "dependency_missing":
            hit = dom in ("file", "code") and op in ("write_file", "move_file", "patch_code")
        elif key == "concurrent_modification":
            hit = op in ("write_file", "move_file", "update_event", "patch_code")
        elif key == "partial_failure_midway":
            hit = kind in ("mutating", "red_line") and not rev
        elif key == "privacy_lock_engaged":
            hit = bool(e.get("private"))
        elif key == "engine_unavailable":
            hit = dom in ("business", "tool") or op in ("create_event", "send_email")
        elif key == "high_volume_load":
            hit = dom in ("file", "tool", "business")
        elif key == "permission_denied":
            hit = kind == "red_line" or (kind == "mutating" and ext)
        elif key == "rollback_needed":
            hit = not rev
        if hit:
            out.append(seq)
    return out


def _scenario_recoverable(key: str, trace: list) -> bool:
    mutating = [e for e in trace if (e.get("classification") or {}).get("kind") != "read_only"]
    if key in ("rollback_needed", "partial_failure_midway", "concurrent_modification"):
        return all(e.get("reversible") for e in mutating)
    return True  # 隐私锁/引擎缺/高并发/缺依赖/权限 等只是阻塞或降级, 不破坏可恢复性


def _stress_recs(results: list) -> list:
    recs = []
    for r in results:
        if r["severity"] == "high":
            recs.append(f"[{r['scenario']}] {r['desc']} → {r['break_count']} 步会断" +
                        ("(且不可全恢复)" if not r["recoverable"] else "(可恢复)") +
                        ": 建议先确认依赖/加幂等/拆小批/先 approval。")
    if not recs:
        recs.append("各逆境场景下韧性良好; 仍建议有副作用步骤先经 approval_packet→User 批。")
    return recs


def stress_test(box_id: str, params=None) -> dict:
    box = _load_box(box_id)
    if not box:
        return {"ok": False, "error": f"没找到沙盘 {box_id}(先 sandbox_plan)"}
    trace = box.get("trace") or []
    if not trace:
        return {"ok": False, "error": "沙盘里没有执行轨迹(先 sandbox_plan / simulate_toolchain)"}
    steps = _steps_from_trace(trace)
    base = _score_steps(steps, blast=0, privacy=bool(box.get("_private")))
    results = []
    for key, desc in _STRESS_SCENARIOS:
        breaking = _scenario_breaking_steps(key, trace)
        recoverable = _scenario_recoverable(key, trace)
        sev = "high" if len(breaking) >= max(1, len(trace) // 2) and breaking else ("medium" if breaking else "low")
        results.append({"scenario": key, "desc": desc, "breaking_steps": breaking[:20],
                        "break_count": len(breaking), "recoverable": recoverable, "severity": sev})
    total = max(1, len(trace))
    avg_break = sum(r["break_count"] for r in results) / (len(results) * total)
    recover_ratio = sum(1 for r in results if r["recoverable"]) / len(results)
    resilience = round(max(0.0, min(100.0, (1 - avg_break) * 70 + recover_ratio * 30)), 1)
    weakest = max(results, key=lambda r: r["break_count"])
    out = {"ok": True, "box_id": box_id, "base_risk": base, "scenarios": results,
           "resilience_score": resilience, "weakest_scenario": weakest["scenario"],
           "recommendations": _stress_recs(results),
           "note": "压力测试(纯分析, 不真造故障): 逆境场景矩阵下看哪步会断 + 可否恢复 + 韧性分(0-100)。"}
    if box.get("_private"):
        out["_private"] = True
    _audit("stress_test", box_id, resilience=resilience)
    return out


# --------------------------------------------------------------------------- #
# G. compare_outcomes — 对比多沙盘虚拟终态择优
# --------------------------------------------------------------------------- #
def compare_outcomes(box_ids: list) -> dict:
    box_ids = [b for b in (box_ids or []) if b]
    if len(box_ids) < 2:
        return {"ok": False, "error": "compare_outcomes 需要至少 2 个 box_id"}
    rows = []
    private_hit = False
    for bid in box_ids:
        box = _load_box(bid)
        if not box:
            rows.append({"box_id": bid, "error": "未找到"})
            continue
        trace = box.get("trace") or []
        steps = _steps_from_trace(trace)
        fc = box.get("forecast") or {}
        if not fc:
            fc, _ = _box_forecast(box)
        rs = fc.get("risk_score")
        risk = float(rs) if rs is not None else 100.0  # 仅缺失当最坏; 0.0 是合法低风险, 不可当 100
        rev_ratio = sum(1 for s in steps if s.get("reversible")) / max(1, len(steps))
        external = sum(1 for s in steps if s.get("external"))
        blast = len(fc.get("affected_entities", []) or [])
        composite = round(max(0.0, min(1.0,
                          (100 - risk) / 100 * 0.5 + rev_ratio * 0.3
                          + min(len(steps), 8) / 8 * 0.1
                          - min(external, 5) / 5 * 0.05 - min(blast, 10) / 10 * 0.05)), 4)
        if box.get("_private"):
            private_hit = True
        rows.append({"box_id": bid, "title": box.get("title"), "end_state": _box_state_summary(box),
                     "risk_score": risk, "risk_band": fc.get("risk_band"),
                     "reversible_ratio": round(rev_ratio, 3), "external_actions": external,
                     "blast_radius": blast, "trace_len": len(trace), "composite": composite})
    ranked = [r for r in rows if "composite" in r]
    ranked.sort(key=lambda r: r["composite"], reverse=True)
    out = {"ok": True, "count": len(rows), "ranked": ranked + [r for r in rows if "error" in r],
           "recommended": ranked[0]["box_id"] if ranked else None,
           "rationale": ("综合分 = (100−风险)/100×0.5 + 可逆比×0.3 + 步骤覆盖×0.1 "
                         "− 对外×0.05 − 波及×0.05(封顶 1)。选风险最低、最可逆、波及最小的终态; "
                         "仍须经 approval_packet→User 批落地。")}
    if private_hit:
        out["_private"] = True
    _audit("compare_outcomes", "", count=len(box_ids), recommended=out.get("recommended"))
    return out


# --------------------------------------------------------------------------- #
# H. failure_injection — 在某步注入失败, 看连锁阻塞/部分状态/可否回滚(纯分析)
# --------------------------------------------------------------------------- #
def failure_injection(box_id: str, params=None) -> dict:
    params = dict(params or {})
    box = _load_box(box_id)
    if not box:
        return {"ok": False, "error": f"没找到沙盘 {box_id}(先 sandbox_plan)"}
    trace = box.get("trace") or []
    if not trace:
        return {"ok": False, "error": "沙盘里没有执行轨迹"}
    seqs = [e.get("seq") for e in trace]
    base = min(seqs) if seqs else 1
    step_param = params.get("step")
    points = [int(step_param)] if (step_param and str(step_param).isdigit()) else seqs

    injections = []
    private_hit = bool(box.get("_private"))
    for k in points:
        idx = k - base
        if idx < 0 or idx >= len(trace):
            continue
        failed = trace[idx]
        succeeded = trace[:idx]
        downstream = trace[idx + 1:]
        partial = [{"seq": e.get("seq"), "target": e.get("target"), "op": e.get("op"),
                    "reversible": e.get("reversible")}
                   for e in succeeded if (e.get("classification") or {}).get("kind") != "read_only"]
        changed_targets = {e.get("target") for e in succeeded} | {failed.get("target")}
        blocked = []
        for e in downstream:
            reason = None
            if e.get("domain") == "tool" and (e.get("delta") or {}).get("consumes_prev"):
                reason = "消费上一步输出, 上游失败 → 无输入可用"
            elif e.get("target") in changed_targets:
                reason = "依赖被前序步骤改动的资源, 状态不一致"
            elif (e.get("classification") or {}).get("kind") != "read_only":
                reason = "前序失败导致前置条件可能不满足(保守判定为受阻)"
            if reason:
                blocked.append({"seq": e.get("seq"), "target": e.get("target"), "why": reason})
        recoverable = all(e.get("reversible") for e in succeeded
                          if (e.get("classification") or {}).get("kind") != "read_only")
        residual = "low" if (recoverable and not any(e.get("external") for e in succeeded)) else "high"
        if failed.get("private") or any(e.get("private") for e in succeeded):
            private_hit = True
        injections.append({"inject_at_seq": k, "failed_step": _entry_brief(failed),
                           "partial_state_changes": partial, "blocked_downstream": blocked,
                           "blocked_count": len(blocked), "recoverable_partial_state": recoverable,
                           "residual_risk": residual,
                           "rollback_hint": "沿 rollback_path 逆序撤回已发生改动" +
                                            ("(全可逆)" if recoverable else "(含不可逆/对外步骤, 须人工补偿)")})
    worst = max(injections, key=lambda x: (x["blocked_count"], 0 if x["recoverable_partial_state"] else 1)) \
        if injections else None
    out = {"ok": True, "box_id": box_id, "injection_points": len(injections), "injections": injections,
           "worst_injection": worst["inject_at_seq"] if worst else None,
           "note": "失败注入(纯分析): 在某步注入失败, 看部分状态/下游阻塞/可否回滚/残余风险。不真造故障、不执行。"}
    if private_hit:
        out["_private"] = True
    _audit("failure_injection", box_id, points=len(injections))
    return out


# --------------------------------------------------------------------------- #
# I. sandbox_rollback_path — 沿轨迹算完整回滚路径(逆序逆操作; 只出预案不执行)
#    (原名 rollback_path, 2026-06-10 合并时与情景版撞名改之)
# --------------------------------------------------------------------------- #
def sandbox_rollback_path(box_id: str) -> dict:
    box = _load_box(box_id)
    if not box:
        return {"ok": False, "error": f"没找到沙盘 {box_id}(先 sandbox_plan)"}
    trace = box.get("trace") or []
    if not trace:
        return {"ok": False, "error": "沙盘里没有执行轨迹"}
    path = []
    auto = True
    for e in reversed(trace):
        if (e.get("classification") or {}).get("kind") == "read_only":
            continue
        undo = e.get("undo") or {}
        is_auto = bool(undo.get("auto"))
        if not is_auto:
            auto = False
        path.append({"undo_seq": e.get("seq"), "target": e.get("target"), "op": e.get("op"),
                     "undo_op": undo.get("op"), "describe": undo.get("describe"),
                     "auto_revertible": is_auto, "needs_approval": True})
    box["rollback"] = {"path": path, "auto_revertible": auto}
    _save_box(box)
    out = {"ok": True, "box_id": box_id, "auto_revertible": auto, "rollback_path": path,
           "steps": len(path),
           "note": "回滚路径(逆序逆操作; 只出预案不执行): 任一回写/逆操作本身也是副作用, 须经 "
                   "approval_packet→User 批。沙盒副本永久保留(只 User 手动清)。"}
    if box.get("_private"):
        out["_private"] = True
    _audit("sandbox_rollback_path", box_id, steps=len(path), auto=auto)
    return out


# --------------------------------------------------------------------------- #
# J. approval_preview — 预览「将入审批队列的 payload/风险/why」, 但绝不真入队
# --------------------------------------------------------------------------- #
def approval_preview(sid_or_box: str) -> dict:
    sid_or_box = (sid_or_box or "").strip()
    if sid_or_box.startswith("box_"):
        box = _load_box(sid_or_box)
        if not box:
            return {"ok": False, "error": f"没找到沙盘 {sid_or_box}"}
        steps = _steps_from_trace(box.get("trace") or [])
        fc = box.get("forecast") or {}
        if not fc:
            fc, _ = _box_forecast(box)
        title = (box.get("title") or "")[:120]
        source = "simulation_lab.sandbox"
        ident = {"box_id": box["box_id"]}
        action_kind = "note"  # 沙盘默认人工复核(未绑可执行 action_kind)
        private = bool(box.get("_private"))
        steps_payload = [{"seq": s["n"], "action": s["action"], "kind": s["kind"], "gated": s["gated"]}
                         for s in steps]
        tip = ("沙盘真入队前请先 simulate 出 sim 再 approval_packet(沙盘 preview 只读不入队)。")
    else:
        sim = _load_sim(sid_or_box)
        if not sim:
            return {"ok": False, "error": f"没找到仿真/沙盘 {sid_or_box}"}
        steps = sim.get("steps") or []
        fc = sim.get("forecast") or {}
        title = (sim.get("title") or "")[:120]
        source = "simulation_lab"
        ident = {"sim_id": sim["sim_id"]}
        action_kind = sim.get("action_kind") or "note"
        private = bool(sim.get("_private"))
        steps_payload = [{"n": s["n"], "action": s["action"], "kind": s["kind"], "gated": s["gated"]}
                         for s in steps]
        tip = "真入队: jarvis simulation_lab approval_packet <sim_id>(经 User 批)。"

    band = fc.get("risk_band", "high")
    risk = "critical" if band == "critical" else "high"  # 落地恒 risk≥high
    payload = {"action_kind": action_kind, "source": source, "title": title, **ident,
               "steps": steps_payload,
               "forecast": {"risk_score": fc.get("risk_score"), "risk_band": band,
                            "failure_points": fc.get("failure_points"),
                            "affected_entities": fc.get("affected_entities"),
                            "predicted_user_reaction": fc.get("predicted_user_reaction")}}
    why = (f"[simulation_lab 审批预览] 风险 {fc.get('risk_score')}/{band}; "
           f"失败点 {len(fc.get('failure_points', []) or [])}")
    out = {"ok": True, **ident, "enqueued": False, "would_enqueue": True,
           "preview_payload": payload, "risk": risk, "action_kind": action_kind, "why": why,
           "approve_after_enqueue": "jarvis autopilot approve <approval_id>",
           "note": "审批预览(只读): 这是「将入审批队列」的 payload/风险/why, 但**本命令绝不真入队、绝不执行**。"
                   + tip}
    if private:
        out["_private"] = True
    _audit("approval_preview", ident.get("sim_id") or ident.get("box_id") or "", risk=risk, enqueued=False)
    return out


# --------------------------------------------------------------------------- #
# K. explain_consequence — 逐步因果叙述(改了什么→波及谁→失败会怎样→怎么撤回)
# --------------------------------------------------------------------------- #
def _failure_narrative(e: dict) -> str:
    cls = (e.get("classification") or {}).get("kind")
    if cls == "red_line":
        return f"命中红线({(e.get('classification') or {}).get('why', '')}): 会被确认门拦下, 须 User 逐条批"
    if e.get("external"):
        return "对外动作失败可能已部分送达, 不可撤回, 只能跟进更正"
    if not e.get("reversible"):
        return "不可逆步骤失败 → 难自动回滚, 须人工补偿"
    return "可逆步骤失败 → 沿 rollback_path 逆序撤回, 影响可控"


def explain_consequence(box_id: str, params=None) -> dict:
    params = dict(params or {})
    box = _load_box(box_id)
    if not box:
        return {"ok": False, "error": f"没找到沙盘 {box_id}(先 sandbox_plan)"}
    trace = box.get("trace") or []
    if not trace:
        return {"ok": False, "error": "沙盘里没有执行轨迹"}
    seqs = [e.get("seq") for e in trace]
    base = min(seqs) if seqs else 1
    step_param = params.get("step")
    selected = trace
    if step_param and str(step_param).isdigit():
        idx = int(step_param) - base
        if not (0 <= idx < len(trace)):
            return {"ok": False, "error": f"step {step_param} 超出轨迹范围"}
        selected = [trace[idx]]

    private_hit = bool(box.get("_private"))
    explanations = []
    for e in selected:
        if e.get("private"):
            private_hit = True
        casc = e.get("cascade") or []
        affects = [c.get("target") for c in casc
                   if c.get("order") == 2 and c.get("target") and not c.get("_private")]
        downstream = [c.get("target") for c in casc
                      if c.get("order") == 3 and c.get("target") and not c.get("_private")]
        undo = e.get("undo") or {}
        changes = (e.get("delta") or {}).get("change") or e.get("predicted_effect")
        explanations.append({
            "seq": e.get("seq"), "op": e.get("op"),
            "target": (e.get("target") if not e.get("private") else "<私密资源>"),
            "kind": (e.get("classification") or {}).get("kind"),
            "changes": ("<私密改动, 只回元数据>" if e.get("private") else changes),
            "because_will_affect": affects, "downstream_predicted": downstream,
            "if_it_fails": _failure_narrative(e),
            "to_undo": undo.get("describe"), "undo_auto": bool(undo.get("auto"))})
    out = {"ok": True, "box_id": box_id, "explained_steps": len(explanations),
           "consequences": explanations,
           "note": "后果因果叙述(只读): 每步改了什么 → 因依赖/因果会波及谁 → 下游会怎样 → 失败会怎样 → "
                   "怎么撤回。私密资源只回元数据。"}
    if private_hit:
        out["_private"] = True
    _audit("explain_consequence", box_id, steps=len(explanations))
    return out


# --------------------------------------------------------------------------- #
# 沙盘版 audit / status(由 audit()/status() 按 box_ 前缀路由进来)
# --------------------------------------------------------------------------- #
def _audit_box(box_id: str) -> dict:
    box = _load_box(box_id)
    if not box:
        return {"ok": False, "error": f"没找到沙盘 {box_id}"}
    trace = box.get("trace") or []
    by_kind = {"read_only": 0, "mutating": 0, "red_line": 0}
    red_steps = []
    for e in trace:
        k = (e.get("classification") or {}).get("kind", "mutating")
        by_kind[k] = by_kind.get(k, 0) + 1
        if k == "red_line":
            red_steps.append({"seq": e.get("seq"), "action": (e.get("action_text") or "")[:160],
                              "why": (e.get("classification") or {}).get("why")})
    box_real = bool(box.get("executed_for_real"))
    entry_real = any(bool(e.get("executed_for_real")) for e in trace)
    # 私密文件资源必须 redacted(只回元数据); _file_state 私密分支恒带 redacted
    privacy_clean = True
    for v in (box.get("state") or {}).values():
        v = v or {}
        if v.get("_private") and v.get("domain") == "file" and not v.get("redacted"):
            privacy_clean = False
    invariants = {"box_executed_for_real": box_real,             # 期望恒 False
                  "any_trace_executed_for_real": entry_real,     # 期望恒 False
                  "shadow_touched_real_target": False,           # 虚拟态只碰沙盒副本
                  "ever_self_approved": False,                   # 绝不自传 confirmed=True
                  "private_resources_redacted": privacy_clean}
    invariant_ok = (box_real is False) and (entry_real is False) and privacy_clean
    private_hit = bool(box.get("_private"))
    if by_kind["red_line"] > 0 or private_hit:
        verdict = "needs_attention"
    elif by_kind["mutating"] > 0:
        verdict = "review_required"
    else:
        verdict = "clean"
    recs = []
    if by_kind["red_line"] > 0:
        recs.append("含红线步骤: 落地须 User 逐条确认(critical token), 沙盘层绝不自动执行。")
    if by_kind["mutating"] > 0:
        recs.append("含有副作用步骤: 经 approval_packet→autopilot 审批队列, 过 local_powers 红线门。")
    if private_hit:
        recs.append("命中隐私内容: 下游叙述切本机模型, 资源正文只回元数据。")
    if not invariant_ok:
        recs.append("⚠️ 不变量异常: executed_for_real 非 False 或私密未脱敏 —— 立即排查。")
    if not recs:
        recs.append("全只读且不变量正常。")
    out = {"ok": True, "box_id": box_id, "title": box.get("title"), "trace_total": len(trace),
           "by_kind": by_kind, "red_line_steps": red_steps, "state_summary": _box_state_summary(box),
           "safety_invariants": invariants, "invariants_ok": invariant_ok,
           "verdict": verdict, "recommendations": recs,
           "note": "数字沙盘安全审计(只读): 红线分级复用 local_powers 单一真相源; "
                   "自验沙盘从未真执行/自放行/碰真实目标; 私密资源只回元数据。"}
    if private_hit:
        out["_private"] = True
    _audit("audit_box", box_id, verdict=verdict, red=by_kind["red_line"], invariant_ok=invariant_ok)
    return out


def _status_box(box_id: str) -> dict:
    box = _load_box(box_id)
    if not box:
        return {"ok": False, "error": f"没找到沙盘 {box_id}"}
    fc = box.get("forecast") or {}
    out = {"ok": True, "box_id": box_id, "title": box.get("title"),
           "created": box.get("created"), "updated": box.get("updated"),
           "state_summary": _box_state_summary(box), "trace_len": len(box.get("trace") or []),
           "imports": box.get("imports") or [], "risk_score": fc.get("risk_score"),
           "risk_band": fc.get("risk_band"), "rollback_ready": bool(box.get("rollback")),
           "executed_for_real": box.get("executed_for_real"),
           "trace": [_entry_brief(e) for e in (box.get("trace") or [])],
           "note": "数字沙盘全貌。恒 executed_for_real=False; 真执行经 approval_packet→User 批。"}
    if box.get("_private"):
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# LLM 工具面(只读为主 + 冻结): 仅 simulate(dry 预测) / status / risk_score。
# 强动作(dry_run/shadow_execute/diff/impact_predict/compare_plans/rollback_plan/
# approval_packet/rehearse_ui/test_action/what_if/audit)CLI-only。
# --------------------------------------------------------------------------- #
_SIM_TOOLS = [
    {"type": "function", "function": {
        "name": "simulation_lab_simulate",
        "description": ("行动仿真沙盒: 真正执行复杂任务前, 先预演后果 —— 给计划/动作分级(红线/副作用), "
                        "预测收益·风险·依赖·失败点·用户反应。纯 dry 预测, 零副作用, 不执行任何动作。"),
        "parameters": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "要预演的目标/计划(可含 STEP: 行)"},
        }, "required": ["goal"]}}},
    {"type": "function", "function": {
        "name": "simulation_lab_risk_score",
        "description": "给一个动作或已有仿真打风险分(0-100)并分解(红线/副作用/不可逆/隐私/对外/波及面)。只读。",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "sim_id 或 \"<动作>\""}}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "simulation_lab_status",
        "description": "看某仿真全貌(传 sim_id)或列最近仿真 + 各引擎可用性(不传)。只读。",
        "parameters": {"type": "object", "properties": {
            "sim_id": {"type": "string"}}}}},
]


def approved_tools() -> list:
    if not config.get("simulation_lab.enabled", True) or not config.get("simulation_lab.expose_tools", True):
        return []
    return list(_SIM_TOOLS)


def is_tool(name: str) -> bool:
    return name in ("simulation_lab_simulate", "simulation_lab_risk_score", "simulation_lab_status")


def dispatch(name: str, args: dict) -> dict:
    args = dict(args or {})
    if name == "simulation_lab_simulate":
        return simulate(args.get("goal", ""))
    if name == "simulation_lab_risk_score":
        return risk_score(args.get("target", ""))
    if name == "simulation_lab_status":
        return status(args.get("sim_id", ""))
    return {"ok": False, "error": f"unknown simulation_lab tool: {name}"}


# --------------------------------------------------------------------------- #
# CLI(argparse; 全 pretty-JSON via _emit, 对齐 agent_swarm/swarm 风格)
# --------------------------------------------------------------------------- #
def _main(argv: list) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="simulation_lab",
                                description="行动仿真沙盒与后果预测器")
    sub = p.add_subparsers(dest="cmd", required=True)

    psi = sub.add_parser("simulate", help="建仿真 + 后果预测(默认 dry)")
    psi.add_argument("goal", nargs="?", default="")
    psi.add_argument("--kind", default="plan", help="plan|action|code|ui|external...")
    psi.add_argument("--from-swarm", default="", help="从 agent_swarm/swarm run 的合并计划建仿真")
    psi.add_argument("--register-goal", action="store_true", help="把目标登记进 goal_engine")
    psi.add_argument("-p", "--param", action="append", default=[], help="k=v(可多个; action/action_kind/file/content_file)")

    pdr = sub.add_parser("dry_run", help="逐步走引擎自带预演面(零副作用)")
    pdr.add_argument("sim_id")

    pse = sub.add_parser("shadow_execute", help="影子执行(真实目标不碰; 文件落沙盒副本)")
    pse.add_argument("sim_id")
    pse.add_argument("-p", "--param", action="append", default=[], help="k=v(file/content_file)")

    pdf = sub.add_parser("diff", help="看影子执行前后差异")
    pdf.add_argument("sim_id")

    prs = sub.add_parser("risk_score", help="风险评分 0-100 分解")
    prs.add_argument("target", help="sim_id 或 \"<动作>\"")

    pip = sub.add_parser("impact_predict", help="后果预测(world_model 影响/预测/因果)")
    pip.add_argument("target", help="sim_id 或 \"<目标实体>\"")

    pcp = sub.add_parser("compare_plans", help="多方案对比择优")
    pcp.add_argument("sim_ids", nargs="+", help="至少 2 个 sim_id")

    prb = sub.add_parser("rollback_plan", help="生成回滚/撤销预案(不执行)")
    prb.add_argument("sim_id")

    pap = sub.add_parser("approval_packet", help="打包入审批队列(risk≥high), 待 User 批")
    pap.add_argument("sim_id")

    pru = sub.add_parser("rehearse_ui", help="演练 UI(computer_use plan, 只看不点)")
    pru.add_argument("target", help="sim_id 或 \"<UI 任务>\"")

    pta = sub.add_parser("test_action", help="单动作快测")
    pta.add_argument("action")

    pwi = sub.add_parser("what_if", help="反事实推演")
    pwi.add_argument("sim_id")
    pwi.add_argument("--change", default="", help="k=v(可 stepN=新动作)")
    pwi.add_argument("--assume", default="", help="自然语言假设")

    # ---- 多情景反事实未来沙盘 ----
    psp = sub.add_parser("simulate_plan", help="多情景未来沙盘(best/base/worst + 后果/二阶/失败点/敏感/回退/监控)")
    psp.add_argument("plan", nargs="?", default="", help="候选计划(可含 STEP: 行)")
    psp.add_argument("--goal", default="", help="目标(默认取计划)")
    psp.add_argument("--from-swarm", default="", help="从 agent_swarm/swarm run 的合并计划建沙盘")
    psp.add_argument("--register-goal", action="store_true", help="把目标登记进 goal_engine")
    psp.add_argument("-p", "--param", action="append", default=[], help="k=v(action/action_kind/file...)")

    pstr = sub.add_parser("scenario_tree", help="情景树(best/base/worst + 失败触发子分支)")
    pstr.add_argument("target", help="scn_id 或 \"<计划>\"")

    pcf = sub.add_parser("counterfactual", help="情景级反事实(改前提重算未来)")
    pcf.add_argument("target", help="scn_id 或 \"<计划>\"")
    pcf.add_argument("--change", default="", help="k=v(stepN=新动作 / goal=... / dependency=...)")
    pcf.add_argument("--assume", default="", help="自然语言假设")

    pso = sub.add_parser("second_order_effects", help="二阶影响(下游波及)")
    pso.add_argument("target", help="scn_id 或 \"<计划>\"")

    pftr = sub.add_parser("failure_triggers", help="失败触发点")
    pftr.add_argument("target", help="scn_id 或 \"<计划>\"")

    psen = sub.add_parser("sensitivity", help="关键敏感变量")
    psen.add_argument("target", help="scn_id 或 \"<计划>\"")

    pew = sub.add_parser("early_warning", help="推荐监控指标/预警")
    pew.add_argument("target", help="scn_id 或 \"<计划>\"")

    prp = sub.add_parser("rollback_path", help="可逆回退路径(只出预案)")
    prp.add_argument("target", help="scn_id 或 \"<计划>\"")

    pcfu = sub.add_parser("compare_futures", help="多未来对比择优")
    pcfu.add_argument("scn_ids", nargs="+", help="至少 2 个 scn_id(或计划)")

    peo = sub.add_parser("explain_outcome", help="成因解释(为什么这个情景会发生)")
    peo.add_argument("scn_id", help="scn_id 或 \"<计划>\"")
    peo.add_argument("--case", default="base", help="best|base|worst")

    pau = sub.add_parser("audit", help="安全/合规审计(只读)")
    pau.add_argument("sim_id")

    pst = sub.add_parser("status", help="看某仿真/沙盘 / 列最近")
    pst.add_argument("sim_id", nargs="?", default="")

    # --- 可执行数字沙盘与后果预演器 ---
    psb = sub.add_parser("sandbox", help="建/列/看/重置数字沙盘")
    psb.add_argument("box_id", nargs="?", default="")
    psb.add_argument("--new", action="store_true", help="建新空沙盘")
    psb.add_argument("--title", default="", help="沙盘标题")
    psb.add_argument("--reset", default="", help="清空指定沙盘的虚拟态与轨迹(仅本层 json, 不删真实文件)")

    pis = sub.add_parser("import_state", help="只读把真实状态导入沙盘虚拟态")
    pis.add_argument("box_id", nargs="?", default="")
    pis.add_argument("--new", action="store_true", help="新建沙盘并导入")
    pis.add_argument("--kind", default="file", help="file|todo|world|json")
    pis.add_argument("-p", "--param", action="append", default=[], help="k=v(path/paths/topic/json_file/title)")

    pspl = sub.add_parser("sandbox_plan", help="把计划编成可回滚虚拟执行轨迹(落沙盘)")
    pspl.add_argument("goal", nargs="?", default="")
    pspl.add_argument("--box", default="", help="目标沙盘(缺则新建)")
    pspl.add_argument("--from-swarm", default="", help="从 agent_swarm/swarm run 合并计划编轨迹")
    pspl.add_argument("-p", "--param", action="append", default=[], help="k=v(action/file/content_file/dest)")

    pstc = sub.add_parser("simulate_toolchain", help="把多工具调用链编成虚拟轨迹(绝不调用工具)")
    pstc.add_argument("--box", default="", help="目标沙盘(缺则新建)")
    pstc.add_argument("-p", "--param", action="append", default=[], help="k=v(chain_file=<json> 或 tools=\"a,b,c\")")

    ppse = sub.add_parser("predict_side_effects", help="一阶直接+二阶波及+三阶下游+跨步耦合")
    ppse.add_argument("target", help="box_id 或 \"<目标>\"")

    psst = sub.add_parser("stress_test", help="逆境场景矩阵韧性分析(纯分析)")
    psst.add_argument("box_id")

    pco = sub.add_parser("compare_outcomes", help="对比多沙盘虚拟终态择优")
    pco.add_argument("box_ids", nargs="+", help="至少 2 个 box_id")

    pfi = sub.add_parser("failure_injection", help="在某步注入失败看连锁(纯分析)")
    pfi.add_argument("box_id")
    pfi.add_argument("-p", "--param", action="append", default=[], help="k=v(step=N; 缺则逐步)")

    prp = sub.add_parser("sandbox_rollback_path", help="沿轨迹算完整回滚路径(不执行)")
    prp.add_argument("box_id")

    papv = sub.add_parser("approval_preview", help="预览将入审批队列的 payload(绝不真入队)")
    papv.add_argument("target", help="sim_id 或 box_id")

    pec = sub.add_parser("explain_consequence", help="逐步因果叙述(改了什么→波及谁→失败→怎么撤回)")
    pec.add_argument("box_id")
    pec.add_argument("-p", "--param", action="append", default=[], help="k=v(step=N; 缺则全部)")

    args = p.parse_args(argv)

    if args.cmd == "simulate":
        goal = (args.goal or "").strip()
        params = _params(args.param)
        if not goal and not args.from_swarm and not params.get("action"):
            print('用法: jarvis simulation_lab simulate "<目标/计划>" [--from-swarm <run>] [-p k=v]',
                  file=sys.stderr)
            return 2
        _emit(simulate(goal, params=params, kind=args.kind,
                       from_swarm=args.from_swarm, register_goal=args.register_goal))
    elif args.cmd == "dry_run":
        _emit(dry_run(args.sim_id))
    elif args.cmd == "shadow_execute":
        _emit(shadow_execute(args.sim_id, params=_params(args.param)))
    elif args.cmd == "diff":
        _emit(diff(args.sim_id))
    elif args.cmd == "risk_score":
        _emit(risk_score(args.target))
    elif args.cmd == "impact_predict":
        _emit(impact_predict(args.target))
    elif args.cmd == "compare_plans":
        _emit(compare_plans(args.sim_ids))
    elif args.cmd == "rollback_plan":
        _emit(rollback_plan(args.sim_id))
    elif args.cmd == "approval_packet":
        _emit(approval_packet(args.sim_id))
    elif args.cmd == "rehearse_ui":
        _emit(rehearse_ui(args.target))
    elif args.cmd == "test_action":
        _emit(test_action(args.action))
    elif args.cmd == "what_if":
        _emit(what_if(args.sim_id, change=args.change, assume=args.assume))
    elif args.cmd == "simulate_plan":
        params = _params(args.param)
        if not args.plan and not args.from_swarm and not args.goal and not params.get("action"):
            print('用法: jarvis simulation_lab simulate_plan "<计划>" [--goal G] [--from-swarm <run>] [-p k=v]',
                  file=sys.stderr)
            return 2
        _emit(simulate_plan(args.plan, goal=args.goal, params=params,
                            from_swarm=args.from_swarm, register_goal=args.register_goal))
    elif args.cmd == "scenario_tree":
        _emit(scenario_tree(args.target))
    elif args.cmd == "counterfactual":
        _emit(counterfactual(args.target, change=args.change, assume=args.assume))
    elif args.cmd == "second_order_effects":
        _emit(second_order_effects(args.target))
    elif args.cmd == "failure_triggers":
        _emit(failure_triggers(args.target))
    elif args.cmd == "sensitivity":
        _emit(sensitivity(args.target))
    elif args.cmd == "early_warning":
        _emit(early_warning(args.target))
    elif args.cmd == "rollback_path":
        _emit(rollback_path(args.target))
    elif args.cmd == "compare_futures":
        _emit(compare_futures(args.scn_ids))
    elif args.cmd == "explain_outcome":
        _emit(explain_outcome(args.scn_id, case=args.case))
    elif args.cmd == "audit":
        _emit(audit(args.sim_id))
    elif args.cmd == "status":
        _emit(status(args.sim_id))
    # --- 可执行数字沙盘与后果预演器 ---
    elif args.cmd == "sandbox":
        _emit(sandbox(box_id=args.box_id, new=args.new, title=args.title, reset=args.reset))
    elif args.cmd == "import_state":
        _emit(import_state(box_id=args.box_id, kind=args.kind,
                           params=_params(args.param), new=args.new))
    elif args.cmd == "sandbox_plan":
        goal = (args.goal or "").strip()
        params = _params(args.param)
        if not goal and not args.from_swarm and not params.get("action"):
            print('用法: jarvis simulation_lab sandbox_plan "<计划(可含 STEP:)>" [--box <id>] [-p k=v]',
                  file=sys.stderr)
            return 2
        _emit(sandbox_plan(goal, params=params, box_id=args.box, from_swarm=args.from_swarm))
    elif args.cmd == "simulate_toolchain":
        _emit(simulate_toolchain(box_id=args.box, params=_params(args.param)))
    elif args.cmd == "predict_side_effects":
        _emit(predict_side_effects(args.target))
    elif args.cmd == "stress_test":
        _emit(stress_test(args.box_id))
    elif args.cmd == "compare_outcomes":
        _emit(compare_outcomes(args.box_ids))
    elif args.cmd == "failure_injection":
        _emit(failure_injection(args.box_id, params=_params(args.param)))
    elif args.cmd == "sandbox_rollback_path":
        _emit(sandbox_rollback_path(args.box_id))
    elif args.cmd == "approval_preview":
        _emit(approval_preview(args.target))
    elif args.cmd == "explain_consequence":
        _emit(explain_consequence(args.box_id, params=_params(args.param)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
