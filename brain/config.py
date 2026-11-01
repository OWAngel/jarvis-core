#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config.py — 小美统一配置中心。

读 ~/.jarvis/config.toml(py3.11 stdlib tomllib),写用自带的极简 TOML 序列化器
(_dump_toml,因为本机 conda myenv 没装 tomli_w,brain 坚持纯 stdlib 不引第三方)。

向后兼容铁律:文件 / key 缺失 → 返回调用方传入的 default(或内嵌 _DEFAULTS),
绝不抛异常炸掉大脑。每个模块调用一律传 default = 自己原硬编码值。

[safety] / [credentials] 两节 User-only:set() 拒写(抛 PermissionError),
小美的 self-manage 工具靠这个 PermissionError 实现「只读」。

热读:get/get_section 每次现取,文件 mtime 变了自动 reload → temperature/model/
voice/safe_word/guard_enabled/insight.*/reminder.* 改完热生效。
daemon 级参数(EVOLVE_*/各端口/server.port/vlm preload)在 daemon 启动时一次读进
模块级常量,改完需重启对应 daemon(由 UI /api/restart 或 registry.restart 负责)。

API:
  get(path, default=_SENTINEL) -> Any        点路径取值
  get_section(section) -> dict                整节(默认 ⊕ 用户覆盖)
  set(path, value) -> None                   写值(过校验 + 原子落盘;safety/credentials 拒写)
  all() -> dict                              完整合并配置
  reload() -> None                          强制重读文件刷新缓存
  path() -> str                             CONFIG_PATH
"""
import os
import sys
import threading
try:
    import tomllib  # Python 3.11+ stdlib
except ModuleNotFoundError:  # py3.9/3.10(CI 矩阵): 无 tomllib → 配置文件不解析, 全走 _DEFAULTS
    tomllib = None  # type: ignore[assignment]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
CONFIG_PATH = os.path.expanduser(os.environ.get("JARVIS_CONFIG_PATH", "~/.jarvis-core/config.toml"))

# 仓根 = 本文件(brain/config.py)的上上层。**全仓路径默认值的唯一真相源。**
#
# 为什么必须算出来而不是写死 "~/.jarvis/workspace":
#   CI 跑在 容器 VM ci-linux VM 里, 而 VM **不隔离宿主文件系统** —— Mac 家目录以
#   同名路径直通进 VM 且可读写。写死绝对路径的默认值(库/凭据/SOUL.md)在 CI 里不会响亮
#   报错, 而是**静默连上并写入生产**。ci/prod_guard/sitecustomize.py 那道 audit hook 就是
#   为拦这个而存在的 —— 但拦是兜底, 根治是别写死。同理, 从 worktree 起的进程也会去读
#   生产资源, 导致改动"看起来没生效"(2026-08-02 接 GPT-Live 时就被 server.py 这条坑了半天)。
#
# 生产服务本就跑在这个仓里, 算出来的值与原先写死的字符串**逐字相同 → 生产零行为变化**;
# 只有 worktree / CI 会正确落到各自本地。
JARVIS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# User-only 节:set() 拒写
_READONLY_SECTIONS = ("safety", "credentials")

# User-only 单键(medium-4):小美的隐私姿态键不可被模型经 config_set 降级
# (如 private → auto 会让 ears 内容上云)。这些键只能由 User 显式操作改:
# config.set(..., _privileged=True)(ears.privacy CLI 走它)或直接编辑配置文件。
# 模型的 config_set 工具永不传 _privileged → 落到 PermissionError, 即"模型只读"。
_READONLY_KEYS = ("ears.privacy_default", "ears.allow_cloud", "ears.speaker_gate",
                  # eyes(常驻视觉)隐私姿态/上云开关: 看屏内容比音频更敏感(密码/银行/证件),
                  # 绝不能让模型经 config_set 把 private→auto 或开 allow_cloud(等于让屏幕正文上云)。
                  "eyes.privacy_default", "eyes.allow_cloud",
                  "hive.agent_permission",
                  # hive: dry/enabled/expose_tools 是"是否真起云端子代理蜂群"的开关,
                  # 模型经 config_set 翻 dry=False 可让普通 hive_spawn 自动升 live(白烧资源+证据上云)。
                  # 锁死 → 只 User 能改, 模型改不了。
                  "hive.dry", "hive.enabled", "hive.expose_tools", "hive.live_max_per_day",
                  # swarm_brain Phase1 预算闸/防递归三闸/胶囊上界(swarm_guard): 这些是"军团"烧钱与
                  # 递归爆炸的 hard cap。模型经 config_set 翻大任一上限 = 旁路烧钱/失控闸。锁死 → 只 User
                  # 能改(往更小调可由 budget 命令做, 但 hard ceiling 本身只 User)。
                  "hive.budget_max_tokens", "hive.budget_max_usd", "hive.budget_max_wall_seconds",
                  "hive.max_depth", "hive.max_fanout", "hive.max_lineage_total",
                  "hive.capsule_permission_ceiling",
                  # agent_swarm(专家军团编排器)enabled/expose_tools: 同 hive, 是"挂不挂军团工具面"开关。
                  # 锁死 → 模型经 config_set 改不动(只 User), 与 hive 对称(2026-06-06)。
                  "agent_swarm.enabled", "agent_swarm.expose_tools",
                  # simulation_lab(行动仿真沙盒)enabled/expose_tools: 同 agent_swarm, 是"挂不挂仿真工具面"开关。
                  # 锁死 → 模型经 config_set 改不动(只 User), 防自行关掉仿真护栏面(2026-06-06)。
                  "simulation_lab.enabled", "simulation_lab.expose_tools",
                  # epistemic_core(信念账本与不确定性校准)enabled/expose_tools/guard_enabled/ask_threshold:
                  # 同 simulation_lab, 是"挂不挂认知工具面"+必问闸总开关与阈值。锁死 → 模型经 config_set
                  # 改不动(只 User), 防自行关掉/调松必问护栏(关 guard 或把 ask_threshold 调到 0)(2026-06-06)。
                  "epistemic_core.enabled", "epistemic_core.expose_tools",
                  "epistemic_core.guard_enabled", "epistemic_core.ask_threshold",
                  # nexus 红线 4: 「某渠道免确认自动发送」白名单是对外发送的最危险开关,
                  # 绝不能让模型经 config_set 自行打开(否则等于绕过确认门自动发给别人)。
                  # 只能 User 显式 _privileged 或手改文件 → 模型只读。
                  "nexus.outbound_auto_send_whitelist",
                  # 小帅(监军)开关: 小美 model 绝不能经 config_set 关掉/弱化看管自己的监军
                  # (否则关掉 enabled 即静默缴械监军, 把未审进化堆进队列无人验)。小帅终验 w7021g61k 发现。
                  # 只 User 能改(_privileged / 手改文件)。
                  "xiaoshuai.enabled", "xiaoshuai.fix_max_rounds", "xiaoshuai.ccc_timeout",
                  # 小帅代批行动审批总开关(User 2026-06-06 授权: 付钱/删资料/对外/红线留 User,
                  # 其余小帅自动批)。模型绝不能经 config_set 自行打开(= 给自己的副作用步骤开闸)
                  # 或关掉。只 User 手改文件。
                  "xiaoshuai.auto_approve_actions",
                  # 影子录制/自主执行开关(E2 process_miner): 模型绝不能经 config_set 自授权录 User 操作
                  # 或自动执行学到的自动化。只 User 用 `observe --on` CLI(_privileged)或手改文件。E2 验证 w0k1sn3nx 发现。
                  "process_miner.enabled", "process_miner.auto_mine", "process_miner.schedule_execute",
                  # 流隐私打标(底座③ privacy_stream): 关流隐私扫描 / 允许高敏感(OTP/凭证/定位/卡号)上云,
                  # 都是把隐私正文往云端漏的危险开关。只 User 能改, 模型 config_set 改不动。
                  "privacy.stream_guard_enabled", "privacy.allow_cloud_high",
                  # awareness(常驻多模态意识流): 隐私姿态/上云开关/麦克风开关/隐私区(自动暂停)——
                  # 比单模态更敏感(屏幕+剪贴板+终端+麦克风全在一处)。绝不能让模型经 config_set 把
                  # private→auto、开 allow_cloud、私自开麦克风、或删隐私区(等于让敏感感知外送)。
                  # 锁死 → 只 User 能改(_privileged / 手改文件)。对齐 eyes/ears 隐私键铁律。
                  "awareness.privacy_default", "awareness.allow_cloud",
                  "awareness.mic_enabled", "awareness.privacy_zones",
                  # mail_unsub 红线4: mailto 退订=以 User 身份 SMTP 真发一封信给对方,触"影响他人"。
                  # 绝不能让模型经 config_set 自行打开自动发(否则等于绕过确认门代 User 对外发信)。
                  # 只 User 显式 _privileged / 手改文件。默认 False → mailto 退订只收集不发。
                  "mail_unsub.auto_send_mailto")

# get() 的「未传 default」哨兵(区分「没传」和「显式传 None」)
class _Sentinel:
    __slots__ = ()
    def __repr__(self) -> str:  # noqa: D401
        return "<no-default>"


_SENTINEL = _Sentinel()

# --------------------------------------------------------------------------- #
# 全量内嵌默认(= config.toml 当前所有值的镜像)。
# 向后兼容的最后兜底:即使 toml 文件整个不存在,get 仍返回正确默认。
# --------------------------------------------------------------------------- #
_DEFAULTS: dict = {
    "schema_version": 1,
    "brain": {
        "cpa_base": "http://127.0.0.1:8317/v1",
        "cpa_model": "gpt-6-astra",
        "cpa_api": "auto",  # Astra 用 Responses，旧模型/oMLX 保留原协议
        "astra_reasoning_effort": "low",
        "astra_max_output_tokens": 8192,
        # 2026-08-15 实证修正: :8008 已被 embedding_server.py 的 bge-m3 顶掉
        # (只供 /v1/embeddings), 真 oMLX app(chat/vision 都通)在 :8077。
        # registry._omlx_chat_ok 是精确 id 比对, 端口和模型名必须同时对,
        # 只改一个探针照样红。
        "omlx_base": "http://127.0.0.1:8077/v1",
        # 云端全挂时的最终兜底脑(不在交互热路径), KPI 是「能不能顺畅跑完
        # _run_tool_loop」, 不是最难编程题得分。选 "Qwen3.8-27B-8bit" 的理由:
        # 它是主线程在 :8077 这个确切端点上唯一实测过的 chat 模型(加载 6s、
        # 12.7 tok/s、图文都通), 且是最新代。旧注释想要的 "Qwen3.6-35B-A3B-8bit"
        # 在这台 oMLX 上根本不存在(实查 26 个 id 全量确认没有这一条) —— 已验证
        # 的确定性 > 未验证的 tok/s。备选:
        #   - "Qwen3.6-27B-8bit"        全参激活更稳, tool_calls JSON 稳定性回退时改这个
        #   - "Qwen3.6-35B-A3B-MLX-VL-oQ8-FP16"  要速度时试(A3B 只激活 3B, VL 变体,
        #     tool_calls 稳定性未验证)
        "omlx_model": "Qwen3.8-27B-8bit",
        "omlx_key": "local-dev-key",
        # oMLX 的 chat template 默认开思考(enable_thinking is undefined or is true)。
        # 实测同一道题 194.1s/2000tok(没答完) vs 12.1s/114tok(答对), 快 16 倍。
        # jarvis 从未主动传过 chat_template_kwargs, 故默认显式关掉; 置 True 可退回。
        "omlx_thinking": False,
        "max_tool_rounds": 10,
        "recent_turns": 12,
        "temperature": 0.8,
        "http_timeout": 120,
        "provider": "cpa",
        "anthropic_base": "https://api.anthropic.com/v1",
        "anthropic_model": "claude-opus-5",
        "anthropic_key_path": "~/.jarvis/.anthropic_key",
        "anthropic_max_tokens": 8192,
        "claude_cli_bin": "~/.local/bin/claude",
        "claude_cli_model": "",
        "claude_cli_timeout": 30,
        "claude_cli_safe_mode": True,   # 兼容保留旗标(--safe-mode 已被 claude 2.1.x 移除, 不再注入)
        "claude_cli_oauth_token_path": "",  # headless: OAuth token 文件(sk-ant-oat01-...)覆盖钥匙串; 空=用钥匙串
        "claude_cli_proxy": "",             # headless: claude 子进程专用代理(如 http://127.0.0.1:7897 绕地理封锁); 空=不设
        "claude_cli_home": "",              # 提速: claude 子进程中性 HOME(无全局 CLAUDE.md/settings/hooks); 实测 16s→3s; 空=用真实 $HOME
    },
    "persona": {
        "soul_path": os.path.join(JARVIS_ROOT, "SOUL.md"),
        "default_emo": "认真",
        "emo_words": ["温柔", "开心", "俏皮", "认真", "轻声", "心疼", "安静"],
        "version": "3.0",   # 里程碑大.小版本(手动); 完整版本号见 brain/version.py(+进化数自动)
    },
    "voice": {
        "tts_backend": "kokoro",
        "say_voice": "Tingting",
        "fish_omlx_url": "http://127.0.0.1:8008",
        "fish_voice": "default",
        "fish_model": "fish-audio-s2-pro-8bit",
        "mic_index": "",
        # ASR 现走 in-process mlx_whisper(whisper-large-v3-turbo, 见
        # voice/jarvis_listen.transcribe_samples / brain/ears_voice._transcribe_pcm);
        # oMLX :8008 已被 bge-m3 嵌入服务顶掉(仅 /v1/embeddings, 无 ASR 端点), 此值只在
        # 未来 :8008 复挂 ASR 的 HTTP 兜底路径用, 不再是实际转写模型。
        "asr_model": "Qwen3-ASR-1.7B",
        "vad_max_secs": 12.0,
        "vad_start_timeout": 6.0,
        "vad_silence_tail": 0.9,
        "kokoro": {
            "dir": "~/.omlx/models/Kokoro-82M-bf16",
            "voice": "zf_xiaoxiao",
            "port": 8351,
            "url": "http://127.0.0.1:8351",
            "python": "python3",
        },
        # GPT-Live 云端实时语音(brain/codex_live.py)。骑 Codex 订阅零额外付费, 但会把
        # **麦克风原始音频直传 OpenAI** —— 与小美原有"音频永不上云"的本地铁律相反,
        # 故 **默认 False**, 必须 User 显式开。开了也只作用于 explorer 网页里那个按需
        # 起停的前台通道, 不碰 ears 常开守护(那套仍是全本地 + _is_local_url 硬闸)。
        "codex_live": {
            "enabled": False,
            "voice": "marin",       # v2 默认音色; 可选见 codex_live status 的 listVoices
        },
    },
    "vision": {
        "vlm_service": "http://127.0.0.1:8009",
        "vlm_service_port": 8009,
        # 看屏 VLM。Qwen3-VL 原生 GUI grounding(认控件+懂功能), 取代 Qwen2.5-VL-7B。
        # ★ 换模型必看 vlm_server.py 的「bbox 坐标空间铁律」: Qwen3-VL 系吐 0-1000
        #   归一化坐标, Qwen2.5-VL 系吐绝对像素, 用错会让 click_on 系统性点偏。
        #   退回旧模型只需把这行改回 Qwen2.5-VL-7B-Instruct-4bit, 坐标空间自动跟着变。
        "vlm_service_path": "~/.omlx/models/Qwen3-VL-30B-A3B-Instruct-MLX-8bit",
        # bbox 坐标空间: "auto"(按模型名判) | "pixel" | "norm1000"。表外模型才需手写。
        "vlm_bbox_space": "auto",
        "vlm_service_timeout": 90,
        "vlm_send_width": 1280,
        "vlm_model": "dots.mocr-4bit",
        "locate_model": "LocateAnything-3B-4bit",
        "locate_engine_dir": "~/.omlx/models/LocateAnything-3B-8bit",
        "default_scale": 2.0,
        "http_timeout": 120,
    },
    "evolve": {
        "interval": 1800,
        "max_per_day": 0,
        "job_timeout": 1200,
        "panel_port": 8352,
        "dry": False,
    },
    "ccc": {
        "bin": "~/.local/bin/claude",
        "default_timeout": 1200,
        "max_output": 16000,
        "ultracode": True,
        "ultracode_effort": "xhigh",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8350,
    },
    "live": {
        "proxy": "http://127.0.0.1:7897",
        "http_timeout": 12,
        "default_city": "Shanghai",
    },
    "insight": {
        "city": "Shanghai",
        "dedup_ttl_days": 7,
        "max_insights": 3,
        "feishu_title": "小美洞察",
    },
    "sentinel": {
        # 环境感知与主动任务引擎(phase-1: 只感知+提议, 绝不执行任何动作)。
        "enabled": False,                # 总开关; 默认关(开了守护才周期感知)
        "interval": 600,                 # 守护感知节拍(秒); 感知低频, 默认 10 分钟
        "dir": "~/.jarvis/sentinel",     # 本机数据目录(建议流 jsonl, 不进 repo)
        "signal_window_hours": 24,       # collect 取近 N 小时信号
        "calendar_days": 3,              # 看未来几天日程
        "due_within_hours": 72,          # 快到期待办的窗口
        "imminent_event_hours": 12,      # 日程「临近」阈值(未来 N 小时内提醒准备)
        "stale_todo_days": 7,            # 待办拖过这么多天算"拖太久"
        "file_change_threshold": 30,     # 文件变更数超此值算"大量变更"
        "max_suggestions": 5,            # 一轮最多产几条建议
        "use_llm": True,                 # 规则初筛后是否再用本机轻量 LLM 复筛(挂了自动退规则)
        "dedup_ttl_days": 7,             # 同一条建议 N 天内不重复
        "push_feishu": False,            # 是否把新建议推飞书提醒(默认关; 只写 jsonl)
        "feishu_title": "小美感知",       # 推送标题(复用 insight 同款飞书机制)
    },
    "reminder": {
        "within_hours": 2,
    },
    "proactive_scanner": {
        # 主动事务扫描器 — 周期读三源(work_hub 逾期/followups 超期/sentinel 预警)
        # 生成"建议动作"写入 ap_approvals(status='pending', 绝不自动执行)。
        "enabled": True,                     # 总开关; 关掉则守护跳过, 零入队
        "interval": 3600,                    # 守护扫描节拍(秒); 默认 1 小时
        "suppress_window_h": 48,             # 同一建议抑制窗口(小时); 窗口内不重复入队
        "max_repush": 1,                     # 同一指纹累计提醒上限, 达到后永久静默
                                             # (只有实体状态/严重度变化 → 新指纹才再提醒);
                                             # 0=不限次(旧行为, 陈旧账本会每窗口刷一轮)
        "notify_mode": "digest",             # 飞书通知: digest=一轮一张汇总卡(默认, 反刷屏)
                                             # | per_item=逐条审批卡 | off=不推
        "followup_stale_days": 3,            # followups.pending(days=) 超期阈值
        "max_suggestions_per_scan": 10,      # 单轮最多入队数量(防灌水; cap 在去重之后)
        "max_suggestions_per_day": 10,       # 全天最多入队数量(防"每轮 10 条×24 轮"刷屏)
        "backlog_fold_threshold": 50,        # 单源候选超此数 → 折叠成一条"积压 N 条"告警,
                                             # 不再逐条提醒(账本烂了该报账本, 不是骚扰 N 次)
        "db_path": "~/.jarvis/proact_scanner.db",  # ps_seen 去重库(与 ap_approvals 库分离)
    },
    "autopilot": {
        "enabled": True,                 # 守护是否干活(关掉则只读不提议)
        "interval": 1800,                # 后台决策节拍(秒)
        "max_actions_per_day": 20,       # 每日提议/执行动作上限(0=不限)
        "autonomy": "suggest",           # suggest=只提议入队(默认最安全) | execute_safe=低风险可自动
        "require_approval_red_line": True,  # 高风险一律入队待批(绝不无人值守执行)
        "notify_mode": "digest",         # 飞书: digest=本轮末一张汇总卡(默认, User 2026-07-25
                                         # 拍板"改成汇总"; 逐条实测 2~14 张/天太吵)
                                         # | per_item=逐条推(旧行为, 带批准按钮) | off=不推
        "notify_min_interval_h": 4,       # digest 跨轮攒批窗口(小时): 距上次汇总不足此数不发,
                                         # 项目留在队列, 下次窗口一起发一张。0=每轮都发。
                                         # 血证: 只做单轮汇总实测仅降 21%(44/56 轮本就只 1 条)
        "notify_immediate_risk": "critical",  # 这些风险级立即单独推(带批准按钮), 不等攒批窗口
        "signal_window_hours": 24,       # 采集近 N 小时信号
        "calendar_days": 2,              # 看未来几天日程
        "constraints": [],               # User 登记的约束(纯字符串列表)
        "forbidden_zones": [],           # 禁区: 命中即强制入队待批
        "approval_rules": [],            # 须先批准的动作关键词
    },
    "mission_control": {
        # 自主执行中枢: 只规划/分解/排程/按偏好决策, 执行 100% 委托 autopilot 受控队列。
        # 本模块绝不直接执行任何有副作用动作; advance 也只是入 autopilot 队列(高危待 User 批)。
        "enabled": False,                # 跨天守护是否干活(默认关; 关掉则守护每轮跳过, 零动作)
        "interval": 3600,                # 跨天守护决策节拍(秒)
        "db_path": "~/.jarvis/mission/mission.db",  # 自有库, 与主库/autopilot 库分离
    },
    "xiaoshuai": {
        # 小美监军: 小美自主进化的代码上线前, 用 ccc ultracode 对抗验证 → 绿灯自动激活 /
        # 红灯自动回滚 / 擦边升级 User。独立于小美(verifier 不 import 小美脑)。
        "enabled": False,                # 守护是否消费待审队列(默认关; 关则只睡不验)
        "port": 8360,                    # backend HTTP(仅 127.0.0.1)
        "interval": 30,                  # 守护轮询队列节拍(秒)
        "dir": "~/.jarvis/xiaoshuai",    # 库/队列/审计/badge 根目录
        "ccc_timeout": 1200,             # 单次 ccc 验证超时(秒)
        "fix_max_rounds": 1,             # FIX_FIRST 自动修上限(User 定 1 轮)
        "push_feishu": True,             # 告警推飞书
    },
    "ears": {
        "enabled": True,                 # 常驻听觉总开关(关掉则不监听)
        "dir": "~/.jarvis/ears",         # 本机数据目录(库+事件流, 不进 repo)
        "db_path": "~/.jarvis/ears/ears.db",
        "events_path": "~/.jarvis/ears/events.jsonl",  # sentinel/autopilot 可消费的音频事件流
        "python": "",                    # 守护用的 python(空=回退 voice.kokoro.python/myenv)
        "sources": ["mic"],              # 采集源: mic(本机麦, 默认) | system(需 loopback 设备)
        "mic_index": "",                 # avfoundation 麦设备号(空=自动选本机麦, 避开 iPhone)
        "system_device": "",             # 系统音频设备号(需 BlackHole/聚合设备; 空=不采系统音)
        "wake_words": ["小美", "jarvis"], # 唤醒词(命中即发 wake 事件)
        "privacy_default": "auto",       # auto=命中敏感词才隐私 | private=全部段当隐私(最保守)
        # User 2026-06-03 显式授权: True = ears 录的内容(含第三方)可走云端主脑(快),
        # 逐字进 events.jsonl + 不再强制本机。False(默认/最稳)= 永远本机、只存计数。
        # 仅 User 可改(在 _READONLY_KEYS), 防模型/注入自行开云端。
        "allow_cloud": False,
        # 声纹门(ears_voice 全双工对话): True(默认)= 已登记 User 声纹时只处理 User 的话,
        # 旁人/电视音频当空段丢弃(内存判定, 不落盘); **未登记则门不激活、放行所有**(避免 enroll
        # 前哑巴)。False = 门全关(放行所有说话人)。在 _READONLY_KEYS: 只 User 能改
        # (模型/注入不能经 config_set 关门降级隐私)。登记: `jarvis ambient enroll`。
        "speaker_gate": True,
        "vad_max_secs": 30.0,            # 单句最长(防跑飞)
        "vad_start_timeout": 3.0,        # 多久没人说话就重听一轮(顺便检查会话收尾)
        "vad_silence_tail": 0.9,         # 说完停顿多久算一句结束
        "session_gap_secs": 180.0,       # 静默超过这么久 → 上一段会议/通话收尾
    },
    "nexus": {
        "enabled": False,                 # 外部数据总线总开关(默认关; 开了才拉/起草)
        # 各连接器开关(inbound 拉取 + outbound 起草 是否对该服务生效)
        "gmail_enabled": False,
        "feishu_enabled": False,
        "outlook_enabled": False,
        "notion_enabled": False,
        "slack_enabled": False,
        "github_enabled": False,
        "wecom_enabled": False,
        "wechat_enabled": False,
        "sync_interval": 1800,            # inbound 自动同步节拍(秒; 守护用, 现为预留)
        "inbound_to_context_brain": True, # inbound 拉进来后是否喂情境大脑(读放开)
        "inbox_read_limit": 30,           # nexus_read 默认返回条数
        # 红线 4 最危险开关: 「某渠道免确认自动发送」白名单。空 = 一切对外发送都过确认门。
        # 在 _READONLY_KEYS → 模型/config_set 永不能改, 只 User 手动。默认空(最安全)。
        "outbound_auto_send_whitelist": [],
    },
    "mail_unsub": {
        "enabled": True,                  # 邮件退订能力总开关
        "scan_limit": 800,                # 每账号只读扫描最近 N 封找营销发件人
        # 红线4: mailto 退订=以 User 身份 SMTP 发信给对方。False(默认)=只收集不发;
        # True=自动发。该键在 _READONLY_KEYS → 模型改不动, 只 User 手动拨。
        "auto_send_mailto": False,
    },
    "safety": {
        "guard_enabled": True,
        "shell_timeout": 120,
        "max_output": 12000,
        "safe_word": "安全口令",
        "computer_max_steps": 6,
        # 隐私授权: True = 隐私文件正文可交云端主脑/Claude Code 处理(求最佳效果);
        # False(历史默认)= 隐私正文只在本机 oMLX、不出本机。仅 User 可改(safety 段只读)。
        "allow_cloud_private": True,
        # 数据层隐私模型总开关(User 2026-07-08 显式: 放弃数据层隐私模型)。
        # False(默认=已放弃): 小美读/存/上云一律原文, _private/_force_local 不再拦内容上云、
        #   不再切本机 oMLX。True: 恢复历史数据层隐私(private 看 allow_cloud_private; force_local 强制本机)。
        # 铁律: 只管数据层。对外发送/动作出口(红线4)+付款/删文件审批不受此开关影响。仅 User 可改。
        "data_layer_privacy_enabled": False,
    },
    "data": {
        "jarvis_db": os.path.join(JARVIS_ROOT, "brain", "jarvis.db"),
        "index_db": "~/.jarvis/index.db",
        "snippet_max": 240,
    },
    "skillforge": {
        "enabled": True,            # 记活动日志 + 发现/合成技能开关
        "data_dir": "~/.jarvis/skillforge",  # 技能库 + 活动日志(本机, 不进 repo)
        "min_occurrences": 3,       # 一个工作流反复出现几次才算候选
        "lookback_days": 30,        # 发现时回看多少天日志
        "max_events": 5000,         # 活动日志读取上限
        "auto_load_approved": True, # 已批准技能自动挂进大脑工具集
    },
    "workflow_learner": {
        # 示范学习与工作流自动生成器(observe/mine/synthesize/test/replay/register/...)。
        # 它是 process_miner + skillforge 之上的薄门面: 影子录制总开关其实是 process_miner.enabled
        # (默认 False = 影子模式默认关, User 用 `observe --on` 才显式授权)。本节只放门面自身配置。
        "enabled": True,            # 门面/命令是否可用(非影子录制开关)
        "data_dir": "~/.jarvis/workflow_learner",  # 生成脚本产物目录(本机, 不进 repo)
        "shadow_default_off": True, # 文档/UI 提示: 影子录制默认关, 需显式授权
    },
    "gep": {
        "enabled": True,                # 经验胶囊总开关
        "select_k": 3,                  # 每轮注入 top-k capsule
        "score_start": 0.30,            # 新建 capsule 效果分起步(对齐 instinct 0.3)
        "distill_max_per_run": 6,       # 单次异步自学最多提炼几条
        "distill_interval_turns": 20,   # 每 N 轮触发一次自学(调度器用)
        "dedup_threshold": 0.85,        # 文本相似 ≥ 此值视为重复
        "capsule_db": "~/.jarvis/capsules.db",  # 独立库,不污染 jarvis.db
    },
    "memory": {
        # 专属语义长期记忆(关于 User 的事实/偏好/人物/项目/她对自己的认知)。
        # 铁律: 记忆全部放本地 —— 库在本机 ~/.jarvis/memory/memory.db, 绝不同步云端;
        # 向量本机算(semantic.embed_text); private 记忆仅在 allow_cloud_private 授权下注入云端主脑。
        "enabled": True,                # 总开关(chat 注入 + 工具)
        "dir": "~/.jarvis/memory",      # 本机库目录(独立, 不污染 jarvis.db)
        "recall_k": 5,                  # chat/工具默认取回条数
        "dedup_threshold": 0.88,        # 新记忆与旧记忆余弦 ≥ 此值 → 合并强化(不新增)
        "decay_factor": 0.97,           # 久未命中记忆重要性衰减系数(不清零不删)
    },
    "appsmith": {
        "enabled": True,                # 万能应用接口学习与控制层总开关
        "data_dir": "~/.jarvis/appsmith",  # adapter 库(本机, 不进 repo)
        "auto_load_approved": True,     # 已批准 adapter 自动挂进大脑工具集
        "scan_app_dirs": "/Applications:/System/Applications:~/Applications",  # discover 扫这些(:分隔)
        "max_apps": 400,                # discover 扫描 app 上限
    },
    "app_control_bus": {
        "enabled": True,                # Mac 全应用结构化控制总线总开关
        "data_dir": "~/.jarvis/app_control_bus",  # 动作模型画像/宏/重放(本机, 不进 repo)
        "scan_app_dirs": "/Applications:/System/Applications:~/Applications",  # discover 扫这些(:分隔)
        "max_apps": 400,                # discover 扫描 app 上限
        "osa_timeout": 25,              # 只读 osascript 自省超时(秒; 执行走 local_powers 60s 门)
    },
    "hive": {
        "enabled": True,                # 并行分身蜂群总开关
        "dry": True,                    # 默认空跑(只编排出计划/入审批队列, 不真派云端子代理); --live 或设 False 才真跑
        "expose_tools": True,           # 把 hive_spawn/status/report 挂进大脑工具集
        "data_dir": "~/.jarvis/hive",   # 本机数据目录(runs/黑板/库, 不进 repo)
        "db_path": "~/.jarvis/hive/hive.db",  # 独立库, 不污染 jarvis.db(对齐 gep/autopilot 做法)
        "agent_timeout": 1200,          # 单子代理超时秒(必须 > 0; 也作 wait 轮询上限推导)
        "agent_permission": "plan",     # claude_code 子代理权限档(plan=只读最安全); 提权 User-only
        "roles": ["researcher", "planner", "executor", "coder",
                  "auditor", "opponent", "summarizer"],
    },
    "awareness": {
        # 常驻多模态意识流与情境黑匣子(jarvis awareness_daemon)。全本机, 数据落
        # ~/.jarvis/awareness/, 绝不联网外传。隐私键(privacy_default/allow_cloud/
        # mic_enabled/privacy_zones)在 _READONLY_KEYS → 模型 config_set 改不动, 只 User。
        "enabled": True,                 # 总开关
        "interval": 6.0,                 # 感知节拍(秒)
        "retention_hours": 72,           # 黑匣子细节正文保留期(过期自动清, 只留摘要骨架)
        "keyframe_secs": 180,            # 无显著变化也每隔这么久落一关键帧
        "privacy_default": "auto",       # auto=命中敏感词/私密应用才隐私 | private=一律隐私(最保守)
        "allow_cloud": False,            # False=隐私情境只本机 oMLX 收尾, 不出本机(对齐 eyes/ears)
        "mic_enabled": False,            # 麦克风默认关; 开了也只经 ears 读且一律 force_local
        # 各模态传感器开关(默认开, mic 除外)
        "sense_screen": True, "sense_clipboard": True, "sense_files": True,
        "sense_terminal": True, "sense_notifications": True, "sense_calendar": True,
        # 推送(顾问/知识面只读 ingest; 行动面一律入 autopilot 人审批队列, 绝不自动执行)
        "push_enabled": True,            # 总推送开关
        "push_world_model": True,        # 把脱敏观察喂 world_model(实体/关系)
        "push_context_brain": True,      # 周期刷新 context_brain ingest
        "push_autopilot": True,          # 触发器入 autopilot 审批队列(默认只发 'note' 顾问)
        "push_insight": False,           # 驱动 insight(会用 LLM/配额)默认关
        "propose_actions": False,        # 默认连可执行提议都不发(纯顾问); 开了才发 computer_use/agent 提议(仍 User 审)
        "max_pushes_per_day": 30,        # 推送日上限(防刷)
        "idle_threshold": 120,           # 空闲判定阈值(秒)
        "task_min_secs": 90,             # 焦点持续多久算"成型任务"(任务/打断检测用)
        "calendar_days": 3,              # 日历前瞻天数
        "imminent_hours": 12,            # 标"临近"的小时窗
        # privacy_zones 默认空(代码内置默认隐私区: 1Password/网银/钥匙串等); User 可经
        # `jarvis awareness_daemon privacy_zone add <pattern>` 增删(_privileged 写)。
        "privacy_zones": [],
    },
    "credentials": {
        "localkey_path": "~/.jarvis/.localkey",
        "gcal_creds": os.path.join(JARVIS_ROOT, "brain", ".gcal_creds.json"),
        "gmail_creds": os.path.join(JARVIS_ROOT, "brain", ".gmail_creds.json"),
        "feishu_script": "~/.claude/scripts/feishu-notify.sh",
    },
}

# --------------------------------------------------------------------------- #
# 校验:枚举 / 范围(只对已知 key 收紧,未知 key 放行)
# --------------------------------------------------------------------------- #
# 枚举型 key(点路径 → 允许值集合)
_ENUMS = {
    "voice.tts_backend": {"kokoro", "say", "fish"},
    "autopilot.autonomy": {"suggest", "execute_safe"},
    "autopilot.notify_mode": {"digest", "per_item", "off"},
    "proactive_scanner.notify_mode": {"digest", "per_item", "off"},
    "ears.privacy_default": {"auto", "private"},
    "awareness.privacy_default": {"auto", "private"},
    "hive.agent_permission": {"plan", "default", "acceptEdits", "bypassPermissions"},
    "brain.provider": {"anthropic", "cpa", "omlx", "claude_cli"},
    "brain.cpa_api": {"auto", "responses", "chat_completions"},
    "brain.astra_reasoning_effort": {"low", "medium", "high", "xhigh", "max"},
}
# 端口型 key(点路径)→ 走 1024–65535 范围
_PORT_KEYS = {
    "voice.kokoro.port",
    "vision.vlm_service_port",
    "evolve.panel_port",
    "server.port",
}
# 必须 > 0 的 key(interval / 各 timeout / 各 max 等)
_POSITIVE_KEYS = {
    "brain.max_tool_rounds",
    "brain.astra_max_output_tokens",
    "brain.recent_turns",
    "brain.http_timeout",
    "vision.vlm_service_timeout",
    "vision.vlm_send_width",
    "vision.http_timeout",
    "evolve.interval",
    "evolve.job_timeout",
    "ccc.default_timeout",
    "ccc.max_output",
    "live.http_timeout",
    "insight.dedup_ttl_days",
    "insight.max_insights",
    "reminder.within_hours",
    "autopilot.interval",          # max_actions_per_day 不在此: 0=不限(同 evolve.max_per_day)
    "autopilot.signal_window_hours",
    "autopilot.calendar_days",
    "safety.shell_timeout",
    "safety.max_output",
    "safety.computer_max_steps",
    "data.snippet_max",
    "appsmith.max_apps",           # discover 扫描上限, 必须 > 0(否则 _scan 立即返回空)
    "hive.agent_timeout",          # 蜂群单子代理超时秒, 必须 > 0
    "nexus.sync_interval",         # 外部总线同步节拍秒, 必须 > 0
    "nexus.inbox_read_limit",      # inbox 读取条数上限, 必须 > 0
    "awareness.interval",          # 意识流感知节拍秒, 必须 > 0(否则 24/7 守护 busy-spin)
    "awareness.retention_hours",   # 黑匣子保留期, 必须 > 0
    "awareness.keyframe_secs",     # 关键帧节拍, 必须 > 0
    "awareness.task_min_secs",     # 任务成型阈值, 必须 > 0
    "brain.anthropic_max_tokens",  # Anthropic max_tokens 必须 > 0 (API required field)
    "brain.claude_cli_timeout",   # claude CLI 超时秒, 必须 > 0
}

# --------------------------------------------------------------------------- #
# 进程内缓存(daemon 多线程安全)
# --------------------------------------------------------------------------- #
_LOCK = threading.RLock()
_CACHE: dict = {}      # 已解析的用户 toml(空 dict = 文件不存在/坏 → 全走 _DEFAULTS)
_MTIME: float = -1.0   # 上次加载时文件的 mtime;-1 = 还没加载过


# --------------------------------------------------------------------------- #
# 内部:加载 / 合并 / 取值
# --------------------------------------------------------------------------- #
def _load() -> None:
    """读 CONFIG_PATH 进 _CACHE。
    文件不存在 → _CACHE = {}(全走默认);解析失败 → log 到 stderr + 退回默认。
    绝不抛:坏配置不能炸大脑。调用方已持 _LOCK。
    """
    global _CACHE, _MTIME
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        # 文件不存在 → 全走 _DEFAULTS
        _CACHE = {}
        _MTIME = -1.0
        return
    if tomllib is None:
        # py<3.11 无 tomllib(CI 3.9 矩阵): 配置文件不解析, 全走 _DEFAULTS
        sys.stderr.write(f"[config] 本 Python 无 tomllib, 忽略 {CONFIG_PATH}, 全走内嵌默认\n")
        _CACHE = {}
        _MTIME = mtime
        return
    try:
        with open(CONFIG_PATH, "rb") as f:
            _CACHE = tomllib.load(f)
        _MTIME = mtime
    except (tomllib.TOMLDecodeError, OSError, ValueError) as e:
        sys.stderr.write(f"[config] 解析 {CONFIG_PATH} 失败,退回内嵌默认: {e}\n")
        _CACHE = {}
        _MTIME = mtime  # 记下 mtime,等文件被改好再重读


def _ensure_loaded() -> None:
    """mtime 自动失效:文件变了就重读。调用方已持 _LOCK。"""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        if _CACHE:
            # 文件曾在、现没了 → 清缓存退回默认
            _load()
        elif _MTIME != -1.0:
            _load()
        return
    if mtime != _MTIME:
        _load()


def _dig(d: dict, parts: list) -> tuple:
    """在嵌套 dict 里按 parts 下钻。返回 (found: bool, value)。"""
    cur = d
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return False, None
    return True, cur


def _deep_merge(base: dict, over: dict) -> dict:
    """递归合并:over 覆盖 base,嵌套 dict 逐层合并。返回新 dict(不改入参)。"""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _validate(path: str, value) -> None:
    """对已知 key 校验类型/枚举/范围,不合法抛 ValueError(消息可读)。
    未知 key:放行(前向兼容),只 warn 到 stderr。
    """
    parts = path.split(".")
    found, default_val = _dig(_DEFAULTS, parts)

    if not found:
        sys.stderr.write(f"[config] 未知配置项 {path},仍写入(前向兼容)。\n")
        return

    # 类型校验:与内嵌默认同类(bool 必须严格;int/float 互通,因为 toml int 可喂 float 位)
    if isinstance(default_val, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{path} 需要 true/false(布尔),收到 {type(value).__name__}")
    elif isinstance(default_val, int) and not isinstance(default_val, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} 需要数字,收到 {type(value).__name__}")
    elif isinstance(default_val, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} 需要数字,收到 {type(value).__name__}")
    elif isinstance(default_val, str):
        if not isinstance(value, str):
            raise ValueError(f"{path} 需要字符串,收到 {type(value).__name__}")
    elif isinstance(default_val, list):
        if not isinstance(value, list):
            raise ValueError(f"{path} 需要数组,收到 {type(value).__name__}")
    elif isinstance(default_val, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{path} 是配置节,不能整节直写;请逐项 set。")

    # 枚举
    if path in _ENUMS and value not in _ENUMS[path]:
        allowed = "/".join(sorted(_ENUMS[path]))
        raise ValueError(f"{path} 只能是 {allowed},收到 {value!r}")

    # 端口范围
    if path in _PORT_KEYS:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{path} 端口需要整数,收到 {value!r}")
        if not (1024 <= value <= 65535):
            raise ValueError(f"{path} 端口要在 1024–65535 之间,收到 {value}")

    # temperature 0–2
    if path == "brain.temperature":
        if not (0.0 <= float(value) <= 2.0):
            raise ValueError(f"brain.temperature 要在 0–2 之间,收到 {value}")

    # 必须 > 0
    if path in _POSITIVE_KEYS:
        if float(value) <= 0:
            raise ValueError(f"{path} 必须 > 0,收到 {value}")


# --------------------------------------------------------------------------- #
# 极简 TOML 序列化器(只覆盖本配置用到的标量/字符串/数组/嵌套表)
# --------------------------------------------------------------------------- #
def _toml_value(v) -> str:
    """把单个标量/数组序列化成 TOML 右值字符串。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        # 避免科学计数法,保留普通小数表达
        s = repr(v)
        return s
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ValueError(f"不支持序列化的类型: {type(v).__name__}")


def _toml_str(s: str) -> str:
    """TOML 基本字符串:双引号 + 转义。"""
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return f'"{out}"'


def _dump_toml(data: dict) -> str:
    """把合并后的完整配置 dict 序列化成 TOML 文本(tomllib 能再读)。

    输出顺序:先顶层标量,再每个表(嵌套子表跟在父表后,用 [a.b] 形式)。
    只支持本配置出现的形状:顶层标量 + 一层表 + 表内可再嵌一层子表(voice.kokoro)。
    """
    lines = ["# ~/.jarvis/config.toml — 小美统一配置中心(由 brain/config.py 写回)。",
             "# safety / credentials 两节 User-only,小美 config_set 拒改。", ""]

    # 1) 顶层标量
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    for k, v in scalars.items():
        lines.append(f"{k} = {_toml_value(v)}")
    if scalars:
        lines.append("")

    # 2) 表(及其嵌套子表)
    for sect, body in data.items():
        if not isinstance(body, dict):
            continue
        lines.append(f"[{sect}]")
        # 表内标量先写
        sub_tables = {}
        for k, v in body.items():
            if isinstance(v, dict):
                sub_tables[k] = v
            else:
                lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
        # 嵌套子表(如 voice.kokoro)
        for sub_name, sub_body in sub_tables.items():
            lines.append(f"[{sect}.{sub_name}]")
            for k, v in sub_body.items():
                if isinstance(v, dict):
                    raise ValueError(f"不支持三层嵌套表: {sect}.{sub_name}.{k}")
                lines.append(f"{k} = {_toml_value(v)}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(text: str) -> None:
    """原子落盘:写临时文件 + os.replace。"""
    d = os.path.dirname(CONFIG_PATH)
    os.makedirs(d, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------- #
# 公开 API
# --------------------------------------------------------------------------- #
def get(path: str, default=_SENTINEL):
    """点路径取值,如 get("brain.cpa_model")。

    查找顺序:① 已加载的 toml(mtime 失效自动 reload)→ ② _DEFAULTS[path]
    → ③ 显式传的 default → ④ 抛 KeyError(仅当三者皆无)。

    向后兼容铁律:模块调用一律传 default = 自己原硬编码值。
    """
    parts = path.split(".")
    with _LOCK:
        _ensure_loaded()
        found, val = _dig(_CACHE, parts)
    if found:
        return val
    found, val = _dig(_DEFAULTS, parts)
    if found:
        return val
    if default is not _SENTINEL:
        return default
    raise KeyError(path)


def get_section(section: str) -> dict:
    """返回整节(默认 ⊕ 用户覆盖),如 get_section("voice")。
    节不存在 → 返回 {}(空表),不抛。
    """
    base = _DEFAULTS.get(section)
    base = base if isinstance(base, dict) else {}
    with _LOCK:
        _ensure_loaded()
        over = _CACHE.get(section)
    over = over if isinstance(over, dict) else {}
    return _deep_merge(base, over)


def set(path: str, value, _privileged: bool = False) -> None:  # noqa: A001 (有意复用 set 这个名字)
    """点路径写值 → 校验 → 更新内存 → 原子落盘。

    拒写 safety.* / credentials.*(抛 PermissionError);这两节只能 User 手改文件,
    小美的 self-manage 工具靠这个 PermissionError 实现「只读」。

    medium-4: 另拒写 _READONLY_KEYS(如 ears.privacy_default)——隐私姿态键不可被模型
    经 config_set 降级。User 显式操作(ears.privacy CLI 传 _privileged=True)或直接编辑
    配置文件才能改;模型的 config_set 工具永不传 _privileged → PermissionError。
    """
    parts = path.split(".")
    if not parts or not parts[0]:
        raise ValueError("空配置路径")
    if parts[0] in _READONLY_SECTIONS:
        raise PermissionError(
            f"[{parts[0]}] 是 User 专属节,小美只读;改它请直接编辑 {CONFIG_PATH}"
        )
    if path in _READONLY_KEYS and not _privileged:
        raise PermissionError(
            f"{path} 是 User 专属的隐私姿态键,小美不能改(防把语音内容降级上云);"
            f"改它请用 `jarvis ears privacy auto|private` 或直接编辑 {CONFIG_PATH}"
        )

    _validate(path, value)

    with _LOCK:
        _ensure_loaded()
        # 在「合并后完整配置」基础上改这一项,再整体写回(保留所有默认+其它用户值)
        merged = _all_locked()
        cur = merged
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
            cur = nxt
        cur[parts[-1]] = value
        _atomic_write(_dump_toml(merged))
        # 写回后立刻刷新缓存,使本进程后续 get 立即可见
        _load()


def all() -> dict:  # noqa: A001
    """返回完整合并配置(默认 ⊕ 用户覆盖),给 UI / jarvis doctor 用。"""
    with _LOCK:
        _ensure_loaded()
        return _all_locked()


def _all_locked() -> dict:
    """合并默认 + 用户覆盖(调用方已持 _LOCK)。"""
    return _deep_merge(_DEFAULTS, _CACHE)


def reload() -> None:
    """强制重读文件刷新缓存(get 已自带 mtime 自动 reload,这是显式版)。"""
    with _LOCK:
        _load()


def path() -> str:
    """返回 CONFIG_PATH(给 UI 显示)。"""
    return CONFIG_PATH


# --------------------------------------------------------------------------- #
# CLI(调试用):python3 config.py [get <path> | all | path]
# --------------------------------------------------------------------------- #
def _main(argv) -> int:
    import json
    if not argv or argv[0] == "all":
        print(json.dumps(all(), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "path":
        print(path())
        return 0
    if argv[0] == "get" and len(argv) >= 2:
        try:
            print(get(argv[1]))
        except KeyError:
            sys.stderr.write(f"没这个 key: {argv[1]}\n")
            return 1
        return 0
    if argv[0] == "set" and len(argv) >= 3:
        # 简易类型推断:true/false/数字/否则字符串
        raw = argv[2]
        if raw in ("true", "false"):
            val = raw == "true"
        else:
            try:
                val = int(raw)
            except ValueError:
                try:
                    val = float(raw)
                except ValueError:
                    val = raw
        try:
            set(argv[1], val)
            print(f"OK {argv[1]} = {val!r}")
        except (ValueError, PermissionError) as e:
            sys.stderr.write(f"{e}\n")
            return 1
        return 0
    sys.stderr.write("用法: config.py [all | path | get <path> | set <path> <value>]\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
