import subprocess
import control


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init", "-q")
    _git(str(repo), "config", "user.email", "t@t")
    _git(str(repo), "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-qm", "base")
    return str(repo)


def test_revert_undoes_commit(tmp_path):
    repo = _init_repo(tmp_path)
    (tmp_path / "repo" / "a.py").write_text("x = 999  # bad\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "bad change")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    c = control.Control(repo=repo)
    assert c.revert(sha) is True
    assert "999" not in (tmp_path / "repo" / "a.py").read_text()  # 被回滚
    assert (tmp_path / "repo" / "a.py").exists()                  # no-delete: 文件还在


def test_pause_resume_evolve(tmp_jarvis):
    c = control.Control(repo=".", evolve_pause=str(tmp_jarvis / "evolve.pause"))
    c.pause_evolve()
    assert (tmp_jarvis / "evolve.pause").exists()
    c.resume_evolve()
    assert not (tmp_jarvis / "evolve.pause").exists()


def test_safe_word_writes_stops(tmp_jarvis):
    c = control.Control(repo=".",
                        ears_stop=str(tmp_jarvis / "ears.stop"),
                        eyes_stop=str(tmp_jarvis / "eyes.stop"))
    c.safe_word()
    assert (tmp_jarvis / "ears.stop").exists() and (tmp_jarvis / "eyes.stop").exists()
