#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Sheets 写入模块 — 供 cainiao_server.py 调用
===================================================
用法:  from gsheet_writer import GoogleSheetsWriter

支持两种认证方式（二选一）:
  A) Service Account (推荐) — 文件路径写在 GOOGLE_SHEETS_CREDENTIALS 或 gsheet_creds.json
  B) OAuth2 桌面端 — 运行 python gsheet_setup.py 一次性完成浏览器授权

配置优先级（从上到下）:
  1. 环境变量 GOOGLE_SHEETS_CREDENTIALS → JSON 文件路径
  2. 当前目录下 gsheet_creds.json
  3. 当前目录下 credentials.json（OAuth 客户端 ID）
  4. gcloud auth application-default (ADC)
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import List

logger = logging.getLogger("gsheet")

# ============================================================
# 配置项（可被环境变量覆盖）
# ============================================================
# 默认 Google Sheet ID（可在 Google Sheets URL 中找到）
# https://docs.google.com/spreadsheets/d/【THIS_IS_SHEET_ID】/edit
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

# 默认工作表名称 (Sheet name / tab 名)
GOOGLE_SHEET_WORKSHEET = os.environ.get("GOOGLE_SHEET_WORKSHEET", "Sheet1")

# 写入范围 e.g. "A:H" 或 "A1:Z1000" 留空则自动
GOOGLE_SHEET_RANGE = os.environ.get("GOOGLE_SHEET_RANGE", "")


# 用于 gspread 的 scope
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


class GoogleSheetsWriter:
    """将 JSON 记录写入 Google Sheets。"""

    def __init__(self, creds_path: str = ""):
        """
        creds_path: 可指定凭证文件路径（JSON）
          - 后缀 .json 且含有 "type": "service_account" → 服务账号
          - 后缀 .json 且含有 "installed"/"web" → OAuth2 客户端 ID
          - 留空则依次尝试环境变量 → 默认文件 → ADC
        """
        self._client = None
        self._sheet = None
        self._worksheet_name = GOOGLE_SHEET_WORKSHEET
        self._sheet_id = GOOGLE_SHEET_ID
        self._creds_path = creds_path

    # ----------------------------------------------------------
    # 公共方法
    # ----------------------------------------------------------
    def set_sheet(self, sheet_id: str, worksheet: str = ""):
        """设置目标表格。"""
        self._sheet_id = sheet_id
        if worksheet:
            self._worksheet_name = worksheet
        self._sheet = None  # 清除缓存

    def write_row(self, row: list) -> bool:
        """追加一行到工作表末尾。"""
        client = self._ensure_client()
        if not client:
            return False
        ws = self._ensure_worksheet(client)
        if not ws:
            return False
        try:
            ws.append_row(row, value_input_option="USER_ENTERED")
            logger.info("✅ 已写入 1 行到 Google Sheets")
            return True
        except Exception as e:
            logger.error(f"写入 Google Sheets 失败: {e}")
            return False

    def write_rows(self, rows: List[List]) -> bool:
        """批量追加多行。"""
        client = self._ensure_client()
        if not client:
            return False
        ws = self._ensure_worksheet(client)
        if not ws:
            return False
        try:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"✅ 已写入 {len(rows)} 行到 Google Sheets")
            return True
        except Exception as e:
            logger.error(f"批量写入 Google Sheets 失败: {e}")
            return False

    def ensure_header(self, header_row: list) -> bool:
        """如果首行无数据则写入表头。"""
        client = self._ensure_client()
        if not client:
            return False
        ws = self._ensure_worksheet(client)
        if not ws:
            return False
        try:
            existing = ws.get_all_values(max_rows=1)
            if existing and existing[0] and any(cell.strip() for cell in existing[0]):
                return True  # 表头已存在
            ws.insert_row(header_row, index=1)
            logger.info(f"✅ 已写入表头: {header_row}")
            return True
        except Exception as e:
            logger.warning(f"写入表头失败: {e}")
            return True  # 不阻断主流程

    # ----------------------------------------------------------
    # 内部方法
    # ----------------------------------------------------------
    def _ensure_client(self):
        """获取或创建 gspread 客户端。"""
        if self._client is not None:
            return self._client

        try:
            import gspread
            from google.auth import default
            from google.oauth2 import service_account
            from google.oauth2.credentials import Credentials
        except ImportError:
            logger.error("缺少依赖。请运行: pip install gspread google-auth")
            return None

        creds = None

        # 1) 指定的凭证文件路径
        if self._creds_path:
            creds = self._load_creds_file(self._creds_path)

        # 2) 环境变量 GOOGLE_SHEETS_CREDENTIALS
        if creds is None:
            env_path = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
            if env_path:
                creds = self._load_creds_file(env_path)

        # 3) 当前目录下 gsheet_creds.json（服务账号）
        if creds is None:
            local_sa = Path("gsheet_creds.json")
            if local_sa.exists():
                creds = self._load_creds_file(str(local_sa))

        # 4) 当前目录下 credentials.json（OAuth2 客户端 ID）
        if creds is None:
            local_oauth = Path("credentials.json")
            if local_oauth.exists():
                creds = self._load_creds_file(str(local_oauth))

        # 5) gcloud ADC (application default credentials)
        if creds is None:
            try:
                creds, _ = default(scopes=_SCOPES)
                logger.info("使用 gcloud ADC (application default credentials)")
            except Exception as e:
                logger.warning(f"gcloud ADC 不可用: {e}")

        if creds is None:
            logger.error(
                "未找到任何 Google 凭证。请:\n"
                "  1) 使用服务账号: 将 JSON 保存为 gsheet_creds.json\n"
                "  2) 或运行: python gsheet_setup.py 完成 OAuth 授权\n"
                "  3) 或设置: set GOOGLE_SHEETS_CREDENTIALS=路径"
            )
            return None

        try:
            self._client = gspread.authorize(creds)
            logger.info("gspread 客户端已就绪")
            return self._client
        except Exception as e:
            logger.error(f"gspread 授权失败: {e}")
            return None

    def _load_creds_file(self, path: str):
        """从 JSON 文件加载凭证（服务账号 / OAuth2）。"""
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials

        path = path.strip().strip('"').strip("'")
        p = Path(path)
        if not p.exists():
            logger.warning(f"凭证文件不存在: {path}")
            return None

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取凭证文件失败: {path} — {e}")
            return None

        # 服务账号
        if data.get("type") == "service_account":
            try:
                creds = service_account.Credentials.from_service_account_info(
                    data, scopes=_SCOPES
                )
                logger.info(f"使用服务账号: {data.get('client_email', '?')}")
                return creds
            except Exception as e:
                logger.warning(f"服务账号解析失败: {e}")
                return None

        # OAuth2 客户端 ID（需要先执行授权流获得 refresh token）
        if "installed" in data or "web" in data:
            logger.warning(
                f"发现 OAuth 客户端 ID 文件 ({path})，请先运行 python gsheet_setup.py"
            )
            return None

        # 已授权的 token（通过 setup 脚本生成）
        if data.get("token") or data.get("refresh_token"):
            try:
                creds = Credentials.from_authorized_user_info(data, _SCOPES)
                logger.info("使用已保存的 OAuth token")
                return creds
            except Exception as e:
                logger.warning(f"OAuth token 解析失败: {e}")
                return None

        logger.warning(f"无法识别的凭证文件格式: {path}")
        return None

    def _ensure_worksheet(self, client):
        """获取或缓存 worksheet 对象。"""
        if self._sheet is not None:
            return self._sheet
        if not self._sheet_id:
            logger.error("未设置 GOOGLE_SHEET_ID")
            return None
        try:
            sh = client.open_by_key(self._sheet_id)
            self._sheet = sh.worksheet(self._worksheet_name)
            logger.info(
                f"工作表已打开: {sh.title} / {self._worksheet_name}"
            )
            return self._sheet
        except Exception as e:
            logger.error(f"打开 Google Sheets 失败: {e}")
            return None


# ----------------------------------------------------------
# 便捷函数 — 提供给 cainiao_server.py 单次调用
# ----------------------------------------------------------
_singleton_writer = None


def write_to_google_sheets(
    header: list,
    row: list,
    sheet_id: str = "",
    worksheet: str = "",
    creds_path: str = "",
) -> bool:
    """便捷函数：向 Google Sheets 追加一行（自动处理认证缓存）。"""
    global _singleton_writer

    if _singleton_writer is None:
        _singleton_writer = GoogleSheetsWriter(creds_path=creds_path)
        if sheet_id:
            _singleton_writer.set_sheet(sheet_id, worksheet)

    actual_sheet_id = sheet_id or GOOGLE_SHEET_ID
    if not actual_sheet_id:
        logger.error("未配置 GOOGLE_SHEET_ID")
        return False

    _singleton_writer.set_sheet(actual_sheet_id, worksheet)
    _singleton_writer.ensure_header(header)
    return _singleton_writer.write_row(row)
