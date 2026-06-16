"""
小财家记账 Telegram Bot + Flask Web 看板
部署环境变量: TELEGRAM_TOKEN, WEBHOOK_URL, PORT (可选)
"""

import json
import logging
import os
import random
import re
import sqlite3
import io
from datetime import datetime, timedelta

import pytz
import requests
import telebot
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TOKEN = (
    os.environ.get("TELEGRAM_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or ""
).strip()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://caicai-799gg.onrender.com").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))

# ========== 品牌与价格（复制新机器人时主要改这里）==========
BOT_NAME = "小财家"
BOT_BRAND = f"{BOT_NAME}记账"
PRICE_1_MONTH = 80
PRICE_2_MONTH = 140
PRICE_3_MONTH = 220

FOUNDER_USERS = [8551762310]
# 卖家联系方式：陌生人想买第二款机器人时展示。可填用户名，或留空自动读 SELLER_USER_ID 的 @用户名
SELLER_USER_ID = int(os.environ.get("SELLER_USER_ID", str(FOUNDER_USERS[0])))
SELLER_USERNAME = os.environ.get("SELLER_USERNAME", "Baima86").strip().lstrip("@")
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"
MAX_LEVEL2_VIPS = 5
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
SETTING_KEYS = {
    "operators", "exchange_rate", "fee_rate", "is_active",
    "language", "timezone", "show_usdt", "expire_time",
}

if not TOKEN:
    raise RuntimeError(
        "缺少 TELEGRAM_TOKEN 环境变量。"
        "请在 Render → Environment 里添加 TELEGRAM_TOKEN=你的BotToken"
    )

bot = telebot.TeleBot(TOKEN)
flask_app = Flask(__name__)
USER_STATE = {}
_CACHED_BOT_NAME = None


def refresh_bot_display_name():
    """从 Telegram 读取当前机器人对外显示名字，并写入缓存。"""
    global _CACHED_BOT_NAME
    try:
        info = bot.get_my_name()
        name = ""
        if info is not None:
            name = (getattr(info, "name", None) or "").strip()
        _CACHED_BOT_NAME = name or BOT_NAME
    except Exception as exc:
        log.warning("get_my_name failed, fallback to default: %s", exc)
        _CACHED_BOT_NAME = BOT_NAME
    return _CACHED_BOT_NAME


def get_bot_display_name():
    """买家通过 set_my_name 改过的名字；未改过则用默认 BOT_NAME。"""
    if _CACHED_BOT_NAME:
        return _CACHED_BOT_NAME
    return refresh_bot_display_name()


def get_bot_short_name():
    """品牌用简称，如「老弟机器人」→「老弟」。"""
    name = get_bot_display_name()
    if name.endswith("机器人"):
        return name[:-3]
    return name


def get_bot_brand():
    """如「老弟记账」「小财家记账」。"""
    return f"{get_bot_short_name()}记账"


def get_bot_join_name():
    """入群欢迎语里的自称，如「老弟机器人」。"""
    name = get_bot_display_name()
    if name.endswith("机器人"):
        return name
    return f"{name}机器人"

# ---------------------------------------------------------------------------
# Blockchain
# ---------------------------------------------------------------------------
def fetch_blockchain_usdt_info(address):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(f"https://api.trongrid.io/v1/accounts/{address}", headers=headers, timeout=10)
        balance = 0.0
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success") and data.get("data"):
                for item in data["data"][0].get("trc20", []):
                    if USDT_CONTRACT in item:
                        balance = float(item[USDT_CONTRACT]) / 1_000_000
                        break

        history_text = ""
        try:
            tx_resp = requests.get(
                f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
                f"?limit=5&contract_address={USDT_CONTRACT}",
                headers=headers,
                timeout=10,
            )
            if tx_resp.status_code == 200:
                tx_list = tx_resp.json().get("data", [])
                if not tx_list:
                    history_text = "  暂无最近的 USDT 转账流水。"
                else:
                    for tx in tx_list:
                        from_addr = tx.get("from", "")
                        to_addr = tx.get("to", "")
                        raw_val = tx.get("value", tx.get("amount", "0"))
                        amount = float(raw_val) / 1_000_000 if raw_val else 0.0
                        if from_addr.lower() == address.lower():
                            direction, peer = "🔴 支出", f"去往: {to_addr[:6]}***{to_addr[-6:]}"
                        else:
                            direction, peer = "🟢 收入", f"来自: {from_addr[:6]}***{from_addr[-6:]}"
                        history_text += f"  {direction} | <b>{amount:.2f} U</b>\n  └ <i>{peer}</i>\n"
            else:
                history_text = "  ⚠️ 暂时无法获取流水明细（公共通道高频受限）。"
        except Exception:
            history_text = "  ⚠️ 链上网络拥堵，流水加载失败。"

        return {"success": True, "balance": balance, "history": history_text}
    except Exception as exc:
        return {"success": False, "msg": str(exc)}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect("bot_data.db", timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            group_id INTEGER PRIMARY KEY,
            operators TEXT DEFAULT '[]',
            exchange_rate REAL DEFAULT 7.2,
            fee_rate REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            language TEXT DEFAULT 'chinese',
            timezone TEXT DEFAULT 'Asia/Shanghai',
            show_usdt INTEGER DEFAULT 1,
            expire_time TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            user_id INTEGER,
            username TEXT,
            remark TEXT,
            amount REAL,
            usdt_amount REAL,
            exchange_rate REAL,
            bill_type TEXT,
            timestamp TEXT,
            date_str TEXT,
            is_settled INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS vip_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            expire_time TEXT,
            level INTEGER DEFAULT 2
        )
    """)
    conn.commit()
    conn.close()


init_db()


def get_current_time(timezone_str="Asia/Shanghai"):
    try:
        tz = pytz.timezone(timezone_str)
    except Exception:
        tz = pytz.timezone("Asia/Shanghai")
    now = datetime.now(tz)
    return now, now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")


def get_user_permission_level(user_id):
    if user_id in FOUNDER_USERS:
        return True, "最高级买家 (系统创始人)", "永久终身授权", 1

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT expire_time, level FROM vip_users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        if row:
            expire = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            if datetime.now() < expire:
                lvl = row[1] or 2
                desc = "最高级买家 (VIP1)" if lvl == 1 else "权限人 (二级VIP2)"
                return True, desc, row[0], lvl
            return False, "已到期", row[0], 0
    except Exception as exc:
        log.exception("get_user_permission_level: %s", exc)
    return False, "普通用户", "未激活", 0


def add_vip_user(user_id, username, months=12, level=2):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT expire_time FROM vip_users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    now = datetime.now()
    if row:
        try:
            current = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            base = current if current > now else now
        except Exception:
            base = now
    else:
        base = now
    expire_str = (base + timedelta(days=30 * months)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT OR REPLACE INTO vip_users (user_id, username, expire_time, level) VALUES (?, ?, ?, ?)",
        (user_id, username, expire_str, level),
    )
    conn.commit()
    conn.close()
    return expire_str


def get_level2_vip_count():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM vip_users WHERE level = 2 AND expire_time > ?",
            (now_str,),
        )
        count = c.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def get_all_level2_vips():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT user_id, username FROM vip_users WHERE level = 2 AND expire_time > ?",
            (now_str,),
        )
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def remove_vip_user(user_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM vip_users WHERE user_id = ? AND level = 2", (user_id,))
        deleted = c.rowcount > 0
        conn.commit()
        conn.close()
        return deleted
    except Exception:
        return False


def get_active_vip1_buyer_id():
    """当前已购机的唯一 VIP1 买家 UID；无人购买时返回 None。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT user_id FROM vip_users WHERE level = 1 AND expire_time > ? LIMIT 1",
            (now_str,),
        )
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def can_submit_purchase(user_id):
    """是否允许走购买/续费流程（本机仅一位买家，其他人需联系卖家）。"""
    if user_id in FOUNDER_USERS:
        return True
    buyer_id = get_active_vip1_buyer_id()
    if buyer_id is None:
        return True
    return user_id == buyer_id


def get_seller_contact_line():
    """卖家 Telegram 联系方式（HTML）。"""
    if SELLER_USERNAME:
        return f'<a href="https://t.me/{SELLER_USERNAME}">@{SELLER_USERNAME}</a>'
    try:
        chat = bot.get_chat(SELLER_USER_ID)
        if getattr(chat, "username", None):
            return f'<a href="https://t.me/{chat.username}">@{chat.username}</a>'
    except Exception as exc:
        log.warning("get seller username failed: %s", exc)
    return f"UID <code>{SELLER_USER_ID}</code>"


def build_bot_sold_message():
    contact = get_seller_contact_line()
    return (
        "⚠️ <b>本机器人已有人购买。</b>\n\n"
        f"如需购买同款机器人，请联系卖家：{contact}"
    )


def build_manual_guide_text(lang="zh"):
    lang = normalize_lang_code(lang)
    c = CMD[lang]
    brand = get_bot_brand() if lang == "zh" else get_bot_brand()
    short = get_bot_short_name()
    if lang == "eng":
        return (
            f"📖 <b>[{brand}] User Guide (English)</b>\n\n"
            f"🤖 Welcome to <b>{short}</b> bot.\n\n"
            "👑 <b>Roles:</b>\n"
            "1. <b>VIP1 buyer</b>: private menu, rename bot, set VIP2.\n"
            "2. <b>VIP2</b>: assign group operators.\n"
            "3. <b>Operator</b>: bookkeeping in group.\n\n"
            f"👥 <b>Group commands</b> (English mode only):\n"
            f"• <code>{c['class_start']}</code> / <code>{c['class_end']}</code>\n"
            f"• <code>{c['set_operator']} @user @user2</code>\n"
            f"• <code>{c['remove_operator']} @user</code>\n"
            f"• <code>{c['set_rate']} 7.4</code>\n"
            f"• <code>{c['set_fee']} 5</code>\n"
            f"• <code>+1000</code> / <code>note+99</code>\n"
            f"• <code>{c['expense']} 800</code>\n"
            f"• <code>{c['bill_zero']}</code> — view bill\n"
            f"• <code>{c['view_remark']} remark</code>\n\n"
            f"🌐 <b>Change language:</b> <code>{c['lang_change']}</code>\n\n"
            f"🗑️ <b>Delete:</b> <code>{c['delete_last']}</code>, "
            f"<code>{c['delete_remark']} remark</code>, "
            f"<code>{c['delete_today']}</code>, <code>{c['delete_all']}</code>\n\n"
            f"🔍 <b>USDT address:</b> <code>{c['view_chain']} T...34chars</code>\n\n"
            "🎨 <b>Private menu (VIP1):</b> rename bot / change avatar"
        )
    if lang == "my":
        return (
            f"📖 <b>[{brand}] အသုံးပြုလမ်းညွှန် (Myanmar)</b>\n\n"
            f"🤖 <b>{short}</b> bot ကို ကြိုဆိုပါသည်。\n\n"
            "👑 <b>အခန်းကဏ္ဍ：</b>\n"
            "1. <b>VIP1 buyer</b>：private menu、bot အမည်ပြောင်း、VIP2 သတ်မှတ်\n"
            "2. <b>VIP2</b>：operator သတ်မှတ်\n"
            "3. <b>Operator</b>：အုပ်စုတွင်မှတ်တမ်းတင်\n\n"
            f"👥 <b>အုပ်စုအမိန့်</b>（Myanmar mode သာ）：\n"
            f"• <code>{c['class_start']}</code> / <code>{c['class_end']}</code>\n"
            f"• <code>{c['set_operator']} @user</code>\n"
            f"• <code>{c['remove_operator']} @user</code>\n"
            f"• <code>{c['set_rate']} 7.4</code>\n"
            f"• <code>{c['set_fee']} 5</code>\n"
            f"• <code>+1000</code> / <code>note+99</code>\n"
            f"• <code>{c['expense']} 800</code>\n"
            f"• <code>{c['bill_zero']}</code>\n"
            f"• <code>{c['view_remark']} remark</code>\n\n"
            f"🌐 <b>ဘာသာပြောင်း：</b> <code>{c['lang_change']}</code>\n\n"
            f"🗑️ <b>ဖျက်ရန်：</b> <code>{c['delete_last']}</code>, "
            f"<code>{c['delete_remark']} remark</code>, "
            f"<code>{c['delete_today']}</code>, <code>{c['delete_all']}</code>\n\n"
            f"🔍 <b>USDT：</b> <code>{c['view_chain']} T...34chars</code>\n\n"
            "🎨 <b>Private (VIP1)：</b> bot အမည်/avatar"
        )
    return (
        f"📖 <b>【{get_bot_brand()}】全功能业务操作指南</b>\n\n"
        f"🤖 欢迎使用 <b>{get_bot_short_name()}</b> 机器人，以下为常用指令：\n\n"
        "👑 <b>权限架构：</b>\n"
        "1. <b>最高级买家</b>：私聊菜单，可改机器人名字/头像，可指派二级权限人。\n"
        "2. <b>权限人(VIP2)</b>：可进群指派群操作人。\n"
        "3. <b>操作人</b>：群内专职记账。\n\n"
        f"👥 <b>群内指令集</b>（中文模式专用）：\n"
        f"• <code>{c['class_start']}</code> / <code>{c['class_end']}</code> — 开启或封存今日记账\n"
        f"• <code>{c['set_operator']} @用户名 @用户名2</code> — 可一次设置多个\n"
        f"• <code>{c['remove_operator']} @用户名</code>\n"
        f"• <code>{c['set_rate']} 7.4</code>\n"
        f"• <code>{c['set_fee']} 5</code> — 费率 5 表示 5%\n"
        f"• <code>+1000</code> / <code>老弟+99</code> — 记入款\n"
        f"• <code>+1000/7.3</code> — 指定汇率入款\n"
        f"• <code>{c['expense']} 800</code> — 记下发（USDT）\n"
        f"• <code>{c['bill_zero']}</code> — 查看今日账单\n"
        f"• <code>{c['view_remark']} 备注名</code> — 查看某备注今日明细\n\n"
        f"🌐 <b>切换群语言</b>（买家/权限人）：<code>{c['lang_change']}</code>\n\n"
        f"🗑️ <b>删账命令</b>（需操作权限）：\n"
        f"• <code>{c['delete_last']}</code> — 撤销最近一笔\n"
        f"• <code>{c['delete_remark']} 备注名</code> — 删当天该备注的所有进单\n"
        f"• <code>{c['delete_today']}</code> — 清空本群今日账单\n"
        f"• <code>{c['delete_all']}</code> — 清空本群全部历史账单\n\n"
        f"🔍 <b>查询 USDT 地址</b>（群/私聊均可）：\n"
        f"• <code>{c['view_chain']} T开头的34位波场地址</code>\n"
        f"• 例：<code>{c['view_chain']} TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw</code>\n"
        f"• （群内 <code>{c['view_remark']} 备注名</code> 为查进单，不是查链上地址）\n\n"
        "🎨 <b>买家专属（私聊菜单）：</b>\n"
        "• <b>改机器人名字</b> / <b>改机器人头像</b>（仅最高级买家）"
    )


def get_setting(group_id, key):
    cols = [
        "group_id", "operators", "exchange_rate", "fee_rate", "is_active",
        "language", "timezone", "show_usdt", "expire_time",
    ]
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
        row = c.fetchone()
        if not row:
            _, _, init_time = get_current_time()
            c.execute(
                "INSERT OR IGNORE INTO settings "
                "(group_id, operators, exchange_rate, fee_rate, is_active, language, timezone, show_usdt, expire_time) "
                "VALUES (?, '[]', 7.2, 0, 1, 'zh', 'Asia/Shanghai', 1, ?)",
                (group_id, init_time),
            )
            conn.commit()
            c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
            row = c.fetchone()
        conn.close()
        return dict(zip(cols, row)).get(key)
    except Exception:
        return None


def update_setting(group_id, key, value):
    if key not in SETTING_KEYS:
        return
    try:
        # 群组首次操作时 settings 表可能还没有记录，UPDATE 会静默失败
        get_setting(group_id, "group_id")
        conn = get_db()
        c = conn.cursor()
        c.execute(f"UPDATE settings SET {key} = ? WHERE group_id = ?", (value, group_id))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.exception("update_setting: %s", exc)


def normalize_billing_text(text):
    """统一记账指令里的符号，兼容全角 + - 和 caption 文本。"""
    text = (text or "").strip()
    for src, dst in (("＋", "+"), ("－", "-"), ("—", "-"), ("–", "-")):
        text = text.replace(src, dst)
    return text


def looks_like_billing_command(text, group_id):
    text = normalize_billing_text(text)
    if text == cmd(group_id, "bill_zero"):
        return True
    if match_exact(text, group_id, "class_start") or match_exact(text, group_id, "class_end"):
        return True
    if re.match(r"^(.*?)([\+\-])(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$", text):
        return True
    if expense_match(text, group_id):
        return True
    return False


def get_message_text(message):
    return normalize_billing_text(message.text or message.caption)


SUPPORTED_LANGS = ("zh", "eng", "my")

TEXTS = {
    "zh": {
        "lang_picker": "🌐 请选择本群语言：",
        "lang_changed": "✅ 语言已切换为：<b>{label}</b>",
        "welcome_thanks": "感谢您把我拉进贵群！",
        "welcome_i_am": "我是{name}🤖",
        "welcome_start": "请发送 <code>上课</code> 唤醒我，并设置费率（如 <code>设置费率 5</code>），然后即可开始记账。",
        "bill_summary": "📊 <b>账单汇总 ({date})</b>",
        "income_header": "📥 <b>入款（{n}笔）</b>",
        "no_income": "暂无入款",
        "category_header": "📥 <b>入款备注分类</b>",
        "no_remark": "无备注",
        "no_category": "暂无分类",
        "expense_header": "📤 <b>下发（{n}笔）</b>",
        "no_expense": "暂无下发",
        "expense_word": "下发",
        "total_income": "💰 <b>总入款:</b> {amount}",
        "fee_rate_label": "📉 <b>费率:</b> {rate}%",
        "exchange_rate_label": "💱 <b>汇率:</b> {rate}",
        "should_issue": "应下发: {amount} U",
        "issued": "已下发: {amount} U",
        "not_issued": "未下发: {amount} U",
        "audit_id": "[核算编号: {code}]",
        "show_more": "show more",
        "web_bill": "📊 查看完整网页账单",
        "class_start": "🟢 记账通道已开启！",
        "class_end": "🔴 下课成功，今日账单已封存。",
        "need_class_start": "⚠️ 请先发送「上课」开启记账。",
        "no_operate_perm": "⚠️ 您不是本群操作人，无权记账。请联系买家设置操作人。",
        "no_manage_operators": "⚠️ 只有买家或二级权限人才能执行此操作。",
        "no_delete_perm": "⚠️ 无权删账。",
        "rate_updated": "✅ 汇率已调整为 <b>{rate}</b>",
        "fee_updated": "✅ 费率已更新为 {rate}%",
        "operators_added": "✅ 已设为本群操作人：{names}",
        "operators_exist": "ℹ️ {names} 已在操作人列表中。",
        "operator_removed": "🗑️ 已移除操作人 <b>{name}</b>。",
        "operator_not_found": "ℹ️ <b>{name}</b> 不是本群操作人。",
        "delete_last_ok": "🗑️ 已撤销：【{remark}: {amount}】",
        "no_bills": "📭 暂无账单。",
        "delete_today_ok": "🗑️ 已清空今日 ({date}) 账单。",
        "delete_all_ok": "🗑️ 已清空本群全部历史账单。",
        "delete_remark_ok": "🗑️ 已删除今日备注【{remark}】共 {n} 笔进单。",
        "delete_remark_none": "🔍 今日无备注【{remark}】的进单。",
        "view_remark_none": "🔍 今日无备注【{remark}】的进单。",
        "view_remark_title": "📋 <b>{remark}进单明细</b>",
        "view_remark_total": "合计 {rmb} RMB / {usdt} USDT",
        "bill_fail": "❌ 记账失败: {err}",
        "lang_label": "中文",
    },
    "eng": {
        "lang_picker": "🌐 Choose group language:",
        "lang_changed": "✅ Language updated: <b>{label}</b>",
        "welcome_thanks": "Thank you for adding me to your group!",
        "welcome_i_am": "I am {name} 🤖",
        "welcome_start": "Send <code>上课</code> to start, set fee rate (e.g. <code>设置费率 5</code>), then begin bookkeeping.",
        "bill_summary": "📊 <b>Summary ({date})</b>",
        "income_header": "📥 <b>Deposits ({n})</b>",
        "no_income": "No deposits yet",
        "category_header": "📥 <b>Remark categories</b>",
        "no_remark": "No remark",
        "no_category": "No categories",
        "expense_header": "📤 <b>Payouts ({n})</b>",
        "no_expense": "No payouts yet",
        "expense_word": "Payout ",
        "total_income": "💰 <b>Total deposit:</b> {amount}",
        "fee_rate_label": "📉 <b>Fee:</b> {rate}%",
        "exchange_rate_label": "💱 <b>Rate:</b> {rate}",
        "should_issue": "To issue: {amount} U",
        "issued": "Issued: {amount} U",
        "not_issued": "Remaining: {amount} U",
        "audit_id": "[Ref: {code}]",
        "show_more": "show more",
        "web_bill": "📊 Full web report",
        "class_start": "🟢 Bookkeeping enabled!",
        "class_end": "🔴 Class ended. Today's bills archived.",
        "need_class_start": "⚠️ Send 上课 first to enable bookkeeping.",
        "no_operate_perm": "⚠️ You are not an operator in this group.",
        "no_manage_operators": "⚠️ Only buyer or VIP2 can do this.",
        "no_delete_perm": "⚠️ No permission to delete bills.",
        "rate_updated": "✅ Exchange rate set to <b>{rate}</b>",
        "fee_updated": "✅ Fee rate updated to {rate}%",
        "operators_added": "✅ Operators added: {names}",
        "operators_exist": "ℹ️ {names} already in operator list.",
        "operator_removed": "🗑️ Removed operator <b>{name}</b>.",
        "operator_not_found": "ℹ️ <b>{name}</b> is not an operator.",
        "delete_last_ok": "🗑️ Reversed: [{remark}: {amount}]",
        "no_bills": "📭 No bills yet.",
        "delete_today_ok": "🗑️ Cleared today's ({date}) bills.",
        "delete_all_ok": "🗑️ Cleared all group history.",
        "delete_remark_ok": "🗑️ Deleted {n} deposit(s) for remark [{remark}] today.",
        "delete_remark_none": "🔍 No deposits for remark [{remark}] today.",
        "view_remark_none": "🔍 No deposits for remark [{remark}] today.",
        "view_remark_title": "📋 <b>{remark} details</b>",
        "view_remark_total": "Total {rmb} RMB / {usdt} USDT",
        "bill_fail": "❌ Failed: {err}",
        "lang_label": "English",
    },
    "my": {
        "lang_picker": "🌐 ဤအုပ်စုအတွက် ဘာသာစကားရွေးပါ：",
        "lang_changed": "✅ ဘာသာစကားပြောင်းပြီး：<b>{label}</b>",
        "welcome_thanks": "ကျွန်ုပ်ကို အုပ်စုသို့ ဖိတ်ခေါ်ပေးသည့်အတွက် ကျေးဇူးတင်ပါသည်！",
        "welcome_i_am": "ကျွန်ုပ်သည် {name} 🤖",
        "welcome_start": "<code>上课</code> ပို့၍ စတင်ပါ၊ ယာဉ်ကျေးနှုန်း သတ်မှတ်ပါ (ဥ：<code>设置费率 5</code>)၊ ထို့နောက် မှတ်တမ်းတင်နိုင်ပါသည်。",
        "bill_summary": "📊 <b>ဘီလ်စာရင်း ({date})</b>",
        "income_header": "📥 <b>ဝင်ငွေ ({n} ခု)</b>",
        "no_income": "ဝင်ငွေမရှိ",
        "category_header": "📥 <b>မှတ်ချက်အမျိုးအစား</b>",
        "no_remark": "မှတ်ချက်မရှိ",
        "no_category": "အမျိုးအစားမရှိ",
        "expense_header": "📤 <b>ထုတ်ပေးမှု ({n} ခု)</b>",
        "no_expense": "ထုတ်ပေးမှုမရှိ",
        "expense_word": "ထုတ်",
        "total_income": "💰 <b>စုဝင်ငွေ:</b> {amount}",
        "fee_rate_label": "📉 <b>အခကြေးငွေ:</b> {rate}%",
        "exchange_rate_label": "💱 <b>နှုန်းထား:</b> {rate}",
        "should_issue": "ထုတ်ပေးရန်: {amount} U",
        "issued": "ထုတ်ပေးပြီး: {amount} U",
        "not_issued": "မထုတ်ရသေး: {amount} U",
        "audit_id": "[စာရင်းနံပါတ်: {code}]",
        "show_more": "show more",
        "web_bill": "📊 ဝဘ်ဘီလ်ကြည့်ရန်",
        "class_start": "🟢 မှတ်တမ်းတင်ခြင်း ဖွင့်ပြီး！",
        "class_end": "🔴 သင်ခန်းစာပိတ်ပြီး၊ ယနေ့ဘီလ်သိမ်းဆည်းပြီး。",
        "need_class_start": "⚠️ ဦးစွာ <code>上课</code> ပို့ပါ。",
        "no_operate_perm": "⚠️ ဤအုပ်စု operator မဟုတ်ပါ。",
        "no_manage_operators": "⚠️ buyer သို့မဟုတ် VIP2 သာ လုပ်နိုင်သည်。",
        "no_delete_perm": "⚠️ ဘီလ်ဖျက်ခွင့်မရှိ。",
        "rate_updated": "✅ နှုန်းထား <b>{rate}</b> သတ်မှတ်ပြီး",
        "fee_updated": "✅ အခကြေးငွေ {rate}% သတ်မှတ်ပြီး",
        "operators_added": "✅ operator ထည့်ပြီး：{names}",
        "operators_exist": "ℹ️ {names} ရှိပြီးသား",
        "operator_removed": "🗑️ operator <b>{name}</b> ဖယ်ပြီး",
        "operator_not_found": "ℹ️ <b>{name}</b> operator မဟုတ်",
        "delete_last_ok": "🗑️ ပယ်ဖျက်ပြီး：【{remark}: {amount}】",
        "no_bills": "📭 ဘီလ်မရှိ",
        "delete_today_ok": "🗑️ ယနေ့ ({date}) ဘီလ်ဖျက်ပြီး",
        "delete_all_ok": "🗑️ အုပ်စုဘီလ်အားလုံးဖျက်ပြီး",
        "delete_remark_ok": "🗑️ ယနေ့ [{remark}] ဝင်ငွေ {n} ခုဖျက်ပြီး",
        "delete_remark_none": "🔍 ယနေ့ [{remark}] ဝင်ငွေမရှိ",
        "view_remark_none": "🔍 ယနေ့ [{remark}] ဝင်ငွေမရှိ",
        "view_remark_title": "📋 <b>{remark} အသေးစိတ်</b>",
        "view_remark_total": "စုစုပေါင်း {rmb} RMB / {usdt} USDT",
        "bill_fail": "❌ မအောင်မြင်: {err}",
        "lang_label": "Myanmar",
    },
}

LANG_BUTTONS = (
    ("中文", "zh"),
    ("English", "eng"),
    ("Myanmar", "my"),
)

CMD = {
    "zh": {
        "class_start": "上课",
        "class_end": "下课",
        "set_rate": "设置汇率",
        "set_fee": "设置费率",
        "set_operator": "设置操作人",
        "remove_operator": "取掉操作人",
        "remove_operator2": "取消操作人",
        "delete_last": "删最后",
        "delete_today": "删今天",
        "delete_all": "删全部",
        "delete_remark": "删",
        "view_remark": "查看",
        "view_chain": "查看",
        "bill_zero": "+0",
        "lang_change": "改语言",
        "expense": "下发",
    },
    "eng": {
        "class_start": "start",
        "class_end": "stop",
        "set_rate": "setrate",
        "set_fee": "setfee",
        "set_operator": "setop",
        "remove_operator": "delop",
        "remove_operator2": "removeop",
        "delete_last": "dellast",
        "delete_today": "deltoday",
        "delete_all": "delall",
        "delete_remark": "del",
        "view_remark": "view",
        "view_chain": "check",
        "bill_zero": "+0",
        "lang_change": "change",
        "expense": "payout",
    },
    "my": {
        "class_start": "စတင်",
        "class_end": "ပိတ်",
        "set_rate": "နှုန်းသတ်",
        "set_fee": "အခကြေးသတ်",
        "set_operator": "operator ထည့်",
        "remove_operator": "operator ဖယ်",
        "remove_operator2": "operator ဖယ်ရှား",
        "delete_last": "နောက်ဆုံးဖျက်",
        "delete_today": "ယနေ့ဖျက်",
        "delete_all": "အားလုံးဖျက်",
        "delete_remark": "ဖျက်",
        "view_remark": "ကြည့်",
        "view_chain": "စစ်ပါ",
        "bill_zero": "+0",
        "lang_change": "စာသားပြောင်း",
        "expense": "ထုတ်",
    },
}


def cmd_lang(lang, key):
    return CMD[normalize_lang_code(lang)][key]


def cmd(group_id, key):
    return cmd_lang(get_group_lang(group_id), key)


def strip_cmd_prefix(text, prefix):
    raw = (text or "").strip()
    if raw == prefix:
        return ""
    if raw.startswith(prefix + " "):
        return raw[len(prefix) + 1:].strip()
    if raw.startswith(prefix):
        return raw[len(prefix):].strip()
    return None


def match_exact(text, group_id, key):
    return (text or "").strip() == cmd(group_id, key)


def build_welcome_start(group_id):
    c = CMD[get_group_lang(group_id)]
    lang = get_group_lang(group_id)
    if lang == "eng":
        return (
            f"Send <code>{c['class_start']}</code> to enable bookkeeping, "
            f"set fee (e.g. <code>{c['set_fee']} 5</code>), then you can start."
        )
    if lang == "my":
        return (
            f"<code>{c['class_start']}</code> ပို့၍ စတင်ပါ၊ "
            f"<code>{c['set_fee']} 5</code> ဖြင့် အခကြေးငွေသတ်မှတ်ပါ。"
        )
    return (
        f"请发送 <code>{c['class_start']}</code> 唤醒我，"
        f"并设置费率（如 <code>{c['set_fee']} 5</code>），然后即可开始记账。"
    )


def build_need_class_start(group_id):
    c = CMD[get_group_lang(group_id)]
    lang = get_group_lang(group_id)
    if lang == "eng":
        return f"⚠️ Send <code>{c['class_start']}</code> first to enable bookkeeping."
    if lang == "my":
        return f"⚠️ ဦးစွာ <code>{c['class_start']}</code> ပို့ပါ。"
    return f"⚠️ 请先发送 <code>{c['class_start']}</code> 开启记账。"


def expense_match(text, group_id):
    word = re.escape(cmd(group_id, "expense"))
    return re.match(rf"^(.*?)(?:{word})\s*(-?\d+(?:\.\d+)?)$", text or "")


def chain_lookup_target(text, group_id=None):
    if group_id and str(group_id).startswith("-"):
        prefixes = [cmd(group_id, "view_chain")]
    else:
        prefixes = [CMD[lang]["view_chain"] for lang in SUPPORTED_LANGS]
    for prefix in prefixes:
        target = strip_cmd_prefix(text, prefix)
        if target is not None and target.startswith("T") and len(target) == 34:
            return target
    return None


def send_manual_guide_picker(chat_id):
    markup = telebot.types.InlineKeyboardMarkup(row_width=3)
    markup.add(*[
        telebot.types.InlineKeyboardButton(label, callback_data=f"guide_lang_{code}")
        for label, code in LANG_BUTTONS
    ])
    bot.send_message(
        chat_id,
        "📖 请选择要查看的用法指南语言：\n"
        "Choose manual language:\n"
        "ဘာသာစကားရွေးပါ：",
        reply_markup=markup,
    )


def normalize_lang_code(raw):
    if not raw:
        return "zh"
    code = str(raw).strip().lower().lstrip("/")
    if code in SUPPORTED_LANGS:
        return code
    aliases = {
        "zh": "zh", "cn": "zh", "chinese": "zh", "中文": "zh",
        "eng": "eng", "en": "eng", "english": "eng",
        "my": "my", "mm": "my", "myanmar": "my", "burmese": "my",
    }
    return aliases.get(code, "zh")


def get_group_lang(group_id):
    stored = get_setting(group_id, "language") or "zh"
    return normalize_lang_code(stored)


def tr(group_id, key, **kwargs):
    lang = get_group_lang(group_id)
    template = TEXTS.get(lang, TEXTS["zh"]).get(key) or TEXTS["zh"].get(key, key)
    return template.format(**kwargs) if kwargs else template


def is_language_change_trigger(text, group_id):
    raw = (text or "").strip()
    if raw == cmd(group_id, "lang_change"):
        return True
    low = raw.lower()
    return low.startswith("/setlanguage") or low.startswith("setlanguage")


def parse_direct_language(text):
    m = re.match(r"^/?setlanguage[\s,]+([A-Za-z]+)", (text or "").strip(), re.I)
    if not m:
        return None
    code = normalize_lang_code(m.group(1).split(",")[0])
    return code if code in SUPPORTED_LANGS else None


def send_language_picker(chat_id, group_id):
    markup = telebot.types.InlineKeyboardMarkup(row_width=3)
    markup.add(*[
        telebot.types.InlineKeyboardButton(label, callback_data=f"setlang_{group_id}_{code}")
        for label, code in LANG_BUTTONS
    ])
    bot.send_message(chat_id, tr(group_id, "lang_picker"), reply_markup=markup)


def send_group_greeting(chat_id, group_id):
    bot.send_message(
        chat_id,
        f"<b>{tr(group_id, 'welcome_thanks')}</b>\n\n"
        f"{tr(group_id, 'welcome_i_am', name=get_bot_join_name())}\n"
        f"{build_welcome_start(group_id)}",
        parse_mode="HTML",
    )


def apply_group_language(chat_id, group_id, lang_code):
    lang_code = normalize_lang_code(lang_code)
    if lang_code not in SUPPORTED_LANGS:
        lang_code = "zh"
    update_setting(group_id, "language", lang_code)
    label = TEXTS[lang_code]["lang_label"]
    bot.send_message(chat_id, tr(group_id, "lang_changed", label=label), parse_mode="HTML")
    send_group_greeting(chat_id, group_id)


def normalize_operator_name(name):
    name = (name or "").strip()
    if not name:
        return ""
    return name if name.startswith("@") else f"@{name}"


def get_group_operators(group_id):
    try:
        return json.loads(get_setting(group_id, "operators") or "[]")
    except Exception:
        return []


def can_operate_in_group(group_id, user_id, tg_username=None):
    has_auth, _, _, _ = get_user_permission_level(user_id)
    if has_auth:
        return True
    ops = get_group_operators(group_id)
    if user_id in ops:
        return True
    if tg_username:
        bare = tg_username.lower()
        for op in ops:
            op_str = str(op).lower().lstrip("@")
            if op_str == bare:
                return True
    return False


def can_manage_group_operators(user_id):
    if user_id in FOUNDER_USERS:
        return True
    has_auth, _, _, lvl = get_user_permission_level(user_id)
    return has_auth and lvl in (1, 2)


def can_customize_bot(user_id):
    """最高级买家 / 创始人可修改本机器人对外名字与头像。"""
    if user_id in FOUNDER_USERS:
        return True
    has_auth, _, _, lvl = get_user_permission_level(user_id)
    return has_auth and lvl == 1


def apply_bot_display_name(name):
    clean = (name or "").strip()[:64]
    if not clean:
        raise ValueError("名字不能为空")
    ok = bot.set_my_name(name=clean)
    if ok is False:
        raise RuntimeError("Telegram 拒绝修改名字")
    global _CACHED_BOT_NAME
    _CACHED_BOT_NAME = clean
    return clean


def prepare_avatar_image(raw_bytes, size=640):
    """把任意图片自动裁成正方形并缩放到头像尺寸。"""
    from PIL import Image

    with Image.open(io.BytesIO(raw_bytes)) as img:
        img = img.convert("RGBA")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        img = img.resize((size, size), Image.Resampling.LANCZOS)

        canvas = Image.new("RGB", (size, size), (255, 255, 255))
        canvas.paste(img, mask=img.split()[3])
        out = io.BytesIO()
        canvas.save(out, format="JPEG", quality=92, optimize=True)
        out.seek(0)
        return out


def apply_bot_profile_photo(file_id):
    file_info = bot.get_file(file_id)
    data = bot.download_file(file_info.file_path)
    raw = data if isinstance(data, bytes) else data.read()
    stream = prepare_avatar_image(raw)
    stream.name = "avatar.jpg"
    profile_photo = telebot.types.InputProfilePhotoStatic(
        telebot.types.InputFile(stream, file_name="avatar.jpg")
    )
    ok = bot.set_my_profile_photo(photo=profile_photo)
    if ok is False:
        raise RuntimeError("Telegram 拒绝修改头像")
    return True


def extract_mention(text, entities):
    mentions = extract_all_mentions(text, entities)
    return mentions[0] if mentions else ""


def extract_all_mentions(text, entities):
    if not entities:
        return []
    mentions = []
    for entity in entities:
        if entity.type == "mention":
            mentions.append(text[entity.offset: entity.offset + entity.length].strip())
    return mentions


def parse_operator_targets(text, entities, command_prefix):
    """从一条消息里解析多个 @操作人。"""
    targets = []
    seen = set()
    for raw in extract_all_mentions(text, entities):
        name = normalize_operator_name(raw)
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            targets.append(name)
    remainder = text.replace(command_prefix, "", 1).strip() if command_prefix else text.strip()
    for match in re.finditer(r"@([A-Za-z0-9_]{3,32})", remainder):
        name = normalize_operator_name(f"@{match.group(1)}")
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            targets.append(name)
    return targets


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
def add_bill(group_id, user_id, username, remark, amount, bill_type, exchange_rate=None):
    if exchange_rate is None:
        exchange_rate = get_setting(group_id, "exchange_rate") or 7.2
    usdt_amount = amount / exchange_rate if bill_type == "income" else amount
    tz = get_setting(group_id, "timezone") or "Asia/Shanghai"
    _, _, full_time = get_current_time(tz)
    date_str = full_time[:10]
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO bills
        (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate,
         bill_type, timestamp, date_str, is_settled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate, bill_type, full_time, date_str),
    )
    conn.commit()
    conn.close()
    return usdt_amount


def get_class_bills_by_date(group_id, target_date):
    conn = get_db()
    c = conn.cursor()
    if target_date == "all":
        c.execute(
            "SELECT remark, username, amount, usdt_amount, exchange_rate, timestamp, date_str, user_id "
            "FROM bills WHERE group_id = ? AND bill_type = 'income' ORDER BY id ASC",
            (group_id,),
        )
        income = c.fetchall()
        c.execute(
            "SELECT remark, username, usdt_amount, exchange_rate, timestamp, date_str, user_id "
            "FROM bills WHERE group_id = ? AND bill_type = 'expense' ORDER BY id ASC",
            (group_id,),
        )
        expense = c.fetchall()
        c.execute(
            "SELECT SUM(amount), SUM(usdt_amount) FROM bills "
            "WHERE group_id = ? AND bill_type = 'income'",
            (group_id,),
        )
        total_income = c.fetchone()
        c.execute(
            "SELECT SUM(usdt_amount) FROM bills "
            "WHERE group_id = ? AND bill_type = 'expense'",
            (group_id,),
        )
        total_expense = c.fetchone()
    else:
        c.execute(
            "SELECT remark, username, amount, usdt_amount, exchange_rate, timestamp, date_str, user_id "
            "FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'income' ORDER BY id ASC",
            (group_id, target_date),
        )
        income = c.fetchall()
        c.execute(
            "SELECT remark, username, usdt_amount, exchange_rate, timestamp, date_str, user_id "
            "FROM bills WHERE group_id = ? AND date_str = ? AND bill_type = 'expense' ORDER BY id ASC",
            (group_id, target_date),
        )
        expense = c.fetchall()
        c.execute(
            "SELECT SUM(amount), SUM(usdt_amount) FROM bills "
            "WHERE group_id = ? AND date_str = ? AND bill_type = 'income'",
            (group_id, target_date),
        )
        total_income = c.fetchone()
        c.execute(
            "SELECT SUM(usdt_amount) FROM bills "
            "WHERE group_id = ? AND date_str = ? AND bill_type = 'expense'",
            (group_id, target_date),
        )
        total_expense = c.fetchone()
    conn.close()
    return income, expense, total_income, total_expense


def get_bill_dates(group_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT date_str, "
        "SUM(CASE WHEN bill_type='income' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN bill_type='expense' THEN 1 ELSE 0 END) "
        "FROM bills WHERE group_id = ? GROUP BY date_str ORDER BY date_str DESC",
        (group_id,),
    )
    rows = c.fetchall()
    conn.close()
    return [{"date": r[0], "income": r[1], "expense": r[2]} for r in rows]


def _html_esc(text):
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tag_remark(remark):
    """Telegram 仅支持有限 HTML 标签，不能用 span/style，否则发送失败。"""
    rem = _html_esc(remark).strip()
    if not rem:
        return ""
    return f"{rem} "


def _tag_operator(name, user_id=None):
    safe = _html_esc(name)
    if user_id:
        try:
            uid = int(user_id)
            if uid > 0:
                return f'<a href="tg://user?id={uid}">{safe}</a>'
        except (TypeError, ValueError):
            pass
    return safe


def _tag_rmb(amount):
    return f"<b>{amount:.0f}</b>"


def _format_income_line(remark, operator, amount, usdt, rate, timestamp, user_id=None):
    time_s = timestamp[11:16]
    core = f"{time_s} {amount:.0f}/{rate:.2f}={usdt:.2f}U"
    op = _tag_operator(operator, user_id)
    rem = _tag_remark(remark)
    if rem:
        return f"{rem}{core} {op}"
    return f"{core} {op}"


def _format_expense_line(remark, operator, usdt, timestamp, user_id=None, group_id=None):
    time_s = timestamp[11:16]
    word = tr(group_id, "expense_word") if group_id else "下发"
    core = f"{time_s} {word}{usdt:.2f}U"
    op = _tag_operator(operator, user_id)
    rem = _tag_remark(remark)
    if rem:
        return f"{rem}{core} {op}"
    return f"{core} {op}"


def build_bill_report_text(group_id, target_date, show_all_categories=False):
    rate = float(get_setting(group_id, "exchange_rate") or 7.2)
    fee_rate = float(get_setting(group_id, "fee_rate") or 0.0)
    income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)

    total_rmb = float((total_income[0] or 0) if total_income else 0)
    total_usdt = float((total_income[1] or 0) if total_income else 0)
    expense_usdt = float((total_expense[0] or 0) if total_expense else 0)
    remaining_usdt = total_usdt - expense_usdt

    summary = {}
    empty_remark = tr(group_id, "no_remark")
    for row in income:
        rem = (row[0] or "").strip() or empty_remark
        summary.setdefault(rem, {"rmb": 0.0, "usdt": 0.0})
        summary[rem]["rmb"] += row[2]
        summary[rem]["usdt"] += row[3]

    lines = [tr(group_id, "bill_summary", date=target_date)]
    lines.append(tr(group_id, "income_header", n=len(income)))
    if income:
        for row in income[-5:]:
            uid = row[7] if len(row) > 7 else None
            lines.append(_format_income_line(row[0], row[1], row[2], row[3], row[4], row[5], uid))
    else:
        lines.append(tr(group_id, "no_income"))

    lines.append("")
    lines.append(tr(group_id, "category_header"))
    category_items = list(summary.items())
    visible_categories = category_items if show_all_categories else category_items[:3]
    if visible_categories:
        cate_lines = []
        for key, val in visible_categories:
            if key != empty_remark:
                key_label = _tag_remark(key).strip()
            else:
                key_label = empty_remark
            cate_lines.append(f"{key_label} 👉 {_tag_rmb(val['rmb'])}/{val['usdt']:.2f}U")
        lines.append(f"<blockquote>{chr(10).join(cate_lines)}</blockquote>")
    else:
        lines.append(f"<blockquote>{tr(group_id, 'no_category')}</blockquote>")

    lines.append("")
    lines.append(tr(group_id, "expense_header", n=len(expense)))
    if expense:
        for row in expense[-5:]:
            uid = row[6] if len(row) > 6 else None
            lines.append(_format_expense_line(row[0], row[1], row[2], row[4], uid, group_id))
    else:
        lines.append(tr(group_id, "no_expense"))

    lines.extend([
        "",
        tr(group_id, "total_income", amount=_tag_rmb(total_rmb)),
        tr(group_id, "fee_rate_label", rate=f"{fee_rate * 100:.0f}"),
        tr(group_id, "exchange_rate_label", rate=f"{rate:.2f}"),
        "",
        tr(group_id, "should_issue", amount=f"{total_usdt:.2f}"),
        tr(group_id, "issued", amount=f"{expense_usdt:.2f}"),
        tr(group_id, "not_issued", amount=f"{remaining_usdt:.2f}"),
        "",
        f"<code>{tr(group_id, 'audit_id', code=random.randint(1000, 9999))}</code>",
    ])

    has_more_categories = len(category_items) > 3 and not show_all_categories
    return "\n".join(lines), has_more_categories


def send_text_bill_report(chat_id, group_id, target_date):
    report, has_more = build_bill_report_text(group_id, target_date)
    markup = telebot.types.InlineKeyboardMarkup()
    if has_more:
        date_key = target_date.replace("-", "")
        markup.add(telebot.types.InlineKeyboardButton(
            tr(group_id, "show_more"),
            callback_data=f"bill_cate_{group_id}_{date_key}",
        ))
    markup.add(telebot.types.InlineKeyboardButton(
        tr(group_id, "web_bill"), url=f"{WEBHOOK_URL}/?group_id={group_id}"
    ))
    try:
        bot.send_message(chat_id, report, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:
        log.exception("账单 HTML 发送失败，改用纯文本: %s", exc)
        plain = re.sub(r"<[^>]+>", "", report)
        try:
            bot.send_message(chat_id, plain, reply_markup=markup)
        except Exception as exc2:
            log.exception("纯文本账单发送失败: %s", exc2)
            raise exc2 from exc


# ---------------------------------------------------------------------------
# Private chat menu
# ---------------------------------------------------------------------------
PRIVATE_MENU_TEXT = {
    "📅 查看到期时间": "btn_check_expire",
    "📖 详细说明书": "btn_manual_guide",
    "💰 自助续费说明": "btn_pay_usdt",
    "✏️ 改机器人名字": "btn_set_bot_name",
    "🖼 改机器人头像": "btn_set_bot_photo",
    "🔑 设置权限人": "btn_grant_vip2",
    "❌ 取掉权限人": "btn_revoke_vip2",
}


def build_private_reply_keyboard(uid):
    has_auth, _, _, lvl = get_user_permission_level(uid)
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📅 查看到期时间", "📖 详细说明书")
    kb.add("💰 自助续费说明")
    if uid in FOUNDER_USERS or (has_auth and lvl == 1):
        kb.add("✏️ 改机器人名字", "🖼 改机器人头像")
        kb.add("🔑 设置权限人", "❌ 取掉权限人")
    kb.add("🏠 主菜单")
    return kb


def build_private_inline_markup(uid):
    has_auth, _, _, lvl = get_user_permission_level(uid)
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("📅 查看到期时间", callback_data="btn_check_expire"),
        telebot.types.InlineKeyboardButton("📖 详细说明书", callback_data="btn_manual_guide"),
    )
    markup.add(telebot.types.InlineKeyboardButton("💰 自助续费说明", callback_data="btn_pay_usdt"))
    if uid in FOUNDER_USERS or (has_auth and lvl == 1):
        markup.add(
            telebot.types.InlineKeyboardButton("🔑 设置权限人", callback_data="btn_grant_vip2"),
            telebot.types.InlineKeyboardButton("❌ 取掉权限人", callback_data="btn_revoke_vip2"),
        )
        markup.add(
            telebot.types.InlineKeyboardButton("✏️ 改机器人名字", callback_data="btn_set_bot_name"),
            telebot.types.InlineKeyboardButton("🖼 改机器人头像", callback_data="btn_set_bot_photo"),
        )
    return markup


def send_private_welcome(chat_id, uid):
    _, lvl_desc, _, _ = get_user_permission_level(uid)
    bot.send_message(
        chat_id,
        f"🤖 <b>您好！欢迎使用{get_bot_brand()}分布式管理中心</b>\n\n"
        f"👤 <b>当前身份：</b> <code>{lvl_desc}</code>\n"
        f"📌 请用<b>输入框下方常驻菜单</b>，或消息里的按钮操作：",
        parse_mode="HTML",
        reply_markup=build_private_reply_keyboard(uid),
    )
    bot.send_message(
        chat_id,
        "👇 也可点这里快捷操作：",
        reply_markup=build_private_inline_markup(uid),
    )


def process_private_menu(uid, chat_id, action):
    """处理私聊菜单动作。返回 alert 文案表示权限不足等提示。"""
    has_auth, lvl_desc, expire_time, lvl = get_user_permission_level(uid)

    if action == "btn_check_expire":
        status = "🟢 正常生效中" if has_auth else "🔴 资质已过期/未激活"
        bot.send_message(
            chat_id,
            f"👤 <b>您的身份体系：</b>\n"
            f"• 级别：<code>{lvl_desc}</code>\n"
            f"• 状态：{status}\n"
            f"• 有效截止期：<code>{expire_time}</code>",
            parse_mode="HTML",
        )
        return None

    if action == "btn_manual_guide":
        send_manual_guide_picker(chat_id)
        return None

    if action == "btn_set_bot_name":
        if not can_customize_bot(uid):
            return "仅最高级买家可修改机器人名字。"
        USER_STATE[uid] = "WAITING_BOT_NAME"
        bot.send_message(
            chat_id,
            "✏️ 请直接发送新的<b>机器人显示名字</b>（最多 64 字）：\n"
            "例如：<code>小财家记账</code>",
            parse_mode="HTML",
        )
        return None

    if action == "btn_set_bot_photo":
        if not can_customize_bot(uid):
            return "仅最高级买家可修改机器人头像。"
        USER_STATE[uid] = "WAITING_BOT_PHOTO"
        bot.send_message(
            chat_id,
            "🖼 请直接发一张图片给我（截图、logo、照片都可以）。\n\n"
            "我会<b>自动裁成正方形</b>并优化成头像尺寸，再帮你换上。",
            parse_mode="HTML",
        )
        return None

    if action == "btn_pay_usdt":
        if not can_submit_purchase(uid):
            bot.send_message(chat_id, build_bot_sold_message(), parse_mode="HTML")
            return None
        bot.send_message(
            chat_id,
            f"💰 <b>USDT 授权价格套餐：</b>\n"
            f"• 1 个月高级买家：<b>{PRICE_1_MONTH}</b> USDT\n"
            f"• 2 个月高级买家：<b>{PRICE_2_MONTH}</b> USDT\n"
            f"• 3 个月高级买家：<b>{PRICE_3_MONTH}</b> USDT\n\n"
            f"💎 <b>官方波场(TRC20)收款地址：</b>\n<code>{TRON_ADDRESS}</code>\n\n"
            f"⚠️ 转账成功后，请将【成功截图凭证】私发给机器人，创始人审核后开通。",
            parse_mode="HTML",
        )
        return None

    if action == "btn_grant_vip2":
        if uid not in FOUNDER_USERS and lvl != 1:
            return "只有最高级买家才能指派二级权限人。"
        if get_level2_vip_count() >= MAX_LEVEL2_VIPS:
            bot.send_message(
                chat_id,
                f"❌ 当前已满 <b>{MAX_LEVEL2_VIPS}</b> 个二级权限人，请先移除旧成员。",
                parse_mode="HTML",
            )
        else:
            USER_STATE[uid] = "WAITING_ADD_VIP2"
            bot.send_message(
                chat_id,
                "➡️ 请直接输入要授权的二级权限人 <b>UID（纯数字）</b>：",
                parse_mode="HTML",
            )
        return None

    if action == "btn_revoke_vip2":
        if uid not in FOUNDER_USERS and lvl != 1:
            return "只有最高级买家才能撤销二级权限人。"
        vip_list = get_all_level2_vips()
        if not vip_list:
            bot.send_message(chat_id, "📭 您还没有设置任何二级权限人。", parse_mode="HTML")
        else:
            lines = [
                f"👤 <b>{name}</b> | UID: <code>{vid}</code>"
                for vid, name in vip_list
            ]
            USER_STATE[uid] = "WAITING_DEL_VIP2"
            bot.send_message(
                chat_id,
                f"📋 <b>二级权限人 ({len(vip_list)}/{MAX_LEVEL2_VIPS})</b>\n\n"
                + "\n".join(lines)
                + "\n\n➡️ 请发送要移除的 UID（纯数字）：",
                parse_mode="HTML",
            )
        return None

    return None


# ---------------------------------------------------------------------------
# Telegram handlers — /start
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    uid = message.from_user.id
    if message.chat.type == "private":
        send_private_welcome(message.chat.id, uid)
    else:
        bot.send_message(
            message.chat.id,
            f"🤖 <b>{get_bot_brand()}智能分布式记账系统已激活</b>\n\n"
            "👉 <b>群内核心记账命令：</b>\n"
            "• 发送 <code>上课</code> / <code>下课</code> 开启或封存账单\n"
            "• 发送 <code>+1000</code> 或 <code>+1000/7.3</code> 记入款\n"
            "• 发送 <code>项目公款+5000</code> 记带备注账目\n"
            "• 发送 <code>下发500</code> 记下发\n"
            "• 发送 <code>+0</code> 查看对账大底\n\n"
            "⚙️ <b>财务群管命令（买家老板/权限人）：</b>\n"
            "• <code>设置汇率 7.35</code>\n"
            "• <code>设置费率 5</code>\n"
            "• <code>设置操作人 @用户名 @用户名2</code> — 可一次多个\n"
            "• <code>取掉操作人 @用户名</code>",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Telegram handlers — private menu callbacks
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("btn_"))
def handle_private_buttons(call):
    alert = process_private_menu(call.from_user.id, call.message.chat.id, call.data)
    if alert:
        bot.answer_callback_query(call.id, alert, show_alert=True)
    else:
        bot.answer_callback_query(call.id)


@bot.my_chat_member_handler()
def handle_my_chat_member(update: telebot.types.ChatMemberUpdated):
    if update.new_chat_member.status in ("member", "administrator"):
        try:
            send_group_greeting(update.chat.id, update.chat.id)
        except Exception as exc:
            log.error("入群欢迎语失败: %s", exc)


@bot.callback_query_handler(func=lambda call: call.data.startswith("setlang_"))
def handle_set_language(call):
    parts = call.data.split("_")
    if len(parts) != 3:
        bot.answer_callback_query(call.id)
        return
    try:
        group_id = int(parts[1])
        lang_code = normalize_lang_code(parts[2])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Invalid language", show_alert=True)
        return
    if call.message.chat.id != group_id:
        bot.answer_callback_query(call.id)
        return
    if not can_manage_group_operators(call.from_user.id):
        bot.answer_callback_query(call.id, tr(group_id, "no_manage_operators"), show_alert=True)
        return
    if lang_code not in SUPPORTED_LANGS:
        bot.answer_callback_query(call.id)
        return
    update_setting(group_id, "language", lang_code)
    bot.answer_callback_query(call.id, TEXTS[lang_code]["lang_label"])
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    label = TEXTS[lang_code]["lang_label"]
    bot.send_message(group_id, tr(group_id, "lang_changed", label=label), parse_mode="HTML")
    send_group_greeting(group_id, group_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("guide_lang_"))
def handle_manual_guide_language(call):
    lang_code = normalize_lang_code(call.data[len("guide_lang_"):])
    if lang_code not in SUPPORTED_LANGS:
        bot.answer_callback_query(call.id)
        return
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(
        call.message.chat.id,
        build_manual_guide_text(lang_code),
        parse_mode="HTML",
    )
    bot.answer_callback_query(call.id, TEXTS[lang_code]["lang_label"])


@bot.message_handler(content_types=["photo"], func=lambda m: m.chat.type == "private")
def handle_receipt_photo(message):
    uid = message.from_user.id

    if USER_STATE.get(uid) == "WAITING_BOT_PHOTO":
        if not can_customize_bot(uid):
            USER_STATE.pop(uid, None)
            bot.reply_to(message, "⚠️ 您没有权限修改机器人头像。")
            return
        USER_STATE.pop(uid, None)
        photo_id = message.photo[-1].file_id
        try:
            apply_bot_profile_photo(photo_id)
            bot.reply_to(
                message,
                "✅ 头像已更新！\n"
                "（已自动裁剪为正方形并优化尺寸，请在聊天列表查看机器人资料）",
            )
        except Exception as exc:
            log.exception("set bot photo failed: %s", exc)
            bot.reply_to(message, f"❌ 头像更新失败：{exc}")
        return

    if not can_submit_purchase(uid):
        bot.reply_to(message, build_bot_sold_message(), parse_mode="HTML")
        return

    username = message.from_user.username or "无用户名"
    first_name = message.from_user.first_name or "买家"
    photo_id = message.photo[-1].file_id

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(f"✅ 开通1个月({PRICE_1_MONTH}U)", callback_data=f"auth_1_{uid}"),
        telebot.types.InlineKeyboardButton(f"✅ 开通2个月({PRICE_2_MONTH}U)", callback_data=f"auth_2_{uid}"),
    )
    markup.add(
        telebot.types.InlineKeyboardButton(f"✅ 开通3个月({PRICE_3_MONTH}U)", callback_data=f"auth_3_{uid}"),
        telebot.types.InlineKeyboardButton("❌ 拒绝开通", callback_data=f"auth_reject_{uid}"),
    )

    for founder in FOUNDER_USERS:
        try:
            bot.send_message(
                founder,
                f"🔔 <b>收到续费申请</b>\n\n"
                f"👤 {first_name} (@{username})\n🆔 UID: <code>{uid}</code>",
                parse_mode="HTML",
            )
            bot.send_photo(founder, photo_id, reply_markup=markup)
        except Exception:
            pass
    bot.reply_to(message, "⏳ 续费凭证已提交，请等待 1-3 分钟审核。")


@bot.callback_query_handler(func=lambda call: call.data.startswith("auth_"))
def handle_auth_buttons(call):
    if call.from_user.id not in FOUNDER_USERS:
        bot.answer_callback_query(call.id, "您不是系统创始人，无权审核！", show_alert=True)
        return

    parts = call.data.split("_")
    action = parts[1]

    if action == "reject":
        buyer_id = int(parts[2])
        try:
            bot.send_message(buyer_id, "❌ <b>续费申请未通过。</b>", parse_mode="HTML")
        except Exception:
            pass
        bot.edit_message_caption("❌ 已驳回该申请。", call.message.chat.id, call.message.message_id)
    else:
        months = int(action)
        buyer_id = int(parts[2])
        existing_buyer = get_active_vip1_buyer_id()
        if existing_buyer and existing_buyer != buyer_id:
            bot.answer_callback_query(
                call.id,
                "本机器人已有买家，无法再开通新的最高级买家。",
                show_alert=True,
            )
            return
        expire_str = add_vip_user(buyer_id, f"user_{buyer_id}", months, level=1)
        try:
            bot.send_message(
                buyer_id,
                f"🎉 <b>最高级买家已开通 {months} 个月！</b>\n到期：{expire_str}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        bot.edit_message_caption(
            f"✅ 审核成功，到期：{expire_str}",
            call.message.chat.id,
            call.message.message_id,
        )
    bot.answer_callback_query(call.id, "操作成功！")


@bot.callback_query_handler(func=lambda call: call.data.startswith("bill_cate_"))
def handle_bill_category_more(call):
    rest = call.data[len("bill_cate_"):]
    sep = rest.rfind("_")
    if sep < 0:
        bot.answer_callback_query(call.id)
        return
    try:
        group_id = int(rest[:sep])
        date_key = rest[sep + 1:]
        target_date = f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:8]}"
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "数据解析失败", show_alert=True)
        return

    report, _ = build_bill_report_text(group_id, target_date, show_all_categories=True)
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton(
        "📊 查看完整网页账单", url=f"{WEBHOOK_URL}/?group_id={group_id}"
    ))
    try:
        bot.edit_message_text(
            report,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except Exception as exc:
        log.exception("expand bill categories: %s", exc)
    bot.answer_callback_query(call.id)


# ---------------------------------------------------------------------------
# Telegram handlers — all text messages
# ---------------------------------------------------------------------------
@bot.message_handler(
    content_types=["text", "photo", "document"],
    func=lambda m: bool((m.text or m.caption or "").strip()),
)
def handle_all_messages(message):
    text = get_message_text(message)
    if not text:
        return
    gid = message.chat.id
    uid = message.from_user.id
    tg_username = message.from_user.username
    display_name = message.from_user.first_name or "用户"

    # --- private chat ---
    if message.chat.type == "private":
        if text == "🏠 主菜单":
            USER_STATE.pop(uid, None)
            send_private_welcome(gid, uid)
            return

        menu_action = PRIVATE_MENU_TEXT.get(text)
        if menu_action:
            USER_STATE.pop(uid, None)
            alert = process_private_menu(uid, gid, menu_action)
            if alert:
                bot.reply_to(message, f"⚠️ {alert}")
            return

        state = USER_STATE.pop(uid, None)
        if state == "WAITING_BOT_NAME":
            if not can_customize_bot(uid):
                bot.reply_to(message, "⚠️ 仅最高级买家可修改机器人名字。")
                return
            try:
                new_name = apply_bot_display_name(text)
                bot.reply_to(
                    message,
                    f"✅ 机器人名字已改为：<b>{_html_esc(new_name)}</b>\n"
                    f"（聊天列表里显示的名称，@用户名不变）",
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.exception("set bot name failed: %s", exc)
                bot.reply_to(message, f"❌ 改名失败：{exc}")
            return
        if state == "WAITING_BOT_PHOTO":
            USER_STATE[uid] = "WAITING_BOT_PHOTO"
            bot.reply_to(message, "⚠️ 请发送一张图片作为头像，不要发文字。")
            return
        if state in ("WAITING_ADD_VIP2", "WAITING_DEL_VIP2"):
            if not text.isdigit():
                bot.reply_to(message, "❌ UID 必须是纯数字，请重新点击菜单操作。", parse_mode="HTML")
                return
            target_uid = int(text)
            if state == "WAITING_ADD_VIP2":
                if get_level2_vip_count() >= MAX_LEVEL2_VIPS:
                    bot.reply_to(message, f"❌ 二级权限人已满 {MAX_LEVEL2_VIPS} 个。", parse_mode="HTML")
                    return
                expire_str = add_vip_user(target_uid, f"vip2_{target_uid}", months=12, level=2)
                bot.reply_to(
                    message,
                    f"✅ 已授权 UID <code>{target_uid}</code> 为二级权限人，到期：{expire_str}",
                    parse_mode="HTML",
                )
                try:
                    bot.send_message(target_uid, "🎉 您已被提升为二级权限人(VIP2)。", parse_mode="HTML")
                except Exception:
                    pass
            elif remove_vip_user(target_uid):
                bot.reply_to(message, f"🗑️ 已移除 UID <code>{target_uid}</code> 的二级权限。", parse_mode="HTML")
                try:
                    bot.send_message(target_uid, "⚠️ 您的二级权限人资格已被撤销。", parse_mode="HTML")
                except Exception:
                    pass
            else:
                bot.reply_to(message, "❌ 未找到该二级权限人，或移除失败。")
            return

    # --- chain lookup (any chat) ---
    addr = chain_lookup_target(text, gid if message.chat.type in ("group", "supergroup") else None)
    if addr:
        wait = bot.reply_to(message, "🔍 正在查询链上数据...")
        result = fetch_blockchain_usdt_info(addr)
        try:
            bot.delete_message(gid, wait.message_id)
        except Exception:
            pass
        if result["success"]:
            bot.reply_to(
                message,
                f"👤 地址：<code>{addr}</code>\n\n"
                f"💰 USDT 余额：<code>{result['balance']:.2f}</code> U\n"
                f"━━━━━━━━━━━━━━━━━━\n📊 流向明细：\n{result['history']}",
                parse_mode="HTML",
            )
        else:
            bot.reply_to(message, f"❌ 检索失败: {result['msg']}")
        return

    if message.chat.type not in ("group", "supergroup"):
        return

    # --- group commands ---
    now, _, _ = get_current_time()
    today = now.strftime("%Y-%m-%d")

    if is_language_change_trigger(text, gid):
        if not can_manage_group_operators(uid):
            bot.reply_to(message, tr(gid, "no_manage_operators"), parse_mode="HTML")
            return
        direct = parse_direct_language(text)
        if direct:
            apply_group_language(gid, gid, direct)
        else:
            send_language_picker(gid, gid)
        return

    rate_rest = strip_cmd_prefix(text, cmd(gid, "set_rate"))
    if rate_rest is not None:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return
        try:
            rate = float(rate_rest)
            update_setting(gid, "exchange_rate", rate)
            bot.reply_to(message, tr(gid, "rate_updated", rate=f"{rate:.2f}"), parse_mode="HTML")
        except ValueError:
            c = cmd(gid, "set_rate")
            bot.reply_to(message, f"❌ 格式错误，例如：{c} 7.3")
        return

    fee_rest = strip_cmd_prefix(text, cmd(gid, "set_fee"))
    if fee_rest is not None:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return
        try:
            fee = float(fee_rest) / 100
            update_setting(gid, "fee_rate", fee)
            bot.reply_to(message, tr(gid, "fee_updated", rate=f"{fee * 100:.0f}"), parse_mode="HTML")
        except ValueError:
            c = cmd(gid, "set_fee")
            bot.reply_to(message, f"❌ 格式错误，例如：{c} 5")
        return

    op_prefix = cmd(gid, "set_operator")
    if strip_cmd_prefix(text, op_prefix) is not None:
        if not can_manage_group_operators(uid):
            bot.reply_to(message, tr(gid, "no_manage_operators"), parse_mode="HTML")
            return
        targets = parse_operator_targets(text, message.entities, op_prefix)
        if not targets:
            bot.reply_to(
                message,
                f"💡 用法：<code>{op_prefix} @user</code>\n"
                f"也可一次多个：<code>{op_prefix} @a @b @c</code>",
                parse_mode="HTML",
            )
            return
        ops = get_group_operators(gid)
        added = []
        for target in targets:
            if target not in ops:
                ops.append(target)
                added.append(target)
        if added:
            update_setting(gid, "operators", json.dumps(ops, ensure_ascii=False))
            names = "、".join(f"<b>{op_name}</b>" for op_name in added)
            bot.reply_to(message, tr(gid, "operators_added", names=names), parse_mode="HTML")
        else:
            names = "、".join(f"<b>{op_name}</b>" for op_name in targets)
            bot.reply_to(message, tr(gid, "operators_exist", names=names), parse_mode="HTML")
        return

    removed_op = False
    for rk in ("remove_operator", "remove_operator2"):
        rest = strip_cmd_prefix(text, cmd(gid, rk))
        if rest is None:
            continue
        if not can_manage_group_operators(uid):
            bot.reply_to(message, tr(gid, "no_manage_operators"), parse_mode="HTML")
            return
        target = extract_mention(text, message.entities) or rest
        target = normalize_operator_name(target)
        ops = get_group_operators(gid)
        removed = False
        for candidate in (target, target.lstrip("@"), f"@{target.lstrip('@')}"):
            if candidate in ops:
                ops.remove(candidate)
                removed = True
                break
        if removed:
            update_setting(gid, "operators", json.dumps(ops, ensure_ascii=False))
            bot.reply_to(message, tr(gid, "operator_removed", name=target), parse_mode="HTML")
        else:
            bot.reply_to(message, tr(gid, "operator_not_found", name=target), parse_mode="HTML")
        removed_op = True
        break
    if removed_op:
        return

    if match_exact(text, gid, "delete_last") or match_exact(text, gid, "delete_today") or match_exact(text, gid, "delete_all"):
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_delete_perm"), parse_mode="HTML")
            return
        conn = get_db()
        c = conn.cursor()
        if match_exact(text, gid, "delete_last"):
            c.execute("SELECT id, remark, amount FROM bills WHERE group_id = ? ORDER BY id DESC LIMIT 1", (gid,))
            row = c.fetchone()
            if row:
                c.execute("DELETE FROM bills WHERE id = ?", (row[0],))
                bot.reply_to(message, tr(gid, "delete_last_ok", remark=row[1] or tr(gid, "no_remark"), amount=row[2]))
            else:
                bot.reply_to(message, tr(gid, "no_bills"))
        elif match_exact(text, gid, "delete_today"):
            c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ?", (gid, today))
            bot.reply_to(message, tr(gid, "delete_today_ok", date=today))
        else:
            c.execute("DELETE FROM bills WHERE group_id = ?", (gid,))
            bot.reply_to(message, tr(gid, "delete_all_ok"))
        conn.commit()
        conn.close()
        send_text_bill_report(gid, gid, today)
        return

    del_rest = strip_cmd_prefix(text, cmd(gid, "delete_remark"))
    if del_rest is not None and del_rest:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_delete_perm"), parse_mode="HTML")
            return
        remark = del_rest
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "DELETE FROM bills WHERE group_id = ? AND date_str = ? AND remark = ? AND bill_type = 'income'",
            (gid, today, remark),
        )
        deleted = c.rowcount
        conn.commit()
        conn.close()
        if deleted:
            bot.reply_to(message, tr(gid, "delete_remark_ok", remark=remark, n=deleted))
            send_text_bill_report(gid, gid, today)
        else:
            bot.reply_to(message, tr(gid, "delete_remark_none", remark=remark))
        return

    view_rest = strip_cmd_prefix(text, cmd(gid, "view_remark"))
    if view_rest is not None:
        if view_rest:
            remark = view_rest
            if remark.startswith("T") and len(remark) == 34:
                return
            conn = get_db()
            c = conn.cursor()
            c.execute(
                "SELECT timestamp, amount, usdt_amount, username FROM bills "
                "WHERE group_id = ? AND date_str = ? AND remark = ? AND bill_type = 'income'",
                (gid, today, remark),
            )
            rows = c.fetchall()
            conn.close()
            if not rows:
                bot.reply_to(message, tr(gid, "view_remark_none", remark=remark))
                return
            detail_lines = [tr(gid, "view_remark_title", remark=_tag_remark(remark).strip())]
            total_r, total_u = 0.0, 0.0
            for ts, amt, uamt, uname in rows:
                detail_lines.append(f"{ts[11:16]} {_tag_rmb(amt)} RMB→{uamt:.1f}U {_tag_operator(uname)}")
                total_r += amt
                total_u += uamt
            detail_lines.append(tr(
                gid, "view_remark_total",
                rmb=_tag_rmb(total_r), usdt=f"{total_u:.1f}",
            ))
            bot.reply_to(message, "\n".join(detail_lines), parse_mode="HTML")
        else:
            c = cmd(gid, "view_remark")
            bot.reply_to(message, f"💡 用法：{c} remark")
        return

    if match_exact(text, gid, "class_start"):
        if not can_operate_in_group(gid, uid, tg_username):
            return
        update_setting(gid, "is_active", 1)
        bot.reply_to(message, tr(gid, "class_start"))
        return

    if match_exact(text, gid, "class_end"):
        if not can_operate_in_group(gid, uid, tg_username):
            return
        update_setting(gid, "is_active", 0)
        bot.reply_to(message, tr(gid, "class_end"))
        send_text_bill_report(gid, gid, today)
        return

    if not get_setting(gid, "is_active"):
        if looks_like_billing_command(text, gid):
            bot.reply_to(message, build_need_class_start(gid), parse_mode="HTML")
        return

    if not can_operate_in_group(gid, uid, tg_username):
        if looks_like_billing_command(text, gid):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
        return

    if match_exact(text, gid, "bill_zero"):
        send_text_bill_report(gid, gid, today)
        return

    m_exp = expense_match(text, gid)
    if m_exp:
        add_bill(gid, uid, display_name, m_exp.group(1).strip(), float(m_exp.group(2)), "expense")
        send_text_bill_report(gid, gid, today)
        return

    m_inc = re.match(r"^(.*?)([\+\-])(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$", text)
    if m_inc:
        try:
            amount = float(m_inc.group(3))
            if m_inc.group(2) == "-":
                amount = -amount
            rate = float(m_inc.group(4)) if m_inc.group(4) else None
            add_bill(gid, uid, display_name, m_inc.group(1).strip(), amount, "income", rate)
            send_text_bill_report(gid, gid, today)
        except Exception as exc:
            log.exception("记入款失败: %s", exc)
            bot.reply_to(message, tr(gid, "bill_fail", err=exc))
        return


# ---------------------------------------------------------------------------
# Flask web dashboard
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>分布式全功能网页账单</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,sans-serif}
body{background:#f4f6f9;color:#475569;padding:12px;line-height:1.35;font-size:12px}
.container{max-width:800px;margin:0 auto;background:#fff;border-radius:12px;padding:14px;box-shadow:0 4px 12px rgba(0,0,0,.05);font-size:12px}
.header{text-align:center;margin-bottom:16px;border-bottom:2px solid #edf2f7;padding-bottom:12px}
.header h2{font-size:16px;color:#334155}
.date-picker{margin:10px 0;background:#f8fafc;padding:8px;border-radius:6px;display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:8px;border:1px dashed #cbd5e1;font-size:11px}
.date-tags{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:6px}
.date-tag{font-size:11px;padding:3px 7px;border-radius:999px;border:1px solid #cbd5e1;background:#fff;cursor:pointer;text-decoration:none;color:#334155}
.date-tag.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
.nav-btn{padding:5px 10px;border-radius:4px;border:1px solid #cbd5e1;background:#fff;cursor:pointer;font-size:11px;color:#334155}
.nav-btn:disabled{opacity:.45;cursor:not-allowed}
.summary-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:20px;border-top:2px dashed #cbd5e1;padding-top:16px}
.card{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px;text-align:center}
.card .title{font-size:11px;color:#64748b}
.card .value{font-size:15px;font-weight:bold;margin-top:2px}
h3{font-size:13px;margin:20px 0 6px;padding-left:6px;border-left:4px solid #3b82f6;color:#334155}
.exp-title{border-left-color:#ef4444}.cate-title{border-left-color:#10b981}
table{width:100%;border-collapse:collapse;margin-top:4px;font-size:11px}
th,td{padding:7px 8px;border-bottom:1px solid #e2e8f0;text-align:left}
th{background:#f1f5f9;color:#64748b;font-size:11px}
.badge{display:inline-block;padding:2px 6px;font-size:10px;border-radius:4px;font-weight:bold;background:#e2e8f0}
.bg-inc{background:#dcfce7;color:#15803d}.bg-exp{background:#fee2e2;color:#b91c1c}
.hint{font-size:11px;color:#64748b;margin-top:4px}
.c-remark{color:#ca8a04;font-weight:600}
.c-op{color:#2563eb;font-weight:500}
.c-rmb{color:#0f172a;font-weight:700}
.c-u{color:#64748b}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h2>📊 分布式对账看板</h2>
<p id="group-text" style="font-size:12px;color:#64748b;margin-top:4px">加载中...</p>
<p id="summary-text" class="hint"></p>
<div class="date-picker">
<button id="btn-prev" type="button" class="nav-btn">◀ 跳前</button>
<label for="date-select">📅 账单日期：</label>
<input type="date" id="date-select">
<button id="btn-next" type="button" class="nav-btn">跳后 ▶</button>
<button id="btn-all" type="button" class="nav-btn">全部历史</button>
</div>
<div id="date-tags" class="date-tags"></div>
</div>
<h3 id="income-title">📥 入款（0笔）</h3>
<table><thead><tr><th>日期</th><th>时间</th><th>备注</th><th>RMB</th><th>U</th><th>操作人</th></tr></thead><tbody id="income-list"></tbody></table>
<h3 class="exp-title" id="expense-title">📤 下发（0笔）</h3>
<table><thead><tr><th>日期</th><th>时间</th><th>备注</th><th>USDT</th><th>操作人</th></tr></thead><tbody id="expense-list"></tbody></table>
<h3 class="cate-title">🗂️ 备注分类</h3>
<table><thead><tr><th>备注</th><th>RMB</th><th>USDT</th><th>笔数</th></tr></thead><tbody id="cate-list"></tbody></table>
<div class="summary-grid">
<div class="card"><div class="title">汇率</div><div class="value" id="rate">0</div></div>
<div class="card"><div class="title">总入款 RMB</div><div class="value" id="total_rmb">0</div></div>
<div class="card"><div class="title">总入款 USDT</div><div class="value" id="total_usdt">0U</div></div>
<div class="card"><div class="title">已下发 USDT</div><div class="value" id="expense_usdt">0U</div></div>
<div class="card" style="grid-column:span 2"><div class="title">未下发 USDT</div><div class="value" id="remaining_usdt">0U</div></div>
</div>
</div>
<script>
const params=new URLSearchParams(location.search);
const groupId=params.get('group_id')||'0';
let currentDate=params.get('date')||'';
document.getElementById('group-text').textContent='群组 ID: '+groupId;
const ds=document.getElementById('date-select');
const btnPrev=document.getElementById('btn-prev');
const btnNext=document.getElementById('btn-next');
function localToday(){
const n=new Date();
return n.getFullYear()+'-'+String(n.getMonth()+1).padStart(2,'0')+'-'+String(n.getDate()).padStart(2,'0');
}
function shiftDate(dateStr,delta){
const p=dateStr.split('-').map(Number);
const dt=new Date(p[0],p[1]-1,p[2]);
dt.setDate(dt.getDate()+delta);
return dt.getFullYear()+'-'+String(dt.getMonth()+1).padStart(2,'0')+'-'+String(dt.getDate()).padStart(2,'0');
}
function goDate(d){
location.href='?group_id='+groupId+'&date='+encodeURIComponent(d);
}
ds.onchange=()=>goDate(ds.value);
document.getElementById('btn-all').onclick=()=>goDate('all');
btnPrev.onclick=()=>{
const base=(currentDate&&currentDate!=='all')?currentDate:(window.__serverToday||localToday());
goDate(shiftDate(base,-1));
};
btnNext.onclick=()=>{
const base=(currentDate&&currentDate!=='all')?currentDate:(window.__serverToday||localToday());
const next=shiftDate(base,1);
const maxDay=window.__serverToday||localToday();
if(next>maxDay)return;
goDate(next);
};
async function load(){
const d=currentDate||localToday();
if(d!=='all'){ds.value=d;}
const r=await fetch('/api/bill?group_id='+groupId+'&date='+encodeURIComponent(d));
const data=await r.json();
if(data.server_today && !params.get('date')){goDate(data.server_today);return;}
window.__serverToday=data.server_today||localToday();
const viewDay=(d==='all')?window.__serverToday:d;
btnPrev.disabled=false;
btnNext.disabled=(viewDay>=window.__serverToday);
document.getElementById('summary-text').textContent=
(d==='all'?'查看全部历史':('当前日期 '+d+'（北京时间）'))+
' · 入款 '+data.income_count+' 笔 · 下发 '+data.expense_count+' 笔';
document.getElementById('income-title').textContent='📥 入款（'+data.income_count+'笔）';
document.getElementById('expense-title').textContent='📤 下发（'+data.expense_count+'笔）';
['rate','total_rmb'].forEach(k=>document.getElementById(k).textContent=data[k]);
document.getElementById('total_usdt').textContent=data.total_usdt+' U';
document.getElementById('expense_usdt').textContent=data.expense_usdt+' U';
document.getElementById('remaining_usdt').textContent=data.remaining_usdt+' U';
const tags=document.getElementById('date-tags');
tags.innerHTML=(data.available_dates||[]).map(x=>{
const active=(d===x.date)?' active':'';
return '<a class="date-tag'+active+'" href="?group_id='+groupId+'&date='+x.date+'">'
+x.date+' ('+x.income+'/'+x.expense+')</a>';
}).join('');
document.getElementById('cate-list').innerHTML=(data.category_summary||[]).length
?data.category_summary.map(c=>'<tr><td><span class="badge bg-inc c-remark">'+c.remark+'</span></td><td><span class="c-rmb">'+c.total_rmb+'</span></td><td class="c-u">'+c.total_usdt+' U</td><td>'+c.count+'</td></tr>').join('')
:'<tr><td colspan="4" style="text-align:center;color:#94a3b8">暂无</td></tr>';
document.getElementById('income-list').innerHTML=(data.income_bills||[]).length
?data.income_bills.map(b=>'<tr><td>'+b.date+'</td><td>'+b.time+'</td><td><span class="c-remark">'+b.remark+'</span></td><td><span class="c-rmb">+'+b.amount+'</span></td><td class="c-u">'+b.usdt+' U</td><td><span class="c-op">'+b.username+'</span></td></tr>').join('')
:'<tr><td colspan="6" style="text-align:center;color:#94a3b8">暂无入款</td></tr>';
document.getElementById('expense-list').innerHTML=(data.expense_bills||[]).length
?data.expense_bills.map(e=>'<tr><td>'+e.date+'</td><td>'+e.time+'</td><td><span class="c-remark">'+e.remark+'</span></td><td class="c-u">-'+e.usdt+' U</td><td><span class="c-op">'+e.username+'</span></td></tr>').join('')
:'<tr><td colspan="5" style="text-align:center;color:#94a3b8">暂无下发</td></tr>';
}
load();
</script>
</body>
</html>"""


@flask_app.route("/")
def index():
    return DASHBOARD_HTML


@flask_app.route("/api/bill")
def api_bill():
    try:
        group_id = int(request.args.get("group_id", "0").strip())
    except ValueError:
        group_id = 0

    tz = get_setting(group_id, "timezone") or "Asia/Shanghai"
    now, _, _ = get_current_time(tz)
    server_today = now.strftime("%Y-%m-%d")
    target_date = request.args.get("date") or server_today

    income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)
    rate = get_setting(group_id, "exchange_rate") or 7.2
    total_rmb = (total_income[0] or 0) if total_income else 0
    total_usdt = (total_income[1] or 0) if total_income else 0
    expense_usdt = (total_expense[0] or 0) if total_expense else 0

    income_bills = [
        {
            "remark": r[0] or "无备注",
            "username": r[1] or "未知",
            "amount": f"{r[2]:.0f}",
            "usdt": f"{r[3]:.2f}",
            "time": r[5][11:19] if r[5] else "",
            "date": r[6] if len(r) > 6 else target_date,
        }
        for r in income
    ]
    expense_bills = [
        {
            "remark": r[0] or "无备注",
            "username": r[1] or "未知",
            "usdt": f"{r[2]:.2f}",
            "time": r[4][11:19] if r[4] else "",
            "date": r[5] if len(r) > 5 else target_date,
        }
        for r in expense
    ]

    summary = {}
    for row in income:
        rem = (row[0] or "空备注").strip() or "空备注"
        summary.setdefault(rem, {"total_rmb": 0.0, "total_usdt": 0.0, "count": 0})
        summary[rem]["total_rmb"] += row[2] or 0
        summary[rem]["total_usdt"] += row[3] or 0
        summary[rem]["count"] += 1

    category_summary = [
        {
            "remark": k,
            "total_rmb": f"{v['total_rmb']:.0f}",
            "total_usdt": f"{v['total_usdt']:.2f}",
            "count": v["count"],
        }
        for k, v in summary.items()
    ]

    return jsonify({
        "exchange_rate": f"{rate:.2f}",
        "total_rmb": f"{total_rmb:.0f}",
        "total_usdt": f"{total_usdt:.2f}",
        "expense_usdt": f"{expense_usdt:.2f}",
        "remaining_usdt": f"{total_usdt - expense_usdt:.2f}",
        "income_bills": income_bills,
        "expense_bills": expense_bills,
        "category_summary": category_summary,
        "income_count": len(income),
        "expense_count": len(expense),
        "server_today": server_today,
        "query_date": target_date,
        "available_dates": get_bill_dates(group_id),
    })


@flask_app.route("/health")
def health():
    return "ok", 200


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "ok", 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def setup_bot_commands():
    """注册 /start 到 Telegram 输入框左侧 Menu（☰）里。"""
    commands = [
        telebot.types.BotCommand("start", "打开主菜单"),
        telebot.types.BotCommand("help", "使用帮助"),
    ]
    try:
        bot.set_my_commands(commands, scope=telebot.types.BotCommandScopeDefault())
        bot.set_my_commands(commands, scope=telebot.types.BotCommandScopeAllPrivateChats())
        bot.set_chat_menu_button(menu_button=telebot.types.MenuButtonCommands())
        me = bot.get_me()
        log.info("Bot menu OK (@%s): /start, /help", me.username)
    except Exception as exc:
        log.exception("注册 Bot 左侧 Menu 失败: %s", exc)


def setup_webhook():
    setup_bot_commands()
    refresh_bot_display_name()
    try:
        bot.remove_webhook()
        ok = bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
        if ok:
            log.info("Webhook OK: %s/webhook", WEBHOOK_URL)
        else:
            log.warning("set_webhook returned False")
    except Exception as exc:
        log.error("Webhook setup failed: %s", exc)


if __name__ == "__main__":
    log.info("Starting on 0.0.0.0:%s  WEBHOOK_URL=%s", PORT, WEBHOOK_URL)
    setup_webhook()
    flask_app.run(host="0.0.0.0", port=PORT)
