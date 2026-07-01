"""
小财家记账 Plus（增强版）Telegram Bot + Flask Web 看板

在 xiaocaicai.py 基础上新增：倍数/费率/U 入款下发、回复撤销、实时汇率、群员自查等。

部署环境变量:
  TELEGRAM_TOKEN  — 请用【新 Bot】的 Token，勿与 xiaocaicai.py 共用
  WEBHOOK_URL     — 本服务地址
  PORT            — 端口（可选）
  DATABASE_PATH   — 默认 bot_data_plus.db，避免与原版数据库冲突
  PHONE_LOOKUP_URL — 可选，手机号归属地 API，用 {phone} 占位

本地测试: python xiaocaicai_plus.py
"""

import json
import logging
import os
import random
import re
import shutil
import sqlite3
import io
import ast
import operator
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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://newbot-oenu.onrender.com").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
DEFAULT_TZ = "Asia/Shanghai"


def _default_database_path():
    env_path = os.environ.get("DATABASE_PATH", "").strip()
    if env_path:
        return env_path
    if os.path.isdir("/data"):
        return "/data/bot_data_plus.db"
    return "bot_data_plus.db"


DATABASE_PATH = _default_database_path()
PHONE_LOOKUP_URL = os.environ.get("PHONE_LOOKUP_URL", "").strip()

# 身份证省级区划（GB/T 2260 前两位）
ID_PROVINCE_MAP = {
    "11": "北京市", "12": "天津市", "13": "河北省", "14": "山西省", "15": "内蒙古自治区",
    "21": "辽宁省", "22": "吉林省", "23": "黑龙江省",
    "31": "上海市", "32": "江苏省", "33": "浙江省", "34": "安徽省", "35": "福建省",
    "36": "江西省", "37": "山东省",
    "41": "河南省", "42": "湖北省", "43": "湖南省", "44": "广东省", "45": "广西壮族自治区",
    "46": "海南省",
    "50": "重庆市", "51": "四川省", "52": "贵州省", "53": "云南省", "54": "西藏自治区",
    "61": "陕西省", "62": "甘肃省", "63": "青海省", "64": "宁夏回族自治区", "65": "新疆维吾尔自治区",
    "71": "台湾省", "81": "香港特别行政区", "82": "澳门特别行政区",
}

# 常见身份证 6 位区划（节选，可继续扩充）
ID_AREA_MAP = {
    "110101": "北京市东城区", "110102": "北京市西城区", "110105": "北京市朝阳区",
    "310101": "上海市黄浦区", "310104": "上海市徐汇区", "310115": "上海市浦东新区",
    "440106": "广东省广州市天河区", "440304": "广东省深圳市福田区",
    "330106": "浙江省杭州市西湖区", "320102": "江苏省南京市玄武区",
    "510104": "四川省成都市锦江区", "420106": "湖北省武汉市武昌区",
}

# 常见银行卡 BIN（前 6 位起匹配，越长越优先）
BANK_BIN_MAP = (
    ("621700", "中国建设银行", "借记卡"),
    ("622700", "中国建设银行", "借记卡"),
    ("622848", "中国农业银行", "借记卡"),
    ("622202", "中国工商银行", "借记卡"),
    ("622208", "中国工商银行", "借记卡"),
    ("621661", "中国银行", "借记卡"),
    ("621660", "中国银行", "借记卡"),
    ("622260", "交通银行", "借记卡"),
    ("622588", "招商银行", "借记卡"),
    ("621098", "中国邮政储蓄银行", "借记卡"),
    ("622150", "中国邮政储蓄银行", "借记卡"),
    ("622126", "中国邮政储蓄银行", "借记卡"),
    ("622908", "兴业银行", "借记卡"),
    ("622666", "光大银行", "借记卡"),
    ("622622", "光大银行", "借记卡"),
    ("622518", "浦发银行", "借记卡"),
    ("622155", "平安银行", "借记卡"),
    ("622568", "广发银行", "借记卡"),
    ("622690", "中信银行", "借记卡"),
    ("622630", "华夏银行", "借记卡"),
)

BANK_CODE_NAMES = {
    "CCB": "中国建设银行", "ICBC": "中国工商银行", "ABC": "中国农业银行",
    "BOC": "中国银行", "COMM": "交通银行", "PSBC": "中国邮政储蓄银行",
    "CMB": "招商银行", "CMBC": "中国民生银行", "CIB": "兴业银行",
    "CEB": "中国光大银行", "SPDB": "浦发银行", "PAB": "平安银行",
    "GDB": "广发银行", "CITIC": "中信银行", "HXB": "华夏银行",
}

CARD_TYPE_NAMES = {
    "DC": "借记卡", "CC": "信用卡", "SCC": "准贷记卡", "PC": "预付费卡",
}

# 手机号段运营商（中国大陆）
PHONE_CARRIER_PREFIXES = (
    (("134", "135", "136", "137", "138", "139", "147", "150", "151", "152", "157", "158", "159",
      "172", "178", "182", "183", "184", "187", "188", "195", "197", "198"), "中国移动"),
    (("130", "131", "132", "145", "155", "156", "166", "171", "175", "176", "185", "186", "196"), "中国联通"),
    (("133", "149", "153", "173", "177", "180", "181", "189", "191", "193", "199"), "中国电信"),
    (("192",), "中国广电"),
)

# ========== 品牌与价格（复制新机器人时主要改这里）==========
BOT_NAME = "小财家Plus"
BOT_BRAND = f"{BOT_NAME}记账"
PRICE_1_MONTH = 80
PRICE_2_MONTH = 140
PRICE_3_MONTH = 220

FOUNDER_USERS = [
    int(x.strip())
    for x in os.environ.get("FOUNDER_USER_IDS", "8551762310").split(",")
    if x.strip().isdigit()
] or [8807178282]
# 卖家联系方式：陌生人想买第二款机器人时展示。可填用户名，或留空自动读 SELLER_USER_ID 的 @用户名
SELLER_USER_ID = int(os.environ.get("SELLER_USER_ID", str(FOUNDER_USERS[0])))
SELLER_USERNAME = os.environ.get("SELLER_USERNAME", "Baima86").strip().lstrip("@")
TRON_ADDRESS = "TVnjLwDrGjYVRTa1ukfoE2mFTmCxtrjoCw"
MAX_LEVEL2_VIPS = 5
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
SETTING_KEYS = {
    "operators", "exchange_rate", "fee_rate", "is_active",
    "language", "timezone", "show_usdt", "expire_time", "extra_settings",
}

DEFAULT_EXTRA_SETTINGS = {
    "income_exchange_rate": None,
    "expense_exchange_rate": None,
    "income_fee_rate": None,
    "expense_fee_rate": None,
    "realtime_rate_offset": 0.0,
    "use_realtime_rate": False,
    "payment_price": 0.0,
    "currency": "CNY",
    "expense_mode": "usdt",
    "multiply_rate_mode": False,
    "show_rmb": True,
    "display_count": 5,
    "time_format": "hm",
    "pin_bills": False,
    "day_cut_hour": None,
    "global_day_cut_hour": None,
    "address_detect": False,
    "bank_detect": False,
    "user_change_notify": False,
    "classify_mode": "none",
    "collection_enabled": False,
    "collection_interval": 1,
    "payout_addresses": [],
    "all_operators": False,
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
    db_dir = os.path.dirname(os.path.abspath(DATABASE_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def now_shanghai():
    return datetime.now(pytz.timezone(DEFAULT_TZ)).replace(tzinfo=None)


def now_shanghai_str():
    return now_shanghai().strftime("%Y-%m-%d %H:%M:%S")


def parse_expire_time(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    return None


def is_expire_active(expire_time_str):
    expire = parse_expire_time(expire_time_str)
    if not expire:
        return False
    return now_shanghai() < expire


def format_expire_remaining(expire_time_str):
    expire = parse_expire_time(expire_time_str)
    if not expire:
        return ""
    delta = expire - now_shanghai()
    if delta.total_seconds() <= 0:
        return "已到期"
    days = delta.days
    hours = (delta.seconds // 3600) % 24
    if days > 0:
        return f"还剩 {days} 天 {hours} 小时"
    return f"还剩 {hours} 小时"


def backup_database():
    """把数据库复制到 backups 目录，部署/故障时可恢复。"""
    if not os.path.exists(DATABASE_PATH):
        return None
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(DATABASE_PATH)), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = now_shanghai().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(backup_dir, f"bot_data_{stamp}.db")
    shutil.copy2(DATABASE_PATH, dest)
    backups = sorted(
        [f for f in os.listdir(backup_dir) if f.startswith("bot_data_") and f.endswith(".db")],
        reverse=True,
    )
    for old in backups[14:]:
        try:
            os.remove(os.path.join(backup_dir, old))
        except OSError:
            pass
    return dest


def get_db_stats():
    stats = {
        "db_path": os.path.abspath(DATABASE_PATH),
        "db_size": 0,
        "vip_users": 0,
        "bills": 0,
        "settings": 0,
        "vip1": None,
        "backups": 0,
    }
    if os.path.exists(DATABASE_PATH):
        stats["db_size"] = os.path.getsize(DATABASE_PATH)
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM vip_users")
        stats["vip_users"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM bills")
        stats["bills"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM settings")
        stats["settings"] = c.fetchone()[0]
        c.execute("SELECT user_id, expire_time FROM vip_users WHERE level = 1 LIMIT 1")
        row = c.fetchone()
        if row:
            stats["vip1"] = {"user_id": row[0], "expire_time": row[1], "active": is_expire_active(row[1])}
        conn.close()
    except Exception as exc:
        log.exception("get_db_stats: %s", exc)
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(DATABASE_PATH)), "backups")
    if os.path.isdir(backup_dir):
        stats["backups"] = len([
            f for f in os.listdir(backup_dir) if f.startswith("bot_data_") and f.endswith(".db")
        ])
    return stats


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


def migrate_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("PRAGMA table_info(settings)")
    setting_cols = {row[1] for row in c.fetchall()}
    if "extra_settings" not in setting_cols:
        c.execute("ALTER TABLE settings ADD COLUMN extra_settings TEXT DEFAULT '{}'")
    c.execute("PRAGMA table_info(bills)")
    bill_cols = {row[1] for row in c.fetchall()}
    if "source_message_id" not in bill_cols:
        c.execute("ALTER TABLE bills ADD COLUMN source_message_id INTEGER")
    conn.commit()
    conn.close()


init_db()
migrate_db()
try:
    backup_path = backup_database()
    if backup_path:
        log.info("Database backup: %s", backup_path)
except Exception as exc:
    log.warning("Database backup skipped: %s", exc)
log.info("SQLite database: %s", os.path.abspath(DATABASE_PATH))
if not os.path.abspath(DATABASE_PATH).startswith(os.path.abspath("/data")):
    msg = "数据库未在 /data 持久化磁盘上，Deploy 后 VIP 与账单可能丢失。"
    if os.environ.get("RENDER"):
        msg += " Render Pro 也请在 Disks 挂载 /data 并设 DATABASE_PATH=/data/bot_data.db"
    log.warning(msg)


def get_current_time(timezone_str=DEFAULT_TZ):
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
            if is_expire_active(row[0]):
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
    now = now_shanghai()
    if row:
        current = parse_expire_time(row[0])
        base = current if current and current > now else now
    else:
        base = now
    expire_str = (base + timedelta(days=30 * months)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT OR REPLACE INTO vip_users (user_id, username, expire_time, level) VALUES (?, ?, ?, ?)",
        (user_id, username, expire_str, level),
    )
    conn.commit()
    conn.close()
    log.info("VIP updated uid=%s level=%s expire=%s db=%s", user_id, level, expire_str, DATABASE_PATH)
    try:
        backup_database()
    except Exception as exc:
        log.warning("Post-VIP backup failed: %s", exc)
    return expire_str


def get_vip1_buyer_user_id():
    """VIP1 买家 UID（含已到期），用于续费归属判断。"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM vip_users WHERE level = 1 LIMIT 1")
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def get_level2_vip_count():
    now_str = now_shanghai_str()
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
    now_str = now_shanghai_str()
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
    now_str = now_shanghai_str()
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
    active = get_active_vip1_buyer_id()
    if active:
        return user_id == active
    registered = get_vip1_buyer_user_id()
    if registered:
        return user_id == registered
    return True


def build_expire_status_message(has_auth, lvl_desc, expire_time, lvl):
    """私聊「查看到期时间」的详细说明（北京时间）。"""
    if has_auth:
        remain = format_expire_remaining(expire_time)
        status = f"🟢 正常生效中（{remain}）" if remain else "🟢 正常生效中"
        footer = (
            "📌 到期后：群内<b>历史账单、操作人、语言设置</b>都会保留。\n"
            "续费后立即恢复，无需重新设置。"
        )
    elif expire_time and expire_time not in ("未激活", "永久终身授权"):
        status = "🔴 已到期，请续费"
        footer = (
            "📌 续费<b>不会删除</b>您群里的：\n"
            "• 历史记账记录（网页账单可查）\n"
            "• 已设群操作人\n"
            "• 二级权限人（若未单独到期）\n"
            "• 汇率 / 费率 / 语言等群设置\n\n"
            "请点「自助续费说明」提交凭证，审核通过即恢复。"
        )
    else:
        status = "🔴 未激活"
        footer = (
            "若您曾开通过却显示未激活，可能是服务器数据库被清空，"
            "请联系创始人检查 Render 是否已挂载 Persistent Disk（/data）。"
        )
    expire_label = f"{expire_time}（北京时间）" if expire_time not in ("未激活", "永久终身授权") else expire_time
    return (
        f"👤 <b>您的身份体系：</b>\n"
        f"• 级别：<code>{lvl_desc}</code>\n"
        f"• 状态：{status}\n"
        f"• 有效截止期：<code>{expire_label}</code>\n\n"
        f"{footer}"
    )


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
    brand = get_bot_brand()
    short = get_bot_short_name()

    if lang == "eng":
        return (
            f"📖 <b>[{brand}] Full Command Guide (English)</b>\n\n"
            f"🤖 <b>{short}</b> — tap <code>command</code> to copy.\n\n"
            "👑 <b>Roles</b>\n"
            "VIP1 buyer · VIP2 manager · Group operator\n\n"
            "💰 <b>Bookkeeping</b>\n"
            "├ Deposit <code>+10000</code>\n"
            "├ Deposit + rate <code>+10000/7.1</code>\n"
            "├ Deposit × multiplier <code>+10000*5</code>\n"
            "├ Deposit × mult + rate <code>+10000*5/7.1</code>\n"
            "├ Deposit + fee % <code>+1000*12%</code>\n"
            "├ Deposit in USDT <code>+10000U</code>\n"
            "├ Deposit + remark <code>+1000 remark text</code>\n"
            "├ Payout <code>下发5000</code>\n"
            "├ Payout × multiplier <code>下发5000*5</code>\n"
            "├ Payout × mult + rate <code>下发5000*5/7.1</code>\n"
            "├ Payout + rate <code>下发1000/7.8</code>\n"
            "├ Payout in USDT <code>下发5000U</code>\n"
            "├ View bill <code>+0</code>\n"
            "├ Member self-check <code>账单</code> or <code>/me</code>\n"
            "├ Start <code>开始</code> (alias: <code>上课</code>)\n"
            "└ Stop <code>关闭</code> (alias: <code>下课</code> / <code>拉停</code>)\n\n"
            "✏️ <b>Edits</b> (reply to a message)\n"
            "├ Undo <code>撤销</code>\n"
            "├ Undo deposit <code>撤销入款</code>\n"
            "├ Undo N deposits <code>撤销入款5条</code>\n"
            "├ Undo N payouts <code>撤销下发5条</code>\n"
            "├ Undo last <code>撤销最后</code>\n"
            "├ Clear today <code>撤销今天</code> / <code>撤销账单</code>\n"
            "├ Clear all <code>撤销全部</code>\n"
            "├ Undo remark deposits <code>撤销 张三</code>\n"
            "├ Sync bill rate <code>修改汇款10</code>\n"
            "└ Halt <code>拉停</code>\n\n"
            "⚙️ <b>Settings</b> (operator+)\n"
            "├ Fee <code>设置费率10</code> (negative % OK, default: income)\n"
            "├ Income fee <code>设置入款费率10</code>\n"
            "├ Payout fee <code>设置下发费率10</code>\n"
            "├ Rate <code>设置汇率8</code>\n"
            "├ Income rate <code>设置入款汇率8</code>\n"
            "├ Payout rate <code>设置下发汇率8</code>\n"
            "├ Live rate <code>设置实时汇率</code>\n"
            "├ Live rate offset <code>设置实时汇率-1</code>\n"
            "├ Payout price <code>设置代付价格10</code>\n"
            "├ Currency <code>设置币种HKD</code>\n"
            "├ Payout mode <code>设置下发人民币模式</code> / <code>设置下发币模式</code>\n"
            "├ Rate mode <code>开启乘汇率模式</code> / <code>关闭乘汇率模式</code>\n"
            "├ Show RMB <code>显示人民币</code> / <code>隐藏人民币</code>\n"
            "├ List lines <code>显示条数10</code>\n"
            "├ Time format <code>显示分秒</code> / <code>显示时分秒</code>\n"
            "├ Pin report <code>开启记账置顶</code> / <code>关闭记账置顶</code>\n"
            "├ Pin message <code>置顶</code> / <code>取消置顶</code> (reply)\n"
            "├ Day cut <code>设置日切04</code>\n"
            "├ Global day cut <code>设置全局日切04</code>\n"
            "├ Off day cut <code>关闭日切</code> / <code>关闭全局日切</code>\n"
            "├ Address detect <code>开启地址识别</code> / <code>关闭地址识别</code>\n"
            "├ Bank detect <code>开启银行卡自动识别</code> / <code>关闭银行卡识别</code>\n"
            "├ Member alerts <code>开启用户变更通知</code> / <code>关闭用户变更通知</code>\n"
            "├ Notify all <code>通知所有人</code>\n"
            "├ Categories <code>开启操作人分类</code> / <code>开启回复人分类</code> / <code>关闭分类</code>\n"
            "├ Collection <code>开启催收</code> / <code>关闭催收</code> / <code>催收1</code> (minutes)\n"
            "├ Payout addresses <code>设置下发地址{addr}</code> / <code>删除下发地址{addr}</code>\n"
            "├ Add operator <code>设置操作人@user</code> (or reply + command)\n"
            "├ Remove operator <code>移除操作人@user</code>\n"
            "└ All operators <code>设置所有人</code> / <code>取消所有人</code>\n\n"
            "🔍 <b>Lookup</b>\n"
            "├ Current rate <code>汇率</code>\n"
            "├ OKX OTC <code>Z0</code>\n"
            "├ Huobi OTC <code>H0</code>\n"
            "├ MYR OTC <code>m0</code>\n"
            "├ Bybit MMK quick <code>mm0</code> · all <code>bma</code>\n"
            "├ Bybit KBZPay buy <code>bkb</code> · Mobile <code>bmm</code>\n"
            "├ Bybit sell U KBZ <code>skb</code> · Bank Transfer <code>smm</code>\n"
            "├ OKX all buy <code>la</code> · bank buy <code>lk</code> · Alipay buy <code>lz</code> · WeChat buy <code>lw</code>\n"
            "├ OKX bank sell <code>lmk</code> · Alipay sell <code>lmz</code> · WeChat sell <code>lmw</code>\n"
            "├ Convert <code>查汇率100</code>\n"
            "├ Point calc <code>币价100 汇率10</code>\n"
            "├ Phone <code>查询13800138000</code> — carrier/location\n"
            "├ Bank card <code>查询6217001234567890</code> — issuer/type\n"
            "├ ID card <code>查询110101199001011234</code> — region/birth/gender\n"
            "└ USDT TRC20 — send address or <code>查看 T...34chars</code>\n\n"
            "🎨 <b>Private (VIP1):</b> rename bot · change avatar · VIP2 · renew"
        )

    if lang == "my":
        return (
            f"📖 <b>[{brand}] အမိန့်စာရင်း (Myanmar)</b>\n\n"
            f"🤖 <b>{short}</b> — <code>command</code> ကို နှိပ်ပြီး copy လုပ်နိုင်သည်。\n\n"
            "👑 <b>အခန်းကဏ္ဍ</b>\n"
            "VIP1 buyer · VIP2 · Operator\n\n"
            "💰 <b>မှတ်တမ်းတင်</b>\n"
            "├ ဝင်ငွေ <code>+10000</code>\n"
            "├ နှုန်းနှင့်ဝင်ငွေ <code>+10000/7.1</code>\n"
            "├ မြှောက်ချက် <code>+10000*5</code>\n"
            "├ မြှောက်+နှုန်း <code>+10000*5/7.1</code>\n"
            "├ အခကြေးနှုန်း <code>+1000*12%</code>\n"
            "├ USDT ဝင်ငွေ <code>+10000U</code>\n"
            "├ မှတ်ချက်ပါ <code>+1000 မှတ်ချက်</code>\n"
            "├ ထုတ်ပေးမှု <code>下发5000</code>\n"
            "├ ထုတ်×မြှောက် <code>下发5000*5</code>\n"
            "├ ထုတ်×မြှောက်+နှုန်း <code>下发5000*5/7.1</code>\n"
            "├ ထုတ်+နှုန်း <code>下发1000/7.8</code>\n"
            "├ USDT ထုတ် <code>下发5000U</code>\n"
            "├ ဘီလ်ကြည့် <code>+0</code>\n"
            "├ ကိုယ်တိုင်ကြည့် <code>账单</code> / <code>/me</code>\n"
            "├ စတင် <code>开始</code>（<code>上课</code>）\n"
            "└ ပိတ် <code>关闭</code>（<code>下课</code> / <code>拉停</code>）\n\n"
            "✏️ <b>ပြင်ဆင်</b>（မက်ဆေ့ချ်ကို reply）\n"
            "├ ပယ်ဖျက် <code>撤销</code>\n"
            "├ ဝင်ငွေပယ် <code>撤销入款</code>\n"
            "├ ဝင်ငွေ N ခုပယ် <code>撤销入款5条</code>\n"
            "├ ထုတ် N ခုပယ် <code>撤销下发5条</code>\n"
            "├ နောက်ဆုံးပယ် <code>撤销最后</code>\n"
            "├ ယနေ့ပယ် <code>撤销今天</code> / <code>撤销账单</code>\n"
            "├ အားလုံးပယ် <code>撤销全部</code>\n"
            "├ မှတ်ချက်ဝင်ငွေပယ် <code>撤销 张三</code>\n"
            "├ နှုန်းညှိ <code>修改汇款10</code>\n"
            "└ ရပ်တန့် <code>拉停</code>\n\n"
            "⚙️ <b>ဆettings</b>\n"
            "├ အခကြေးငွေ <code>设置费率10</code>\n"
            "├ ဝင်ငွေအခကြေး <code>设置入款费率10</code>\n"
            "├ ထုတ်အခကြေး <code>设置下发费率10</code>\n"
            "├ နှုန်းထား <code>设置汇率8</code>\n"
            "├ ဝင်ငွေနှုန်း <code>设置入款汇率8</code>\n"
            "├ ထုတ်နှုန်း <code>设置下发汇率8</code>\n"
            "├ Live နှုန်း <code>设置实时汇率</code> / <code>设置实时汇率-1</code>\n"
            "├ Operator <code>设置操作人@user</code>\n"
            "├ Operator ဖယ် <code>移除操作人@user</code>\n"
            "└ အားလုံး <code>设置所有人</code> / <code>取消所有人</code>\n\n"
            "🔍 <b>ရှာဖွေ</b>\n"
            "├ နှုန်း <code>汇率</code> · OKX <code>Z0</code> · Huobi <code>H0</code> · MYR <code>m0</code>\n"
            "├ MMK <code>mm0</code> · all <code>bma</code> · KBZ <code>bkb</code> · Mobile <code>bmm</code>\n"
            "├ OKX buy all <code>la</code> · bank <code>lk</code> · Alipay <code>lz</code> · WeChat <code>lw</code>\n"
            "├ OKX sell bank <code>lmk</code> · Alipay <code>lmz</code> · WeChat <code>lmw</code>\n"
            "├ ပြောင်း <code>查汇率100</code> · <code>币价100 汇率10</code>\n"
            "├ ဖုန်း <code>查询13800138000</code> သို့မဟုတ် 11 လုံး ပို့ပါ\n"
            "├ ဘဏ်ကတ် <code>查询6217001234567890</code>\n"
            "├ မှတ်ပုံ <code>查询110101199001011234</code>\n"
            "└ USDT TRC20 လိပ်စာ ပို့ပါ\n\n"
            "🎨 <b>Private VIP1:</b> bot အမည်/avatar · VIP2 · သက်တမ်းတိုး"
        )

    return (
        f"📖 <b>【{brand}】全功能指令说明</b>\n\n"
        f"🤖 欢迎使用 <b>{short}</b>，点击下方 <code>指令</code> 可复制。\n\n"
        "👑 <b>权限架构</b>\n"
        "最高级买家 · 二级权限人 · 群操作人\n\n"
        "💰 <b>记账操作</b>（点击可复制）\n"
        "├ 入款 <code>+10000</code>\n"
        "├ 入款+汇率 <code>+10000/7.1</code>\n"
        "├ 入款+倍数 <code>+10000*5</code>\n"
        "├ 入款+倍数+汇率 <code>+10000*5/7.1</code>\n"
        "├ 入款+费率 <code>+1000*12%</code>\n"
        "├ 入款U <code>+10000U</code>\n"
        "├ 入款备注 <code>+1000 备注内容</code>\n"
        "├ 下发 <code>下发5000</code>\n"
        "├ 下发+倍数 <code>下发5000*5</code>\n"
        "├ 下发+倍数+汇率 <code>下发5000*5/7.1</code>\n"
        "├ 下发+汇率 <code>下发1000/7.8</code>\n"
        "├ 下发U <code>下发5000U</code>\n"
        "├ 查账单 <code>+0</code>\n"
        "├ 群员自查 <code>账单</code> 或 <code>/我</code>\n"
        "├ 开始记账 <code>开始</code>（兼容 <code>上课</code>）\n"
        "└ 停止记账 <code>关闭</code>（兼容 <code>下课</code> / <code>拉停</code>）\n\n"
        "✏️ <b>修改操作</b>\n"
        "├ 撤销（回复消息）<code>撤销</code>\n"
        "├ 撤销入款（回复消息）<code>撤销入款</code>\n"
        "├ 撤销多条入款（回复消息）<code>撤销入款5条</code>\n"
        "├ 撤销多条下发（回复消息）<code>撤销下发5条</code>\n"
        "├ 撤销最后 <code>撤销最后</code>\n"
        "├ 撤销今天 <code>撤销今天</code> / <code>撤销账单</code>\n"
        "├ 撤销全部 <code>撤销全部</code>\n"
        "├ 撤销备注入款 <code>撤销 张三</code>\n"
        "├ 修改汇款 <code>修改汇款10</code>（同步更新账单汇率）\n"
        "└ 拉停 <code>拉停</code>\n\n"
        "⚙️ <b>设置操作</b>（需操作权限）\n"
        "├ 设置费率 <code>设置费率10</code>（支持负数%，默认入款）\n"
        "├ 设置入款费率 <code>设置入款费率10</code>\n"
        "├ 设置下发费率 <code>设置下发费率10</code>\n"
        "├ 设置汇率 <code>设置汇率8</code>（支持负数%，默认入款）\n"
        "├ 设置入款汇率 <code>设置入款汇率8</code>\n"
        "├ 设置下发汇率 <code>设置下发汇率8</code>\n"
        "├ 同步实时汇率 <code>设置实时汇率</code>\n"
        "├ 同步实时汇率偏移 <code>设置实时汇率-1</code>\n"
        "├ 设置代付价格 <code>设置代付价格10</code>\n"
        "├ 设置币种 <code>设置币种HKD</code>\n"
        "├ 下发模式 <code>设置下发人民币模式</code> / <code>设置下发币模式</code>\n"
        "├ 汇率模式 <code>开启乘汇率模式</code> / <code>关闭乘汇率模式</code>\n"
        "├ 人民币显示 <code>显示人民币</code> / <code>隐藏人民币</code>\n"
        "├ 显示条数 <code>显示条数10</code>\n"
        "├ 时间格式 <code>显示分秒</code> / <code>显示时分秒</code>\n"
        "├ 记账置顶 <code>开启记账置顶</code> / <code>关闭记账置顶</code>\n"
        "├ 置顶消息 <code>置顶</code> / <code>取消置顶</code>（回复消息）\n"
        "├ 日切 <code>设置日切04</code>（4 点换日归属）\n"
        "├ 全局日切 <code>设置全局日切04</code>\n"
        "├ 关闭日切 <code>关闭日切</code> / <code>关闭全局日切</code>\n"
        "├ 地址识别 <code>开启地址识别</code> / <code>关闭地址识别</code>\n"
        "├ 银行卡识别 <code>开启银行卡自动识别</code> / <code>关闭银行卡识别</code>\n"
        "├ 变更通知 <code>开启用户变更通知</code> / <code>关闭用户变更通知</code>\n"
        "├ 通知所有人 <code>通知所有人</code>\n"
        "├ 分类 <code>开启操作人分类</code> / <code>开启回复人分类</code> / <code>关闭分类</code>\n"
        "├ 催收 <code>开启催收</code> / <code>关闭催收</code> / <code>催收1</code>（分钟）\n"
        "├ 下发地址 <code>设置下发地址{地址}</code> / <code>删除下发地址{地址}</code>\n"
        "├ 增加记员 <code>设置操作人@xxxx</code>（或回复消息发设置操作人）\n"
        "├ 移除记员 <code>移除操作人@xxxx</code>\n"
        "└ 全部记员 <code>设置所有人</code> / <code>取消所有人</code>\n\n"
        "🔍 <b>查询操作</b>\n"
        "├ 查汇率 <code>汇率</code>\n"
        "├ 欧易汇率 <code>Z0</code>\n"
        "├ 火币汇率 <code>H0</code>\n"
        "├ 马币汇率 <code>m0</code>\n"
        "├ 缅币快查 <code>mm0</code>（Bybit P2P）\n"
        "├ 缅币全部买价 <code>bma</code>（KBZ + Mobile Banking）\n"
        "├ KBZ 买U Top10 <code>bkb</code>\n"
        "├ Mobile Banking 买U Top10 <code>bmm</code>\n"
        "├ U换KBZ Top10 <code>skb</code>\n"
        "├ U换Bank Transfer Top10 <code>smm</code>\n"
        "├ 欧易全部买价 <code>la</code>\n"
        "├ 欧易银行卡买价 <code>lk</code> · 支付宝买价 <code>lz</code> · 微信买价 <code>lw</code>\n"
        "├ 欧易银行卡卖价 <code>lmk</code> · 支付宝卖价 <code>lmz</code> · 微信卖价 <code>lmw</code>\n"
        "├ 查汇率换算 <code>查汇率100</code>\n"
        "├ 点位计算 <code>币价100 汇率10</code>\n"
        "├ 查手机号 <code>查询13800138000</code> — 运营商/归属地\n"
        "├ 查银行卡 <code>查询6217001234567890</code> — 发卡行/卡类型\n"
        "├ 查身份证 <code>查询110101199001011234</code> — 归属地/生日/性别\n"
        "└ 查地址 发送 USDT TRC20 地址，或 <code>查看 T开头34位地址</code>\n\n"
        "🎨 <b>私聊菜单（VIP1）：</b>改名字 · 改头像 · 设权限人 · 续费"
    )


def get_setting(group_id, key):
    cols = [
        "group_id", "operators", "exchange_rate", "fee_rate", "is_active",
        "language", "timezone", "show_usdt", "expire_time", "extra_settings",
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
                "(group_id, operators, exchange_rate, fee_rate, is_active, language, "
                "timezone, show_usdt, expire_time, extra_settings) "
                "VALUES (?, '[]', 7.2, 0, 1, 'zh', 'Asia/Shanghai', 1, ?, '{}')",
                (group_id, init_time),
            )
            conn.commit()
            c.execute("SELECT * FROM settings WHERE group_id = ?", (group_id,))
            row = c.fetchone()
        conn.close()
        if not row:
            return None
        # 兼容旧库列数不足时仍可读前几列
        mapped = dict(zip(cols, row[: len(cols)]))
        return mapped.get(key)
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


_CALC_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _normalize_calc_expression(text):
    t = normalize_billing_text(text).strip()
    for src, dst in (
        ("×", "*"), ("✕", "*"), ("⨉", "*"), ("·", "*"),
        ("÷", "/"), ("／", "/"), ("＊", "*"),
        ("x", "*"), ("X", "*"),
    ):
        t = t.replace(src, dst)
    return re.sub(r"\s+", "", t)


def _safe_eval_calc(expr):
    def _eval_node(node):
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("unsupported constant")
        num_type = getattr(ast, "Num", None)
        if num_type is not None and isinstance(node, num_type):
            return node.n
        if isinstance(node, ast.UnaryOp):
            op = _CALC_BIN_OPS.get(type(node.op))
            if op is None:
                raise ValueError("unsupported unary op")
            return op(_eval_node(node.operand))
        if isinstance(node, ast.BinOp):
            op = _CALC_BIN_OPS.get(type(node.op))
            if op is None:
                raise ValueError("unsupported binary op")
            return op(_eval_node(node.left), _eval_node(node.right))
        raise ValueError("unsupported expression")

    tree = ast.parse(expr, mode="eval")
    return _eval_node(tree)


def _format_calc_result(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        text = f"{value:.8f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def is_pure_arithmetic_expression(text):
    """纯数字算式（如 500+300、5000/5），不是 +100 这类记账指令。"""
    raw = (text or "").strip()
    if not raw or len(raw) > 80:
        return False
    if re.search(r"[\u4e00-\u9fff]", raw):
        return False
    if re.match(
        r"^(设置|查询|删除|开启|关闭|改语言|通知|上课|下课|开始|拉停|撤销|查汇率|币价|汇率$|/|@)",
        raw,
    ):
        return False
    if re.match(r"^[a-zA-Z]", raw):
        return False
    # +100、+0、-50 等是记账，不是计算器
    if re.match(r"^[+\-]\d", raw):
        return False
    expr = _normalize_calc_expression(raw)
    if not expr or not re.search(r"[\+\-\*/]", expr):
        return False
    if not re.fullmatch(r"[\d+\-*/().]+", expr):
        return False
    if len(re.findall(r"\d+\.?\d*", expr)) < 2:
        return False
    if expr.lstrip("+-").isdigit() and len(re.sub(r"\D", "", expr)) >= 11:
        return False
    return True


def try_reply_calculator(message, text):
    if not is_pure_arithmetic_expression(text):
        return False
    expr = _normalize_calc_expression(text)
    show_expr = (text or "").strip()
    try:
        result = _safe_eval_calc(expr)
    except Exception as exc:
        log.warning("calculator failed expr=%r: %s", expr, exc)
        bot.reply_to(message, f"🧮 算式无法计算：<code>{_html_esc(show_expr)}</code>")
        return True
    bot.reply_to(
        message,
        f"🧮 机器人计算 = <b>{_format_calc_result(result)}</b>\n"
        f"<code>{_html_esc(show_expr)}</code>",
        parse_mode="HTML",
    )
    return True


def looks_like_billing_command(text, group_id):
    text = normalize_billing_text(text)
    if text == cmd(group_id, "bill_zero"):
        return True
    if match_class_start(text, group_id) or match_class_end(text, group_id):
        return True
    if parse_income_command(text, group_id):
        return True
    if parse_expense_command(text, group_id):
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
        "income_header": "<b>入款（{n}笔）</b>",
        "no_income": "暂无入款",
        "category_header": "<b>入款备注分类</b>",
        "no_remark": "无备注",
        "no_category": "暂无分类",
        "expense_header": "<b>下发（{n}笔）</b>",
        "no_expense": "暂无下发",
        "expense_word": "下发",
        "total_income": "<b>总入款:</b> {amount}",
        "fee_rate_label": "<b>费率:</b> {rate}%",
        "exchange_rate_label": "<b>汇率:</b> {rate}",
        "income_rate_label": "<b>入款汇率:</b> {rate}",
        "expense_rate_label": "<b>下发汇率:</b> {rate}",
        "should_issue": "应下发: {amount} U",
        "issued": "已下发: {amount} U",
        "not_issued": "未下发: {amount} U",
        "audit_id": "[核算编号: {code}]",
        "show_more": "🌍 show more",
        "web_bill": "🌍 查看完整网页账单",
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
        "delete_remark_ok": "🗑️ 已撤销今日备注【{remark}】共 {n} 笔进单。",
        "delete_remark_none": "🔍 今日无备注【{remark}】的进单。",
        "view_remark_none": "🔍 今日无备注【{remark}】的进单。",
        "view_remark_title": "📋 <b>{remark}进单明细</b>",
        "view_remark_total": "合计 {rmb} RMB / {usdt} USDT",
        "bill_fail": "❌ 记账失败: {err}",
        "lang_label": "中文",
        "lang_active_hint": (
            "记账仍处于开启状态，可继续使用 <code>+0</code> 查账、<code>+金额</code> 入款等指令。"
            "开关记账：<code>上课</code> / <code>开始</code> / <code>start</code>。"
        ),
    },
    "eng": {
        "lang_picker": "🌐 Choose group language:",
        "lang_changed": "✅ Language updated: <b>{label}</b>",
        "lang_active_hint": (
            "Bookkeeping is still ON. Use <code>+0</code> for summary, <code>+amount</code> for deposits. "
            "Toggle: <code>start</code> / <code>上课</code> / <code>开始</code>."
        ),
        "welcome_thanks": "Thank you for adding me to your group!",
        "welcome_i_am": "I am {name} 🤖",
        "welcome_start": "Send <code>上课</code> to start, set fee rate (e.g. <code>设置费率 5</code>), then begin bookkeeping.",
        "bill_summary": "📊 <b>Summary ({date})</b>",
        "income_header": "<b>Deposits ({n})</b>",
        "no_income": "No deposits yet",
        "category_header": "<b>Remark categories</b>",
        "no_remark": "No remark",
        "no_category": "No categories",
        "expense_header": "<b>Payouts ({n})</b>",
        "no_expense": "No payouts yet",
        "expense_word": "Payout ",
        "total_income": "<b>Total deposit:</b> {amount}",
        "fee_rate_label": "<b>Fee:</b> {rate}%",
        "exchange_rate_label": "<b>Rate:</b> {rate}",
        "income_rate_label": "<b>Deposit rate:</b> {rate}",
        "expense_rate_label": "<b>Payout rate:</b> {rate}",
        "should_issue": "To issue: {amount} U",
        "issued": "Issued: {amount} U",
        "not_issued": "Remaining: {amount} U",
        "audit_id": "[Ref: {code}]",
        "show_more": "🌍 show more",
        "web_bill": "🌍 Full web report",
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
        "income_header": "<b>ဝင်ငွေ ({n} ခု)</b>",
        "no_income": "ဝင်ငွေမရှိ",
        "category_header": "<b>မှတ်ချက်အမျိုးအစား</b>",
        "no_remark": "မှတ်ချက်မရှိ",
        "no_category": "အမျိုးအစားမရှိ",
        "expense_header": "<b>ထုတ်ပေးမှု ({n} ခု)</b>",
        "no_expense": "ထုတ်ပေးမှုမရှိ",
        "expense_word": "ထုတ်",
        "total_income": "<b>စုဝင်ငွေ:</b> {amount}",
        "fee_rate_label": "<b>အခကြေးငွေ:</b> {rate}%",
        "exchange_rate_label": "<b>နှုန်းထား:</b> {rate}",
        "income_rate_label": "<b>ဝင်ငွေနှုန်း:</b> {rate}",
        "expense_rate_label": "<b>ထုတ်နှုန်း:</b> {rate}",
        "should_issue": "ထုတ်ပေးရန်: {amount} U",
        "issued": "ထုတ်ပေးပြီး: {amount} U",
        "not_issued": "မထုတ်ရသေး: {amount} U",
        "audit_id": "[စာရင်းနံပါတ်: {code}]",
        "show_more": "🌍 show more",
        "web_bill": "🌍 ဝဘ်ဘီလ်ကြည့်ရန်",
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
        "lang_active_hint": (
            "မှတ်တမ်းတင်ခြင်း ဆက်ဖွင့်ထားသည်။ <code>+0</code> ဖြင့် ကြည့်နိုင်သည်။ "
            "<code>上课</code> / <code>start</code> ဖြင့် ဖွင့်/ပိတ်နိုင်သည်。"
        ),
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
        "remove_operator3": "移除操作人",
        "delete_last": "撤销最后",
        "delete_today": "撤销今天",
        "delete_all": "撤销全部",
        "delete_remark": "撤销",
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


def cmd_variants(key):
    """某命令在所有语言下的别名（改语言后仍全部有效）。"""
    seen = set()
    for lang in SUPPORTED_LANGS:
        val = CMD.get(lang, {}).get(key)
        if val:
            seen.add(val)
    for val in DELETE_CMD_LEGACY_ALIASES.get(key, ()):
        seen.add(val)
    return seen


DELETE_CMD_LEGACY_ALIASES = {
    "delete_last": ("删最后",),
    "delete_today": ("删今天", "删除账单", "撤销账单"),
    "delete_all": ("删全部",),
    "delete_remark": ("删",),
}


def parse_remark_delete_command(text):
    """按备注撤销今日入款：撤销 张三 / 删 张三 / 删张三（兼容）。"""
    t = (text or "").strip()
    if not t:
        return None
    for pattern in (
        r"^撤销\s+(.+)$",
        r"^删\s+(.+)$",
        r"^撤销备注\s*(.+)$",
        r"^删备注\s*(.+)$",
    ):
        m = re.match(pattern, t)
        if not m:
            continue
        remark = m.group(1).strip()
        if remark and not re.match(r"^(入款|下发)", remark):
            return remark
    rest = strip_cmd_prefix_any(t, "delete_remark")
    if rest and not re.match(r"^(入款|下发)", rest):
        blocked = cmd_variants("delete_last") | cmd_variants("delete_today") | cmd_variants("delete_all")
        if t not in blocked and rest not in ("最后", "今天", "全部", "账单"):
            return rest
    return None


def is_group_active(group_id):
    try:
        return int(get_setting(group_id, "is_active") or 0) == 1
    except (TypeError, ValueError):
        return False


def strip_cmd_prefix_any(text, key):
    raw = (text or "").strip()
    for prefix in sorted(cmd_variants(key), key=len, reverse=True):
        rest = strip_cmd_prefix(raw, prefix)
        if rest is not None:
            return rest
    return None


def find_cmd_prefix(text, key):
    raw = (text or "").strip()
    for prefix in sorted(cmd_variants(key), key=len, reverse=True):
        if strip_cmd_prefix(raw, prefix) is not None:
            return prefix
    return None


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
    return (text or "").strip() in cmd_variants(key)


def build_welcome_start(group_id):
    c = CMD[get_group_lang(group_id)]
    lang = get_group_lang(group_id)
    if lang == "eng":
        return (
            f"Send <code>{c['class_start']}</code> / <code>上课</code> / <code>开始</code> to enable bookkeeping, "
            f"set fee (e.g. <code>{c['set_fee']} 5</code> or <code>设置费率 5</code>), then you can start."
        )
    if lang == "my":
        return (
            f"<code>{c['class_start']}</code> / <code>上课</code> / <code>开始</code> ပို့၍ စတင်ပါ၊ "
            f"<code>{c['set_fee']} 5</code> / <code>设置费率 5</code> ဖြင့် အခကြေးငွေသတ်မှတ်ပါ。"
        )
    return (
        f"请发送 <code>{c['class_start']}</code> / <code>开始</code> / <code>start</code> 唤醒我，"
        f"并设置费率（如 <code>{c['set_fee']} 5</code> / <code>设置费率 5</code>），然后即可开始记账。"
    )


def build_need_class_start(group_id):
    c = CMD[get_group_lang(group_id)]
    lang = get_group_lang(group_id)
    if lang == "eng":
        return (
            f"⚠️ Send <code>{c['class_start']}</code> / <code>上课</code> / <code>开始</code> "
            f"first to enable bookkeeping."
        )
    if lang == "my":
        return (
            f"⚠️ ဦးစွာ <code>{c['class_start']}</code> / <code>上课</code> / <code>开始</code> ပို့ပါ。"
        )
    return (
        f"⚠️ 请先发送 <code>{c['class_start']}</code> / <code>开始</code> / <code>上课</code> / <code>start</code> 开启记账。"
    )


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


WEB_TEXTS = {
    "zh": {
        "page_title": "网页账单",
        "dashboard_title": "对账看板",
        "group_id": "群组 ID: {id}",
        "loading": "加载中...",
        "summary_all": "查看全部历史",
        "summary_day": "当前日期 {date}（北京时间）",
        "income_unit": "入款",
        "expense_unit": "下发",
        "count_unit": "笔",
        "btn_prev": "◀ 跳前",
        "btn_next": "跳后 ▶",
        "btn_all": "全部历史",
        "date_label": "账单日期：",
        "income_title": "入款（{n}笔）",
        "expense_title": "下发（{n}笔）",
        "category_title": "备注分类",
        "th_date": "日期",
        "th_time": "时间",
        "th_remark": "备注",
        "th_rmb": "RMB",
        "th_u": "U",
        "th_usdt": "USDT",
        "th_operator": "操作人",
        "th_count": "笔数",
        "card_rate": "汇率",
        "card_total_rmb": "总入款 RMB",
        "card_total_usdt": "总入款 USDT",
        "card_expense_usdt": "已下发 USDT",
        "card_remaining_usdt": "未下发 USDT",
        "no_data": "暂无",
        "no_remark": "无备注",
        "no_income": "暂无入款",
        "no_expense": "暂无下发",
    },
    "eng": {
        "page_title": "Web Bill",
        "dashboard_title": "Reconciliation",
        "group_id": "Group ID: {id}",
        "loading": "Loading...",
        "summary_all": "All history",
        "summary_day": "Date {date} (Beijing time)",
        "income_unit": "Deposits",
        "expense_unit": "Payouts",
        "count_unit": "items",
        "btn_prev": "◀ Prev",
        "btn_next": "Next ▶",
        "btn_all": "All history",
        "date_label": "Bill date:",
        "income_title": "Deposits ({n})",
        "expense_title": "Payouts ({n})",
        "category_title": "Remark categories",
        "th_date": "Date",
        "th_time": "Time",
        "th_remark": "Remark",
        "th_rmb": "RMB",
        "th_u": "U",
        "th_usdt": "USDT",
        "th_operator": "Operator",
        "th_count": "Count",
        "card_rate": "Rate",
        "card_total_rmb": "Total RMB",
        "card_total_usdt": "Total USDT",
        "card_expense_usdt": "Issued USDT",
        "card_remaining_usdt": "Remaining USDT",
        "no_data": "None",
        "no_remark": "No remark",
        "no_income": "No deposits",
        "no_expense": "No payouts",
    },
    "my": {
        "page_title": "ဝဘ်ဘီလ်",
        "dashboard_title": "စာရင်းညှိ",
        "group_id": "အုပ်စု ID: {id}",
        "loading": "တင်နေသည်...",
        "summary_all": "မှတ်တမ်းအားလုံး",
        "summary_day": "ရက်စွဲ {date}（Beijing）",
        "income_unit": "ဝင်ငွေ",
        "expense_unit": "ထုတ်ပေးမှု",
        "count_unit": "ခု",
        "btn_prev": "◀ ရှေ့",
        "btn_next": "နောက် ▶",
        "btn_all": "အားလုံး",
        "date_label": "ဘီလ်ရက်：",
        "income_title": "ဝင်ငွေ ({n} ခု)",
        "expense_title": "ထုတ်ပေးမှု ({n} ခု)",
        "category_title": "မှတ်ချက်အမျိုးအစား",
        "th_date": "ရက်",
        "th_time": "အချိန်",
        "th_remark": "မှတ်ချက်",
        "th_rmb": "RMB",
        "th_u": "U",
        "th_usdt": "USDT",
        "th_operator": "Operator",
        "th_count": "အရေအတွက်",
        "card_rate": "နှုန်းထား",
        "card_total_rmb": "စုဝင်ငွေ RMB",
        "card_total_usdt": "စုဝင်ငွေ USDT",
        "card_expense_usdt": "ထုတ်ပေးပြီး USDT",
        "card_remaining_usdt": "မထုတ်ရသေး USDT",
        "no_data": "မရှိ",
        "no_remark": "မှတ်ချက်မရှိ",
        "no_income": "ဝင်ငွေမရှိ",
        "no_expense": "ထုတ်ပေးမှုမရှိ",
    },
}


def web_tr(lang, key, **kwargs):
    lang = normalize_lang_code(lang)
    template = WEB_TEXTS.get(lang, WEB_TEXTS["zh"]).get(key) or WEB_TEXTS["zh"].get(key, key)
    return template.format(**kwargs) if kwargs else template


def resolve_web_lang(group_id, lang_param=None):
    if lang_param:
        code = normalize_lang_code(lang_param)
        if code in SUPPORTED_LANGS:
            return code
    if group_id:
        return get_group_lang(group_id)
    return "zh"

def tr(group_id, key, **kwargs):
    lang = get_group_lang(group_id)
    template = TEXTS.get(lang, TEXTS["zh"]).get(key) or TEXTS["zh"].get(key, key)
    return template.format(**kwargs) if kwargs else template


def is_language_change_trigger(text, group_id):
    raw = (text or "").strip()
    if raw in cmd_variants("lang_change") or raw == "改语言":
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
    if is_group_active(group_id):
        bot.send_message(chat_id, tr(group_id, "lang_active_hint"), parse_mode="HTML")
    else:
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
    extra = get_extra_settings(group_id)
    if extra.get("all_operators"):
        return True
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
# Extended settings & advanced billing
# ---------------------------------------------------------------------------
def get_extra_settings(group_id):
    raw = get_setting(group_id, "extra_settings") or "{}"
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    merged = dict(DEFAULT_EXTRA_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_EXTRA_SETTINGS})
    return merged


def save_extra_settings(group_id, data):
    merged = dict(DEFAULT_EXTRA_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_EXTRA_SETTINGS})
    update_setting(group_id, "extra_settings", json.dumps(merged, ensure_ascii=False))


def apply_expense_exchange_rate(group_id, rate):
    """原子写入：下发汇率 + 人民币下发模式。"""
    data = get_extra_settings(group_id)
    data["expense_exchange_rate"] = float(rate)
    data["expense_mode"] = "rmb"
    save_extra_settings(group_id, data)


def expense_amount_is_rmb(extra, inline_rate, is_usdt_input):
    """下发金额是否按人民币除以汇率换算成 U。"""
    if is_usdt_input:
        return False
    if inline_rate is not None:
        return True
    return (extra.get("expense_mode") or "usdt") == "rmb"


def update_extra_setting(group_id, key, value):
    if key not in DEFAULT_EXTRA_SETTINGS:
        return
    data = get_extra_settings(group_id)
    data[key] = value
    save_extra_settings(group_id, data)


def get_effective_rate(group_id, bill_type="income"):
    extra = get_extra_settings(group_id)
    if bill_type == "income":
        rate = extra.get("income_exchange_rate")
    else:
        rate = extra.get("expense_exchange_rate")
    if rate is not None:
        return float(rate)
    if extra.get("use_realtime_rate"):
        base = fetch_okx_usdt_cny_rate()
        if base:
            return max(base + float(extra.get("realtime_rate_offset") or 0), 0.01)
    return float(get_setting(group_id, "exchange_rate") or 7.2)


def get_effective_fee(group_id, bill_type="income", override_pct=None):
    if override_pct is not None:
        return float(override_pct) / 100.0
    extra = get_extra_settings(group_id)
    if bill_type == "income":
        fee = extra.get("income_fee_rate")
    else:
        fee = extra.get("expense_fee_rate")
    if fee is None:
        fee = get_setting(group_id, "fee_rate") or 0.0
    return float(fee)


def get_billing_date_str(group_id):
    extra = get_extra_settings(group_id)
    cut = extra.get("day_cut_hour")
    if cut is None:
        cut = extra.get("global_day_cut_hour")
    tz = get_setting(group_id, "timezone") or "Asia/Shanghai"
    now, _, full_time = get_current_time(tz)
    if cut is not None:
        try:
            cut_h = int(cut)
            if now.hour < cut_h:
                now = now - timedelta(days=1)
        except (TypeError, ValueError):
            pass
    return now.strftime("%Y-%m-%d"), full_time


def convert_rmb_to_usdt(rmb, rate, fee_rate=0.0, multiply_mode=False):
    net_rmb = rmb * (1 - fee_rate)
    if multiply_mode:
        return net_rmb * rate
    if rate <= 0:
        rate = 7.2
    return net_rmb / rate


def convert_usdt_to_rmb(usdt, rate, multiply_mode=False):
    if multiply_mode:
        return usdt / rate if rate else usdt * 7.2
    return usdt * rate


def parse_star_modifier(text):
    mult = 1.0
    fee_pct = None
    for m in re.finditer(r"\*(\d+(?:\.\d+)?)(%?)", text):
        val = float(m.group(1))
        if m.group(2) == "%":
            fee_pct = val
        else:
            mult *= val
    return mult, fee_pct


def parse_income_command(text, group_id):
    text = normalize_billing_text(text)
    if is_pure_arithmetic_expression(text):
        return None
    if text in ("+0", cmd(group_id, "bill_zero")):
        return {"kind": "bill_zero"}
    m = re.match(
        r"^(?P<prefix>.*?)(?P<sign>[\+\-])(?P<num>\d+(?:\.\d+)?)"
        r"(?P<usdt>[Uu])?"
        r"(?P<tail>.*)$",
        text,
    )
    if not m:
        return None
    prefix = m.group("prefix").strip()
    sign = m.group("sign")
    base_amount = float(m.group("num"))
    is_usdt = bool(m.group("usdt"))
    tail = (m.group("tail") or "").strip()
    mult, inline_fee = parse_star_modifier(tail)
    tail_clean = re.sub(r"\*(\d+(?:\.\d+)?%?)", "", tail).strip()
    rate = None
    rate_m = re.search(r"/(\d+(?:\.\d+)?)", tail_clean)
    suffix_remark = ""
    if rate_m:
        rate = float(rate_m.group(1))
        suffix_remark = tail_clean[rate_m.end():].strip()
    else:
        suffix_remark = tail_clean.strip()
    remark = prefix or suffix_remark
    amount = base_amount * mult
    if sign == "-":
        amount = -amount
    return {
        "kind": "income",
        "remark": remark,
        "amount": amount,
        "is_usdt": is_usdt,
        "rate": rate,
        "fee_pct": inline_fee,
    }


def parse_expense_command(text, group_id):
    text = normalize_billing_text(text)
    for word in sorted(cmd_variants("expense"), key=len, reverse=True):
        m = re.match(
            rf"^(?P<prefix>.*?)(?:{re.escape(word)})(?P<num>\d+(?:\.\d+)?)"
            r"(?P<usdt>[Uu])?"
            r"(?P<tail>.*)$",
            text,
        )
        if not m:
            continue
        prefix = m.group("prefix").strip()
        base_amount = float(m.group("num"))
        is_usdt = bool(m.group("usdt"))
        tail = (m.group("tail") or "").strip()
        mult, _ = parse_star_modifier(tail)
        tail_clean = re.sub(r"\*(\d+(?:\.\d+)?%?)", "", tail).strip()
        rate = None
        rate_m = re.search(r"/(\d+(?:\.\d+)?)", tail_clean)
        if rate_m:
            rate = float(rate_m.group(1))
        amount = base_amount * mult
        return {
            "kind": "expense",
            "remark": prefix,
            "amount": amount,
            "is_usdt": is_usdt,
            "rate": rate,
        }
    return None


def match_class_start(text, group_id):
    t = (text or "").strip()
    extras = ("开始", "上课", "start", "Start", "START")
    if t in extras:
        return True
    return t in cmd_variants("class_start")


def match_class_end(text, group_id):
    t = (text or "").strip()
    extras = ("关闭", "下课", "拉停", "stop", "Stop", "STOP")
    if t in extras:
        return True
    return t in cmd_variants("class_end")


def format_rate_reply(label, rate, note=""):
    if rate is None:
        return f"⚠️ 暂时无法获取{label}汇率"
    extra = f" ({note})" if note else ""
    return f"💱 <b>{label}</b>{extra}：<code>{rate:.4f}</code>"


def _p2p_http_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }


_okx_c2c_cache = {}
OKX_C2C_CACHE_TTL = 60
OKX_C2C_PAYMENTS = (
    ("bank", "银行卡"),
    ("aliPay", "支付宝"),
    ("wxPay", "微信"),
)
OKX_C2C_QUERY_COMMANDS = {
    "la": ("buy", None),
    "lk": ("buy", "bank"),
    "lz": ("buy", "aliPay"),
    "lw": ("buy", "wxPay"),
    "lmk": ("sell", "bank"),
    "lmz": ("sell", "aliPay"),
    "lmw": ("sell", "wxPay"),
}
OKX_C2C_COMMAND_HELP = {
    "la": "所有类型买价",
    "lk": "银行卡买价",
    "lz": "支付宝买价",
    "lw": "微信买价",
    "lmk": "银行卡卖价",
    "lmz": "支付宝卖价",
    "lmw": "微信卖价",
}


def fetch_okx_c2c_orders(payment_method, trade_side, limit=10):
    """trade_side: buy=买价(用户买U), sell=卖价(用户卖U)。"""
    okx_side = "sell" if trade_side == "buy" else "buy"
    cache_key = (payment_method, okx_side, limit)
    cached = _okx_c2c_cache.get(cache_key)
    now = datetime.now().timestamp()
    if cached and (now - cached[1]) < OKX_C2C_CACHE_TTL:
        return cached[0]

    try:
        resp = requests.get(
            "https://www.okx.com/v3/c2c/tradingOrders/books",
            params={
                "quoteCurrency": "CNY",
                "baseCurrency": "USDT",
                "side": okx_side,
                "paymentMethod": payment_method,
            },
            timeout=10,
            headers=_p2p_http_headers(),
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        if body.get("code") not in (None, 0, "0"):
            return None
        rows = body.get("data", {}).get(okx_side, [])[:limit]
        orders = [
            {
                "price": float(row["price"]),
                "nickName": (row.get("nickName") or "").strip() or "—",
            }
            for row in rows
        ]
        _okx_c2c_cache[cache_key] = (orders, now)
        return orders
    except Exception as exc:
        log.warning("OKX C2C %s/%s fetch failed: %s", payment_method, trade_side, exc)
    return None


def build_query_command_footer(command_help, current_cmd):
    """查询结果底部附其他同类命令说明（不含当前已用的命令）。"""
    current = (current_cmd or "").strip().lower()
    lines = []
    for cmd, desc in command_help.items():
        if cmd.lower() == current:
            continue
        lines.append(f"{cmd} {desc}")
    if not lines:
        return ""
    return "\n\n命令：\n" + "\n".join(lines)


def append_query_command_footer(body, command_help, current_cmd):
    footer = build_query_command_footer(command_help, current_cmd)
    return (body or "") + footer


def format_okx_c2c_section(payment_label, trade_side, orders):
    action = "购买" if trade_side == "buy" else "卖出"
    prefix = "买" if trade_side == "buy" else "卖"
    if not orders:
        return f"⚠️ 暂时无法获取欧易-{payment_label}USDT{action}价"
    lines = [f"当前设置欧易-{payment_label}USDT{action}价"]
    for idx, row in enumerate(orders, 1):
        lines.append(f"{prefix}{idx}：{row['price']:.2f}   {row['nickName']}")
    return "\n".join(lines)


def build_okx_c2c_reply(command):
    cmd = (command or "").lower()
    spec = OKX_C2C_QUERY_COMMANDS.get(cmd)
    if not spec:
        return None
    trade_side, payment = spec
    if payment is None:
        sections = []
        for pm_key, pm_label in OKX_C2C_PAYMENTS:
            orders = fetch_okx_c2c_orders(pm_key, trade_side)
            sections.append(format_okx_c2c_section(pm_label, trade_side, orders))
        body = "\n\n".join(sections)
    else:
        pm_label = dict(OKX_C2C_PAYMENTS).get(payment, payment)
        orders = fetch_okx_c2c_orders(payment, trade_side)
        body = format_okx_c2c_section(pm_label, trade_side, orders)
    return append_query_command_footer(body, OKX_C2C_COMMAND_HELP, cmd)


def fetch_binance_p2p_rate(fiat, trade_type="SELL", pay_types=None):
    """Binance C2C 报价；SELL=用户买 USDT。"""
    orders = fetch_binance_p2p_orders(fiat, pay_types or [], trade_type, limit=1)
    if orders:
        return orders[0]["price"]
    return None


_binance_p2p_cache = {}
BINANCE_P2P_CACHE_TTL = 60
BYBIT_OTC_URL = "https://api2.bybit.com/fiat/otc/item/online"
_mmk_p2p_cache = {}
MMK_P2P_CACHE_TTL = 60
# Bybit MMK 支付方式 ID（side=1 表示商家卖 USDT，用户用缅币买 U）
BYBIT_MMK_PAYMENTS = (
    (["601"], "KBZPay"),
    (["602", "605"], "Mobile Banking"),
)
MMK_QUERY_COMMANDS = {
    "bma": None,
    "bkb": "KBZPay",
    "bmm": "Mobile Banking",
}
MMK_SELL_QUERY_COMMANDS = {
    "skb": "KBZPay",
    "smm": "Mobile Banking",
}
MMK_COMMAND_HELP = {
    "mm0": "缅币买U快查",
    "bma": "全部渠道买U Top10",
    "bkb": "KBZ买U Top10",
    "bmm": "Mobile Banking买U Top10",
    "skb": "U换KBZ Top10",
    "smm": "U换Bank Transfer Top10",
}
OTC_COMMAND_HELP = {
    "Z0": "欧易 OTC 汇率",
    "H0": "火币 OTC 汇率",
    "m0": "马币 OTC 汇率",
    "mm0": "缅币 P2P 快查",
}
MMK_QUERY_PAYMENT_IDS = {
    "KBZPay": ["601"],
    "Mobile Banking": ["602", "605"],
}


def _binance_p2p_headers(fiat="MMK"):
    headers = {
        **_p2p_http_headers(),
        "Content-Type": "application/json",
        "clienttype": "web",
        "lang": "en",
    }
    if fiat == "MMK":
        headers["bnc-location"] = "MM"
    return headers


def fetch_binance_p2p_orders(fiat, pay_types, trade_type="SELL", limit=10):
    pay_types = list(pay_types or [])
    cache_key = (fiat, tuple(pay_types), trade_type, limit)
    cached = _binance_p2p_cache.get(cache_key)
    now = datetime.now().timestamp()
    if cached and (now - cached[1]) < BINANCE_P2P_CACHE_TTL:
        return cached[0]

    payload = {
        "asset": "USDT",
        "fiat": fiat,
        "merchantCheck": False,
        "page": 1,
        "payTypes": pay_types,
        "publisherType": None,
        "rows": limit,
        "tradeType": trade_type,
        "countries": [],
        "proMerchantAds": False,
        "shieldMerchantAds": False,
        "filterType": "all",
        "periods": [],
        "additionalKycVerifyFilter": 0,
        "classifies": ["mass", "profession", "fiat_trade"],
    }
    try:
        resp = requests.post(
            "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
            json=payload,
            timeout=12,
            headers=_binance_p2p_headers(fiat),
        )
        if resp.status_code != 200:
            return None
        rows = resp.json().get("data") or []
        orders = []
        for item in rows[:limit]:
            adv = item.get("adv") or {}
            advertiser = item.get("advertiser") or {}
            price = adv.get("price")
            if price is None:
                continue
            orders.append({
                "price": float(price),
                "nickName": (advertiser.get("nickName") or "").strip() or "—",
            })
        _binance_p2p_cache[cache_key] = (orders, now)
        return orders
    except Exception as exc:
        log.warning("Binance P2P orders %s/%s failed: %s", fiat, pay_types, exc)
    return None


def _bybit_p2p_headers():
    return {
        **_p2p_http_headers(),
        "Content-Type": "application/json",
        "lang": "en-US",
        "platform": "PC",
    }


def fetch_bybit_p2p_orders(currency, payment_ids, side="1", limit=10):
    """Bybit OTC；side=1 商家卖 USDT（用户买 U）。"""
    payment_ids = [str(x) for x in (payment_ids or [])]
    cache_key = ("bybit", currency, tuple(payment_ids), side, limit)
    cached = _mmk_p2p_cache.get(cache_key)
    now = datetime.now().timestamp()
    if cached and (now - cached[1]) < MMK_P2P_CACHE_TTL:
        return cached[0]

    payload = {
        "tokenId": "USDT",
        "currencyId": currency,
        "side": str(side),
        "size": str(limit),
        "page": "1",
        "amount": "",
        "payment": payment_ids,
    }
    try:
        resp = requests.post(
            BYBIT_OTC_URL,
            json=payload,
            timeout=12,
            headers=_bybit_p2p_headers(),
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        if body.get("ret_code") not in (0, "0", None):
            log.warning("Bybit OTC %s failed: %s", payment_ids, body.get("ret_msg"))
            return None
        items = (body.get("result") or {}).get("items") or []
        orders = []
        for item in items[:limit]:
            if item.get("price") is None:
                continue
            orders.append({
                "price": float(item["price"]),
                "nickName": (item.get("nickName") or "").strip() or "—",
                "minAmount": item.get("minAmount"),
                "maxAmount": item.get("maxAmount"),
                "lastQuantity": item.get("lastQuantity"),
            })
        _mmk_p2p_cache[cache_key] = (orders, now)
        return orders
    except Exception as exc:
        log.warning("Bybit P2P orders %s failed: %s", payment_ids, exc)
    return None


def fetch_mmk_p2p_orders(payment_ids, limit=10, side="1"):
    """缅币 P2P：优先 Bybit，备用 Binance。side=1 买U，side=0 卖U。"""
    orders = fetch_bybit_p2p_orders("MMK", payment_ids, str(side), limit)
    if orders is not None:
        return orders, "Bybit"
    binance_map = {
        "601": "KBZPay1",
        "602": "WavePay1",
        "605": "WaveMobile",
    }
    if payment_ids and len(payment_ids) == 1:
        binance_pay = [binance_map.get(payment_ids[0], payment_ids[0])]
    elif payment_ids:
        binance_pay = [binance_map.get(p, p) for p in payment_ids]
    else:
        binance_pay = []
    trade_type = "SELL" if str(side) == "1" else "BUY"
    orders = fetch_binance_p2p_orders("MMK", binance_pay, trade_type, limit)
    if orders is not None:
        return orders, "Binance"
    return None, "Bybit"


def _format_mmk_limit(min_amount, max_amount):
    try:
        min_val = float(min_amount)
        max_val = float(max_amount)
        return f"限额 {min_val:,.0f}-{max_val:,.0f} MMK"
    except (TypeError, ValueError):
        return ""


def format_mmk_p2p_section(payment_label, orders, source="Bybit", trade_side="buy"):
    if trade_side == "sell":
        action = "卖出"
        prefix = "卖"
        title = f"当前{source}-{payment_label} USDT卖出价（U换{payment_label}）"
    else:
        action = "购买"
        prefix = "买"
        title = f"当前设置{source}-{payment_label} USDT{action}价"
    if orders is None:
        return (
            f"⚠️ 暂时无法连接 {source}-{payment_label} P2P\n"
            "请稍后重试，或在 Bybit App → P2P → MMK 查看。"
        )
    if not orders:
        return (
            f"⚠️ {source}-{payment_label} 当前 P2P 无挂单\n"
            "请稍后再试，或在 Bybit App 的 P2P 缅甸区核对。"
        )
    lines = [title]
    for idx, row in enumerate(orders, 1):
        limit_text = _format_mmk_limit(row.get("minAmount"), row.get("maxAmount"))
        if limit_text:
            lines.append(f"{prefix}{idx}：{row['price']:.2f}   {row['nickName']}   {limit_text}")
        else:
            lines.append(f"{prefix}{idx}：{row['price']:.2f}   {row['nickName']}")
    return "\n".join(lines)


def build_mmk_p2p_reply(command):
    cmd = (command or "").lower()
    pay_label = MMK_QUERY_COMMANDS.get(cmd)
    if pay_label is None and cmd != "bma":
        return None
    if cmd == "bma":
        sections = []
        for payment_ids, label in BYBIT_MMK_PAYMENTS:
            orders, source = fetch_mmk_p2p_orders(payment_ids, 10)
            sections.append(format_mmk_p2p_section(label, orders, source))
        body = "\n\n".join(sections)
    else:
        payment_ids = MMK_QUERY_PAYMENT_IDS.get(pay_label, [])
        orders, source = fetch_mmk_p2p_orders(payment_ids, 10)
        body = format_mmk_p2p_section(pay_label, orders, source)
    return append_query_command_footer(body, MMK_COMMAND_HELP, cmd)


def build_mmk_p2p_sell_reply(command):
    cmd = (command or "").lower()
    pay_label = MMK_SELL_QUERY_COMMANDS.get(cmd)
    if not pay_label:
        return None
    payment_ids = MMK_QUERY_PAYMENT_IDS.get(pay_label, [])
    orders, source = fetch_mmk_p2p_orders(payment_ids, 10, side="0")
    body = format_mmk_p2p_section(pay_label, orders, source, trade_side="sell")
    return append_query_command_footer(body, MMK_COMMAND_HELP, cmd)


def fetch_coingecko_usdt_rate(vs_currency):
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "tether", "vs_currencies": vs_currency.lower()},
            timeout=10,
            headers=_p2p_http_headers(),
        )
        if resp.status_code == 200:
            val = resp.json().get("tether", {}).get(vs_currency.lower())
            if val is not None:
                return float(val)
    except Exception as exc:
        log.warning("CoinGecko %s fetch failed: %s", vs_currency, exc)
    return None


def fetch_okx_usdt_cny_rate():
    headers = _p2p_http_headers()
    try:
        resp = requests.get(
            "https://www.okx.com/v3/c2c/tradingOrders/books",
            params={
                "quoteCurrency": "CNY",
                "baseCurrency": "USDT",
                "side": "sell",
                "paymentMethod": "all",
            },
            timeout=10,
            headers=headers,
        )
        if resp.status_code == 200:
            body = resp.json()
            if body.get("code") in (None, 0, "0"):
                sell = body.get("data", {}).get("sell", [])
                if sell:
                    return float(sell[0]["price"])
    except Exception as exc:
        log.warning("OKX rate fetch failed: %s", exc)
    return fetch_binance_p2p_rate("CNY", "SELL")


def fetch_huobi_usdt_cny_rate():
    headers = {**_p2p_http_headers(), "Referer": "https://www.htx.com/"}
    sources = (
        ("https://otc-api.huobi.pro/v1/data/trade-market", {
            "coinId": 2, "currency": 1, "tradeType": "sell", "currPage": 1, "payMethod": 0,
        }),
        ("https://www.htx.com/-/x/otc/v1/data/trade-market", {
            "coinId": 2, "currency": 1, "tradeType": "sell", "currPage": 1, "payMethod": 0,
        }),
    )
    for url, params in sources:
        try:
            resp = requests.get(url, params=params, timeout=10, headers=headers)
            if resp.status_code != 200:
                continue
            payload = resp.json()
            rows = payload.get("data")
            if isinstance(rows, list) and rows:
                price = rows[0].get("price")
                if price is not None:
                    return float(price)
        except Exception as exc:
            log.warning("Huobi/HTX rate fetch failed (%s): %s", url, exc)
    rate = fetch_binance_p2p_rate("CNY", "SELL")
    if rate is not None:
        return rate
    return fetch_okx_usdt_cny_rate()


def fetch_myr_usdt_rate():
    rate = fetch_binance_p2p_rate("MYR", "SELL")
    if rate is not None:
        return rate
    rate = fetch_coingecko_usdt_rate("myr")
    if rate is not None:
        return rate
    return None


def fetch_myr_usdt_rate_with_note():
    rate = fetch_binance_p2p_rate("MYR", "SELL")
    if rate is not None:
        return rate, ""
    rate = fetch_coingecko_usdt_rate("myr")
    if rate is not None:
        return rate, "现货参考"
    return None, ""


def fetch_mmk_usdt_rate_with_note():
    """缅币 MMK/USDT：优先 Bybit P2P（KBZPay → Mobile Banking → 全渠道）。"""
    for payment_ids, label in (
        (["601"], "KBZPay"),
        (["602", "605"], "Mobile Banking"),
        ([], "Bybit P2P"),
    ):
        orders, source = fetch_mmk_p2p_orders(payment_ids, 1)
        if orders:
            note = label if source == "Bybit" else f"{source} {label}"
            return orders[0]["price"], note
    return None, ""


def delete_bills_by_source_message(group_id, source_message_id, bill_type=None, limit=None):
    conn = get_db()
    c = conn.cursor()
    if bill_type:
        sql = (
            "SELECT id FROM bills WHERE group_id = ? AND source_message_id = ? AND bill_type = ? "
            "ORDER BY id DESC"
        )
        params = (group_id, source_message_id, bill_type)
    else:
        sql = "SELECT id FROM bills WHERE group_id = ? AND source_message_id = ? ORDER BY id DESC"
        params = (group_id, source_message_id)
    c.execute(sql, params)
    ids = [row[0] for row in c.fetchall()]
    if limit:
        ids = ids[:limit]
    for bid in ids:
        c.execute("DELETE FROM bills WHERE id = ?", (bid,))
    deleted = len(ids)
    conn.commit()
    conn.close()
    return deleted


def delete_recent_bills(group_id, bill_type, count, target_date=None):
    conn = get_db()
    c = conn.cursor()
    if target_date:
        c.execute(
            "SELECT id FROM bills WHERE group_id = ? AND bill_type = ? AND date_str = ? ORDER BY id DESC LIMIT ?",
            (group_id, bill_type, target_date, count),
        )
    else:
        c.execute(
            "SELECT id FROM bills WHERE group_id = ? AND bill_type = ? ORDER BY id DESC LIMIT ?",
            (group_id, bill_type, count),
        )
    ids = [row[0] for row in c.fetchall()]
    for bid in ids:
        c.execute("DELETE FROM bills WHERE id = ?", (bid,))
    conn.commit()
    conn.close()
    return len(ids)


def update_bills_exchange_rate(group_id, new_rate, target_date=None):
    conn = get_db()
    c = conn.cursor()
    if target_date:
        c.execute(
            "UPDATE bills SET exchange_rate = ?, usdt_amount = amount / ? "
            "WHERE group_id = ? AND date_str = ? AND bill_type = 'income' AND amount != 0",
            (new_rate, new_rate, group_id, target_date),
        )
    else:
        c.execute(
            "UPDATE bills SET exchange_rate = ?, usdt_amount = amount / ? "
            "WHERE group_id = ? AND bill_type = 'income' AND amount != 0",
            (new_rate, new_rate, group_id),
        )
    updated = c.rowcount
    conn.commit()
    conn.close()
    update_setting(group_id, "exchange_rate", new_rate)
    return updated


def get_user_bills_today(group_id, user_id, target_date):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT bill_type, remark, amount, usdt_amount, timestamp FROM bills "
        "WHERE group_id = ? AND date_str = ? AND user_id = ? ORDER BY id ASC",
        (group_id, target_date, user_id),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def build_self_bill_report(group_id, user_id, target_date, display_name):
    rows = get_user_bills_today(group_id, user_id, target_date)
    if not rows:
        return f"📭 <b>{_html_esc(display_name)}</b> 今日暂无个人账单。"
    income_lines, expense_lines = [], []
    total_in, total_out = 0.0, 0.0
    for bill_type, remark, amount, usdt, ts in rows:
        time_s = ts[11:19] if len(ts) > 16 else ts[11:16]
        rem = _html_esc(remark or tr(group_id, "no_remark"))
        if bill_type == "income":
            income_lines.append(f"{time_s} {rem} {_tag_rmb(amount)} / {usdt:.2f}U")
            total_in += usdt or 0
        else:
            expense_lines.append(f"{time_s} {rem} {usdt:.2f}U")
            total_out += usdt or 0
    parts = [f"👤 <b>{_html_esc(display_name)}</b> 今日自查 ({target_date})"]
    if income_lines:
        parts.append("\n<b>入款</b>\n" + "\n".join(income_lines))
        parts.append(f"入款合计：<b>{total_in:.2f}U</b>")
    if expense_lines:
        parts.append("\n<b>下发</b>\n" + "\n".join(expense_lines))
        parts.append(f"下发合计：<b>{total_out:.2f}U</b>")
    return "\n".join(parts)


def process_extended_settings(message, text, gid, uid, tg_username, today):
    """处理扩展设置/查询命令，返回 True 表示已处理。"""
    t = (text or "").strip()

    if t in ("汇率", "查汇率"):
        rate = get_effective_rate(gid, "income")
        bot.reply_to(message, f"💱 当前入款汇率：<code>{rate:.4f}</code>", parse_mode="HTML")
        return True
    if t in ("下发汇率", "查下发汇率"):
        rate = get_effective_rate(gid, "expense")
        mode = get_extra_settings(gid).get("expense_mode") or "usdt"
        mode_label = "人民币下发" if mode == "rmb" else "U 下发"
        bot.reply_to(
            message,
            f"💱 当前下发汇率：<code>{rate:.4f}</code>（{mode_label}）\n"
            f"例：<code>下发500</code> → "
            f"{'500÷' + f'{rate:.2f}' + '=' + f'{500/rate:.2f}' if mode == 'rmb' else '500U'}",
            parse_mode="HTML",
        )
        return True
    if t.upper() == "Z0":
        body = format_rate_reply("欧易 OTC", fetch_okx_usdt_cny_rate())
        bot.reply_to(
            message,
            append_query_command_footer(body, OTC_COMMAND_HELP, "Z0"),
            parse_mode="HTML",
        )
        return True
    if t.upper() == "H0":
        body = format_rate_reply("火币 OTC", fetch_huobi_usdt_cny_rate())
        bot.reply_to(
            message,
            append_query_command_footer(body, OTC_COMMAND_HELP, "H0"),
            parse_mode="HTML",
        )
        return True
    if t.lower() == "m0":
        rate, note = fetch_myr_usdt_rate_with_note()
        body = format_rate_reply("马币 OTC", rate, note)
        bot.reply_to(
            message,
            append_query_command_footer(body, OTC_COMMAND_HELP, "m0"),
            parse_mode="HTML",
        )
        return True
    if t.lower() == "mm0":
        rate, note = fetch_mmk_usdt_rate_with_note()
        if rate is None:
            body = (
                "⚠️ <b>Bybit MMK P2P 当前无 USDT 卖单</b>\n\n"
                "请试分渠道命令：\n"
                "• <code>bkb</code> — KBZPay 买U Top10\n"
                "• <code>bmm</code> — Mobile Banking 买U Top10\n"
                "• <code>skb</code> — U换KBZ Top10\n"
                "• <code>smm</code> — U换Bank Transfer Top10\n"
                "• <code>bma</code> — 以上买U渠道各 Top10"
            )
        else:
            body = format_rate_reply("Bybit MMK P2P", rate, note)
        bot.reply_to(
            message,
            append_query_command_footer(body, MMK_COMMAND_HELP, "mm0"),
            parse_mode="HTML",
        )
        return True

    if t.lower() in MMK_SELL_QUERY_COMMANDS:
        try:
            bot.reply_to(message, build_mmk_p2p_sell_reply(t))
        except Exception as exc:
            log.exception("MMK P2P sell query failed: %s", exc)
            bot.reply_to(message, f"⚠️ MMK 卖U查询失败，请稍后重试。（{exc}）")
        return True

    if t.lower() in MMK_QUERY_COMMANDS:
        try:
            bot.reply_to(message, build_mmk_p2p_reply(t))
        except Exception as exc:
            log.exception("MMK P2P query failed: %s", exc)
            bot.reply_to(message, f"⚠️ MMK 汇率查询失败，请稍后重试。（{exc}）")
        return True

    if t.lower() in OKX_C2C_QUERY_COMMANDS:
        bot.reply_to(message, build_okx_c2c_reply(t))
        return True

    m = re.match(r"^查汇率(\d+(?:\.\d+)?)$", t)
    if m:
        cny = float(m.group(1))
        rate = get_effective_rate(gid, "income")
        usdt = convert_rmb_to_usdt(
            cny, rate, get_effective_fee(gid, "income"),
            get_extra_settings(gid).get("multiply_rate_mode"),
        )
        bot.reply_to(
            message,
            f"💱 {cny:.2f} CNY ≈ <code>{usdt:.4f}</code> U（汇率 {rate:.4f}）",
            parse_mode="HTML",
        )
        return True

    m = re.match(r"^币价(\d+(?:\.\d+)?)\s+汇率(\d+(?:\.\d+)?)$", t)
    if m:
        price = float(m.group(1))
        rate = float(m.group(2))
        result = price / rate if rate else 0
        bot.reply_to(message, f"🧮 点位计算：{price} / {rate} = <code>{result:.4f}</code> U", parse_mode="HTML")
        return True

    if process_lookup_queries(message, t):
        return True

    setting_patterns = [
        (r"^设置入款费率\s*(-?\d+(?:\.\d+)?)$", "income_fee_rate", lambda v: float(v) / 100),
        (r"^设置下发费率\s*(-?\d+(?:\.\d+)?)$", "expense_fee_rate", lambda v: float(v) / 100),
        (r"^设置入款汇率\s*(-?\d+(?:\.\d+)?)$", "income_exchange_rate", float),
        (r"^设置下发汇率\s*(-?\d+(?:\.\d+)?)$", "expense_exchange_rate", float),
        (r"^设置代付价格(\d+(?:\.\d+)?)$", "payment_price", float),
        (r"^设置币种([A-Za-z]{3})$", "currency", lambda v: v.upper()),
        (r"^显示条数(\d+)$", "display_count", int),
        (r"^设置日切(\d{1,2})$", "day_cut_hour", int),
        (r"^设置全局日切(\d{1,2})$", "global_day_cut_hour", int),
        (r"^催收(\d+)$", "collection_interval", int),
    ]
    for pattern, key, conv in setting_patterns:
        m = re.match(pattern, t)
        if m:
            if not can_operate_in_group(gid, uid, tg_username):
                bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
                return True
            val = conv(m.group(1))
            if key == "collection_interval":
                update_extra_setting(gid, "collection_enabled", True)
            if key == "expense_exchange_rate":
                apply_expense_exchange_rate(gid, val)
                saved = get_effective_rate(gid, "expense")
                bot.reply_to(
                    message,
                    f"✅ 已设置下发汇率 <code>{saved:.4f}</code>，并切换为<b>人民币下发模式</b>\n"
                    f"（例：<code>下发500</code> = 500 ÷ {saved} = <code>{500 / saved:.2f}</code>U）\n"
                    f"💡 发送 <code>下发汇率</code> 可随时查看当前下发汇率",
                    parse_mode="HTML",
                )
                return True
            update_extra_setting(gid, key, val)
            bot.reply_to(message, f"✅ 已设置 <b>{key}</b> = <code>{val}</code>", parse_mode="HTML")
            return True

    simple_toggles = {
        "设置实时汇率": ("use_realtime_rate", True),
        "开启乘汇率模式": ("multiply_rate_mode", True),
        "关闭乘汇率模式": ("multiply_rate_mode", False),
        "显示人民币": ("show_rmb", True),
        "隐藏人民币": ("show_rmb", False),
        "显示分秒": ("time_format", "hm"),
        "显示时分秒": ("time_format", "hms"),
        "开启记账置顶": ("pin_bills", True),
        "关闭记账置顶": ("pin_bills", False),
        "关闭日切": ("day_cut_hour", None),
        "关闭全局日切": ("global_day_cut_hour", None),
        "开启地址识别": ("address_detect", True),
        "关闭地址识别": ("address_detect", False),
        "开启银行卡自动识别": ("bank_detect", True),
        "关闭银行卡识别": ("bank_detect", False),
        "开启用户变更通知": ("user_change_notify", True),
        "关闭用户变更通知": ("user_change_notify", False),
        "开启操作人分类": ("classify_mode", "operator"),
        "开启回复人分类": ("classify_mode", "replier"),
        "关闭分类": ("classify_mode", "none"),
        "开启催收": ("collection_enabled", True),
        "关闭催收": ("collection_enabled", False),
        "设置下发人民币模式": ("expense_mode", "rmb"),
        "设置下发币模式": ("expense_mode", "usdt"),
        "设置所有人": ("all_operators", True),
        "取消所有人": ("all_operators", False),
    }
    if t in simple_toggles:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return True
        key, val = simple_toggles[t]
        update_extra_setting(gid, key, val)
        bot.reply_to(message, f"✅ 已更新：<b>{t}</b>", parse_mode="HTML")
        return True

    m = re.match(r"^设置实时汇率(-?\d+(?:\.\d+)?)$", t)
    if m:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return True
        update_extra_setting(gid, "use_realtime_rate", True)
        update_extra_setting(gid, "realtime_rate_offset", float(m.group(1)))
        bot.reply_to(message, f"✅ 已开启实时汇率，偏移 <code>{m.group(1)}</code>", parse_mode="HTML")
        return True

    m = re.match(r"^设置下发地址(.+)$", t)
    if m:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return True
        addr = m.group(1).strip()
        extra = get_extra_settings(gid)
        addrs = list(extra.get("payout_addresses") or [])
        if addr not in addrs:
            addrs.append(addr)
        update_extra_setting(gid, "payout_addresses", addrs)
        bot.reply_to(message, f"✅ 已添加下发地址：<code>{_html_esc(addr)}</code>", parse_mode="HTML")
        return True

    m = re.match(r"^删除下发地址(.+)$", t)
    if m:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return True
        addr = m.group(1).strip()
        extra = get_extra_settings(gid)
        addrs = [a for a in (extra.get("payout_addresses") or []) if a != addr]
        update_extra_setting(gid, "payout_addresses", addrs)
        bot.reply_to(message, "✅ 已删除下发地址。", parse_mode="HTML")
        return True

    if t in ("删除账单", "撤销账单", "撤销今天", "删今天"):
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_delete_perm"), parse_mode="HTML")
            return True
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM bills WHERE group_id = ? AND date_str = ?", (gid, today))
        conn.commit()
        conn.close()
        bot.reply_to(message, tr(gid, "delete_today_ok", date=today))
        send_text_bill_report(gid, gid, today)
        return True

    m = re.match(r"^修改汇款(-?\d+(?:\.\d+)?)$", t)
    if m:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return True
        new_rate = float(m.group(1))
        updated = update_bills_exchange_rate(gid, new_rate, today)
        bot.reply_to(
            message,
            f"✅ 已同步更新今日 {updated} 笔入款汇率为 <code>{new_rate:.4f}</code>",
            parse_mode="HTML",
        )
        send_text_bill_report(gid, gid, today)
        return True

    if t == "通知所有人":
        if not can_manage_group_operators(uid):
            bot.reply_to(message, tr(gid, "no_manage_operators"), parse_mode="HTML")
            return True
        try:
            bot.send_message(gid, "📢 请各位操作人注意查账。")
        except Exception as exc:
            bot.reply_to(message, f"❌ 通知失败：{exc}")
            return True
        bot.reply_to(message, "✅ 已发送群通知。")
        return True

    if t in ("置顶", "取消置顶"):
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return True
        if t == "置顶" and message.reply_to_message:
            try:
                bot.pin_chat_message(gid, message.reply_to_message.message_id, disable_notification=True)
                bot.reply_to(message, "📌 已置顶。")
            except Exception as exc:
                bot.reply_to(message, f"❌ 置顶失败：{exc}")
        elif t == "取消置顶" and message.reply_to_message:
            try:
                bot.unpin_chat_message(gid, message.reply_to_message.message_id)
                bot.reply_to(message, "📌 已取消置顶。")
            except Exception as exc:
                bot.reply_to(message, f"❌ 取消置顶失败：{exc}")
        else:
            bot.reply_to(message, "💡 请回复要置顶/取消置顶的消息。")
        return True

    return False


def _luhn_valid(card_no):
    digits = [int(c) for c in card_no if c.isdigit()]
    if len(digits) < 13:
        return False
    checksum = 0
    parity = len(digits) % 2
    for idx, digit in enumerate(digits):
        if idx % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _id_card_checksum_valid(id_num):
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check_map = "10X98765432"
    body, check = id_num[:17], id_num[17].upper()
    if not body.isdigit():
        return False
    total = sum(int(body[i]) * weights[i] for i in range(17))
    return check_map[total % 11] == check


def _id_card_region(code6):
    if code6 in ID_AREA_MAP:
        return ID_AREA_MAP[code6]
    prov = ID_PROVINCE_MAP.get(code6[:2])
    if prov:
        return f"{prov}（{code6}）"
    return f"未知地区（{code6}）"


def parse_id_card_info(id_num):
    id_num = id_num.upper()
    if not re.fullmatch(r"\d{17}[\dX]", id_num):
        return None
    valid = _id_card_checksum_valid(id_num)
    region = _id_card_region(id_num[:6])
    birth_raw = id_num[6:14]
    try:
        birth = datetime.strptime(birth_raw, "%Y%m%d").date()
        age = (datetime.now().date() - birth).days // 365
        birth_text = birth.strftime("%Y-%m-%d")
    except ValueError:
        birth_text = f"{birth_raw[:4]}-{birth_raw[4:6]}-{birth_raw[6:8]}"
        age = None
    gender = "男" if int(id_num[16]) % 2 else "女"
    return {
        "number": id_num,
        "valid": valid,
        "region": region,
        "birthday": birth_text,
        "age": age,
        "gender": gender,
    }


def _phone_carrier(phone):
    prefix3 = phone[:3]
    for prefixes, carrier in PHONE_CARRIER_PREFIXES:
        if prefix3 in prefixes:
            return carrier
    return "未知运营商"


def _fetch_phone_from_sogou(phone):
    """搜狗号段库备用（前 7 位）。"""
    if len(phone) < 7:
        return None
    try:
        resp = requests.get(
            "https://www.sogou.com/websearch/phoneAddress.jsp",
            params={"phoneNumber": phone[:7]},
            timeout=8,
            headers=_p2p_http_headers(),
        )
        if resp.status_code != 200:
            return None
        match = re.search(r'void\("(.+?)"\)', resp.text)
        if not match:
            return None
        tokens = match.group(1).strip().split()
        if not tokens:
            return None
        carrier = ""
        loc_tokens = tokens
        if any(k in tokens[-1] for k in ("移动", "联通", "电信", "广电")):
            carrier = tokens[-1]
            loc_tokens = tokens[:-1]
        location = "".join(loc_tokens)
        if not location and not carrier:
            return None
        return {
            "province": location,
            "city": "",
            "carrier": carrier,
            "source": "搜狗号段库",
        }
    except Exception as exc:
        log.warning("sogou phone lookup failed: %s", exc)
    return None


def fetch_phone_info(phone):
    info = {"number": phone, "carrier": _phone_carrier(phone), "province": "", "city": "", "source": "号段识别"}
    urls = []
    if PHONE_LOOKUP_URL:
        urls.append(PHONE_LOOKUP_URL.replace("{phone}", phone))
    urls.append(f"https://api.vvhan.com/api/phone?tel={phone}")
    for url in urls:
        try:
            resp = requests.get(url, timeout=8, headers=_p2p_http_headers())
            if resp.status_code != 200:
                continue
            payload = resp.json()
            if payload.get("success") is False:
                continue
            block = payload.get("info") or payload.get("data") or payload
            province = block.get("province") or block.get("prov") or ""
            city = block.get("city") or block.get("area") or ""
            carrier = block.get("carrier") or block.get("operator") or block.get("sp") or ""
            if province or city or carrier:
                info["province"] = str(province).strip()
                info["city"] = str(city).strip()
                if carrier:
                    info["carrier"] = str(carrier).strip()
                info["source"] = "在线号段库"
                return info
        except Exception as exc:
            log.warning("phone lookup failed (%s): %s", url, exc)

    sogou = _fetch_phone_from_sogou(phone)
    if sogou:
        info["province"] = sogou.get("province") or ""
        info["city"] = sogou.get("city") or ""
        if sogou.get("carrier"):
            info["carrier"] = sogou["carrier"]
        info["source"] = sogou.get("source") or "搜狗号段库"
    return info


def _bank_from_local_bin(card_no):
    for prefix, bank, card_type in sorted(BANK_BIN_MAP, key=lambda x: len(x[0]), reverse=True):
        if card_no.startswith(prefix):
            return {"bank": bank, "card_type": card_type, "validated": None, "source": "本地BIN库"}
    return None


def fetch_bank_card_info(card_no):
    info = _bank_from_local_bin(card_no) or {
        "bank": "", "card_type": "", "validated": None, "source": "",
    }
    info["number"] = card_no
    info["luhn"] = _luhn_valid(card_no)
    try:
        resp = requests.get(
            "https://ccdcapi.alipay.com/validateAndCacheCardInfo.json",
            params={"cardNo": card_no, "cardBinCheck": "true"},
            timeout=8,
            headers=_p2p_http_headers(),
        )
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("validated"):
                bank_code = payload.get("bank") or ""
                card_type_code = payload.get("cardType") or ""
                info["bank"] = BANK_CODE_NAMES.get(bank_code, bank_code or info.get("bank", ""))
                info["card_type"] = CARD_TYPE_NAMES.get(card_type_code, card_type_code or info.get("card_type", ""))
                info["validated"] = True
                info["source"] = "支付宝BIN"
            elif not info.get("bank"):
                info["validated"] = False
    except Exception as exc:
        log.warning("bank card lookup failed: %s", exc)
    if not info.get("bank") and info.get("luhn"):
        info["source"] = info.get("source") or "格式校验"
    return info


def format_phone_lookup_reply(info):
    lines = [
        "📱 <b>手机号查询</b>",
        f"号码：<code>{info['number']}</code>",
        f"运营商：{info['carrier']}",
    ]
    loc_parts = [x for x in (info.get("province"), info.get("city")) if x]
    if loc_parts:
        lines.append(f"归属地：{''.join(loc_parts)}")
    else:
        lines.append("归属地：暂未识别（在线号段库连接失败）")
    lines.append(f"来源：{info.get('source', '号段识别')}")
    lines.append("说明：不含机主姓名等隐私信息。")
    return "\n".join(lines)


def format_bank_lookup_reply(info):
    lines = [
        "💳 <b>银行卡查询</b>",
        f"卡号：<code>{info['number']}</code>",
    ]
    if info.get("bank"):
        lines.append(f"发卡行：{info['bank']}")
    else:
        lines.append("发卡行：未识别（请确认卡号前 6 位是否正确）")
    if info.get("card_type"):
        lines.append(f"卡类型：{info['card_type']}")
    if info.get("validated") is True:
        lines.append("BIN校验：通过")
    elif info.get("validated") is False:
        lines.append("BIN校验：未通过（可能是测试号或卡号有误）")
    if info.get("luhn") is not None:
        lines.append(f"Luhn校验：{'通过' if info['luhn'] else '未通过'}")
    if info.get("source"):
        lines.append(f"来源：{info['source']}")
    lines.append("说明：不含持卡人姓名/余额；仅识别发卡行与卡类型。")
    return "\n".join(lines)


def format_id_lookup_reply(info):
    lines = [
        "🪪 <b>身份证解析</b>",
        f"号码：<code>{info['number']}</code>",
        f"归属地：{info['region']}",
        f"出生日期：{info['birthday']}",
    ]
    if info.get("age") is not None:
        lines.append(f"年龄：约 {info['age']} 岁")
    lines.append(f"性别：{info['gender']}")
    lines.append(f"校验码：{'有效' if info['valid'] else '无效（号码可能有误）'}")
    lines.append("说明：由号码规则解析，不含姓名等实名信息。")
    return "\n".join(lines)


def _reply_lookup(message, text):
    wait = bot.reply_to(message, "🔍 正在查询资料…")
    try:
        reply = text
        if isinstance(text, str):
            bot.reply_to(message, reply, parse_mode="HTML")
        else:
            bot.reply_to(message, reply)
    finally:
        try:
            bot.delete_message(message.chat.id, wait.message_id)
        except Exception:
            pass


def process_lookup_queries(message, text):
    """手机号 / 银行卡 / 身份证查询（群/私聊均可）。"""
    t = (text or "").strip()
    gid = message.chat.id

    m = re.match(r"^查询(\d{11})$", t)
    if m and m.group(1).startswith("1"):
        phone = m.group(1)
        info = fetch_phone_info(phone)
        _reply_lookup(message, format_phone_lookup_reply(info))
        return True

    m = re.match(r"^查询(\d{17}[\dXx])$", t)
    if m:
        id_num = m.group(1).upper()
        info = parse_id_card_info(id_num)
        if info:
            _reply_lookup(message, format_id_lookup_reply(info))
        else:
            bot.reply_to(message, "❌ 身份证号码格式不正确。", parse_mode="HTML")
        return True

    m = re.match(r"^查询(\d{16,19})$", t)
    if m:
        card = m.group(1)
        info = fetch_bank_card_info(card)
        _reply_lookup(message, format_bank_lookup_reply(info))
        return True

    if re.match(r"^1\d{10}$", t):
        if message.chat.type in ("group", "supergroup") and looks_like_billing_command(t, gid):
            return False
        info = fetch_phone_info(t)
        _reply_lookup(message, format_phone_lookup_reply(info))
        return True
    return False


def process_reply_undo(message, gid, uid, tg_username, today):
    t = (text or "").strip() if (text := get_message_text(message)) else ""
    if not message.reply_to_message:
        return False
    if not can_operate_in_group(gid, uid, tg_username):
        bot.reply_to(message, tr(gid, "no_delete_perm"), parse_mode="HTML")
        return True
    src_id = message.reply_to_message.message_id
    if t == "撤销":
        n = delete_bills_by_source_message(gid, src_id)
        bot.reply_to(message, f"🗑️ 已撤销 {n} 笔关联账单。" if n else "🔍 未找到可撤销账单。")
        if n:
            send_text_bill_report(gid, gid, today)
        return True
    m = re.match(r"^撤销入款(\d+)?条?$", t)
    if m or t == "撤销入款":
        count = int(m.group(1)) if m and m.group(1) else 1
        n = delete_bills_by_source_message(gid, src_id, "income", count)
        if not n:
            n = delete_recent_bills(gid, "income", count, today)
        bot.reply_to(message, f"🗑️ 已撤销入款 {n} 笔。")
        if n:
            send_text_bill_report(gid, gid, today)
        return True
    m = re.match(r"^撤销下发(\d+)?条?$", t)
    if m or t == "撤销下发":
        count = int(m.group(1)) if m and m.group(1) else 1
        n = delete_bills_by_source_message(gid, src_id, "expense", count)
        if not n:
            n = delete_recent_bills(gid, "expense", count, today)
        bot.reply_to(message, f"🗑️ 已撤销下发 {n} 笔。")
        if n:
            send_text_bill_report(gid, gid, today)
        return True
    return False


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
def add_bill(
    group_id, user_id, username, remark, amount, bill_type,
    exchange_rate=None, source_message_id=None, fee_rate=None, is_usdt_input=False,
):
    extra = get_extra_settings(group_id)
    multiply_mode = bool(extra.get("multiply_rate_mode"))
    inline_rate = exchange_rate

    if fee_rate is None:
        fee_rate = get_effective_fee(group_id, bill_type)

    if bill_type == "income":
        if exchange_rate is None:
            exchange_rate = get_effective_rate(group_id, "income")
        if is_usdt_input:
            usdt_amount = abs(amount)
            rmb_amount = convert_usdt_to_rmb(usdt_amount, exchange_rate, multiply_mode)
            if amount < 0:
                rmb_amount = -rmb_amount
                usdt_amount = -usdt_amount
            amount = rmb_amount
        else:
            usdt_amount = convert_rmb_to_usdt(abs(amount), exchange_rate, fee_rate, multiply_mode)
            if amount < 0:
                usdt_amount = -usdt_amount
    else:
        eff_rate = (
            float(inline_rate)
            if inline_rate is not None
            else get_effective_rate(group_id, "expense")
        )
        exchange_rate = eff_rate
        if is_usdt_input:
            usdt_amount = abs(amount)
            amount = convert_usdt_to_rmb(usdt_amount, eff_rate, multiply_mode)
        elif expense_amount_is_rmb(extra, inline_rate, is_usdt_input):
            rmb_val = abs(amount)
            usdt_amount = convert_rmb_to_usdt(rmb_val, eff_rate, fee_rate, multiply_mode)
            amount = rmb_val
        else:
            usdt_amount = abs(amount)
            amount = convert_usdt_to_rmb(usdt_amount, eff_rate, multiply_mode)

    tz = get_setting(group_id, "timezone") or "Asia/Shanghai"
    date_str, full_time = get_billing_date_str(group_id)
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO bills
        (group_id, user_id, username, remark, amount, usdt_amount, exchange_rate,
         bill_type, timestamp, date_str, is_settled, source_message_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            group_id, user_id, username, remark, amount, usdt_amount, exchange_rate,
            bill_type, full_time, date_str, source_message_id,
        ),
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


def _bill_time_str(timestamp, group_id):
    extra = get_extra_settings(group_id)
    fmt = extra.get("time_format") or "hm"
    if fmt == "hms" and timestamp and len(timestamp) >= 19:
        return timestamp[11:19]
    if timestamp and len(timestamp) >= 16:
        return timestamp[11:16]
    return ""


def _format_income_line(remark, operator, amount, usdt, rate, timestamp, user_id=None, group_id=None):
    time_s = _bill_time_str(timestamp, group_id) if group_id else (timestamp[11:16] if timestamp else "")
    extra = get_extra_settings(group_id) if group_id else {}
    show_rmb = extra.get("show_rmb", True)
    if show_rmb:
        core = f"{time_s} {amount:.0f}/{rate:.2f}={usdt:.2f}U"
    else:
        core = f"{time_s} {usdt:.2f}U"
    op = _tag_operator(operator, user_id)
    rem = _tag_remark(remark)
    if rem:
        return f"{rem}{core} {op}"
    return f"{core} {op}"


def _format_expense_line(remark, operator, usdt, timestamp, user_id=None, group_id=None):
    time_s = _bill_time_str(timestamp, group_id) if group_id else (timestamp[11:16] if timestamp else "")
    word = tr(group_id, "expense_word") if group_id else "下发"
    core = f"{time_s} {word}{usdt:.2f}U"
    op = _tag_operator(operator, user_id)
    rem = _tag_remark(remark)
    if rem:
        return f"{rem}{core} {op}"
    return f"{core} {op}"


def _remark_for_category(remark):
    """分类汇总用：空备注返回 None，不参与「入款备注分类」。"""
    rem = (remark or "").strip()
    return rem or None


def build_bill_report_text(group_id, target_date, show_all_categories=False):
    income_rate = get_effective_rate(group_id, "income")
    expense_rate = get_effective_rate(group_id, "expense")
    fee_rate = get_effective_fee(group_id, "income")
    display_n = int(get_extra_settings(group_id).get("display_count") or 5)
    income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)

    total_rmb = float((total_income[0] or 0) if total_income else 0)
    total_usdt = float((total_income[1] or 0) if total_income else 0)
    expense_usdt = float((total_expense[0] or 0) if total_expense else 0)
    remaining_usdt = total_usdt - expense_usdt

    summary = {}
    for row in income:
        rem = _remark_for_category(row[0])
        if not rem:
            continue
        summary.setdefault(rem, {"rmb": 0.0, "usdt": 0.0})
        summary[rem]["rmb"] += row[2]
        summary[rem]["usdt"] += row[3]

    lines = [tr(group_id, "income_header", n=len(income))]
    if income:
        for row in income[-display_n:]:
            uid = row[7] if len(row) > 7 else None
            lines.append(_format_income_line(row[0], row[1], row[2], row[3], row[4], row[5], uid, group_id))
    else:
        lines.append(tr(group_id, "no_income"))

    category_items = list(summary.items())
    if category_items:
        lines.append("")
        lines.append(tr(group_id, "category_header"))
        visible_categories = category_items if show_all_categories else category_items[:3]
        cate_lines = []
        for key, val in visible_categories:
            cate_lines.append(f"{_tag_remark(key).strip()} 👉 {_tag_rmb(val['rmb'])}/{val['usdt']:.2f}U")
        lines.append(f"<blockquote>{chr(10).join(cate_lines)}</blockquote>")

    lines.append("")
    lines.append(tr(group_id, "expense_header", n=len(expense)))
    if expense:
        for row in expense[-display_n:]:
            uid = row[6] if len(row) > 6 else None
            lines.append(_format_expense_line(row[0], row[1], row[2], row[4], uid, group_id))
    else:
        lines.append(tr(group_id, "no_expense"))

    rate_lines = [
        tr(group_id, "total_income", amount=_tag_rmb(total_rmb)),
        tr(group_id, "fee_rate_label", rate=f"{fee_rate * 100:.0f}"),
    ]
    if abs(income_rate - expense_rate) < 0.001:
        rate_lines.append(tr(group_id, "exchange_rate_label", rate=f"{income_rate:.2f}"))
    else:
        rate_lines.extend([
            tr(group_id, "income_rate_label", rate=f"{income_rate:.2f}"),
            tr(group_id, "expense_rate_label", rate=f"{expense_rate:.2f}"),
        ])
    lines.extend([
        "",
        *rate_lines,
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
        sent = bot.send_message(chat_id, report, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:
        log.exception("账单 HTML 发送失败，改用纯文本: %s", exc)
        plain = re.sub(r"<[^>]+>", "", report)
        try:
            sent = bot.send_message(chat_id, plain, reply_markup=markup)
        except Exception as exc2:
            log.exception("纯文本账单发送失败: %s", exc2)
            raise exc2 from exc
    extra = get_extra_settings(group_id)
    if extra.get("pin_bills"):
        try:
            bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except Exception as exc:
            log.warning("pin bill report failed: %s", exc)


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
        bot.send_message(
            chat_id,
            build_expire_status_message(has_auth, lvl_desc, expire_time, lvl),
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
def can_view_dbstatus(user_id):
    if user_id in FOUNDER_USERS:
        return True
    has_auth, _, _, lvl = get_user_permission_level(user_id)
    return has_auth and lvl == 1


@bot.message_handler(commands=["dbstatus"])
def cmd_dbstatus(message):
    uid = message.from_user.id
    if not can_view_dbstatus(uid):
        bot.reply_to(message, "⚠️ 此命令仅创始人或最高级买家可用。")
        return
    stats = get_db_stats()
    vip1_line = "无"
    if stats["vip1"]:
        v = stats["vip1"]
        flag = "生效中" if v["active"] else "已到期"
        vip1_line = f"UID {v['user_id']} | {v['expire_time']} | {flag}"
    db_path = stats["db_path"].replace("\\", "/")
    persistent = "是 ✅" if db_path.startswith("/data/") else "否 ⚠️ 请挂载 /data 磁盘"
    bot.reply_to(
        message,
        f"🗄 <b>数据库状态</b>\n"
        f"• 路径：<code>{stats['db_path']}</code>\n"
        f"• 持久化磁盘：{persistent}\n"
        f"• 大小：{stats['db_size']:,} bytes\n"
        f"• VIP 记录：{stats['vip_users']} 条\n"
        f"• 账单：{stats['bills']} 条\n"
        f"• 群设置：{stats['settings']} 个\n"
        f"• 本地备份：{stats['backups']} 份\n"
        f"• VIP1 买家：{vip1_line}",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["me", "我"])
def cmd_self_bill(message):
    if message.chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "💡 请在群内使用 /我 或发送「账单」自查。")
        return
    gid = message.chat.id
    uid = message.from_user.id
    today, _ = get_billing_date_str(gid)
    display_name = message.from_user.first_name or "用户"
    bot.reply_to(
        message,
        build_self_bill_report(gid, uid, today, display_name),
        parse_mode="HTML",
    )


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    uid = message.from_user.id
    if message.chat.type == "private":
        send_private_welcome(message.chat.id, uid)
    else:
        bot.send_message(
            message.chat.id,
            f"🤖 <b>{get_bot_brand()}智能分布式记账系统已激活</b>\n\n"
            "📖 完整指令请私聊机器人点 <b>详细说明书</b>（中/英/缅三语）。\n\n"
            "👉 <b>群内常用：</b>\n"
            "• <code>开始</code> / <code>关闭</code> 开启或停止记账\n"
            "• <code>+1000</code>、<code>+1000/7.1</code>、<code>+1000*5</code>、<code>+1000U</code>\n"
            "• <code>下发500</code>、<code>下发500/7.8</code>、<code>+0</code> 查账\n"
            "• <code>账单</code> 或 <code>/我</code> 群员自查\n"
            "• 回复消息：<code>撤销</code>、<code>撤销入款5条</code>",
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
    if is_group_active(group_id):
        bot.send_message(group_id, tr(group_id, "lang_active_hint"), parse_mode="HTML")
    else:
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
        existing_buyer = get_vip1_buyer_user_id()
        if existing_buyer and existing_buyer != buyer_id:
            bot.answer_callback_query(
                call.id,
                "本机器人已有买家，无法再开通新的最高级买家。",
                show_alert=True,
            )
            return
        expire_str = add_vip_user(buyer_id, f"user_{buyer_id}", months, level=1)
        stats = get_db_stats()
        restore_note = (
            f"\n\n📋 数据保留情况：账单 {stats['bills']} 条，"
            f"群设置 {stats['settings']} 个，VIP {stats['vip_users']} 人。"
        )
        try:
            bot.send_message(
                buyer_id,
                f"🎉 <b>最高级买家已开通 {months} 个月！</b>\n"
                f"到期：<code>{expire_str}</code>（北京时间）\n\n"
                f"✅ 您群里的操作人、权限人与历史记账记录均已保留，可直接继续使用。"
                f"{restore_note}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        bot.edit_message_caption(
            f"✅ 审核成功，到期：{expire_str}（北京时间）{restore_note}",
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
        tr(group_id, "web_bill"), url=f"{WEBHOOK_URL}/?group_id={group_id}"
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

    if process_lookup_queries(message, text):
        return

    # 纯算式优先（群/私聊均可，无需操作人权限）
    if try_reply_calculator(message, text):
        return

    if message.chat.type not in ("group", "supergroup"):
        return

    # --- group commands ---
    today, _ = get_billing_date_str(gid)

    if text.strip() == "账单":
        bot.reply_to(
            message,
            build_self_bill_report(gid, uid, today, display_name),
            parse_mode="HTML",
        )
        return

    if process_reply_undo(message, gid, uid, tg_username, today):
        return

    if process_extended_settings(message, text, gid, uid, tg_username, today):
        return

    parsed_zero = parse_income_command(text, gid)
    if parsed_zero and parsed_zero.get("kind") == "bill_zero":
        try:
            send_text_bill_report(gid, gid, today)
        except Exception as exc:
            log.exception("查账失败: %s", exc)
            bot.reply_to(message, tr(gid, "bill_fail", err=exc))
        return

    extra = get_extra_settings(gid)
    if extra.get("address_detect"):
        addr_m = re.search(r"\b(T[A-Za-z0-9]{33})\b", text)
        if addr_m and not looks_like_billing_command(text, gid):
            addr = addr_m.group(1)
            result = fetch_blockchain_usdt_info(addr)
            if result["success"]:
                bot.reply_to(
                    message,
                    f"👤 地址：<code>{addr}</code>\n\n"
                    f"💰 USDT 余额：<code>{result['balance']:.2f}</code> U\n"
                    f"━━━━━━━━━━━━━━━━━━\n📊 流向明细：\n{result['history']}",
                    parse_mode="HTML",
                )
                return
    if extra.get("bank_detect"):
        card_m = re.search(r"\b(\d{16,19})\b", text)
        if card_m and not looks_like_billing_command(text, gid):
            bot.reply_to(message, f"💳 识别到银行卡号：<code>{card_m.group(1)}</code>", parse_mode="HTML")
            return

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

    rate_rest = strip_cmd_prefix_any(text, "set_rate")
    if rate_rest is not None:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return
        try:
            rate = float(rate_rest)
            update_setting(gid, "exchange_rate", rate)
            update_extra_setting(gid, "income_exchange_rate", None)
            bot.reply_to(message, tr(gid, "rate_updated", rate=f"{rate:.2f}"), parse_mode="HTML")
        except ValueError:
            c = cmd(gid, "set_rate")
            bot.reply_to(message, f"❌ 格式错误，例如：{c} 7.3")
        return

    fee_rest = strip_cmd_prefix_any(text, "set_fee")
    if fee_rest is not None:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return
        try:
            fee = float(fee_rest) / 100
            update_setting(gid, "fee_rate", fee)
            update_extra_setting(gid, "income_fee_rate", fee)
            bot.reply_to(message, tr(gid, "fee_updated", rate=f"{fee * 100:.0f}"), parse_mode="HTML")
        except ValueError:
            c = cmd(gid, "set_fee")
            bot.reply_to(message, f"❌ 格式错误，例如：{c} 5")
        return

    op_prefix = find_cmd_prefix(text, "set_operator")
    if op_prefix is not None:
        if not can_manage_group_operators(uid):
            bot.reply_to(message, tr(gid, "no_manage_operators"), parse_mode="HTML")
            return
        targets = parse_operator_targets(text, message.entities, op_prefix)
        if not targets and message.reply_to_message and message.reply_to_message.from_user:
            ru = message.reply_to_message.from_user
            if ru.username:
                targets = [normalize_operator_name(ru.username)]
            else:
                targets = [str(ru.id)]
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
    for rk in ("remove_operator", "remove_operator2", "remove_operator3"):
        rest = strip_cmd_prefix_any(text, rk)
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

    del_remark = parse_remark_delete_command(text)
    if del_remark:
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_delete_perm"), parse_mode="HTML")
            return
        remark = del_remark
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

    view_rest = strip_cmd_prefix_any(text, "view_remark")
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

    if match_class_start(text, gid):
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return
        update_setting(gid, "is_active", 1)
        bot.reply_to(message, tr(gid, "class_start"))
        return

    if match_class_end(text, gid):
        if not can_operate_in_group(gid, uid, tg_username):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
            return
        update_setting(gid, "is_active", 0)
        bot.reply_to(message, tr(gid, "class_end"))
        send_text_bill_report(gid, gid, today)
        return

    if not is_group_active(gid):
        if looks_like_billing_command(text, gid):
            bot.reply_to(message, build_need_class_start(gid), parse_mode="HTML")
        return

    if not can_operate_in_group(gid, uid, tg_username):
        if looks_like_billing_command(text, gid):
            bot.reply_to(message, tr(gid, "no_operate_perm"), parse_mode="HTML")
        return

    parsed_inc = parse_income_command(text, gid)
    if parsed_inc:
        try:
            fee_rate = None
            if parsed_inc.get("fee_pct") is not None:
                fee_rate = float(parsed_inc["fee_pct"]) / 100.0
            add_bill(
                gid, uid, display_name,
                parsed_inc.get("remark", ""),
                parsed_inc["amount"],
                "income",
                exchange_rate=parsed_inc.get("rate"),
                source_message_id=message.message_id,
                fee_rate=fee_rate,
                is_usdt_input=parsed_inc.get("is_usdt", False),
            )
            send_text_bill_report(gid, gid, today)
        except Exception as exc:
            log.exception("记入款失败: %s", exc)
            bot.reply_to(message, tr(gid, "bill_fail", err=exc))
        return

    parsed_exp = parse_expense_command(text, gid)
    if parsed_exp:
        try:
            add_bill(
                gid, uid, display_name,
                parsed_exp.get("remark", ""),
                parsed_exp["amount"],
                "expense",
                exchange_rate=parsed_exp.get("rate"),
                source_message_id=message.message_id,
                is_usdt_input=parsed_exp.get("is_usdt", False),
            )
            send_text_bill_report(gid, gid, today)
        except Exception as exc:
            log.exception("记下发失败: %s", exc)
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
<title>Bill</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,sans-serif}
body{background:#f4f6f9;color:#475569;padding:12px;line-height:1.35;font-size:12px}
.container{max-width:800px;margin:0 auto;background:#fff;border-radius:12px;padding:14px;box-shadow:0 4px 12px rgba(0,0,0,.05);font-size:12px}
.header{text-align:center;margin-bottom:16px;border-bottom:2px solid #edf2f7;padding-bottom:12px}
.header h2{font-size:16px;color:#334155}
.lang-switch{display:flex;gap:6px;justify-content:center;margin-top:10px;flex-wrap:wrap}
.lang-btn{padding:4px 12px;border-radius:999px;border:1px solid #cbd5e1;background:#fff;cursor:pointer;font-size:11px;color:#334155}
.lang-btn.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
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
<h2 id="dashboard-title">...</h2>
<p id="group-text" style="font-size:12px;color:#64748b;margin-top:4px">...</p>
<div class="lang-switch">
<button type="button" class="lang-btn" data-lang="zh">Chinese</button>
<button type="button" class="lang-btn" data-lang="eng">Eng</button>
<button type="button" class="lang-btn" data-lang="my">Myanmar</button>
</div>
<p id="summary-text" class="hint"></p>
<div class="date-picker">
<button id="btn-prev" type="button" class="nav-btn">...</button>
<label for="date-select" id="date-label">...</label>
<input type="date" id="date-select">
<button id="btn-next" type="button" class="nav-btn">...</button>
<button id="btn-all" type="button" class="nav-btn">...</button>
</div>
<div id="date-tags" class="date-tags"></div>
</div>
<h3 id="income-title">...</h3>
<table><thead><tr>
<th id="th-inc-date">...</th><th id="th-inc-time">...</th><th id="th-inc-remark">...</th>
<th id="th-inc-rmb">...</th><th id="th-inc-u">...</th><th id="th-inc-op">...</th>
</tr></thead><tbody id="income-list"></tbody></table>
<h3 class="exp-title" id="expense-title">...</h3>
<table><thead><tr>
<th id="th-exp-date">...</th><th id="th-exp-time">...</th><th id="th-exp-remark">...</th>
<th id="th-exp-usdt">...</th><th id="th-exp-op">...</th>
</tr></thead><tbody id="expense-list"></tbody></table>
<h3 class="cate-title" id="category-title">...</h3>
<table><thead><tr>
<th id="th-cate-remark">...</th><th id="th-cate-rmb">...</th><th id="th-cate-u">...</th><th id="th-cate-count">...</th>
</tr></thead><tbody id="cate-list"></tbody></table>
<div class="summary-grid">
<div class="card"><div class="title" id="card-rate">...</div><div class="value" id="rate">0</div></div>
<div class="card"><div class="title" id="card-total-rmb">...</div><div class="value" id="total_rmb">0</div></div>
<div class="card"><div class="title" id="card-total-usdt">...</div><div class="value" id="total_usdt">0U</div></div>
<div class="card"><div class="title" id="card-expense-usdt">...</div><div class="value" id="expense_usdt">0U</div></div>
<div class="card" style="grid-column:span 2"><div class="title" id="card-remaining-usdt">...</div><div class="value" id="remaining_usdt">0U</div></div>
</div>
</div>
<script>
const params=new URLSearchParams(location.search);
const groupId=params.get('group_id')||'0';
let currentDate=params.get('date')||'';
let labels={};
let currentLang='zh';
const ds=document.getElementById('date-select');
const btnPrev=document.getElementById('btn-prev');
const btnNext=document.getElementById('btn-next');
function fmt(s,vars){return String(s).replace(/\\{(\\w+)\\}/g,(_,k)=>vars[k]??'');}
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
function pageUrl(extraDate){
let u='?group_id='+encodeURIComponent(groupId);
const d=extraDate!==undefined?extraDate:currentDate;
if(d){u+='&date='+encodeURIComponent(d);}
return u;
}
function goDate(d){location.href=pageUrl(d);}
function applyStaticLabels(){
document.title=labels.page_title||'Bill';
document.getElementById('dashboard-title').textContent=labels.dashboard_title||'';
document.getElementById('group-text').textContent=fmt(labels.group_id||'',{id:groupId});
document.getElementById('date-label').textContent=labels.date_label||'';
btnPrev.textContent=labels.btn_prev||'';
btnNext.textContent=labels.btn_next||'';
document.getElementById('btn-all').textContent=labels.btn_all||'';
document.getElementById('category-title').textContent=labels.category_title||'';
['date','time','remark','rmb','u','op'].forEach(k=>{
const el=document.getElementById('th-inc-'+k);
if(el)el.textContent=labels['th_'+k]||'';
});
['date','time','remark','usdt','op'].forEach(k=>{
const el=document.getElementById('th-exp-'+k);
if(el)el.textContent=labels['th_'+k]||'';
});
document.getElementById('th-cate-remark').textContent=labels.th_remark||'';
document.getElementById('th-cate-rmb').textContent=labels.th_rmb||'';
document.getElementById('th-cate-u').textContent=labels.th_u||'';
document.getElementById('th-cate-count').textContent=labels.th_count||'';
document.getElementById('card-rate').textContent=labels.card_rate||'';
document.getElementById('card-total-rmb').textContent=labels.card_total_rmb||'';
document.getElementById('card-total-usdt').textContent=labels.card_total_usdt||'';
document.getElementById('card-expense-usdt').textContent=labels.card_expense_usdt||'';
document.getElementById('card-remaining-usdt').textContent=labels.card_remaining_usdt||'';
document.querySelectorAll('.lang-btn').forEach(btn=>{
btn.classList.toggle('active', btn.dataset.lang===currentLang);
});
}
async function setLanguage(lang){
if(!groupId||groupId==='0')return;
await fetch('/api/set-language?group_id='+encodeURIComponent(groupId)+'&lang='+encodeURIComponent(lang),{method:'POST'});
location.href=pageUrl(currentDate||'');
}
document.querySelectorAll('.lang-btn').forEach(btn=>{btn.onclick=()=>setLanguage(btn.dataset.lang);});
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
const r=await fetch('/api/bill?group_id='+encodeURIComponent(groupId)+'&date='+encodeURIComponent(d));
const data=await r.json();
if(data.server_today && !params.get('date')){goDate(data.server_today);return;}
window.__serverToday=data.server_today||localToday();
labels=data.labels||{};
currentLang=data.language||'zh';
applyStaticLabels();
const viewDay=(d==='all')?window.__serverToday:d;
btnPrev.disabled=false;
btnNext.disabled=(viewDay>=window.__serverToday);
const summaryPart=(d==='all'?labels.summary_all:fmt(labels.summary_day,{date:d}));
document.getElementById('summary-text').textContent=
summaryPart+' · '+labels.income_unit+' '+data.income_count+' '+labels.count_unit
+' · '+labels.expense_unit+' '+data.expense_count+' '+labels.count_unit;
document.getElementById('income-title').textContent=fmt(labels.income_title,{n:data.income_count});
document.getElementById('expense-title').textContent=fmt(labels.expense_title,{n:data.expense_count});
['rate','total_rmb'].forEach(k=>document.getElementById(k).textContent=data[k]);
document.getElementById('total_usdt').textContent=data.total_usdt+' U';
document.getElementById('expense_usdt').textContent=data.expense_usdt+' U';
document.getElementById('remaining_usdt').textContent=data.remaining_usdt+' U';
const tags=document.getElementById('date-tags');
tags.innerHTML=(data.available_dates||[]).map(x=>{
const active=(d===x.date)?' active':'';
return '<a class="date-tag'+active+'" href="'+pageUrl(x.date)+'">'
+x.date+' ('+x.income+'/'+x.expense+')</a>';
}).join('');
const noData=labels.no_data||'';
document.getElementById('cate-list').innerHTML=(data.category_summary||[]).length
?data.category_summary.map(c=>'<tr><td><span class="badge bg-inc c-remark">'+c.remark+'</span></td><td><span class="c-rmb">'+c.total_rmb+'</span></td><td class="c-u">'+c.total_usdt+' U</td><td>'+c.count+'</td></tr>').join('')
:'<tr><td colspan="4" style="text-align:center;color:#94a3b8">'+noData+'</td></tr>';
document.getElementById('income-list').innerHTML=(data.income_bills||[]).length
?data.income_bills.map(b=>'<tr><td>'+b.date+'</td><td>'+b.time+'</td><td><span class="c-remark">'+b.remark+'</span></td><td><span class="c-rmb">+'+b.amount+'</span></td><td class="c-u">'+b.usdt+' U</td><td><span class="c-op">'+b.username+'</span></td></tr>').join('')
:'<tr><td colspan="6" style="text-align:center;color:#94a3b8">'+(labels.no_income||noData)+'</td></tr>';
document.getElementById('expense-list').innerHTML=(data.expense_bills||[]).length
?data.expense_bills.map(e=>'<tr><td>'+e.date+'</td><td>'+e.time+'</td><td><span class="c-remark">'+e.remark+'</span></td><td class="c-u">-'+e.usdt+' U</td><td><span class="c-op">'+e.username+'</span></td></tr>').join('')
:'<tr><td colspan="5" style="text-align:center;color:#94a3b8">'+(labels.no_expense||noData)+'</td></tr>';
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

    lang = resolve_web_lang(group_id, request.args.get("lang"))
    empty_remark = web_tr(lang, "no_remark")
    unknown_user = {"zh": "未知", "eng": "Unknown", "my": "မသိ"}[lang]

    tz = get_setting(group_id, "timezone") or "Asia/Shanghai"
    now, _, _ = get_current_time(tz)
    server_today = now.strftime("%Y-%m-%d")
    target_date = request.args.get("date") or server_today

    income, expense, total_income, total_expense = get_class_bills_by_date(group_id, target_date)
    income_rate = get_effective_rate(group_id, "income")
    expense_rate = get_effective_rate(group_id, "expense")
    total_rmb = (total_income[0] or 0) if total_income else 0
    total_usdt = (total_income[1] or 0) if total_income else 0
    expense_usdt = (total_expense[0] or 0) if total_expense else 0

    income_bills = [
        {
            "remark": r[0] or empty_remark,
            "username": r[1] or unknown_user,
            "amount": f"{r[2]:.0f}",
            "usdt": f"{r[3]:.2f}",
            "time": r[5][11:19] if r[5] else "",
            "date": r[6] if len(r) > 6 else target_date,
        }
        for r in income
    ]
    expense_bills = [
        {
            "remark": r[0] or empty_remark,
            "username": r[1] or unknown_user,
            "usdt": f"{r[2]:.2f}",
            "time": r[4][11:19] if r[4] else "",
            "date": r[5] if len(r) > 5 else target_date,
        }
        for r in expense
    ]

    summary = {}
    for row in income:
        rem = _remark_for_category(row[0])
        if not rem:
            continue
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
        "exchange_rate": f"{income_rate:.2f}",
        "income_rate": f"{income_rate:.2f}",
        "expense_rate": f"{expense_rate:.2f}",
        "rate": f"{income_rate:.2f}",
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
        "language": lang,
        "labels": WEB_TEXTS.get(lang, WEB_TEXTS["zh"]),
    })


@flask_app.route("/api/set-language", methods=["POST"])
def api_set_language():
    try:
        group_id = int(request.args.get("group_id", "0").strip())
    except ValueError:
        return jsonify({"ok": False, "error": "invalid group_id"}), 400
    if not group_id:
        return jsonify({"ok": False, "error": "group_id required"}), 400
    lang = normalize_lang_code(request.args.get("lang", "zh"))
    if lang not in SUPPORTED_LANGS:
        return jsonify({"ok": False, "error": "invalid lang"}), 400
    update_setting(group_id, "language", lang)
    return jsonify({"ok": True, "language": lang})


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
    """私聊左侧 Menu（☰）显示 /start、/help；群内不显示 slash 命令提示。"""
    commands = [
        telebot.types.BotCommand("start", "打开主菜单"),
        telebot.types.BotCommand("help", "使用帮助"),
    ]
    try:
        bot.set_my_commands(commands, scope=telebot.types.BotCommandScopeAllPrivateChats())
        bot.set_my_commands([], scope=telebot.types.BotCommandScopeAllGroupChats())
        bot.set_my_commands([], scope=telebot.types.BotCommandScopeDefault())
        bot.set_chat_menu_button(menu_button=telebot.types.MenuButtonCommands())
        me = bot.get_me()
        log.info("Bot menu OK (@%s): private /start, /help; groups cleared", me.username)
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
    log.info("Starting xiaocaicai_plus on 0.0.0.0:%s  WEBHOOK_URL=%s  DB=%s", PORT, WEBHOOK_URL, DATABASE_PATH)
    setup_webhook()
    flask_app.run(host="0.0.0.0", port=PORT)
