"""金额隐私控制、多语言查询参数重定向、开机自启与桌面托盘后台常驻综合单元测试。"""

from __future__ import annotations

import plistlib
import sys

import pytest
from fastapi.testclient import TestClient

from kestrel.cli import build_parser
from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.desktop import app as desktop_app
from kestrel.desktop import autostart, tray
from kestrel.fees import plans as plan_service
from kestrel.services import config as config_service
from kestrel.services import glossary, rules
from kestrel.settings import settings


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """提供完全隔离的数据库目录与开机自启目录，绝不污染真实系统环境。"""
    home_dir = tmp_path / "kestrel_home"
    autostart_dir = tmp_path / "autostart_dir"
    monkeypatch.setattr(settings, "home", home_dir)
    monkeypatch.setenv("KESTREL_AUTOSTART_DIR", str(autostart_dir))
    settings.ensure_dirs()
    init_db()
    plan_service.hydrate()
    glossary.seed()
    rules.ensure_seeded()
    return {"home": home_dir, "autostart": autostart_dir}


@pytest.fixture()
def client(isolated_env):
    from kestrel.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


# ------------------------------------------------------------------ 1. 配置项默认值与读写校验


def test_config_defaults_and_bool_coercion(isolated_env):
    """新增的 ui.privacy / desktop.close_to_tray / desktop.autostart 默认值与布尔归一化校验。"""
    conn = connect()
    try:
        assert config_service.get(conn, "ui.privacy") is False
        assert config_service.get(conn, "desktop.close_to_tray") is True
        assert config_service.get(conn, "desktop.autostart") is False

        config_service.put(conn, "ui.privacy", "true")
        config_service.put(conn, "desktop.close_to_tray", "0")
        config_service.put(conn, "desktop.autostart", 1)
        conn.commit()

        assert config_service.get(conn, "ui.privacy") is True
        assert config_service.get(conn, "desktop.close_to_tray") is False
        assert config_service.get(conn, "desktop.autostart") is True

        merged = config_service.all_values(conn)
        assert merged["ui.privacy"] is True
        assert merged["desktop.close_to_tray"] is False
        assert merged["desktop.autostart"] is True
    finally:
        conn.close()


# ------------------------------------------------------------------ 2. 多语言查询参数与未引导重定向


def test_unonboarded_root_preserves_locale_redirect_and_cookie(client):
    """未完成引导访问 /?locale=en 必须重定向到 /welcome?locale=en 并保持英文渲染与 Cookie。"""
    r_redirect = client.get("/?locale=en", follow_redirects=False)
    assert r_redirect.status_code == 303
    assert r_redirect.headers["location"] == "/welcome?locale=en"
    assert r_redirect.cookies.get("kestrel_locale") == "en"

    # 跟随重定向后应立即以英文渲染欢迎页
    r_welcome = client.get(r_redirect.headers["location"])
    assert r_welcome.status_code == 200
    assert 'lang="en"' in r_welcome.text

    # 后续不带参数的页面请求依赖写入的 kestrel_locale Cookie 依然保持英文
    r_market = client.get("/market")
    assert r_market.status_code == 200
    assert 'lang="en"' in r_market.text

    # 兼容 ?locale=zh 映射为 zh-CN 并覆盖 Cookie
    r_zh = client.get("/market?locale=zh")
    assert r_zh.status_code == 200
    assert 'lang="zh-CN"' in r_zh.text
    assert client.cookies.get("kestrel_locale") == "zh-CN"


def test_api_locale_toggle_endpoint(client):
    """顶栏双语胶囊调用 POST /api/locale 可在 zh-CN 与 en 之间切换并设置 Cookie。"""
    res_en = client.post("/api/locale", json={"locale": "en"})
    assert res_en.status_code == 200
    assert res_en.json() == {"ok": True, "locale": "en"}
    assert res_en.cookies.get("kestrel_locale") == "en"

    # 不传参数时自动在当前语言与另一语言间翻转
    res_flip = client.post("/api/locale", json={})
    assert res_flip.status_code == 200
    assert res_flip.json() == {"ok": True, "locale": "zh-CN"}


# ------------------------------------------------------------------ 3. 金额隐私 API 与 SSR 注入


def test_ui_privacy_toggle_and_ssr_context(client):
    """POST /api/ui/privacy 支持取反与显式赋值，并在 SSR 上下文注入 ui_privacy。"""
    conn = connect()
    try:
        config_service.put(conn, "ui.onboarded", True)
        conn.commit()
    finally:
        conn.close()

    # 初始未开启隐私
    r0 = client.get("/")
    assert r0.status_code == 200
    assert r0.context["ui_privacy"] is False

    # 不传 enabled 时自动取反为 True
    t1 = client.post("/api/ui/privacy", json={}).json()
    assert t1 == {"ok": True, "enabled": True}

    r1 = client.get("/")
    assert r1.context["ui_privacy"] is True

    # 再次不传 enabled 自动取反回 False
    t2 = client.post("/api/ui/privacy").json()
    assert t2 == {"ok": True, "enabled": False}

    # 显式传入 enabled=True
    t3 = client.post("/api/ui/privacy", json={"enabled": True}).json()
    assert t3 == {"ok": True, "enabled": True}
    assert client.get("/market").context["ui_privacy"] is True


# ------------------------------------------------------------------ 4. 桌面状态、设置接口与表单保存


def test_desktop_status_and_settings_api_and_form(client, isolated_env):
    """验证 /api/desktop/status、/api/desktop/settings 与 /settings/desktop 表单持久化及自启联动。"""
    st0 = client.get("/api/desktop/status").json()
    assert st0["ok"] is True
    assert st0["close_to_tray"] is True
    assert st0["autostart"] is False
    assert st0["scheduler_running"] is True
    assert isinstance(st0["tray_supported"], bool)
    assert st0["autostart_installed"] is False

    # 页面 SSR 上下文同样注入 desktop_state 四要素
    page = client.get("/settings")
    assert page.status_code == 200
    ds = page.context["desktop_state"]
    assert set(ds.keys()) == {"close_to_tray", "autostart", "scheduler_running", "tray_supported"}
    assert ds["scheduler_running"] is True

    # 部分更新：开启开机自启与金额隐私，关闭最小化到托盘
    updated = client.post(
        "/api/desktop/settings",
        json={"close_to_tray": False, "autostart": True, "privacy": True},
    ).json()
    assert updated["ok"] is True
    assert updated["close_to_tray"] is False
    assert updated["autostart"] is True
    assert updated["privacy"] is True
    assert updated["autostart_installed"] is True

    # 确认启动项文件仅创建在隔离的临时目录内
    target_file = autostart.autostart_file()
    assert target_file.exists()
    assert str(isolated_env["autostart"]) in str(target_file)

    # 通过表单提交 /settings/desktop 关闭 autostart（未勾选 checkbox 不出现在表单中）
    form_resp = client.post(
        "/settings/desktop",
        data={"desktop.close_to_tray": "on"},
        follow_redirects=False,
    )
    assert form_resp.status_code == 303
    st_after = client.get("/api/desktop/status").json()
    assert st_after["close_to_tray"] is True
    assert st_after["autostart"] is False
    assert st_after["autostart_installed"] is False
    assert not target_file.exists()


# ------------------------------------------------------------------ 5. 跨平台开机自启文件生成与移除


def test_autostart_enable_and_disable_across_platforms(isolated_env):
    """在隔离目录下分别校验 macOS plist、Linux .desktop 与 Windows .cmd 的生成和清理。"""
    # macOS LaunchAgent plist
    res_mac = autostart.sync_autostart(True, platform="darwin")
    assert res_mac["ok"] is True and res_mac["installed"] is True
    plist_path = autostart.autostart_file("darwin")
    assert plist_path.name == "com.kestrel.desktop.plist"
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["Label"] == "com.kestrel.desktop"
    assert parsed["RunAtLoad"] is True
    assert "--minimized" in parsed["ProgramArguments"]

    res_mac_off = autostart.sync_autostart(False, platform="darwin")
    assert res_mac_off["ok"] is True and res_mac_off["installed"] is False
    assert not plist_path.exists()

    # Linux XDG autostart .desktop
    res_linux = autostart.sync_autostart(True, platform="linux")
    assert res_linux["ok"] is True and res_linux["installed"] is True
    desk_path = autostart.autostart_file("linux")
    assert "[Desktop Entry]" in desk_path.read_text(encoding="utf-8")
    assert "--minimized" in desk_path.read_text(encoding="utf-8")
    autostart.sync_autostart(False, platform="linux")
    assert not desk_path.exists()

    # Windows Startup .cmd
    res_win = autostart.sync_autostart(True, platform="win32")
    assert res_win["ok"] is True and res_win["installed"] is True
    cmd_path = autostart.autostart_file("win32")
    assert "--minimized" in cmd_path.read_text(encoding="utf-8")
    autostart.sync_autostart(False, platform="win32")
    assert not cmd_path.exists()


# ------------------------------------------------------------------ 6. 托盘控制器与窗口关闭拦截逻辑


class _DummyWindow:
    def __init__(self) -> None:
        self.hidden_calls = 0
        self.shown_calls = 0
        self.restored_calls = 0
        self.destroyed_calls = 0

    def hide(self) -> None:
        self.hidden_calls += 1

    def show(self) -> None:
        self.shown_calls += 1

    def restore(self) -> None:
        self.restored_calls += 1

    def destroy(self) -> None:
        self.destroyed_calls += 1


class _DummyServer:
    def __init__(self) -> None:
        self.should_exit = False


def test_tray_window_closing_interception_and_explicit_quit(isolated_env, monkeypatch):
    """托盘激活且开启 close_to_tray 时拦截窗口关闭并隐藏；关闭该选项或显式退出时正常销毁并停服。"""
    assert desktop_app.DEFAULT_WIDTH == 1360
    assert desktop_app.DEFAULT_HEIGHT == 860
    assert desktop_app.MIN_WINDOW_SIZE == (960, 640)

    win = _DummyWindow()
    srv = _DummyServer()
    quit_called = []

    ctrl = tray.TrayController(
        url="http://127.0.0.1:8899",
        window=win,
        server=srv,
        on_quit=lambda: quit_called.append(True),
        locale="zh-CN",
    )
    ctrl.active = True

    # 1) 默认 close_to_tray=True 且未点击退出：拦截关闭（返回 False），隐藏窗口，服务继续跑
    assert ctrl.should_intercept_close() is True
    allow_close = ctrl.handle_window_closing()
    assert allow_close is False
    assert win.hidden_calls == 1
    assert srv.should_exit is False
    assert quit_called == []

    # 通过托盘恢复显示窗口（屏蔽真实 AppKit 抢焦点调用）
    monkeypatch.setattr(sys, "platform", "linux")
    ctrl.show_window()
    assert win.shown_calls == 1
    assert win.restored_calls == 1

    # 2) 用户通过托盘切换关闭「关闭窗口时驻留后台」后，再次点窗口关闭应直接放行退出
    new_flag = ctrl.toggle_close_to_tray()
    assert new_flag is False
    assert ctrl.should_intercept_close() is False
    allow_close_2 = ctrl.handle_window_closing()
    assert allow_close_2 is True
    assert srv.should_exit is True
    assert len(quit_called) == 1

    # 3) 当 close_to_tray=True 但用户点击托盘菜单「退出 Kestrel」时：标记 _quitting=True 并销毁窗口
    ctrl2 = tray.TrayController(url="http://127.0.0.1:8899", window=win, server=_DummyServer())
    ctrl2.active = True
    ctrl2.toggle_close_to_tray()  # 切回 True
    assert ctrl2.get_close_to_tray() is True
    ctrl2.request_quit()
    assert ctrl2.quitting is True
    assert ctrl2.server.should_exit is True
    assert win.destroyed_calls == 1


def test_cli_desktop_minimized_and_menu_locales():
    """验证 CLI desktop --minimized / --background 参数解析及托盘中英文菜单完整性。"""
    parser = build_parser()
    args_min = parser.parse_args(["desktop", "--minimized"])
    assert args_min.minimized is True

    args_bg = parser.parse_args(["desktop", "--background"])
    assert args_bg.minimized is True

    zh_labels = tray.menu_labels("zh-CN")
    en_labels = tray.menu_labels("en")
    assert "后台盯盘运行中" in zh_labels["status_running"]
    assert zh_labels["quit"] == "退出 Kestrel"
    assert "Background Watch Active" in en_labels["status_running"]
    assert en_labels["quit"] == "Quit Kestrel"


def test_quick_stock_route_renders_200(client):
    """顶栏代码搜索直达 /stock/600519 在未入库与已入库时均返回 HTTP 200。"""
    resp = client.get("/stock/600519")
    assert resp.status_code == 200
    assert "600519" in resp.text

