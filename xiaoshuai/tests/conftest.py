import os
import sys
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)
# 先插 brain(供 config), 再插 _PKG(xiaoshuai) → _PKG 在最前, 让 xiaoshuai 自己的
# server/store/gate 优先于 brain 的同名模块(brain/server.py 会撞 import server)。
for p in (os.path.join(os.path.dirname(_PKG), "brain"), _PKG):
    while p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)


@pytest.fixture
def tmp_jarvis(tmp_path, monkeypatch):
    """把小帅数据目录重定向到临时目录, 隔离测试。"""
    home = tmp_path / "dot_jarvis"
    home.mkdir()
    monkeypatch.setenv("XIAOSHUAI_DIR", str(home))
    return home
