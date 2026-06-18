"""
client/windows_client.py
========================
Windows 桌面客户端（tkinter）。

功能：
- 连接 Ubuntu 服务器 WS，携带 token 鉴权
- 断线自动重连（指数退避）
- 本地修改监测条件并同步到服务端
- 收到提醒：Windows 原生 Toast / 声音 + 主窗口历史列表
- 最小化到系统托盘（右下角）

不依赖任何交易功能，纯只读提醒。
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

import websockets

APP_ID = "PolymarketAlert"
APP_NAME = "Polymarket 世界杯冷门提醒"


def _app_dir() -> Path:
    """Return the script directory in source mode, or the exe directory when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DEFAULT_CONFIG_PATH = _app_dir() / "config.json"
LOCAL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_server_timestamp(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None

    normalized = text
    if normalized.upper().endswith(" UTC"):
        normalized = normalized[:-4] + "+00:00"
    elif normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue
        else:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_alert_local_time(value: object) -> str:
    dt = _parse_server_timestamp(value)
    if dt is None:
        dt = datetime.now().astimezone()
    return dt.astimezone().strftime(LOCAL_TIME_FORMAT)


def current_local_time() -> str:
    return datetime.now().astimezone().strftime(LOCAL_TIME_FORMAT)


def register_windows_notification_identity() -> str:
    """Register a stable AppUserModelID so Windows Toasts work from a one-file exe."""
    if not sys.platform.startswith("win"):
        return "not-windows"
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        from win32com.shell import shell

        programs = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        programs.mkdir(parents=True, exist_ok=True)
        shortcut_path = programs / f"{APP_ID}.lnk"

        target = Path(sys.executable).resolve()
        args = ""
        if not getattr(sys, "frozen", False):
            args = f'"{Path(__file__).resolve()}"'

        link = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink,
            None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IShellLink,
        )
        link.SetPath(str(target))
        link.SetArguments(args)
        link.SetWorkingDirectory(str(_app_dir()))
        link.SetDescription(APP_NAME)
        try:
            link.SetIconLocation(str(target), 0)
        except Exception:
            pass

        prop_store = link.QueryInterface(
            pythoncom.MakeIID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
        )
        prop_store.SetValue(
            pscon.PKEY_AppUserModel_ID,
            propsys.PROPVARIANTType(APP_ID, pythoncom.VT_LPWSTR),
        )
        prop_store.Commit()
        persist_file = link.QueryInterface(pythoncom.IID_IPersistFile)
        persist_file.Save(str(shortcut_path), 0)
        return "registered"
    except Exception as e:  # noqa: BLE001
        return f"shortcut-failed:{e}"


SETTING_GROUPS = [
    ("detector", "提醒阈值", [
        ("large_trade_usd", "普通提醒额 (USDC)"),
        ("important_trade_usd", "重要提醒额 (USDC)"),
        ("severe_trade_usd", "严重提醒额 (USDC)"),
        ("underdog_score_threshold", "冷门评分阈值"),
        ("underdog_max_implied_prob", "冷门概率上限"),
        ("min_book_depth_usd", "最低盘口深度 (USDC)"),
        ("short_window_large_usd", "短窗累计额 (USDC)"),
        ("short_window_sec", "短窗长度 (秒)"),
        ("price_spike_ratio", "价格异动比例"),
        ("volume_anomaly_multiplier", "成交异常倍数"),
    ]),
    ("discovery", "发现范围 / 频率", [
        ("refresh_interval_sec", "市场刷新间隔 (秒)"),
        ("min_liquidity_usd", "最低流动性 (USDC)"),
        ("max_markets", "最大监测市场数"),
    ]),
]

PARAM_META = {
    ("detector", "large_trade_usd"): (
        "USDC",
        "单笔成交额低于此值不会进入提醒判定；短窗口累计模式另看“短窗累计额”。",
    ),
    ("detector", "important_trade_usd"): (
        "USDC",
        "成交额达到此值时，提醒级别标为 IMPORTANT。必须大于等于普通提醒额。",
    ),
    ("detector", "severe_trade_usd"): (
        "USDC",
        "成交额达到此值时，提醒级别标为 SEVERE。必须大于等于重要提醒额。",
    ),
    ("detector", "underdog_score_threshold"): (
        "0-100 分",
        "综合冷门评分门槛。服务端按隐含概率、相对弱势、短时涨幅、成交异常、盘口深度加权计算。",
    ),
    ("detector", "underdog_max_implied_prob"): (
        "0-1 概率",
        "Outcome 当前价格高于此值就不算冷门方向。例如 0.35 表示概率高于 35% 不提醒。",
    ),
    ("detector", "min_book_depth_usd"): (
        "USDC",
        "盘口深度低于此值会被当作噪声忽略，用来过滤极薄盘口假信号。",
    ),
    ("detector", "short_window_large_usd"): (
        "USDC",
        "同一 outcome 在短窗口内累计成交达到此值，也按一次大额事件处理，用来识别拆单。",
    ),
    ("detector", "short_window_sec"): (
        "秒",
        "短窗口长度，用于计算短窗累计成交和短时价格异动。",
    ),
    ("detector", "price_spike_ratio"): (
        "比例",
        "短窗口内涨幅达到此比例时，价格异动子分 C 记满分。例如 0.05 表示上涨 5%。",
    ),
    ("detector", "volume_anomaly_multiplier"): (
        "倍数",
        "当前成交额相对近 1 小时中位数达到此倍数时，成交异常子分 D 记满分。",
    ),
    ("discovery", "refresh_interval_sec"): (
        "秒",
        "服务端重新发现世界杯市场的间隔。低于 60 秒会被拒绝；调低会更积极但请求更多。",
    ),
    ("discovery", "min_liquidity_usd"): (
        "USDC",
        "只监测流动性不低于此值的市场；调低会覆盖更多小盘口，噪声也会增加。",
    ),
    ("discovery", "max_markets"): (
        "个",
        "服务端同时监测的最大市场数。当前逻辑会优先覆盖 group/advance 等类别。",
    ),
}

INTEGER_FIELDS = {
    ("detector", "short_window_sec"),
    ("discovery", "refresh_interval_sec"),
    ("discovery", "max_markets"),
}


def load_config() -> dict:
    if DEFAULT_CONFIG_PATH.exists():
        with DEFAULT_CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg.setdefault("server_url", "ws://127.0.0.1:8765")
    cfg.setdefault("token", "change-me-please")
    cfg.setdefault("sound_enabled", True)
    cfg.setdefault("popup_enabled", True)
    cfg.setdefault("minimize_to_tray", True)
    cfg.setdefault("insecure_skip_verify", False)
    return cfg


def save_config(cfg: dict) -> None:
    with DEFAULT_CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def build_ssl_context(insecure_skip_verify: bool = False):
    """Build a WSS SSL context that works reliably inside a PyInstaller exe."""
    import ssl

    if insecure_skip_verify:
        return ssl._create_unverified_context()

    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class NotifierBackend:
    """封装 Windows Toast + 声音，自动降级。"""

    def __init__(self, sound_enabled: bool, popup_enabled: bool):
        self.sound_enabled = sound_enabled
        self.popup_enabled = popup_enabled
        self._notifier = None
        self.last_error = ""
        self.identity_status = register_windows_notification_identity()
        try:
            from winotify import Notification  # noqa: F401
            self._notifier = "winotify"
        except Exception:
            try:
                from plyer import notification  # noqa: F401
                self._notifier = "plyer"
            except Exception:
                try:
                    from win10toast import ToastNotifier  # noqa: F401
                    self._notifier = "win10toast"
                except Exception:
                    self._notifier = None

    @property
    def backend_name(self) -> str:
        return self._notifier or "none"

    def beep(self) -> None:
        if not self.sound_enabled:
            return
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass

    def notify(self, title: str, message: str) -> bool:
        if not self.popup_enabled:
            self.last_error = "Toast 已关闭"
            return False
        try:
            if self._notifier == "winotify":
                from winotify import Notification, audio
                toast = Notification(
                    app_id=APP_ID,
                    title=title,
                    msg=message,
                    duration="short",
                )
                toast.set_audio(audio.Default if self.sound_enabled else audio.Silent, loop=False)
                toast.show()
                self.last_error = ""
                return True
            if self._notifier == "plyer":
                from plyer import notification
                notification.notify(title=title, message=message,
                                    app_name=APP_NAME, timeout=10)
                self.last_error = ""
                return True
            if self._notifier == "win10toast":
                from win10toast import ToastNotifier
                ToastNotifier().show_toast(title, message, duration=10,
                                           threaded=True)
                self.last_error = ""
                return True
            self.last_error = "没有可用的 Windows Toast 后端"
            return False
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            return False


class WSClient:
    def __init__(self, cfg: dict, on_alert, on_status, on_settings, on_notice):
        self.cfg = cfg
        self.on_alert = on_alert
        self.on_status = on_status
        self.on_settings = on_settings
        self.on_notice = on_notice
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ws = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._cancel(), self._loop)

    def send_json(self, payload: dict) -> None:
        if not self._loop or self._loop.is_closed():
            self.on_notice("未连接：事件循环未启动")
            return
        asyncio.run_coroutine_threadsafe(self._send_json(payload), self._loop)

    async def _send_json(self, payload: dict) -> None:
        if self._ws is None:
            self.on_notice("未连接：稍后自动重试")
            return
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _cancel(self) -> None:
        for task in asyncio.all_tasks(self._loop):
            if task is not asyncio.current_task(self._loop):
                task.cancel()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            if not self._stop.is_set():
                self.on_status(f"fatal:{e}")
        finally:
            self._loop.close()

    async def _main(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            url, is_wss = self._build_url()
            headers = {"X-Auth-Token": self.cfg["token"]}
            try:
                self.on_status("connecting")
                connect_kwargs = dict(
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=30,
                    max_size=2 ** 22,
                )
                if is_wss:
                    connect_kwargs["ssl"] = build_ssl_context(
                        self.cfg.get("insecure_skip_verify", False)
                    )
                async with websockets.connect(url, **connect_kwargs) as ws:
                    self._ws = ws
                    self.on_status("connected")
                    await ws.send(json.dumps({"type": "recent", "limit": 30}))
                    await ws.send(json.dumps({"type": "get_settings"}))
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        self._handle(raw)
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._ws = None
                self.on_status(f"disconnected:{e}")
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)
            else:
                self._ws = None
                self.on_status("reconnecting")
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)

    def _build_url(self):
        base = self.cfg["server_url"].rstrip("/")
        return base, base.lower().startswith("wss://")

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        if mtype == "alert":
            self.on_alert(msg.get("data", {}))
        elif mtype == "recent":
            for item in reversed(msg.get("data", [])):
                self.on_alert(item, silent=True)
        elif mtype in ("settings", "settings_ack", "settings_updated"):
            self.on_settings(msg.get("data", {}), msg.get("schema", {}), mtype)
        elif mtype == "settings_error":
            self.on_notice(f"参数应用失败：{msg.get('error', '')}")
        elif mtype == "test_ack":
            self.on_notice("服务器测试提醒已发送")


LEVEL_COLORS = {
    "SEVERE": "#d9534f",
    "IMPORTANT": "#f0ad4e",
    "INFO": "#5bc0de",
}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.backend = NotifierBackend(self.cfg.get("sound_enabled", True),
                                       self.cfg.get("popup_enabled", True))
        self.setting_vars: dict[tuple[str, str], tk.StringVar] = {}
        self.settings_schema: dict = {}
        self._row_payload: dict[str, dict] = {}
        self._configured_tags: set[str] = set()
        self._tray_icon = None
        self._tray_thread: Optional[threading.Thread] = None
        self._quitting = False

        root.title("Polymarket 世界杯冷门提醒")
        root.geometry("1080x760")
        root.minsize(920, 660)

        self._build_ui()
        self.ws = self._new_ws_client()
        self.ws.start()

        root.bind("<Unmap>", self._on_unmap)
        self._on_notice(
            f"Toast={self.backend.backend_name}；身份={self.backend.identity_status}"
        )

    def _new_ws_client(self) -> WSClient:
        return WSClient(
            self.cfg,
            on_alert=self._on_alert,
            on_status=self._on_status,
            on_settings=self._on_settings,
            on_notice=self._on_notice,
        )

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        shell = ttk.Frame(self.root, padding=10)
        shell.grid(row=0, column=0, sticky=tk.NSEW)
        shell.columnconfigure(0, weight=1)

        conn = ttk.LabelFrame(shell, text="连接", padding=8)
        conn.grid(row=0, column=0, sticky=tk.EW)
        conn.columnconfigure(1, weight=1)
        conn.columnconfigure(3, weight=1)

        ttk.Label(conn, text="服务器").grid(row=0, column=0, sticky=tk.W)
        self.url_var = tk.StringVar(value=self.cfg["server_url"])
        ttk.Entry(conn, textvariable=self.url_var, width=38).grid(
            row=0, column=1, sticky=tk.EW, padx=(6, 14)
        )

        ttk.Label(conn, text="Token").grid(row=0, column=2, sticky=tk.W)
        self.token_var = tk.StringVar(value=self.cfg["token"])
        ttk.Entry(conn, textvariable=self.token_var, width=34, show="*").grid(
            row=0, column=3, sticky=tk.EW, padx=(6, 14)
        )

        ttk.Button(conn, text="连接 / 重连", command=self._connect_or_reconnect).grid(
            row=0, column=4, padx=(0, 6)
        )
        ttk.Button(conn, text="测试提醒", command=self._send_test_alert).grid(row=0, column=5)

        self.connection_status_var = tk.StringVar(value="连接状态：启动中")
        ttk.Label(conn, textvariable=self.connection_status_var,
                  foreground="#0f6b3f").grid(row=1, column=0, columnspan=6, sticky=tk.W, pady=(6, 0))

        notify = ttk.LabelFrame(shell, text="本机提醒", padding=8)
        notify.grid(row=1, column=0, sticky=tk.EW, pady=(8, 0))
        self.sound_var = tk.BooleanVar(value=self.cfg.get("sound_enabled", True))
        ttk.Checkbutton(notify, text="声音", variable=self.sound_var,
                        command=self._on_toggle_options).grid(row=0, column=0, sticky=tk.W)
        self.popup_var = tk.BooleanVar(value=self.cfg.get("popup_enabled", True))
        ttk.Checkbutton(notify, text="Windows 通知", variable=self.popup_var,
                        command=self._on_toggle_options).grid(row=0, column=1, sticky=tk.W, padx=(18, 0))
        self.tray_var = tk.BooleanVar(value=self.cfg.get("minimize_to_tray", True))
        ttk.Checkbutton(notify, text="最小化到托盘", variable=self.tray_var,
                        command=self._on_toggle_options).grid(row=0, column=2, sticky=tk.W, padx=(18, 0))
        ttk.Button(notify, text="隐藏到托盘", command=self.hide_to_tray).grid(row=0, column=3, padx=(24, 0))

        self.notice_status_var = tk.StringVar(value="操作提示：等待操作")
        ttk.Label(shell, textvariable=self.notice_status_var,
                  foreground="#555").grid(row=2, column=0, sticky=tk.W, pady=(8, 0))

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=1, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))

        alerts_tab = ttk.Frame(notebook, padding=8)
        alerts_tab.columnconfigure(0, weight=1)
        alerts_tab.rowconfigure(1, weight=1)
        notebook.add(alerts_tab, text="提醒列表")

        cols = ("time", "level", "match", "outcome", "price", "usd", "score")
        headers = {"time": "本机时间", "level": "级别", "match": "比赛/市场",
                   "outcome": "Outcome", "price": "价格",
                   "usd": "估算额(USDC)", "score": "冷门分"}
        actions = ttk.Frame(alerts_tab)
        actions.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(0, 6))
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="清空提示", command=self._clear_alerts).grid(
            row=0, column=1, sticky=tk.E
        )
        self.tree = ttk.Treeview(alerts_tab, columns=cols, show="headings", height=16)
        for c in cols:
            self.tree.heading(c, text=headers[c])
            width = 130
            if c == "match":
                width = 360
            elif c == "level":
                width = 90
            elif c == "time":
                width = 150
            self.tree.column(c, width=width, anchor=tk.W)
        self.tree.grid(row=1, column=0, sticky=tk.NSEW)
        self.tree.bind("<Double-1>", self._on_double_click)

        scroll = ttk.Scrollbar(alerts_tab, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky=tk.NS)

        detail_frame = ttk.LabelFrame(alerts_tab, text="提醒详情", padding=6)
        detail_frame.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        detail_frame.columnconfigure(0, weight=1)
        self.detail = tk.Text(detail_frame, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self.detail.grid(row=0, column=0, sticky=tk.EW)

        settings_tab = ttk.Frame(notebook, padding=8)
        settings_tab.columnconfigure(0, weight=1)
        settings_tab.rowconfigure(1, weight=1)
        notebook.add(settings_tab, text="监测参数")

        actions = ttk.Frame(settings_tab)
        actions.grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(actions, text="刷新服务器参数", command=self._request_settings).grid(
            row=0, column=0, padx=(0, 6)
        )
        ttk.Button(actions, text="应用到服务器", command=self._apply_settings_to_server).grid(
            row=0, column=1
        )
        self.settings_status_var = tk.StringVar(value="参数尚未刷新")
        ttk.Label(actions, textvariable=self.settings_status_var,
                  foreground="#555").grid(row=0, column=2, sticky=tk.W, padx=(12, 0))

        settings_canvas = tk.Canvas(settings_tab, highlightthickness=0)
        settings_canvas.grid(row=1, column=0, sticky=tk.NSEW, pady=(10, 0))
        settings_scroll = ttk.Scrollbar(settings_tab, orient=tk.VERTICAL,
                                        command=settings_canvas.yview)
        settings_scroll.grid(row=1, column=1, sticky=tk.NS, pady=(10, 0))
        settings_canvas.configure(yscrollcommand=settings_scroll.set)

        settings_area = ttk.Frame(settings_canvas)
        settings_window = settings_canvas.create_window((0, 0), window=settings_area, anchor=tk.NW)

        def _sync_scroll_region(_event=None) -> None:
            settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))

        def _sync_canvas_width(event) -> None:
            settings_canvas.itemconfigure(settings_window, width=event.width)

        settings_area.bind("<Configure>", _sync_scroll_region)
        settings_canvas.bind("<Configure>", _sync_canvas_width)
        settings_area.columnconfigure(0, weight=1)
        settings_area.columnconfigure(1, weight=1)
        for col, (section, title, fields) in enumerate(SETTING_GROUPS):
            frame = ttk.LabelFrame(settings_area, text=title, padding=10)
            frame.grid(row=0, column=col, sticky=tk.NSEW, padx=(0, 8 if col == 0 else 0))
            frame.columnconfigure(1, weight=1)
            for i, (field, label) in enumerate(fields):
                unit, help_text = PARAM_META.get((section, field), ("", ""))
                label_frame = ttk.Frame(frame)
                label_frame.grid(row=i, column=0, sticky=tk.EW, pady=4)
                ttk.Label(label_frame, text=label).grid(row=0, column=0, sticky=tk.W)
                ttk.Label(label_frame, text=help_text, foreground="#666",
                          wraplength=360).grid(row=1, column=0, sticky=tk.W)
                var = tk.StringVar(value="")
                self.setting_vars[(section, field)] = var
                ttk.Entry(frame, textvariable=var, width=16).grid(
                    row=i, column=1, sticky=tk.EW, padx=(8, 0), pady=3
                )
                ttk.Label(frame, text=unit, foreground="#555", width=10).grid(
                    row=i, column=2, sticky=tk.W, padx=(8, 0), pady=3
                )

    def _connect_or_reconnect(self) -> None:
        self.cfg["server_url"] = self.url_var.get().strip()
        self.cfg["token"] = self.token_var.get().strip()
        save_config(self.cfg)
        self._on_notice("连接配置已更新，正在重连")
        self.ws.stop()
        time.sleep(0.4)
        self.ws = self._new_ws_client()
        self.ws.start()

    def _on_toggle_options(self) -> None:
        self.cfg["sound_enabled"] = self.sound_var.get()
        self.cfg["popup_enabled"] = self.popup_var.get()
        self.cfg["minimize_to_tray"] = self.tray_var.get()
        self.backend.sound_enabled = self.sound_var.get()
        self.backend.popup_enabled = self.popup_var.get()
        save_config(self.cfg)
        self._on_notice("本机提醒选项已更新")

    def _request_settings(self) -> None:
        if hasattr(self, "settings_status_var"):
            self.settings_status_var.set("正在刷新服务器参数")
        self.ws.send_json({"type": "get_settings"})

    def _apply_settings_to_server(self) -> None:
        try:
            payload: dict[str, dict[str, int | float]] = {}
            for (section, field), var in self.setting_vars.items():
                text = var.get().strip()
                if text == "":
                    continue
                value = int(float(text)) if (section, field) in INTEGER_FIELDS else float(text)
                payload.setdefault(section, {})[field] = value
        except ValueError:
            messagebox.showerror("参数错误", "请输入有效数字")
            return
        if not payload:
            messagebox.showinfo("没有参数", "请先刷新服务器参数，或填写需要应用的参数。")
            return
        if hasattr(self, "settings_status_var"):
            self.settings_status_var.set("正在应用到服务器")
        self.ws.send_json({"type": "update_settings", "settings": payload})

    def _send_test_alert(self) -> None:
        self.backend.beep()
        ok = self.backend.notify(
            "PolymarketAlert 本机通知测试",
            "如果你看到这条 Windows 通知，说明本机原生 Toast 已可用。",
        )
        if ok:
            self._on_notice("本机 Toast 测试已触发；同时请求服务器测试推送")
        else:
            self._on_notice(f"本机 Toast 失败：{self.backend.last_error}")
        self.ws.send_json({"type": "test_alert", "note": "Windows 客户端测试提醒"})

    def _on_status(self, status: str) -> None:
        label = self._friendly_status(status)
        self.root.after(0, lambda: self.connection_status_var.set(f"连接状态：{label}"))

    def _on_notice(self, message: str) -> None:
        self.root.after(0, lambda: self.notice_status_var.set(f"操作提示：{message}"))

    @staticmethod
    def _friendly_status(status: str) -> str:
        mapping = {
            "connecting": "正在连接",
            "connected": "已连接",
            "reconnecting": "正在重连",
        }
        if status in mapping:
            return mapping[status]
        if status.startswith("disconnected:"):
            return "连接断开：" + status.split(":", 1)[1]
        return status

    def _on_settings(self, data: dict, schema: dict, source: str) -> None:
        self.root.after(0, lambda: self._render_settings(data, schema, source))

    def _render_settings(self, data: dict, schema: dict, source: str) -> None:
        self.settings_schema = schema or self.settings_schema
        for section, fields in data.items():
            if not isinstance(fields, dict):
                continue
            for field, value in fields.items():
                var = self.setting_vars.get((section, field))
                if var is not None:
                    var.set(str(value))
        if source == "settings_ack":
            self.notice_status_var.set("操作提示：参数已应用到服务器")
            if hasattr(self, "settings_status_var"):
                self.settings_status_var.set("已应用到服务器")
        elif source in ("settings", "settings_updated") and hasattr(self, "settings_status_var"):
            self.settings_status_var.set("已刷新服务器参数")

    def _on_alert(self, data: dict, silent: bool = False) -> None:
        self.root.after(0, lambda: self._render_alert(data, silent))

    def _render_alert(self, data: dict, silent: bool) -> None:
        level = data.get("level", "INFO")
        display_data = dict(data)
        display_data["_server_timestamp"] = data.get("timestamp", "")
        display_data["_local_timestamp"] = (
            format_alert_local_time(data.get("timestamp")) if silent else current_local_time()
        )
        iid = self.tree.insert("", 0, values=(
            display_data["_local_timestamp"],
            level,
            data.get("market_question", "")[:70],
            data.get("outcome_name", ""),
            f"{data.get('current_price', 0):.3f}",
            f"{data.get('estimated_usd', 0):,.0f}",
            f"{data.get('underdog_score', 0):.1f}",
        ))
        tag = f"lvl_{level}"
        if tag not in self._configured_tags:
            self.tree.tag_configure(tag, foreground=LEVEL_COLORS.get(level, "#333"))
            self._configured_tags.add(tag)
        self.tree.item(iid, tags=(tag,))
        self._row_payload[iid] = display_data

        if not silent:
            self.backend.beep()
            ok = self.backend.notify(
                title=f"[{level}] {data.get('outcome_name','')} ${data.get('estimated_usd',0):,.0f}",
                message=f"{display_data['_local_timestamp']}\n"
                        f"{data.get('market_question','')}\n"
                        f"冷门分 {data.get('underdog_score',0)} 价格 {data.get('current_price',0)}",
            )
            if not ok:
                self._on_notice(f"Toast 失败：{self.backend.last_error}")

    def _clear_alerts(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._row_payload.clear()
        self.detail.config(state=tk.NORMAL)
        self.detail.delete("1.0", tk.END)
        self.detail.config(state=tk.DISABLED)
        self._on_notice("提示列表已清空（仅清空本机显示）")

    def _on_double_click(self, _ev) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        data = self._row_payload.get(sel[0], {})
        local_time = data.get("_local_timestamp") or format_alert_local_time(data.get("timestamp"))
        server_time = data.get("_server_timestamp") or data.get("timestamp", "")
        lines = [
            f"本机时间：{local_time}",
            f"服务器时间：{server_time}",
            f"级别：{data.get('level','')}",
            f"比赛：{data.get('match_name','')}",
            f"市场：{data.get('market_question','')}",
            f"Outcome：{data.get('outcome_name','')}",
            f"价格 / 隐含概率：{data.get('current_price','')} ({data.get('implied_prob','')})",
            f"估算成交额：{data.get('estimated_usd',0):,.2f} USDC",
            f"冷门评分：{data.get('underdog_score','')}",
            f"判定原因：{data.get('reason','')}",
            f"短窗价格变化：{data.get('price_change_short','')}",
            f"短窗成交量：{data.get('volume_change_short','')}",
            f"链接：{data.get('market_url','')}",
            "",
            f"注意：{data.get('disclaimer','本提醒不构成投注建议。')}",
        ]
        self.detail.config(state=tk.NORMAL)
        self.detail.delete("1.0", tk.END)
        self.detail.insert(tk.END, "\n".join(lines))
        self.detail.config(state=tk.DISABLED)

    def _on_unmap(self, _ev) -> None:
        if self._quitting or not self.tray_var.get():
            return
        self.root.after(80, self._hide_if_iconic)

    def _hide_if_iconic(self) -> None:
        if self.root.state() == "iconic":
            self.hide_to_tray()

    def hide_to_tray(self) -> None:
        if not self.tray_var.get():
            self.root.iconify()
            return
        if self._ensure_tray():
            self.root.withdraw()
        else:
            self.root.iconify()

    def _ensure_tray(self) -> bool:
        if self._tray_icon is not None:
            return True
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return False

        image = Image.new("RGB", (64, 64), "#1f6feb")
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 54, 54), fill="#ffffff")
        draw.rectangle((16, 16, 48, 48), fill="#1f6feb")
        draw.text((23, 20), "P", fill="#ffffff")

        self._tray_icon = pystray.Icon(
            "PolymarketAlert",
            image,
            "PolymarketAlert",
            menu=pystray.Menu(
                pystray.MenuItem("显示", lambda _icon, _item: self.root.after(0, self.show_from_tray)),
                pystray.MenuItem("退出", lambda _icon, _item: self.root.after(0, self.quit_app)),
            ),
        )
        self._tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
        self._tray_thread.start()
        return True

    def show_from_tray(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def quit_app(self) -> None:
        self._quitting = True
        try:
            self.ws.stop()
        except Exception:
            pass
        try:
            if self._tray_icon:
                self._tray_icon.stop()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.hide_to_tray)
    root.mainloop()


if __name__ == "__main__":
    main()
