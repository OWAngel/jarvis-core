#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""local_powers.py — 小美的「本地行动力」: 在 User 这台 Mac 上真正动手。

给她最强的本地授权: 跑命令 / 读写移动文件 / 开 app·文件·网址 / 跑 AppleScript。
全本机, 纯 stdlib subprocess。

安全模型 (照 User 自己的 4 红线):
  默认 YES —— 绝大多数操作直接执行, 不问。
  唯独命中 4 红线的操作会被「确认门」拦住, 返回 needs_confirm, 要 User 一句「确认」
  (调用时带 confirmed=True) 才放行:
    1. 不可逆数据销毁: rm -rf 根/家/业务目录, DROP/TRUNCATE/无条件 DELETE, 格式化磁盘, 写裸盘
    2. 真实付钱: curl/wget 命中 payment/checkout/charge/subscribe/billing
    3. 影响他人: (本机暂无对外发送工具; 预留)
    4. 高危改写: git push --force, git reset --hard, sudo 提权

  门是给 User 兜底的, 不是限制他。想完全裸放(去掉门): 把 GUARD_ENABLED 设 False。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata

import config as _config

# 安全门开关 — 从 config.toml [safety] 节读取;缺失时回退原硬编码默认。
# 安全保守设计:模块级常量,改 guard_enabled 需重启 server 才生效
# (防止运行中被热关闭安全门)。只有 User 手改 config.toml 才能动这个值。
GUARD_ENABLED: bool = _config.get("safety.guard_enabled", True)

SHELL_TIMEOUT: int = _config.get("safety.shell_timeout", 120)   # 单条命令最长跑多久
MAX_OUTPUT: int = _config.get("safety.max_output", 12000)        # 回给大脑的 stdout/stderr 截断长度

# 会话级命令批准白名单(Hermes 借鉴 #23): User 本进程内经 confirmed=True 批过的**确切命令串**,
# 之后同一条免再问(减少反复批同一操作的打扰, 契合 AGGRESSIVE AUTONOMOUS 少问)。
# **默认关**(session_command_allowlist=false → 零行为变化, 每条红线仍逐次确认); User 显式
# 开启才生效。铁律: ① 只按**完全相同命令串**匹配(不做 pattern, 最保守)。② hardline 硬底线
# 永不进白名单、永不受影响(灾难销毁/凭据写即便批过也每次 fail-closed)。③ 进程存活期有效, 重启清零。
SESSION_ALLOWLIST_ENABLED: bool = _config.get("safety.session_command_allowlist", False)
_SESSION_APPROVED: set = set()

# --------------------------------------------------------------------------- #
# 4 红线 pattern (命中 = 需要 User 显式确认)
# --------------------------------------------------------------------------- #
_RED_LINES = [
    # 1. 不可逆数据销毁
    (r"\brm\s+-[rfRF]*[rR][fF]*\s+(/|~|\$HOME|/Users/\w+)\s*$", "rm -rf 根/家目录"),
    (r"\brm\s+-[rfRF]+\s+.*(workspace|核心业务|重要项目|客户资料|/Documents|/Desktop|业务|生意)", "rm -rf 业务/重要目录"),
    # 1b. 「绝不删 User 的文件」硬规矩(User 2026-06-04): 任何形式删文件都要 User 确认,
    #     自主路径(autopilot/hive 子代理/toolsmith 造的工具/eyes)一律删不了。宁可误拦不可误删。
    (r"\brm\s+\S", "rm 删除文件(绝不删 User 文件 → 需确认)"),
    (r"\b(unlink|shred|srm|trash|trash-put|rmtrash)\s+\S", "删除文件命令(unlink/shred/trash)"),
    (r"\bfind\b[\s\S]*\s-delete\b", "find -delete 批量删除"),
    (r"\b(os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree)\s*\(", "代码内删除文件(os.remove/unlink/rmtree)"),
    (r"\.unlink\(\s*(missing_ok\s*=\s*\w+\s*)?\)", "Path.unlink 删文件"),
    (r"\bmv\b[\s\S]*(\.Trash\b|/Trash\b)", "mv 移入废纸篓(等于删)"),
    (r"\bDROP\s+TABLE\b", "DROP TABLE 删表"),
    (r"\bTRUNCATE\b", "TRUNCATE 清空表"),
    (r"\bDELETE\s+FROM\b(?![\s\S]*\bWHERE\b)", "无 WHERE 的 DELETE(清空表)"),
    (r"\bmkfs\b|\bdiskutil\s+(erase|reformat)", "格式化磁盘"),
    (r">\s*/dev/(disk|sd|rdisk)", "写裸磁盘设备"),
    (r"\bdd\s+.*of=/dev/", "dd 写裸盘"),
    # 2. 真实付钱
    (r"\b(curl|wget|http)\b[\s\S]*(payment|checkout|/charge|subscribe|subscription|billing|/pay\b)", "可能触发真实付费"),
    # 2b. RCE 面(防自改/将来 toolsmith 拉任意代码执行): 装包 + 远程脚本管道执行 → 需确认
    (r"\b(pip[0-9.]*|pip3|npm|pnpm|yarn|gem|cargo|brew)\s+(install|add|i)\b", "装包(可能引入任意代码)"),
    (r"\b(curl|wget)\b[\s\S]*\|\s*(sh|bash|zsh|python[0-9.]*|node|ruby|perl)\b", "curl/wget | sh 远程脚本执行(RCE)"),
    # 4. 高危改写
    (r"\bgit\s+push\b[\s\S]*--force(?!-with-lease)", "git push --force"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard(可能丢未保存改动)"),
    (r"(^|\s)sudo\s", "sudo 提权"),
    # 5. 护栏篡改 / 监军(小帅)/ 数据外传(E2 对抗验证 w0k1sn3nx 补 —— 这些缺口影响整个活系统)
    (r"(>>?|\btee\b)\s*[^\n|]*\.jarvis/config\.toml", "shell 写 ~/.jarvis/config.toml(改护栏/隐私开关 → 走 config 工具不走 shell)"),
    (r"(?i)guard_enabled\s*[=:]\s*false", "关闭安全护栏 GUARD_ENABLED"),
    (r"(>>?|\btee\b|\bcp\b|\bmv\b|\bsed\b\s+-i)\s*[^\n|]*xiaoshuai/", "改动监军 xiaoshuai/(小帅永久白名单, 不可被碰)"),
    (r"\bcurl\b[^\n]*[-]{1,2}(F|form|T|T,|upload-file|data-binary|data)\b[^\n]*@[/~$]", "curl 上传本地文件到远程(数据外传)"),
    (r"\bscp\b\s+[^\n]*\s+\S+@\S+:", "scp 传文件到远程主机(数据外传)"),
    (r"\brsync\b[^\n]*\s+\S+@\S+:", "rsync 传到远程主机(数据外传)"),
    # 5b. 敏感写硬拦的 shell 形状(防绕过 write_file/move_file 的 _SENSITIVE_WRITE_RX, 改用
    #     run_shell `echo >> ~/.ssh/authorized_keys` 直写凭据/持久化)。写操作符 + 敏感目标。
    (r"(>>?|\btee\b|\bcp\b|\bmv\b|\binstall\b|\bln\b)\s*[^\n|]*"
     r"(\.ssh/|\.aws/|\.gnupg/|authorized_keys|\.git-credentials|\.netrc|"
     r"\.bashrc|\.zshrc|\.bash_profile|\.zprofile|\.zshenv|\.profile|"
     r"/LaunchAgents/|/LaunchDaemons/|sudoers|/\.env(\s|$|\.))",
     "shell 写 SSH/凭据/shell-rc/LaunchAgents 等敏感区(改走 write_file 也会被拦)"),
]
_RED_RX = [(re.compile(p, re.IGNORECASE), why) for p, why in _RED_LINES]

# --------------------------------------------------------------------------- #
# HARDLINE 硬底线 (fail-closed: 即便 confirmed=True 也拒绝 —— 连 User 一句确认也不放行)
# --------------------------------------------------------------------------- #
# 移植自 Hermes v0.18 approval.py 的 HARDLINE_PATTERNS vs DANGEROUS_PATTERNS 分层思路
# (2026-07-04 对比借鉴 #3, User 拍板灾难销毁类全部硬拦)。
#
# 分层动机: 原来「护栏篡改 / 凭据写 / 灾难销毁」全混在 _RED_LINES 里走 needs_confirm ——
# 一句 confirmed=True 就放行。但其中两类本该 fail-closed:
#   A. 护栏/监军篡改 + 凭据/持久化 shell 写: write_file 的 _SENSITIVE_WRITE_RX 早已对这些
#      路径 fail-closed(即便 confirmed 也拒), 但 run_shell 的 shell 形状只走 needs_confirm ——
#      `echo >> ~/.ssh/authorized_keys` 一句 confirmed 就绕过。这是 bug, 此处对齐成 fail-closed。
#   B. 灾难性不可逆销毁(rm -rf 根/家、mkfs、dd 写裸盘): 从不是 jarvis 的合法业务动作,
#      User 要格盘/刷盘自己在终端做 —— 连 User 经 CLI 的 confirmed 也不该让小美代跑(User 2026-07-04)。
#
# 边界诚实(威胁模型就地声明, 借鉴 Hermes file_safety 每函数 docstring 的做法):
#   这层挡的是 **LLM/自动化路径生成的 run_shell/applescript**, 不挡 User 自己在终端手敲。
#   真正的对外边界在**发送/动作层**(outbound_email / 对外接口), 不在这层 shell 门。
#   GUARD_ENABLED=False(User 手改 config.toml 显式裸放)时本层同 _RED_LINES 一并让开 ——
#   与全模块「guard 关 = 全开」语义一致; 而运行时改 GUARD_ENABLED 已被 evolve/skillforge/gep
#   自扫 + 本表「写 config.toml / guard_enabled=false」双重硬拦覆盖。
#
# 铁律: 这些 pattern **同时仍在 _RED_LINES 里**(_check_red_line 是 ~30 个下游分类器的单一真相源,
# 绝不能从 _RED_LINES 移除, 否则 swarm/autopilot/epistemic_core 等会漏判)。本表是执行入口
# (run_shell/applescript)之上的更强 overlay, 与 _RED_LINES 有意重叠。
_HARDLINE_LINES = [
    # A. 护栏/监军篡改(自我提权面: 改护栏/隐私开关走 config 工具不走 shell; 监军 xiaoshuai 不可碰)
    (r"(>>?|\btee\b)\s*[^\n|]*\.jarvis/config\.toml", "shell 写 ~/.jarvis/config.toml(改护栏/隐私开关)"),
    (r"(?i)guard_enabled\s*[=:]\s*false", "关闭安全护栏 GUARD_ENABLED"),
    (r"(>>?|\btee\b|\bcp\b|\bmv\b|\bsed\b\s+-i)\s*[^\n|]*xiaoshuai/", "改动监军 xiaoshuai/(永久白名单)"),
    # A. 凭据/持久化 shell 写(与 write_file 的 _SENSITIVE_WRITE_RX 同姿态: 即便 confirmed 也拒)
    (r"(>>?|\btee\b|\bcp\b|\bmv\b|\binstall\b|\bln\b)\s*[^\n|]*"
     r"(\.ssh/|\.aws/|\.gnupg/|authorized_keys|\.git-credentials|\.netrc|"
     r"\.bashrc|\.zshrc|\.bash_profile|\.zprofile|\.zshenv|\.profile|"
     r"/LaunchAgents/|/LaunchDaemons/|sudoers|/\.env(\s|$|\.))",
     "shell 写 SSH/凭据/shell-rc/LaunchAgents 等敏感区"),
    # B. 灾难性不可逆销毁(User 2026-07-04 拍板: 连他自己 confirmed 也不放行)
    # rm 根/家: 目标后允许空白/引号/结尾(防 `do shell script "rm -rf /"` 用引号绕过 $ 锚)。
    # `/tmp` 等非根目标不误伤: 裸 `/` 后必须紧跟分隔符, `/tmp` 的 `t` 不是分隔符 → 不命中。
    (r"""\brm\s+-[rfRF]*[rR][fF]*\s+(/|~|\$HOME|/Users/\w+)(\s|["']|$)""", "rm -rf 根/家目录(灾难销毁)"),
    (r"\bmkfs\b|\bdiskutil\s+(erase|reformat)", "格式化磁盘(灾难销毁)"),
    (r">\s*/dev/(disk|sd|rdisk)", "写裸磁盘设备(灾难销毁)"),
    (r"\bdd\s+.*of=/dev/", "dd 写裸盘(灾难销毁)"),
]
_HARDLINE_RX = [(re.compile(p, re.IGNORECASE), why) for p, why in _HARDLINE_LINES]

# --------------------------------------------------------------------------- #
# AppleScript 危险/只读判别 (单一真相源 —— skillforge / appsmith 复用)
# --------------------------------------------------------------------------- #
# 痛点: 上面的 _RED_LINES 全是 *shell 语法形状*。AppleScript 用的是英语动词
# (empty trash / delete / quit / restart / set volume / keystroke …), 对 shell
# 红线表完全隐形 —— 而 appsmith 立身之本就是"用 AppleScript 控制任意 App", 这些
# 原生动词零确认就执行 = 红线 1 不可逆。故在此单独建 AppleScript 危险动词表。
#
# 黑名单 _AS_DANGER_RX: 命中 = 危险, 需 User 确认。
_AS_DANGER = [
    (r"\bempty\s+(the\s+)?trash\b", "清空废纸篓(不可逆)"),
    (r"\bdelete\b", "delete(删除对象)"),
    (r"\berase\b", "erase(抹除)"),
    (r"\bmove\b[\s\S]*\bto\s+trash\b", "move … to trash(移入废纸篓)"),
    (r"\bdo\s+shell\s+script\b", "do shell script(AS 内跑 shell)"),
    (r"\bdo\s+javascript\b", "do javascript(AS 内跑 JS)"),
    (r"\bquit\b", "quit(退出 app)"),
    (r"\bshut\s+down\b", "shut down(关机)"),
    (r"\brestart\b", "restart(重启)"),
    (r"\blog\s+out\b", "log out(注销)"),
    (r"\bset\s+volume\b", "set volume(改音量)"),
    (r"\bkeystroke\b", "keystroke(模拟按键)"),
    (r"\bkey\s+code\b", "key code(模拟按键)"),
    (r"\bsend\b", "send(发送, 可能对外)"),
    (r"\bclose\s+every\b", "close every(批量关闭)"),
]
_AS_DANGER_RX = [(re.compile(p, re.IGNORECASE), why) for p, why in _AS_DANGER]

# 敏感写硬拦(移植 Hermes _SENSITIVE_WRITE_TARGET): 这些路径**永不是 jarvis 合法写目标**,
# 即便 confirmed=True 也 fail-closed 拒绝。防 LLM 生成的路径写凭据/持久化(SSH 密钥/
# authorized_keys / shell rc / LaunchAgents / sudoers / cloud 凭据 / .env)。
# 业务区(Documents/Desktop/workspace)与 ~/.jarvis 不在此列, 正常写不受影响。
_SENSITIVE_WRITE_RX = re.compile(
    r"(/\.ssh/|/\.aws/|/\.gnupg/|/\.config/gh/|/\.config/gcloud/|/\.docker/config|"
    r"/\.netrc$|/\.npmrc$|/\.pypirc$|/\.git-credentials$|/authorized_keys$|"
    # .env 全家族但放行模板示例(.env.example/.sample/.template 不含真凭据, 是合法脚手架)
    r"(^|/)\.env(?!\.(?:example|sample|template)$)(\.[^/]*)?$|"
    r"/\.bash_profile$|/\.bashrc$|/\.zshrc$|/\.zprofile$|/\.zshenv$|/\.profile$|"
    # macOS canonical: /etc → /private/etc, /var → /private/var(realpath 后是 /private/...)
    r"(^|/)sudoers(\.d/|$)|^(/private)?/etc/|"
    r"/Library/LaunchAgents/|/Library/LaunchDaemons/|/crontabs?/)",
    re.IGNORECASE)

# 白名单 _AS_READONLY_RX: 只读/无害动词。比黑名单更稳健——黑名单可被变量拼串绕过,
# 故对 appsmith adapter 路径采取"非白名单一律要确认"的姿态(见 appsmith._compute_confirm)。
# 这里收的是 AppleScript 里真正纯读/无副作用的常见动词。
_AS_READONLY = [
    "get", "count", "exists", "return", "display dialog", "display notification",
    "display alert", "play", "name of", "properties of", "id of", "title of",
    "url of", "path of", "version of", "whose", "every",
]
# 仅当脚本里出现的「动作动词」全部落在只读集合内, 才算只读。用一个粗粒度的命令动词
# 抽取 + 白名单核对(见 applescript_is_readonly)。
_AS_VERB_RX = re.compile(
    r"\b(get|set|make|delete|move|copy|duplicate|open|close|quit|save|print|"
    r"count|exists|run|activate|launch|tell|do\s+shell\s+script|do\s+javascript|"
    r"empty|erase|send|reply|keystroke|key\s+code|click|launch|restart|shut\s+down|"
    r"log\s+out|set\s+volume|add|remove|create|play|pause|display)\b",
    re.IGNORECASE)
# 在抽到的动词中, 这些算"安全/只读或显示类"(其余动词 → 视为有副作用)。
_AS_SAFE_VERBS = {
    "get", "count", "exists", "tell", "activate", "launch", "run", "play",
    "pause", "display", "return",
}


def applescript_danger(script: str):
    """AppleScript 危险动词黑名单检测。命中返回原因(str), 否则 None。
    单一真相源 —— skillforge / appsmith 复用此函数, 不另造表。
    GUARD_ENABLED=False 时放开(与 browser_danger/physical_danger 行为统一;
    原来只在调用方 applescript() 检查, 直调本函数的复用方会绕过总闸语义)。"""
    if not GUARD_ENABLED:
        return None
    for rx, why in _AS_DANGER_RX:
        if rx.search(script or ""):
            return why
    return None


def applescript_is_readonly(script: str) -> bool:
    """AppleScript 是否"纯只读/无副作用"(白名单姿态, 比黑名单更稳)。
    抽出脚本里的动作动词, 全部落在 _AS_SAFE_VERBS 才算只读; 出现任一非安全动词 → False。
    空脚本视为只读(无操作)。"""
    s = script or ""
    if not s.strip():
        return True
    if applescript_danger(s):  # 黑名单命中直接非只读
        return False
    verbs = {m.group(0).lower().replace("  ", " ").strip()
             for m in _AS_VERB_RX.finditer(s)}
    # 规范化多空格动词(do shell script / key code / shut down / log out / set volume)
    verbs = {re.sub(r"\s+", " ", v) for v in verbs}
    for v in verbs:
        head = v.split()[0]
        if head not in _AS_SAFE_VERBS:
            return False
    return True


# --------------------------------------------------------------------------- #
# 非 shell 副作用分级闸(共享底座②, 2026-06-05)
# --------------------------------------------------------------------------- #
# 背景: _check_red_line 是"shell 语法形状"的, applescript_danger 是"AS 动词"的 ——
# 两者对**浏览器点击(Playwright click/submit/付款)**和**物理设备动作(开锁/发短信)**
# 完全隐形。device_mesh / browser_runner 上线时若各自重写确认逻辑, 等于把红线 2/3 的
# 最后一道闸新写一遍(评审 XSF-2 风险)。这里建**单一真相源**: 非 shell 副作用的危险动词
# 检测, 让它们复用, 绝不各写各的。镜像 applescript_danger 的黑名单姿态。
# 注: 中文字符没有 \b 词边界, 故对 CJK 关键词不加 \b; 英文词用 (?<![A-Za-z]) 前后界
# 防 substring 误伤(如 "display" 不该命中 "pay")。
def _w(en_words, cn_words):
    """组装一条危险 pattern: 英文词带 ASCII 边界, 中文词裸匹配。"""
    parts = []
    for w in en_words:
        parts.append(rf"(?<![A-Za-z]){w}(?![A-Za-z])")
    parts.extend(cn_words)
    return "(?:" + "|".join(parts) + ")"


_WEB_DANGER = [
    (_w(["submit"], ["提交"]), "网页提交表单(可能下单/付款/发帖)"),
    (_w(["pay", "payment", "checkout", r"place[_\s-]?order"],
        ["下单", "付款", "支付", "结算"]), "网页支付/下单(真实付钱, 红线2)"),
    (_w(["send", "post", "reply", "publish"],
        ["发送", "发帖", "回复", "发布"]), "网页对外发送(影响他人, 红线3)"),
    (_w(["delete", "remove"], ["删除", "移除"]), "网页删除(可能不可逆)"),
    (_w(["transfer", "wire"], ["转账", "汇款"]), "网页转账(真实付钱, 红线2)"),
    (_w([r"confirm[_\s-]?order", r"buy[_\s-]?now"], ["确认订单", "立即购买"]), "确认购买"),
]
_WEB_DANGER_RX = [(re.compile(p, re.IGNORECASE), why) for p, why in _WEB_DANGER]

# 物理世界 / 设备动作(device_mesh 域): 开锁/门禁/电话/短信/IoT 等不可逆或对外动作。
_PHYS_DANGER = [
    (_w(["unlock", r"door[_\s-]?lock"], ["开锁", "解锁", "门锁"]), "门锁(物理不可逆, 红线1)"),
    (_w(["disarm"], ["撤防", "关闭安防"]), "撤防安防(物理风险)"),
    (_w(["garage"], ["车库门", "车库"]), "车库门(物理不可逆)"),
    (_w(["gas", "valve"], ["燃气", "阀门"]), "燃气/阀门(物理危险)"),
    (_w(["sms", r"text[_\s-]?message"], ["短信", "发短信"]), "发短信(对外, 红线3)"),
    (_w(["call", "dial"], ["拨号", "打电话"]), "打电话(对外, 红线3)"),
    (_w([r"unlock[_\s-]?car"], ["远程启动", "车辆"]), "车辆控制(物理不可逆)"),
]
_PHYS_DANGER_RX = [(re.compile(p, re.IGNORECASE), why) for p, why in _PHYS_DANGER]


def browser_danger(action_text: str):
    """浏览器/网页动作危险检测(单一真相源)。命中返回原因(str), 否则 None。
    browser_runner / computer_use 的网页步骤复用此函数, 绝不各写各的确认逻辑。"""
    if not GUARD_ENABLED:
        return None
    for rx, why in _WEB_DANGER_RX:
        if rx.search(action_text or ""):
            return why
    return None


def physical_danger(action_text: str):
    """物理世界/设备动作危险检测(单一真相源, device_mesh 域)。命中返回原因, 否则 None。"""
    if not GUARD_ENABLED:
        return None
    for rx, why in _PHYS_DANGER_RX:
        if rx.search(action_text or ""):
            return why
    return None


def non_shell_danger(action_text: str, *, kind: str | None = None):
    """非 shell 副作用统一闸: 按 kind 路由到 browser/physical/applescript 危险检测,
    kind 未知时三者全扫(保守)。命中返回原因(str), 否则 None。

    这是给 browser_runner / device_mesh / appsmith 等"不经 shell"的副作用路径用的
    单一确认门入口 —— 它们碰危险动作就该走这里, 而不是自建 confirmed 判定。"""
    if not GUARD_ENABLED:
        return None
    t = action_text or ""
    if kind in ("browser", "web", "playwright"):
        return browser_danger(t)
    if kind in ("device", "physical", "home", "sms", "call", "lock"):
        return physical_danger(t)
    if kind in ("applescript", "as"):
        return applescript_danger(t)
    # 未知 kind: 三者全扫(任一命中即危险), fail-safe。
    return browser_danger(t) or physical_danger(t) or applescript_danger(t)


# --------------------------------------------------------------------------- #
# 命令反混淆归一化 (Hermes v0.18 借鉴 #1: approval.py _normalize_command_for_detection)
# --------------------------------------------------------------------------- #
# 痛点: 红线/hardline 正则是「shell 语法形状」的, 但攻击者能用编码花招绕过 ——
#   rm${IFS}-rf${IFS}/  (用 $IFS 代空格)  /  全角 ｒｍ　－ｒｆ  /  r\m -rf /  (反斜杠转义)
#   /  r''m -rf /  (空串拼接)  /  $(rm -rf /)  (subshell 分组)  —— 都能让裸正则漏判。
# 解法: 匹配前先把命令归一化成"去花招"形态**只喂给 matcher**(执行的仍是用户原始命令)。
# 铁律: ① 绝不改 _RED_RX/_HARDLINE_RX 正则本体(单一真相源)——只在其前面加归一化预处理层。
#       ② matcher 同时看原始串**和**归一化串(任一命中即拦)——保证只 ADD 匹配、绝不减少
#          (原始能匹配的现在照样匹配, 零回归; 额外多抓混淆形态)。
_ANSI_RX = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")          # ANSI 转义序列
_IFS_RX = re.compile(r"\$\{?IFS\}?(\$\d+)?")             # $IFS / ${IFS} / $IFS$9
_EMPTY_QUOTE_RX = re.compile(r"''|\"\"")                 # 空串拼接 r''m
_BACKSLASH_ESC_RX = re.compile(r"\\(?=[A-Za-z0-9])")     # 转义反斜杠 r\m -> rm
_GROUP_DELIM_RX = re.compile(r"\$\(|[`(){}]")            # subshell/分组分隔符 -> 空格


def _normalize_command_for_detection(command: str) -> str:
    """把命令剥掉编码/语法混淆花招, **仅用于红线检测**(绝不改真正执行的原始命令)。

    处理: null/ANSI 剥除 → NFKC(全角→半角) → 续行折叠 → 空串拼接消除 → $IFS→空格
    → subshell/分组分隔符→空格(暴露内层命令给 flat matcher) → 转义反斜杠剥除。
    """
    if not command:
        return ""
    s = command.replace("\x00", "")
    s = _ANSI_RX.sub("", s)
    try:
        s = unicodedata.normalize("NFKC", s)       # ｒｍ→rm, 全角空格→半角
    except Exception:
        pass
    s = s.replace("\\\n", "")                       # 折叠续行
    s = _EMPTY_QUOTE_RX.sub("", s)                  # r''m -> rm
    s = _IFS_RX.sub(" ", s)                         # rm${IFS}-rf -> rm -rf
    s = _GROUP_DELIM_RX.sub(" ", s)                 # $(rm -rf /) -> " rm -rf / "
    s = _BACKSLASH_ESC_RX.sub("", s)                # r\m -> rm
    return s


def _rx_hit(rx, original: str, normalized: str) -> bool:
    """原始串或(仅当归一化改变了内容时)归一化串任一命中 —— 只 ADD 匹配, 零回归。"""
    if rx.search(original):
        return True
    return normalized != original and bool(rx.search(normalized))


def _check_red_line(text: str):
    """返回命中的红线原因(str); 没命中返回 None。匹配前做反混淆归一化(原始+归一化双看)。"""
    if not GUARD_ENABLED:
        return None
    t = text or ""
    norm = _normalize_command_for_detection(t)
    for rx, why in _RED_RX:
        if _rx_hit(rx, t, norm):
            return why
    return None


def _check_hardline(text: str):
    """返回命中的 hardline 硬底线原因(str); 没命中返回 None。

    hardline = 即便 confirmed=True 也 fail-closed 拒绝的操作(护栏篡改 / 凭据写 / 灾难销毁)。
    只在执行入口(run_shell / applescript)调用, 是 _check_red_line(needs_confirm)之上的更强
    overlay。GUARD_ENABLED=False 时同 _check_red_line 一并让开(全模块「guard 关 = 全开」语义)。
    匹配前同样做反混淆归一化(原始+归一化双看), 防混淆绕过硬底线。"""
    if not GUARD_ENABLED:
        return None
    t = text or ""
    norm = _normalize_command_for_detection(t)
    for rx, why in _HARDLINE_RX:
        if _rx_hit(rx, t, norm):
            return why
    return None


def _blocked(why: str, target: str):
    """生成"需要确认"的标准回包, 让大脑去问 User。"""
    return {
        "ok": False,
        "needs_confirm": True,
        "red_line": why,
        "message": f"这个操作命中红线「{why}」: {target} —— 不可逆/有风险, "
                   f"要 User 明确说「确认」我才动。",
    }


def _hard_blocked(why: str, target: str):
    """fail-closed 硬拦回包: 永不放行(即便 confirmed 也拒), 不提示去确认(避免无效一来一回)。"""
    return {
        "ok": False,
        "blocked": True,
        "needs_confirm": False,
        "red_line": why,
        "message": f"「{why}」: {target} —— 永不可写, 确认也不放行(敏感系统/凭据路径)。",
    }


def _guard_path(fp: str) -> str:
    """敏感判定专用: 把父目录 realpath(解析符号链接)再拼 basename, 防软链目录把写入
    引到 ~/.ssh 等敏感区而字符串不含敏感 token。文件本身可不存在(只解析父目录)。"""
    try:
        d = os.path.realpath(os.path.dirname(fp))
        return os.path.join(d, os.path.basename(fp))
    except Exception:
        return fp


def _trunc(s: str) -> str:
    s = s or ""
    if len(s) > MAX_OUTPUT:
        return s[:MAX_OUTPUT] + f"\n…(截断, 共 {len(s)} 字)"
    return s


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path or ""))


# --------------------------------------------------------------------------- #
# 1. 跑 shell 命令
# --------------------------------------------------------------------------- #
def run_shell(command: str, confirmed: bool = False, cwd: str | None = None) -> dict:
    """在 User 的 Mac 上跑一条 bash 命令。

    命中 4 红线且 confirmed!=True -> 返回 needs_confirm (要 User 确认)。
    否则直接执行, 返回 {ok, exit, stdout, stderr}。
    """
    command = (command or "").strip()
    if not command:
        return {"ok": False, "error": "空命令"}
    # hardline 硬底线优先: 护栏篡改/凭据写/灾难销毁 —— 即便 confirmed=True 也 fail-closed 拒绝。
    # (hardline 永不进会话白名单: 下面 allowlist 逻辑在 hard 之后, 已被此处 return 挡在门外。)
    hard = _check_hardline(command)
    if hard:
        return _hard_blocked(hard, command)
    why = _check_red_line(command)
    if why:
        if confirmed:
            # User 显式批准: 若开了会话白名单, 记住这条确切命令, 本进程内免再问。
            if SESSION_ALLOWLIST_ENABLED:
                _SESSION_APPROVED.add(command)
        elif SESSION_ALLOWLIST_ENABLED and command in _SESSION_APPROVED:
            pass   # 本 session 已批准过同一条确切命令 → 免再问(#23)
        else:
            return _blocked(why, command)
    try:
        p = subprocess.run(
            ["/bin/bash", "-lc", command],
            capture_output=True, text=True,
            timeout=SHELL_TIMEOUT,
            cwd=_expand(cwd) if cwd else None,
        )
        return {
            "ok": p.returncode == 0,
            "exit": p.returncode,
            "stdout": _trunc(p.stdout),
            "stderr": _trunc(p.stderr),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"命令超时(>{SHELL_TIMEOUT}s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# 2. 读文件 (含 Office/PDF 文本抽取)
# --------------------------------------------------------------------------- #
def _extract_office(fp: str) -> str | None:
    """从 docx/xlsx/pptx(zip+xml) / pdf 抽纯文本。不是这些格式返回 None。

    docx/pptx: 抽 <w:t>/<a:t> 文字; xlsx: 抽 sharedStrings 的 <t>; pdf: pdftotext。
    纯 stdlib(zipfile+re) + 可选 pdftotext(poppler)。失败返回 ""(让上层兜底)。
    """
    ext = os.path.splitext(fp)[1].lower()
    try:
        if ext == ".pdf":
            for tool in ("/opt/homebrew/bin/pdftotext", "pdftotext"):
                try:
                    p = subprocess.run([tool, "-q", fp, "-"],
                                       capture_output=True, text=True, timeout=60)
                    if p.returncode == 0 and p.stdout.strip():
                        return p.stdout
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    continue
            return ""  # 没 pdftotext / 扫描件无文字层 -> 建议走 ocr_file
        if ext in (".docx", ".pptx", ".xlsx"):
            import zipfile
            with zipfile.ZipFile(fp) as z:
                names = z.namelist()
                chunks = []
                if ext == ".docx":
                    targets = [n for n in names if n == "word/document.xml"
                               or n.startswith("word/header") or n.startswith("word/footer")]
                elif ext == ".pptx":
                    targets = sorted(n for n in names if n.startswith("ppt/slides/slide"))
                else:  # xlsx
                    targets = [n for n in names if n == "xl/sharedStrings.xml"]
                for n in targets:
                    xml = z.read(n).decode("utf-8", "replace")
                    xml = re.sub(r"</w:p>|</a:p>|</a:r>|</si>", "\n", xml)
                    txt = re.sub(r"<[^>]+>", "", xml)
                    if txt.strip():
                        chunks.append(txt)
                return "\n".join(chunks)
    except Exception:
        return ""
    return None


def read_file(path: str, max_bytes: int = 60000, allow_private: bool = False) -> dict:
    """读本机文件文本内容(给 User 本人, 本机不外传)。docx/xlsx/pptx/pdf 自动抽文本。

    隐私文件(index.db 标 private=1, 银行/工资/身份证等)照读, 但返回里带 _private=True 信号 ——
    大脑看到这个信号会切到本机模型(oMLX Qwen3.6-27B)处理, 正文绝不送云端 gpt-5.5/不出本机。
    (allow_private 参数保留兼容, 现已不需要; 隐私靠"本机模型处理"而非"拒读")
    """
    fp = _expand(path)
    if not os.path.isfile(fp):
        return {"ok": False, "error": f"不是文件或不存在: {fp}"}
    _private = False
    try:
        import file_tools
        _private = file_tools.is_private_path(fp)
    except Exception:
        pass
    try:
        extracted = _extract_office(fp)
        if extracted is not None:
            # Office/PDF 格式
            if not extracted.strip():
                return {"ok": False, "path": fp,
                        "error": "这是 Office/PDF 但抽不到文字层(可能是扫描件), 试试 ocr_file。",
                        **({"_private": True} if _private else {})}
            data = extracted
        else:
            # 普通文本文件
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        out = {"ok": True, "path": fp, "content": data[:max_bytes],
               "truncated": truncated}
        if _private:
            out["_private"] = True  # 信号: 大脑切本机模型处理, 正文不出本机
        return out
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# E4 时间机器: best-effort 记录小美的副作用动作(失败绝不影响主流程)
# --------------------------------------------------------------------------- #
def _tm_log(kind, target, detail="", undo=None, reversible=False, source="local_powers"):
    try:
        import time_machine
        time_machine.record(kind, target, detail, undo=undo,
                            reversible=reversible, source=source)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 3. 写文件 (新建/覆盖)
# --------------------------------------------------------------------------- #
def write_file(path: str, content: str, confirmed: bool = False,
               append: bool = False) -> dict:
    """写文件。覆盖已存在文件算"改写", 命中红线词的路径要确认。

    新建/写到非业务区 -> 直接写。覆盖业务/重要目录里已存在文件 -> 要确认。
    """
    fp = _expand(path)
    # 敏感写硬拦(fail-closed, 即便 confirmed 也拒): SSH/凭据/shell rc/LaunchAgents/sudoers
    # 等永不是 jarvis 合法写目标。防 LLM 生成路径写持久化/凭据。父目录 realpath 防软链绕过。
    # GUARD_ENABLED 关时不拦(同既有姿态)。
    if GUARD_ENABLED and _SENSITIVE_WRITE_RX.search(_guard_path(fp)):
        return _hard_blocked("敏感系统/凭据路径", fp)
    overwrite = os.path.exists(fp) and not append
    target_risky = re.search(r"(workspace|核心业务|重要项目|客户资料|/Documents/|/Desktop/)", fp)
    if overwrite and target_risky and not confirmed and GUARD_ENABLED:
        return _blocked("覆盖业务区已存在文件", fp)
    # E4 时间机器: 覆盖前抓旧内容(<256KB 才存, 大文件只观测)供撤销
    _old = None
    if overwrite:
        try:
            if os.path.getsize(fp) <= 256 * 1024:
                with open(fp, "r", encoding="utf-8") as _f:
                    _old = _f.read()
        except Exception:
            _old = None
    try:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        mode = "a" if append else "w"
        with open(fp, mode, encoding="utf-8") as f:
            f.write(content or "")
        _tm_log("write", fp, "改写" if overwrite else ("append" if append else "新建"),
                undo={"path": fp, "old_content": _old} if (overwrite and _old is not None) else None,
                reversible=bool(overwrite and _old is not None), source="write_file")
        return {"ok": True, "path": fp, "bytes": len(content or ""),
                "mode": "append" if append else ("overwrite" if overwrite else "create")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# 4. 移动/重命名文件
# --------------------------------------------------------------------------- #
def move_file(src: str, dst: str, confirmed: bool = False) -> dict:
    """移动或重命名。覆盖已存在的目标算高风险, 要确认。"""
    s, d = _expand(src), _expand(dst)
    # 敏感写硬拦(fail-closed): 目标若是 SSH/凭据/shell rc/LaunchAgents 等永不可写区, 拒绝
    # (防绕过 write_file 改用 move 落地凭据/持久化)。父目录 realpath 防软链绕过。
    if GUARD_ENABLED and _SENSITIVE_WRITE_RX.search(_guard_path(d)):
        return _hard_blocked("移动目标是敏感系统/凭据路径", d)
    if not os.path.exists(s):
        return {"ok": False, "error": f"源不存在: {s}"}
    if os.path.exists(d) and not confirmed and GUARD_ENABLED:
        return _blocked("移动会覆盖已存在目标", d)
    try:
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.move(s, d)
        _tm_log("move", d, f"{s} → {d}", undo={"from": d, "to": s},
                reversible=True, source="move_file")
        return {"ok": True, "src": s, "dst": d}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# 5. 打开文件/app/网址 (macOS open)
# --------------------------------------------------------------------------- #
def open_path(target: str) -> dict:
    """用 macOS `open` 打开: 文件 / 文件夹 / app / http(s) 网址。"""
    target = (target or "").strip()
    if not target:
        return {"ok": False, "error": "空目标"}
    is_url = target.startswith(("http://", "https://"))
    is_app = target.endswith(".app") or (not is_url and "/" not in target and "." not in target)
    arg = target if (is_url or is_app) else _expand(target)
    cmd = ["open"]
    if is_app and not target.endswith(".app"):
        cmd += ["-a", arg]
    else:
        cmd += [arg]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return {"ok": p.returncode == 0, "target": arg,
                "stderr": _trunc(p.stderr)} if p.returncode == 0 else \
               {"ok": False, "error": _trunc(p.stderr) or "open 失败", "target": arg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# 6. AppleScript (macOS 自动化: 控制 app / 系统)
# --------------------------------------------------------------------------- #
def applescript(script: str, confirmed: bool = False) -> dict:
    """跑一段 AppleScript (osascript)。命中 shell 红线词 *或* AppleScript 原生危险动词
    (empty trash / delete / quit / restart / set volume / keystroke / do shell script …)
    且未确认 → 要 User 确认。AppleScript 危险动词表是 shell 红线表的盲区补丁(单一真相源)。"""
    script = (script or "").strip()
    if not script:
        return {"ok": False, "error": "空脚本"}
    if GUARD_ENABLED:
        hard = _check_hardline(script)     # 硬底线: 即便 confirmed 也拒(如 AS 内 do shell script 灾难命令)
        if hard:
            return _hard_blocked(hard, "AppleScript")
        why = _check_red_line(script) or applescript_danger(script)
        if why and not confirmed:
            return _blocked(why, "AppleScript")
    try:
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=60)
        return {"ok": p.returncode == 0, "stdout": _trunc(p.stdout),
                "stderr": _trunc(p.stderr)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# 自测
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import json as _json
    print("== safe shell ==")
    print(_json.dumps(run_shell("echo hi && whoami"), ensure_ascii=False))
    print("== red-line shell (should be blocked) ==")
    print(_json.dumps(run_shell("rm -rf ~/Documents"), ensure_ascii=False))
    print("== red-line confirmed (would run; using harmless echo proxy) ==")
    print(_json.dumps(run_shell("echo 'pretend dangerous'", confirmed=True), ensure_ascii=False))
    print("== read self ==")
    r = read_file(__file__, max_bytes=40)
    print(_json.dumps({k: (v[:30] if k == "content" else v) for k, v in r.items()}, ensure_ascii=False))
    print("== applescript danger (empty trash, should be blocked) ==")
    print(_json.dumps(applescript('tell application "Finder" to empty trash'), ensure_ascii=False))
    print("== applescript readonly (get name, should run) ==")
    print(_json.dumps(applescript('tell application "Finder" to get name of every window'), ensure_ascii=False))
