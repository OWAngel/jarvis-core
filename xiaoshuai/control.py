"""小帅监督控制:对小美能做的动作。包现有 jarvis 机制(git/pause文件/stop文件/launchctl)。
红线:回滚走 git 历史, 绝不 rm 删 User 文件。"""
from __future__ import annotations
import os
import subprocess
from typing import Optional


class Control:
    def __init__(self, repo: str = ".",
                 evolve_pause: Optional[str] = None,
                 ears_stop: Optional[str] = None,
                 eyes_stop: Optional[str] = None,
                 claude_bin: Optional[str] = None):
        self.repo = repo
        exp = os.path.expanduser
        self.evolve_pause = evolve_pause or exp("~/.jarvis/evolve.pause")
        self.ears_stop = ears_stop or exp("~/.jarvis/ears.stop")
        self.eyes_stop = eyes_stop or exp("~/.jarvis/eyes.stop")
        self.claude_bin = claude_bin or "~/.local/bin/claude"

    def _git(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True, timeout=60)

    def revert(self, commit_sha: str) -> bool:
        """git revert 指定 commit(非交互, 不删文件)。"""
        r = self._git("revert", "--no-edit", commit_sha)
        if r.returncode != 0:
            self._git("revert", "--abort")
            return False
        return True

    def merge_branch(self, branch: str, worktree: str = "") -> bool:
        """把隔离 self-improve 分支 merge 回 main(小帅验证 SAFE 才调)。
        闭环(2026-06-05): 自改在隔离 worktree 分支, 唯有小帅绿灯走这里才进 main。
        merge 冲突 → abort 不留烂摊子。完了删 worktree + 分支。"""
        if not branch:
            return False
        # 防漂移(2026-06-06 修根因): merge 前必先回 main —— 否则若主 repo 漂到别的分支,
        # 会把进化 merge 到那个分支而非 main(整个 session 的工作就这么堆到 feat/awareness-daemon,
        # main 一直落后)。普通 checkout 被运行态 dirty 挡 → 强制切(self_improve 真改动在隔离
        # worktree, 主 repo 不该有真 dirty, 只有 .cc_jobs/.insighted 等运行态可弃)。
        if self._git("checkout", "main").returncode != 0:
            if self._git("checkout", "-f", "main").returncode != 0:
                return False  # 切不回 main → 宁可不激活也不 merge 错分支
        r = self._git("merge", "--no-ff", "-m",
                      f"merge self-improve {branch} (小帅监军 SAFE 放行)", branch)
        if r.returncode != 0:
            self._git("merge", "--abort")
            return False
        if worktree:
            self._git("worktree", "remove", "--force", worktree)
        self._git("branch", "-D", branch)
        return True

    def discard_branch(self, branch: str, worktree: str = "") -> bool:
        """丢弃隔离分支(小帅判 BLOCK / 撤销时)。main 从没被 merge, 删分支即天然回滚。"""
        if worktree:
            self._git("worktree", "remove", "--force", worktree)
        if branch:
            self._git("branch", "-D", branch)
        return True

    def activate(self, commit_sha: str, branch: str = "", worktree: str = "",
                 restart_cb=None) -> bool:
        """激活一次进化。带 branch(隔离分支模式)→ 先 merge 回 main 再重启生效;
        不带 branch(旧路径/已在 main)→ 直接重启。merge 失败不重启(不激活半成品)。"""
        try:
            if branch:
                if not self.merge_branch(branch, worktree):
                    return False  # merge 冲突 → 不激活
            if restart_cb:
                restart_cb(commit_sha)
            else:
                self.restart("server")
            return True
        except Exception:
            return False

    def attempt_fix(self, commit_sha: str, findings: list) -> bool:
        """起 ccc 按 findings 修一轮(bypassPermissions, 只在 fixing 态)。
        findings 由 LLM 产出非完全可信(二阶注入面)→ 限量 + 截断 + 去换行污染后再入 prompt。
        安全网: 修完必重验, 改坏/碰护栏会被后续 verify BLOCK→revert 兜回。"""
        def _clean(s):
            return str(s or "").replace("\n", " ").replace("\r", " ")[:300]
        bullets = "\n".join(f"- [{_clean(f.get('severity', '?'))[:12]}] {_clean(f.get('detail', ''))}"
                            for f in (findings or [])[:10])
        prompt = (f"小美进化 {commit_sha[:64]} 验证为 FIX_FIRST。只在本 repo 内**最小改动修复**以下问题, "
                  f"绝不新增能力/工具、绝不改动任何安全护栏(红线/只读配置/隐私路由/确认门/xiaoshuai 监军), "
                  f"绝不删文件:\n{bullets}")
        try:
            r = subprocess.run([self.claude_bin, "-p", prompt,
                                "--permission-mode", "bypassPermissions",
                                "--settings", '{"ultracode": true}'],
                               capture_output=True, text=True, timeout=1200, cwd=self.repo)
            return r.returncode == 0
        except Exception:
            return False

    def pause_evolve(self) -> None:
        open(self.evolve_pause, "w").close()

    def resume_evolve(self) -> None:
        if os.path.exists(self.evolve_pause):
            os.remove(self.evolve_pause)  # 删的是自己造的 pause 标记, 非 User 文件

    def safe_word(self) -> None:
        for p in (self.ears_stop, self.eyes_stop):
            open(p, "w").close()

    # 只允许重启这些已知 daemon(白名单, 防未来调用方误传任意 target 注入 launchctl 服务名)
    _ALLOWED_RESTART = ("server", "ears", "eyes", "sentinel", "evolve",
                        "mission", "xiaoshuai", "kokoro", "vlm")

    def restart(self, target: str) -> bool:
        """重启 launchd 管的 daemon(server 走脚本; daemon 走 kickstart)。仅白名单内 target。"""
        if target not in self._ALLOWED_RESTART:
            return False
        try:
            if target == "server":
                subprocess.run(["pkill", "-f", "brain/server.py"], timeout=10)
                subprocess.Popen(
                    ["python3", "brain/server.py"],
                    cwd=self.repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                uid = os.getuid()
                subprocess.run(["launchctl", "kickstart", "-k",
                                f"gui/{uid}/com.jarvis.agent.{target}"], timeout=15)
            return True
        except Exception:
            return False
