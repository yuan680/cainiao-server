#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Sheets OAuth2 授权设置脚本（一次性）
=============================================
为 cainiao_server.py 提供 Google Sheets 写入所需的 OAuth 授权。

用法:
  1. 打开 https://console.cloud.google.com/apis/credentials
  2. 创建 OAuth 客户端 ID → 桌面应用 → 下载 JSON → 保存为 credentials.json
  3. 在 Google Cloud Console 启用 Google Sheets API
  4. 运行: python gsheet_setup.py
  5. 在浏览器中授权，完成后会生成 token.json
  6. 设置环境变量:
       set GOOGLE_SHEET_ID=你的SheetID
       set GOOGLE_SHEET_CREDENTIALS=token.json
"""

import json
import os
import sys
from pathlib import Path

# ============================================================
# 配置
# ============================================================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
CLIENT_SECRET_FILE = "credentials.json"
TOKEN_FILE = "token.json"


def main():
    print("=" * 60)
    print("  Google Sheets OAuth2 授权设置")
    print("=" * 60)

    # --- 检查客户端 ID 文件 ---
    cs_path = Path(CLIENT_SECRET_FILE)
    if not cs_path.exists():
        print(f"\n❌ 未找到 {CLIENT_SECRET_FILE}")
        print("\n请先在 Google Cloud Console 创建 OAuth 客户端 ID：")
        print("  1. 打开 https://console.cloud.google.com/apis/credentials")
        print("  2. 点击「创建凭据」→「OAuth 客户端 ID」")
        print("  3. 应用类型: 「桌面应用」")
        print("  4. 下载 JSON，保存为 credentials.json")
        print("  5. 在「API 和服务」→「库」中搜索并启用 Google Sheets API")
        sys.exit(1)

    try:
        client_config = json.loads(cs_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"\n❌ 读取 {CLIENT_SECRET_FILE} 失败: {e}")
        sys.exit(1)

    if "installed" not in client_config and "web" not in client_config:
        print(f"\n❌ {CLIENT_SECRET_FILE} 格式不正确，不是 OAuth 客户端 ID")
        print("请确认下载的是「OAuth 客户端 ID」而非「服务账号」")
        sys.exit(1)

    # --- 检查已有 token ---
    tk_path = Path(TOKEN_FILE)
    if tk_path.exists():
        try:
            token_data = json.loads(tk_path.read_text(encoding="utf-8"))
            if token_data.get("refresh_token"):
                print(f"\n✅ 已有有效 token ({TOKEN_FILE})")
                print("如需重新授权，请删除 token.json 后重试")
                _print_next_steps()
                return
        except Exception:
            pass

    # --- 启动 OAuth 授权 ---
    print(f"\n正在启动本地浏览器进行 OAuth 授权…")
    print(f"请确保 {CLIENT_SECRET_FILE} 对应的 API 已启用 Google Sheets API\n")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            CLIENT_SECRET_FILE, SCOPES
        )
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            prompt="consent",
        )

        # 保存 token
        token_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }
        tk_path.write_text(
            json.dumps(token_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\n✅ OAuth 授权成功！token 已保存到 {TOKEN_FILE}")

    except ImportError:
        print("\n⚠ 缺少 google-auth-oauthlib 依赖")
        print("请运行: pip install google-auth-oauthlib")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ OAuth 授权失败: {e}")
        sys.exit(1)

    _print_next_steps()


def _print_next_steps():
    print("\n" + "-" * 60)
    print("下一步：")
    print(f"  1. 设置环境变量 GOOGLE_SHEET_ID=你的SheetID")
    print(f"  2. 设置环境变量 GOOGLE_SHEET_CREDENTIALS={TOKEN_FILE}")
    print(f"  3. 在 Google Sheet 中确认数据已写入")
    print("-" * 60)
    print(f"\n或者使用服务账号（更简单）：")
    print(f"  1. 在 Google Cloud Console 创建服务账号")
    print(f"  2. 下载 JSON 密钥 → 保存为 gsheet_creds.json")
    print(f"  3. 将目标 Google Sheet 共享给服务账号邮箱")
    print(f"  4. 设置 GOOGLE_SHEET_ID=你的SheetID")
    print()

    # 提示设置环境变量
    print("Windows PowerShell 设置:")
    print(f'  $env:GOOGLE_SHEET_ID = "你的SheetID"')
    print(f'  $env:GOOGLE_SHEET_CREDENTIALS = "{TOKEN_FILE}"')
    print("CMD:")
    print(f'  set GOOGLE_SHEET_ID=你的SheetID')
    print(f'  set GOOGLE_SHEET_CREDENTIALS={TOKEN_FILE}')
    print()


if __name__ == "__main__":
    main()
