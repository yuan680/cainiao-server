"""
cainiao_helper.py - 菜鸟物流查询剪贴板助手
监视剪贴板，自动识别菜鸟单号 (LP开头) 并查询物流状态，
查询结果自动复制到剪贴板 + 右下角托盘提示。
运行方式：pythonw cainiao_helper.py 或 编译的 EXE
"""

import re
import json
import sys
import os
import urllib.request
import urllib.error
import win32clipboard
import win32gui
import win32api
import win32con
import ctypes
import ctypes.wintypes
import traceback

TRACKING_PATTERN = re.compile(r'(LP\d{14,20})')

class CainiaoHelper:
    def __init__(self):
        self.last_text = ""
        self.hwnd = None
        self.tray_icon_data = None

    # ── 查询本地 API ──────────────────────────────────────────

    def query_tracking(self, mail_no):
        url = f"http://localhost/query?mailNo={mail_no}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"error": str(e)}

    def format_result(self, data):
        if "error" in data:
            return f"查询失败: {data['error']}"
        if "data" in data and data["data"]:
            item = data["data"][0]
            status = item.get("status", "")
            code = item.get("statusCode", "")
            origin = item.get("origin", "")
            dest = item.get("dest", "")
            t = item.get("latestTime", "")
            e = item.get("latestEvent", "")
            return f"状态: {status} ({code})\n路线: {origin} → {dest}\n时间: {t}\n事件: {e}"
        return "未找到物流信息"

    def get_status_summary(self, data):
        """短状态（用于托盘气泡）"""
        if "data" in data and data["data"]:
            return data["data"][0].get("status", "查询成功")
        return "查询失败"

    # ── 窗口过程 ──────────────────────────────────────────────

    def wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_CLIPBOARDUPDATE:
            self.on_clipboard_change()
        elif msg == win32con.WM_DESTROY:
            self.remove_tray_icon()
            win32gui.PostQuitMessage(0)
        elif msg == win32con.WM_COMMAND:
            lo = wparam & 0xFFFF
            if lo == 1001:  # 退出
                win32gui.DestroyWindow(hwnd)
            elif lo == 1002:  # 关于
                win32api.MessageBox(hwnd, "菜鸟物流查询助手 v1.0\n复制 LP 开头的单号自动查询", "关于", win32con.MB_OK)
        elif msg == self.WM_TRAYICON:
            if lparam == win32con.WM_LBUTTONDBLCLK:
                # 双击显示最后查询结果
                pass
            elif lparam == win32con.WM_RBUTTONUP:
                self.show_tray_menu(hwnd)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # ── 托盘图标 ──────────────────────────────────────────────

    WM_TRAYICON = win32con.WM_USER + 100

    def add_tray_icon(self, hwnd):
        """添加系统托盘图标"""
        hinst = win32api.GetModuleHandle(None)
        try:
            icon = win32gui.LoadIcon(None, win32con.IDI_INFORMATION)
        except:
            icon = win32gui.LoadIcon(None, win32con.IDI_APPLICATION)
        self.tray_icon_data = (
            hwnd,      # hwnd
            0,         # uID
            win32con.NIF_ICON | win32con.NIF_MESSAGE | win32con.NIF_TIP,
            self.WM_TRAYICON,  # uCallbackMessage
            icon,      # hIcon
            "菜鸟物流助手",  # tip
            0, 0, 0   # uTimeout, uVersion, ???
        )
        try:
            win32gui.Shell_NotifyIcon(win32con.NIM_ADD, self.tray_icon_data)
        except:
            pass

    def remove_tray_icon(self):
        if self.tray_icon_data:
            try:
                win32gui.Shell_NotifyIcon(win32con.NIM_DELETE, self.tray_icon_data)
            except:
                pass

    def show_tray_balloon(self, title, msg):
        """显示托盘气泡通知"""
        if not self.tray_icon_data:
            return
        balloon_data = (
            self.tray_icon_data[0],  # hwnd
            self.tray_icon_data[1],  # uID
            win32con.NIF_INFO,
            0, 0, 0, 0,  # unused
            title,
            msg,
            win32con.NIIF_INFO,
            10,  # timeout seconds
            0
        )
        try:
            win32gui.Shell_NotifyIcon(win32con.NIM_MODIFY, balloon_data)
        except:
            pass

    def show_tray_menu(self, hwnd):
        """右键菜单"""
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, 1002, "关于")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "-")
        win32gui.AppendMenu(menu, win32con.MF_STRING, 1001, "退出")
        # 显示菜单
        pos = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(hwnd)
        win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN, pos[0], pos[1], 0, hwnd, None)
        win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)

    # ── 剪贴板处理 ───────────────────────────────────────────

    def on_clipboard_change(self):
        """当剪贴板内容变化时触发"""
        try:
            win32clipboard.OpenClipboard(self.hwnd)
            try:
                data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            except TypeError:
                data = None
            win32clipboard.CloseClipboard()
        except Exception:
            return

        if data and data != self.last_text:
            match = TRACKING_PATTERN.search(data)
            if match:
                mail_no = match.group(1)
                # 查询 API
                api_data = self.query_tracking(mail_no)
                result = self.format_result(api_data)
                summary = self.get_status_summary(api_data)

                # 将结果写入剪贴板
                try:
                    win32clipboard.OpenClipboard(self.hwnd)
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(result, win32con.CF_UNICODETEXT)
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass

                # 托盘提示
                self.show_tray_balloon(f"菜鸟物流 - {mail_no}", f"{summary}\n结果已复制到剪贴板")

                # 提示音
                try:
                    win32api.MessageBeep(win32con.MB_OK)
                except:
                    pass

        self.last_text = data or ""

    # ── 主循环 ────────────────────────────────────────────────

    def run(self):
        # 注册窗口类
        hinst = win32api.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self.wnd_proc
        wc.lpszClassName = "CainiaoHelperClass"
        wc.hInstance = hinst
        try:
            class_atom = win32gui.RegisterClass(wc)
        except Exception:
            # 可能已注册
            class_atom = win32gui.GetClassInfo(hinst, "CainiaoHelperClass")

        self.hwnd = win32gui.CreateWindow(
            class_atom, "CainiaoHelper", 0,
            0, 0, 0, 0, 0, 0, hinst, None
        )

        # 注册剪贴板监听 (Windows Vista+)
        ctypes.windll.user32.AddClipboardFormatListener(self.hwnd)

        # 系统托盘图标
        self.add_tray_icon(self.hwnd)

        # 消息循环
        win32gui.PumpMessages()

        # 清理
        ctypes.windll.user32.RemoveClipboardFormatListener(self.hwnd)


if __name__ == "__main__":
    helper = CainiaoHelper()
    try:
        helper.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # 写错误日志（无窗口程序看不到 stderr）
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helper_error.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
