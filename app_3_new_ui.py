from flask import Flask, request, redirect, url_for, session, send_file, jsonify
from functools import wraps
import base64
import hashlib
import io
import json
import math
import os
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from datetime import date, datetime, timedelta
from markupsafe import escape

import pymysql
import pymysql.converters
import pymysql.cursors
from pymysql.constants import FIELD_TYPE
from pymysql.err import IntegrityError

app = Flask(__name__)
app.secret_key = "ocean-restaurant-secret-key-change-me"


# =========================================================
# CẤU HÌNH KẾT NỐI MYSQL
# =========================================================
# Đổi thông tin kết nối bằng biến môi trường, không cần sửa code:
#   DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "root"),
    "password": "Ly@P4m!209#Mx26",
    "database": os.environ.get("DB_NAME", "ocean_restaurant"),
}

BASE_DIR = Path(__file__).resolve().parent
# File SQL chứa cấu trúc bảng + dữ liệu (đặt cùng thư mục với file .py này).
SQL_FILE = BASE_DIR / os.environ.get("SQL_FILE", "ocean_restaurant.sql")

# Thông tin in trên hóa đơn bán hàng - sửa lại cho đúng với nhà hàng.
RESTAURANT_INFO = {
    "name": "OCEAN RESTAURANT",
    "address": "Địa chỉ: ........................................",
    "phone": "Hotline: ..............",
    "tax_code": "MST: ..............",
}

# Tài khoản nhận chuyển khoản in kèm mã QR trên hóa đơn.
# bank_bin là mã BIN NAPAS của ngân hàng (Agribank = 970405). account_name phải đúng tên chủ tài khoản
# do ngân hàng xác thực; display_name chỉ là tên cửa hàng in cạnh QR.
PAYMENT_INFO = {
    "display_name": RESTAURANT_INFO["name"],
    "bank_name": "Agribank",
    "bank_bin": "970405",
    "account_no": "3506205314839",
    "account_name": "PHAM HA LY",
}
# =========================================================
# TÊN KHÓA CỦA TỪNG BẢNG
# =========================================================
# Quy ước: khóa chính của bảng X tên là <X_số_ít>_id, và khóa ngoại ở bảng khác
# dùng ĐÚNG tên đó. Ví dụ items.category_id  ->  categories.category_id
TABLE_KEYS = {
    "categories": "category_id",
    "items": "item_id",
    "ingredients": "ingredient_id",
    "recipes": "recipe_id",
    "promotions": "promotion_id",
    "member_tiers": "member_tier_id",
    "customers": "customer_id",
    "users": "user_id",
    "orders": "order_id",
    "order_details": "order_detail_id",
    "inventory_movements": "movement_id",
}

# Số chữ số của phần số trong mã (DM01, KH001, DH037...).
CODE_WIDTH = {"recipes": 3, "orders": 3, "order_details": 3, "customers": 3}


# =========================================================
# DATABASE
# =========================================================

class Row(dict):
    """Một dòng kết quả: truy cập theo tên cột row["name"] hoặc theo vị trí row[0]."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class RowCursor(pymysql.cursors.DictCursor):
    dict_type = Row


def _decimal_to_number(text):
    """DECIMAL của MySQL -> int nếu là số nguyên, ngược lại float (code bên dưới tính toán bằng số thường)."""
    value = Decimal(text)
    return int(value) if value == value.to_integral_value() else float(value)


def _keep_text(text):
    return text


def _build_converters():
    conv = pymysql.converters.conversions.copy()
    conv[FIELD_TYPE.DECIMAL] = _decimal_to_number
    conv[FIELD_TYPE.NEWDECIMAL] = _decimal_to_number
    # Ngày/giờ trả về dạng chuỗi 'YYYY-MM-DD HH:MM:SS' để các hàm xử lý ngày hiện có dùng tiếp.
    conv[FIELD_TYPE.DATETIME] = _keep_text
    conv[FIELD_TYPE.TIMESTAMP] = _keep_text
    conv[FIELD_TYPE.DATE] = _keep_text
    return conv


CONVERTERS = _build_converters()


class Database:
    """Lớp bọc kết nối MySQL: giữ cách viết conn.execute(sql, params) với dấu ? như trước."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=None):
        cursor = self.raw.cursor()
        cursor.execute(sql.replace("?", "%s"), tuple(params) if params else None)
        return cursor

    def executemany(self, sql, rows):
        cursor = self.raw.cursor()
        cursor.executemany(sql.replace("?", "%s"), [tuple(r) for r in rows])
        return cursor

    def begin(self):
        self.raw.begin()

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def get_db():
    raw = pymysql.connect(
        **DB_CONFIG,
        charset="utf8mb4",
        cursorclass=RowCursor,
        conv=CONVERTERS,
        autocommit=False,
    )
    return Database(raw)


def ensure_database():
    """Tạo database nếu chưa có."""
    config = dict(DB_CONFIG)
    db_name = config.pop("database")
    raw = pymysql.connect(**config, charset="utf8mb4", autocommit=True)
    try:
        with raw.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        raw.close()


def hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def next_code(conn, table, prefix):
    """Tạo mã chuỗi theo tiền tố, ví dụ DM10, KH003, DH037."""
    key = TABLE_KEYS[table]
    rows = conn.execute(
        f"SELECT `{key}` FROM `{table}` WHERE `{key}` LIKE ?", (prefix + "%",)
    ).fetchall()
    numbers = []
    for row in rows:
        code = str(row[key] or "")
        suffix = code[len(prefix):]
        if suffix.isdigit():
            numbers.append(int(suffix))
    width = CODE_WIDTH.get(table, 2)
    return f"{prefix}{(max(numbers, default=0) + 1):0{width}d}"


def item_prefix(category_name):
    mapping = {
        "Khai vị": "KH", "Hải sản": "HS", "Bánh mì": "BM", "Cơm": "CM",
        "Món rau và phô mai": "RP", "Nước sốt và gia vị": "SG", "Món gà": "GA",
        "Món thịt": "MT", "Đồ uống": "DU",
    }
    return mapping.get(category_name, "MH")


def next_item_code(conn, category_id):
    row = conn.execute("SELECT name FROM categories WHERE category_id = ?", (category_id,)).fetchone()
    return next_code(conn, "items", item_prefix(row["name"] if row else ""))


# ---------------------------------------------------------
# Nạp file SQL (cấu trúc + dữ liệu)
# ---------------------------------------------------------

def split_sql_statements(text):
    """Tách nội dung file SQL thành từng câu lệnh.

    Bỏ qua chú thích (-- ..., # ..., /* ... */) và không cắt nhầm dấu ; nằm trong chuỗi '...'.
    """
    statements, buf = [], []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:          # ký tự thoát: \' hoặc \\
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":   # '' = dấu nháy đơn nằm trong chuỗi
                    buf.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
        elif (text.startswith("--", i) and (i + 2 >= n or text[i + 2] in " \t\r\n")) or ch == "#":
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def import_sql_file(path):
    """Chạy toàn bộ file .sql (tạo bảng + nạp dữ liệu) vào database đang kết nối. Trả về True nếu thành công.

    File viết theo dạng INSERT ... ON DUPLICATE KEY UPDATE nên chạy lại nhiều lần được:
    mã đã có thì cập nhật, mã mới thì thêm.
    """
    path = Path(path)
    if not path.is_file() and (BASE_DIR / path).is_file():
        path = BASE_DIR / path
    if not path.is_file():
        print(f"Không tìm thấy file SQL: {path}")
        return False

    statements = split_sql_statements(path.read_text(encoding="utf-8-sig"))
    conn = get_db()
    executed = 0
    try:
        cursor = conn.raw.cursor()      # chạy nguyên văn, không qua bộ đổi dấu ?
        for statement in statements:
            head = statement.lstrip().upper()
            # Database đã được chọn khi kết nối (DB_NAME) nên bỏ qua 2 lệnh này.
            if head.startswith("CREATE DATABASE") or head.startswith("USE "):
                continue
            try:
                cursor.execute(statement)
            except Exception as exc:
                conn.rollback()
                print(f"Lỗi khi chạy file {path.name}, tại câu lệnh:")
                print("   " + " ".join(statement.split())[:150] + " ...")
                print(f"   -> {exc}")
                print("Sửa lỗi trong file rồi chạy lại (file chạy lại nhiều lần được).")
                return False
            executed += 1
        conn.commit()
    finally:
        conn.close()
    print(f"Đã nạp file {path.name}: {executed} câu lệnh. "
          f"Database: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    return True


def database_needs_setup():
    """True nếu chưa có bảng hoặc chưa có dữ liệu (lần chạy đầu tiên)."""
    conn = get_db()
    try:
        if not conn.execute("SHOW TABLES LIKE 'categories'").fetchone():
            return True
        return conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0
    finally:
        conn.close()


def ensure_schema(conn):
    """Thêm cột mới khi chạy bản cập nhật (an toàn nếu đã có)."""
    cols = {r["Field"] for r in conn.execute("SHOW COLUMNS FROM ingredients").fetchall()}
    if "expiry_date" not in cols:
        conn.execute("ALTER TABLE ingredients ADD COLUMN expiry_date DATE NULL")
        # Dữ liệu mẫu ban đầu để thấy chức năng; sửa lại ở Quản lý nguyên liệu > Danh mục.
        demo_days = {"Rohu Fish": 1, "Chicken": 2, "Spinach": 3, "Milk": 6, "King Prawn": 5,
                     "Mutton": 10, "Tomato": 12, "Mushroom": 14, "Paneer": 20, "Eggs": 25}
        for ing in conn.execute("SELECT ingredient_id, name FROM ingredients").fetchall():
            days = demo_days.get(ing["name"], 120)
            conn.execute("UPDATE ingredients SET expiry_date = ? WHERE ingredient_id = ?",
                         ((date.today() + timedelta(days=days)).isoformat(), ing["ingredient_id"]))
    ucols = {r["Field"] for r in conn.execute("SHOW COLUMNS FROM users").fetchall()}
    if "extra_permissions" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN extra_permissions VARCHAR(500) NULL")
    # Lô nguyên liệu theo hạn sử dụng: mỗi dòng = số lượng của một nguyên liệu hết hạn vào một ngày.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingredient_batches (
            batch_id      INT           NOT NULL AUTO_INCREMENT,
            ingredient_id VARCHAR(20)   NOT NULL,
            quantity      DECIMAL(14,4) NOT NULL DEFAULT 0,
            expiry_date   DATE          NULL,
            created_at    DATETIME      NOT NULL,
            PRIMARY KEY (batch_id),
            KEY idx_batches_ingredient (ingredient_id, expiry_date)
        )
    """)
    reconcile_batches(conn)
    apply_member_rules(conn)
    conn.commit()


def init_db():
    """Bảo đảm luôn có tài khoản admin để đăng nhập (admin / admin123 nếu file SQL không có)."""
    conn = get_db()
    try:
        ensure_schema(conn)
        if not conn.execute("SELECT user_id FROM users WHERE username = 'admin'").fetchone():
            conn.execute("""
                INSERT INTO users(user_id, username, password_hash, full_name, role, active, created_at)
                VALUES(?, ?, ?, ?, ?, 1, ?)
            """, (next_code(conn, "users", "ND"), "admin", hash_password("admin123"),
                  "Quản trị viên", "admin", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =========================================================
# HELPER
# =========================================================

def inventory_status(quantity, minimum):
    if quantity <= 0:
        return "Hết hàng"
    if quantity <= minimum:
        return "Sắp hết"
    return "Còn hàng"


# =========================================================
# KHUYẾN MẠI - PHÂN LOẠI VÀ TRẠNG THÁI
# =========================================================

# Khai báo ở phạm vi toàn cục để các hàm đều sử dụng được
MEMBER_KEYWORDS = (
    "thành viên",
    "hạng",
    "member",
    "vip"
)

HOLIDAY_KEYWORDS = (
    "lễ",
    "quốc tế",
    "quốc khánh",
    "giáng sinh",
    "noel",
    "tết",
    "phụ nữ",
    "thiếu nhi",
    "trung thu",
    "valentine",
    "30/4",
    "1/5"
)

TIER_KEYWORDS = {
    "Kim cương": ("kim cương", "diamond"),
    "Vàng": ("vàng", "gold"),
    "Bạc": ("bạc", "silver"),
    "Đồng": ("đồng", "bronze"),
    "VIP": ("vip",)
}


def promo_date(value):
    """Chuẩn hóa ngày khuyến mại về định dạng YYYY-MM-DD."""
    return str(value or "")[:10]


def promotion_status(start_date, end_date):
    """Xác định trạng thái của chương trình khuyến mại."""
    today = date.today().isoformat()
    start = promo_date(start_date)
    end = promo_date(end_date)

    if today < start:
        return "Sắp diễn ra"

    if today > end:
        return "Đã kết thúc"

    return "Đang diễn ra"


def promo_group(p):
    """Phân loại chương trình khuyến mại."""
    name = (p["name"] or "").lower()

    # Khuyến mại dành cho thành viên
    if any(keyword in name for keyword in MEMBER_KEYWORDS):
        return "Hạng thành viên"

    # Khuyến mại vào dịp lễ
    if any(keyword in name for keyword in HOLIDAY_KEYWORDS):
        return "Ngày lễ"

    # Khuyến mại theo món ăn
    if p["apply_type"] == "item":
        return "Theo món"

    # Khuyến mại theo hóa đơn
    return "Theo hóa đơn"


def promo_tier(p, group):
    """Xác định hạng thành viên được áp dụng khuyến mại."""
    if group != "Hạng thành viên":
        return "—"

    name = (p["name"] or "").lower()

    for tier, keywords in TIER_KEYWORDS.items():
        if any(keyword in name for keyword in keywords):
            return tier

    return "Mọi hạng"


def promo_condition_key(p):
    """Xác định loại điều kiện áp dụng khuyến mại."""
    if p["apply_type"] == "item":
        return "item"

    if float(p["min_order_value"] or 0) > 0:
        return "min_order"

    return "none"


def promo_condition_label(p):
    """Tạo nội dung mô tả điều kiện khuyến mại."""
    key = promo_condition_key(p)

    if key == "item":
        return f'Áp dụng cho món: {p["item_name"] or p["item_id"]}'

    if key == "min_order":
        return f'Đơn từ {float(p["min_order_value"]):,.0f} đ'

    return "Không điều kiện"


def promo_condition_key(p):
    if p["apply_type"] == "item":
        return "item"
    return "min_order" if float(p["min_order_value"] or 0) > 0 else "none"


def promo_condition_label(p):
    key = promo_condition_key(p)
    if key == "item":
        return f'Áp dụng cho món: {p["item_name"] or p["item_id"]}'
    if key == "min_order":
        return f'Đơn từ {float(p["min_order_value"]):,.0f} đ'
    return "Không điều kiện"


# ---------------------------------------------------------
# Hạng thành viên: ngưỡng chi tiêu và ưu đãi theo hạng
# ---------------------------------------------------------
BASE_TIER_ID = "HT00"            # khách đã đăng ký nhưng chưa đủ chi tiêu để lên hạng Bạc
SILVER_MIN_SPENT = 10000000      # chi tiêu tối thiểu của hạng Bạc
# Ba chương trình mẫu "Ưu đãi hóa đơn 5/10/15%" thực chất là ưu đãi theo hạng thành viên.
TIER_PROMO_RENAMES = {
    "Ưu đãi hóa đơn 5%": "Ưu đãi hạng Bạc 5%",
    "Ưu đãi hóa đơn 10%": "Ưu đãi hạng Vàng 10%",
    "Ưu đãi hóa đơn 15%": "Ưu đãi hạng Kim cương 15%",
}


def tier_for_spent(conn, spent):
    """Mã hạng thành viên ứng với tổng chi tiêu (đọc ngưỡng từ bảng member_tiers)."""
    row = conn.execute(
        "SELECT member_tier_id FROM member_tiers WHERE min_total_spent <= ? ORDER BY min_total_spent DESC LIMIT 1",
        (float(spent or 0),)).fetchone()
    if not row:
        row = conn.execute("SELECT member_tier_id FROM member_tiers ORDER BY min_total_spent LIMIT 1").fetchone()
    return row["member_tier_id"] if row else BASE_TIER_ID


def member_promo_usage(conn, promos, start, end):
    """{promotion_id: {n, disc, rev}} cho các chương trình gắn hạng thành viên: tính theo đơn của khách
    thuộc đúng hạng đó, trong thời gian chương trình và khoảng ngày báo cáo."""
    usage = {}
    for p in promos:
        if p["apply_type"] != "order" or promo_group(p) != "Hạng thành viên":
            continue
        tier = promo_tier(p, "Hạng thành viên")
        sql = """SELECT COUNT(*) AS n, COALESCE(SUM(member_discount), 0) AS disc, COALESCE(SUM(final_total), 0) AS rev
                 FROM orders
                 WHERE status != 'Đã hủy' AND member_discount > 0
                   AND date(order_date) BETWEEN ? AND ?
                   AND date(order_date) BETWEEN ? AND ?"""
        params = [start, end, promo_date(p["start_date"]), promo_date(p["end_date"])]
        if tier != "Mọi hạng":
            sql += " AND member_tier_name = ?"
            params.append(tier)
        r = conn.execute(sql, params).fetchone()
        usage[p["promotion_id"]] = {"n": int(r["n"] or 0), "disc": float(r["disc"] or 0), "rev": float(r["rev"] or 0)}
    return usage


def apply_member_rules(conn):
    """Cập nhật dữ liệu theo quy định mới (chạy lại nhiều lần được, lần sau không đổi gì thêm):
    1. Ba chương trình "Ưu đãi hóa đơn 5/10/15%" thành ưu đãi hạng Bạc/Vàng/Kim cương; đơn cũ đã lấy
       nhầm mức 15% chung được tính lại theo đúng hạng của khách lúc đó.
    2. Hạng Bạc cần chi tiêu từ 10.000.000 đ; dưới mức đó là "Thành viên" (chưa có ưu đãi).
    3. Xếp lại hạng khách theo tổng chi tiêu và tính lại các đơn chưa hoàn thành."""
    for old, new in TIER_PROMO_RENAMES.items():
        conn.execute("UPDATE promotions SET name = ? WHERE name = ? AND apply_type = 'order'", (new, old))

    tier_promos = [p["promotion_id"] for p in conn.execute(
        "SELECT * FROM promotions WHERE apply_type = 'order'").fetchall() if promo_group(p) == "Hạng thành viên"]
    open_orders = set()
    if tier_promos:
        marks = ",".join(["?"] * len(tier_promos))
        affected = conn.execute(f"""
            SELECT o.order_id, o.status, o.customer_id, o.total, o.discount_amount, o.final_total,
                   o.member_discount, t.tier_name, t.discount_percent
            FROM orders o
            LEFT JOIN customers c ON c.customer_id = o.customer_id
            LEFT JOIN member_tiers t ON t.member_tier_id = c.member_tier_id
            WHERE o.promotion_id IN ({marks})""", tier_promos).fetchall()
        delta = {}
        for o in affected:
            if o["status"] not in ("Đã hoàn thành", "Đã hủy"):
                conn.execute("UPDATE orders SET promotion_id = NULL WHERE order_id = ?", (o["order_id"],))
                open_orders.add(o["order_id"])
                continue
            item_disc = float(conn.execute(
                "SELECT COALESCE(SUM(item_discount), 0) AS d FROM order_details WHERE order_id = ?",
                (o["order_id"],)).fetchone()["d"] or 0)
            total = float(o["total"] or 0)
            after_item = max(0.0, total - item_disc)
            pct = float(o["discount_percent"] or 0) if o["customer_id"] else 0.0
            member = after_item * pct / 100
            final = max(0.0, total - item_disc - member)
            conn.execute("""UPDATE orders SET promotion_id = NULL, discount_amount = ?, final_total = ?,
                            member_discount = ?, member_tier_name = ? WHERE order_id = ?""",
                         (item_disc + member, final, member, o["tier_name"] if member > 0 else None, o["order_id"]))
            if o["status"] == "Đã hoàn thành" and o["customer_id"]:
                delta[o["customer_id"]] = delta.get(o["customer_id"], 0.0) + final - float(o["final_total"] or 0)
        for customer_id, diff in delta.items():
            conn.execute("UPDATE customers SET total_spent = GREATEST(0, total_spent + ?) WHERE customer_id = ?",
                         (diff, customer_id))

    silver = conn.execute("SELECT * FROM member_tiers WHERE tier_name = 'Bạc'").fetchone()
    if silver and float(silver["min_total_spent"] or 0) < SILVER_MIN_SPENT:
        conn.execute("UPDATE member_tiers SET min_total_spent = ? WHERE member_tier_id = ?",
                     (SILVER_MIN_SPENT, silver["member_tier_id"]))
    if silver and not conn.execute("SELECT 1 FROM member_tiers WHERE member_tier_id = ?", (BASE_TIER_ID,)).fetchone():
        conn.execute("""INSERT INTO member_tiers(member_tier_id, tier_name, min_total_spent, max_total_spent,
                                                 discount_percent, description)
                        VALUES(?, 'Thành viên', 0, ?, 0, 'Đã đăng ký, chưa đạt hạng Bạc – chưa có ưu đãi')""",
                     (BASE_TIER_ID, SILVER_MIN_SPENT - 1))

    for c in conn.execute("SELECT customer_id, total_spent, member_tier_id FROM customers").fetchall():
        tier_id = tier_for_spent(conn, c["total_spent"])
        if tier_id != c["member_tier_id"]:
            conn.execute("UPDATE customers SET member_tier_id = ? WHERE customer_id = ?", (tier_id, c["customer_id"]))
    for order_id in open_orders:
        calculate_order_totals(conn, order_id)


def sel(a, b):
    return "selected" if a == b else ""


def css_status(status):
    return {
        "Còn hàng": "green",
        "Sắp hết": "yellow",
        "Hết hàng": "red",
        "Đang diễn ra": "green",
        "Sắp diễn ra": "yellow",
        "Đã kết thúc": "red",
        "Chờ xác nhận": "gray",
        "Đã xác nhận": "blue",
        "Đang chuẩn bị": "yellow",
        "Đã hoàn thành": "green",
        "Đã hủy": "red",
        "Hết hạn": "red",
        "Sắp hết hạn": "yellow",
        "Còn hạn": "green",
        "Chưa khai báo": "gray",
    }.get(status, "gray")


def parse_order_time(order_date):
    return datetime.strptime(order_date, "%Y-%m-%d %H:%M:%S")


def cancel_deadline(order_date):
    return parse_order_time(order_date) + timedelta(minutes=10)


def can_customer_cancel(order):
    if order["status"] in ("Đã hoàn thành", "Đã hủy"):
        return False
    return datetime.now() <= cancel_deadline(order["order_date"])

FOOD_IMAGE_DEFAULT = "https://images.unsplash.com/photo-1547592180-85f173990554?auto=format&fit=crop&w=160&h=120&q=80"
FOOD_IMAGES = {
    "DM01": "https://images.unsplash.com/photo-1547592180-85f173990554?auto=format&fit=crop&w=160&h=120&q=80",
    "DM02": "https://images.unsplash.com/photo-1559737558-2f5a35f4523b?auto=format&fit=crop&w=160&h=120&q=80",
    "DM03": "https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=160&h=120&q=80",
    "DM04": "https://images.unsplash.com/photo-1512058564366-18510be2db19?auto=format&fit=crop&w=160&h=120&q=80",
    "DM05": "https://images.unsplash.com/photo-1512621776951-a57141f2e3c1?auto=format&fit=crop&w=160&h=120&q=80",
    "DM06": "https://images.unsplash.com/photo-1472476443507-c7a5948772fc?auto=format&fit=crop&w=160&h=120&q=80",
    "DM07": "https://images.unsplash.com/photo-1532550907401-a500c9a57435?auto=format&fit=crop&w=160&h=120&q=80",
    "DM08": "https://images.unsplash.com/photo-1544025162-d76694265947?auto=format&fit=crop&w=160&h=120&q=80",
    "DM09": "https://images.unsplash.com/photo-1513558161293-cdaf765edfd7?auto=format&fit=crop&w=160&h=120&q=80",
}


def get_best_sellers(conn, limit=5, days=30):
    """Top món bán chạy (không tính đơn đã hủy). Ưu tiên `days` ngày gần nhất;
    nếu khoảng đó chưa có đơn nào thì tính từ trước đến nay."""
    base = """
        SELECT items.item_id, items.name, items.price, items.category_id,
               categories.name AS category_name,
               SUM(od.quantity - COALESCE(od.canceled_quantity, 0)) AS sold
        FROM order_details od
        JOIN orders o ON o.order_id = od.order_id
        JOIN items ON items.item_id = od.item_id
        JOIN categories ON categories.category_id = items.category_id
        WHERE o.status != 'Đã hủy' AND items.status = 'Đang bán'
          AND (od.quantity - COALESCE(od.canceled_quantity, 0)) > 0
    """
    tail = """
        GROUP BY items.item_id, items.name, items.price, items.category_id, categories.name
        ORDER BY sold DESC, items.name
        LIMIT ?
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(base + " AND date(o.order_date) >= ? " + tail, (since, limit)).fetchall()
    if rows:
        return rows, f"{days} ngày gần nhất"
    return conn.execute(base + tail, (limit,)).fetchall(), "từ trước đến nay"


def get_order_screen_context(conn):
    best, label = get_best_sellers(conn)
    rank_map = {b["item_id"]: i + 1 for i, b in enumerate(best)}
    blocked = get_unavailable_item_ids(conn)
    sale = get_expiry_item_discounts(conn)
    return best, label, rank_map, blocked, sale


FOOD_PLACEHOLDER = "data:image/svg+xml," + __import__("urllib.parse", fromlist=["quote"]).quote(
    '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#eef2f7"/>'
    '<circle cx="40" cy="40" r="15" fill="none" stroke="#b6c1d1" stroke-width="3"/><circle cx="40" cy="40" r="7" fill="#d5dce7"/></svg>')


def food_thumb_html(category_id, name=""):
    src = FOOD_IMAGES.get(category_id, FOOD_IMAGE_DEFAULT)
    return (f'<img class="food-thumb" src="{src}" alt="Ảnh món {escape(name)}" loading="lazy" '
            f'onerror="this.onerror=null;this.src=\'{FOOD_PLACEHOLDER}\'">')


def _hot_points():
    pts = []
    for i in range(28):
        ang = math.pi * 2 * i / 28 - math.pi / 2
        r = (1.0 if i % 4 == 0 else 0.86) if i % 2 == 0 else 0.6
        pts.append(f"{50 + 49 * r * math.cos(ang):.1f},{30 + 29 * r * math.sin(ang):.1f}")
    return " ".join(pts)


HOT_BADGE_SVG = (
    '<svg class="hot-badge" viewBox="0 0 100 60" width="{w}" height="{h}" role="img" aria-label="Hot" '
    'style="vertical-align:middle;flex:none;overflow:visible;">'
    '<title>{title}</title>'
    f'<polygon points="{_hot_points()}" fill="#94a3b8" opacity=".55" transform="translate(2.5,3)"/>'
    f'<polygon points="{_hot_points()}" fill="#e11d1d" stroke="#7f1d1d" stroke-width="1"/>'
    '<text x="50" y="37" text-anchor="middle" font-family="Impact,Arial Black,Arial,sans-serif" font-size="19" '
    'font-weight="900" fill="#fde047" stroke="#7f1d1d" stroke-width=".7" paint-order="stroke">Hot !!!</text></svg>')


def hot_badge_html(title="", width=72):
    """Biểu tượng "Hot !!!" (ngôi sao nổ đỏ, chữ vàng) cho các món đang giảm mức cao nhất."""
    return HOT_BADGE_SVG.format(w=width, h=round(width * 0.6), title=escape(title))


def is_hot_discount(pct):
    """Mức giảm xả hàng cao nhất (30%) được gắn biểu tượng Hot thay cho nhãn số ngày còn lại."""
    return float(pct or 0) >= EXPIRY_DISCOUNT_RULES[0][1]


def item_flags_html(item_id, rank_map=None, blocked=None, sale=None):
    html = ""
    if rank_map and item_id in rank_map:
        html += f' <span class="status yellow">Bán chạy #{rank_map[item_id]}</span>'
    if sale and item_id in sale:
        s = sale[item_id]
        tip = f'Nguyên liệu {s["ingredient_name"]} {days_text(s["days_left"])}'
        if is_hot_discount(s["discount"]):
            html += " " + hot_badge_html(tip, 58)
        html += (f' <span class="status green" title="{escape(tip)}">Giảm {s["discount"]:g}% (xả HSD)</span>')
    if blocked and item_id in blocked:
        html += ' <span class="status red">Hết nguyên liệu</span>'
    return html


def best_seller_panel_html(best, label, blocked=None, sale=None):
    if not best:
        return ""
    blocked = blocked or set()
    cards = ""
    for idx, b in enumerate(best):
        rank = ["🥇", "🥈", "🥉"][idx] if idx < 3 else f"#{idx + 1}"
        img = FOOD_IMAGES.get(b["category_id"], FOOD_IMAGE_DEFAULT)
        warn = ""
        if b["item_id"] in blocked:
            warn += '<div><span class="status red">Hết nguyên liệu</span></div>'
        if sale and b["item_id"] in sale:
            warn += f'<div><span class="status green">Giảm {sale[b["item_id"]]["discount"]:g}% xả HSD</span></div>'
        cards += f"""
        <div class="bs-card">
            <img class="bs-img" src="{img}" alt="{escape(b["name"])}" loading="lazy" onerror="this.onerror=null;this.src='{FOOD_PLACEHOLDER}'">
            <div class="bs-body">
                <div class="bs-rank">{rank}</div>
                <div class="food-name">{escape(b["name"])}</div>
                <div class="muted">{b["price"]:,.0f} đ · Đã bán <strong>{int(b["sold"])}</strong> phần</div>
                {warn}
                <button type="button" class="btn btn-primary bs-btn" onclick="bestAdd('{escape(b["item_id"])}', this)" {'disabled title="Món tạm ngừng đặt do không đủ nguyên liệu còn hạn"' if b["item_id"] in blocked else ""}>{"Tạm hết món" if b["item_id"] in blocked else "+ Thêm 1"}</button>
            </div>
        </div>"""
    style = """
    <style>
      .bs-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:12px; margin-top:12px; }
      .bs-card { display:flex; gap:10px; border:1px solid #e2e8f0; border-radius:12px; padding:10px; background:#fff; }
      .bs-img { width:64px; height:64px; object-fit:cover; border-radius:10px; flex:none; background:#e0f2fe; }
      .bs-body { min-width:0; }
      .bs-rank { font-size:20px; font-weight:800; line-height:1.2; }
      .bs-btn { margin-top:8px; padding:6px 10px; }
    </style>
    <script>
      function bestAdd(id, btn) {
        var el = document.querySelector('input[name="qty_' + id + '"]');
        if (!el || el.disabled || btn.disabled) return;
        el.value = (parseInt(el.value) || 0) + 1;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        var old = btn.getAttribute('data-label') || btn.textContent;
        btn.setAttribute('data-label', old);
        btn.textContent = 'Đã chọn ' + el.value;
        clearTimeout(btn._t);
        btn._t = setTimeout(function () { btn.textContent = old; }, 1200);
      }
    </script>
    """
    return f"""
    <div class="panel">
        <h2>Món bán chạy <span class="muted" style="font-weight:500;">· {escape(label)}</span></h2>
        <div class="bs-grid">{cards}</div>
    </div>
    {style}
    """


def order_picker_html(items, rank_map=None, blocked=None, sale=None):
    rows = ""
    for item in items:
        text = f'{item["name"]} {item["item_id"]} {item["category_name"]}'
        rows += f"""
        <tr class="pick-row" data-text="{escape(text)}" data-cat="{escape(item["category_name"])}">
            <td>{food_thumb_html(item["category_id"], item["name"])}</td>
            <td>{escape(item["category_name"])}</td>
            <td>{item["item_id"]}</td>
            <td>{escape(item["name"])}{item_flags_html(item["item_id"], rank_map, blocked, sale)}</td>
            <td>{item["price"]:,.0f} đ</td>
            <td><input type="number" class="pick-qty" name="qty_{item["item_id"]}" value="0" min="0" step="1" style="width:100px" {'disabled title="Món tạm ngừng đặt do không đủ nguyên liệu còn hạn"' if blocked and item["item_id"] in blocked else ""}></td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="6" class="empty">Chưa có mặt hàng đang bán.</td></tr>'

    cats = sorted({i["category_name"] for i in items})
    cat_opts = "".join(f'<option value="{escape(c)}">{escape(c)}</option>' for c in cats)

    html = f"""
    <div class="actions" style="margin:12px 0;">
        <input id="pick-search" placeholder="Tìm món theo tên, mã hoặc danh mục..." style="max-width:360px;" autocomplete="off">
        <select id="pick-cat" style="max-width:200px;"><option value="">Tất cả danh mục</option>{cat_opts}</select>
        <label style="display:flex;gap:6px;align-items:center;"><input type="checkbox" id="pick-selected" style="width:auto;"> Chỉ món đã chọn</label>
        <button type="button" class="btn btn-secondary" onclick="pickReset()">Xóa lọc</button>
    </div>
    <p class="muted" id="pick-summary" style="margin-bottom:10px;"></p>
    <table>
        <thead><tr><th>Hình</th><th>Danh mục</th><th>Mã</th><th>Món ăn</th><th>Đơn giá</th><th>Số lượng</th></tr></thead>
        <tbody id="pick-body">{rows}</tbody>
    </table>
    <p class="empty" id="pick-none" style="display:none;">Không tìm thấy món phù hợp.</p>
    """
    script = r"""
    <script>
    (function () {
        function norm(s) {
            return (s || '').toLowerCase().normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '').replace(/đ/g, 'd');
        }
        var search = document.getElementById('pick-search');
        var cat = document.getElementById('pick-cat');
        var onlySel = document.getElementById('pick-selected');
        var rows = Array.prototype.slice.call(document.querySelectorAll('.pick-row'));
        rows.forEach(function (r) { r._text = norm(r.dataset.text); });

        function apply() {
            var kw = norm(search.value.trim()), shown = 0, lines = 0, qtyTotal = 0;
            rows.forEach(function (r) {
                var qty = parseInt(r.querySelector('.pick-qty').value) || 0;
                if (qty > 0) { lines++; qtyTotal += qty; }
                r.style.background = qty > 0 ? '#eff6ff' : '';
                var ok = (!kw || kw.split(/\s+/).every(function (w) { return r._text.indexOf(w) !== -1; }))
                      && (!cat.value || r.dataset.cat === cat.value)
                      && (!onlySel.checked || qty > 0);
                r.style.display = ok ? '' : 'none';
                if (ok) shown++;
            });
            document.getElementById('pick-none').style.display = shown ? 'none' : 'block';
            document.getElementById('pick-summary').textContent =
                'Đã chọn ' + lines + ' món · Tổng SL ' + qtyTotal;
        }
        window.pickReset = function () {
            search.value = ''; cat.value = ''; onlySel.checked = false; apply(); search.focus();
        };
        search.addEventListener('input', apply);
        search.addEventListener('keydown', function (e) { if (e.key === 'Enter') e.preventDefault(); });
        cat.addEventListener('change', apply);
        onlySel.addEventListener('change', apply);
        document.addEventListener('input', function (e) {
            if (e.target.classList && e.target.classList.contains('pick-qty')) apply();
        });
        apply();
    })();
    </script>
    """
    return html + script
# =========================================================
# XÁC THỰC & PHÂN QUYỀN
# =========================================================

def get_current_user():
    if "user_id" not in session:
        return None
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE user_id = ? AND active = 1",
        (session["user_id"],)
    ).fetchone()
    conn.close()
    return user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


# =========================================================
# PHÂN QUYỀN: vị trí (role) có quyền mặc định, quản lý cấp thêm bằng cách tick chọn
# =========================================================

PERMISSIONS = {
    "menu":           ("", "Danh mục & món ăn",        "Thêm, sửa, ẩn/hiện, xóa danh mục và món ăn."),
    "inventory_edit": ("", "Nhập/xuất & sửa nguyên liệu", "Thêm nguyên liệu, nhập/xuất kho, cập nhật hạn sử dụng."),
    "recipes":        ("", "Công thức món ăn",         "Thiết lập định lượng nguyên liệu cho từng món."),
    "promotions":     ("", "Quản lý khuyến mại",       "Tạo/xóa khuyến mại, xem báo cáo và chương trình xả hàng."),
    "customers":      ("", "Quản lý khách hàng",       "Đăng ký và theo dõi khách hàng thành viên."),
    "orders_create":  ("", "Tạo đơn, gọi thêm, hủy món/đơn", "Nhận order tại bàn, gọi thêm, hủy trong thời gian cho phép."),
    "orders_kitchen": ("", "Xác nhận đơn & chế biến", "Xác nhận đơn (trừ kho), bắt đầu chuẩn bị, hoàn thành đơn."),
    "analysis":       ("", "Phân tích món ăn",         "Xem thống kê bán chạy/bán chậm, biểu đồ và xuất Excel."),
    "users":          ("", "Quản lý tài khoản",        "Tạo tài khoản, phân quyền, khóa/mở/xóa nhân viên."),
}

ROLES = {
    "admin":   ("", "Quản lý",  "Toàn quyền hệ thống."),
    "cashier": ("", "Thu ngân", "Danh mục món, khuyến mại, khách hàng, tạo/hủy đơn."),
    "waiter":  ("", "Phục vụ",  "Nhận order, gọi thêm món, hủy món/đơn."),
    "chef":    ("", "Đầu bếp",  "Công thức, nhập/xuất nguyên liệu, chế biến đơn."),
}

ROLE_DEFAULT_PERMS = {
    "admin": set(PERMISSIONS),
    "cashier": {"menu", "promotions", "customers", "orders_create"},
    "waiter": {"orders_create"},
    "chef": {"inventory_edit", "recipes", "orders_kitchen"},
}

# Hàm xử lý (view) nào thuộc quyền nào -> dùng để cho phép quyền cấp thêm.
PERM_BY_FUNC = {}
for _perm, _funcs in {
    "menu": "categories add_category update_category toggle_category delete_category add_item edit_item update_item delete_item",
    "inventory_edit": "add_ingredient update_material delete_material change_inventory update_batch_expiry remove_batch_quantity",
    "recipes": "recipes save_recipe",
    "promotions": "promotions add_promotion delete_promotion promotions_report promotion_expiry_detail",
    "customers": "customers add_customer",
    "orders_create": "new_order add_order add_items_to_order process_add_items cancel_order cancel_order_item",
    "orders_kitchen": "kitchen_alerts kitchen_ack confirm_order update_order_status",
    "analysis": "analysis export_analysis",
    "users": "users add_user toggle_user reset_password delete_user users_edit update_user",
}.items():
    for _name in _funcs.split():
        PERM_BY_FUNC[_name] = _perm


def user_extra_perms(user):
    raw = ""
    if user:
        try:
            raw = user.get("extra_permissions") or ""
        except AttributeError:
            raw = user["extra_permissions"] if "extra_permissions" in user.keys() else ""
    return {p for p in str(raw).split(",") if p in PERMISSIONS}


def has_perm(user, key):
    if not user:
        return False
    role = user["role"]
    if role == "admin" or key in ROLE_DEFAULT_PERMS.get(role, set()):
        return True
    return key in user_extra_perms(user)


def role_required(*allowed_roles):
    def decorator(f):
        perm = PERM_BY_FUNC.get(f.__name__)

        @wraps(f)
        def wrapper(*args, **kwargs):
            user = get_current_user()
            if not user:
                return redirect(url_for("login", next=request.path))
            if user["role"] not in allowed_roles and not (perm and has_perm(user, perm)):
                return render_forbidden()
            return f(*args, **kwargs)
        return wrapper
    return decorator


def render_forbidden():
    body = """
    <div class="header">
        <div><h1>Không có quyền truy cập</h1></div>
    </div>
    <div class="panel">
        <p>Vui lòng quay lại trang chính hoặc liên hệ quản lý nếu cần cấp quyền.</p>
        <br>
        <a class="btn btn-primary" href="/">← Về trang chủ</a>
    </div>
    """
    return render_page("Không có quyền", body, "")



# =========================================================
# THÔNG BÁO CHO BẾP KHI KHÁCH GỌI THÊM MÓN
# =========================================================

def get_kitchen_alerts(conn, order_id=None):
    """Các món gọi thêm mà bếp chưa bấm 'Đã nhận', gom theo đơn."""
    sql = """
        SELECT oi.order_id, o.table_number, o.customer_name, i.name AS item_name,
               (oi.quantity - COALESCE(oi.canceled_quantity, 0)) AS qty, oi.ordered_at
        FROM order_details oi
        JOIN orders o ON o.order_id = oi.order_id
        JOIN items i ON i.item_id = oi.item_id
        WHERE COALESCE(oi.kitchen_ack, 1) = 0
          AND o.status NOT IN ('Đã hủy', 'Đã hoàn thành')
          AND (oi.quantity - COALESCE(oi.canceled_quantity, 0)) > 0
    """
    params = []
    if order_id:
        sql += " AND oi.order_id = ?"
        params.append(order_id)
    sql += " ORDER BY oi.ordered_at, oi.order_id"
    grouped = {}
    for r in conn.execute(sql, params).fetchall():
        g = grouped.setdefault(r["order_id"], {"order_id": r["order_id"], "table": r["table_number"],
                                               "customer": r["customer_name"], "items": [], "time": r["ordered_at"]})
        g["items"].append(f'{r["item_name"]} x{int(r["qty"])}')
    return list(grouped.values())


def kitchen_alert_html(alerts):
    if not alerts:
        return ""
    blocks = ""
    for g in alerts:
        blocks += f"""
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin:6px 0;">
            <div><strong>Đơn #{escape(g["order_id"])}</strong> ({escape(g["table"] or "Chưa có bàn")}) vừa gọi thêm lúc {escape(g["time"] or "")}:
                {escape(", ".join(g["items"]))}</div>
            <div class="actions">
                <a class="btn btn-primary" href="/orders/{escape(g["order_id"])}">Xem đơn</a>
                <form method="POST" action="/orders/{escape(g["order_id"])}/kitchen-ack" style="display:inline;">
                    <button class="btn btn-warning">Đã nhận</button>
                </form>
            </div>
        </div>"""
    return f"""<div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:12px 16px;margin-bottom:16px;">
        <strong>Có món mới được gọi thêm!</strong>{blocks}</div>"""


@app.route("/kitchen/alerts")
@role_required("admin", "chef")
def kitchen_alerts():
    conn = get_db()
    alerts = get_kitchen_alerts(conn)
    conn.close()
    return jsonify({"count": len(alerts), "html": kitchen_alert_html(alerts)})


@app.route("/orders/<string:id>/kitchen-ack", methods=["POST"])
@role_required("admin", "chef")
def kitchen_ack(id):
    conn = get_db()
    conn.execute("UPDATE order_details SET kitchen_ack = 1 WHERE order_id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("order_detail", id=id))

# =========================================================
# KHUYẾN MÃI
# =========================================================

def get_item_promotion(conn, item_id, check_date=None):
    if check_date is None:
        check_date = date.today().isoformat()

    promo = conn.execute("""
        SELECT * FROM promotions
        WHERE apply_type = 'item'
          AND item_id = ?
          AND start_date <= ?
          AND end_date >= ?
        ORDER BY discount DESC
        LIMIT 1
    """, (item_id, check_date, check_date)).fetchone()

    # Món dùng nguyên liệu sắp hết hạn được giảm giá tự động (lấy mức cao hơn nếu trùng khuyến mại).
    expiry = get_expiry_item_discounts(conn, check_date).get(item_id)
    if expiry and (not promo or expiry["discount"] > float(promo["discount"])):
        return {"promotion_id": expiry["program_id"], "name": f"Xả hàng HSD: {expiry['ingredient_name']}",
                "discount": expiry["discount"], "apply_type": "item", "item_id": item_id}
    return promo


def get_order_promotion(conn, check_date=None):
    if check_date is None:
        check_date = date.today().isoformat()

    rows = conn.execute("""
        SELECT * FROM promotions
        WHERE apply_type = 'order'
          AND start_date <= ?
          AND end_date >= ?
        ORDER BY discount DESC
    """, (check_date, check_date)).fetchall()
    rows = [p for p in rows if promo_group(p) != "Hạng thành viên"]
    return rows[0] if rows else None


def calculate_order_totals(conn, order_id):
    order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if not order:
        return None

    # Đơn đã hoàn thành phải giữ nguyên mức giảm giá tại thời điểm đặt đơn,
    # kể cả khi khách được nâng hạng sau khi thanh toán.
    if order["status"] == "Đã hoàn thành":
        return {
            "subtotal": float(order["total"] or 0),
            "item_discount": 0,
            "order_discount": max(0, float(order["discount_amount"] or 0) - float(order["member_discount"] or 0)),
            "member_discount": float(order["member_discount"] or 0),
            "total_discount": float(order["discount_amount"] or 0),
            "final_total": float(order["final_total"] or 0),
            "order_promo": None,
        }

    check_date = order["order_date"].split(" ")[0]

    items = conn.execute("""
        SELECT oi.order_detail_id, oi.item_id, (oi.quantity - COALESCE(oi.canceled_quantity, 0)) AS quantity, oi.price
        FROM order_details oi
        WHERE oi.order_id = ? AND (oi.quantity - COALESCE(oi.canceled_quantity, 0)) > 0
    """, (order_id,)).fetchall()

    subtotal = 0
    item_discount_total = 0

    for it in items:
        line_total = it["quantity"] * it["price"]
        subtotal += line_total

        promo = get_item_promotion(conn, it["item_id"], check_date)
        line_discount = 0
        if promo:
            line_discount = line_total * (promo["discount"] / 100)

        conn.execute("UPDATE order_details SET item_discount = ? WHERE order_detail_id = ?",
                     (line_discount, it["order_detail_id"]))
        item_discount_total += line_discount

    after_item_discount = subtotal - item_discount_total

    eligible_promos = conn.execute("""
        SELECT * FROM promotions
        WHERE apply_type = 'order' AND start_date <= ? AND end_date >= ?
          AND COALESCE(min_order_value, 0) <= ?
        ORDER BY discount DESC
    """, (check_date, check_date, after_item_discount)).fetchall()
    # Chương trình gắn với hạng thành viên (vd "Ưu đãi hạng Bạc 5%") không phải khuyến mại hóa đơn chung:
    # khách chỉ nhận đúng mức của hạng mình qua phần chiết khấu hạng thành viên bên dưới.
    eligible_promos = [p for p in eligible_promos if promo_group(p) != "Hạng thành viên"]
    order_promo = eligible_promos[0] if eligible_promos else None
    order_discount = 0
    if order_promo:
        order_discount = after_item_discount * (order_promo["discount"] / 100)

    # Chiết khấu hạng thành viên áp dụng sau khuyến mại theo món và hóa đơn.
    # Không cộng dồn chiết khấu hạng với chính sách khuyến mại khác theo tỷ lệ trên cùng một phần tiền:
    # tính trên số tiền còn lại sau khuyến mại món/hóa đơn.
    customer = None
    if order["customer_id"]:
        customer = conn.execute("""
            SELECT c.customer_id, c.total_spent, t.tier_name, t.discount_percent
            FROM customers c JOIN member_tiers t ON t.member_tier_id = c.member_tier_id
            WHERE c.customer_id = ? AND c.active = 1
        """, (order["customer_id"],)).fetchone()
    member_discount = max(0, after_item_discount - order_discount) * (customer["discount_percent"] / 100) if customer else 0
    tier_name = customer["tier_name"] if customer else None
    total_discount = item_discount_total + order_discount + member_discount
    final_total = max(0, subtotal - total_discount)
    promotion_id = order_promo["promotion_id"] if order_promo else None

    conn.execute("""
        UPDATE orders
        SET total = ?, discount_amount = ?, final_total = ?, promotion_id = ?,
            member_discount = ?, member_tier_name = ?
        WHERE order_id = ?
    """, (subtotal, total_discount, final_total, promotion_id, member_discount, tier_name, order_id))

    return {
        "subtotal": subtotal,
        "item_discount": item_discount_total,
        "order_discount": order_discount,
        "member_discount": member_discount,
        "total_discount": total_discount,
        "final_total": final_total,
        "order_promo": order_promo
    }


# =========================================================
# GIAO DIỆN CHUNG
# =========================================================

# ---------------------------------------------------------
# Ô chọn ngày dùng chung: nhãn tháng dạng T01..T12
# ---------------------------------------------------------
# Lịch có sẵn của trình duyệt ghi tháng là "Thg1", "Thg2"... và không chỉnh được bằng CSS, nên mọi ô
# <input type="date"> được thay bằng ô nhập dd/mm/yyyy kèm lịch riêng. Giá trị gửi lên máy chủ vẫn là
# yyyy-mm-dd qua chính ô gốc (đổi thành ô ẩn), nên các biểu mẫu hoạt động như cũ.
DATEPICKER_ASSETS = """
<style>
  .dp-wrap { position:relative; display:block; }
  .dp-wrap.dp-inline { display:inline-block; vertical-align:middle; }
  .dp-text { padding-right:34px !important; background:#fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='%2333536b' stroke-width='2'%3E%3Crect x='3' y='5' width='18' height='16' rx='2'/%3E%3Cpath d='M3 10h18M8 3v4M16 3v4'/%3E%3C/svg%3E") no-repeat right 9px center !important; }
  .dp-pop { position:absolute; z-index:3000; width:276px; padding:12px; background:#fff; border:1px solid #cbd8e5; border-radius:14px; box-shadow:0 18px 40px rgba(15,45,75,.22); font-size:13px; color:#0f2d4b; }
  .dp-head { display:flex; align-items:center; justify-content:space-between; gap:6px; margin-bottom:8px; }
  .dp-pop button { font:inherit; cursor:pointer; border:0; background:none; color:inherit; border-radius:8px; }
  .dp-nav { width:32px; height:32px; font-size:18px !important; }
  .dp-nav:hover, .dp-title:hover, .dp-day:hover, .dp-month:hover, .dp-foot button:hover { background:#e0f2fe; }
  .dp-title { flex:1; padding:7px 8px; font-weight:800 !important; font-size:14px !important; }
  .dp-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:2px; text-align:center; }
  .dp-dow { font-size:11px; font-weight:800; color:#64748b; padding:4px 0; }
  .dp-day { height:32px; font-variant-numeric:tabular-nums; }
  .dp-day.other { color:#a8b5c4; }
  .dp-day.today, .dp-month.today { box-shadow:inset 0 0 0 1.5px #0284c7; }
  .dp-day.sel, .dp-month.sel { background:#0369a1 !important; color:#fff !important; font-weight:800; }
  .dp-months { display:grid; grid-template-columns:repeat(4,1fr); gap:6px; }
  .dp-month { padding:13px 0; font-weight:700 !important; font-variant-numeric:tabular-nums; }
  .dp-foot { display:flex; justify-content:space-between; margin-top:8px; padding-top:8px; border-top:1px solid #edf2f7; }
  .dp-foot button { padding:6px 10px; color:#0369a1; font-weight:700 !important; }
</style>
<script>
(function () {
  var MONTHS = ['T01','T02','T03','T04','T05','T06','T07','T08','T09','T10','T11','T12'];
  var DOW = ['T2','T3','T4','T5','T6','T7','CN'];
  var pop = null, cur = null;   // cur = {text, hidden, y, m, view}

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function iso(y, m, d) { return y + '-' + pad(m + 1) + '-' + pad(d); }
  function parseIso(v) { var r = /^(\\d{4})-(\\d{2})-(\\d{2})/.exec(v || ''); return r ? { y: +r[1], m: +r[2] - 1, d: +r[3] } : null; }
  function show(v) { var p = parseIso(v); return p ? pad(p.d) + '/' + pad(p.m + 1) + '/' + p.y : ''; }
  function parseVn(t) {
    var r = /^\\s*(\\d{1,2})[\\/\\-.](\\d{1,2})[\\/\\-.](\\d{4})\\s*$/.exec(t);
    if (!r) return null;
    var d = +r[1], m = +r[2] - 1, y = +r[3], dt = new Date(y, m, d);
    return (dt.getFullYear() === y && dt.getMonth() === m && dt.getDate() === d) ? iso(y, m, d) : null;
  }
  function setValue(text, hidden, value) {
    text.value = show(value);
    text.setCustomValidity('');
    if (hidden.value !== value) {
      hidden.value = value;
      hidden.dispatchEvent(new Event('input', { bubbles: true }));
      hidden.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }
  function close() { if (pop) { pop.remove(); pop = null; cur = null; } }

  function render() {
    var now = new Date(), sel = parseIso(cur.hidden.value), html = '';
    if (cur.view === 'months') {
      html += '<div class="dp-head"><button type="button" class="dp-nav" data-act="py" aria-label="Năm trước">‹</button>'
            + '<button type="button" class="dp-title" data-act="days">Năm ' + cur.y + '</button>'
            + '<button type="button" class="dp-nav" data-act="ny" aria-label="Năm sau">›</button></div><div class="dp-months">';
      for (var i = 0; i < 12; i++) {
        var c = 'dp-month' + (sel && sel.y === cur.y && sel.m === i ? ' sel' : '') + (now.getFullYear() === cur.y && now.getMonth() === i ? ' today' : '');
        html += '<button type="button" class="' + c + '" data-month="' + i + '">' + MONTHS[i] + '</button>';
      }
      html += '</div>';
    } else {
      html += '<div class="dp-head"><button type="button" class="dp-nav" data-act="pm" aria-label="Tháng trước">‹</button>'
            + '<button type="button" class="dp-title" data-act="months" title="Chọn tháng, năm">' + MONTHS[cur.m] + ' / ' + cur.y + ' ▾</button>'
            + '<button type="button" class="dp-nav" data-act="nm" aria-label="Tháng sau">›</button></div><div class="dp-grid">';
      for (var k = 0; k < 7; k++) html += '<div class="dp-dow">' + DOW[k] + '</div>';
      var first = new Date(cur.y, cur.m, 1), offset = (first.getDay() + 6) % 7;
      for (var n = 0; n < 42; n++) {
        var dt = new Date(cur.y, cur.m, 1 - offset + n), y = dt.getFullYear(), m = dt.getMonth(), d = dt.getDate();
        var cls = 'dp-day' + (m !== cur.m ? ' other' : '')
          + (sel && sel.y === y && sel.m === m && sel.d === d ? ' sel' : '')
          + (now.getFullYear() === y && now.getMonth() === m && now.getDate() === d ? ' today' : '');
        html += '<button type="button" class="' + cls + '" data-date="' + iso(y, m, d) + '">' + d + '</button>';
      }
      html += '</div>';
    }
    html += '<div class="dp-foot"><button type="button" data-act="today">Hôm nay</button>'
          + (cur.text.required ? '<span></span>' : '<button type="button" data-act="clear">Xóa</button>') + '</div>';
    pop.innerHTML = html;
  }

  function open(text, hidden) {
    if (cur && cur.text === text) return;
    close();
    var p = parseIso(hidden.value) || (function () { var n = new Date(); return { y: n.getFullYear(), m: n.getMonth() }; })();
    cur = { text: text, hidden: hidden, y: p.y, m: p.m, view: 'days' };
    pop = document.createElement('div');
    pop.className = 'dp-pop';
    document.body.appendChild(pop);
    render();
    var r = text.getBoundingClientRect();
    var left = Math.min(r.left + window.scrollX, window.scrollX + document.documentElement.clientWidth - 288);
    pop.style.left = Math.max(8, left) + 'px';
    pop.style.top = (r.bottom + window.scrollY + 6) + 'px';
    pop.addEventListener('mousedown', function (e) { e.preventDefault(); });
    pop.addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.date) { setValue(cur.text, cur.hidden, b.dataset.date); close(); return; }
      if (b.dataset.month) { cur.m = +b.dataset.month; cur.view = 'days'; render(); return; }
      var a = b.dataset.act;
      if (a === 'pm') { if (--cur.m < 0) { cur.m = 11; cur.y--; } }
      else if (a === 'nm') { if (++cur.m > 11) { cur.m = 0; cur.y++; } }
      else if (a === 'py') cur.y--;
      else if (a === 'ny') cur.y++;
      else if (a === 'months') cur.view = 'months';
      else if (a === 'days') cur.view = 'days';
      else if (a === 'today') { var n = new Date(); setValue(cur.text, cur.hidden, iso(n.getFullYear(), n.getMonth(), n.getDate())); close(); return; }
      else if (a === 'clear') { setValue(cur.text, cur.hidden, ''); close(); return; }
      render();
    });
  }

  function enhance(hidden) {
    if (hidden.dataset.dp) return;
    hidden.dataset.dp = '1';
    var wrap = document.createElement('span'), text = document.createElement('input');
    wrap.className = 'dp-wrap';
    text.type = 'text'; text.className = 'dp-text'; text.placeholder = 'dd/mm/yyyy';
    text.autocomplete = 'off'; text.inputMode = 'numeric'; text.maxLength = 10;
    text.required = hidden.required; text.title = hidden.title || 'Ngày (dd/mm/yyyy)';
    if (hidden.style.width) { wrap.classList.add('dp-inline'); wrap.style.width = hidden.style.width; }
    if (hidden.style.padding) text.style.padding = hidden.style.padding;
    text.value = show(hidden.value);
    hidden.parentNode.insertBefore(wrap, hidden);
    wrap.appendChild(text); wrap.appendChild(hidden);
    hidden.type = 'hidden';
    text.addEventListener('focus', function () { open(text, hidden); });
    text.addEventListener('click', function () { open(text, hidden); });
    text.addEventListener('change', function () {
      if (!text.value.trim()) { setValue(text, hidden, ''); return; }
      var v = parseVn(text.value);
      if (v) { setValue(text, hidden, v); if (cur && cur.text === text) { var p = parseIso(v); cur.y = p.y; cur.m = p.m; render(); } }
      else text.setCustomValidity('Nhập ngày theo dạng dd/mm/yyyy, ví dụ 05/11/2026.');
    });
    text.addEventListener('input', function () { text.setCustomValidity(''); });
    text.addEventListener('keydown', function (e) { if (e.key === 'Escape' || e.key === 'Tab') close(); });
  }

  function init() { Array.prototype.forEach.call(document.querySelectorAll('input[type="date"]'), enhance); }
  document.addEventListener('mousedown', function (e) {
    if (pop && !pop.contains(e.target) && !(cur && e.target === cur.text)) close();
  });
  window.addEventListener('resize', close);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
</script>
"""


# ---------------------------------------------------------
# Mô hình cảnh báo tồn kho: Báo động / Sắp hết hàng / Đã hết hàng
# ---------------------------------------------------------
def get_stock_alert_model(conn):
    """Dữ liệu cho 3 ô cảnh báo tồn kho, tính trực tiếp từ bảng ingredients + recipes mỗi lần gọi
    nên luôn khớp với tồn kho hiện tại. Báo động = Sắp hết hàng + Đã hết hàng."""
    ingredients = {}
    for r in conn.execute("SELECT ingredient_id, name, unit, quantity, min_quantity FROM ingredients ORDER BY name").fetchall():
        qty, minimum = float(r["quantity"] or 0), float(r["min_quantity"] or 0)
        ingredients[r["ingredient_id"]] = {
            "id": r["ingredient_id"], "name": r["name"], "unit": r["unit"], "qty": qty, "min": minimum,
            "status": inventory_status(qty, minimum),
        }
    dishes = {}
    for r in conn.execute("""
        SELECT it.item_id, it.name, c.name AS category_name, r.ingredient_id, r.quantity AS need
        FROM recipes r
        JOIN items it ON it.item_id = r.item_id
        LEFT JOIN categories c ON c.category_id = it.category_id
        WHERE it.status = 'Đang bán'
        ORDER BY it.name
    """).fetchall():
        ing = ingredients.get(r["ingredient_id"])
        if not ing:
            continue
        dish = dishes.setdefault(r["item_id"], {"id": r["item_id"], "name": r["name"],
                                                "category": r["category_name"] or "", "ingredients": []})
        dish["ingredients"].append(dict(ing, need=float(r["need"] or 0)))
    alarm_dishes = []
    used = set()
    for dish in dishes.values():
        dish["n_out"] = sum(1 for i in dish["ingredients"] if i["status"] == "Hết hàng")
        dish["n_low"] = sum(1 for i in dish["ingredients"] if i["status"] == "Sắp hết")
        if dish["n_out"] or dish["n_low"]:
            dish["ingredients"].sort(key=lambda i: (STATUS_ORDER[i["status"]], i["name"]))
            alarm_dishes.append(dish)
            used.update(i["id"] for i in dish["ingredients"] if i["status"] != "Còn hàng")
    alarm_dishes.sort(key=lambda d: (-d["n_out"], -d["n_low"], d["name"]))
    low = [i for i in ingredients.values() if i["status"] == "Sắp hết"]
    out = [i for i in ingredients.values() if i["status"] == "Hết hàng"]
    return {
        "counts": {"alarm": len(low) + len(out), "low": len(low), "out": len(out), "total": len(ingredients)},
        "ingredients": {"low": low, "out": out},
        "dishes": alarm_dishes,
        "unused": [i["name"] for i in low + out if i["id"] not in used],
    }


STOCK_DASH_ASSETS = """
<style>
  .sd-top { display:grid; grid-template-columns:minmax(260px,1fr) minmax(0,1.6fr); gap:16px; align-items:stretch; }
  .sd-tiles { display:grid; grid-template-columns:1fr; gap:10px; align-content:stretch; }
  .sd-tile { position:relative; padding:14px 16px; border-radius:8px; border:1px solid #e2e8f0; border-left:4px solid var(--c); background:#fff; cursor:pointer; outline:none; }
  .sd-tile:hover, .sd-tile:focus-within, .sd-tile.pinned { box-shadow:0 10px 26px rgba(15,45,75,.14); }
  .sd-label { font-size:12px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; color:var(--c); }
  .sd-num { font-size:32px; line-height:1.1; font-weight:900; color:#0f2d4b; font-variant-numeric:tabular-nums; }
  .sd-sub { font-size:12px; color:#64748b; }
  .sd-pop { display:none; position:absolute; left:-1px; top:100%; z-index:60; width:max(100%, 300px); max-height:330px; overflow:auto; padding:10px; background:#fff; border:1px solid #cbd8e5; border-radius:0 0 14px 14px; box-shadow:0 18px 36px rgba(15,45,75,.2); cursor:default; }
  .sd-tile:last-child .sd-pop { left:auto; right:-1px; }
  .sd-tile:hover .sd-pop, .sd-tile:focus-within .sd-pop, .sd-tile.pinned .sd-pop { display:block; }
  .sd-pop h4 { font-size:11px; font-weight:800; letter-spacing:.05em; text-transform:uppercase; color:#64748b; margin:2px 4px 6px; }
  .sd-dish { display:flex; width:100%; justify-content:space-between; align-items:center; gap:8px; padding:8px 9px; border:0; border-radius:9px; background:none; font:inherit; font-size:13px; text-align:left; color:#0f2d4b; cursor:pointer; }
  .sd-dish:hover, .sd-dish:focus-visible, .sd-dish.on { background:#e0f2fe; }
  .sd-dish b { font-weight:700; min-width:0; overflow-wrap:anywhere; }
  .sd-chips { display:flex; gap:4px; flex:none; }
  .sd-chip { padding:2px 7px; border-radius:999px; font-size:11px; font-weight:800; white-space:nowrap; }
  .sd-chip.out { background:#ffe4e6; color:#9f1239; } .sd-chip.low { background:#fef3c7; color:#92400e; } .sd-chip.ok { background:#dcfce7; color:#166534; }
  .sd-note { font-size:12px; color:#64748b; padding:6px 4px 2px; }
  .sd-chart { border:1px solid #e2e8f0; border-radius:8px; padding:12px 12px 6px; background:#fff; }
  .sd-chart svg { max-width:520px; display:block; margin:0 auto; }
  .sd-chart h3 { font-size:13px; font-weight:800; color:#33536b; margin-bottom:4px; }
  .sd-mini { margin-top:16px; padding:16px; border-radius:14px; border:1px solid #bae6fd; background:#f0f9ff; }
  .sd-mini-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap; margin-bottom:12px; }
  .sd-mini-head h3 { font-size:17px; font-weight:800; color:#0f2d4b; }
  .sd-rows { display:grid; grid-template-columns:repeat(auto-fill,minmax(270px,1fr)); gap:10px; }
  .sd-row { padding:12px; border-radius:12px; background:#fff; border:1px solid #e2e8f0; border-left:5px solid var(--c); }
  .sd-row-top { display:flex; justify-content:space-between; gap:8px; align-items:center; font-weight:800; color:#0f2d4b; }
  .sd-bar { height:9px; border-radius:6px; background:#e5e7eb; overflow:hidden; margin:9px 0 7px; }
  .sd-bar i { display:block; height:100%; background:var(--c); border-radius:6px; }
  .sd-row p { font-size:12px; color:#475569; margin-top:2px; }
  .sd-foot { display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-top:10px; font-size:12px; color:#64748b; }
  @media (max-width:1000px) { .sd-top { grid-template-columns:1fr; } }
  @media (max-width:620px) { .sd-tiles { grid-template-columns:1fr; } .sd-pop { position:static; width:auto; box-shadow:none; border:0; padding:8px 0 0; } }
</style>
<script>
(function () {
  var root = document.getElementById('stock-dash');
  if (!root) return;
  var model = JSON.parse(document.getElementById('stock-dash-data').textContent);
  var lastJson = JSON.stringify(model), selected = null, pinned = null;
  var GROUPS = [
    { key: 'alarm', label: 'Báo động', color: '#ea580c' },
    { key: 'low', label: 'Sắp hết hàng', color: '#d97706' },
    { key: 'out', label: 'Đã hết hàng', color: '#dc2626' }
  ];
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function num(v) { return (Math.round(v * 1000) / 1000).toLocaleString('vi-VN'); }
  function dishesOf(key) {
    return model.dishes.filter(function (d) { return key === 'alarm' || (key === 'low' ? d.n_low > 0 : d.n_out > 0); });
  }
  function chips(d) {
    return '<span class="sd-chips">' + (d.n_out ? '<span class="sd-chip out">' + d.n_out + ' hết</span>' : '')
         + (d.n_low ? '<span class="sd-chip low">' + d.n_low + ' sắp hết</span>' : '') + '</span>';
  }
  function tileHtml(g) {
    var list = dishesOf(g.key), names;
    if (g.key === 'alarm') names = model.ingredients.out.concat(model.ingredients.low); else names = model.ingredients[g.key];
    var pop = '<h4>Mặt hàng đang ' + g.label.toLowerCase() + ' (' + list.length + ')</h4>';
    if (list.length) {
      pop += list.map(function (d) {
        return '<button type="button" class="sd-dish' + (selected === d.id ? ' on' : '') + '" data-dish="' + esc(d.id) + '"><b>' + esc(d.name) + '</b>' + chips(d) + '</button>';
      }).join('');
    } else {
      pop += '<div class="sd-note">' + (names.length ? 'Chưa có món đang bán nào dùng các nguyên liệu này.' : 'Không có mặt hàng nào.') + '</div>';
    }
    if (names.length) pop += '<div class="sd-note">Nguyên liệu: ' + names.map(function (i) { return esc(i.name); }).join(', ') + '</div>';
    return '<div class="sd-tile' + (pinned === g.key ? ' pinned' : '') + '" tabindex="0" data-group="' + g.key + '" style="--c:' + g.color + '">'
         + '<div class="sd-label">' + g.label + '</div><div class="sd-num">' + model.counts[g.key] + '</div>'
         + '<div class="sd-sub">nguyên liệu · ' + list.length + ' món bị ảnh hưởng</div>'
         + '<div class="sd-pop">' + pop + '</div></div>';
  }
  function chartHtml() {
    var W = 360, H = 230, L = 34, R = 10, T = 30, B = 40, max = Math.max(1, model.counts.alarm, model.counts.low, model.counts.out);
    var top = Math.max(4, Math.ceil(max / 4) * 4), band = (W - L - R) / 3, s = '';
    for (var k = 0; k <= 4; k++) {
      var gy = T + (H - T - B) * (1 - k / 4);
      s += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + gy + '" y2="' + gy + '" stroke="#e2e8f0"/>'
         + '<text x="' + (L - 6) + '" y="' + (gy + 4) + '" text-anchor="end" font-size="11" fill="#64748b">' + (top * k / 4) + '</text>';
    }
    GROUPS.forEach(function (g, i) {
      var v = model.counts[g.key], h = Math.max(v ? 3 : 1, (H - T - B) * v / top), x = L + band * i + band * 0.2, w = band * 0.6, y = H - B - h;
      s += '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="4" fill="' + g.color + '"><title>' + g.label + ': ' + v + ' nguyên liệu</title></rect>'
         + '<text x="' + (x + w / 2) + '" y="' + (y - 7) + '" text-anchor="middle" font-size="15" font-weight="800" fill="#0f2d4b">' + v + '</text>'
         + '<text x="' + (x + w / 2) + '" y="' + (H - B + 18) + '" text-anchor="middle" font-size="12" font-weight="700" fill="#33536b">' + g.label + '</text>';
    });
    return '<div class="sd-chart"><h3>Số nguyên liệu theo mức cảnh báo</h3><svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;" role="img" aria-label="Biểu đồ cột: báo động '
         + model.counts.alarm + ', sắp hết hàng ' + model.counts.low + ', đã hết hàng ' + model.counts.out + '">' + s + '</svg></div>';
  }
  function miniHtml() {
    if (!selected) return '';
    var d = model.dishes.filter(function (x) { return x.id === selected; })[0];
    if (!d) return '<div class="sd-mini"><div class="sd-mini-head"><h3>Mặt hàng đã chọn không còn cảnh báo</h3><span class="sd-chip ok">Đủ nguyên liệu</span></div><span class="sd-sub">Tồn kho vừa thay đổi: mọi nguyên liệu của món này đã trên mức tối thiểu.</span></div>';
    var bad = d.ingredients.filter(function (i) { return i.status !== 'Còn hàng'; });
    var ok = d.ingredients.filter(function (i) { return i.status === 'Còn hàng'; });
    var rows = bad.map(function (i) {
      var out = i.status === 'Hết hàng', color = out ? '#dc2626' : '#d97706';
      var pct = i.min > 0 ? Math.max(0, Math.min(100, i.qty / i.min * 100)) : 0;
      var lack = Math.max(0, i.min - i.qty), portions = i.need > 0 ? Math.floor(i.qty / i.need + 1e-9) : null;
      return '<div class="sd-row" style="--c:' + color + '"><div class="sd-row-top"><span>' + esc(i.name) + '</span><span class="sd-chip ' + (out ? 'out' : 'low') + '">' + (out ? 'Đã hết' : 'Sắp hết') + '</span></div>'
           + '<div class="sd-bar"><i style="width:' + pct + '%"></i></div>'
           + '<p>Tồn <b>' + num(i.qty) + ' ' + esc(i.unit) + '</b> / mức tối thiểu ' + num(i.min) + ' ' + esc(i.unit) + '</p>'
           + '<p>' + (lack > 0 ? 'Cần nhập thêm hơn ' + num(lack) + ' ' + esc(i.unit) + ' để vượt mức tối thiểu.' : 'Đang đúng bằng mức tối thiểu, nên nhập thêm.') + '</p>'
           + (portions === null ? '' : '<p>Mỗi phần dùng ' + num(i.need) + ' ' + esc(i.unit) + ' → còn làm được ' + portions + ' phần.</p>')
           + '<p><a href="/inventory?q=' + encodeURIComponent(i.name) + '#stock-table">Nhập kho nguyên liệu này →</a></p></div>';
    }).join('');
    return '<div class="sd-mini"><div class="sd-mini-head"><div><h3>' + esc(d.name) + '</h3><span class="sd-sub">' + esc(d.category) + ' · ' + d.ingredients.length + ' nguyên liệu trong công thức</span></div>'
         + '<div class="sd-chips"><span class="sd-chip out">' + d.n_out + ' đã hết</span><span class="sd-chip low">' + d.n_low + ' sắp hết</span><span class="sd-chip ok">' + ok.length + ' còn đủ</span>'
         + '<button type="button" class="btn btn-secondary" data-close="1" style="padding:4px 10px;">Đóng</button></div></div>'
         + '<div class="sd-rows">' + rows + '</div>'
         + (ok.length ? '<div class="sd-note">Còn đủ: ' + ok.map(function (i) { return esc(i.name); }).join(', ') + '</div>' : '') + '</div>';
  }
  function render() {
    var focusGroup = document.activeElement && document.activeElement.closest ? document.activeElement.closest('.sd-tile') : null;
    root.innerHTML = '<div class="sd-top"><div class="sd-tiles">' + GROUPS.map(tileHtml).join('') + '</div>' + chartHtml() + '</div>' + miniHtml()
      + '<div class="sd-foot"><span>Tổng ' + model.counts.total + ' nguyên liệu</span><span id="sd-time"></span></div>';
    if (focusGroup) { var t = root.querySelector('.sd-tile[data-group="' + focusGroup.dataset.group + '"]'); if (t) t.focus(); }
    stamp();
  }
  var lastOk = new Date(), failed = false;
  function stamp() {
    var el = document.getElementById('sd-time');
    if (el) el.textContent = failed ? 'Mất kết nối, số liệu lúc ' + lastOk.toLocaleTimeString('vi-VN') : 'Cập nhật ' + lastOk.toLocaleTimeString('vi-VN');
  }
  function refresh() {
    fetch('/inventory/alert-model', { headers: { 'Accept': 'application/json' }, cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        failed = false; lastOk = new Date();
        var js = JSON.stringify(data);
        if (js !== lastJson) { lastJson = js; model = data; render(); } else stamp();
      })
      .catch(function () { failed = true; stamp(); });
  }
  root.addEventListener('click', function (e) {
    var dish = e.target.closest('[data-dish]'), tile = e.target.closest('.sd-tile');
    if (e.target.closest('[data-close]')) { selected = null; render(); return; }
    if (dish) { selected = dish.dataset.dish; pinned = null; if (document.activeElement) document.activeElement.blur(); render();
                var mini = root.querySelector('.sd-mini'); if (mini && mini.scrollIntoView) mini.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); return; }
    if (tile && !e.target.closest('.sd-pop')) { pinned = pinned === tile.dataset.group ? null : tile.dataset.group; render(); }
  });
  document.addEventListener('click', function (e) { if (pinned && !root.contains(e.target)) { pinned = null; render(); } });
  render();
  setInterval(refresh, 10000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
  window.refreshStockDash = refresh;
})();
</script>
"""


def stock_alert_dashboard_html(conn):
    """Khối 3 ô Báo động / Sắp hết hàng / Đã hết hàng + biểu đồ cột + dashboard mini theo món."""
    data = json.dumps(get_stock_alert_model(conn), ensure_ascii=False).replace("</", "<\\/")
    return f"""
    <div class="panel" id="stock-dash-panel">
      <h2>Cảnh báo tồn kho theo mặt hàng</h2>
      <div id="stock-dash"></div>
      <script type="application/json" id="stock-dash-data">{data}</script>
      {STOCK_DASH_ASSETS}
    </div>"""


@app.route("/inventory/alert-model")
@login_required
def inventory_alert_model():
    conn = get_db()
    model = get_stock_alert_model(conn)
    conn.close()
    response = jsonify(model)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# ---------------------------------------------------------
# Thẻ (tab) dùng chung: chia trang dài thành nhiều thẻ nhỏ, giống thanh thẻ ở Quản lý nguyên liệu.
# ---------------------------------------------------------
# Thẻ đang mở được giữ lại khi lọc / tải lại trang (sessionStorage) và có thể mở thẳng bằng
# #ma-the trên đường dẫn, ví dụ /promotions#add-promo, nên các liên kết và chuyển hướng cũ vẫn dùng được.
def tabs_html(group, tabs, default=None):
    """tabs = [(key, nhãn, nội dung html), ...] hoặc (key, nhãn, nội dung, số đếm)."""
    default = default or tabs[0][0]
    bar, panes = "", ""
    for tab in tabs:
        key, label, content = tab[0], tab[1], tab[2]
        count = tab[3] if len(tab) > 3 else None
        on = key == default
        badge = f' <span class="tab-badge">{count}</span>' if count not in (None, "") else ""
        bar += (f'<button type="button" class="btn {"btn-primary" if on else "btn-secondary"}" data-tab="{key}" '
                f'role="tab" aria-selected="{"true" if on else "false"}">{label}{badge}</button>')
        panes += f'<section class="tab-pane{" on" if on else ""}" id="{key}" data-pane="{key}" role="tabpanel">{content}</section>'
    return (f'<div class="tabs" data-group="{escape(group)}" data-default="{default}">'
            f'<div class="tab-bar actions" role="tablist">{bar}</div>{panes}</div>')


TABS_JS = """
<script>
(function () {
  function storeKey(box) { return 'tab:' + location.pathname + ':' + box.dataset.group; }
  function activate(box, key, remember) {
    var btns = box.querySelectorAll(':scope > .tab-bar [data-tab]'), found = false;
    btns.forEach(function (b) { if (b.dataset.tab === key) found = true; });
    if (!found) return false;
    btns.forEach(function (b) {
      var on = b.dataset.tab === key;
      b.classList.toggle('btn-primary', on); b.classList.toggle('btn-secondary', !on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    box.querySelectorAll(':scope > .tab-pane').forEach(function (p) { p.classList.toggle('on', p.dataset.pane === key); });
    if (remember) { try { sessionStorage.setItem(storeKey(box), key); } catch (e) {} }
    return true;
  }
  function scrollToBox(box) {
    var y = box.getBoundingClientRect().top + window.pageYOffset - 72;
    window.scrollTo(0, Math.max(0, y));
  }
  function fromHash(initial) {
    var id = decodeURIComponent(location.hash.slice(1));
    if (!id) return;
    var el = document.getElementById(id), pane = el && el.closest('.tab-pane');
    if (!pane) return;
    var box = pane.closest('.tabs');
    activate(box, pane.dataset.pane, true);
    setTimeout(function () {
      // Có thông báo lỗi ở đầu trang thì giữ ở đầu trang để người dùng đọc được lỗi.
      if (initial && document.querySelector('.content > .message')) window.scrollTo(0, 0);
      else scrollToBox(box);
    }, 0);
  }
  document.querySelectorAll('.tabs').forEach(function (box) {
    var saved = null;
    try { saved = sessionStorage.getItem(storeKey(box)); } catch (e) {}
    if (saved) activate(box, saved, false);
    box.querySelector(':scope > .tab-bar').addEventListener('click', function (e) {
      var b = e.target.closest('[data-tab]');
      if (!b) return;
      activate(box, b.dataset.tab, true);
      try { history.replaceState(null, '', location.pathname + location.search + '#' + b.dataset.tab); } catch (e2) {}
    });
  });
  fromHash(true);
  window.addEventListener('hashchange', function () { fromHash(false); });
})();
</script>
"""


APP_CSS = r"""
:root{--brand:#1769e0;--brand-dark:#1257bd;--brand-soft:#e8f0fd;--ink:#1f2a3d;--muted:#6b778c;--line:#e3e8ef;--surface:#fff;--canvas:#f3f5f9;--nav:#0f2240;
--ocean-950:#0f2240;--ocean-900:#14305a;--ocean-800:#1257bd;--ocean-700:#1769e0;--ocean-600:#2b7cf0;--ocean-100:#e8f0fd;--ocean-50:#f5f8fe;}
*{box-sizing:border-box;margin:0;padding:0;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;}
body{background:var(--canvas);color:var(--ink);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;}
a{color:var(--brand);}
.sidebar{position:fixed;inset:0 auto 0 0;width:236px;height:100vh;background:var(--nav);color:#fff;padding:0 12px 20px;overflow-y:auto;z-index:30;}
.logo{display:flex;align-items:center;gap:11px;padding:18px 8px;border-bottom:1px solid rgba(255,255,255,.08);}
.logo-mark{width:34px;height:34px;border-radius:8px;background:var(--brand);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:17px;flex:none;}
.logo-text{font-size:15px;font-weight:700;line-height:1.2;}
.logo-text small{display:block;font-size:11.5px;font-weight:400;color:#8fa0bb;margin-top:2px;}
.menu{list-style:none;margin-top:14px;}
.menu li{margin:2px 0;}
.menu a{display:flex;align-items:center;gap:11px;padding:10px 12px;border-radius:7px;color:#b9c4d6;text-decoration:none;font-size:13.5px;font-weight:500;transition:background .15s,color .15s;}
.menu a:hover{background:rgba(255,255,255,.07);color:#fff;}
.menu a.active{background:var(--brand);color:#fff;font-weight:600;}
.menu .ni{width:18px;height:18px;flex:none;}
.main{margin-left:236px;min-height:100vh;}
.top-bar{position:sticky;top:0;z-index:20;display:flex;justify-content:flex-end;align-items:center;gap:14px;height:56px;padding:0 28px;background:#fff;border-bottom:1px solid var(--line);}
.content{padding:24px 28px 48px;max-width:1720px;}
.user{display:flex;align-items:center;gap:10px;}
.u-avatar{width:32px;height:32px;border-radius:50%;background:var(--brand-soft);color:var(--brand-dark);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;flex:none;}
.u-meta{display:flex;flex-direction:column;line-height:1.25;}
.u-meta b{font-size:13px;font-weight:600;}
.u-meta small{font-size:11.5px;color:var(--muted);}
.logout{margin-left:6px;padding:6px 11px;border:1px solid var(--line);border-radius:6px;color:#475569;text-decoration:none;font-size:12.5px;font-weight:600;}
.logout:hover{background:#f6f8fb;}
.header{display:flex;justify-content:space-between;align-items:center;gap:16px;margin:0 0 18px;}
.header h1{font-size:20px;font-weight:700;color:var(--ink);letter-spacing:-.2px;}
.header p{margin-top:2px;color:var(--muted);font-size:13px;}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:16px;}
.card,.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;}
.card{padding:16px 18px;}
.card-title{color:var(--muted);font-size:12.5px;font-weight:500;margin-bottom:6px;}
.card-value{font-size:26px;font-weight:700;color:var(--ink);letter-spacing:-.4px;line-height:1.2;}
.panel{padding:20px;margin-bottom:16px;min-width:0;}
.panel h2{font-size:15px;font-weight:700;margin-bottom:14px;color:var(--ink);}
.muted{color:var(--muted);font-size:12.5px;}
.grid-main{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:16px;}
.grid-main>.panel{margin-bottom:16px;}
.form-grid{display:grid;gap:14px;align-items:end;}
.form-category{grid-template-columns:minmax(0,1fr) auto;}
.form-item{grid-template-columns:minmax(1.5fr,2fr) minmax(120px,1fr) minmax(140px,1fr) auto;}
.form-inventory,.form-promotion{grid-template-columns:repeat(4,minmax(110px,1fr)) auto;}
.form-group label{display:block;color:#475569;font-size:12.5px;font-weight:600;margin-bottom:6px;}
input:not([type=checkbox]):not([type=radio]),select,textarea{width:100%;min-width:0;padding:8px 11px;background:#fff;color:var(--ink);border:1px solid #cfd7e3;border-radius:6px;font-size:13.5px;outline:none;transition:border-color .15s,box-shadow .15s;}
input[type=checkbox],input[type=radio]{width:auto;accent-color:var(--brand);}
input:focus,select:focus,textarea:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(23,105,224,.14);}
input::placeholder{color:#9aa6b8;}
input:disabled,select:disabled{background:#f3f5f9;color:#8a96a8;}
.btn{display:inline-flex;justify-content:center;align-items:center;gap:6px;padding:8px 14px;border:1px solid transparent;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;text-decoration:none;white-space:nowrap;line-height:1.3;transition:background .15s,border-color .15s;}
.btn-primary{background:var(--brand);color:#fff;}
.btn-primary:hover{background:var(--brand-dark);}
.btn-secondary{background:#fff;color:#334155;border-color:#cfd7e3;}
.btn-secondary:hover{background:#f6f8fb;}
.btn-warning{background:#fff;color:#a15c07;border-color:#e8c680;}
.btn-warning:hover{background:#fff8e8;}
.btn-danger{background:#fff;color:#c0262d;border-color:#f0b4b7;}
.btn-danger:hover{background:#fff3f3;}
.btn:disabled,.btn[disabled]{opacity:.55;cursor:not-allowed;}
table{width:100%;border-collapse:collapse;}
.panel table{min-width:650px;}
.panel:has(table){overflow-x:auto;}
th,td{padding:11px 12px;text-align:left;border-bottom:1px solid #edf0f5;vertical-align:middle;}
th{background:#f6f8fb;color:#5b6b82;font-size:12px;font-weight:600;white-space:nowrap;border-top:1px solid #edf0f5;}
td{font-size:13px;color:#334155;}
tbody tr:hover td{background:#f9fbfe;}
tbody tr:last-child td{border-bottom:0;}
.empty{text-align:center;padding:28px;color:#94a0b3;}
.status{display:inline-flex;align-items:center;padding:3px 9px;border-radius:999px;font-size:11.5px;font-weight:600;white-space:nowrap;}
.green{background:#e4f5ea;color:#17703f;}
.yellow{background:#fdf1d8;color:#8f5b00;}
.red{background:#fde7e7;color:#b42318;}
.blue{background:#e5eefc;color:#1257bd;}
.gray{background:#eef1f6;color:#51607a;}
.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.actions form{margin:0;}
.message{padding:11px 14px;border-radius:6px;margin-bottom:14px;background:#fdecec;color:#a8201a;border:1px solid #f6c6c6;font-size:13px;}
.notice{padding:11px 14px;border-radius:6px;background:#e8f0fd;color:#1257bd;margin-bottom:14px;border:1px solid #c9dcf8;font-size:13px;}
.order-total{text-align:right;font-size:18px;font-weight:700;color:var(--ink);margin-top:18px;}
.food-thumb{width:48px;height:40px;object-fit:cover;border-radius:6px;background:#eef2f7;border:1px solid var(--line);display:block;}
.food-name{font-weight:600;color:var(--ink);}
.rc-ico{display:none;}
.actions input:not([type=checkbox]):not([type=radio]),.actions select{width:auto;flex:0 1 240px;min-width:150px;}
#kitchen-alert{margin-bottom:14px;}
.tabs{margin-bottom:16px;}
.tab-bar{margin-bottom:16px;gap:8px;}
.tab-pane{display:none;}
.tab-pane.on{display:block;}
.tab-badge{display:inline-flex;align-items:center;justify-content:center;min-width:20px;height:18px;padding:0 6px;border-radius:999px;background:rgba(15,34,64,.08);font-size:11px;font-weight:700;}
.btn-primary .tab-badge{background:rgba(255,255,255,.25);}
#kitchen-alert:empty{margin:0;}
.toast{position:fixed;top:68px;right:20px;z-index:2000;max-width:420px;padding:12px 16px;border-radius:8px;background:#16794a;color:#fff;font-weight:600;font-size:13.5px;box-shadow:0 10px 30px rgba(0,0,0,.2);animation:toast-in .3s ease;transition:opacity .45s,transform .45s;}
.toast.hide{opacity:0;transform:translateY(-10px);}
@keyframes toast-in{from{opacity:0;transform:translateY(-12px);}to{opacity:1;transform:none;}}
@media(max-width:1250px){.cards{grid-template-columns:repeat(2,minmax(0,1fr));}.form-item,.form-inventory,.form-promotion{grid-template-columns:repeat(2,minmax(0,1fr));}}
@media(max-width:900px){.sidebar{position:relative;width:100%;height:auto;padding:0 10px 10px;}.menu{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px;}.menu a{padding:8px 10px;font-size:12.5px;}.main{margin-left:0;}.top-bar{position:static;padding:0 14px;}.content{padding:16px 14px 32px;}.header{align-items:flex-start;flex-direction:column;}.grid-main{grid-template-columns:1fr;}}
@media(max-width:520px){.cards{grid-template-columns:1fr 1fr;}.form-category,.form-item,.form-inventory,.form-promotion{grid-template-columns:1fr;}.u-meta{display:none;}}
"""

NAV_SVG = ('<svg class="ni" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
           'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>')
NAV_ICONS = {
    "home": '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/>',
    "categories": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "materials": '<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/>',
    "recipes": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h6"/>',
    "promotions": '<path d="M20 12l-8 8-9-9V3h8z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    "customers": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14c2.2.6 3.5 2.5 3.5 6"/>',
    "orders": '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>',
    "analysis": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "users": '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
}


def render_page(title, body, active):
    user = get_current_user()

    nav = [("/", "Tổng quan", "home")]

    if user:
        if has_perm(user, "menu"):
            nav.append(("/categories", "Danh mục món", "categories"))
        nav.append(("/materials", "Nguyên liệu", "materials"))
        if has_perm(user, "recipes"):
            nav.append(("/recipes", "Công thức", "recipes"))
        if has_perm(user, "promotions"):
            nav.append(("/promotions", "Khuyến mại", "promotions"))
        if has_perm(user, "customers"):
            nav.append(("/customers", "Khách hàng", "customers"))
        nav.append(("/orders", "Đặt món", "orders"))
        if has_perm(user, "analysis"):
            nav.append(("/analysis", "Phân tích", "analysis"))
        if has_perm(user, "users"):
            nav.append(("/users", "Tài khoản", "users"))

    menu_parts = []
    for href, text, key in nav:
        active_class = "active" if active == key else ""
        icon = NAV_SVG.format(NAV_ICONS.get(key, ""))
        menu_parts.append(f'<li><a href="{href}" class="{active_class}">{icon}<span>{text}</span></a></li>')
    menu = "".join(menu_parts)

    if user:
        role_label = {
            "admin": "Quản lý",
            "cashier": "Thu ngân",
            "waiter": "Phục vụ",
            "chef": "Đầu bếp"
        }.get(user["role"], user["role"])
        initial = (user["full_name"].strip() or "?")[0].upper()
        user_block = f"""
        <div class="user">
            <span class="u-avatar">{escape(initial)}</span>
            <span class="u-meta"><b>{escape(user["full_name"])}</b><small>{role_label}</small></span>
            <a class="logout" href="/logout">Đăng xuất</a>
        </div>
        """
    else:
        user_block = ""

    # Chuông thông báo cạnh tài khoản: gom các cảnh báo tồn kho và hạn sử dụng.
    bell_block = ""
    if user:
        _nc = get_db()
        try:
            bell_block = notification_bell_html(build_notifications(get_inventory_alerts(_nc)), user["user_id"])
        finally:
            _nc.close()

    inv_check_block = pop_inventory_check_html() if user else ""

    toast_msg = request.args.get("ok") or request.args.get("success") or ""
    toast_block = ""
    if toast_msg:
        toast_block = f'<div id="toast" class="toast">{escape(toast_msg)}</div>' + """
        <script>
        (function () {
            var t = document.getElementById('toast');
            setTimeout(function () { if (t) t.classList.add('hide'); }, 4200);
            setTimeout(function () { if (t) t.remove(); }, 4800);
            try {
                var u = new URL(location.href);
                u.searchParams.delete('ok'); u.searchParams.delete('success');
                history.replaceState(null, '', u.pathname + u.search + u.hash);
            } catch (e) {}
        })();
        </script>"""

    kitchen_block = ""
    if has_perm(user, "orders_kitchen"):
        _c = get_db()
        _alerts = get_kitchen_alerts(_c)
        _c.close()
        kitchen_block = f"""<div id="kitchen-alert">{kitchen_alert_html(_alerts)}</div>
        <script>
        (function() {{
            var last = {len(_alerts)};
            function beep() {{
                try {{
                    var ctx = new (window.AudioContext || window.webkitAudioContext)();
                    var o = ctx.createOscillator(); o.frequency.value = 880; o.connect(ctx.destination);
                    o.start(); setTimeout(function() {{ o.stop(); ctx.close(); }}, 250);
                }} catch (e) {{}}
            }}
            setInterval(function() {{
                fetch('/kitchen/alerts').then(function(r) {{ return r.json(); }}).then(function(d) {{
                    document.getElementById('kitchen-alert').innerHTML = d.html;
                    if (d.count > last) beep();
                    last = d.count;
                }}).catch(function() {{}});
            }}, 10000);
        }})();
        </script>"""

    return f"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{escape(title)} - Ocean Restaurant</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>{APP_CSS}</style>
</head>
<body>
    <div class="sidebar">
        <div class="logo">
            <span class="logo-mark">O</span>
            <span class="logo-text">Ocean Restaurant<small>Quản lý nhà hàng</small></span>
        </div>
        <ul class="menu">{menu}</ul>
    </div>
    <div class="main">
        <div class="top-bar">
            {bell_block}
            {user_block}
        </div>
        <div class="content">
        {kitchen_block}
        {body}
        </div>
    </div>
    {inv_check_block}
    {toast_block}
    {DATEPICKER_ASSETS}
    {TABS_JS}
</body>
</html>
"""


# =========================================================
# ĐĂNG NHẬP / ĐĂNG XUẤT
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,)
        ).fetchone()
        conn.close()

        if not user:
            error = "Tên đăng nhập không tồn tại."
        elif not user["active"]:
            error = "Tài khoản đã bị khóa."
        elif user["password_hash"] != hash_password(password):
            error = "Mật khẩu không đúng."
        else:
            session["user_id"] = user["user_id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            next_url = request.args.get("next") or url_for("home")
            return redirect(next_url)

    error_html = f'<div class="message">{escape(error)}</div>' if error else ""

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Đăng nhập - Ocean Restaurant</title>
    <style>
        * {{ box-sizing:border-box; margin:0; padding:0; font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; }}
        body {{ min-height:100vh; display:grid; grid-template-columns:1.1fr 1fr; background:#fff; color:#1f2a3d; -webkit-font-smoothing:antialiased; }}
        .side {{ background:#0f2240; color:#fff; padding:56px; display:flex; flex-direction:column; justify-content:space-between; }}
        .brand {{ display:flex; align-items:center; gap:12px; font-size:18px; font-weight:700; }}
        .mark {{ width:38px; height:38px; border-radius:9px; background:#1769e0; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:19px; }}
        .side h2 {{ font-size:34px; line-height:1.2; font-weight:700; letter-spacing:-.5px; max-width:420px; }}
        .side p {{ margin-top:14px; color:#9db0cc; font-size:15px; max-width:420px; }}
        .copy {{ color:#6f84a6; font-size:12.5px; }}
        .form-side {{ display:flex; align-items:center; justify-content:center; padding:40px 24px; }}
        .box {{ width:100%; max-width:380px; }}
        .box h1 {{ font-size:24px; font-weight:700; margin-bottom:24px; }}
        .group {{ margin-bottom:16px; }}
        .group label {{ display:block; font-size:13px; font-weight:600; color:#475569; margin-bottom:6px; }}
        input {{ width:100%; padding:11px 12px; border:1px solid #cfd7e3; border-radius:6px; font-size:14px; outline:none; }}
        input:focus {{ border-color:#1769e0; box-shadow:0 0 0 3px rgba(23,105,224,.14); }}
        .btn {{ display:block; width:100%; padding:12px; border:0; border-radius:6px; cursor:pointer; font-size:14.5px; background:#1769e0; color:#fff; font-weight:600; margin-top:8px; }}
        .btn:hover {{ background:#1257bd; }}
        .message {{ padding:11px 14px; border-radius:6px; margin-bottom:16px; background:#fdecec; color:#a8201a; border:1px solid #f6c6c6; font-size:13.5px; }}
        @media (max-width:860px) {{ body {{ grid-template-columns:1fr; }} .side {{ display:none; }} }}
    </style>
</head>
<body>
    <div class="side">
        <div class="brand"><span class="mark">O</span>Ocean Restaurant</div>
        <div>
            <h2>Hệ thống quản lý nhà hàng</h2>
            <p>Đặt món, kho nguyên liệu, khuyến mại và báo cáo trong một nơi.</p>
        </div>
        <div class="copy">Ocean Restaurant</div>
    </div>
    <div class="form-side">
        <div class="box">
            <h1>Đăng nhập</h1>
            {error_html}
            <form method="POST" action="/login">
                <div class="group">
                    <label>Tên đăng nhập</label>
                    <input name="username" required autofocus autocomplete="username">
                </div>
                <div class="group">
                    <label>Mật khẩu</label>
                    <input type="password" name="password" required autocomplete="current-password">
                </div>
                <button class="btn">Đăng nhập</button>
            </form>
        </div>
    </div>
</body>
</html>
"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================================================
# TỔNG QUAN
# =========================================================

@app.route("/")
@login_required
def home():
    conn = get_db()
    today = date.today()
    t0 = today.isoformat()
    row = conn.execute("""
        SELECT COUNT(*) AS n,
               COALESCE(SUM(CASE WHEN status = 'Đã hoàn thành' THEN final_total ELSE 0 END), 0) AS rev
        FROM orders WHERE status != 'Đã hủy' AND date(order_date) = ?
    """, (t0,)).fetchone()
    orders_today, revenue_today = int(row["n"] or 0), float(row["rev"] or 0)
    open_orders = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE status IN ('Chờ xác nhận', 'Đã xác nhận', 'Đang chuẩn bị')").fetchone()[0]
    alerts = get_inventory_alerts(conn)
    since = (today - timedelta(days=6)).isoformat()
    per_day = {str(r["d"]): int(r["c"]) for r in conn.execute("""
        SELECT date(order_date) AS d, COUNT(*) AS c FROM orders
        WHERE status != 'Đã hủy' AND date(order_date) >= ? GROUP BY date(order_date)
    """, (since,)).fetchall()}
    recent = conn.execute("SELECT * FROM orders ORDER BY order_date DESC, order_id DESC LIMIT 8").fetchall()
    conn.close()

    bars = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        bars.append({"label": d.strftime("%d/%m"), "value": per_day.get(d.isoformat(), 0)})
    chart = column_chart_svg(bars, "#1769e0", "đơn")

    notes = build_notifications(alerts)[:6]
    if notes:
        alert_rows = "".join(
            f'<tr><td><a href="{escape(n["href"])}" style="text-decoration:none;color:inherit;"><strong>{escape(n["title"])}</strong>'
            f'<br><span class="muted">{escape(n["sub"])}</span></a></td></tr>' for n in notes)
    else:
        alert_rows = '<tr><td class="empty">Kho ở mức an toàn.</td></tr>'

    order_rows = ""
    for o in recent:
        total = o["final_total"] if o["final_total"] else o["total"]
        od = str(o["order_date"] or "")
        when = f"{od[8:10]}/{od[5:7]} {od[11:16]}"
        order_rows += f"""<tr>
            <td><a href="/orders/{escape(o["order_id"])}">#{escape(o["order_id"])}</a></td>
            <td>{escape(o["customer_name"])}</td><td>{escape(o["table_number"] or "")}</td><td>{when}</td>
            <td>{float(total or 0):,.0f} đ</td>
            <td><span class="status {css_status(o["status"])}">{escape(o["status"])}</span></td></tr>"""
    if not order_rows:
        order_rows = '<tr><td colspan="6" class="empty">Chưa có đơn hàng nào.</td></tr>'

    body = f"""
    <div class="header"><h1>Tổng quan</h1></div>
    <div class="cards">
        <div class="card"><div class="card-title">Doanh thu hôm nay</div><div class="card-value">{revenue_today:,.0f} đ</div></div>
        <div class="card"><div class="card-title">Đơn hôm nay</div><div class="card-value">{orders_today}</div></div>
        <div class="card"><div class="card-title">Đơn đang xử lý</div><div class="card-value">{open_orders}</div></div>
        <div class="card"><div class="card-title">Cảnh báo kho</div><div class="card-value" style="color:{'#b42318' if alerts else '#17703f'};">{len(alerts)}</div></div>
    </div>
    <div class="grid-main">
        <div class="panel"><h2>Số đơn 7 ngày gần nhất</h2>{chart}</div>
        <div class="panel"><h2>Cảnh báo kho</h2>
            <table style="min-width:0;"><tbody>{alert_rows}</tbody></table>
            <div style="margin-top:12px;"><a class="btn btn-secondary" href="/inventory/check">Xem tất cả</a></div>
        </div>
    </div>
    <div class="panel">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
            <h2 style="margin:0;">Đơn hàng gần đây</h2><a class="btn btn-secondary" href="/orders">Xem tất cả</a>
        </div>
        <table><thead><tr><th>Mã đơn</th><th>Khách hàng</th><th>Bàn</th><th>Thời gian</th><th>Thành tiền</th><th>Trạng thái</th></tr></thead>
        <tbody>{order_rows}</tbody></table>
    </div>
    """
    return render_page("Tổng quan", body, "home")


# =========================================================
# QUẢN LÝ DANH MỤC
# =========================================================

@app.route("/categories")
@role_required("admin", "cashier")
def categories():
    conn = get_db()
    category_search = request.args.get("category_search", "").strip()
    item_search = request.args.get("item_search", "").strip()
    category_sort = request.args.get("category_sort", "name")
    item_sort = request.args.get("item_sort", "name")
    category_order = {"name": "name", "id": "category_id", "status": "active DESC, name"}.get(category_sort, "name")
    item_order = {"name": "items.name", "price_asc": "items.price ASC", "price_desc": "items.price DESC", "id": "items.item_id", "category": "categories.name, items.name"}.get(item_sort, "items.name")
    categories_data = conn.execute(f"SELECT * FROM categories WHERE name LIKE ? ORDER BY {category_order}", (f"%{category_search}%",)).fetchall()
    active_categories_for_form = conn.execute("SELECT * FROM categories WHERE active = 1 ORDER BY name").fetchall()
    items = conn.execute(f"""
        SELECT items.*, categories.name AS category_name
        FROM items JOIN categories ON items.category_id = categories.category_id
        WHERE items.name LIKE ? OR categories.name LIKE ? OR items.item_id LIKE ?
        ORDER BY {item_order}
    """, (f"%{item_search}%", f"%{item_search}%", f"%{item_search}%")).fetchall()
    conn.close()

    error = request.args.get("error", "")
    message = f'<div class="message">{escape(error)}</div>' if error else ""

    category_rows = ""
    for category in categories_data:
        category_rows += f"""
        <tr>
            <td>{category["category_id"]}</td>
            <td>{escape(category["name"])}</td>
            <td><span class="status {"green" if category["active"] else "gray"}">{"Đang sử dụng" if category["active"] else "Ngừng sử dụng"}</span></td>
            <td><form method="POST" action="/categories/update/{category["category_id"]}" class="actions">
                <input name="name" value="{escape(category["name"])}" required style="min-width:140px;">
                <button class="btn btn-primary">Lưu tên</button>
            </form></td>
            <td><form method="POST" action="/categories/toggle/{category["category_id"]}">
                <button class="btn {"btn-warning" if category["active"] else "btn-primary"}">{"Ngừng sử dụng" if category["active"] else "Kích hoạt lại"}</button>
            </form></td>
        </tr>
        """
    if not category_rows:
        category_rows = '<tr><td colspan="5" class="empty">Chưa có danh mục nào.</td></tr>'

    category_options = ""
    for category in active_categories_for_form:
        category_options += f'<option value="{category["category_id"]}">{escape(category["name"])}</option>'
    if not category_options:
        category_options = '<option value="">Chưa có danh mục</option>'

    item_rows = ""
    food_images = {
        "DM01": "https://images.unsplash.com/photo-1547592180-85f173990554?auto=format&fit=crop&w=160&h=120&q=80",
        "DM02": "https://images.unsplash.com/photo-1559737558-2f5a35f4523b?auto=format&fit=crop&w=160&h=120&q=80",
        "DM03": "https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&w=160&h=120&q=80",
        "DM04": "https://images.unsplash.com/photo-1512058564366-18510be2db19?auto=format&fit=crop&w=160&h=120&q=80",
        "DM05": "https://images.unsplash.com/photo-1512621776951-a57141f2e3c1?auto=format&fit=crop&w=160&h=120&q=80",
        "DM06": "https://images.unsplash.com/photo-1472476443507-c7a5948772fc?auto=format&fit=crop&w=160&h=120&q=80",
        "DM07": "https://images.unsplash.com/photo-1532550907401-a500c9a57435?auto=format&fit=crop&w=160&h=120&q=80",
        "DM08": "https://images.unsplash.com/photo-1544025162-d76694265947?auto=format&fit=crop&w=160&h=120&q=80",
        "DM09": "https://images.unsplash.com/photo-1513558161293-cdaf765edfd7?auto=format&fit=crop&w=160&h=120&q=80",
    }
    for item in items:
        food_image = food_images.get(item["category_id"], "https://images.unsplash.com/photo-1547592180-85f173990554?auto=format&fit=crop&w=160&h=120&q=80")
        item_rows += f"""
        <tr>
            <td><img class="food-thumb" src="{food_image}" alt="Ảnh món {escape(item["name"])}" loading="lazy" onerror="this.onerror=null;this.src='{FOOD_PLACEHOLDER}'"></td>
            <td>{item["item_id"]}</td>
            <td><span class="food-name">{escape(item["name"])}</span></td>
            <td>{escape(item["category_name"])}</td>
            <td>{item["price"]:,.0f} đ</td>
            <td><span class="status {css_status(item["status"])}">{escape(item["status"])}</span></td>
            <td>
                <div class="actions">
                    <a class="btn btn-warning" href="/items/edit/{item["item_id"]}">Sửa</a>
                    <form method="POST" action="/items/delete/{item["item_id"]}">
                        <button class="btn btn-danger">Xóa</button>
                    </form>
                </div>
            </td>
        </tr>
        """
    if not item_rows:
        item_rows = '<tr><td colspan="7" class="empty">Chưa có mặt hàng nào.</td></tr>'

    def cat_opt(value, label, current):
        return f'<option value="{value}" {"selected" if current == value else ""}>{label}</option>'

    items_html = f"""
    <div class="panel">
        <form method="GET" action="/categories#items" class="actions" style="margin-bottom:12px;">
            <input name="item_search" value="{escape(item_search)}" placeholder="Tìm mã, tên món hoặc danh mục">
            <select name="item_sort">{cat_opt("name", "Tên A–Z", item_sort)}{cat_opt("price_asc", "Giá tăng dần", item_sort)}{cat_opt("price_desc", "Giá giảm dần", item_sort)}{cat_opt("id", "Mã ID", item_sort)}{cat_opt("category", "Danh mục", item_sort)}</select>
            <input type="hidden" name="category_search" value="{escape(category_search)}"><input type="hidden" name="category_sort" value="{escape(category_sort)}">
            <button class="btn btn-primary">Lọc mặt hàng</button><a class="btn btn-secondary" href="/categories#items">Đặt lại</a>
        </form>
        <table>
            <thead>
                <tr><th>Ảnh</th><th>Mã</th><th>Tên món</th><th>Danh mục</th><th>Giá</th><th>Trạng thái</th><th>Thao tác</th></tr>
            </thead>
            <tbody>{item_rows}</tbody>
        </table>
    </div>"""

    add_item_html = f"""
    <div class="panel">
        <form method="POST" action="/items/add">
            <div class="form-grid form-item">
                <div class="form-group"><label>Tên mặt hàng</label><input name="name" required></div>
                <div class="form-group"><label>Giá bán</label><input type="number" name="price" min="0" step="any" required></div>
                <div class="form-group"><label>Danh mục</label><select name="category_id" required>{category_options}</select></div>
                <button class="btn btn-primary">+ Thêm mặt hàng</button>
            </div>
        </form>
    </div>"""

    cats_html = f"""
    <div class="panel">
        <h2>Thêm danh mục</h2>
        <form method="POST" action="/categories/add">
            <div class="form-grid form-category">
                <input name="name" placeholder="Ví dụ: Món chính" required>
                <button class="btn btn-primary">+ Thêm danh mục</button>
            </div>
        </form>
    </div>
    <div class="panel">
        <h2>Danh sách danh mục</h2>
        <form method="GET" action="/categories#cats" class="actions" style="margin-bottom:12px;">
            <input name="category_search" value="{escape(category_search)}" placeholder="Tìm tên danh mục">
            <select name="category_sort">{cat_opt("name", "Tên A–Z", category_sort)}{cat_opt("id", "Mã ID", category_sort)}{cat_opt("status", "Trạng thái", category_sort)}</select>
            <input type="hidden" name="item_search" value="{escape(item_search)}"><input type="hidden" name="item_sort" value="{escape(item_sort)}">
            <button class="btn btn-primary">Lọc danh mục</button><a class="btn btn-secondary" href="/categories#cats">Đặt lại</a>
        </form>
        <table>
            <thead><tr><th>Mã</th><th>Tên danh mục</th><th>Trạng thái</th><th>Chỉnh sửa</th><th>Thao tác</th></tr></thead>
            <tbody>{category_rows}</tbody>
        </table>
    </div>"""

    menu_tabs = tabs_html("menu", [
        ("items", "Danh sách món", items_html, len(items)),
        ("add-item", "+ Thêm món", add_item_html),
        ("cats", "Danh mục", cats_html, len(categories_data)),
    ])
    body = f"""
    <div class="header">
        <div><h1>Quản lý danh mục</h1></div>
    </div>
    {message}
    {menu_tabs}
    """
    return render_page("Quản lý danh mục", body, "categories")


@app.route("/categories/add", methods=["POST"])
@role_required("admin", "cashier")
def add_category():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("categories", _anchor="cats", error="Tên danh mục không được để trống."))
    conn = get_db()
    try:
        conn.execute("INSERT INTO categories(category_id, name, active) VALUES(?, ?, 1)", (next_code(conn, "categories", "DM"), name))
        conn.commit()
    except IntegrityError:
        conn.close()
        return redirect(url_for("categories", _anchor="cats", error="Tên danh mục đã tồn tại."))
    conn.close()
    return redirect(url_for("categories", _anchor="cats"))


@app.route("/categories/update/<string:id>", methods=["POST"])
@role_required("admin", "cashier")
def update_category(id):
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("categories", _anchor="cats", error="Tên danh mục không được để trống."))
    conn = get_db()
    try:
        conn.execute("UPDATE categories SET name = ? WHERE category_id = ?", (name, id))
        conn.commit()
    except IntegrityError:
        conn.close()
        return redirect(url_for("categories", _anchor="cats", error="Tên danh mục đã được sử dụng."))
    conn.close()
    return redirect(url_for("categories", _anchor="cats"))


@app.route("/categories/toggle/<string:id>", methods=["POST"])
@role_required("admin", "cashier")
def toggle_category(id):
    conn = get_db()
    category = conn.execute("SELECT * FROM categories WHERE category_id = ?", (id,)).fetchone()
    if not category:
        conn.close()
        return redirect(url_for("categories", _anchor="cats", error="Không tìm thấy danh mục."))
    conn.execute("UPDATE categories SET active = ? WHERE category_id = ?", (0 if category["active"] else 1, id))
    conn.commit()
    conn.close()
    return redirect(url_for("categories", _anchor="cats"))


@app.route("/categories/delete/<string:id>", methods=["POST"])
@role_required("admin", "cashier")
def delete_category(id):
    # Giữ tương thích route cũ nhưng chuyển sang ngừng sử dụng thay vì xóa dữ liệu.
    return redirect(url_for("toggle_category", id=id), code=307)


@app.route("/items/add", methods=["POST"])
@role_required("admin", "cashier")
def add_item():
    name = request.form.get("name", "").strip()
    category_id = request.form.get("category_id", "")
    try:
        price = float(request.form.get("price", 0))
    except ValueError:
        price = 0

    if name and category_id and price >= 0:
        conn = get_db()
        conn.execute("INSERT INTO items(item_id, name, price, category_id) VALUES(?, ?, ?, ?)",
                     (next_item_code(conn, category_id), name, price, category_id))
        conn.commit()
        conn.close()
    return redirect(url_for("categories", _anchor="items"))


@app.route("/items/edit/<string:id>")
@role_required("admin", "cashier")
def edit_item(id):
    conn = get_db()
    item = conn.execute("SELECT * FROM items WHERE item_id = ?", (id,)).fetchone()
    categories_data = conn.execute("SELECT * FROM categories WHERE active = 1 ORDER BY name").fetchall()
    conn.close()

    if not item:
        return redirect(url_for("categories", _anchor="items"))

    options = ""
    for category in categories_data:
        selected = "selected" if category["category_id"] == item["category_id"] else ""
        options += f'<option value="{category["category_id"]}" {selected}>{escape(category["name"])}</option>'

    body = f"""
    <div class="header">
        <div><h1>Sửa mặt hàng</h1></div>
    </div>
    <div class="panel">
        <form method="POST" action="/items/update/{id}">
            <div class="form-grid" style="grid-template-columns: 1.5fr 1fr 1fr 1fr;">
                <div class="form-group"><label>Tên</label><input name="name" value="{escape(item["name"])}" required></div>
                <div class="form-group"><label>Giá</label><input type="number" name="price" value="{item["price"]}" min="0" step="any" required></div>
                <div class="form-group"><label>Danh mục</label><select name="category_id">{options}</select></div>
                <div class="form-group">
                    <label>Trạng thái</label>
                    <select name="status">
                        <option {"selected" if item["status"] == "Đang bán" else ""}>Đang bán</option>
                        <option {"selected" if item["status"] == "Tạm ngưng" else ""}>Tạm ngưng</option>
                    </select>
                </div>
            </div>
            <br>
            <button class="btn btn-primary">Lưu</button>
            <a class="btn btn-secondary" href="/categories">Quay lại</a>
        </form>
    </div>
    """
    return render_page("Sửa mặt hàng", body, "categories")


@app.route("/items/update/<string:id>", methods=["POST"])
@role_required("admin", "cashier")
def update_item(id):
    name = request.form.get("name", "").strip()
    category_id = request.form.get("category_id", "")
    status = request.form.get("status", "Đang bán")
    try:
        price = float(request.form.get("price", 0))
    except ValueError:
        price = 0

    conn = get_db()
    conn.execute("""
        UPDATE items SET name = ?, price = ?, category_id = ?, status = ? WHERE item_id = ?
    """, (name, price, category_id, status, id))
    conn.commit()
    conn.close()
    return redirect(url_for("categories", _anchor="items"))


@app.route("/items/delete/<string:id>", methods=["POST"])
@role_required("admin", "cashier")
def delete_item(id):
    conn = get_db()
    conn.execute("DELETE FROM items WHERE item_id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("categories", _anchor="items"))


# =========================================================
# NGUYÊN LIỆU TỒN KHO
# =========================================================

# =========================================================
# QUẢN LÝ NGUYÊN LIỆU  (danh mục  |  tồn kho  |  kiểm tra & cảnh báo)
# =========================================================

STATUS_ORDER = {"Hết hàng": 0, "Hết hạn": 0, "Sắp hết": 1, "Sắp hết hạn": 1, "Còn hàng": 2, "Còn hạn": 2, "Chưa khai báo": 2}

# ---------- Hạn sử dụng & khuyến mại xả hàng ----------
EXPIRY_SOON_DAYS = 7
# (còn <= n ngày, giảm %) - kiểm tra từ trên xuống
EXPIRY_DISCOUNT_RULES = [(1, 30), (3, 20), (7, 10)]


def parse_iso_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def expiry_info(expiry_date, today=None):
    """-> (trạng thái, số ngày còn lại)"""
    d = parse_iso_date(expiry_date)
    if not d:
        return "Chưa khai báo", None
    days = (d - (today or date.today())).days
    if days < 0:
        return "Hết hạn", days
    if days <= EXPIRY_SOON_DAYS:
        return "Sắp hết hạn", days
    return "Còn hạn", days


def expiry_discount(days_left):
    if days_left is None or days_left < 0:
        return 0
    for limit, pct in EXPIRY_DISCOUNT_RULES:
        if days_left <= limit:
            return pct
    return 0


def days_text(days):
    if days is None:
        return ""
    if days < 0:
        return f"quá hạn {abs(days)} ngày"
    if days == 0:
        return "hết hạn hôm nay"
    return f"còn {days} ngày"


def expiry_chip_html(expiry_date):
    if not parse_iso_date(expiry_date):
        return '<span class="status gray">Chưa khai báo HSD</span>'
    status, days = expiry_info(expiry_date)
    return f'<span class="status {css_status(status)}">{status} · {days_text(days)}</span>'


# ---------- Lô nguyên liệu theo hạn sử dụng ----------
# Mỗi lần nhập có hạn sử dụng riêng được lưu thành một lô (cùng hạn sử dụng thì gộp chung một lô).
# ingredients.quantity luôn bằng tổng các lô; ingredients.expiry_date là hạn gần nhất trong các lô còn hàng.

def fmt_date(value):
    d = parse_iso_date(value)
    return d.strftime("%d/%m/%Y") if d else "Chưa khai báo"


def fmt_num(value):
    """Số gọn, không làm tròn mất phần lẻ (dùng cho value/max của ô nhập)."""
    text = f"{round(float(value), 4):.4f}".rstrip("0").rstrip(".")
    return text or "0"


def batch_sync(conn, ingredient_id):
    """Cập nhật ingredients.expiry_date = hạn gần nhất trong các lô còn hàng."""
    row = conn.execute(
        "SELECT MIN(expiry_date) AS d FROM ingredient_batches WHERE ingredient_id = ? AND quantity > 0",
        (ingredient_id,)).fetchone()
    nearest = str(row["d"])[:10] if row and row["d"] else None
    conn.execute("UPDATE ingredients SET expiry_date = ? WHERE ingredient_id = ?", (nearest, ingredient_id))


def batch_add(conn, ingredient_id, amount, expiry=None):
    """Nhập thêm vào lô có cùng hạn sử dụng, chưa có thì tạo lô mới."""
    amount = round(float(amount), 4)
    if amount <= 0:
        return
    if expiry:
        row = conn.execute(
            "SELECT batch_id FROM ingredient_batches WHERE ingredient_id = ? AND expiry_date = ? LIMIT 1",
            (ingredient_id, expiry)).fetchone()
    else:
        row = conn.execute(
            "SELECT batch_id FROM ingredient_batches WHERE ingredient_id = ? AND expiry_date IS NULL LIMIT 1",
            (ingredient_id,)).fetchone()
    if row:
        conn.execute("UPDATE ingredient_batches SET quantity = quantity + ? WHERE batch_id = ?",
                     (amount, row["batch_id"]))
    else:
        conn.execute("""INSERT INTO ingredient_batches(ingredient_id, quantity, expiry_date, created_at)
                        VALUES (?, ?, ?, ?)""",
                     (ingredient_id, amount, expiry, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def batch_take(conn, ingredient_id, amount, valid_first=False):
    """Trừ số lượng khỏi các lô, lô hết hạn sớm nhất trừ trước.

    valid_first=True (chế biến món): đóng băng lô hết hạn, chỉ dùng phần chưa hết hạn.
    """
    remaining = round(float(amount), 4)
    rows = conn.execute("""
        SELECT batch_id, quantity, expiry_date FROM ingredient_batches
        WHERE ingredient_id = ? AND quantity > 0
        ORDER BY expiry_date IS NULL, expiry_date, batch_id
        FOR UPDATE
    """, (ingredient_id,)).fetchall()
    if valid_first:
        today = date.today()
        rows = [r for r in rows if expiry_info(r["expiry_date"], today)[0] != "Hết hạn"]
        if remaining > sum(float(r["quantity"]) for r in rows) + 1e-9:
            raise ValueError("Không đủ nguyên liệu còn hạn để chế biến.")
    for r in rows:
        if remaining <= 0:
            break
        have = float(r["quantity"])
        used = min(have, remaining)
        left = round(have - used, 4)
        if left <= 0:
            conn.execute("DELETE FROM ingredient_batches WHERE batch_id = ?", (r["batch_id"],))
        else:
            conn.execute("UPDATE ingredient_batches SET quantity = ? WHERE batch_id = ?", (left, r["batch_id"]))
        remaining = round(remaining - used, 4)


def usable_stock_for_cooking(conn, ingredient_id, total_stock):
    """Khóa các lô và chỉ tính phần chưa hết hạn để chế biến."""
    rows = conn.execute("""
        SELECT quantity, expiry_date FROM ingredient_batches
        WHERE ingredient_id = ? AND quantity > 0
        ORDER BY expiry_date IS NULL, expiry_date, batch_id
        FOR UPDATE
    """, (ingredient_id,)).fetchall()
    today = date.today()
    usable = sum(float(r["quantity"]) for r in rows
                 if expiry_info(r["expiry_date"], today)[0] != "Hết hạn")
    return min(float(total_stock), usable)


def batch_restore(conn, ingredient_id, amount):
    """Hoàn kho khi hủy món/đơn: trả về lô còn hạn gần nhất; không còn lô nào thì tạo lô chưa khai báo hạn."""
    amount = round(float(amount), 4)
    if amount <= 0:
        return
    row = conn.execute("""
        SELECT batch_id FROM ingredient_batches
        WHERE ingredient_id = ? AND quantity > 0 AND expiry_date >= ?
        ORDER BY expiry_date, batch_id LIMIT 1
    """, (ingredient_id, date.today().isoformat())).fetchone()
    if row:
        conn.execute("UPDATE ingredient_batches SET quantity = quantity + ? WHERE batch_id = ?",
                     (amount, row["batch_id"]))
    else:
        batch_add(conn, ingredient_id, amount, None)


def reconcile_batches(conn):
    """Bảo đảm tổng các lô khớp với tồn kho (lần chạy đầu: mỗi nguyên liệu thành một lô theo hạn đang lưu)."""
    totals = {r["ingredient_id"]: float(r["total"] or 0) for r in conn.execute(
        "SELECT ingredient_id, SUM(quantity) AS total FROM ingredient_batches GROUP BY ingredient_id").fetchall()}
    for ing in conn.execute("SELECT ingredient_id, quantity, expiry_date FROM ingredients").fetchall():
        have = totals.get(ing["ingredient_id"])
        diff = round(float(ing["quantity"] or 0) - (have or 0), 4)
        if diff > 0:
            expiry = str(ing["expiry_date"])[:10] if (have is None and ing["expiry_date"]) else None
            batch_add(conn, ing["ingredient_id"], diff, expiry)
        elif diff < 0:
            batch_take(conn, ing["ingredient_id"], -diff)
        if diff or have is None:
            batch_sync(conn, ing["ingredient_id"])


def empty_batch_summary():
    return {"batches": [], "total": 0.0, "usable": 0.0, "expired_qty": 0.0, "expired_days": None,
            "soon_qty": 0.0, "soon_days": None, "soon_date": None, "ok_qty": 0.0, "undeclared_qty": 0.0}


def get_batch_summary(conn, today=None):
    """{ingredient_id: tổng hợp các lô còn hàng} - số lượng đã hết hạn, sắp hết hạn, còn hạn và từng lô."""
    today = today or date.today()
    result = {}
    rows = conn.execute("""
        SELECT * FROM ingredient_batches WHERE quantity > 0
        ORDER BY expiry_date IS NULL, expiry_date, batch_id
    """).fetchall()
    for r in rows:
        s = result.setdefault(r["ingredient_id"], empty_batch_summary())
        qty = float(r["quantity"])
        status, days = expiry_info(r["expiry_date"], today)
        s["batches"].append({"batch_id": r["batch_id"], "expiry": parse_iso_date(r["expiry_date"]),
                             "quantity": qty, "status": status, "days": days})
        s["total"] += qty
        if status == "Hết hạn":
            s["expired_qty"] += qty
            if s["expired_days"] is None:
                s["expired_days"] = days
        else:
            s["usable"] += qty
            if status == "Sắp hết hạn":
                s["soon_qty"] += qty
                if s["soon_days"] is None:
                    s["soon_days"], s["soon_date"] = days, parse_iso_date(r["expiry_date"])
            elif status == "Còn hạn":
                s["ok_qty"] += qty
            else:
                s["undeclared_qty"] += qty
    return result


def batch_cell_html(ingredient_id, unit, summary):
    """Ô 'Hạn sử dụng' trong bảng: 2 lô gần nhất kèm số lượng + liên kết sang trang chi tiết."""
    link = f'<a href="/materials/{escape(ingredient_id)}/batches" style="font-size:12px;">Chi tiết</a>'
    batches = summary["batches"] if summary else []
    if not batches:
        return f'<span class="muted">Không có lô</span><br>{link}'
    lines = ""
    for b in batches[:2]:
        chip = ""
        if b["status"] in ("Hết hạn", "Sắp hết hạn"):
            chip = f' <span class="status {css_status(b["status"])}">{days_text(b["days"])}</span>'
        label = b["expiry"].strftime("%d/%m/%Y") if b["expiry"] else "Chưa khai báo"
        lines += (f'<div style="white-space:nowrap;margin-bottom:3px;">{label}: '
                  f'<strong>{b["quantity"]:g} {escape(unit)}</strong>{chip}</div>')
    more = f'<span class="muted">+{len(batches) - 2} lô khác · </span>' if len(batches) > 2 else ""
    return f"{lines}{more}{link}"


def get_expiry_programs(conn, check_date=None):
    """Chương trình xả hàng tự động: mỗi nguyên liệu có lô sắp hết hạn (còn tồn) = 1 chương trình,
    áp dụng cho mọi món đang bán có dùng nguyên liệu đó. Mức giảm tính theo lô còn hạn gần nhất."""
    today = parse_iso_date(check_date) or date.today()
    summaries = get_batch_summary(conn, today)
    programs = []
    for ing in conn.execute("SELECT ingredient_id, name, unit FROM ingredients").fetchall():
        s = summaries.get(ing["ingredient_id"])
        if not s or s["soon_qty"] <= 0:
            continue
        pct = expiry_discount(s["soon_days"])
        if pct <= 0:
            continue
        programs.append({
            "program_id": f"HSD-{ing['ingredient_id']}", "ingredient_id": ing["ingredient_id"],
            "ingredient_name": ing["name"], "unit": ing["unit"], "quantity": s["soon_qty"],
            "expiry_date": s["soon_date"].isoformat(), "days_left": s["soon_days"], "discount": pct, "items": [],
        })
    if not programs:
        return []
    ids = [p["ingredient_id"] for p in programs]
    marks = ",".join(["?"] * len(ids))
    rows = conn.execute(f"""
        SELECT r.ingredient_id, r.quantity AS per_portion, it.item_id, it.name, it.price, it.category_id
        FROM recipes r JOIN items it ON it.item_id = r.item_id
        WHERE it.status = 'Đang bán' AND r.ingredient_id IN ({marks})
        ORDER BY it.name
    """, ids).fetchall()
    by_ing = {p["ingredient_id"]: p for p in programs}
    for r in rows:
        by_ing[r["ingredient_id"]]["items"].append(dict(r))
    programs.sort(key=lambda p: (p["days_left"], p["ingredient_name"]))
    return programs


def get_expiry_item_discounts(conn, check_date=None):
    """{item_id: {discount, ingredient_id, ingredient_name, days_left, program_id}} - lấy mức giảm cao nhất."""
    result = {}
    for p in get_expiry_programs(conn, check_date):
        for it in p["items"]:
            cur = result.get(it["item_id"])
            if not cur or p["discount"] > cur["discount"]:
                result[it["item_id"]] = {
                    "discount": p["discount"], "ingredient_id": p["ingredient_id"],
                    "ingredient_name": p["ingredient_name"], "days_left": p["days_left"],
                    "program_id": p["program_id"],
                }
    return result


def promo_banner_html(promos, expiry_programs, tiers):
    """Banner ưu đãi đầu trang Quản lý khuyến mại, dựng từ các chương trình đang chạy thật."""
    today = date.today().isoformat()

    def active(p):
        return promo_date(p["start_date"]) <= today <= promo_date(p["end_date"])

    order_now = sorted([p for p in promos if p["apply_type"] == "order" and active(p)
                        and promo_group(p) != "Hạng thành viên"], key=lambda p: -float(p["discount"]))
    order_next = sorted([p for p in promos if p["apply_type"] == "order" and promo_date(p["start_date"]) > today
                         and promo_group(p) != "Hạng thành viên"], key=lambda p: promo_date(p["start_date"]))
    item_now = [p for p in promos if p["apply_type"] == "item" and active(p)]
    item_next = sorted([p for p in promos if p["apply_type"] == "item" and promo_date(p["start_date"]) > today],
                       key=lambda p: promo_date(p["start_date"]))
    sale_items = {i["item_id"] for prog in expiry_programs for i in prog["items"]}
    sale_max = max([prog["discount"] for prog in expiry_programs if prog["items"]], default=0)

    def dmy(value):
        d = parse_iso_date(value)
        return d.strftime("%d/%m") if d else str(value)

    def ticket(kicker, big, line, sub, tone):
        return (f'<div class="pb-ticket {tone}"><div class="pb-kicker">{kicker}</div><div class="pb-big">{big}</div>'
                f'<div class="pb-line">{line}</div><div class="pb-sub">{sub}</div></div>')

    if order_now:
        p = order_now[0]
        cond = f'cho đơn từ {float(p["min_order_value"]):,.0f} đ' if float(p["min_order_value"] or 0) > 0 else "không cần điều kiện"
        t_order = ticket("Ưu đãi hóa đơn", f'{float(p["discount"]):g}%', "áp dụng trên tổng hóa đơn",
                         f'{escape(p["name"])} · {cond} · đến {dmy(p["end_date"])}', "gold")
        headline = escape(p["name"])
    elif order_next:
        p = order_next[0]
        t_order = ticket("Ưu đãi hóa đơn · sắp diễn ra", f'{float(p["discount"]):g}%', f'bắt đầu từ {dmy(p["start_date"])}',
                         escape(p["name"]), "ghost")
        headline = "Ưu đãi sắp diễn ra"
    else:
        t_order = ticket("Ưu đãi hóa đơn", "—", "chưa có chương trình đang chạy",
                         "Thêm ở thẻ “Thêm chương trình”", "ghost")
        headline = "Ưu đãi tại Ocean Restaurant"

    item_max = max([float(p["discount"]) for p in item_now], default=0)
    n_items = len({p["item_id"] for p in item_now} | sale_items)
    best_item = max(item_max, sale_max)
    if best_item > 0:
        parts = []
        if item_now:
            parts.append(f"{len(item_now)} chương trình theo món")
        if sale_items:
            parts.append(f"{len(sale_items)} món xả hàng theo hạn sử dụng")
        hot = hot_badge_html("Mức giảm cao nhất", 64) if is_hot_discount(sale_max) else ""
        t_item = ticket("Giảm giá món", f'{best_item:g}%{hot}', f"giảm đến mức này cho {n_items} món chọn lọc",
                        " · ".join(parts), "red")
    elif item_next:
        p = item_next[0]
        t_item = ticket("Giảm giá món · sắp diễn ra", f'{float(p["discount"]):g}%', f'bắt đầu từ {dmy(p["start_date"])}',
                        escape(p["name"]), "ghost")
    else:
        t_item = ticket("Giảm giá món", "—", "chưa có món nào đang giảm", "Thêm ở thẻ “Thêm chương trình”", "ghost")

    if tiers:
        tier_line = " · ".join(f'{escape(t["tier_name"])} {float(t["discount_percent"]):g}%' for t in tiers)
        top = max(float(t["discount_percent"]) for t in tiers)
        first = tiers[0]
        t_tier = ticket("Ưu đãi thành viên", f"{top:g}%", "giảm đến mức này theo hạng thành viên",
                        f'{tier_line} · hạng {escape(first["tier_name"])} từ {float(first["min_total_spent"]):,.0f} đ chi tiêu', "sea")
    else:
        t_tier = ""

    return f"""
    <style>
      .pb {{ position:relative; overflow:hidden; margin-bottom:18px; padding:24px 26px; border-radius:20px; color:#fff;
             background:radial-gradient(circle at 12% 0%, #0e7490 0, transparent 46%), linear-gradient(120deg,#06283d 0%,#0b4a6f 55%,#0e7490 100%);
             box-shadow:0 14px 34px rgba(8,47,73,.22); }}
      .pb:before {{ content:""; position:absolute; right:-70px; top:-90px; width:300px; height:300px; border-radius:50%;
                    border:34px solid rgba(253,224,71,.13); }}
      .pb-head {{ position:relative; display:flex; justify-content:space-between; align-items:flex-end; gap:16px; flex-wrap:wrap; margin-bottom:16px; }}
      .pb-eyebrow {{ font-size:12px; font-weight:800; letter-spacing:.14em; text-transform:uppercase; color:#fde047; }}
      .pb-title {{ font-size:26px; line-height:1.2; font-weight:900; margin-top:4px; text-wrap:balance; }}
      .pb-date {{ font-size:13px; color:#bae6fd; margin-top:4px; }}
      .pb-actions {{ display:flex; gap:8px; flex-wrap:wrap; }}
      .pb-actions a {{ padding:9px 14px; border-radius:999px; font-size:13px; font-weight:800; text-decoration:none; background:#fde047; color:#422006; }}
      .pb-actions a.alt {{ background:rgba(255,255,255,.12); color:#fff; border:1px solid rgba(255,255,255,.35); }}
      .pb-tickets {{ position:relative; display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }}
      .pb-ticket {{ padding:16px 18px; border-radius:16px; background:#fff; color:#0f172a; border-left:8px solid #f59e0b; min-width:0; }}
      .pb-ticket.red {{ border-left-color:#e11d1d; }} .pb-ticket.sea {{ border-left-color:#0891b2; }}
      .pb-ticket.ghost {{ background:rgba(255,255,255,.1); color:#fff; border-left-color:rgba(255,255,255,.4); }}
      .pb-kicker {{ font-size:11px; font-weight:800; letter-spacing:.1em; text-transform:uppercase; color:#b45309; }}
      .pb-ticket.red .pb-kicker {{ color:#b91c1c; }} .pb-ticket.sea .pb-kicker {{ color:#0e7490; }} .pb-ticket.ghost .pb-kicker {{ color:#fde047; }}
      .pb-big {{ display:flex; align-items:center; gap:10px; font-size:44px; line-height:1.05; font-weight:900; margin:4px 0 2px; font-variant-numeric:tabular-nums; }}
      .pb-line {{ font-size:14px; font-weight:700; }}
      .pb-sub {{ font-size:12px; margin-top:6px; opacity:.78; overflow-wrap:anywhere; }}
      @media (max-width:1000px) {{ .pb-tickets {{ grid-template-columns:1fr; }} .pb-title {{ font-size:22px; }} }}
    </style>
    <div class="pb">
      <div class="pb-head">
        <div><div class="pb-eyebrow">Ocean Restaurant · Tri ân khách hàng</div>
          <div class="pb-title">{headline}</div>
          <div class="pb-date">Các ưu đãi đang áp dụng hôm nay, {date.today().strftime("%d/%m/%Y")}</div></div>
        <div class="pb-actions"><a href="#add-promo">+ Thêm chương trình</a><a class="alt" href="#order-report">Xem báo cáo</a></div>
      </div>
      <div class="pb-tickets">{t_order}{t_item}{t_tier}</div>
    </div>"""


def expiry_promotions_panel_html(programs):
    rules = " · ".join(f"còn ≤ {d} ngày: giảm {p}%" for d, p in EXPIRY_DISCOUNT_RULES)
    if not programs:
        rows = ('<tr><td colspan="6" class="empty">Hiện không có nguyên liệu nào sắp hết hạn '
                '(trong vòng 7 ngày) nên chưa có chương trình xả hàng.</td></tr>')
    else:
        rows = ""
        for p in programs:
            names = ", ".join(escape(i["name"]) for i in p["items"][:3])
            more = f" và {len(p['items']) - 3} món khác" if len(p["items"]) > 3 else ""
            href = f'/promotions/expiry/{escape(p["ingredient_id"])}'
            when = (hot_badge_html(days_text(p["days_left"])) if is_hot_discount(p["discount"])
                    else f'<span class="status {css_status(expiry_info(p["expiry_date"])[0])}">{days_text(p["days_left"])}</span>')
            rows += f"""
            <tr style="cursor:pointer;" onclick="location.href='{href}'">
                <td><strong>🍃 Xả hàng: {escape(p["ingredient_name"])}</strong><br><span class="muted">Tự động theo hạn sử dụng</span></td>
                <td>{p["quantity"]:g} {escape(p["unit"])}</td>
                <td>{parse_iso_date(p["expiry_date"]).strftime("%d/%m/%Y")}<br>{when}</td>
                <td><span class="status green" style="font-size:13px;">-{p["discount"]}%</span></td>
                <td>{len(p["items"])} món<br><span class="muted">{names}{more}</span></td>
                <td><a class="btn btn-primary" href="{href}">Chi tiết</a></td>
            </tr>"""
    return f"""
    <div class="panel" style="border-color:#bef264;background:linear-gradient(120deg,#fff,#f7fee7);">
        <h2>🍃 Khuyến mại xả hàng theo hạn sử dụng (tự động)</h2>
        <p class="muted" style="margin-bottom:12px;">Nguyên liệu sắp hết hạn (≤ {EXPIRY_SOON_DAYS} ngày) làm các món dùng nó được giảm giá tự động.
           Quy tắc: {rules}. Bấm vào chương trình để xem lý do và các món được giảm.</p>
        <div style="overflow-x:auto;"><table>
            <thead><tr><th>Chương trình</th><th>Số lượng sắp hết hạn</th><th>Hạn sử dụng</th><th>Mức giảm</th><th>Món áp dụng</th><th></th></tr></thead>
            <tbody>{rows}</tbody>
        </table></div>
    </div>"""


def get_inventory_alerts(conn):
    """Cảnh báo kho: hết hàng / sắp hết (tồn) và số lượng hết hạn / sắp hết hạn (theo lô), nặng nhất xếp trước."""
    alerts = []
    summaries = get_batch_summary(conn)
    for row in conn.execute("SELECT * FROM ingredients").fetchall():
        status = inventory_status(row["quantity"], row["min_quantity"])
        if status != "Còn hàng":
            item = dict(row)
            item["status"], item["kind"] = status, "stock"
            alerts.append(item)
        s = summaries.get(row["ingredient_id"])
        if s and s["expired_qty"] > 0:
            item = dict(row)
            item.update(status="Hết hạn", kind="expiry", days_left=s["expired_days"], exp_qty=s["expired_qty"],
                        exp_date=next(b["expiry"] for b in s["batches"] if b["status"] == "Hết hạn"))
            alerts.append(item)
        if s and s["soon_qty"] > 0:
            item = dict(row)
            item.update(status="Sắp hết hạn", kind="expiry", days_left=s["soon_days"], exp_qty=s["soon_qty"],
                        exp_date=s["soon_date"])
            alerts.append(item)
    alerts.sort(key=lambda x: (STATUS_ORDER[x["status"]], x["name"]))
    return alerts


def to_float(value, default=0.0):
    try:
        return float(str(value).replace(",", ".").strip())
    except (ValueError, TypeError):
        return default


def get_items_affected_by(conn, ingredient_ids):
    """{ingredient_id: [tên các món đang bán dùng nguyên liệu đó]}"""
    ids = list(ingredient_ids)
    if not ids:
        return {}
    marks = ",".join(["?"] * len(ids))
    rows = conn.execute(f"""
        SELECT r.ingredient_id, it.name
        FROM recipes r JOIN items it ON it.item_id = r.item_id
        WHERE r.ingredient_id IN ({marks}) AND it.status = 'Đang bán'
        ORDER BY it.name
    """, ids).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["ingredient_id"], []).append(r["name"])
    return result


def get_unavailable_item_ids(conn):
    """{item_id: 'stock' | 'expired'} - món không thể làm vì thiếu nguyên liệu, hoặc phần còn hạn không đủ."""
    result = {}
    summaries = get_batch_summary(conn)
    rows = conn.execute("""
        SELECT r.item_id, r.ingredient_id, i.quantity, r.quantity AS need
        FROM recipes r JOIN ingredients i ON i.ingredient_id = r.ingredient_id
    """).fetchall()
    for r in rows:
        total, need = float(r["quantity"]), float(r["need"])
        s = summaries.get(r["ingredient_id"])
        usable = s["usable"] if s else total
        if total + 1e-9 < need:
            result.setdefault(r["item_id"], "stock")
        elif usable + 1e-9 < need:
            result[r["item_id"]] = "expired"
    return result


def unavailable_selection_error(conn, selected):
    """Kiểm tra lại món hết nguyên liệu ngay trước khi lưu đơn/gọi thêm."""
    blocked = get_unavailable_item_ids(conn)
    rejected = [item_id for item_id, qty, price in selected if qty > 0 and item_id in blocked]
    if not rejected:
        return ""
    placeholders = ",".join("?" for _ in rejected)
    names = {r["item_id"]: r["name"] for r in conn.execute(
        f"SELECT item_id, name FROM items WHERE item_id IN ({placeholders})", rejected
    ).fetchall()}
    details = [f'{names.get(item_id, item_id)} (hết nguyên liệu)'
               for item_id in rejected]
    return "Không thể đặt món: " + "; ".join(details) + ". Vui lòng chọn món khác."


def inventory_alert_banner(alerts):
    if not alerts:
        return ('<div class="notice" style="background:#dcfce7;color:#166534;border-color:#bbf7d0;">'
                'Tất cả nguyên liệu đang ở mức an toàn.</div>')

    def names(rows, with_expiry=False):
        parts = []
        for r in rows[:8]:
            label = escape(r["name"])
            if with_expiry:
                label += f' ({r["exp_qty"]:g} {escape(r["unit"])}, {days_text(r["days_left"])})'
            parts.append(label)
        return ", ".join(parts) + (f" và {len(rows) - 8} nguyên liệu khác" if len(rows) > 8 else "")

    out = [a for a in alerts if a["status"] == "Hết hàng"]
    low = [a for a in alerts if a["status"] == "Sắp hết"]
    expired = [a for a in alerts if a["status"] == "Hết hạn"]
    soon = [a for a in alerts if a["status"] == "Sắp hết hạn"]
    html = ""
    if out:
        html += f'<div class="message"><strong>{len(out)} nguyên liệu đã hết:</strong> {names(out)}.</div>'
    if expired:
        html += f'<div class="message"><strong>{len(expired)} nguyên liệu có lô đã hết hạn:</strong> {names(expired, True)}.</div>'
    if low:
        html += (f'<div class="notice" style="background:#fef3c7;color:#92400e;border-color:#fde68a;">'
                 f'<strong>{len(low)} nguyên liệu sắp hết:</strong> {names(low)}.</div>')
    if soon:
        html += (f'<div class="notice" style="background:#ecfccb;color:#3f6212;border-color:#bef264;">'
                 f'<strong>{len(soon)} nguyên liệu có lô sắp hết hạn:</strong> {names(soon, True)}.</div>')
    return html


def material_tabs(active, alert_count=0):
    tabs = [
        ("materials", "/materials", "Danh mục nguyên liệu"),
        ("inventory", "/inventory", "Quản lý tồn kho nguyên liệu"),
        ("check", "/inventory/check", "Kiểm tra & cảnh báo"),
        ("expiry", "/inventory/expiry", "Dashboard hạn sử dụng"),
    ]
    html = '<div class="actions" style="margin-bottom:18px;gap:8px;">'
    for key, href, label in tabs:
        cls = "btn btn-primary" if key == active else "btn btn-secondary"
        badge = f' <span class="status red">{alert_count}</span>' if key == "check" and alert_count else ""
        html += f'<a class="{cls}" href="{href}">{label}{badge}</a>'
    return html + "</div>"


# ---------- Chuông thông báo (cạnh tài khoản) ----------

def build_notifications(alerts):
    """Chuyển các cảnh báo kho thành danh sách thông báo cho chuông ở góc trên."""
    items = []
    for a in alerts:
        iid, name, unit = a["ingredient_id"], a["name"], a["unit"]
        if a["status"] == "Hết hàng":
            items.append({"key": f"out:{iid}", "level": "red", "href": "/inventory/check",
                          "title": f"{name} đã hết hàng", "sub": "Cần nhập thêm để tiếp tục chế biến"})
        elif a["status"] == "Sắp hết":
            items.append({"key": f"low:{iid}", "level": "amber", "href": "/inventory/check",
                          "title": f"{name} sắp hết",
                          "sub": f'Còn {a["quantity"]:g} {unit}, mức tối thiểu {a["min_quantity"]:g} {unit}'})
        elif a["status"] == "Hết hạn":
            items.append({"key": f"expired:{iid}:{a['exp_date']}", "level": "red",
                          "href": f"/materials/{iid}/batches",
                          "title": f'{name}: {a["exp_qty"]:g} {unit} đã hết hạn',
                          "sub": f'{days_text(a["days_left"]).capitalize()}, cần loại bỏ'})
        else:
            items.append({"key": f"soon:{iid}:{a['exp_date']}", "level": "green",
                          "href": f"/materials/{iid}/batches",
                          "title": f'{name}: {a["exp_qty"]:g} {unit} sắp hết hạn',
                          "sub": f'{days_text(a["days_left"]).capitalize()} (hạn {fmt_date(a["exp_date"])})'})
    return items


NOTIF_STYLE = """<style>
  .nt-wrap { position:relative; }
  .nt-btn { position:relative; width:36px; height:36px; display:flex; align-items:center; justify-content:center; background:#fff; color:#33536b; border:1px solid #e2e8f0; border-radius:8px; cursor:pointer; }
  .nt-btn:hover, .nt-btn[aria-expanded="true"] { color:#0369a1; border-color:#bae6fd; background:#f0f9ff; }
  .nt-badge { position:absolute; top:-6px; right:-6px; min-width:19px; height:19px; padding:0 5px; border-radius:999px; background:#e11d48; color:#fff; font-size:11px; font-weight:800; display:flex; align-items:center; justify-content:center; border:2px solid #f4f8fc; }
  .nt-panel { position:absolute; right:0; top:52px; width:340px; max-width:calc(100vw - 28px); background:#fff; border:1px solid #e2e8f0; border-radius:12px; box-shadow:0 14px 40px rgba(15,45,75,.18); z-index:900; overflow:hidden; }
  .nt-badge[hidden], .nt-panel[hidden] { display:none; }
  .nt-head { display:flex; justify-content:space-between; align-items:baseline; padding:11px 14px; border-bottom:1px solid #edf2f7; font-weight:800; color:#082f49; }
  .nt-head small { font-weight:500; color:#64748b; font-size:12px; }
  .nt-list { max-height:360px; overflow-y:auto; }
  .nt-item { display:flex; gap:10px; align-items:flex-start; padding:10px 14px; border-bottom:1px solid #f1f5f9; text-decoration:none; color:#334155; }
  .nt-item:hover { background:#f8fcff; }
  .nt-item.unread { background:#f0f9ff; }
  .nt-dot { width:8px; height:8px; border-radius:50%; flex:none; margin-top:6px; }
  .nt-red { background:#e11d48; } .nt-amber { background:#f59e0b; } .nt-green { background:#65a30d; }
  .nt-text { min-width:0; }
  .nt-text b { display:block; font-size:13px; font-weight:700; color:#172b4d; }
  .nt-text small { display:block; font-size:12px; color:#64748b; }
  .nt-empty { padding:26px 14px; text-align:center; color:#94a3b8; font-size:13px; }
  .nt-foot { display:block; padding:10px 14px; text-align:center; font-size:13px; font-weight:700; color:#0369a1; text-decoration:none; background:#f8fafc; }
  .nt-foot:hover { background:#f0f9ff; }
  @media (max-width:520px) { .nt-wrap { position:static; } .nt-panel { left:0; right:0; width:auto; max-width:none; } }
</style>"""

NOTIF_BELL_SVG = ('<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                  '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>')

NOTIF_SCRIPT = """<script>
(function () {
  var wrap = document.getElementById('nt-wrap');
  if (!wrap) return;
  var btn = document.getElementById('nt-btn'), panel = document.getElementById('nt-panel'), badge = document.getElementById('nt-badge');
  var items = Array.prototype.slice.call(panel.querySelectorAll('.nt-item'));
  var storeKey = 'ocean_notif_seen_' + wrap.dataset.user, seen = [];
  try { seen = JSON.parse(localStorage.getItem(storeKey) || '[]'); } catch (e) {}
  var unread = items.filter(function (a) { return seen.indexOf(a.dataset.key) === -1; });
  unread.forEach(function (a) { a.classList.add('unread'); });
  if (unread.length) { badge.textContent = unread.length > 99 ? '99+' : unread.length; badge.hidden = false; }
  function toggle(show) {
    panel.hidden = !show;
    btn.setAttribute('aria-expanded', show ? 'true' : 'false');
    if (show) {
      badge.hidden = true;
      try { localStorage.setItem(storeKey, JSON.stringify(items.map(function (a) { return a.dataset.key; }))); } catch (e) {}
    }
  }
  btn.addEventListener('click', function (e) { e.stopPropagation(); toggle(panel.hidden); });
  document.addEventListener('click', function (e) { if (!wrap.contains(e.target)) toggle(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') toggle(false); });
})();
</script>"""


def notification_bell_html(items, user_id=""):
    rows = ""
    for n in items:
        rows += (f'<a class="nt-item" href="{escape(n["href"])}" data-key="{escape(n["key"])}">'
                 f'<span class="nt-dot nt-{n["level"]}"></span>'
                 f'<span class="nt-text"><b>{escape(n["title"])}</b><small>{escape(n["sub"])}</small></span></a>')
    if not rows:
        rows = '<div class="nt-empty">Không có thông báo nào.</div>'
    count = f"<small>{len(items)} cảnh báo</small>" if items else ""
    return f"""{NOTIF_STYLE}
    <div class="nt-wrap" id="nt-wrap" data-user="{escape(user_id)}">
        <button type="button" class="nt-btn" id="nt-btn" aria-label="Thông báo" aria-expanded="false" title="Thông báo">
            {NOTIF_BELL_SVG}<span class="nt-badge" id="nt-badge" hidden>0</span>
        </button>
        <div class="nt-panel" id="nt-panel" hidden>
            <div class="nt-head"><span>Thông báo</span>{count}</div>
            <div class="nt-list">{rows}</div>
            <a class="nt-foot" href="/inventory/check">Xem tất cả cảnh báo</a>
        </div>
    </div>
    {NOTIF_SCRIPT}"""


def flash_html():
    error = request.args.get("error", "")
    return f'<div class="message">{escape(error)}</div>' if error else ""


def push_inventory_check(title, changes):
    """Ghi nhớ các nguyên liệu vừa nhập/xuất để trang kế tiếp hiện bảng kiểm tra.

    Chỉ lưu dữ liệu gọn (mã + số liệu) vì session lưu trong cookie (giới hạn ~4KB).
    """
    compact = []
    for c in changes[:25]:
        compact.append({
            "id": c["id"], "type": c["type"],
            "amount": round(float(c["amount"]), 4),
            "before": round(float(c["before"]), 4),
            "after": round(float(c["after"]), 4),
        })
    if compact:
        session["inv_check"] = {"title": title, "changes": compact}


def pop_inventory_check_html():
    """Thông báo nhỏ ở góc dưới bên phải sau khi nhập/xuất kho. Chỉ hiện một lần."""
    data = session.pop("inv_check", None)
    if not data or not data.get("changes"):
        return ""
    changes = data["changes"]
    conn = get_db()
    try:
        ids = [c["id"] for c in changes]
        marks = ",".join(["?"] * len(ids))
        info = {r["ingredient_id"]: r for r in conn.execute(
            f"SELECT * FROM ingredients WHERE ingredient_id IN ({marks})", ids).fetchall()}
        out_ids = [i for i in ids if i in info and
                   inventory_status(info[i]["quantity"], info[i]["min_quantity"]) == "Hết hàng"]
        affected = get_items_affected_by(conn, out_ids)
        summaries = get_batch_summary(conn)
    finally:
        conn.close()

    warn_rows, ok_rows = [], []
    worst = "#16a34a"
    for c in changes:
        ing = info.get(c["id"])
        if not ing:
            continue
        status = inventory_status(ing["quantity"], ing["min_quantity"])
        unit = escape(ing["unit"])
        warns = []
        if status == "Hết hàng":
            dishes = affected.get(c["id"], [])
            more = f" và {len(dishes) - 3} món khác" if len(dishes) > 3 else ""
            text = "Đã hết, cần nhập thêm."
            if dishes:
                text += f' Ảnh hưởng: {escape(", ".join(dishes[:3]))}{more}.'
            warns.append(("#be123c", text))
        elif status == "Sắp hết":
            warns.append(("#92400e", f'Dưới mức tối thiểu {ing["min_quantity"]:g} {unit}.'))
        s = summaries.get(c["id"])
        if s and s["expired_qty"] > 0:
            warns.append(("#be123c", f'{s["expired_qty"]:g} {unit} đã hết hạn.'))
        if s and s["soon_qty"] > 0:
            warns.append(("#3f6212", f'{s["soon_qty"]:g} {unit} sắp hết hạn ({days_text(s["soon_days"])}).'))
        if any(color == "#be123c" for color, _ in warns):
            worst = "#e11d48"
        elif warns and worst != "#e11d48":
            worst = "#f59e0b"
        sign = "+" if c["type"] == "Nhập" else "-"
        warn_html = "".join(f'<div style="color:{color};font-size:12px;font-weight:600;">{text}</div>'
                            for color, text in warns)
        row = f"""
        <div style="padding:8px 0;border-top:1px solid #edf2f7;">
          <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;">
            <strong style="font-size:13px;">{escape(ing["name"])}</strong>
            <span class="status {css_status(status)}">{status}</span>
          </div>
          <div class="muted" style="font-size:12px;">{escape(c["type"])} {sign}{c["amount"]:g} {unit} · tồn {c["before"]:g} → <strong>{c["after"]:g}</strong> {unit}</div>
          {warn_html}
        </div>"""
        (warn_rows if warns else ok_rows).append(row)

    # Nhiều nguyên liệu (xác nhận đơn): chỉ liệt kê dòng có cảnh báo, phần còn lại gộp thành một dòng.
    rows = warn_rows + ok_rows
    extra = ""
    if len(rows) > 4:
        rows = warn_rows[:6]
        hidden = len(warn_rows) + len(ok_rows) - len(rows)
        if hidden:
            extra = (f'<div class="muted" style="padding:8px 0;border-top:1px solid #edf2f7;font-size:12px;">'
                     f'{hidden} nguyên liệu khác {"ở mức an toàn" if not warn_rows[6:] else "xem ở bảng kiểm tra"}.</div>')
    auto_close = "true" if not warn_rows else "false"

    return f"""
    <div id="inv-check-card" role="status" style="position:fixed;right:20px;bottom:20px;z-index:1000;width:400px;max-width:calc(100vw - 24px);max-height:60vh;overflow:auto;background:#fff;border:1px solid #e2e8f0;border-left:4px solid {worst};border-radius:12px;padding:12px 14px;box-shadow:0 12px 34px rgba(15,45,75,.2);">
      <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;margin-bottom:6px;">
        <strong style="font-size:13px;color:#082f49;">{escape(data.get("title", "Kiểm tra tồn kho"))}</strong>
        <button type="button" aria-label="Đóng" onclick="document.getElementById('inv-check-card').remove()" style="border:0;background:none;cursor:pointer;font-size:18px;line-height:1;color:#64748b;padding:0 2px;">&times;</button>
      </div>
      {"".join(rows)}{extra}
      <div style="text-align:right;padding-top:8px;border-top:1px solid #edf2f7;">
        <a href="/inventory/check" style="font-size:12px;font-weight:700;color:#0369a1;text-decoration:none;">Xem kiểm tra tồn kho</a>
      </div>
    </div>
    <script>
      (function () {{
        function closeCard() {{ var m = document.getElementById('inv-check-card'); if (m) m.remove(); }}
        document.addEventListener('keydown', function (e) {{ if (e.key === 'Escape') closeCard(); }});
        if ({auto_close}) setTimeout(closeCard, 8000);
      }})();
    </script>
    """


# ---------- Danh mục nguyên liệu ----------

def parse_expiry_input(raw):
    """-> (giá trị lưu DB hoặc None, lỗi hoặc None)"""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if not parse_iso_date(raw):
        return None, "Hạn sử dụng không hợp lệ."
    return raw[:10], None


@app.route("/materials")
@login_required
def materials():
    conn = get_db()
    q = request.args.get("q", "").strip()
    ingredients = conn.execute(
        "SELECT * FROM ingredients WHERE (name LIKE ? OR ingredient_id LIKE ?) ORDER BY ingredient_id",
        (f"%{q}%", f"%{q}%")).fetchall()
    usage = {r["ingredient_id"]: r["c"] for r in conn.execute(
        "SELECT ingredient_id, COUNT(*) AS c FROM recipes GROUP BY ingredient_id").fetchall()}
    summaries = get_batch_summary(conn)
    alert_count = len(get_inventory_alerts(conn))
    conn.close()

    user = get_current_user()
    can_edit = has_perm(user, "inventory_edit")

    rows = ""
    for ing in ingredients:
        iid = escape(ing["ingredient_id"])
        status = inventory_status(ing["quantity"], ing["min_quantity"])
        used = int(usage.get(ing["ingredient_id"], 0))
        expiry_cell = batch_cell_html(ing["ingredient_id"], ing["unit"], summaries.get(ing["ingredient_id"]))
        if can_edit:
            rows += f"""
            <tr>
                <td>{iid}</td>
                <td><input form="mf-{iid}" name="name" value="{escape(ing["name"])}" required></td>
                <td><input form="mf-{iid}" name="unit" value="{escape(ing["unit"])}" required style="width:80px;"></td>
                <td><input form="mf-{iid}" type="number" name="min_quantity" value="{ing["min_quantity"]:g}" min="0" step="any" required style="width:100px;"></td>
                <td>{ing["quantity"]:g} {escape(ing["unit"])}<br><span class="status {css_status(status)}">{status}</span></td>
                <td>{expiry_cell}</td>
                <td>{used} món</td>
                <td><div class="actions">
                    <form id="mf-{iid}" method="POST" action="/materials/update/{iid}"><button class="btn btn-primary">Lưu</button></form>
                    <form method="POST" action="/materials/delete/{iid}" onsubmit="return confirm('Xóa nguyên liệu này?');"><button class="btn btn-danger">Xóa</button></form>
                </div></td>
            </tr>"""
        else:
            rows += f"""
            <tr>
                <td>{iid}</td><td>{escape(ing["name"])}</td><td>{escape(ing["unit"])}</td>
                <td>{ing["min_quantity"]:g}</td>
                <td>{ing["quantity"]:g} {escape(ing["unit"])}<br><span class="status {css_status(status)}">{status}</span></td>
                <td>{expiry_cell}</td>
                <td>{used} món</td><td><span class="muted">Chỉ xem</span></td>
            </tr>"""
    if not rows:
        rows = '<tr><td colspan="8" class="empty">Chưa có nguyên liệu nào.</td></tr>'

    add_form = ""
    if can_edit:
        add_form = """
        <div class="panel">
            <h2>Thêm nguyên liệu mới</h2>
            <form method="POST" action="/materials/add">
                <div class="form-grid" style="grid-template-columns:repeat(5,minmax(110px,1fr)) auto;">
                    <div class="form-group"><label>Tên nguyên liệu</label><input name="name" required></div>
                    <div class="form-group"><label>Đơn vị</label><input name="unit" placeholder="kg, lít, quả..." required></div>
                    <div class="form-group"><label>Số lượng ban đầu</label><input type="number" name="quantity" min="0" step="any" value="0" required></div>
                    <div class="form-group"><label>Mức tối thiểu</label><input type="number" name="min_quantity" min="0" step="any" value="0" required></div>
                    <div class="form-group"><label>Hạn sử dụng</label><input type="date" name="expiry_date"></div>
                    <button class="btn btn-primary">+ Thêm</button>
                </div>
            </form>
        </div>"""

    body = f"""
    <div class="header">
        <div><h1>Quản lý nguyên liệu</h1></div>
    </div>
    {material_tabs("materials", alert_count)}
    {flash_html()}
    {add_form}
    <div class="panel">
        <h2>Danh mục nguyên liệu</h2>
        <form method="GET" action="/materials" class="actions" style="margin-bottom:12px;">
            <input name="q" value="{escape(q)}" placeholder="Tìm mã hoặc tên nguyên liệu" style="max-width:320px;">
            <button class="btn btn-primary">Tìm</button><a class="btn btn-secondary" href="/materials">Đặt lại</a>
        </form>
        <table>
            <thead><tr><th>Mã</th><th>Nguyên liệu</th><th>Đơn vị</th><th>Mức tối thiểu</th><th>Tồn hiện tại</th><th>Hạn sử dụng theo lô</th><th>Dùng trong</th><th>Thao tác</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    """
    return render_page("Quản lý nguyên liệu", body, "materials")


@app.route("/materials/add", methods=["POST"])
@app.route("/inventory/add", methods=["POST"])
@role_required("admin", "chef")
def add_ingredient():
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "").strip()
    quantity = to_float(request.form.get("quantity", 0))
    min_quantity = to_float(request.form.get("min_quantity", 0))
    expiry, exp_error = parse_expiry_input(request.form.get("expiry_date"))
    if exp_error:
        return redirect(url_for("materials", error=exp_error))
    if not name or not unit or quantity < 0 or min_quantity < 0:
        return redirect(url_for("materials", error="Thông tin nguyên liệu không hợp lệ."))

    conn = get_db()
    try:
        new_id = next_code(conn, "ingredients", "NL")
        conn.execute("""
            INSERT INTO ingredients(ingredient_id, name, unit, quantity, min_quantity, expiry_date) VALUES(?, ?, ?, ?, ?, ?)
        """, (new_id, name, unit, quantity, min_quantity, expiry if quantity > 0 else None))
        if quantity > 0:
            batch_add(conn, new_id, quantity, expiry)
            conn.execute("""INSERT INTO inventory_movements
                (ingredient_id, movement_type, quantity, before_quantity, after_quantity, note, created_at)
                VALUES (?, 'Nhập', ?, 0, ?, ?, ?)""",
                (new_id, quantity, quantity, "Khởi tạo nguyên liệu" + (f" – HSD {fmt_date(expiry)}" if expiry else ""),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except IntegrityError:
        conn.rollback()
        conn.close()
        return redirect(url_for("materials", error="Tên nguyên liệu đã tồn tại."))
    conn.close()
    return redirect(url_for("materials", ok=f"Đã thêm nguyên liệu {name} thành công."))


@app.route("/materials/update/<string:id>", methods=["POST"])
@role_required("admin", "chef")
def update_material(id):
    # Hạn sử dụng được quản lý theo từng lô ở trang chi tiết, không sửa ở đây.
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "").strip()
    min_quantity = to_float(request.form.get("min_quantity", 0))
    if not name or not unit or min_quantity < 0:
        return redirect(url_for("materials", error="Thông tin nguyên liệu không hợp lệ."))
    conn = get_db()
    try:
        conn.execute("UPDATE ingredients SET name = ?, unit = ?, min_quantity = ? WHERE ingredient_id = ?",
                     (name, unit, min_quantity, id))
        conn.commit()
    except IntegrityError:
        conn.rollback()
        conn.close()
        return redirect(url_for("materials", error="Tên nguyên liệu đã tồn tại."))
    conn.close()
    return redirect(url_for("materials", ok=f"Đã cập nhật nguyên liệu {name} thành công."))


@app.route("/materials/delete/<string:id>", methods=["POST"])
@role_required("admin", "chef")
def delete_material(id):
    conn = get_db()
    ing = conn.execute("SELECT name FROM ingredients WHERE ingredient_id = ?", (id,)).fetchone()
    if not ing:
        conn.close()
        return redirect(url_for("materials"))
    used = conn.execute("SELECT COUNT(*) FROM recipes WHERE ingredient_id = ?", (id,)).fetchone()[0]
    if used:
        conn.close()
        return redirect(url_for("materials", error=f"Không thể xóa {ing['name']}: đang được dùng trong {used} công thức món ăn."))
    try:
        conn.execute("DELETE FROM inventory_movements WHERE ingredient_id = ?", (id,))
        conn.execute("DELETE FROM ingredient_batches WHERE ingredient_id = ?", (id,))
        conn.execute("DELETE FROM ingredients WHERE ingredient_id = ?", (id,))
        conn.commit()
    except IntegrityError:
        conn.rollback()
        conn.close()
        return redirect(url_for("materials", error="Không thể xóa nguyên liệu này."))
    conn.close()
    return redirect(url_for("materials", ok=f"Đã xóa nguyên liệu {ing['name']}."))


# ---------- Chi tiết lô theo hạn sử dụng ----------

@app.route("/materials/<string:id>/batches")
@login_required
def material_batches(id):
    conn = get_db()
    ing = conn.execute("SELECT * FROM ingredients WHERE ingredient_id = ?", (id,)).fetchone()
    if not ing:
        conn.close()
        return redirect(url_for("materials", error="Không tìm thấy nguyên liệu."))
    summary = get_batch_summary(conn).get(id) or empty_batch_summary()
    movements = conn.execute("""SELECT * FROM inventory_movements WHERE ingredient_id = ?
        ORDER BY movement_id DESC LIMIT 20""", (id,)).fetchall()
    alert_count = len(get_inventory_alerts(conn))
    conn.close()

    can_edit = has_perm(get_current_user(), "inventory_edit")
    iid = escape(id)
    unit = escape(ing["unit"])
    total = summary["total"]

    rows = ""
    for b in summary["batches"]:
        share = (b["quantity"] / total * 100) if total else 0
        if b["expiry"]:
            chip = f'<span class="status {css_status(b["status"])}">{b["status"]} · {days_text(b["days"])}</span>'
        else:
            chip = '<span class="status gray">Chưa khai báo</span>'
        bg = {"Hết hạn": "#fff1f2", "Sắp hết hạn": "#fffbeb"}.get(b["status"], "")
        actions = ""
        if can_edit:
            iso = b["expiry"].isoformat() if b["expiry"] else ""
            qty = fmt_num(b["quantity"])
            actions = f"""
            <td><form method="POST" action="/materials/{iid}/batches/{b["batch_id"]}/expiry" class="actions">
                <input type="date" name="expiry_date" value="{iso}" style="width:150px;padding:6px;">
                <button class="btn btn-secondary">Lưu</button></form></td>
            <td><form method="POST" action="/materials/{iid}/batches/{b["batch_id"]}/remove" class="actions"
                      onsubmit="return confirm('Xuất số lượng này khỏi lô?');">
                <input type="number" name="amount" value="{qty}" min="0.0001" max="{qty}" step="any" style="width:95px;padding:6px;">
                <button class="btn btn-warning">Xuất</button></form></td>"""
        rows += f"""
        <tr style="background:{bg};">
            <td><strong>{fmt_date(b["expiry"])}</strong></td>
            <td><strong>{b["quantity"]:g} {unit}</strong></td>
            <td>{share:.0f}%</td>
            <td>{chip}</td>{actions}
        </tr>"""
    col_count = 6 if can_edit else 4
    if not rows:
        rows = f'<tr><td colspan="{col_count}" class="empty">Nguyên liệu này hiện không còn lô nào trong kho.</td></tr>'
    action_heads = "<th>Đổi hạn sử dụng</th><th>Xuất khỏi lô</th>" if can_edit else ""

    add_form = ""
    if can_edit:
        add_form = f"""
        <div class="panel">
            <h2>Nhập lô mới</h2>
            <form method="POST" action="/inventory/change/{iid}">
                <input type="hidden" name="next" value="batches">
                <div class="form-grid" style="grid-template-columns:repeat(2,minmax(140px,220px)) auto;justify-content:start;">
                    <div class="form-group"><label>Số lượng ({unit})</label><input type="number" name="amount" min="0.01" step="any" required></div>
                    <div class="form-group"><label>Hạn sử dụng</label><input type="date" name="expiry_date"></div>
                    <button class="btn btn-primary" name="action" value="increase">+ Nhập</button>
                </div>
            </form>
        </div>"""

    movement_rows = ""
    for m in movements:
        movement_rows += f"""<tr><td>{escape(m['created_at'])}</td><td>{escape(m['movement_type'])}</td>
            <td>{m['quantity']:g} {unit}</td><td>{m['before_quantity']:g} → {m['after_quantity']:g}</td>
            <td>{escape(m['note'] or '')}</td></tr>"""
    if not movement_rows:
        movement_rows = '<tr><td colspan="5" class="empty">Chưa có giao dịch nào.</td></tr>'

    usable_rest = summary["ok_qty"] + summary["undeclared_qty"]
    body = f"""
    <div class="header">
        <div><h1>Chi tiết hạn sử dụng: {escape(ing["name"])}</h1><p>{iid}</p></div>
        <a class="btn btn-secondary" href="/inventory">← Quay lại tồn kho</a>
    </div>
    {material_tabs("inventory", alert_count)}
    {flash_html()}
    <div class="cards">
        <div class="card"><div class="card-title">Tổng tồn</div><div class="card-value">{ing["quantity"]:g} <span style="font-size:15px;">{unit}</span></div></div>
        <div class="card"><div class="card-title">Đã hết hạn</div><div class="card-value" style="color:#be123c;">{summary["expired_qty"]:g} <span style="font-size:15px;">{unit}</span></div></div>
        <div class="card"><div class="card-title">Sắp hết hạn (≤ {EXPIRY_SOON_DAYS} ngày)</div><div class="card-value" style="color:#92400e;">{summary["soon_qty"]:g} <span style="font-size:15px;">{unit}</span></div></div>
        <div class="card"><div class="card-title">Còn hạn</div><div class="card-value" style="color:#166534;">{usable_rest:g} <span style="font-size:15px;">{unit}</span></div></div>
    </div>
    <div class="panel">
        <h2>Số lượng theo từng ngày hết hạn</h2>
        <div style="overflow-x:auto;"><table>
            <thead><tr><th>Hạn sử dụng</th><th>Số lượng</th><th>Tỷ lệ tồn</th><th>Tình trạng</th>{action_heads}</tr></thead>
            <tbody>{rows}</tbody>
        </table></div>
    </div>
    {add_form}
    <div class="panel">
        <h2>Lịch sử giao dịch gần đây</h2>
        <table><thead><tr><th>Thời gian</th><th>Loại giao dịch</th><th>Số lượng</th><th>Tồn trước → sau</th><th>Ghi chú</th></tr></thead>
        <tbody>{movement_rows}</tbody></table>
    </div>
    """
    return render_page(f"Hạn sử dụng {ing['name']}", body, "materials")


@app.route("/materials/<string:id>/batches/<int:batch_id>/expiry", methods=["POST"])
@role_required("admin", "chef")
def update_batch_expiry(id, batch_id):
    expiry, exp_error = parse_expiry_input(request.form.get("expiry_date"))
    if exp_error:
        return redirect(url_for("material_batches", id=id, error=exp_error))
    conn = get_db()
    batch = conn.execute("SELECT * FROM ingredient_batches WHERE batch_id = ? AND ingredient_id = ?",
                         (batch_id, id)).fetchone()
    ing = conn.execute("SELECT * FROM ingredients WHERE ingredient_id = ?", (id,)).fetchone()
    if not batch or not ing:
        conn.close()
        return redirect(url_for("material_batches", id=id, error="Không tìm thấy lô nguyên liệu."))
    old = str(batch["expiry_date"])[:10] if batch["expiry_date"] else None
    if old == expiry:
        conn.close()
        return redirect(url_for("material_batches", id=id))
    qty = float(batch["quantity"])
    # Xóa rồi nhập lại để lô tự gộp nếu đã có lô khác cùng hạn sử dụng.
    conn.execute("DELETE FROM ingredient_batches WHERE batch_id = ?", (batch_id,))
    batch_add(conn, id, qty, expiry)
    batch_sync(conn, id)
    stock = float(ing["quantity"])
    conn.execute("""INSERT INTO inventory_movements
        (ingredient_id, movement_type, quantity, before_quantity, after_quantity, note, created_at)
        VALUES (?, 'Điều chỉnh', ?, ?, ?, ?, ?)""",
        (id, qty, stock, stock, f"Đổi HSD lô: {fmt_date(old)} → {fmt_date(expiry)}",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    return redirect(url_for("material_batches", id=id, ok="Đã cập nhật hạn sử dụng của lô."))


@app.route("/materials/<string:id>/batches/<int:batch_id>/remove", methods=["POST"])
@role_required("admin", "chef")
def remove_batch_quantity(id, batch_id):
    amount = round(to_float(request.form.get("amount", 0), 0), 4)
    conn = get_db()
    batch = conn.execute("SELECT * FROM ingredient_batches WHERE batch_id = ? AND ingredient_id = ?",
                         (batch_id, id)).fetchone()
    ing = conn.execute("SELECT * FROM ingredients WHERE ingredient_id = ?", (id,)).fetchone()
    if not batch or not ing:
        conn.close()
        return redirect(url_for("material_batches", id=id, error="Không tìm thấy lô nguyên liệu."))
    have = round(float(batch["quantity"]), 4)
    if amount <= 0 or amount > have:
        conn.close()
        return redirect(url_for("material_batches", id=id,
                                error=f'Số lượng xuất phải lớn hơn 0 và không quá {have:g} {ing["unit"]} của lô.'))
    left = round(have - amount, 4)
    if left <= 0:
        conn.execute("DELETE FROM ingredient_batches WHERE batch_id = ?", (batch_id,))
    else:
        conn.execute("UPDATE ingredient_batches SET quantity = ? WHERE batch_id = ?", (left, batch_id))
    before = float(ing["quantity"])
    after = max(0, round(before - amount, 4))
    conn.execute("UPDATE ingredients SET quantity = ? WHERE ingredient_id = ?", (after, id))
    batch_sync(conn, id)
    conn.execute("""INSERT INTO inventory_movements
        (ingredient_id, movement_type, quantity, before_quantity, after_quantity, note, created_at)
        VALUES (?, 'Xuất', ?, ?, ?, ?, ?)""",
        (id, amount, before, after, f"Xuất từ lô HSD {fmt_date(batch['expiry_date'])}",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    push_inventory_check(f"Đã xuất {ing['name']}",
                         [{"id": id, "type": "Xuất", "amount": amount, "before": before, "after": after}])
    return redirect(url_for("material_batches", id=id))


# ---------- Quản lý tồn kho nguyên liệu ----------

@app.route("/inventory")
@login_required
def inventory():
    conn = get_db()
    inv_search = request.args.get("q", "").strip()
    inv_status = request.args.get("status", "")
    inv_sort = request.args.get("sort", "name")
    order_by = {"name": "name", "quantity_asc": "quantity ASC", "quantity_desc": "quantity DESC", "id": "ingredient_id", "minimum": "min_quantity DESC", "expiry": "expiry_date IS NULL, expiry_date ASC"}.get(inv_sort, "name")
    ingredients = conn.execute(f"SELECT * FROM ingredients WHERE (name LIKE ? OR ingredient_id LIKE ?) ORDER BY {order_by}", (f"%{inv_search}%", f"%{inv_search}%")).fetchall()
    summaries = get_batch_summary(conn)
    if inv_status:
        if inv_status in ("Hết hạn", "Sắp hết hạn"):
            field = "expired_qty" if inv_status == "Hết hạn" else "soon_qty"
            ingredients = [x for x in ingredients if (summaries.get(x["ingredient_id"]) or {}).get(field, 0) > 0]
        else:
            ingredients = [x for x in ingredients if inventory_status(x["quantity"], x["min_quantity"]) == inv_status]
    movements = conn.execute("""SELECT m.*, i.name AS ingredient_name, i.unit
        FROM inventory_movements m JOIN ingredients i ON i.ingredient_id = m.ingredient_id
        WHERE i.name LIKE ? OR i.ingredient_id LIKE ?
        ORDER BY m.movement_id DESC LIMIT 50""", (f"%{inv_search}%", f"%{inv_search}%")).fetchall()
    alert_count = len(get_inventory_alerts(conn))
    conn.close()

    movement_rows = ""
    for m in movements:
        movement_rows += f"""<tr><td>{escape(m['created_at'])}</td><td>{escape(m['ingredient_name'])}</td>
            <td>{escape(m['movement_type'])}</td><td>{m['quantity']:g} {escape(m['unit'])}</td>
            <td>{m['before_quantity']:g} → {m['after_quantity']:g}</td><td>{escape(m['note'] or '')}</td></tr>"""
    if not movement_rows:
        movement_rows = '<tr><td colspan="6" class="empty">Chưa có lịch sử nhập, xuất hoặc điều chỉnh.</td></tr>'

    user = get_current_user()
    can_edit = has_perm(user, "inventory_edit")

    rows = ""
    for ingredient in ingredients:
        status = inventory_status(ingredient["quantity"], ingredient["min_quantity"])
        if can_edit:
            action_cell = f"""
            <form method="POST" action="/inventory/change/{ingredient["ingredient_id"]}"
                  style="display:flex; gap:5px; align-items:center; flex-wrap:wrap;">
                <input type="number" name="amount" value="1" min="0.01" step="any" title="Số lượng"
                       style="width:80px; padding:6px;">
                <input type="date" name="expiry_date" title="Hạn sử dụng của lô nhập"
                       style="width:135px; padding:6px;">
                <button class="btn btn-primary" name="action" value="increase">+ Nhập</button>
                <button class="btn btn-warning" name="action" value="decrease">- Xuất</button>
            </form>
            """
        else:
            action_cell = '<span class="muted">Chỉ xem</span>'

        rows += f"""
        <tr>
            <td>{ingredient["ingredient_id"]}</td>
            <td>{escape(ingredient["name"])}</td>
            <td>{escape(ingredient["unit"])}</td>
            <td>{ingredient["quantity"]:g}</td>
            <td>{ingredient["min_quantity"]:g}</td>
            <td><span class="status {css_status(status)}">{status}</span></td>
            <td>{batch_cell_html(ingredient["ingredient_id"], ingredient["unit"], summaries.get(ingredient["ingredient_id"]))}</td>
            <td>{action_cell}</td>
        </tr>
        """
    if not rows:
        rows = '<tr><td colspan="8" class="empty">Chưa có nguyên liệu nào.</td></tr>'

    def opt(value, label, current):
        return f'<option value="{value}" {"selected" if current == value else ""}>{label}</option>'

    stock_html = f"""
    <div class="panel">
        <form method="GET" action="/inventory#stock-table" class="actions" style="margin-bottom:12px;">
            <input name="q" value="{escape(inv_search)}" placeholder="Tìm mã hoặc tên nguyên liệu">
            <select name="status"><option value="">Tất cả trạng thái</option>{opt("Còn hàng", "Còn hàng", inv_status)}{opt("Sắp hết", "Sắp hết", inv_status)}{opt("Hết hàng", "Hết hàng", inv_status)}{opt("Sắp hết hạn", "Sắp hết hạn", inv_status)}{opt("Hết hạn", "Hết hạn", inv_status)}</select>
            <select name="sort">{opt("name", "Tên A–Z", inv_sort)}{opt("quantity_asc", "Tồn tăng dần", inv_sort)}{opt("quantity_desc", "Tồn giảm dần", inv_sort)}{opt("minimum", "Mức tối thiểu", inv_sort)}{opt("expiry", "Hạn sử dụng gần nhất", inv_sort)}{opt("id", "Mã ID", inv_sort)}</select>
            <button class="btn btn-primary">Lọc</button><a class="btn btn-secondary" href="/inventory#stock-table">Đặt lại</a>
            <a class="btn btn-warning" href="/inventory/check#by-dish" style="margin-left:auto;">Xem cảnh báo tồn kho{f' ({alert_count})' if alert_count else ''}</a>
        </form>
        <div style="overflow-x:auto;"><table>
            <thead>
                <tr><th>Mã</th><th>Nguyên liệu</th><th>Đơn vị</th><th>Số lượng</th><th>Mức tối thiểu</th><th>Trạng thái</th><th>Hạn sử dụng theo lô</th><th>Số lượng · HSD lô nhập</th></tr>
            </thead>
            <tbody>{rows}</tbody>
        </table></div>
    </div>"""
    history_html = f"""
    <div class="panel">
        <h2>50 giao dịch gần nhất</h2>
        <table><thead><tr><th>Thời gian</th><th>Nguyên liệu</th><th>Loại giao dịch</th><th>Số lượng</th><th>Tồn trước → sau</th><th>Ghi chú</th></tr></thead>
        <tbody>{movement_rows}</tbody></table>
    </div>"""
    inv_tabs = tabs_html("inventory", [
        ("stock-table", "Tồn kho nguyên liệu", stock_html, len(ingredients)),
        ("stock-history", "Lịch sử nhập / xuất", history_html, len(movements)),
    ])
    body = f"""
    <div class="header">
        <div><h1>Quản lý tồn kho nguyên liệu</h1></div>
    </div>
    {material_tabs("inventory", alert_count)}
    {flash_html()}
    {inv_tabs}
    """
    return render_page("Quản lý tồn kho nguyên liệu", body, "materials")


@app.route("/inventory/check")
@login_required
def inventory_check():
    conn = get_db()
    summaries = get_batch_summary(conn)
    ingredients = []
    for r in conn.execute("SELECT * FROM ingredients").fetchall():
        item = dict(r)
        item["status"] = inventory_status(r["quantity"], r["min_quantity"])
        item["batch"] = summaries.get(r["ingredient_id"]) or empty_batch_summary()
        exp_level = 0 if item["batch"]["expired_qty"] > 0 else (1 if item["batch"]["soon_qty"] > 0 else 2)
        item["severity"] = min(STATUS_ORDER[item["status"]], exp_level)
        ingredients.append(item)
    ingredients.sort(key=lambda x: (x["severity"], STATUS_ORDER[x["status"]], x["name"]))
    alerts = get_inventory_alerts(conn)
    affected = get_items_affected_by(conn, [x["ingredient_id"] for x in ingredients if x["status"] == "Hết hàng"])
    exp_discounts = {p["ingredient_id"]: p["discount"] for p in get_expiry_programs(conn)}
    stock_dash = stock_alert_dashboard_html(conn)
    conn.close()

    can_edit = has_perm(get_current_user(), "inventory_edit")
    n_out = sum(1 for x in ingredients if x["status"] == "Hết hàng")
    n_low = sum(1 for x in ingredients if x["status"] == "Sắp hết")
    n_exp = sum(1 for x in ingredients if x["batch"]["expired_qty"] > 0 or x["batch"]["soon_qty"] > 0)
    n_ok = sum(1 for x in ingredients if x["severity"] == 2)

    rows = ""
    for ing in ingredients:
        unit = escape(ing["unit"])
        batch = ing["batch"]
        diff = float(ing["quantity"]) - float(ing["min_quantity"])
        diff_html = (f'<span style="color:#be123c;font-weight:700;">{diff:g}</span>' if diff < 0
                     else f'<span style="color:#166534;">+{diff:g}</span>')
        notes = []
        bg = ""
        if ing["status"] == "Hết hàng":
            dishes = affected.get(ing["ingredient_id"], [])
            more = f" và {len(dishes) - 6} món khác" if len(dishes) > 6 else ""
            notes.append('<strong style="color:#be123c;">Đã hết, cần nhập ngay</strong>'
                         + (f'<br><span class="muted">Ảnh hưởng: {escape(", ".join(dishes[:6]))}{more}</span>' if dishes else ""))
            bg = "#fff1f2"
        elif ing["status"] == "Sắp hết":
            need = float(ing["min_quantity"]) - float(ing["quantity"])
            notes.append(f'<strong style="color:#92400e;">Thiếu {need:g} {unit} so với mức tối thiểu</strong>')
            bg = "#fffbeb"
        if batch["expired_qty"] > 0:
            notes.append(f'<strong style="color:#be123c;">{batch["expired_qty"]:g} {unit} đã hết hạn, cần loại bỏ</strong>')
            bg = "#fff1f2"
        if batch["soon_qty"] > 0:
            pct = exp_discounts.get(ing["ingredient_id"], 0)
            notes.append(f'<strong style="color:#3f6212;">{batch["soon_qty"]:g} {unit} sắp hết hạn ({days_text(batch["soon_days"])})'
                         + (f', món liên quan đang giảm {pct}%' if pct else "") + '</strong>')
            bg = bg or "#f7fee7"
        if not notes:
            notes.append('<span style="color:#166534;">Đạt mức an toàn</span>')
        quick = ""
        if can_edit and (ing["status"] != "Còn hàng"):
            quick = f"""<form method="POST" action="/inventory/change/{escape(ing["ingredient_id"])}" style="display:flex;gap:5px;align-items:center;">
                <input type="hidden" name="next" value="check">
                <input type="number" name="amount" value="1" min="0.01" step="any" style="width:80px;padding:6px;">
                <button class="btn btn-primary" name="action" value="increase">+ Nhập</button></form>"""
        rows += f"""
        <tr style="background:{bg};">
            <td>{escape(ing["ingredient_id"])}</td><td><strong>{escape(ing["name"])}</strong></td>
            <td>{ing["quantity"]:g} {unit}</td><td>{ing["min_quantity"]:g} {unit}</td><td>{diff_html}</td>
            <td><span class="status {css_status(ing["status"])}">{ing["status"]}</span></td>
            <td>{batch_cell_html(ing["ingredient_id"], ing["unit"], batch)}</td>
            <td>{"<br>".join(notes)}</td><td>{quick}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="9" class="empty">Chưa có nguyên liệu nào.</td></tr>'

    table_html = f"""
    {inventory_alert_banner(alerts)}
    <div class="panel">
        <div style="overflow-x:auto;"><table>
            <thead><tr><th>Mã</th><th>Nguyên liệu</th><th>Tồn hiện tại</th><th>Mức tối thiểu</th><th>Chênh lệch</th><th>Trạng thái</th><th>Hạn sử dụng theo lô</th><th style="min-width:230px;">Cảnh báo</th><th>Nhập nhanh</th></tr></thead>
            <tbody>{rows}</tbody>
        </table></div>
    </div>"""
    check_tabs = tabs_html("check", [
        ("by-dish", "Cảnh báo theo mặt hàng", stock_dash, n_low + n_out),
        ("check-table", "Bảng kiểm tra từng nguyên liệu", table_html, len(ingredients)),
    ])
    body = f"""
    <div class="header">
        <div><h1>Kiểm tra & cảnh báo tồn kho</h1></div>
    </div>
    {material_tabs("check", len(alerts))}
    {flash_html()}
    <div class="cards">
        <div class="card"><div class="card-title">Đạt mức an toàn</div><div class="card-value" style="color:#166534;">{n_ok}</div></div>
        <div class="card"><div class="card-title">Sắp hết</div><div class="card-value" style="color:#92400e;">{n_low}</div></div>
        <div class="card"><div class="card-title">Đã hết hàng</div><div class="card-value" style="color:#be123c;">{n_out}</div></div>
        <div class="card"><div class="card-title">Sắp / đã hết hạn</div><div class="card-value" style="color:#3f6212;">{n_exp}</div><a href="/inventory/expiry" style="font-size:12px;">Xem dashboard hạn sử dụng →</a></div>
    </div>
    {check_tabs}
    """
    return render_page("Kiểm tra tồn kho", body, "materials")


# ---------- Dashboard hạn sử dụng ----------

# (mã mức, nhãn, còn tối đa n ngày, màu cột biểu đồ, lớp màu nhãn)
EXPIRY_LEVELS = [
    ("expired", "Đã hết hạn", -1, "#881337", "red"),
    ("critical", "Hôm nay – ngày mai", 1, "#be123c", "red"),
    ("warning", "Còn 2–3 ngày", 3, "#d97706", "yellow"),
    ("soon", "Còn 4–7 ngày", 7, "#0284c7", "blue"),
    ("later", "Sau 7 ngày", None, "#64748b", "gray"),
]
EXPIRY_LEVEL_META = {code: (label, color, css) for code, label, _, color, css in EXPIRY_LEVELS}
EXPIRY_ADVICE = {
    "expired": "Loại bỏ khỏi kho",
    "critical": "Ưu tiên dùng ngay hôm nay",
    "warning": "Ưu tiên chế biến trước",
    "soon": "Theo dõi, dùng trước lô mới",
    "later": "",
}


def expiry_level(days):
    if days < 0:
        return "expired"
    for code, _, limit, _, _ in EXPIRY_LEVELS[1:]:
        if limit is None or days <= limit:
            return code
    return "later"


def get_daily_usage(conn, days=7):
    """{ingredient_id: lượng dùng trung bình mỗi ngày} tính từ nguyên liệu xuất cho đơn hàng (đã trừ phần hoàn kho)."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("""
        SELECT ingredient_id, movement_type, SUM(quantity) AS total
        FROM inventory_movements
        WHERE created_at >= ?
          AND (movement_type = 'Hoàn kho' OR (movement_type = 'Xuất' AND (note LIKE ? OR note LIKE ?)))
        GROUP BY ingredient_id, movement_type
    """, (since, "Chế biến%", "Gọi thêm%")).fetchall()
    net = {}
    for r in rows:
        sign = -1 if r["movement_type"] == "Hoàn kho" else 1
        net[r["ingredient_id"]] = net.get(r["ingredient_id"], 0) + sign * float(r["total"] or 0)
    return {k: v / days for k, v in net.items() if v > 0}


def collect_expiry_lots(conn):
    """Danh sách mọi lô có hạn sử dụng, kèm mức cảnh báo, tỷ lệ tồn và lượng dự kiến không dùng kịp."""
    summaries = get_batch_summary(conn)
    usage = get_daily_usage(conn)
    dish_count = {r["ingredient_id"]: r["c"] for r in conn.execute("""
        SELECT r.ingredient_id, COUNT(*) AS c
        FROM recipes r JOIN items it ON it.item_id = r.item_id
        WHERE it.status = 'Đang bán' GROUP BY r.ingredient_id
    """).fetchall()}
    discounts = {p["ingredient_id"]: p["discount"] for p in get_expiry_programs(conn)}
    lots = []
    for ing in conn.execute("SELECT ingredient_id, name, unit FROM ingredients").fetchall():
        s = summaries.get(ing["ingredient_id"])
        if not s:
            continue
        rate = usage.get(ing["ingredient_id"], 0)
        usable_before, wasted = 0.0, 0.0
        for b in s["batches"]:
            if not b["expiry"]:
                continue
            level = expiry_level(b["days"])
            leftover = None
            if level == "expired":
                leftover = b["quantity"]
            elif rate > 0:
                # Lô hết hạn sớm được dùng trước: so tổng lượng phải dùng với lượng bán được tới ngày hết hạn.
                usable_before += b["quantity"]
                leftover = min(b["quantity"], max(0.0, usable_before - wasted - rate * (b["days"] + 1)))
                wasted += leftover
            lots.append({
                "ingredient_id": ing["ingredient_id"], "name": ing["name"], "unit": ing["unit"],
                "expiry": b["expiry"], "days": b["days"], "quantity": b["quantity"], "level": level,
                "share": b["quantity"] / s["total"] * 100 if s["total"] else 0,
                "dishes": int(dish_count.get(ing["ingredient_id"], 0)),
                "discount": discounts.get(ing["ingredient_id"], 0) if b["status"] == "Sắp hết hạn" else 0,
                "rate": rate, "leftover": leftover,
            })
    lots.sort(key=lambda x: (x["days"], x["name"]))
    return lots


# Bảng đếm ngược: mỗi cột là một mức khẩn cấp, mỗi thẻ là một lô (số ngày còn lại in to bên trái).
# (mã, tiêu đề cột, việc cần làm, điều kiện theo số ngày còn lại, màu chính, màu nền)
EXPIRY_BOARD_COLS = [
    ("expired", "Đã hết hạn", "Loại bỏ khỏi kho", lambda d: d < 0, "#b91c1c", "#fef2f2"),
    ("today", "Hết hạn hôm nay", "Dùng ngay trong ca", lambda d: d == 0, "#dc2626", "#fff5f5"),
    ("tomorrow", "Hết hạn ngày mai", "Ưu tiên chế biến", lambda d: d == 1, "#ea580c", "#fff7ed"),
    ("d23", "Còn 2–3 ngày", "Dùng trước lô mới", lambda d: 2 <= d <= 3, "#d97706", "#fffbeb"),
    ("d47", "Còn 4–7 ngày", "Theo dõi", lambda d: 4 <= d <= 7, "#0369a1", "#f0f9ff"),
]
WEEKDAYS_VN = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]


def expiry_board_color(days):
    for _, _, _, test, color, _ in EXPIRY_BOARD_COLS:
        if test(days):
            return color
    return "#64748b"


EXPIRY_CSS = """
<style>
  .xp-head { display:flex; gap:16px; align-items:flex-start; justify-content:space-between; flex-wrap:wrap; padding:16px 18px; border-radius:8px; border:1px solid; margin-bottom:14px; }
  .xp-head.bad { background:#fef2f2; border-color:#fecaca; color:#991b1b; }
  .xp-head.warn { background:#fff7ed; border-color:#fed7aa; color:#9a3412; }
  .xp-head.info { background:#f0f9ff; border-color:#bae6fd; color:#075985; }
  .xp-head.ok { background:#f0fdf4; border-color:#bbf7d0; color:#166534; }
  .xp-head h2 { font-size:19px; font-weight:800; color:inherit; margin:0; }
  .xp-head p { font-size:13px; margin-top:4px; color:inherit; }
  .xp-chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
  .xp-chip { display:inline-flex; align-items:center; gap:5px; padding:4px 10px; border-radius:999px; background:#fff; border:1px solid var(--c); color:var(--c); font-size:12px; font-weight:600; }
  .xp-chip b { font-size:14px; }
  .xp-today { text-align:right; font-size:12px; color:inherit; white-space:nowrap; }
  .xp-today b { display:block; font-size:22px; line-height:1.1; }
  .xp-strip { display:grid; grid-template-columns:repeat(14,minmax(70px,1fr)); gap:6px; overflow-x:auto; padding-bottom:4px; }
  .xp-day { border:1px solid var(--line); border-radius:8px; padding:8px 6px; text-align:center; background:#f8fafc; color:#94a3b8; min-width:70px; }
  .xp-day b { display:block; font-size:22px; line-height:1.2; font-weight:800; }
  .xp-day-top { display:block; font-size:11px; font-weight:600; white-space:nowrap; }
  .xp-day-names { display:block; font-size:10.5px; line-height:1.3; min-height:2.6em; overflow:hidden; color:#475569; }
  .xp-day.has { background:#fff; border-color:var(--c); border-top:4px solid var(--c); color:var(--c); cursor:pointer; }
  .xp-day.has:hover { box-shadow:0 6px 16px rgba(15,45,75,.12); }
  .xp-board { display:grid; grid-template-columns:repeat(5,minmax(220px,1fr)); gap:12px; overflow-x:auto; padding-bottom:4px; }
  .xp-col { background:var(--bg); border:1px solid var(--line); border-top:4px solid var(--c); border-radius:8px; min-width:220px; display:flex; flex-direction:column; }
  .xp-col-head { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; padding:12px 12px 8px; }
  .xp-col-head b { display:block; color:var(--c); font-size:14px; }
  .xp-col-head small { display:block; color:var(--muted); font-size:11.5px; }
  .xp-col-n { min-width:34px; height:34px; padding:0 8px; border-radius:999px; background:var(--c); color:#fff; font-weight:800; font-size:16px; display:flex; align-items:center; justify-content:center; }
  .xp-col-body { display:flex; flex-direction:column; gap:8px; padding:0 10px 10px; max-height:480px; overflow-y:auto; }
  .xp-card { display:flex; gap:10px; align-items:stretch; width:100%; text-align:left; background:#fff; border:1px solid var(--line); border-left:4px solid var(--c); border-radius:8px; padding:8px; cursor:pointer; font:inherit; color:var(--ink); }
  .xp-card:hover { box-shadow:0 6px 16px rgba(15,45,75,.12); }
  .xp-count { flex:none; width:60px; border-radius:6px; background:var(--c); color:#fff; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:4px 2px; text-align:center; }
  .xp-count b { font-size:24px; line-height:1; font-weight:800; }
  .xp-count small { font-size:9.5px; line-height:1.2; margin-top:3px; }
  .xp-info { min-width:0; display:flex; flex-direction:column; gap:2px; }
  .xp-name { font-weight:700; font-size:13.5px; overflow-wrap:anywhere; }
  .xp-meta { font-size:12px; color:var(--muted); }
  .xp-notes { display:flex; flex-wrap:wrap; gap:4px; margin-top:2px; }
  .xp-note { font-size:11px; font-weight:600; padding:1px 7px; border-radius:999px; background:#f1f5f9; color:#475569; }
  .xp-note.bad { background:#fee2e2; color:#b91c1c; } .xp-note.good { background:#dcfce7; color:#166534; } .xp-note.sale { background:#fef3c7; color:#92400e; }
  .xp-empty { font-size:12px; color:#94a3b8; text-align:center; padding:18px 0; }
  .xp-modal { display:none; position:fixed; inset:0; background:rgba(15,35,55,.48); z-index:2500; align-items:center; justify-content:center; padding:18px; }
  .xp-modal.open { display:flex; }
  .xp-modal-box { width:min(720px,100%); max-height:86vh; overflow:auto; background:#fff; border-radius:12px; box-shadow:0 25px 80px rgba(0,0,0,.25); padding:20px; }
  .xp-modal-head { display:flex; justify-content:space-between; align-items:center; gap:12px; border-bottom:1px solid var(--line); padding-bottom:12px; margin-bottom:14px; }
  .xp-modal-head h2 { margin:0; font-size:18px; }
  .xp-modal-x { border:0; background:#f1f5f9; border-radius:8px; padding:6px 11px; cursor:pointer; font-size:18px; color:#475569; }
  .xp-stats { display:grid; grid-template-columns:repeat(3,1fr); gap:9px; margin-bottom:14px; }
  .xp-stat { padding:10px 12px; border-radius:8px; background:#f8fafc; border:1px solid var(--line); }
  .xp-stat b { display:block; font-size:22px; }
  .xp-stat span { font-size:11.5px; color:var(--muted); }
  .xp-modal table { min-width:0; }
  @media (max-width:520px) { .xp-stats { grid-template-columns:1fr; } }
</style>
"""

EXPIRY_MODAL_JS = """
<div class="xp-modal" id="xpModal" aria-hidden="true">
  <section class="xp-modal-box" role="dialog" aria-modal="true" aria-labelledby="xpModalTitle">
    <div class="xp-modal-head"><h2 id="xpModalTitle"></h2><button type="button" class="xp-modal-x" id="xpModalClose" aria-label="Đóng">×</button></div>
    <div id="xpModalBody"></div>
  </section>
</div>
<script>
(function () {
  var lots = __LOTS__;
  var modal = document.getElementById('xpModal'), body = document.getElementById('xpModalBody');
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function num(v) { return Number(v).toLocaleString('vi-VN'); }
  function color(d) { return d < 0 ? '#b91c1c' : d === 0 ? '#dc2626' : d === 1 ? '#ea580c' : d <= 3 ? '#d97706' : d <= 7 ? '#0369a1' : '#64748b'; }
  function daysText(d) { return d < 0 ? 'Quá hạn ' + (-d) + ' ngày' : d === 0 ? 'Hết hạn hôm nay' : 'Còn ' + d + ' ngày'; }
  function open(id) {
    var all = lots.filter(function (x) { return String(x.ingredient_id) === String(id); }).sort(function (a, b) { return a.days - b.days; });
    if (!all.length) return;
    document.getElementById('xpModalTitle').textContent = all[0].name;
    var expired = all.filter(function (x) { return x.days < 0; }).length;
    var soon = all.filter(function (x) { return x.days >= 0 && x.days <= 7; }).length;
    var stat = function (n, label, c) { return '<div class="xp-stat"><b style="color:' + c + '">' + n + '</b><span>' + label + '</span></div>'; };
    var rows = all.map(function (x) {
      return '<tr><td><b>' + esc(x.expiry) + '</b></td><td><span style="color:' + color(x.days) + ';font-weight:700;">' + daysText(x.days) + '</span></td>'
           + '<td>' + num(x.quantity) + ' ' + esc(x.unit) + '</td><td>' + x.dishes + ' món</td>'
           + '<td>' + (x.leftover == null ? '—' : num(Math.round(x.leftover * 100) / 100) + ' ' + esc(x.unit)) + '</td></tr>';
    }).join('');
    body.innerHTML = '<div class="xp-stats">' + stat(all.length, 'Tổng số lô', '#173b5b') + stat(expired, 'Lô đã hết hạn', '#b91c1c') + stat(soon, 'Lô hết hạn trong 7 ngày', '#d97706') + '</div>'
      + '<table><thead><tr><th>Hạn sử dụng</th><th>Tình trạng</th><th>Số lượng</th><th>Món đang dùng</th><th>Dự kiến dư</th></tr></thead><tbody>' + rows + '</tbody></table>'
      + '<div style="margin-top:14px;text-align:right;"><a class="btn btn-primary" href="/materials/' + encodeURIComponent(id) + '/batches">Quản lý các lô →</a></div>';
    modal.classList.add('open'); modal.setAttribute('aria-hidden', 'false');
  }
  function close() { modal.classList.remove('open'); modal.setAttribute('aria-hidden', 'true'); }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-ingredient]');
    if (b) { e.preventDefault(); open(b.getAttribute('data-ingredient')); }
  });
  document.getElementById('xpModalClose').addEventListener('click', close);
  modal.addEventListener('click', function (e) { if (e.target === modal) close(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
})();
</script>
"""


def expiry_headline_html(lots):
    """Một câu tóm tắt to, đọc là hiểu ngay tình hình hạn sử dụng hôm nay."""
    counts = {key: [x for x in lots if test(x["days"])] for key, _, _, test, _, _ in EXPIRY_BOARD_COLS}
    risk = [x for x in lots if 0 <= x["days"] <= EXPIRY_SOON_DAYS and (x["leftover"] or 0) >= 0.01]

    def names(rows, n=5):
        uniq = []
        for x in rows:
            if x["name"] not in uniq:
                uniq.append(x["name"])
        more = f" và {len(uniq) - n} nguyên liệu khác" if len(uniq) > n else ""
        return escape(", ".join(uniq[:n])) + more

    urgent = counts["today"] + counts["tomorrow"]
    week = counts["d23"] + counts["d47"]
    if counts["expired"]:
        tone = "bad"
        title = f"Có {len(counts['expired'])} lô đã hết hạn – cần loại bỏ ngay"
        sub = f"Gồm: {names(counts['expired'])}."
    elif urgent:
        tone = "warn"
        title = f"{len(urgent)} lô hết hạn trong hôm nay và ngày mai"
        sub = f"Ưu tiên dùng: {names(urgent)}."
    elif week:
        tone = "info"
        title = f"Không có lô quá hạn · {len(week)} lô hết hạn trong 7 ngày tới"
        sub = f"Sắp tới: {names(week)}."
    else:
        tone = "ok"
        title = "Kho an toàn: không có lô nào hết hạn trong 7 ngày tới"
        sub = ""
    chips = "".join(
        f'<span class="xp-chip" style="--c:{color}"><b>{len(counts[key])}</b> {label.lower()}</span>'
        for key, label, _, _, color, _ in EXPIRY_BOARD_COLS if counts[key])
    if risk:
        chips += f'<span class="xp-chip" style="--c:#9a3412"><b>{len(risk)}</b> lô dự kiến không dùng kịp</span>'
    today = date.today()
    return f"""
    <div class="xp-head {tone}">
      <div><h2>{title}</h2>{f'<p>{sub}</p>' if sub else ''}{f'<div class="xp-chips">{chips}</div>' if chips else ''}</div>
      <div class="xp-today">Hôm nay<b>{today.strftime("%d/%m")}</b>{WEEKDAYS_VN[today.weekday()]}, {today.year}</div>
    </div>"""


def expiry_strip_html(lots, horizon=14):
    """Lịch 14 ngày tới: mỗi ô là một ngày, số to = số lô hết hạn hôm đó, kèm tên nguyên liệu."""
    today = date.today()
    by_day = {}
    for x in lots:
        if 0 <= x["days"] < horizon:
            by_day.setdefault(x["days"], []).append(x)
    cells = ""
    for i in range(horizon):
        d = today + timedelta(days=i)
        day_lots = by_day.get(i, [])
        top = "Hôm nay" if i == 0 else ("Ngày mai" if i == 1 else f'{WEEKDAYS_VN[d.weekday()]} {d.strftime("%d/%m")}')
        if day_lots:
            uniq = []
            for x in day_lots:
                if x["name"] not in uniq:
                    uniq.append(x["name"])
            label = ", ".join(uniq[:2]) + (f" +{len(uniq) - 2}" if len(uniq) > 2 else "")
            tip = "; ".join(f'{x["name"]} {x["quantity"]:g} {x["unit"]}' for x in day_lots)
            cells += (f'<div class="xp-day has" style="--c:{expiry_board_color(i)}" title="{escape(tip)}" '
                      f'data-ingredient="{escape(str(day_lots[0]["ingredient_id"]))}">'
                      f'<span class="xp-day-top">{top}</span><b>{len(day_lots)}</b>'
                      f'<span class="xp-day-names">{escape(label)}</span></div>')
        else:
            cells += (f'<div class="xp-day"><span class="xp-day-top">{top}</span><b>–</b>'
                      f'<span class="xp-day-names">&nbsp;</span></div>')
    return f'<div class="xp-strip">{cells}</div>'


def expiry_board_card(lot, color):
    d = lot["days"]
    if d < 0:
        big, cap = str(-d), "ngày quá hạn"
    elif d == 0:
        big, cap = "0", "hết hạn hôm nay"
    else:
        big, cap = str(d), "ngày còn lại"
    unit = escape(lot["unit"])
    if lot["level"] == "expired":
        note = '<span class="xp-note bad">Cần loại bỏ</span>'
    elif lot["rate"] <= 0:
        note = '<span class="xp-note">Chưa có dữ liệu bán</span>'
    elif (lot["leftover"] or 0) >= 0.01:
        note = f'<span class="xp-note bad">Dự kiến dư ~{round(lot["leftover"], 2):g} {unit}</span>'
    else:
        note = '<span class="xp-note good">Dùng kịp trước hạn</span>'
    sale = f'<span class="xp-note sale">Món đang giảm {lot["discount"]:g}%</span>' if lot["discount"] else ""
    return f"""<button type="button" class="xp-card" style="--c:{color}" data-ingredient="{escape(str(lot["ingredient_id"]))}" title="Xem tất cả lô của {escape(lot["name"])}">
      <span class="xp-count"><b>{big}</b><small>{cap}</small></span>
      <span class="xp-info"><span class="xp-name">{escape(lot["name"])}</span>
        <span class="xp-meta">{lot["quantity"]:g} {unit} · HSD {lot["expiry"].strftime("%d/%m/%Y")}</span>
        <span class="xp-notes">{note}{sale}</span></span>
    </button>"""


def expiry_board_html(lots):
    cols = ""
    for key, label, hint, test, color, bg in EXPIRY_BOARD_COLS:
        group = [x for x in lots if test(x["days"])]
        cards = "".join(expiry_board_card(x, color) for x in group) or '<div class="xp-empty">Không có lô nào</div>'
        cols += f"""
        <div class="xp-col" style="--c:{color};--bg:{bg}">
          <div class="xp-col-head"><div><b>{label}</b><small>{hint}</small></div><span class="xp-col-n">{len(group)}</span></div>
          <div class="xp-col-body">{cards}</div>
        </div>"""
    later = [x for x in lots if x["days"] > 7]
    foot = (f'<p class="muted" style="margin-top:10px;">Còn {len(later)} lô hạn trên 7 ngày – xem ở thẻ “Danh sách chi tiết”.</p>'
            if later else "")
    return f'<div class="xp-board">{cols}</div>{foot}'


@app.route("/inventory/expiry")
@login_required
def expiry_dashboard():
    try:
        window = int(request.args.get("days", EXPIRY_SOON_DAYS))
    except ValueError:
        window = EXPIRY_SOON_DAYS
    if window not in (3, 7, 14, 30):
        window = EXPIRY_SOON_DAYS
    conn = get_db()
    lots = collect_expiry_lots(conn)
    alert_count = len(get_inventory_alerts(conn))
    conn.close()

    rows = ""
    shown = [x for x in lots if x["days"] <= window]
    for lot in shown:
        label, color, css = EXPIRY_LEVEL_META[lot["level"]]
        unit = escape(lot["unit"])
        if lot["level"] == "expired":
            forecast = '<span class="muted">—</span>'
        elif lot["rate"] <= 0:
            forecast = '<span class="muted">Chưa có dữ liệu bán</span>'
        elif lot["leftover"] >= 0.01:
            forecast = (f'<strong style="color:#be123c;">Dư khoảng {round(lot["leftover"], 2):g} {unit}</strong>'
                        f'<br><span class="muted">Dùng TB {round(lot["rate"], 2):g} {unit}/ngày</span>')
        else:
            forecast = (f'<span style="color:#166534;font-weight:700;">Dùng kịp</span>'
                        f'<br><span class="muted">Dùng TB {round(lot["rate"], 2):g} {unit}/ngày</span>')
        if lot["dishes"]:
            dishes = f'{lot["dishes"]} món' + (f' <span class="status green">đang giảm {lot["discount"]}%</span>' if lot["discount"] else "")
        else:
            dishes = '<span class="muted">Chưa có món dùng</span>'
        bg = {"expired": "#fff1f2", "critical": "#fff1f2", "warning": "#fffbeb"}.get(lot["level"], "")
        rows += f"""
        <tr style="background:{bg};">
            <td style="border-left:4px solid {color};"><strong>{escape(lot["name"])}</strong><br><span class="muted">{escape(lot["ingredient_id"])}</span></td>
            <td>{fmt_date(lot["expiry"])}<br><span class="status {css}">{days_text(lot["days"])}</span></td>
            <td><strong>{lot["quantity"]:g} {unit}</strong>
                <div style="display:flex;align-items:center;gap:8px;margin-top:5px;">
                    <div style="width:70px;height:6px;background:#e5e7eb;border-radius:3px;overflow:hidden;"><div style="width:{lot["share"]:.0f}%;height:6px;background:{color};"></div></div>
                    <span class="muted">{lot["share"]:.0f}% tồn</span></div></td>
            <td>{dishes}</td>
            <td>{forecast}</td>
            <td>{EXPIRY_ADVICE[lot["level"]] or '<span class="muted">—</span>'}</td>
            <td><a class="btn btn-secondary" href="/materials/{escape(lot["ingredient_id"])}/batches">Chi tiết</a></td>
        </tr>"""
    if not rows:
        rows = f'<tr><td colspan="7" class="empty">Không có lô nào đã hết hạn hoặc hết hạn trong {window} ngày tới.</td></tr>'

    options = "".join(f'<option value="{d}" {"selected" if d == window else ""}>Trong {d} ngày tới</option>' for d in (3, 7, 14, 30))
    lots_json = json.dumps([
        {"ingredient_id": x["ingredient_id"], "name": x["name"], "unit": x["unit"],
         "expiry": fmt_date(x["expiry"]), "days": x["days"], "quantity": x["quantity"],
         "dishes": x["dishes"], "leftover": x["leftover"]}
        for x in lots], ensure_ascii=False).replace("</", "<\\/")

    board_html = f"""
    <div class="panel">
      <h2>Lô cần xử lý, xếp theo số ngày còn lại <span class="muted" style="font-weight:500;">· bấm vào thẻ để xem mọi lô của nguyên liệu</span></h2>
      {expiry_board_html(lots)}
    </div>"""
    table_html = f"""
    <div class="panel">
      <form method="GET" action="/inventory/expiry#xp-table" class="actions" style="margin-bottom:12px;">
        <select name="days" onchange="this.form.submit()" style="width:auto;">{options}</select>
      </form>
      <div style="overflow-x:auto;"><table>
        <thead><tr><th>Nguyên liệu</th><th>Hạn sử dụng</th><th>Số lượng</th><th>Món đang bán</th><th>Dự kiến dùng</th><th>Đề xuất</th><th></th></tr></thead>
        <tbody>{rows}</tbody>
      </table></div>
    </div>"""
    urgent_n = sum(1 for x in lots if x["days"] <= EXPIRY_SOON_DAYS)
    expiry_tabs = tabs_html("expiry", [
        ("xp-board", "Bảng đếm ngược", board_html, urgent_n),
        ("xp-table", "Danh sách chi tiết", table_html, len(shown)),
    ])

    body = f"""
    {EXPIRY_CSS}
    <div class="header"><div><h1>Dashboard hạn sử dụng</h1></div></div>
    {material_tabs("expiry", alert_count)}
    {expiry_headline_html(lots)}
    <div class="panel"><h2>14 ngày tới</h2>{expiry_strip_html(lots)}</div>
    {expiry_tabs}
    {EXPIRY_MODAL_JS.replace("__LOTS__", lots_json)}
    """
    return render_page("Dashboard hạn sử dụng", body, "materials")


@app.route("/inventory/change/<string:id>", methods=["POST"])
@role_required("admin", "chef")
def change_inventory(id):
    action = request.form.get("action", "increase")
    amount = to_float(request.form.get("amount", 1), 1)
    next_page = request.form.get("next")

    def back(**kwargs):
        if next_page == "batches":
            return redirect(url_for("material_batches", id=id, **kwargs))
        return redirect(url_for("inventory_check" if next_page == "check" else "inventory", **kwargs))

    if amount <= 0:
        return back(error="Số lượng phải lớn hơn 0.")
    new_expiry = None
    if action == "increase":
        new_expiry, exp_error = parse_expiry_input(request.form.get("expiry_date"))
        if exp_error:
            return back(error=exp_error)

    conn = get_db()
    ingredient = conn.execute("SELECT * FROM ingredients WHERE ingredient_id = ?", (id,)).fetchone()
    if not ingredient:
        conn.close()
        return back()
    before = float(ingredient["quantity"])
    note = "Điều chỉnh thủ công"
    if action == "increase":
        after, movement_type = round(before + amount, 4), "Nhập"
        batch_add(conn, id, amount, new_expiry)      # mỗi hạn sử dụng là một lô riêng
        if new_expiry:
            note += f" – HSD lô nhập {fmt_date(new_expiry)}"
    else:
        if amount > before:
            conn.close()
            return back(error=f'Không thể xuất {amount:g} {ingredient["unit"]} {ingredient["name"]}: kho chỉ còn {before:g} {ingredient["unit"]}.')
        after, movement_type = round(before - amount, 4), "Xuất"
        batch_take(conn, id, amount)                 # lô hết hạn sớm nhất xuất trước
    conn.execute("UPDATE ingredients SET quantity = ? WHERE ingredient_id = ?", (after, id))
    batch_sync(conn, id)
    conn.execute("""INSERT INTO inventory_movements
        (ingredient_id, movement_type, quantity, before_quantity, after_quantity, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (id, movement_type, amount, before, after, note, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

    push_inventory_check(
        f"Đã {movement_type.lower()} {ingredient['name']}",
        [{"id": id, "type": movement_type, "amount": amount, "before": before, "after": after}])
    return back()


# =========================================================
# CÔNG THỨC MÓN ĂN
# =========================================================

@app.route("/recipes")
@role_required("admin", "chef")
def recipes():
    conn = get_db()

    items = conn.execute("SELECT item_id, name FROM items ORDER BY name").fetchall()
    ingredients = conn.execute("SELECT ingredient_id, name, unit FROM ingredients ORDER BY name").fetchall()

    selected_item_id = request.args.get("item_id", "")

    current_recipes = []
    selected_item_name = ""
    if selected_item_id:
        current_recipes = conn.execute("""
            SELECT r.recipe_id, r.ingredient_id, r.quantity, i.name AS ing_name, i.unit
            FROM recipes r
            JOIN ingredients i ON r.ingredient_id = i.ingredient_id
            WHERE r.item_id = ?
            ORDER BY i.name
        """, (selected_item_id,)).fetchall()

        item_row = conn.execute("SELECT name FROM items WHERE item_id = ?", (selected_item_id,)).fetchone()
        if item_row:
            selected_item_name = item_row["name"]

    recipe_summary = conn.execute("""
        SELECT items.item_id, items.name, COUNT(recipes.recipe_id) AS ing_count
        FROM items
        LEFT JOIN recipes ON items.item_id = recipes.item_id
        GROUP BY items.item_id, items.name
        ORDER BY items.name
    """).fetchall()

    conn.close()

    item_options = '<option value="">-- Chọn món ăn --</option>'
    for it in items:
        selected = "selected" if str(it["item_id"]) == str(selected_item_id) else ""
        item_options += f'<option value="{it["item_id"]}" {selected}>{escape(it["name"])}</option>'

    ingredient_options = '<option value="">-- Chọn nguyên liệu --</option>'
    for ing in ingredients:
        ingredient_options += f'<option value="{ing["ingredient_id"]}">{escape(ing["name"])} ({escape(ing["unit"])})</option>'

    initial_rows = ""
    if current_recipes:
        for r in current_recipes:
            row_ing_options = '<option value="">-- Chọn nguyên liệu --</option>'
            for ing in ingredients:
                sel = "selected" if ing["ingredient_id"] == r["ingredient_id"] else ""
                row_ing_options += f'<option value="{ing["ingredient_id"]}" {sel}>{escape(ing["name"])} ({escape(ing["unit"])})</option>'

            initial_rows += f"""
            <tr class="recipe-row">
                <td>
                    <select name="ingredient_id[]" required>
                        {row_ing_options}
                    </select>
                </td>
                <td>
                    <input type="number" name="quantity[]" value="{r["quantity"]:g}"
                           min="0.0001" step="any" required style="width:140px;">
                </td>
                <td>
                    <button type="button" class="btn btn-danger" onclick="removeRow(this)">Xóa</button>
                </td>
            </tr>
            """
    else:
        initial_rows = f"""
        <tr class="recipe-row">
            <td>
                <select name="ingredient_id[]" required>
                    {ingredient_options}
                </select>
            </td>
            <td>
                <input type="number" name="quantity[]" min="0.0001" step="any"
                       required style="width:140px;">
            </td>
            <td>
                <button type="button" class="btn btn-danger" onclick="removeRow(this)">Xóa</button>
            </td>
        </tr>
        """

    summary_rows = ""
    for s in recipe_summary:
        status = (
            f'<span class="status green">{s["ing_count"]} nguyên liệu</span>'
            if s["ing_count"] > 0
            else '<span class="status red">Chưa có công thức</span>'
        )
        summary_rows += f"""
        <tr>
            <td>{escape(s["name"])}</td>
            <td>{status}</td>
            <td><a class="btn btn-primary" href="/recipes?item_id={s["item_id"]}">Thiết lập</a></td>
        </tr>
        """

    if not summary_rows:
        summary_rows = '<tr><td colspan="3" class="empty">Chưa có món ăn nào.</td></tr>'

    form_section = ""
    if selected_item_id:
        form_section = f"""
        <div class="panel">
            <h2>Công thức cho: {escape(selected_item_name)}</h2>
            <form method="POST" action="/recipes/save">
                <input type="hidden" name="item_id" value="{selected_item_id}">
                <table id="recipe-table">
                    <thead>
                        <tr>
                            <th style="width:60%;">Nguyên liệu</th>
                            <th style="width:25%;">Định lượng (cho 1 món)</th>
                            <th style="width:15%;">Thao tác</th>
                        </tr>
                    </thead>
                    <tbody id="recipe-body">
                        {initial_rows}
                    </tbody>
                </table>
                <br>
                <button type="button" class="btn btn-secondary" onclick="addRow()">+ Thêm nguyên liệu</button>
                <button class="btn btn-primary">Lưu toàn bộ công thức</button>
                <a class="btn btn-secondary" href="/recipes">Hủy</a>
            </form>
        </div>

        <script>
            var ingredientOptionsHTML = `{ingredient_options}`;

            function addRow() {{
                var tbody = document.getElementById('recipe-body');
                var tr = document.createElement('tr');
                tr.className = 'recipe-row';
                tr.innerHTML = `
                    <td>
                        <select name="ingredient_id[]" required>
                            ${{ingredientOptionsHTML}}
                        </select>
                    </td>
                    <td>
                        <input type="number" name="quantity[]" min="0.0001" step="any"
                               required style="width:140px;">
                    </td>
                    <td>
                        <button type="button" class="btn btn-danger" onclick="removeRow(this)">Xóa</button>
                    </td>
                `;
                tbody.appendChild(tr);
            }}

            function removeRow(btn) {{
                var tr = btn.closest('tr');
                var tbody = document.getElementById('recipe-body');
                if (tbody.querySelectorAll('tr').length > 1) {{
                    tr.remove();
                }} else {{
                    alert('Phải có ít nhất 1 dòng nguyên liệu.');
                }}
            }}
        </script>
        """
    else:
        form_section = ""

    body = f"""
    <div class="header">
        <div><h1>Công thức món ăn</h1></div>
    </div>

    <div class="panel">
        <h2>Chọn món ăn</h2>
        <form method="GET" action="/recipes">
            <div class="form-grid" style="grid-template-columns: 1fr auto;">
                <div class="form-group">
                    <label>Món ăn</label>
                    <select name="item_id" onchange="this.form.submit()">
                        {item_options}
                    </select>
                </div>
                <button class="btn btn-primary">Chọn</button>
            </div>
        </form>
    </div>

    {form_section}

    <div class="panel">
        <h2>Danh sách món và công thức</h2>
        <table>
            <thead>
                <tr><th>Món ăn</th><th>Trạng thái công thức</th><th>Thao tác</th></tr>
            </thead>
            <tbody>{summary_rows}</tbody>
        </table>
    </div>
    """
    return render_page("Công thức món ăn", body, "recipes")


@app.route("/recipes/save", methods=["POST"])
@role_required("admin", "chef")
def save_recipe():
    item_id = request.form.get("item_id")

    if not item_id:
        return redirect(url_for("recipes"))

    ingredient_ids = request.form.getlist("ingredient_id[]")
    quantities = request.form.getlist("quantity[]")

    valid_rows = []
    for ing_id, qty_str in zip(ingredient_ids, quantities):
        if not ing_id:
            continue
        try:
            qty = float(qty_str)
        except (ValueError, TypeError):
            continue
        if qty > 0:
            valid_rows.append((ing_id, qty))

    conn = get_db()
    conn.execute("DELETE FROM recipes WHERE item_id = ?", (item_id,))

    for ing_id, qty in valid_rows:
        conn.execute("""
            INSERT INTO recipes(recipe_id, item_id, ingredient_id, quantity)
            VALUES(?, ?, ?, ?)
        """, (next_code(conn, "recipes", "CT"), item_id, ing_id, qty))

    conn.commit()
    conn.close()

    return redirect(url_for("recipes", item_id=item_id))


# =========================================================
# KHUYẾN MẠI
# =========================================================

@app.route("/promotions")
@role_required("admin", "cashier")
def promotions():
    conn = get_db()
    promo_q = request.args.get("q", "").strip()
    promo_status = request.args.get("status", "")
    promo_type = request.args.get("apply_type", "")
    promo_sort = request.args.get("sort", "newest")
    promo_order = {"newest": "p.start_date DESC, p.promotion_id DESC", "oldest": "p.start_date ASC, p.promotion_id ASC", "discount_desc": "p.discount DESC", "name": "p.name"}.get(promo_sort, "p.start_date DESC, p.promotion_id DESC")
    data = conn.execute(f"""
        SELECT p.*, i.name AS item_name FROM promotions p
        LEFT JOIN items i ON p.item_id = i.item_id
        WHERE (p.name LIKE ? OR p.promotion_id LIKE ? OR COALESCE(i.name, '') LIKE ?)
        ORDER BY {promo_order}
    """, (f"%{promo_q}%", f"%{promo_q}%", f"%{promo_q}%")).fetchall()
    if promo_type: data = [p for p in data if p["apply_type"] == promo_type]
    if promo_status: data = [p for p in data if promotion_status(p["start_date"], p["end_date"]) == promo_status]
    all_promos = conn.execute("""SELECT p.*, i.name AS item_name FROM promotions p
        LEFT JOIN items i ON p.item_id = i.item_id""").fetchall()
    items = conn.execute("SELECT item_id, name FROM items ORDER BY name").fetchall()
    report_end = request.args.get("report_end", date.today().isoformat())
    report_start = request.args.get("report_start", (date.today() - timedelta(days=30)).isoformat())
    report_item = request.args.get("report_item", "").strip()
    report_data = conn.execute("""
        SELECT p.promotion_id, p.name, p.apply_type, COUNT(o.order_id) AS order_count,
               COALESCE(SUM(o.discount_amount), 0) AS discount_total,
               COALESCE(SUM(o.final_total), 0) AS revenue_total
        FROM promotions p
        LEFT JOIN orders o ON o.promotion_id = p.promotion_id
          AND date(o.order_date) BETWEEN ? AND ? AND o.status != 'Đã hủy'
        WHERE p.apply_type = 'order'
        GROUP BY p.promotion_id, p.name, p.apply_type
        ORDER BY discount_total DESC, order_count DESC
    """, (report_start, report_end)).fetchall()
    # Chương trình gắn hạng thành viên: đếm theo đơn của khách đúng hạng đó (không theo orders.promotion_id).
    order_promos = conn.execute("SELECT * FROM promotions WHERE apply_type = 'order'").fetchall()
    tier_usage = member_promo_usage(conn, order_promos, report_start, report_end)
    merged = []
    for r in report_data:
        d = dict(r)
        d["tier"] = "—"
        if d["promotion_id"] in tier_usage:
            u = tier_usage[d["promotion_id"]]
            d.update(order_count=u["n"], discount_total=u["disc"], revenue_total=u["rev"],
                     tier=promo_tier(d, "Hạng thành viên"))
        merged.append(d)
    report_data = sorted(merged, key=lambda d: (-float(d["discount_total"] or 0), -int(d["order_count"] or 0), d["name"]))
    item_report_sql = """
        SELECT p.promotion_id, p.name AS promo_name, p.discount, i.name AS item_name,
               COUNT(DISTINCT o.order_id) AS order_count,
               COALESCE(SUM(CASE WHEN o.order_id IS NOT NULL THEN oi.quantity - COALESCE(oi.canceled_quantity, 0) END), 0) AS qty,
               COALESCE(SUM(CASE WHEN o.order_id IS NOT NULL THEN oi.item_discount END), 0) AS discount_total,
               COALESCE(SUM(CASE WHEN o.order_id IS NOT NULL THEN (oi.quantity - COALESCE(oi.canceled_quantity, 0)) * oi.price - oi.item_discount END), 0) AS revenue_total
        FROM promotions p
        JOIN items i ON i.item_id = p.item_id
        LEFT JOIN order_details oi ON oi.item_id = p.item_id AND oi.item_discount > 0
        LEFT JOIN orders o ON o.order_id = oi.order_id AND o.status != 'Đã hủy'
             AND date(o.order_date) BETWEEN substr(p.start_date, 1, 10) AND substr(p.end_date, 1, 10)
             AND date(o.order_date) BETWEEN ? AND ?
        WHERE p.apply_type = 'item'
    """
    item_report_params = [report_start, report_end]
    if report_item:
        item_report_sql += " AND p.item_id = ?"
        item_report_params.append(report_item)
    item_report_sql += """
        GROUP BY p.promotion_id, p.name, p.discount, i.name
        ORDER BY discount_total DESC, qty DESC, i.name
    """
    item_report_data = conn.execute(item_report_sql, item_report_params).fetchall()
    promo_item_ids = {r["item_id"] for r in conn.execute(
        "SELECT DISTINCT item_id FROM promotions WHERE apply_type = 'item' AND item_id IS NOT NULL").fetchall()}
    tiers = conn.execute("SELECT * FROM member_tiers WHERE discount_percent > 0 ORDER BY min_total_spent").fetchall()
    expiry_programs = get_expiry_programs(conn)
    conn.close()

    item_report_rows = ""
    sum_orders = sum_qty = sum_disc = sum_rev = 0
    for r in item_report_data:
        sum_orders += r["order_count"]; sum_qty += r["qty"]
        sum_disc += r["discount_total"]; sum_rev += r["revenue_total"]
        item_report_rows += f"""<tr><td>{escape(r['item_name'])}</td><td>{escape(r['promo_name'])}</td>
          <td>{r['discount']:g}%</td><td>{r['order_count']}</td><td>{r['qty']:g}</td>
          <td>{r['discount_total']:,.0f} đ</td><td>{r['revenue_total']:,.0f} đ</td></tr>"""
    if item_report_rows:
        item_report_rows += f"""<tr style="font-weight:700;"><td colspan="3">Tổng cộng</td><td>{sum_orders}</td><td>{sum_qty:g}</td>
          <td>{sum_disc:,.0f} đ</td><td>{sum_rev:,.0f} đ</td></tr>"""
    else:
        item_report_rows = ('<tr><td colspan="7" class="empty">Món đã chọn không có chương trình khuyến mại theo món.</td></tr>'
                            if report_item else '<tr><td colspan="7" class="empty">Chưa có khuyến mại theo món.</td></tr>')

    report_rows = ""
    for r in report_data:
        tier_cell = f'<span class="status blue">Hạng {escape(r["tier"])}</span>' if r["tier"] != "—" else '<span class="muted">Mọi khách</span>'
        report_rows += f"""<tr><td>{escape(r['name'])}</td><td>{tier_cell}</td><td>{r['order_count']}</td>
          <td>{r['discount_total']:,.0f} đ</td><td>{r['revenue_total']:,.0f} đ</td></tr>"""
    if not report_rows:
        report_rows = '<tr><td colspan="5" class="empty">Chưa có dữ liệu báo cáo.</td></tr>'

    stats = {"Đang diễn ra": 0, "Sắp diễn ra": 0, "Đã kết thúc": 0}
    promo_error = request.args.get("error", "")
    message = f'<div class="message">{escape(promo_error)}</div>' if promo_error else ""

    rows = ""
    for promotion in data:
        status = promotion_status(promotion["start_date"], promotion["end_date"])
        stats[status] += 1

        if promotion["apply_type"] == "item" and promotion["item_name"]:
            apply_label = f'<span class="status blue">Theo món</span> {escape(promotion["item_name"])}'
        else:
            apply_label = '<span class="status green">Toàn đơn</span>'

        rows += f"""
        <tr>
            <td>{promotion["promotion_id"]}</td>
            <td>{escape(promotion["name"])}</td>
            <td>{apply_label}</td>
            <td>{promotion["discount"]:g}%</td>
            <td>{promotion["min_order_value"]:,.0f} đ</td>
            <td>{promotion["start_date"]}</td>
            <td>{promotion["end_date"]}</td>
            <td><span class="status {css_status(status)}">{status}</span></td>
            <td>
                <form method="POST" action="/promotions/delete/{promotion["promotion_id"]}" onsubmit="return confirm('Xóa chương trình khuyến mại này?');">
                    <button class="btn btn-danger">Xóa</button>
                </form>
            </td>
        </tr>
        """
    if not rows:
        rows = '<tr><td colspan="9" class="empty">Chưa có chương trình khuyến mại nào.</td></tr>'

    # Điền sẵn form khi bấm "Tạo khuyến mại" từ đề xuất ở trang Phân tích món ăn.
    new_item = request.args.get("new_item", "").strip()
    new_discount = request.args.get("new_discount", "").strip()
    new_name = ""
    if new_item:
        picked = next((it for it in items if it["item_id"] == new_item), None)
        if picked:
            new_name = f"Giảm {new_discount}% {picked['name']}" if new_discount else f"Khuyến mại {picked['name']}"
        else:
            new_item = ""
    item_options = "".join(
        f'<option value="{it["item_id"]}" {"selected" if it["item_id"] == new_item else ""}>{escape(it["name"])}</option>' for it in items
    ) or '<option value="">Chưa có mặt hàng</option>'
    report_item_options = '<option value="">-- Tất cả món có khuyến mại --</option>' + "".join(
        f'<option value="{it["item_id"]}" {"selected" if it["item_id"] == report_item else ""}>{escape(it["name"])}</option>'
        for it in items if it["item_id"] in promo_item_ids)

    def promo_opt(value, label, current):
        return f'<option value="{value}" {"selected" if current == value else ""}>{label}</option>'

    status_chips = " ".join(
        f'<span class="status {css_status(k)}">{k}: {v}</span>' for k, v in stats.items())
    list_html = f"""
    <div class="panel">
        <form method="GET" action="/promotions#promo-list" class="actions" style="margin-bottom:12px;flex-wrap:wrap;">
            <input name="q" value="{escape(promo_q)}" placeholder="Tìm mã, chương trình hoặc món">
            <select name="status"><option value="">Mọi trạng thái</option>{promo_opt("Đang diễn ra", "Đang diễn ra", promo_status)}{promo_opt("Sắp diễn ra", "Sắp diễn ra", promo_status)}{promo_opt("Đã kết thúc", "Đã kết thúc", promo_status)}</select>
            <select name="apply_type"><option value="">Toàn bộ phạm vi</option>{promo_opt("order", "Toàn đơn", promo_type)}{promo_opt("item", "Theo món", promo_type)}</select>
            <select name="sort">{promo_opt("newest", "Ngày mới nhất", promo_sort)}{promo_opt("oldest", "Ngày cũ nhất", promo_sort)}{promo_opt("discount_desc", "Mức giảm cao nhất", promo_sort)}{promo_opt("name", "Tên A–Z", promo_sort)}</select>
            <button class="btn btn-primary">Lọc</button><a class="btn btn-secondary" href="/promotions#promo-list">Đặt lại</a>
        </form>
        <div class="actions" style="margin-bottom:12px;">{status_chips}</div>
        <table>
            <thead>
                <tr><th>Mã</th><th>Chương trình</th><th>Phạm vi</th><th>Mức giảm</th><th>Đơn tối thiểu</th><th>Bắt đầu</th><th>Kết thúc</th><th>Trạng thái</th><th>Thao tác</th></tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""

    add_html = f"""
    <div class="panel">
        <form method="POST" action="/promotions/add">
            <div class="form-grid" style="grid-template-columns: 1.5fr 1fr 1fr 1fr 1.2fr auto;">
                <div class="form-group"><label>Tên chương trình</label><input name="name" value="{escape(new_name)}" required></div>
                <div class="form-group"><label>Mức giảm (%)</label><input type="number" name="discount" min="0" max="100" step="any" value="{escape(new_discount)}" required></div>
                <div class="form-group"><label>Ngày bắt đầu</label><input type="date" name="start_date" value="{date.today().isoformat() if new_item else ''}" required></div>
                <div class="form-group"><label>Ngày kết thúc</label><input type="date" name="end_date" value="{(date.today() + timedelta(days=14)).isoformat() if new_item else ''}" required></div>
                <div class="form-group"><label>Đơn tối thiểu (đ)</label><input type="number" name="min_order_value" min="0" step="1000" value="0" required></div>
                <button class="btn btn-primary">+ Thêm</button>
            </div>
            <div class="form-grid" style="grid-template-columns: 1fr 1fr; margin-top: 12px;">
                <div class="form-group">
                    <label>Loại khuyến mại</label>
                    <select name="apply_type" id="apply_type" onchange="toggleItemSelect()">
                        <option value="order">Toàn đơn</option>
                        <option value="item" {"selected" if new_item else ""}>Theo mặt hàng cụ thể</option>
                    </select>
                </div>
                <div class="form-group" id="item_select_group" style="display:{'block' if new_item else 'none'};">
                    <label>Chọn mặt hàng</label>
                    <select name="item_id">{item_options}</select>
                </div>
            </div>
        </form>
    </div>"""

    order_report_html = f"""
    <div class="panel">
        <form method="GET" action="/promotions#order-report">
          <input type="hidden" name="report_item" value="{escape(report_item)}">
          <div class="form-grid" style="grid-template-columns:1fr 1fr auto;">
            <div class="form-group"><label>Từ ngày</label><input type="date" name="report_start" value="{escape(report_start)}" required></div>
            <div class="form-group"><label>Đến ngày</label><input type="date" name="report_end" value="{escape(report_end)}" required></div>
            <button class="btn btn-primary">Lọc báo cáo</button>
          </div>
        </form><br>
        <table><thead><tr><th>Chương trình</th><th>Đối tượng</th><th>Số đơn áp dụng</th><th>Tổng tiền giảm</th><th>Doanh thu sau giảm</th></tr></thead><tbody>{report_rows}</tbody></table>
    </div>"""

    item_report_html = f"""
    <div class="panel">
        <form method="GET" action="/promotions#item-report">
          <div class="form-grid" style="grid-template-columns:1.4fr 1fr 1fr auto auto;">
            <div class="form-group"><label>Món</label><select name="report_item">{report_item_options}</select></div>
            <div class="form-group"><label>Từ ngày</label><input type="date" name="report_start" value="{escape(report_start)}" required></div>
            <div class="form-group"><label>Đến ngày</label><input type="date" name="report_end" value="{escape(report_end)}" required></div>
            <button class="btn btn-primary">Lọc theo món</button>
            <a class="btn btn-secondary" href="/promotions?{escape(urlencode({"report_start": report_start, "report_end": report_end}))}#item-report">Tất cả món</a>
          </div>
        </form><br>
        <div style="overflow-x:auto;">
        <table><thead><tr><th>Món</th><th>Chương trình</th><th>Mức giảm</th><th>Số đơn</th><th>SL bán được giảm</th><th>Tổng tiền giảm</th><th>Doanh thu sau giảm</th></tr></thead><tbody>{item_report_rows}</tbody></table>
        </div>
    </div>"""

    promo_tabs = tabs_html("promo", [
        ("promo-list", "Danh sách khuyến mại", list_html, len(data)),
        ("add-promo", "+ Thêm chương trình", add_html),
        ("expiry-sale", "🍃 Xả hàng HSD", expiry_promotions_panel_html(expiry_programs), len(expiry_programs)),
        ("order-report", "Báo cáo toàn đơn", order_report_html),
        ("item-report", "Báo cáo theo món", item_report_html),
    ], default="add-promo" if new_item else "promo-list")

    body = f"""
    <div class="header">
        <div><h1>Quản lý khuyến mại</h1></div>
    </div>
    {message}
    {promo_banner_html(all_promos, expiry_programs, tiers)}
    {promo_tabs}
    <script>
        function toggleItemSelect() {{
            var applyType = document.getElementById('apply_type').value;
            document.getElementById('item_select_group').style.display =
                (applyType === 'item') ? 'block' : 'none';
        }}
    </script>
    """
    return render_page("Quản lý khuyến mại", body, "promotions")



@app.route("/promotions/expiry/<string:ingredient_id>")
@role_required("admin", "cashier")
def promotion_expiry_detail(ingredient_id):
    conn = get_db()
    ing = conn.execute("SELECT * FROM ingredients WHERE ingredient_id = ?", (ingredient_id,)).fetchone()
    if not ing:
        conn.close()
        return redirect(url_for("promotions"))
    item_map = get_expiry_item_discounts(conn)
    batch = get_batch_summary(conn).get(ingredient_id) or empty_batch_summary()
    dishes = conn.execute("""
        SELECT it.item_id, it.name, it.price, it.category_id, r.quantity AS per_portion
        FROM recipes r JOIN items it ON it.item_id = r.item_id
        WHERE r.ingredient_id = ? AND it.status = 'Đang bán' ORDER BY it.name
    """, (ingredient_id,)).fetchall()
    conn.close()

    # Tính theo lô sắp hết hạn gần nhất (nếu có); không có thì theo hạn gần nhất của nguyên liệu.
    has_soon = batch["soon_qty"] > 0
    exp_date = batch["soon_date"] if has_soon else ing["expiry_date"]
    exp_qty = batch["soon_qty"] if has_soon else float(ing["quantity"])
    status, days = expiry_info(exp_date)
    pct = expiry_discount(days) if (days is not None and exp_qty > 0) else 0
    unit = escape(ing["unit"])
    d = parse_iso_date(exp_date)
    date_label = d.strftime("%d/%m/%Y") if d else "chưa khai báo"

    if pct:
        reason = (f"Nguyên liệu <strong>{escape(ing['name'])}</strong> còn <strong>{exp_qty:g} {unit}</strong> trong kho "
                  f"nhưng hạn sử dụng là <strong>{date_label}</strong> ({days_text(days)}). Để tránh lãng phí, hệ thống tự động "
                  f"giảm <strong>{pct}%</strong> cho các món dùng nguyên liệu này, khuyến khích khách gọi trước khi nguyên liệu hết hạn.")
        tone = "background:#ecfccb;border-color:#bef264;color:#3f6212;"
    elif status == "Hết hạn":
        reason = (f"Nguyên liệu <strong>{escape(ing['name'])}</strong> đã quá hạn sử dụng ({date_label}, {days_text(days)}). "
                  f"Không áp dụng giảm giá – nguyên liệu cần được loại bỏ và các món dùng nó tạm không nên bán.")
        tone = "background:#ffe4e6;border-color:#fecdd3;color:#9f1239;"
    elif float(ing["quantity"]) <= 0:
        reason = f"Nguyên liệu <strong>{escape(ing['name'])}</strong> hiện đã hết trong kho nên không có chương trình xả hàng."
        tone = "background:#f1f5f9;border-color:#dbe4ed;color:#475569;"
    else:
        reason = (f"Nguyên liệu <strong>{escape(ing['name'])}</strong> (HSD {date_label}) chưa tới ngưỡng "
                  f"{EXPIRY_SOON_DAYS} ngày nên hiện chưa có chương trình xả hàng.")
        tone = "background:#e0f2fe;border-color:#bae6fd;color:#075985;"

    rule_rows = ""
    prev = -1
    for limit, p in EXPIRY_DISCOUNT_RULES:
        current = bool(pct) and pct == p and days is not None and days <= limit and days > prev
        label = f"Còn {prev + 1} – {limit} ngày" if limit - prev > 1 else f"Còn {limit} ngày"
        if prev == -1:
            label = "Hết hạn hôm nay hoặc còn 1 ngày" if limit == 1 else f"Còn ≤ {limit} ngày"
        rule_rows += (f'<tr style="{"background:#ecfccb;font-weight:700;" if current else ""}">'
                      f'<td>{label}</td><td>-{p}%</td><td>{"Đang áp dụng" if current else ""}</td></tr>')
        prev = limit

    dish_rows = ""
    for dish in dishes:
        applied = item_map.get(dish["item_id"])
        if applied and applied["ingredient_id"] == ingredient_id:
            eff = applied["discount"]
            note = "Đang áp dụng"
        elif applied:
            eff = applied["discount"]
            note = f'Đang áp dụng mức cao hơn từ nguyên liệu {escape(applied["ingredient_name"])}'
        else:
            eff, note = 0, "Không giảm"
        new_price = float(dish["price"]) * (1 - eff / 100)
        price_html = (f'<s class="muted">{dish["price"]:,.0f} đ</s> <strong style="color:#166534;">{new_price:,.0f} đ</strong>'
                      if eff else f'{dish["price"]:,.0f} đ')
        dish_rows += f"""<tr>
            <td><div style="display:flex;gap:12px;align-items:center;">{food_thumb_html(dish["category_id"], dish["name"])}<span class="food-name">{escape(dish["name"])}</span></div></td>
            <td>{dish["per_portion"]:g} {unit} / phần</td><td>{price_html}</td>
            <td>{('<span class="status green">-%g%%</span> ' % eff) if eff else ''}{note}</td></tr>"""
    if not dish_rows:
        dish_rows = '<tr><td colspan="4" class="empty">Chưa có món đang bán nào dùng nguyên liệu này (kiểm tra mục Công thức món ăn).</td></tr>'

    body = f"""
    <div class="header">
        <div><h1>Xả hàng: {escape(ing["name"])}</h1></div>
        <div class="actions"><a class="btn btn-secondary" href="/materials/{escape(ingredient_id)}/batches">Chi tiết lô theo hạn sử dụng</a>
        <a class="btn btn-secondary" href="/promotions">← Quay lại khuyến mại</a></div>
    </div>
    <div class="panel" style="{tone}">
        <h2 style="color:inherit;">Vì sao có chương trình này?</h2>
        <p style="font-size:15px;line-height:1.7;">{reason}</p>
    </div>
    <div class="cards">
        <div class="card"><div class="card-title">Tồn kho</div><div class="card-value">{ing["quantity"]:g} <span style="font-size:15px;">{unit}</span></div></div>
        <div class="card"><div class="card-title">Hạn sử dụng</div><div class="card-value" style="font-size:22px;">{date_label}</div></div>
        <div class="card"><div class="card-title">Thời gian còn lại</div><div class="card-value" style="font-size:22px;">{days_text(days) if days is not None else "—"}</div></div>
        <div class="card"><div class="card-title">Mức giảm hiện tại</div><div class="card-value" style="color:#16a34a;">{f"-{pct}%" if pct else "0%"}</div></div>
    </div>
    <div class="panel">
        <h2>Quy tắc giảm giá theo hạn sử dụng</h2>
        <table style="min-width:0;"><thead><tr><th>Thời gian còn lại</th><th>Mức giảm</th><th></th></tr></thead><tbody>{rule_rows}</tbody></table>
    </div>
    <div class="panel">
        <h2>Các món được giảm giá ({len(dishes)} món)</h2>
        <div style="overflow-x:auto;"><table>
            <thead><tr><th>Món ăn</th><th>Dùng nguyên liệu</th><th>Giá bán</th><th>Áp dụng</th></tr></thead>
            <tbody>{dish_rows}</tbody>
        </table></div>
    </div>
    """
    return render_page(f"Xả hàng {ing['name']}", body, "promotions")


@app.route("/promotions/add", methods=["POST"])
@role_required("admin", "cashier")
def add_promotion():
    name = request.form.get("name", "").strip()
    apply_type = request.form.get("apply_type", "order")
    item_id = request.form.get("item_id", "").strip() or None
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    try:
        discount = float(request.form.get("discount", ""))
        min_order_value = float(request.form.get("min_order_value", 0) or 0)
    except ValueError:
        return redirect(url_for("promotions", _anchor="add-promo", error="Mức giảm và đơn tối thiểu phải là số."))

    if not name or not start_date or not end_date:
        return redirect(url_for("promotions", _anchor="add-promo", error="Vui lòng nhập đầy đủ thông tin chương trình."))
    if not 0 <= discount <= 100:
        return redirect(url_for("promotions", _anchor="add-promo", error="Mức giảm phải từ 0 đến 100%."))
    if min_order_value < 0:
        return redirect(url_for("promotions", _anchor="add-promo", error="Đơn tối thiểu không được âm."))
    if end_date < start_date:
        return redirect(url_for("promotions", _anchor="add-promo", error="Ngày kết thúc phải sau hoặc cùng ngày bắt đầu."))
    if apply_type not in ("order", "item"):
        apply_type = "order"

    conn = get_db()
    if apply_type == "item":
        if not item_id or not conn.execute("SELECT 1 FROM items WHERE item_id = ?", (item_id,)).fetchone():
            conn.close()
            return redirect(url_for("promotions", _anchor="add-promo", error="Vui lòng chọn mặt hàng áp dụng khuyến mại."))
        min_order_value = 0
    else:
        item_id = None
    conn.execute("""INSERT INTO promotions(promotion_id, name, discount, start_date, end_date, item_id, apply_type, min_order_value)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                 (next_code(conn, "promotions", "KM"), name, discount, start_date, end_date,
                  item_id, apply_type, min_order_value))
    conn.commit()
    conn.close()
    return redirect(url_for("promotions", _anchor="promo-list", ok=f"Đã thêm chương trình {name}."))


@app.route("/promotions/delete/<string:id>", methods=["POST"])
@role_required("admin", "cashier")
def delete_promotion(id):
    conn = get_db()
    promo = conn.execute("SELECT name FROM promotions WHERE promotion_id = ?", (id,)).fetchone()
    if not promo:
        conn.close()
        return redirect(url_for("promotions", _anchor="promo-list", error="Không tìm thấy chương trình khuyến mại."))
    try:
        conn.execute("DELETE FROM promotions WHERE promotion_id = ?", (id,))
        conn.commit()
    except IntegrityError:
        # Chương trình toàn đơn đã gắn vào đơn hàng -> không xóa để giữ lịch sử giảm giá.
        conn.rollback()
        conn.close()
        return redirect(url_for("promotions", _anchor="promo-list", error=f"Không thể xóa \"{promo['name']}\" vì đã có đơn hàng sử dụng chương trình này."))
    conn.close()
    return redirect(url_for("promotions", _anchor="promo-list", ok=f'Đã xóa chương trình {promo["name"]}.'))


@app.route("/promotions/report")
@role_required("admin", "cashier")
def promotions_report():
    q = request.args.get("q", "").strip().lower()
    f_group = request.args.get("group", "")
    f_tier = request.args.get("tier", "")
    f_cond = request.args.get("condition", "")
    f_status = request.args.get("status", "")
    f_usage = request.args.get("usage", "")
    sort = request.args.get("sort", "newest")
    rep_start = request.args.get("report_start", "").strip()
    rep_end = request.args.get("report_end", "").strip()

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    min_disc = to_float(request.args.get("min_discount"))
    max_disc = to_float(request.args.get("max_discount"))

    rs, re_ = rep_start or "1000-01-01", rep_end or "9999-12-31"

    conn = get_db()
    promos = conn.execute("""
        SELECT p.*, i.name AS item_name FROM promotions p
        LEFT JOIN items i ON p.item_id = i.item_id
    """).fetchall()

    usage = {}
    for r in conn.execute("""
        SELECT promotion_id AS pid, COUNT(*) AS n,
               COALESCE(SUM(discount_amount),0) AS disc, COALESCE(SUM(final_total),0) AS rev
        FROM orders
        WHERE promotion_id IS NOT NULL AND status != 'Đã hủy'
          AND date(order_date) BETWEEN ? AND ?
        GROUP BY promotion_id
    """, (rs, re_)).fetchall():
        usage[r["pid"]] = {"n": r["n"], "disc": r["disc"], "rev": r["rev"]}

    for r in conn.execute("""
        SELECT p.promotion_id AS pid, COUNT(DISTINCT o.order_id) AS n,
               COALESCE(SUM(oi.item_discount),0) AS disc,
               COALESCE(SUM((oi.quantity - COALESCE(oi.canceled_quantity,0)) * oi.price - oi.item_discount),0) AS rev
        FROM promotions p
        JOIN order_details oi ON oi.item_id = p.item_id AND oi.item_discount > 0
        JOIN orders o ON o.order_id = oi.order_id AND o.status != 'Đã hủy'
             AND date(o.order_date) BETWEEN substr(p.start_date,1,10) AND substr(p.end_date,1,10)
             AND date(o.order_date) BETWEEN ? AND ?
        WHERE p.apply_type = 'item'
        GROUP BY p.promotion_id
    """, (rs, re_)).fetchall():
        usage[r["pid"]] = {"n": r["n"], "disc": r["disc"], "rev": r["rev"]}
    usage.update(member_promo_usage(conn, promos, rs, re_))
    conn.close()

    data = []
    for p in promos:
        status = promotion_status(p["start_date"], p["end_date"])
        group = promo_group(p)
        tier = promo_tier(p, group)
        u = usage.get(p["promotion_id"], {"n": 0, "disc": 0, "rev": 0})
        if q and q not in f'{p["promotion_id"]} {p["name"]} {p["item_name"] or ""}'.lower():
            continue
        if f_group and group != f_group:
            continue
        if f_tier and tier != f_tier:
            continue
        if f_cond and promo_condition_key(p) != f_cond:
            continue
        if f_status and status != f_status:
            continue
        if f_usage == "used" and u["n"] == 0:
            continue
        if f_usage == "unused" and u["n"] > 0:
            continue
        if min_disc is not None and p["discount"] < min_disc:
            continue
        if max_disc is not None and p["discount"] > max_disc:
            continue
        data.append({"p": p, "status": status, "group": group, "tier": tier, "u": u})

    sorters = {
        "newest": lambda d: promo_date(d["p"]["start_date"]),
        "discount_desc": lambda d: d["p"]["discount"],
        "usage_desc": lambda d: d["u"]["n"],
        "saved_desc": lambda d: d["u"]["disc"],
        "name": lambda d: d["p"]["name"].lower(),
    }
    data.sort(key=sorters.get(sort, sorters["newest"]), reverse=sort != "name")

    stats = {"Đang diễn ra": 0, "Sắp diễn ra": 0, "Đã kết thúc": 0}
    total_uses = total_disc = 0
    rows = ""
    group_css = {"Hạng thành viên": "yellow", "Ngày lễ": "red", "Theo hóa đơn": "green", "Theo món": "blue"}
    for d in data:
        p, u = d["p"], d["u"]
        stats[d["status"]] += 1
        total_uses += u["n"]
        total_disc += u["disc"]
        rows += f"""
        <tr>
            <td>{p["promotion_id"]}</td>
            <td>{escape(p["name"])}</td>
            <td><span class="status {group_css[d["group"]]}">{d["group"]}</span></td>
            <td>{d["tier"]}</td>
            <td>{p["discount"]:g}%</td>
            <td>{escape(promo_condition_label(p))}</td>
            <td>{promo_date(p["start_date"])}<br><span class="muted">→ {promo_date(p["end_date"])}</span></td>
            <td><span class="status {css_status(d["status"])}">{d["status"]}</span></td>
            <td>{u["n"]}</td>
            <td>{u["disc"]:,.0f} đ</td>
            <td>{u["rev"]:,.0f} đ</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="11" class="empty">Không có chương trình khuyến mại phù hợp bộ lọc.</td></tr>'

    group_opts = "".join(f'<option {sel(g, f_group)}>{g}</option>'
                         for g in ("Hạng thành viên", "Ngày lễ", "Theo hóa đơn", "Theo món"))
    tier_opts = "".join(f'<option {sel(t, f_tier)}>{t}</option>'
                        for t in ("Kim cương", "Vàng", "Bạc", "Đồng", "VIP", "Mọi hạng"))
    cond_opts = "".join(f'<option value="{k}" {sel(k, f_cond)}>{v}</option>' for k, v in
                        (("none", "Không điều kiện"), ("min_order", "Có đơn tối thiểu"), ("item", "Áp dụng cho món cụ thể")))
    status_opts = "".join(f'<option {sel(s, f_status)}>{s}</option>' for s in stats)
    usage_opts = "".join(f'<option value="{k}" {sel(k, f_usage)}>{v}</option>' for k, v in
                         (("used", "Đã có lượt dùng"), ("unused", "Chưa có lượt dùng")))
    sort_opts = "".join(f'<option value="{k}" {sel(k, sort)}>{v}</option>' for k, v in
                        (("newest", "Mới nhất"), ("discount_desc", "Mức giảm cao nhất"),
                         ("usage_desc", "Nhiều lượt dùng nhất"), ("saved_desc", "Tổng giảm nhiều nhất"),
                         ("name", "Tên A–Z")))

    body = f"""
    <div class="header">
        <div><h1>Quản lý khuyến mại</h1></div>
    </div>
    <div class="cards">
        <div class="card"><div class="card-title">Tổng chương trình</div><div class="card-value">{len(data)}</div></div>
        <div class="card"><div class="card-title">Đang diễn ra</div><div class="card-value">{stats["Đang diễn ra"]}</div></div>
        <div class="card"><div class="card-title">Sắp diễn ra</div><div class="card-value">{stats["Sắp diễn ra"]}</div></div>
        <div class="card"><div class="card-title">Đã kết thúc</div><div class="card-value">{stats["Đã kết thúc"]}</div></div>
    </div>
    <div class="cards" style="grid-template-columns:repeat(2,1fr);">
        <div class="card"><div class="card-title">Tổng lượt áp dụng (theo bộ lọc)</div><div class="card-value">{total_uses}</div></div>
        <div class="card"><div class="card-title">Tổng tiền đã giảm (theo bộ lọc)</div><div class="card-value">{total_disc:,.0f} đ</div></div>
    </div>
    <div class="panel">
        <h2>Bộ lọc báo cáo</h2>
        <form method="GET" action="/promotions">
            <div class="form-grid" style="grid-template-columns:repeat(4,1fr);">
                <div class="form-group"><label>Tìm kiếm</label><input name="q" value="{escape(q)}" placeholder="Mã, tên chương trình, tên món"></div>
                <div class="form-group"><label>Nhóm khuyến mại</label><select name="group"><option value="">Tất cả</option>{group_opts}</select></div>
                <div class="form-group"><label>Hạng thành viên</label><select name="tier"><option value="">Tất cả</option>{tier_opts}</select></div>
                <div class="form-group"><label>Điều kiện áp dụng</label><select name="condition"><option value="">Tất cả</option>{cond_opts}</select></div>
                <div class="form-group"><label>Tình trạng</label><select name="status"><option value="">Tất cả</option>{status_opts}</select></div>
                <div class="form-group"><label>Lượt sử dụng</label><select name="usage"><option value="">Tất cả</option>{usage_opts}</select></div>
                <div class="form-group"><label>Mức giảm từ (%)</label><input type="number" name="min_discount" min="0" max="100" step="any" value="{escape(request.args.get('min_discount', ''))}"></div>
                <div class="form-group"><label>Mức giảm đến (%)</label><input type="number" name="max_discount" min="0" max="100" step="any" value="{escape(request.args.get('max_discount', ''))}"></div>
                <div class="form-group"><label>Thống kê lượt dùng từ ngày</label><input type="date" name="report_start" value="{escape(rep_start)}"></div>
                <div class="form-group"><label>Đến ngày</label><input type="date" name="report_end" value="{escape(rep_end)}"></div>
                <div class="form-group"><label>Sắp xếp</label><select name="sort">{sort_opts}</select></div>
            </div>
            <br>
            <button class="btn btn-primary">Lọc báo cáo</button>
            <a class="btn btn-secondary" href="/promotions">Đặt lại</a>
        </form>
    </div>
    <div class="panel">
        <h2>Chi tiết chương trình khuyến mại</h2>
        <div style="overflow-x:auto;">
        <table>
            <thead><tr><th>Mã</th><th>Chương trình</th><th>Nhóm</th><th>Hạng TV</th><th>Mức giảm</th><th>Điều kiện</th><th>Thời gian</th><th>Tình trạng</th><th>Số đơn</th><th>Tổng giảm</th><th>Doanh thu sau giảm</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
        </div>
    </div>
    """
    return render_page("Quản lý khuyến mại", body, "promotions")


# =========================================================
# HỖ TRỢ ĐƠN HÀNG + CÔNG THỨC
# =========================================================

def aggregate_recipe_requirements(conn, order_id):
    rows = conn.execute("""
        SELECT oi.item_id, (oi.quantity - COALESCE(oi.canceled_quantity, 0)) AS order_qty,
               r.ingredient_id, r.quantity AS recipe_qty,
               i.name AS ingredient_name, i.unit,
               i.quantity AS stock
        FROM order_details oi
        JOIN recipes r ON oi.item_id = r.item_id
        JOIN ingredients i ON r.ingredient_id = i.ingredient_id
        WHERE oi.order_id = ? AND (oi.quantity - COALESCE(oi.canceled_quantity, 0)) > 0
        FOR UPDATE
    """, (order_id,)).fetchall()

    requirements = {}
    covered_items = set()

    ordered_items = conn.execute(
        "SELECT item_id FROM order_details WHERE order_id = ? AND (quantity - COALESCE(canceled_quantity, 0)) > 0", (order_id,)
    ).fetchall()

    for row in rows:
        covered_items.add(row["item_id"])
        required_amount = row["order_qty"] * row["recipe_qty"]

        if row["ingredient_id"] not in requirements:
            requirements[row["ingredient_id"]] = {
                "name": row["ingredient_name"],
                "unit": row["unit"],
                "required": 0,
                "stock": row["stock"]
            }
        requirements[row["ingredient_id"]]["required"] += required_amount

    missing_recipe = [
        row["item_id"] for row in ordered_items
        if row["item_id"] not in covered_items
    ]
    return requirements, missing_recipe


def confirm_order_stock(order_id):
    conn = get_db()
    conn.begin()
    try:
        order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if not order:
            raise ValueError("Không tìm thấy đơn hàng.")
        if order["status"] != "Chờ xác nhận":
            raise ValueError("Đơn hàng này không còn ở trạng thái chờ xác nhận.")

        requirements, missing_recipe = aggregate_recipe_requirements(conn, order_id)
        if missing_recipe:
            raise ValueError("Có món trong đơn chưa được thiết lập công thức. Hãy khai báo công thức trước khi xác nhận.")

        insufficient = []
        for ingredient_id, req in requirements.items():
            usable = usable_stock_for_cooking(conn, ingredient_id, req["stock"])
            if req["required"] > usable + 1e-9:
                insufficient.append(
                    f'{req["name"]}: cần {req["required"]:g} {req["unit"]}, '
                    f'còn dùng được {usable:g} {req["unit"]}'
                )
        if insufficient:
            raise ValueError("Không đủ nguyên liệu: " + "; ".join(insufficient))

        changes = []
        for ingredient_id, req in requirements.items():
            before = float(req["stock"])
            after = before - float(req["required"])
            changes.append({"id": ingredient_id, "type": "Xuất", "amount": float(req["required"]),
                            "before": before, "after": after})
            conn.execute("UPDATE ingredients SET quantity = ? WHERE ingredient_id = ?", (after, ingredient_id))
            batch_take(conn, ingredient_id, req["required"], valid_first=True)
            batch_sync(conn, ingredient_id)
            conn.execute("""INSERT INTO inventory_movements
                (ingredient_id, movement_type, quantity, before_quantity, after_quantity, note, created_at)
                VALUES (?, 'Xuất', ?, ?, ?, ?, ?)""",
                (ingredient_id, req["required"], before, after, f"Chế biến đơn #{order_id}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        confirmed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            UPDATE orders SET status = 'Đã xác nhận', confirmed_at = ?, stock_deducted = 1
            WHERE order_id = ?
        """, (confirmed_at, order_id))

        calculate_order_totals(conn, order_id)

        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return changes


def restore_order_stock(conn, order_id):
    requirements, _ = aggregate_recipe_requirements(conn, order_id)
    for ingredient_id, req in requirements.items():
        before = float(req["stock"])
        after = before + float(req["required"])
        conn.execute("UPDATE ingredients SET quantity = ? WHERE ingredient_id = ?", (after, ingredient_id))
        batch_restore(conn, ingredient_id, req["required"])
        batch_sync(conn, ingredient_id)
        conn.execute("""INSERT INTO inventory_movements
            (ingredient_id, movement_type, quantity, before_quantity, after_quantity, note, created_at)
            VALUES (?, 'Hoàn kho', ?, ?, ?, ?, ?)""",
            (ingredient_id, req["required"], before, after, f"Hủy đơn #{order_id}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


# =========================================================
# DANH SÁCH ĐƠN HÀNG
# =========================================================

@app.route("/orders")
@login_required
def orders():
    conn = get_db()
    order_search = request.args.get("q", "").strip()
    order_status = request.args.get("status", "")
    order_start = request.args.get("start_date", "")
    order_end = request.args.get("end_date", "")
    order_sort = request.args.get("sort", "newest")
    order_by = {"newest": "order_date DESC, order_id DESC", "oldest": "order_date ASC, order_id ASC", "total_desc": "final_total DESC, total DESC", "total_asc": "final_total ASC, total ASC"}.get(order_sort, "order_date DESC, order_id DESC")
    sql = "SELECT * FROM orders WHERE (customer_name LIKE ? OR order_id LIKE ?)"
    params = [f"%{order_search}%", f"%{order_search}%"]
    if order_status:
        sql += " AND status = ?"; params.append(order_status)
    if order_start:
        sql += " AND date(order_date) >= ?"; params.append(order_start)
    if order_end:
        sql += " AND date(order_date) <= ?"; params.append(order_end)
    data = conn.execute(sql + f" ORDER BY {order_by}", params).fetchall()
    pending_ack_orders = {g["order_id"] for g in get_kitchen_alerts(conn)}
    # Bổ sung thông tin số dòng món và tên món để người dùng thấy một đơn có thể gồm nhiều món.
    order_summaries = {}
    for order in data:
        summary = conn.execute("""
            SELECT COUNT(*) AS item_lines,
                   COALESCE(SUM(oi.quantity - COALESCE(oi.canceled_quantity, 0)), 0) AS total_qty,
                   GROUP_CONCAT(i.name SEPARATOR ', ') AS item_names
            FROM order_details oi
            JOIN items i ON i.item_id = oi.item_id
            WHERE oi.order_id = ?
              AND (oi.quantity - COALESCE(oi.canceled_quantity, 0)) > 0
        """, (order["order_id"],)).fetchone()
        order_summaries[order["order_id"]] = summary
    conn.close()

    user = get_current_user()
    can_create = has_perm(user, "orders_create")

    rows = ""
    for order in data:
        final_total = order["final_total"] if order["final_total"] else order["total"]
        summary = order_summaries.get(order["order_id"])
        item_lines = int(summary["item_lines"] or 0) if summary else 0
        total_qty = int(summary["total_qty"] or 0) if summary else 0
        item_names = (summary["item_names"] or "Chưa có món") if summary else "Chưa có món"
        # Rút gọn danh sách tên món trên bảng; trang chi tiết vẫn hiển thị đầy đủ từng dòng.
        if len(item_names) > 110:
            item_names = item_names[:107] + "..."
        invoice_link = ("" if order["status"] == "Đã hủy" else
                        f'<a class="btn btn-secondary" href="/orders/{order["order_id"]}/invoice" target="_blank">Hóa đơn</a>')
        rows += f"""
        <tr>
            <td>#{order["order_id"]}</td>
            <td>{escape(order["customer_name"])}</td>
            <td>{order["order_date"]}</td>
            <td><strong>{item_lines} món</strong><br><span class="muted">Tổng SL: {total_qty}</span><br><span class="muted">{escape(item_names)}</span></td>
            <td>{final_total:,.0f} đ</td>
            <td><span class="status {css_status(order["status"])}">{escape(order["status"])}</span>{'<br><span class="status red">Có món mới</span>' if order["order_id"] in pending_ack_orders else ''}</td>
            <td><div class="actions"><a class="btn btn-primary" href="/orders/{order["order_id"]}">Chi tiết</a>{invoice_link}</div></td>
        </tr>
        """
    if not rows:
        rows = '<tr><td colspan="7" class="empty">Chưa có đơn hàng nào.</td></tr>'

    create_btn = (
        '<a class="btn btn-primary" href="/orders/new">+ Tạo đơn hàng</a>'
        if can_create else ""
    )

    body = f"""
    <div class="header">
        <div><h1>Quản lý đặt món</h1></div>
    </div>
    <div class="panel">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <h2>Danh sách đơn hàng</h2>
            {create_btn}
        </div>
        <form method="GET" action="/orders" class="actions" style="margin:12px 0;flex-wrap:wrap;">
            <input name="q" value="{escape(order_search)}" placeholder="Tìm mã đơn hoặc khách hàng">
            <select name="status"><option value="">Tất cả trạng thái</option>{''.join(f'<option value="{st}" {"selected" if order_status==st else ""}>{st}</option>' for st in ["Chờ xác nhận","Đã xác nhận","Đang chuẩn bị","Đã hoàn thành","Đã hủy"])}</select>
            <label>Từ <input type="date" name="start_date" value="{escape(order_start)}"></label><label>Đến <input type="date" name="end_date" value="{escape(order_end)}"></label>
            <select name="sort"><option value="newest" {"selected" if order_sort=="newest" else ""}>Mới nhất</option><option value="oldest" {"selected" if order_sort=="oldest" else ""}>Cũ nhất</option><option value="total_desc" {"selected" if order_sort=="total_desc" else ""}>Tổng tiền cao–thấp</option><option value="total_asc" {"selected" if order_sort=="total_asc" else ""}>Tổng tiền thấp–cao</option></select>
            <button class="btn btn-primary">Lọc đơn hàng</button><a class="btn btn-secondary" href="/orders">Đặt lại</a>
        </form>
        <table>
            <thead>
                <tr><th>Mã đơn</th><th>Khách hàng</th><th>Thời gian</th><th>Các món trong đơn</th><th>Thành tiền</th><th>Trạng thái</th><th>Thao tác</th></tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    """
    return render_page("Quản lý đặt món", body, "orders")


# =========================================================
# HÓA ĐƠN BÁN HÀNG
# =========================================================

def vnd_in_words(amount):
    """Số tiền bằng chữ: 1005000 -> 'Một triệu không trăm lẻ năm nghìn đồng'."""
    n = int(round(amount))
    if n <= 0:
        return "Không đồng"
    digits = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]

    def read3(num, full):
        tram, chuc, donvi = num // 100, (num % 100) // 10, num % 10
        parts = []
        if full or tram > 0:
            parts.append(digits[tram] + " trăm")
        if chuc > 1:
            parts.append(digits[chuc] + " mươi")
        elif chuc == 1:
            parts.append("mười")
        elif donvi > 0 and (full or tram > 0):
            parts.append("lẻ")
        if donvi > 0:
            if chuc > 1 and donvi == 1:
                parts.append("mốt")
            elif chuc > 0 and donvi == 5:
                parts.append("lăm")
            else:
                parts.append(digits[donvi])
        return " ".join(parts)

    units = ["", " nghìn", " triệu", " tỷ"]
    groups = []
    while n > 0:
        groups.append(n % 1000)
        n //= 1000
    out = []
    for idx in range(len(groups) - 1, -1, -1):
        if groups[idx] == 0:
            continue
        full = idx < len(groups) - 1
        out.append(read3(groups[idx], full) + (units[idx] if idx < len(units) else ""))
    words = " ".join(out) + " đồng"
    return words[0].upper() + words[1:]


# ---------------------------------------------------------
# QR chuyển khoản (VietQR) in trên hóa đơn
# ---------------------------------------------------------
# Mã QR được tạo ngay trong file này (không gọi dịch vụ ngoài, không cần cài thêm thư viện),
# theo chuẩn VietQR/NAPAS 247: ứng dụng ngân hàng tự điền tài khoản, số tiền và nội dung.
# Tên người nhận do ứng dụng ngân hàng tự tra theo số tài khoản (PHAM HA LY), không lấy từ QR.

def _tlv(tag, value):
    return f"{tag}{len(value):02d}{value}"


def _crc16_ccitt(text):
    crc = 0xFFFF
    for byte in text.encode("ascii"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def vietqr_payload(bank_bin, account_no, amount, memo):
    """Chuỗi dữ liệu VietQR chuyển khoản tới số tài khoản, kèm số tiền (đồng, số nguyên) và nội dung."""
    account = _tlv("00", bank_bin) + _tlv("01", account_no)
    merchant = _tlv("00", "A000000727") + _tlv("01", account) + _tlv("02", "QRIBFTTA")
    body = (_tlv("00", "01") + _tlv("01", "12") + _tlv("38", merchant) + _tlv("53", "704")
            + _tlv("54", str(int(amount))) + _tlv("58", "VN") + _tlv("62", _tlv("08", memo)) + "6304")
    return body + _crc16_ccitt(body)


# Bảng thông số mã QR cho mức sửa lỗi M, chỉ số = phiên bản (1..40).
_QR_ECC_PER_BLOCK = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
                     26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28]
_QR_NUM_BLOCKS = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
                  17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49]


def _qr_gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _qr_rs_remainder(data, degree):
    divisor = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            divisor[j] = _qr_gf_mul(divisor[j], root)
            if j + 1 < degree:
                divisor[j] ^= divisor[j + 1]
        root = _qr_gf_mul(root, 2)
    result = [0] * degree
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _qr_gf_mul(coef, factor)
    return result


def _qr_raw_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        numalign = ver // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if ver >= 7:
            result -= 36
    return result


def _qr_penalty(m):
    size = len(m)
    score = 0
    lines = [row for row in m] + [[m[y][x] for y in range(size)] for x in range(size)]
    for line in lines:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
        text = "".join("1" if v else "0" for v in line)
        score += 40 * (text.count("10111010000") + text.count("00001011101"))
    for y in range(size - 1):
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(1 for v in row if v) for row in m)
    score += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
    return score


def qr_matrix(text):
    """Ma trận mã QR (True = ô đen) cho chuỗi text: chế độ byte, mức sửa lỗi M, tự chọn phiên bản."""
    data = text.encode("utf-8")
    for ver in range(1, 41):
        data_codewords = _qr_raw_modules(ver) // 8 - _QR_ECC_PER_BLOCK[ver] * _QR_NUM_BLOCKS[ver]
        count_bits = 8 if ver <= 9 else 16
        if 4 + count_bits + 8 * len(data) <= data_codewords * 8:
            break
    else:
        raise ValueError("Dữ liệu quá dài, không tạo được mã QR.")

    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in range(count_bits - 1, -1, -1)]
    for b in data:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    capacity = data_codewords * 8
    bits += [0] * min(4, capacity - len(bits))
    bits += [0] * (-len(bits) % 8)
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(codewords) < data_codewords:
        codewords.append(pad)
        pad ^= 0xEC ^ 0x11

    # Chia khối, thêm mã sửa lỗi Reed-Solomon rồi xen kẽ các khối.
    num_blocks = _QR_NUM_BLOCKS[ver]
    ecc_len = _QR_ECC_PER_BLOCK[ver]
    raw_codewords = _qr_raw_modules(ver) // 8
    num_short = num_blocks - raw_codewords % num_blocks
    short_len = raw_codewords // num_blocks
    blocks, k = [], 0
    for i in range(num_blocks):
        dat = codewords[k:k + short_len - ecc_len + (0 if i < num_short else 1)]
        k += len(dat)
        ecc = _qr_rs_remainder(dat, ecc_len)
        if i < num_short:
            dat = dat + [0]
        blocks.append(dat + ecc)
    stream = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= num_short:
                stream.append(blk[i])

    size = ver * 4 + 17
    m = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x, y, dark):
        m[y][x] = bool(dark)
        fixed[y][x] = True

    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    if ver >= 2:
        numalign = ver // 7 + 2
        step = 26 if ver == 32 else (ver * 4 + numalign * 2 + 1) // (numalign * 2 - 2) * 2
        pos = [6] + sorted(size - 7 - i * step for i in range(numalign - 1))
        last = len(pos) - 1
        for i, cy in enumerate(pos):
            for j, cx in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        fmt = mask                      # mức M có 2 bit mức sửa lỗi = 00
        rem = fmt
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        val = ((fmt << 10) | rem) ^ 0x5412
        bit = lambda i: (val >> i) & 1
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6))
        put(8, 8, bit(7))
        put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, size - 15 + i, bit(i))
        put(8, size - 8, True)

    draw_format(0)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        val = (ver << 12) | rem
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            put(a, b, (val >> i) & 1)
            put(b, a, (val >> i) & 1)

    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not fixed[y][x] and i < len(stream) * 8:
                    m[y][x] = bool((stream[i >> 3] >> (7 - (i & 7))) & 1)
                    i += 1
        right -= 2

    masks = [
        lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
        lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
        lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
        lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
    ]

    def apply_mask(fn):
        for y in range(size):
            for x in range(size):
                if not fixed[y][x] and fn(x, y):
                    m[y][x] = not m[y][x]

    best_mask, best_score = 0, None
    for idx, fn in enumerate(masks):
        apply_mask(fn)
        draw_format(idx)
        score = _qr_penalty(m)
        if best_score is None or score < best_score:
            best_mask, best_score = idx, score
        apply_mask(fn)                  # XOR lần nữa để bỏ mặt nạ
    apply_mask(masks[best_mask])
    draw_format(best_mask)
    return m


def qr_svg(text, quiet=4):
    """Ảnh SVG của mã QR (ô vuông sắc nét, có viền trắng theo chuẩn)."""
    m = qr_matrix(text)
    size = len(m)
    parts = []
    for y, row in enumerate(m):
        x = 0
        while x < size:
            if row[x]:
                start = x
                while x < size and row[x]:
                    x += 1
                parts.append(f"M{start + quiet} {y + quiet}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    full = size + quiet * 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {full} {full}" width="{full * 8}" '
            f'height="{full * 8}" shape-rendering="crispEdges"><rect width="{full}" height="{full}" fill="#fff"/>'
            f'<path d="{"".join(parts)}" fill="#000"/></svg>')


# Bố cục hóa đơn: phần .invoice rộng đúng 190 mm (A4 dọc trừ lề 10 mm) cả trên màn hình lẫn khi in,
# mọi kích thước bên trong tính theo em nên chỉ cần đổi --fit là thu nhỏ đồng đều.
INVOICE_CSS = """
  * { box-sizing:border-box; margin:0; padding:0; font-family:Inter,-apple-system,"Segoe UI",Arial,sans-serif; }
  html { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  body { background:#e8eef5; color:#172b4d; font-size:14px; line-height:1.45; padding:18px 12px 40px; }
  .toolbar { width:210mm; max-width:100%; margin:0 auto 12px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .btn { display:inline-flex; align-items:center; gap:6px; padding:10px 14px; border-radius:9px; border:1px solid #dbe4ed; background:#fff; color:#334155; font-weight:700; font-size:13px; text-decoration:none; cursor:pointer; }
  .btn.primary { background:#0369a1; color:#fff; border-color:#0369a1; }
  .btn[disabled] { opacity:.6; cursor:wait; }
  .hint { display:none; }
  .notice { display:none; width:210mm; max-width:100%; margin:0 auto 12px; padding:11px 14px; border-radius:9px; background:#fef2f2; border:1px solid #fca5a5; color:#991b1b; font-size:13px; gap:10px; align-items:center; flex-wrap:wrap; }
  .notice.show { display:flex; }
  .notice span { flex:1 1 260px; }
  .viewport { overflow-x:auto; }
  .sheet { width:210mm; min-height:297mm; margin:0 auto; padding:10mm; background:#fff; box-shadow:0 8px 30px rgba(15,45,75,.14); }
  .invoice { width:190mm; font-size:calc(11pt * var(--fit, 1)); line-height:1.38; }
  .head { display:flex; justify-content:space-between; align-items:flex-start; gap:1.5em; padding-bottom:.6em; border-bottom:.2em solid #0c4a6e; }
  .brand h1 { font-size:1.9em; line-height:1.2; letter-spacing:.06em; color:#082f49; }
  .brand p { font-size:.86em; color:#475569; }
  .doc { text-align:right; flex:none; }
  .title { font-size:1.5em; line-height:1.25; font-weight:800; letter-spacing:.04em; color:#0c4a6e; }
  .subtitle { font-size:1em; color:#334155; }
  .stamp { display:inline-block; margin-top:.35em; padding:.15em .8em; border:.14em solid; border-radius:.4em; font-weight:800; font-size:.86em; letter-spacing:.06em; }
  .stamp.paid { color:#166534; border-color:#166534; }
  .stamp.draft { color:#b45309; border-color:#b45309; }
  .meta { display:grid; grid-template-columns:1fr 1fr; gap:.2em 2em; margin:.7em 0; padding:.55em 1em; background:#f0f9ff; border:1px solid #bae6fd; border-radius:.6em; }
  .meta b { color:#334155; }
  table { width:100%; border-collapse:collapse; }
  thead { display:table-header-group; }
  th { background:#0c4a6e; color:#fff; font-size:.86em; text-transform:uppercase; letter-spacing:.02em; padding:.5em .7em; text-align:left; }
  td { padding:.36em .7em; border-bottom:1px dashed #94a3b8; vertical-align:top; }
  tr { break-inside:avoid; page-break-inside:avoid; }
  .r { text-align:right; } .c { text-align:center; }
  th.r { text-align:right; } th.c { text-align:center; }
  .sub { color:#b91c1c; font-size:.82em; }
  .pay { display:grid; grid-template-columns:17em 1fr; gap:1.8em; align-items:stretch; margin-top:.9em; break-inside:avoid; page-break-inside:avoid; }
  .pay.noqr { grid-template-columns:1fr 28em; }
  .paycol { display:flex; flex-direction:column; min-width:0; }
  .qrbox { border:1px solid #94a3b8; border-radius:.7em; padding:.6em .6em .7em; text-align:center; align-self:start; }
  .qr-cap { font-size:.86em; font-weight:700; color:#0c4a6e; text-transform:uppercase; letter-spacing:.03em; margin-bottom:.3em; }
  .qrbox img, .qrbox svg { display:block; width:13em; height:13em; min-width:34mm; min-height:34mm; margin:0 auto; image-rendering:pixelated; }
  .qr-fail { display:none; width:13em; height:13em; min-width:34mm; min-height:34mm; margin:0 auto; border:1px dashed #b91c1c; color:#b91c1c; font-size:.9em; align-items:center; justify-content:center; padding:1em; }
  .qrbox.failed img, .qrbox.failed svg { display:none; }
  .qrbox.failed .qr-fail { display:flex; }
  .bank { margin-top:.4em; font-size:.92em; line-height:1.4; }
  .bank .shop { font-size:1.12em; font-weight:800; color:#082f49; letter-spacing:.03em; }
  .bank .acc { font-size:1.08em; font-weight:800; letter-spacing:.04em; }
  .bank .memo { margin-top:.3em; padding-top:.3em; border-top:1px dashed #94a3b8; }
  .totals div { display:flex; justify-content:space-between; gap:1em; padding:.28em 0; }
  .totals .disc { color:#b91c1c; }
  .totals .grand { border-top:.14em solid #0c4a6e; margin-top:.45em; padding-top:.5em; font-size:1.5em; font-weight:800; color:#0c4a6e; align-items:baseline; }
  .totals .grand span:first-child { font-size:.72em; }
  .words { margin-top:.7em; }
  .note { margin-top:.6em; font-size:.92em; color:#334155; }
  .sign { display:grid; grid-template-columns:1fr 1fr; text-align:center; margin-top:auto; padding-top:1.3em; min-height:7.2em; break-inside:avoid; page-break-inside:avoid; }
  .sign span { display:block; color:#64748b; font-size:.82em; }
  .thanks { text-align:center; margin-top:1em; padding-top:.6em; border-top:1px solid #cbd5e1; color:#0c4a6e; font-weight:700; break-inside:avoid; page-break-inside:avoid; }
  .thanks small { display:block; color:#64748b; font-weight:400; font-size:.8em; margin-top:.15em; }
  @page { size:A4 portrait; margin:10mm; }
  @media print {
    html, body { background:#fff; }
    body { padding:0; }
    .toolbar, .notice { display:none !important; }
    .viewport { overflow:visible; }
    .sheet { width:auto; min-height:0; margin:0; padding:0; box-shadow:none; }
    .invoice { width:190mm; max-width:100%; }
    .qrbox { display:block !important; break-inside:avoid; page-break-inside:avoid; }
    .qrbox svg { display:block !important; visibility:visible !important; }
    .qrbox svg rect { fill:#fff !important; }
    .qrbox svg path { fill:#000 !important; }
  }
"""

# Chờ QR tải xong rồi mới in; chỉ thu nhỏ (--fit) khi hóa đơn cao hơn vùng in của một trang A4.
# Phép đo làm trên chính khối .invoice rộng 190 mm - cũng là bố cục lúc in - và trang in không
# thêm bước co nào khác, nên hóa đơn không bị thu nhỏ hai lần.
INVOICE_JS = """
(function () {
  var PAGE_MM = 277;      // chiều cao vùng in: A4 297 mm trừ 2 lề 10 mm
  var SAFE_MM = 3;        // chừa lại để sai số làm tròn không đẩy sang trang 2
  var MIN_FIT = 0.66;     // nhỏ hơn mức này thì để hóa đơn sang trang thay vì chữ quá nhỏ
  var inv = document.getElementById('invoice');
  var qr = document.getElementById('qr-img');
  var box = document.getElementById('qr-box');
  var btn = document.getElementById('print-btn');
  var notice = document.getElementById('notice');
  var hint = document.getElementById('fit-hint');
  var qrServerError = box && box.getAttribute('data-error') === '1';

  function pxPerMm() {
    var probe = document.createElement('div');
    probe.style.cssText = 'position:absolute;visibility:hidden;height:100mm;width:1px;';
    document.body.appendChild(probe);
    var v = probe.getBoundingClientRect().height / 100;
    document.body.removeChild(probe);
    return v || 96 / 25.4;
  }
  function heightAt(fit) {
    inv.style.setProperty('--fit', fit);
    return inv.getBoundingClientRect().height;
  }
  function fitInvoice() {
    var limit = (PAGE_MM - SAFE_MM) * pxPerMm();
    var fit = 1, pages = 1;
    if (heightAt(1) > limit) {
      if (heightAt(MIN_FIT) > limit) {
        fit = 1;                       // quá dài: giữ cỡ chữ gốc, in nhiều trang
        pages = Math.ceil(heightAt(1) / (PAGE_MM * pxPerMm()));
      } else {
        var lo = MIN_FIT, hi = 1;
        for (var i = 0; i < 12; i++) {
          var mid = (lo + hi) / 2;
          if (heightAt(mid) <= limit) lo = mid; else hi = mid;
        }
        fit = Math.floor(lo * 1000) / 1000;
      }
    }
    inv.style.setProperty('--fit', fit);
    inv.setAttribute('data-fit', fit);
    inv.setAttribute('data-pages', pages);
    if (hint) {
      hint.textContent = pages > 1 ? 'Hóa đơn dài: in khoảng ' + pages + ' trang A4 (giữ cỡ chữ gốc).'
        : fit < 1 ? 'Đã thu nhỏ còn ' + Math.round(fit * 100) + '% để vừa 1 trang A4.'
        : 'Vừa 1 trang A4, cỡ chữ gốc.';
    }
    return fit;
  }

  // 'ok' | 'none' (hóa đơn không cần QR) | 'error' | 'timeout'
  function waitForQr(timeoutMs) {
    return new Promise(function (resolve) {
      if (!box) return resolve('none');
      if (qrServerError || !qr) return resolve('error');
      if (qr.namespaceURI === 'http://www.w3.org/2000/svg') {
        return resolve(qr.querySelector('path') ? 'ok' : 'error');
      }
      function check() {
        if (!qr.complete) return false;
        resolve(qr.naturalWidth > 0 ? 'ok' : 'error');
        return true;
      }
      if (check()) return;
      var timer = setTimeout(function () { resolve('timeout'); }, timeoutMs);
      qr.addEventListener('load', function () { clearTimeout(timer); resolve(qr.naturalWidth > 0 ? 'ok' : 'error'); });
      qr.addEventListener('error', function () { clearTimeout(timer); resolve('error'); });
    });
  }
  function showQrProblem(state) {
    if (box) box.classList.add('failed');
    document.getElementById('notice-text').textContent = state === 'timeout'
      ? 'Mã QR chuyển khoản tải quá lâu nên chưa in. Hãy tải lại hóa đơn rồi in lại.'
      : 'Không tải được mã QR chuyển khoản nên chưa in. Hãy tải lại hóa đơn rồi in lại.';
    notice.classList.add('show');
    fitInvoice();
  }
  function doPrint() {
    fitInvoice();
    window.print();
  }
  function printInvoice(force) {
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Đang chờ mã QR...';
    waitForQr(8000).then(function (state) {
      btn.disabled = false;
      btn.textContent = label;
      if (state === 'ok' || state === 'none') { notice.classList.remove('show'); doPrint(); }
      else showQrProblem(state);
    });
  }
  window.printInvoice = printInvoice;

  btn.addEventListener('click', function () { printInvoice(false); });
  document.getElementById('reload-btn').addEventListener('click', function () { location.reload(); });
  window.addEventListener('beforeprint', fitInvoice);          // kể cả khi bấm Ctrl+P
  window.addEventListener('pageshow', function (e) { if (e.persisted) location.reload(); });

  fitInvoice();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitInvoice);
  window.addEventListener('load', fitInvoice);
  waitForQr(8000).then(function (state) {
    if (state === 'error' || state === 'timeout') showQrProblem(state); else fitInvoice();
  });
})();
"""


@app.route("/orders/<string:id>/invoice")
@login_required
def order_invoice(id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (id,)).fetchone()
    if not order:
        conn.close()
        return redirect(url_for("orders"))
    if order["status"] == "Đã hủy":
        conn.close()
        return redirect(url_for("order_detail", id=id, error="Đơn hàng đã hủy nên không có hóa đơn."))

    totals = calculate_order_totals(conn, id)
    conn.commit()
    order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (id,)).fetchone()
    lines = conn.execute("""
        SELECT oi.quantity, COALESCE(oi.canceled_quantity, 0) AS canceled_quantity,
               oi.price, oi.item_discount, items.name
        FROM order_details oi JOIN items ON items.item_id = oi.item_id
        WHERE oi.order_id = ?
        ORDER BY oi.ordered_at, oi.order_detail_id
    """, (id,)).fetchall()
    promo = None
    if order["promotion_id"]:
        promo = conn.execute("SELECT name, discount FROM promotions WHERE promotion_id = ?",
                             (order["promotion_id"],)).fetchone()
    customer = None
    if order["customer_id"]:
        customer = conn.execute("SELECT full_name, phone FROM customers WHERE customer_id = ?",
                                (order["customer_id"],)).fetchone()
    conn.close()

    rows = ""
    stt = 0
    item_discount = 0.0
    for ln in lines:
        qty = int(ln["quantity"] or 0) - int(ln["canceled_quantity"] or 0)
        if qty <= 0:
            continue
        stt += 1
        line_total = qty * float(ln["price"])
        disc = float(ln["item_discount"] or 0)
        item_discount += disc
        disc_html = f'<div class="sub">Khuyến mại món: -{disc:,.0f} đ</div>' if disc > 0 else ""
        rows += f"""<tr><td class="c">{stt}</td><td>{escape(ln["name"])}{disc_html}</td>
            <td class="c">{qty}</td><td class="r">{float(ln["price"]):,.0f}</td><td class="r">{line_total:,.0f}</td></tr>"""
    if not rows:
        rows = '<tr><td colspan="5" class="c" style="color:#94a3b8;padding:1.4em;">Đơn chưa có món nào.</td></tr>'

    subtotal = float(totals["subtotal"])
    member_discount = float(totals.get("member_discount", 0))
    total_discount = float(totals["total_discount"])
    order_discount = max(0.0, total_discount - item_discount - member_discount)
    final_total = float(totals["final_total"])
    # Số tiền phải trả, làm tròn đúng như cách hiển thị {final_total:,.0f}; QR dùng chính số này.
    pay_amount = int(f"{final_total:.0f}")

    discount_lines = ""
    if item_discount > 0:
        discount_lines += f'<div class="disc"><span>Khuyến mại theo món</span><span>-{item_discount:,.0f} đ</span></div>'
    if order_discount > 0:
        promo_label = f' ({escape(promo["name"])})' if promo else ""
        discount_lines += f'<div class="disc"><span>Khuyến mại hóa đơn{promo_label}</span><span>-{order_discount:,.0f} đ</span></div>'
    if member_discount > 0:
        discount_lines += f'<div class="disc"><span>Ưu đãi hạng {escape(order["member_tier_name"] or "thành viên")}</span><span>-{member_discount:,.0f} đ</span></div>'

    done = order["status"] == "Đã hoàn thành"
    stamp = ('<span class="stamp paid">ĐÃ HOÀN THÀNH</span>' if done
             else '<span class="stamp draft">PHIẾU TẠM TÍNH</span>')
    digits = "".join(ch for ch in order["order_id"] if ch.isdigit()) or order["order_id"]
    invoice_no = f"HD{digits}"
    user = get_current_user()
    cust_name = customer["full_name"] if customer else order["customer_name"]
    cust_extra = ""
    if customer:
        phone = str(customer["phone"] or "")
        masked = phone[:3] + "****" + phone[-3:] if len(phone) >= 7 else phone
        cust_extra = f' · {escape(order["member_tier_name"] or "Thành viên")} · {escape(masked)}'
    special = (f'<div class="note"><b>Yêu cầu đặc biệt:</b> {escape(order["special_request"])}</div>'
               if order["special_request"] else "")
    info = RESTAURANT_INFO

    # QR chuyển khoản: kèm trên hóa đơn có số tiền thanh toán, kể cả đơn đã hoàn thành.
    # Việc khách quét QR không làm thay đổi trạng thái đơn - thu ngân vẫn tự xác nhận như cũ.
    qr_block = ""
    if pay_amount > 0:
        pay = PAYMENT_INFO
        memo_code = "".join(ch for ch in invoice_no if ch.isascii() and ch.isalnum())
        memo = f"Thanh toan {memo_code}"
        try:
            payload = vietqr_payload(pay["bank_bin"], pay["account_no"], pay_amount, memo)
            # Nhúng SVG trực tiếp để hiển thị và in không cần tải ảnh hoặc kết nối mạng.
            qr_img = qr_svg(payload).replace(
                '<svg ', f'<svg id="qr-img" role="img" aria-label="Mã QR chuyển khoản {pay_amount:,} đ" ', 1)
            qr_error = "0"
        except Exception as exc:        # không để lỗi tạo QR làm hỏng cả hóa đơn
            print(f"Không tạo được QR cho hóa đơn {invoice_no}: {exc}")
            qr_img = ""
            qr_error = "1"
        qr_block = f"""<div class="qrbox{' failed' if qr_error == '1' else ''}" id="qr-box" data-error="{qr_error}" data-amount="{pay_amount}" data-memo="{escape(memo)}">
        <div class="qr-cap">Quét mã để chuyển khoản</div>
        {qr_img}<div class="qr-fail">Không tải được mã QR. Vui lòng chuyển khoản theo thông tin bên dưới.</div>
        <div class="bank">
          <div class="shop">{escape(pay["display_name"])}</div>
          <div>Ngân hàng: <b>{escape(pay["bank_name"])}</b></div>
          <div>Số tài khoản: <span class="acc">{escape(pay["account_no"])}</span></div>
          <div>Chủ tài khoản: <b>{escape(pay["account_name"])}</b></div>
          <div class="memo">Số tiền: <b>{pay_amount:,} đ</b><br>Nội dung: <b>{escape(memo)}</b></div>
        </div>
      </div>"""

    html = f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hóa đơn {escape(invoice_no)} - {escape(info["name"])}</title>
<style>{INVOICE_CSS}</style></head>
<body>
  <div class="toolbar">
    <button class="btn primary" id="print-btn" type="button">In hóa đơn</button>
    <a class="btn" href="/orders/{escape(order["order_id"])}">← Quay lại đơn #{escape(order["order_id"])}</a>
    <a class="btn" href="/orders">Danh sách đơn hàng</a>
    <span class="hint" id="fit-hint"></span>
  </div>
  <div class="notice" id="notice" role="alert">
    <span id="notice-text"></span>
    <button class="btn" id="reload-btn" type="button">↻ Tải lại hóa đơn</button>
  </div>
  <div class="viewport"><div class="sheet">
  <div class="invoice" id="invoice">
    <div class="head">
      <div class="brand"><h1>{escape(info["name"])}</h1>
        <p>{escape(info["address"])}</p><p>{escape(info["phone"])} &nbsp;|&nbsp; {escape(info["tax_code"])}</p></div>
      <div class="doc">
        <div class="title">HÓA ĐƠN BÁN HÀNG</div>
        <div class="subtitle">Số: <b>{escape(invoice_no)}</b></div>
        {stamp}
      </div>
    </div>
    <div class="meta">
      <div><b>Mã đơn:</b> #{escape(order["order_id"])}</div>
      <div><b>Ngày giờ:</b> {escape(order["order_date"])}</div>
      <div><b>Bàn:</b> {escape(order["table_number"] or "—")}</div>
      <div><b>Thu ngân:</b> {escape(user["full_name"] if user else "—")}</div>
      <div style="grid-column:1/-1;"><b>Khách hàng:</b> {escape(cust_name)}{cust_extra}</div>
    </div>
    <table>
      <thead><tr><th class="c" style="width:3.4em;">STT</th><th>Tên món</th><th class="c" style="width:3.6em;">SL</th><th class="r" style="width:8em;">Đơn giá</th><th class="r" style="width:9em;">Thành tiền</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <div class="pay{'' if qr_block else ' noqr'}">
      {qr_block or '<div></div>'}
      <div class="paycol">
        <div class="totals">
          <div><span>Tạm tính</span><span>{subtotal:,.0f} đ</span></div>
          {discount_lines}
          <div class="grand"><span>TỔNG THANH TOÁN</span><span>{pay_amount:,} đ</span></div>
        </div>
        <div class="words"><b>Bằng chữ:</b> <i>{escape(vnd_in_words(final_total))}</i></div>
        {special}
        <div class="sign"><div><b>Khách hàng</b><span>(Ký, ghi rõ họ tên)</span></div><div><b>Người bán hàng</b><span>(Ký, ghi rõ họ tên)</span></div></div>
      </div>
    </div>
    <div class="thanks">Cảm ơn quý khách – Hẹn gặp lại!<small>In lúc {datetime.now().strftime("%d/%m/%Y %H:%M")}</small></div>
  </div>
  </div></div>
<script>{INVOICE_JS}</script>
</body></html>"""
    # Không lưu đệm: mỗi lần mở lại hóa đơn đều tính lại tiền và tạo lại QR theo số mới.
    response = app.make_response(html)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# =========================================================
# TẠO ĐƠN
# =========================================================

@app.route("/orders/new")
@role_required("admin", "cashier", "waiter")
def new_order():
    conn = get_db()
    customers = conn.execute("SELECT c.customer_id, c.full_name, c.phone, t.tier_name, t.discount_percent FROM customers c JOIN member_tiers t ON t.member_tier_id=c.member_tier_id WHERE c.active=1 ORDER BY c.full_name").fetchall()
    items = conn.execute("""
        SELECT items.item_id, items.name, items.price, items.category_id, categories.name AS category_name
        FROM items
        JOIN categories ON items.category_id = categories.category_id
        WHERE items.status = 'Đang bán'
        ORDER BY categories.name, items.name
    """).fetchall()
    best, best_label, rank_map, blocked_ids, sale_map = get_order_screen_context(conn)
    conn.close()
    customer_options = '<option value="">Khách lẻ (không áp dụng hạng)</option>' + ''.join(
        f'<option value="{c["customer_id"]}">{escape(c["full_name"])} | {escape(c["tier_name"])} (-{c["discount_percent"]:g}%)</option>'
        for c in customers)

    rows = ""


    body = f"""
    <div class="header">
        <div><h1>Tạo đơn hàng</h1></div>
    </div>
    {f'<div class="message">{escape(request.args.get("error", ""))}</div>' if request.args.get("error") else ""}
    <form method="POST" action="/orders/add">
        <div class="panel">
            <h2>Thông tin khách hàng</h2>
            <div class="form-group">
                <label>Khách hàng thành viên</label>
                <select id="order-customer" name="customer_id">{customer_options}</select>
            </div>
            <div class="form-group" style="margin-top:12px;">
                <label>Tên khách lẻ</label>
                <input id="customer-name" name="customer_name" placeholder="Nhập tên khách lẻ">
            </div>
            <div class="form-grid" style="grid-template-columns: 1fr 2fr; margin-top:12px;">
                <div class="form-group"><label>Số bàn</label><input name="table_number" placeholder="Ví dụ: Bàn 05" required></div>
                <div class="form-group"><label>Yêu cầu đặc biệt</label><input name="special_request" placeholder="Ít cay, không hành..."></div>
            </div>
        </div>
        <div class="panel">
            <h2>Chọn món</h2>
            {best_seller_panel_html(best, best_label, blocked_ids, sale_map)}
            {order_picker_html(items, rank_map, blocked_ids, sale_map)}
        </div>
        <button class="btn btn-primary">Tạo đơn</button>
        <a class="btn btn-secondary" href="/orders">Hủy</a>
    </form>
    <script>
    (function () {{
        var member = document.getElementById('order-customer');
        var guest = document.getElementById('customer-name');
        function syncCustomer() {{
            var isMember = !!member.value;
            guest.disabled = isMember;
            guest.required = !isMember;
            if (isMember) guest.value = '';
            guest.placeholder = isMember ? 'Đã chọn khách hàng thành viên' : 'Nhập tên khách lẻ';
        }}
        member.addEventListener('change', syncCustomer);
        window.addEventListener('pageshow', syncCustomer);
        syncCustomer();
    }})();
    </script>
    """
    return render_page("Tạo đơn hàng", body, "orders")


@app.route("/orders/add", methods=["POST"])
@role_required("admin", "cashier", "waiter")
def add_order():
    customer = request.form.get("customer_name", "").strip()
    table_number = request.form.get("table_number", "").strip()
    special_request = request.form.get("special_request", "").strip()
    customer_id = request.form.get("customer_id", "").strip() or None
    if not table_number:
        return redirect(url_for("new_order", error="Vui lòng nhập số bàn."))

    conn = get_db()
    items = conn.execute("SELECT item_id, price FROM items WHERE status = 'Đang bán'").fetchall()

    selected = []
    total = 0
    for item in items:
        try:
            quantity = int(request.form.get(f"qty_{item['item_id']}", 0))
        except ValueError:
            quantity = 0
        if quantity > 0:
            total += quantity * item["price"]
            selected.append((item["item_id"], quantity, item["price"]))

    if not selected:
        conn.close()
        return redirect(url_for("orders"))

    stock_error = unavailable_selection_error(conn, selected)
    if stock_error:
        conn.close()
        return redirect(url_for("new_order", error=stock_error))

    if customer_id:
        registered = conn.execute("SELECT full_name FROM customers WHERE customer_id=? AND active=1", (customer_id,)).fetchone()
        if not registered:
            conn.close()
            return redirect(url_for("new_order"))
        # Lấy tên chuẩn từ hồ sơ thành viên, không cần nhập số điện thoại khi đặt món.
        customer = registered["full_name"]
    if not customer:
        conn.close()
        return redirect(url_for("new_order", error="Vui lòng nhập tên khách lẻ hoặc chọn thành viên."))
    order_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    order_id = next_code(conn, "orders", "DH")
    conn.execute("""
        INSERT INTO orders(order_id, customer_name, customer_id, order_date, status, total, final_total, table_number, special_request)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (order_id, customer, customer_id, order_date, "Chờ xác nhận", total, total, table_number, special_request))

    for item_id, qty, price in selected:
        conn.execute("""INSERT INTO order_details(order_detail_id, order_id, item_id, quantity, price, ordered_at)
            VALUES(?, ?, ?, ?, ?, ?)""",
            (next_code(conn, "order_details", "CTDH"), order_id, item_id, qty, price, order_date))

    calculate_order_totals(conn, order_id)

    conn.commit()
    conn.close()
    return redirect(url_for("order_detail", id=order_id, ok=f"Tạo đơn hàng #{order_id} thành công. Đơn đang chờ bếp xác nhận."))


# =========================================================
# CHI TIẾT ĐƠN
# =========================================================

@app.route("/orders/<string:id>")
@login_required
def order_detail(id):
    conn = get_db()

    order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (id,)).fetchone()
    if not order:
        conn.close()
        return redirect(url_for("orders"))

    totals = calculate_order_totals(conn, id)
    conn.commit()

    details = conn.execute("""
        SELECT oi.order_detail_id, oi.quantity, COALESCE(oi.canceled_quantity, 0) AS canceled_quantity,
               oi.price, oi.item_discount, oi.ordered_at, items.name, items.category_id
        FROM order_details oi
        JOIN items ON oi.item_id = items.item_id
        WHERE oi.order_id = ?
    """, (id,)).fetchall()

    kitchen_alerts_here = get_kitchen_alerts(conn, id)
    conn.close()

    user = get_current_user()
    role = user["role"] if user else None
    kitchen_panel = kitchen_alert_html(kitchen_alerts_here) if has_perm(user, "orders_kitchen") else ""

    rows = ""
    for detail in details:
        active_qty = int(detail["quantity"] or 0) - int(detail["canceled_quantity"] or 0)
        subtotal = max(0, active_qty) * detail["price"]
        ordered_at = detail["ordered_at"] or order["order_date"]
        cancel_action = '<span class="muted">Không thể hủy</span>'
        try:
            line_deadline = datetime.strptime(ordered_at, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=10)
            if active_qty > 0 and datetime.now() <= line_deadline and order["status"] not in ("Đã hoàn thành", "Đã hủy") and has_perm(user, "orders_create"):
                cancel_action = f"""
                <form method="POST" action="/orders/{id}/items/{detail['order_detail_id']}/cancel" class="actions">
                  <input type="number" name="quantity" min="1" max="{active_qty}" value="{active_qty}" required style="width:75px;padding:6px;">
                  <button class="btn btn-danger">Hủy số lượng</button>
                </form>"""
        except (ValueError, TypeError):
            pass
        rows += f"""
        <tr>
            <td><div style="display:flex;gap:12px;align-items:center;">{food_thumb_html(detail["category_id"], detail["name"])}
                <div><span class="food-name">{escape(detail["name"])}</span><br><span class="muted">Đặt lúc: {escape(ordered_at)}</span></div></div></td>
            <td>{detail["price"]:,.0f} đ</td>
            <td>{max(0, active_qty)}{f' (đã hủy {detail["canceled_quantity"]})' if detail["canceled_quantity"] else ''}</td>
            <td>{subtotal:,.0f} đ</td>
            <td style="color:#dc2626;">-{detail["item_discount"]:,.0f} đ</td>
            <td>{cancel_action}</td>
        </tr>
        """

    error = request.args.get("error", "")
    message = f'<div class="message">{escape(error)}</div>' if error else ""

    # Xác nhận đơn, bắt đầu chuẩn bị, hoàn thành: chỉ bếp (và quản lý)
    actions = ""
    if has_perm(user, "orders_kitchen"):
        if order["status"] == "Chờ xác nhận":
            actions = f"""
            <form method="POST" action="/orders/{id}/confirm">
                <button class="btn btn-primary">Xác nhận đơn & trừ kho</button>
            </form>
            """
        elif order["status"] == "Đã xác nhận":
            actions = f"""
            <form method="POST" action="/orders/{id}/status">
                <input type="hidden" name="status" value="Đang chuẩn bị">
                <button class="btn btn-primary">Bắt đầu chuẩn bị</button>
            </form>
            """

    if has_perm(user, "orders_kitchen"):
        if order["status"] == "Đang chuẩn bị":
            actions += f"""
            <form method="POST" action="/orders/{id}/status">
                <input type="hidden" name="status" value="Đã hoàn thành">
                <button class="btn btn-primary">Hoàn thành đơn</button>
            </form>
            """

    if not actions:
        if order["status"] in ("Đã hoàn thành", "Đã hủy"):
            actions = '<span class="muted">Đơn hàng đã kết thúc.</span>'
        else:
            actions = '<span class="muted">Không có hành động nào khả dụng với vai trò của bạn.</span>'

    add_items_btn = ""
    if has_perm(user, "orders_create") and order["status"] not in ("Đã hoàn thành", "Đã hủy"):
        add_items_btn = f'<a class="btn btn-warning" href="/orders/{id}/add-items">+ Gọi thêm món</a>'

    cancel_section = ""
    if has_perm(user, "orders_create"):
        if can_customer_cancel(order):
            remaining_minutes = max(0, int((
                cancel_deadline(order["order_date"]) - datetime.now()
            ).total_seconds() / 60) + 1)
            cancel_section = f"""
            <div class="notice">
                Khách hàng còn khoảng <strong>{remaining_minutes} phút</strong> để hủy đơn.
            </div>
            <form method="POST" action="/orders/{id}/cancel"
                  onsubmit="return confirm('Khách hàng có chắc muốn hủy đơn?');">
                <button class="btn btn-danger">Khách hàng hủy đơn</button>
            </form>
            """
        elif order["status"] not in ("Đã hoàn thành", "Đã hủy"):
            cancel_section = '<div class="message">Đã quá 10 phút kể từ khi đặt món, khách hàng không thể hủy đơn.</div>'
        else:
            cancel_section = ""
    else:
        cancel_section = '<span class="muted">Chỉ phục vụ/thu ngân/quản lý mới có quyền hủy đơn.</span>'

    promo_html = ""
    if totals and totals["order_promo"]:
        promo_html += f"""
        <p><strong>Khuyến mãi toàn đơn:</strong> {escape(totals["order_promo"]["name"])}
        (Giảm {totals["order_promo"]["discount"]:g}%)</p>
        """
    if totals and totals["item_discount"] > 0:
        promo_html += f"""
        <p><strong>Khuyến mãi theo món:</strong> Đã giảm {totals["item_discount"]:,.0f} đ</p>
        """
    if not promo_html:
        promo_html = "<p class='muted'>Không có chương trình khuyến mãi nào được áp dụng.</p>"

    invoice_btn = ("" if order["status"] == "Đã hủy" else
                   f'<a class="btn btn-secondary" href="/orders/{id}/invoice" target="_blank">Xem / In hóa đơn</a>')

    body = f"""
    <div class="header">
        <div><h1>Chi tiết đơn #{order["order_id"]}</h1></div>
    </div>
    {message}
    {kitchen_panel}
    <div class="panel">
        <h2>Thông tin đơn</h2>
        <p><strong>Khách hàng:</strong> {escape(order["customer_name"])}</p>
        <br>
        <p><strong>Số bàn:</strong> {escape(order["table_number"] or "Chưa nhập")}</p>
        <br>
        <p><strong>Yêu cầu đặc biệt:</strong> {escape(order["special_request"] or "Không có")}</p>
        <br>
        <p><strong>Thời gian:</strong> {order["order_date"]}</p>
        <br>
        <p><strong>Trạng thái:</strong>
            <span class="status {css_status(order["status"])}">{escape(order["status"])}</span>
        </p>
    </div>
    <div class="panel">
        <h2>Món đã đặt</h2>
        <table>
            <thead>
                <tr><th>Món ăn</th><th>Đơn giá</th><th>Số lượng</th><th>Thành tiền</th><th>Giảm giá món</th><th>Hủy món (trong 10 phút)</th></tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        <div style="margin-top: 20px; text-align: right;">
            <p>Tạm tính: <strong>{totals["subtotal"]:,.0f} đ</strong></p>
            <p>Giảm giá món: <strong style="color:#dc2626;">-{totals["item_discount"]:,.0f} đ</strong></p>
            <p>Giảm giá đơn: <strong style="color:#dc2626;">-{totals["order_discount"]:,.0f} đ</strong></p>
            <p>Giảm giá hạng thành viên ({escape(order["member_tier_name"] or "Khách lẻ")}): <strong style="color:#dc2626;">-{totals.get("member_discount", 0):,.0f} đ</strong></p>
            <hr style="margin: 10px 0; border: 0; border-top: 1px solid #e5e7eb;">
            <p style="font-size: 20px;">Thành tiền: <strong style="color:#2563eb;">{totals["final_total"]:,.0f} đ</strong></p>
        </div>
    </div>
    <div class="panel">
        <h2>Thông tin khuyến mãi</h2>
        {promo_html}
    </div>
    <div class="panel">
        <h2>Cập nhật trạng thái</h2>
        <div class="actions">{actions} {add_items_btn} {invoice_btn}</div>
    </div>
    <div class="panel">
        <h2>Quyền hủy của khách hàng</h2>
        {cancel_section}
    </div>
    <a class="btn btn-secondary" href="/orders">← Quay lại</a>
    """
    return render_page(f"Chi tiết đơn #{id}", body, "orders")


# =========================================================
# GỌI THÊM MÓN
# =========================================================

@app.route("/orders/<string:id>/add-items")
@role_required("admin", "cashier", "waiter")
def add_items_to_order(id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (id,)).fetchone()

    if not order or order["status"] in ("Đã hoàn thành", "Đã hủy"):
        conn.close()
        return redirect(url_for("order_detail", id=id))

    items = conn.execute("""
        SELECT items.item_id, items.name, items.price, items.category_id, categories.name AS category_name
        FROM items
        JOIN categories ON items.category_id = categories.category_id
        WHERE items.status = 'Đang bán'
        ORDER BY categories.name, items.name
    """).fetchall()
    best, best_label, rank_map, blocked_ids, sale_map = get_order_screen_context(conn)
    conn.close()

    rows = ""
    for item in items:
        rows += f"""
        <tr>
            <td>{food_thumb_html(item["category_id"], item["name"])}</td>
            <td>{escape(item["category_name"])}</td>
            <td>{escape(item["name"])}{item_flags_html(item["item_id"], rank_map, blocked_ids, sale_map)}</td>
            <td>{item["price"]:,.0f} đ</td>
            <td><input type="number" name="qty_{item["item_id"]}" value="0" min="0" step="1" style="width:100px" {'disabled title="Món tạm ngừng đặt do không đủ nguyên liệu còn hạn"' if item["item_id"] in blocked_ids else ""}></td>
        </tr>
        """

    body = f"""
    <div class="header">
        <div><h1>Gọi thêm món - Đơn #{id}</h1></div>
    </div>
    <form method="POST" action="/orders/{id}/add-items">
        {best_seller_panel_html(best, best_label, blocked_ids, sale_map)}
        <div class="panel">
            <h2>Danh sách món</h2>
            <table>
                <thead><tr><th>Hình</th><th>Danh mục</th><th>Món ăn</th><th>Đơn giá</th><th>Số lượng thêm</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
        <button class="btn btn-primary">Thêm vào đơn</button>
        <a class="btn btn-secondary" href="/orders/{id}">Hủy</a>
    </form>
    """
    return render_page(f"Gọi thêm món #{id}", body, "orders")


@app.route("/orders/<string:id>/add-items", methods=["POST"])
@role_required("admin", "cashier", "waiter")
def process_add_items(id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (id,)).fetchone()
    if not order or order["status"] in ("Đã hoàn thành", "Đã hủy"):
        conn.close()
        return redirect(url_for("order_detail", id=id))

    available_items = conn.execute("SELECT item_id, price FROM items WHERE status = 'Đang bán'").fetchall()
    selected = []
    for item in available_items:
        try:
            qty = int(request.form.get(f"qty_{item['item_id']}", 0))
        except (ValueError, TypeError):
            qty = 0
        if qty < 0:
            conn.close()
            return redirect(url_for("order_detail", id=id, error="Số lượng gọi thêm không hợp lệ."))
        if qty > 0:
            selected.append((item["item_id"], qty, item["price"]))
    if not selected:
        conn.close()
        return redirect(url_for("order_detail", id=id, error="Bạn chưa chọn món cần gọi thêm."))

    stock_error = unavailable_selection_error(conn, selected)
    if stock_error:
        conn.close()
        return redirect(url_for("order_detail", id=id, error=stock_error))

    try:
        conn.begin()
        requirements = {}
        check_changes = []
        if order["stock_deducted"]:
            for item_id, qty, price in selected:
                recipe_rows = conn.execute("""
                    SELECT r.ingredient_id, r.quantity AS recipe_qty, i.name, i.unit, i.quantity AS stock
                    FROM recipes r JOIN ingredients i ON i.ingredient_id = r.ingredient_id
                    WHERE r.item_id = ?
                    FOR UPDATE
                """, (item_id,)).fetchall()
                if not recipe_rows:
                    raise ValueError("Có món chưa được thiết lập công thức nên không thể gọi thêm khi đơn đã trừ kho.")
                for r in recipe_rows:
                    ing_id = r["ingredient_id"]
                    if ing_id not in requirements:
                        requirements[ing_id] = {"name": r["name"], "unit": r["unit"], "stock": r["stock"], "required": 0}
                    requirements[ing_id]["required"] += float(r["recipe_qty"]) * qty
            shortages = []
            for ing_id, req in requirements.items():
                usable = usable_stock_for_cooking(conn, ing_id, req["stock"])
                if req["required"] > usable + 1e-9:
                    shortages.append(f'{req["name"]}: cần {req["required"]:g} {req["unit"]}, '
                                     f'còn dùng được {usable:g} {req["unit"]}')
            if shortages:
                raise ValueError("Không đủ nguyên liệu để gọi thêm: " + "; ".join(shortages))
            for ing_id, req in requirements.items():
                before = float(req["stock"])
                after = before - req["required"]
                check_changes.append({"id": ing_id, "type": "Xuất", "amount": req["required"],
                                      "before": before, "after": after})
                conn.execute("UPDATE ingredients SET quantity = ? WHERE ingredient_id = ?", (after, ing_id))
                batch_take(conn, ing_id, req["required"], valid_first=True)
                batch_sync(conn, ing_id)
                conn.execute("""INSERT INTO inventory_movements
                    (ingredient_id, movement_type, quantity, before_quantity, after_quantity, note, created_at)
                    VALUES (?, 'Xuất', ?, ?, ?, ?, ?)""",
                    (ing_id, req["required"], before, after, f"Gọi thêm món trong đơn #{id}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for item_id, qty, price in selected:
            conn.execute("""INSERT INTO order_details(order_detail_id, order_id, item_id, quantity, price, ordered_at, kitchen_ack)
                VALUES (?, ?, ?, ?, ?, ?, 0)""", (next_code(conn, "order_details", "CTDH"), id, item_id, qty, price, now_str))
        calculate_order_totals(conn, id)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        return redirect(url_for("order_detail", id=id, error=str(exc) or "Không thể gọi thêm món."))
    conn.close()
    push_inventory_check(f"Kiểm tra tồn kho sau khi gọi thêm món cho đơn #{id}", check_changes)
    return redirect(url_for("order_detail", id=id, ok=f"Đã gọi thêm món vào đơn #{id} thành công."))


# =========================================================
# XÁC NHẬN ĐƠN
# =========================================================

@app.route("/orders/<string:id>/confirm", methods=["POST"])
@role_required("admin", "chef")
def confirm_order(id):
    try:
        changes = confirm_order_stock(id)
    except ValueError as error:
        return redirect(url_for("order_detail", id=id, error=str(error)))
    push_inventory_check(f"Kiểm tra tồn kho sau khi xuất nguyên liệu cho đơn #{id}", changes)
    return redirect(url_for("order_detail", id=id, ok=f"Đã xác nhận đơn #{id} và trừ kho nguyên liệu thành công."))


# =========================================================
# CẬP NHẬT TRẠNG THÁI
# =========================================================

@app.route("/orders/<string:id>/status", methods=["POST"])
@role_required("admin", "chef")
def update_order_status(id):
    new_status = request.form.get("status", "")
    user = get_current_user()
    role = user["role"]

    conn = get_db()
    order = conn.execute("SELECT status FROM orders WHERE order_id = ?", (id,)).fetchone()

    allowed_transitions = {
        "Đã xác nhận": "Đang chuẩn bị",
        "Đang chuẩn bị": "Đã hoàn thành"
    }

    # Chỉ bếp (chef) và quản lý (admin) được cập nhật trạng thái:
    # Đã xác nhận -> Đang chuẩn bị -> Đã hoàn thành
    current = order["status"] if order else None
    can_do = False

    if current == "Đã xác nhận" and new_status == "Đang chuẩn bị" and has_perm(user, "orders_kitchen"):
        can_do = True
    elif current == "Đang chuẩn bị" and new_status == "Đã hoàn thành" and has_perm(user, "orders_kitchen"):
        can_do = True

    if order and can_do:
        conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", (new_status, id))
        if new_status == "Đã hoàn thành":
            completed = conn.execute("SELECT customer_id, final_total, member_accounted FROM orders WHERE order_id=?", (id,)).fetchone()
            # Chỉ ghi nhận một lần; đơn không có thành viên không làm thay đổi hồ sơ khách hàng.
            if completed and completed["customer_id"] and not completed["member_accounted"]:
                spent_amount = max(0, float(completed["final_total"] or 0))
                conn.execute("""UPDATE customers
                    SET total_spent = total_spent + ?, total_orders = total_orders + 1
                    WHERE customer_id=?""", (spent_amount, completed["customer_id"]))
                spent = float(conn.execute("SELECT total_spent FROM customers WHERE customer_id=?", (completed["customer_id"],)).fetchone()["total_spent"] or 0)
                member_tier_id = tier_for_spent(conn, spent)
                conn.execute("UPDATE customers SET member_tier_id=? WHERE customer_id=?", (member_tier_id, completed["customer_id"]))
                conn.execute("UPDATE orders SET member_accounted=1 WHERE order_id=?", (id,))
        conn.commit()
    conn.close()
    if order and can_do:
        return redirect(url_for("order_detail", id=id, ok=f"Đã cập nhật đơn #{id} sang trạng thái “{new_status}”."))
    return redirect(url_for("order_detail", id=id))


# =========================================================
# KHÁCH HỦY ĐƠN
# =========================================================

@app.route("/orders/<string:id>/cancel", methods=["POST"])
@role_required("admin", "cashier", "waiter")
def cancel_order(id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (id,)).fetchone()

    if not order:
        conn.close()
        return redirect(url_for("orders"))

    if not can_customer_cancel(order):
        conn.close()
        return redirect(url_for("order_detail", id=id,
            error="Đơn hàng không còn trong thời gian được phép hủy."))

    try:
        conn.begin()
        if order["stock_deducted"]:
            restore_order_stock(conn, id)

        canceled_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            UPDATE orders SET status = 'Đã hủy', canceled_at = ?, stock_deducted = 0
            WHERE order_id = ?
        """, (canceled_at, id))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        return redirect(url_for("order_detail", id=id,
            error="Không thể hoàn kho cho đơn hàng này. Hãy kiểm tra công thức món ăn."))

    conn.close()
    return redirect(url_for("order_detail", id=id, ok=f"Hủy đơn #{id} thành công." + (" Nguyên liệu đã được hoàn lại kho." if order["stock_deducted"] else "")))


@app.route("/orders/<string:id>/items/<string:line_id>/cancel", methods=["POST"])
@role_required("admin", "cashier", "waiter")
def cancel_order_item(id, line_id):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (id,)).fetchone()
    line = conn.execute("SELECT * FROM order_details WHERE order_detail_id = ? AND order_id = ?", (line_id, id)).fetchone()
    if not order or not line:
        conn.close()
        return redirect(url_for("orders"))
    if order["status"] in ("Đã hoàn thành", "Đã hủy"):
        conn.close()
        return redirect(url_for("order_detail", id=id, error="Đơn hàng đã kết thúc, không thể hủy món."))
    ordered_at = line["ordered_at"] or order["order_date"]
    try:
        deadline = datetime.strptime(ordered_at, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=10)
    except ValueError:
        conn.close()
        return redirect(url_for("order_detail", id=id, error="Không xác định được thời điểm đặt món."))
    if datetime.now() > deadline:
        conn.close()
        return redirect(url_for("order_detail", id=id, error="Đã quá 10 phút kể từ khi đặt món này."))
    try:
        cancel_qty = int(request.form.get("quantity", 0))
    except (ValueError, TypeError):
        cancel_qty = 0
    active_qty = int(line["quantity"]) - int(line["canceled_quantity"] or 0)
    if cancel_qty <= 0 or cancel_qty > active_qty:
        conn.close()
        return redirect(url_for("order_detail", id=id, error="Số lượng hủy không hợp lệ."))

    try:
        conn.begin()
        if order["stock_deducted"]:
            recipe_rows = conn.execute("""
                SELECT r.ingredient_id, r.quantity, i.quantity AS stock
                FROM recipes r JOIN ingredients i ON i.ingredient_id = r.ingredient_id
                WHERE r.item_id = ?
                FOR UPDATE
            """, (line["item_id"],)).fetchall()
            if not recipe_rows:
                raise ValueError("Không tìm thấy công thức để hoàn kho.")
            for recipe in recipe_rows:
                amount = float(recipe["quantity"]) * cancel_qty
                before = float(recipe["stock"])
                after = before + amount
                conn.execute("UPDATE ingredients SET quantity = ? WHERE ingredient_id = ?", (after, recipe["ingredient_id"]))
                batch_restore(conn, recipe["ingredient_id"], amount)
                batch_sync(conn, recipe["ingredient_id"])
                conn.execute("""INSERT INTO inventory_movements
                    (ingredient_id, movement_type, quantity, before_quantity, after_quantity, note, created_at)
                    VALUES (?, 'Hoàn kho', ?, ?, ?, ?, ?)""",
                    (recipe["ingredient_id"], amount, before, after, f"Hủy {cancel_qty} phần món trong đơn #{id}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.execute("UPDATE order_details SET canceled_quantity = COALESCE(canceled_quantity, 0) + ? WHERE order_detail_id = ?", (cancel_qty, line_id))
        calculate_order_totals(conn, id)
        remaining = conn.execute("SELECT COUNT(*) FROM order_details WHERE quantity > COALESCE(canceled_quantity, 0) AND order_id = ?", (id,)).fetchone()[0]
        if remaining == 0:
            conn.execute("UPDATE orders SET status = 'Đã hủy', canceled_at = ?, stock_deducted = 0 WHERE order_id = ?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), id))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        return redirect(url_for("order_detail", id=id, error=str(exc) or "Không thể hủy món."))
    item_row = conn.execute("SELECT name FROM items WHERE item_id = ?", (line["item_id"],)).fetchone()
    item_name = item_row["name"] if item_row else "món ăn"
    conn.close()
    if remaining == 0:
        msg = f"Hủy món {item_name} thành công. Đơn #{id} không còn món nào nên đã được hủy."
    elif cancel_qty == active_qty:
        msg = f"Hủy món {item_name} thành công."
    else:
        msg = f"Hủy {cancel_qty} phần {item_name} thành công."
    return redirect(url_for("order_detail", id=id, ok=msg))


# =========================================================
# PHÂN TÍCH MẶT HÀNG
# =========================================================

TIER_META = {
    "best": ("Bán chạy", "green", "#16a34a"),
    "mid": ("Trung bình", "yellow", "#f59e0b"),
    "slow": ("Bán chậm", "red", "#ef4444"),
    "none": ("Chưa bán được", "gray", "#94a3b8"),
}
CATEGORY_PALETTE = ["#0284c7", "#16a34a", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6", "#f97316", "#64748b"]


def collect_item_stats(conn, start, end, category_id="", item_id=""):
    """Số lượng bán + doanh thu của từng món (kể cả món chưa bán được), kèm hạng và mức bán."""
    sql = """
        SELECT items.item_id, items.name AS item_name, items.price, items.status AS item_status,
               items.category_id, categories.name AS category_name,
               COALESCE(SUM(CASE WHEN o.order_id IS NOT NULL THEN od.quantity - COALESCE(od.canceled_quantity, 0) END), 0) AS total_sold,
               COALESCE(SUM(CASE WHEN o.order_id IS NOT NULL THEN (od.quantity - COALESCE(od.canceled_quantity, 0)) * od.price END), 0) AS total_revenue,
               COUNT(DISTINCT CASE WHEN o.order_id IS NOT NULL AND od.quantity - COALESCE(od.canceled_quantity, 0) > 0 THEN o.order_id END) AS order_count
        FROM items
        JOIN categories ON categories.category_id = items.category_id
        LEFT JOIN order_details od ON od.item_id = items.item_id
        LEFT JOIN orders o ON o.order_id = od.order_id AND o.status != 'Đã hủy'
             AND date(o.order_date) BETWEEN ? AND ?
        WHERE 1 = 1
    """
    params = [start, end]
    if category_id:
        sql += " AND categories.category_id = ?"
        params.append(category_id)
    if item_id:
        sql += " AND items.item_id = ?"
        params.append(item_id)
    sql += " GROUP BY items.item_id, items.name, items.price, items.status, items.category_id, categories.name"
    rows = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["total_sold"] = float(d["total_sold"] or 0)
        d["total_revenue"] = float(d["total_revenue"] or 0)
        d["order_count"] = int(d["order_count"] or 0)
        if d["total_sold"] > 0 or d["item_status"] == "Đang bán":
            rows.append(d)
    rows.sort(key=lambda x: (-x["total_sold"], -x["total_revenue"], x["item_name"]))

    sellers = [r for r in rows if r["total_sold"] > 0]
    n = len(sellers)
    k = max(1, math.ceil(n * 0.3)) if n else 0
    for i, r in enumerate(sellers, 1):
        r["rank"] = i
        if i <= k:
            r["tier"] = "best"
        elif n >= 3 and i > n - k:
            r["tier"] = "slow"
        else:
            r["tier"] = "mid"
    for r in rows:
        if r["total_sold"] <= 0:
            r["rank"], r["tier"] = None, "none"
    return rows


# Đề xuất giảm giá cho món bán chậm theo số đơn trong khoảng thời gian đang xem: (tối đa n đơn, giảm %).
SLOW_DISCOUNT_RULES = [(0, 50), (3, 30), (10, 10)]


def suggest_discount(order_count):
    for limit, pct in SLOW_DISCOUNT_RULES:
        if order_count <= limit:
            return pct
    return 0


def column_chart_svg(bars, color, unit, label_color="#0f2d4b"):
    """Biểu đồ cột dọc có nhãn giá trị trên đầu cột. bars = [{label, value, note?}] (note: nhãn phụ, vd "-50%")."""
    if not bars:
        return '<p class="empty">Chưa có dữ liệu để vẽ biểu đồ.</p>'
    n = len(bars)
    has_note = any(b.get("note") for b in bars)
    W, H, L, R, T, B = 760, 340, 44, 12, (52 if has_note else 30), 104
    vmax = max(b["value"] for b in bars)
    step = max(1, math.ceil(vmax / 4)) if vmax > 0 else 1
    top = step * 4
    band = (W - L - R) / n
    bw = min(64, band * 0.62)
    out = ""
    for k in range(5):
        gy = T + (H - T - B) * (1 - k / 4)
        out += (f'<line x1="{L}" y1="{gy:.1f}" x2="{W - R}" y2="{gy:.1f}" stroke="#e2e8f0"/>'
                f'<text x="{L - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="11" fill="#64748b">{step * k:g}</text>')
    for i, b in enumerate(bars):
        v = b["value"]
        h = max(2.0, (H - T - B) * v / top)
        cx = L + band * i + band / 2
        y = H - B - h
        name = b["label"] if len(b["label"]) <= 22 else b["label"][:21].rstrip() + "…"
        out += (f'<rect x="{cx - bw / 2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="4" fill="{color}">'
                f'<title>{escape(b["label"])}: {v:g} {unit}</title></rect>'
                f'<text x="{cx:.1f}" y="{y - 7:.1f}" text-anchor="middle" font-size="13" font-weight="800" fill="{label_color}">{v:g}</text>')
        if b.get("note"):
            out += (f'<rect x="{cx - 21:.1f}" y="{y - 41:.1f}" width="42" height="18" rx="9" fill="#fef2f2" stroke="{color}"/>'
                    f'<text x="{cx:.1f}" y="{y - 28:.1f}" text-anchor="middle" font-size="11" font-weight="800" fill="{color}">{escape(b["note"])}</text>')
        out += (f'<text transform="translate({cx + 4:.1f},{H - B + 14}) rotate(-35)" text-anchor="end" font-size="11.5" '
                f'fill="#334155">{escape(name)}</text>')
    out += f'<text x="{L - 34}" y="{T - 14}" font-size="11" fill="#64748b">{unit}</text>'
    return f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;max-height:380px;" role="img">{out}</svg>'


def trend_svg(points, start, end):
    """Biểu đồ đường số phần bán theo ngày (SVG thuần, không cần thư viện ngoài)."""
    if not points:
        return '<p class="empty">Chưa có dữ liệu để vẽ biểu đồ theo ngày.</p>'
    by_day = {p[0]: p[1] for p in points}
    d0, d1 = parse_iso_date(start), parse_iso_date(end)
    series = []
    if d0 and d1 and 0 <= (d1 - d0).days <= 120:
        cur = d0
        while cur <= d1:
            series.append((cur.isoformat(), by_day.get(cur.isoformat(), 0)))
            cur += timedelta(days=1)
    else:
        series = sorted(by_day.items())
    W, H, L, R, T, B = 760, 250, 46, 16, 16, 34
    vmax = max(v for _, v in series) or 1
    n = len(series)
    xs = [L + (W - L - R) * (i / (n - 1 if n > 1 else 1)) for i in range(n)]
    ys = [T + (H - T - B) * (1 - v / vmax) for _, v in series]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{xs[0]:.1f},{H - B} " + line + f" {xs[-1]:.1f},{H - B}"
    grid = ""
    for frac in (0, 0.5, 1):
        gy = T + (H - T - B) * (1 - frac)
        grid += (f'<line x1="{L}" y1="{gy:.1f}" x2="{W - R}" y2="{gy:.1f}" stroke="#e2e8f0" stroke-dasharray="4 4"/>'
                 f'<text x="{L - 8}" y="{gy + 4:.1f}" text-anchor="end" font-size="11" fill="#64748b">{vmax * frac:g}</text>')
    labels = ""
    for i in sorted({0, n // 2, n - 1}):
        d = parse_iso_date(series[i][0])
        labels += (f'<text x="{xs[i]:.1f}" y="{H - 10}" text-anchor="middle" font-size="11" fill="#64748b">'
                   f'{d.strftime("%d/%m") if d else series[i][0]}</text>')
    best_i = max(range(n), key=lambda i: series[i][1])
    worst_i = min(range(n), key=lambda i: series[i][1])
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#0284c7"/>' for x, y in zip(xs, ys)) if n <= 45 else ""
    marks = (f'<circle cx="{xs[best_i]:.1f}" cy="{ys[best_i]:.1f}" r="6" fill="#16a34a" stroke="#fff" stroke-width="2"><title>Cao nhất: {series[best_i][1]:g} phần ({series[best_i][0]})</title></circle>'
             f'<circle cx="{xs[worst_i]:.1f}" cy="{ys[worst_i]:.1f}" r="6" fill="#ef4444" stroke="#fff" stroke-width="2"><title>Thấp nhất: {series[worst_i][1]:g} phần ({series[worst_i][0]})</title></circle>')
    return f"""
    <svg viewBox="0 0 {W} {H}" style="width:100%;height:auto;max-height:300px;">
      {grid}
      <polygon points="{area}" fill="#e0f2fe" opacity=".85"/>
      <polyline points="{line}" fill="none" stroke="#0284c7" stroke-width="2.5" stroke-linejoin="round"/>
      {dots}{marks}{labels}
    </svg>
    <p class="muted" style="margin-top:6px;"><span style="color:#16a34a;">●</span> Ngày bán cao nhất ({series[best_i][1]:g} phần, {series[best_i][0]})
       &nbsp; <span style="color:#ef4444;">●</span> Ngày bán thấp nhất ({series[worst_i][1]:g} phần, {series[worst_i][0]})</p>"""


def donut_html(rows):
    by_cat = {}
    for r in rows:
        by_cat[r["category_name"]] = by_cat.get(r["category_name"], 0) + r["total_revenue"]
    total = sum(by_cat.values())
    if total <= 0:
        return '<p class="empty">Chưa có doanh thu để vẽ biểu đồ.</p>'
    ordered = sorted(by_cat.items(), key=lambda kv: -kv[1])
    stops, legend, acc = [], "", 0.0
    for idx, (name, rev) in enumerate(ordered):
        color = CATEGORY_PALETTE[idx % len(CATEGORY_PALETTE)]
        pct = rev / total * 100
        stops.append(f"{color} {acc:.2f}% {acc + pct:.2f}%")
        acc += pct
        legend += (f'<div style="display:flex;align-items:center;gap:8px;margin:6px 0;font-size:13px;">'
                   f'<span style="width:12px;height:12px;border-radius:3px;background:{color};flex:none;"></span>'
                   f'<span style="flex:1;">{escape(name)}</span><strong>{pct:.1f}%</strong>'
                   f'<span class="muted">{rev:,.0f} đ</span></div>')
    return f"""
    <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap;">
      <div style="width:170px;height:170px;border-radius:50%;background:conic-gradient({", ".join(stops)});position:relative;flex:none;">
        <div style="position:absolute;inset:34px;background:#fff;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;">
          <span class="muted" style="font-size:11px;">Doanh thu</span><strong style="font-size:15px;">{total:,.0f}</strong><span class="muted" style="font-size:11px;">đồng</span>
        </div>
      </div>
      <div style="flex:1;min-width:230px;">{legend}</div>
    </div>"""


def analysis_card_html(r, accent, big=False, medal=""):
    sold_text = f'{r["total_sold"]:g} phần' if r["total_sold"] > 0 else "Chưa bán được"
    size = 96 if big else 64
    return f"""
    <div class="an-card" style="border-left:5px solid {accent};{'' if big else ''}">
      <div style="width:{size}px;height:{size}px;flex:none;">{food_thumb_html(r["category_id"], r["item_name"]).replace('class="food-thumb"', f'class="food-thumb" style="width:{size}px;height:{size}px;border-radius:12px;object-fit:cover;"')}</div>
      <div style="min-width:0;">
        <div style="font-size:{'20px' if big else '15px'};font-weight:800;">{medal} {escape(r["item_name"])}</div>
        <div class="muted">{escape(r["category_name"])}</div>
        <div style="margin-top:4px;"><strong style="color:{accent};">{sold_text}</strong> · {r["total_revenue"]:,.0f} đ</div>
      </div>
    </div>"""


@app.route("/analysis")
@role_required("admin")
def analysis():
    conn = get_db()
    end_date_str = request.args.get("end_date", date.today().isoformat())
    start_date_str = request.args.get("start_date", (date.today() - timedelta(days=30)).isoformat())
    category_filter = request.args.get("category_id", "")
    item_filter = request.args.get("item_id", "")

    categories = conn.execute("SELECT * FROM categories WHERE active = 1 ORDER BY name").fetchall()
    items = conn.execute("SELECT item_id, name, category_id FROM items ORDER BY name").fetchall()
    stats = collect_item_stats(conn, start_date_str, end_date_str, category_filter, item_filter)

    trend_sql = """
        SELECT date(o.order_date) AS d, SUM(od.quantity - COALESCE(od.canceled_quantity, 0)) AS qty
        FROM order_details od
        JOIN orders o ON o.order_id = od.order_id
        JOIN items ON items.item_id = od.item_id
        WHERE o.status != 'Đã hủy' AND date(o.order_date) BETWEEN ? AND ?
    """
    t_params = [start_date_str, end_date_str]
    if category_filter:
        trend_sql += " AND items.category_id = ?"
        t_params.append(category_filter)
    if item_filter:
        trend_sql += " AND items.item_id = ?"
        t_params.append(item_filter)
    trend_sql += " GROUP BY date(o.order_date) ORDER BY d"
    trend_points = [(str(r["d"]), float(r["qty"] or 0)) for r in conn.execute(trend_sql, t_params).fetchall()]
    conn.close()

    can_promote = get_current_user()["role"] in ("admin", "cashier")
    sold_rows = [r for r in stats if r["total_sold"] > 0]
    total_qty = sum(r["total_sold"] for r in stats)
    total_rev = sum(r["total_revenue"] for r in stats)
    n_unsold = sum(1 for r in stats if r["total_sold"] <= 0)

    # --- thẻ tổng quan ---
    kpis = f"""
    <div class="cards">
      <div class="card"><div class="card-title">Tổng số phần đã bán</div><div class="card-value" style="color:#0284c7;">{total_qty:g}</div></div>
      <div class="card"><div class="card-title">Tổng doanh thu</div><div class="card-value" style="color:#16a34a;font-size:24px;">{total_rev:,.0f} đ</div></div>
      <div class="card"><div class="card-title">Món có doanh số</div><div class="card-value" style="color:#f59e0b;">{len(sold_rows)}<span style="font-size:15px;color:#64748b;"> / {len(stats)}</span></div></div>
      <div class="card"><div class="card-title">Món chưa bán được</div><div class="card-value" style="color:#ef4444;">{n_unsold}</div></div>
    </div>"""

    # --- bán chạy (có hình ảnh) ---
    best_html = '<p class="empty">Chưa có món nào được bán trong khoảng thời gian này.</p>'
    if sold_rows:
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        top = sold_rows[:5]
        share = (top[0]["total_revenue"] / total_rev * 100) if total_rev else 0
        best_html = f"""
        <div style="display:grid;grid-template-columns:minmax(300px,1.3fr) 1fr;gap:14px;">
          <div>{analysis_card_html(top[0], "#16a34a", big=True, medal="🏆 Bán chạy nhất –")}
            <p class="muted" style="margin-top:8px;">Chiếm {share:.1f}% doanh thu.</p></div>
          <div style="display:grid;gap:10px;">{"".join(analysis_card_html(r, "#16a34a", medal=medals[i + 1]) for i, r in enumerate(top[1:]))}</div>
        </div>"""

    # --- biểu đồ cột món bán chạy (màu xanh) ---
    best_chart = column_chart_svg([{"label": r["item_name"], "value": r["total_sold"]} for r in sold_rows[:10]],
                                  "#16a34a", "phần")

    # --- bán chậm: biểu đồ cột màu đỏ + đề xuất giảm giá theo số đơn ---
    top_ids = {r["item_id"] for r in sold_rows[:5]}
    slow_list = sorted([r for r in stats if r["item_id"] not in top_ids and r["item_status"] == "Đang bán"],
                       key=lambda x: (x["order_count"], x["total_sold"], x["total_revenue"], x["item_name"]))
    for r in slow_list:
        r["suggest"] = suggest_discount(r["order_count"])
    # Bảng liệt kê mọi món được đề xuất giảm (≤ 10 đơn); nếu không có món nào thì lấy 10 món ít đơn nhất.
    slow_list = [r for r in slow_list if r["suggest"]] or slow_list[:10]
    # Biểu đồ: 12 món ít đơn nhất, nhưng tối đa 6 món chưa có đơn để vẫn thấy được các mức đề xuất khác.
    zero_rows = [r for r in slow_list if r["order_count"] == 0]
    chart_rows = (zero_rows[:6] + [r for r in slow_list if r["order_count"] > 0])[:12]
    hidden_zero = len(zero_rows) - min(len(zero_rows), 6)
    slow_chart = column_chart_svg(
        [{"label": r["item_name"], "value": r["order_count"], "note": f'-{r["suggest"]}%' if r["suggest"] else ""} for r in chart_rows],
        "#dc2626", "đơn")
    slow_html = '<p class="empty">Không có dữ liệu.</p>'
    if slow_list:
        slow_rows = ""
        for r in slow_list:
            if r["suggest"]:
                new_price = float(r["price"] or 0) * (100 - r["suggest"]) / 100
                suggest_cell = f'<span class="status red" style="font-size:12px;">Giảm {r["suggest"]}%</span>'
                price_cell = f'<s class="muted">{float(r["price"] or 0):,.0f}</s> → <strong>{new_price:,.0f} đ</strong>'
                action = (f'<a class="btn btn-primary" href="/promotions?{escape(urlencode({"new_item": r["item_id"], "new_discount": r["suggest"]}))}#add-promo">'
                          f'Tạo khuyến mại</a>') if can_promote else ""
            else:
                suggest_cell = '<span class="status gray">Chưa cần giảm</span>'
                price_cell = f'{float(r["price"] or 0):,.0f} đ'
                action = ""
            slow_rows += f"""<tr><td><span class="food-name">{escape(r["item_name"])}</span><br><span class="muted">{escape(r["category_name"])}</span></td>
              <td><strong style="color:#dc2626;">{r["order_count"]}</strong></td><td>{r["total_sold"]:g}</td>
              <td>{suggest_cell}</td><td>{price_cell}</td><td>{action}</td></tr>"""
        rule_text = " · ".join(
            (f"{lo} đơn" if lo == hi else f"{lo}–{hi} đơn") + f": giảm {pct}%"
            for (lo, hi, pct) in [(0, 0, 50), (1, 3, 30), (4, 10, 10)])
        slow_html = f"""{slow_chart}
        <div style="overflow:auto;max-height:520px;"><table style="background:#fff;border-radius:12px;">
          <thead style="position:sticky;top:0;z-index:1;"><tr><th>Món</th><th>Số đơn</th><th>Số phần bán</th><th>Đề xuất</th><th>Giá sau giảm</th><th></th></tr></thead>
          <tbody>{slow_rows}</tbody></table></div>"""

    # --- biểu đồ cột có màu theo mức bán ---
    max_sold = max([r["total_sold"] for r in stats], default=0)
    chart_bars = ""
    for r in stats[:12]:
        label, _, color = TIER_META[r["tier"]]
        width = max(2, int(r["total_sold"] / max_sold * 100)) if max_sold else 2
        chart_bars += f"""
        <div style="display:grid;grid-template-columns:minmax(110px,210px) 1fr 90px;gap:12px;align-items:center;margin:10px 0;">
          <div style="font-size:13px;overflow-wrap:anywhere;">{escape(r['item_name'])}</div>
          <div style="height:22px;background:#e5e7eb;border-radius:6px;overflow:hidden;">
            <div title="{label}: {r['total_sold']:g} phần" style="height:22px;width:{width}%;background:{color};border-radius:6px;"></div>
          </div>
          <div style="text-align:right;font-size:13px;font-weight:700;color:{color};">{r['total_sold']:g} phần</div>
        </div>"""
    if not chart_bars:
        chart_bars = '<p class="empty">Chưa có dữ liệu để vẽ biểu đồ.</p>'
    legend = "".join(f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px;font-size:13px;">'
                     f'<span style="width:12px;height:12px;border-radius:3px;background:{c};"></span>{lbl}</span>'
                     for lbl, _, c in TIER_META.values())

    # --- bảng ---
    rows = ""
    for r in stats:
        label, css, color = TIER_META[r["tier"]]
        share_txt = f'{r["total_revenue"] / total_rev * 100:.1f}%' if total_rev and r["total_revenue"] else "—"
        rows += f"""
        <tr>
          <td style="border-left:5px solid {color};">{r["rank"] if r["rank"] else "—"}</td>
          <td><div style="display:flex;gap:10px;align-items:center;">{food_thumb_html(r["category_id"], r["item_name"])}<span class="food-name">{escape(r['item_name'])}</span></div></td>
          <td>{escape(r['category_name'])}</td>
          <td><strong style="color:{color};">{r['total_sold']:g}</strong></td>
          <td>{r['total_revenue']:,.0f} đ</td><td>{share_txt}</td>
          <td><span class="status {css}">{label}</span></td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="7" class="empty">Không có dữ liệu trong khoảng thời gian này.</td></tr>'

    category_options = '<option value="">-- Tất cả danh mục --</option>'
    for c in categories:
        selected = "selected" if str(c["category_id"]) == category_filter else ""
        category_options += f'<option value="{c["category_id"]}" {selected}>{escape(c["name"])}</option>'
    item_options = '<option value="">-- Tất cả món ăn --</option>'
    for it in items:
        selected = "selected" if str(it["item_id"]) == item_filter else ""
        item_options += f'<option value="{it["item_id"]}" {selected}>{escape(it["name"])}</option>'

    style = """<style>
      .an-card { display:flex; gap:14px; align-items:center; padding:12px 14px; border:1px solid #e2e8f0; border-radius:14px; background:#fff; }
      .an-grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
      @media (max-width: 1000px) { .an-grid-2 { grid-template-columns:1fr; } }
    </style>"""

    style += """<style>
      .an-filter { display:grid; grid-template-columns:repeat(4,minmax(140px,1fr)) auto auto; gap:12px; align-items:end; }
      @media (max-width:1000px) { .an-filter { grid-template-columns:repeat(2,minmax(0,1fr)); } }
    </style>"""
    filter_html = f"""
    <div class="panel">
      <form method="GET" action="/analysis" class="an-filter">
        <div class="form-group"><label>Từ ngày</label><input type="date" name="start_date" value="{escape(start_date_str)}" required></div>
        <div class="form-group"><label>Đến ngày</label><input type="date" name="end_date" value="{escape(end_date_str)}" required></div>
        <div class="form-group"><label>Danh mục</label><select name="category_id">{category_options}</select></div>
        <div class="form-group"><label>Món ăn</label><select name="item_id">{item_options}</select></div>
        <button class="btn btn-primary">Xem thống kê</button>
        <a class="btn btn-secondary" href="/analysis">Đặt lại</a>
      </form>
    </div>"""
    best_pane = f"""
    <div class="panel"><h2>Món bán chạy nhất</h2>{best_html}
      <h3 style="margin:18px 0 6px;font-size:15px;color:#166534;">Số phần đã bán (tối đa 10 món)</h3>
      <div style="background:#fff;border:1px solid #bbf7d0;border-radius:8px;padding:12px;">{best_chart}</div></div>"""
    slow_pane = f"""
    <div class="panel"><h2>Món bán chậm & đề xuất giảm giá</h2>
      <div style="background:#fff;border:1px solid #fecaca;border-radius:8px;padding:12px;">{slow_html}</div></div>"""
    charts_pane = f"""
    <div class="an-grid-2">
      <div class="panel"><h2>Số lượng bán theo món ăn</h2>
        <p class="muted">{legend}</p><br>{chart_bars}</div>
      <div class="panel"><h2>Tỷ trọng doanh thu theo danh mục</h2><br>{donut_html(stats)}</div>
    </div>
    <div class="panel"><h2>Xu hướng số phần bán theo ngày</h2><br>{trend_svg(trend_points, start_date_str, end_date_str)}</div>"""
    table_pane = f"""
    <div class="panel">
      <div style="display:flex;justify-content:flex-end;margin-bottom:12px;">
        <a class="btn btn-primary" href="/analysis/export?{escape(urlencode({"start_date": start_date_str, "end_date": end_date_str, "category_id": category_filter, "item_id": item_filter}))}">Xuất báo cáo (Excel)</a>
      </div>
      <div style="overflow-x:auto;"><table><thead><tr><th>Hạng</th><th>Món ăn</th><th>Danh mục</th><th>Số lượng bán</th><th>Doanh thu theo giá bán</th><th>Tỷ trọng</th><th>Mức bán</th></tr></thead><tbody>{rows}</tbody></table></div>
    </div>"""
    n_suggest = sum(1 for r in slow_list if r.get("suggest"))
    an_tabs = tabs_html("analysis", [
        ("an-best", "Bán chạy", best_pane, min(5, len(sold_rows))),
        ("an-slow", "Bán chậm & đề xuất giảm giá", slow_pane, n_suggest),
        ("an-charts", "Biểu đồ", charts_pane),
        ("an-table", "Bảng kết quả", table_pane, len(stats)),
    ])
    body = f"""
    {style}
    <div class="header"><div><h1>Phân tích món ăn</h1></div></div>
    {filter_html}
    {kpis}
    {an_tabs}
    """
    return render_page("Phân tích món ăn", body, "analysis")


@app.route("/analysis/export")
@role_required("admin")
def export_analysis():
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    conn = get_db()
    start_date_str = request.args.get("start_date", (date.today() - timedelta(days=30)).isoformat())
    end_date_str = request.args.get("end_date", date.today().isoformat())
    category_filter = request.args.get("category_id", "")
    item_filter = request.args.get("item_id", "")

    query = """
        SELECT items.item_id AS item_id, items.name AS item_name, categories.name AS category_name,
               SUM(order_details.quantity - COALESCE(order_details.canceled_quantity, 0)) AS total_sold,
               SUM((order_details.quantity - COALESCE(order_details.canceled_quantity, 0)) * order_details.price) AS total_revenue
        FROM order_details
        JOIN orders ON order_details.order_id = orders.order_id
        JOIN items ON order_details.item_id = items.item_id
        JOIN categories ON items.category_id = categories.category_id
        WHERE orders.status != 'Đã hủy'
          AND date(orders.order_date) BETWEEN ? AND ?
          AND (order_details.quantity - COALESCE(order_details.canceled_quantity, 0)) > 0
    """
    params = [start_date_str, end_date_str]
    if category_filter:
        query += " AND categories.category_id = ?"
        params.append(category_filter)
    if item_filter:
        query += " AND items.item_id = ?"
        params.append(item_filter)
    query += " GROUP BY items.item_id, items.name, categories.name ORDER BY total_sold DESC, total_revenue DESC"
    stats = conn.execute(query, params).fetchall()
    cat_name = "Tất cả danh mục"
    if category_filter:
        r = conn.execute("SELECT name FROM categories WHERE category_id = ?", (category_filter,)).fetchone()
        cat_name = r["name"] if r else category_filter
    item_name = "Tất cả món ăn"
    if item_filter:
        r = conn.execute("SELECT name FROM items WHERE item_id = ?", (item_filter,)).fetchone()
        item_name = r["name"] if r else item_filter
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = "Phân tích món ăn"
    thin = Side(style="thin", color="D1D5DB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws["A1"] = "BÁO CÁO PHÂN TÍCH MÓN ĂN - OCEAN RESTAURANT"
    ws["A1"].font = Font(bold=True, size=14)
    ws.merge_cells("A1:F1")
    ws["A2"] = f"Thời gian: {start_date_str} đến {end_date_str}"
    ws["A3"] = f"Danh mục: {cat_name}   |   Món ăn: {item_name}"
    ws["A4"] = f"Ngày xuất báo cáo: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    header_row = 6
    headers = ["STT", "Mã món", "Món ăn", "Danh mục", "Số lượng bán", "Doanh thu (đ)"]
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=header_row, column=col, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2563EB")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border

    row = header_row
    for idx, s in enumerate(stats, 1):
        row += 1
        vals = [idx, s["item_id"], s["item_name"], s["category_name"], s["total_sold"], s["total_revenue"]]
        for col, v in enumerate(vals, 1):
            c = ws.cell(row=row, column=col, value=v)
            c.border = border
            if col == 6:
                c.number_format = "#,##0"
            if col in (1, 5):
                c.alignment = Alignment(horizontal="center")

    row += 1
    ws.cell(row=row, column=3, value="TỔNG CỘNG")
    if stats:
        ws.cell(row=row, column=5, value=f"=SUM(E{header_row + 1}:E{row - 1})")
        ws.cell(row=row, column=6, value=f"=SUM(F{header_row + 1}:F{row - 1})")
    else:
        ws.cell(row=row, column=5, value=0)
        ws.cell(row=row, column=6, value=0)
    for col in range(1, 7):
        c = ws.cell(row=row, column=col)
        c.font = Font(bold=True)
        c.border = border
        c.fill = PatternFill("solid", fgColor="E5E7EB")
    ws.cell(row=row, column=5).alignment = Alignment(horizontal="center")
    ws.cell(row=row, column=6).number_format = "#,##0"

    for col, w in zip("ABCDEF", [7, 12, 34, 26, 15, 18]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"bao_cao_phan_tich_{start_date_str}_{end_date_str}.xlsx"
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# =========================================================
# QUẢN LÝ TÀI KHOẢN (chỉ admin)
# =========================================================

def account_styles():
    return """<style>
      .steps { display:flex; gap:10px; align-items:center; margin:6px 0 10px; }
      .step-no { width:30px; height:30px; border-radius:50%; background:#1769e0; color:#fff; display:flex; align-items:center; justify-content:center; font-weight:800; flex:none; }
      .step-title { font-weight:800; color:#0c4a6e; font-size:16px; }
      .step-sub { color:#64748b; font-size:13px; }
      .step-block { padding:16px 18px; border:1px solid #e2e8f0; border-radius:14px; background:#f8fbff; margin-bottom:14px; }
      .role-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px; }
      .role-card { position:relative; cursor:pointer; }
      .role-card input { position:absolute; opacity:0; }
      .rc-body { display:flex; gap:10px; align-items:center; padding:12px; border:2px solid #e2e8f0; border-radius:12px; background:#fff; transition:.15s; height:100%; }
      .rc-body b { display:block; color:#0c4a6e; }
      .rc-body span { font-size:12px; color:#64748b; line-height:1.35; display:block; }
      .rc-ico { font-size:26px; }
      .role-card input:checked + .rc-body { border-color:#0284c7; background:#e0f2fe; box-shadow:0 4px 14px rgba(2,132,199,.18); }
      .role-card input:disabled + .rc-body { opacity:.75; cursor:not-allowed; }
      .perm-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:10px; }
      .perm-item { display:flex; gap:10px; align-items:flex-start; padding:10px 12px; border:1px solid #e2e8f0; border-radius:12px; background:#fff; cursor:pointer; position:relative; }
      .perm-item input { margin-top:4px; width:auto; }
      .perm-item b { display:block; font-size:14px; color:#0c4a6e; }
      .perm-item span.d { font-size:12px; color:#64748b; display:block; }
      .perm-tag { margin-left:auto; flex:none; white-space:nowrap; font-size:11px; font-weight:800; font-style:normal; }
      .perm-item.is-default { background:#f1f5f9; border-color:#cbd5e1; }
      .perm-item.is-default .perm-tag { color:#64748b; }
      .perm-item.is-extra { background:#e0f2fe; border-color:#38bdf8; }
      .perm-item.is-extra .perm-tag { color:#0369a1; }
      .acc-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:14px; }
      .acc-card { border:1px solid #e2e8f0; border-radius:16px; padding:16px; background:#fff; display:flex; flex-direction:column; gap:10px; box-shadow:0 4px 14px rgba(15,45,75,.05); }
      .acc-card.locked { background:#f8fafc; opacity:.85; }
      .acc-top { display:flex; gap:12px; align-items:center; }
      .avatar { width:48px; height:48px; border-radius:50%; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:800; font-size:18px; flex:none; }
      .av-admin { background:#be123c; } .av-cashier { background:#1769e0; }
      .av-waiter { background:#15803d; } .av-chef { background:#b45309; }
      .chip { display:inline-block; padding:3px 9px; border-radius:999px; font-size:11px; font-weight:700; margin:2px 3px 2px 0; }
      .chip.def { background:#f1f5f9; color:#475569; } .chip.ext { background:#dbeafe; color:#1d4ed8; } .chip.all { background:#fee2e2; color:#b91c1c; }
    </style>"""


ACCOUNT_JS = """
<script>
function initPermForm(form) {
  var defaults = __DEFAULTS__;
  function currentRole() { var r = form.querySelector('input[name=role]:checked'); return r ? r.value : ''; }
  function refresh() {
    var def = defaults[currentRole()] || [];
    form.querySelectorAll('.perm-item').forEach(function (item) {
      var key = item.dataset.perm, cb = item.querySelector('input'), tag = item.querySelector('.perm-tag');
      if (def.indexOf(key) >= 0) {
        cb.checked = true; cb.disabled = true;
        item.classList.add('is-default'); item.classList.remove('is-extra'); tag.textContent = 'Mặc định';
      } else {
        if (cb.disabled) { cb.checked = false; }
        cb.disabled = false; item.classList.remove('is-default');
        item.classList.toggle('is-extra', cb.checked); tag.textContent = cb.checked ? '+ Cấp thêm' : '';
      }
    });
  }
  form.addEventListener('change', refresh);
  refresh();
}
function suggestUsername(name) {
  var s = name.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').replace(/đ/g, 'd').replace(/Đ/g, 'D').toLowerCase().trim();
  var w = s.split(/\\s+/).filter(Boolean);
  if (!w.length) return '';
  var last = w[w.length - 1], ini = w.slice(0, -1).map(function (x) { return x[0]; }).join('');
  return (last + ini).replace(/[^a-z0-9]/g, '');
}
</script>
"""


def role_cards_html(selected, locked=False):
    html = '<div class="role-grid">'
    for key, (ico, label, desc) in ROLES.items():
        checked = "checked" if key == selected else ""
        disabled = "disabled" if locked else ""
        html += (f'<label class="role-card"><input type="radio" name="role" value="{key}" {checked} {disabled}>'
                 f'<div class="rc-body"><div class="rc-ico">{ico}</div><div><b>{label}</b><span>{desc}</span></div></div></label>')
    return html + "</div>"


def perm_grid_html(extras=()):
    html = '<div class="perm-grid">'
    for key, (ico, label, desc) in PERMISSIONS.items():
        checked = "checked" if key in extras else ""
        html += (f'<label class="perm-item" data-perm="{key}"><input type="checkbox" name="perm_{key}" value="1" {checked}>'
                 f'<div><b>{ico} {label}</b><span class="d">{desc}</span></div><em class="perm-tag"></em></label>')
    return html + "</div>"


def account_js():
    defaults = {role: sorted(perms) for role, perms in ROLE_DEFAULT_PERMS.items()}
    return ACCOUNT_JS.replace("__DEFAULTS__", json.dumps(defaults))


def parse_extra_perms(form, role):
    defaults = ROLE_DEFAULT_PERMS.get(role, set())
    return [k for k in PERMISSIONS if form.get(f"perm_{k}") and k not in defaults]


def user_card_html(u, current_id):
    ico, role_label, _ = ROLES.get(u["role"], ("", u["role"], ""))
    initials = (u["full_name"].strip().split() or ["?"])[-1][0].upper()
    extras = user_extra_perms(u)
    if u["role"] == "admin":
        chips = '<span class="chip all">Toàn quyền hệ thống</span>'
    else:
        chips = "".join(f'<span class="chip def">{PERMISSIONS[k][0]} {PERMISSIONS[k][1]}</span>'
                        for k in PERMISSIONS if k in ROLE_DEFAULT_PERMS.get(u["role"], set()))
        chips += "".join(f'<span class="chip ext">＋ {PERMISSIONS[k][0]} {PERMISSIONS[k][1]}</span>' for k in PERMISSIONS if k in extras)
    active = bool(u["active"])
    status = '<span class="status green">Hoạt động</span>' if active else '<span class="status gray">Đã khóa</span>'
    uid = escape(u["user_id"])
    if u["user_id"] == current_id:
        actions = f'<span class="muted">Tài khoản của bạn</span><a class="btn btn-secondary" href="/users/{uid}/edit">Sửa</a>'
    else:
        actions = f"""
        <form method="POST" action="/users/toggle/{uid}"><button class="btn btn-warning">{"Khóa" if active else "Mở khóa"}</button></form>
        <a class="btn btn-secondary" href="/users/{uid}/edit">Phân quyền</a>
        <form method="POST" action="/users/reset-password/{uid}" onsubmit="return confirm('Đặt lại mật khẩu về 123456?');"><button class="btn btn-secondary">Đặt lại MK</button></form>
        <form method="POST" action="/users/delete/{uid}" onsubmit="return confirm('Xóa vĩnh viễn tài khoản này?');"><button class="btn btn-danger">Xóa</button></form>"""
    return f"""
    <div class="acc-card {'' if active else 'locked'}">
      <div class="acc-top">
        <div class="avatar av-{escape(u["role"])}">{escape(initials)}</div>
        <div style="flex:1;min-width:0;">
          <div style="font-weight:800;font-size:16px;">{escape(u["full_name"])}</div>
          <div class="muted">@{escape(u["username"])} · {uid}</div>
        </div>
        <div style="text-align:right;"><span class="status blue">{ico} {role_label}</span><br>{status}</div>
      </div>
      <div><div class="muted" style="font-size:12px;margin-bottom:2px;">Quyền chức năng</div>{chips}</div>
      <div class="muted" style="font-size:12px;">Tạo ngày {escape(str(u["created_at"]))}</div>
      <div class="actions" style="flex-wrap:wrap;gap:8px;">{actions}</div>
    </div>"""


@app.route("/users")
@role_required("admin")
def users():
    conn = get_db()
    user_q = request.args.get("q", "").strip()
    user_role = request.args.get("role", "")
    user_active = request.args.get("active", "")
    user_sort = request.args.get("sort", "name")
    user_order = {"name": "full_name", "id": "user_id", "role": "role, full_name"}.get(user_sort, "full_name")
    user_sql = "SELECT * FROM users WHERE (username LIKE ? OR full_name LIKE ? OR user_id LIKE ?)"
    user_params = [f"%{user_q}%", f"%{user_q}%", f"%{user_q}%"]
    if user_role:
        user_sql += " AND role = ?"
        user_params.append(user_role)
    if user_active in ("0", "1"):
        user_sql += " AND active = ?"
        user_params.append(int(user_active))
    all_users = conn.execute(user_sql + f" ORDER BY {user_order}", user_params).fetchall()
    everyone = conn.execute("SELECT role, active FROM users").fetchall()
    conn.close()

    current_user = get_current_user()
    error = request.args.get("error", "")
    message = f'<div class="message">{escape(error)}</div>' if error else ""

    total = len(everyone)
    n_active = sum(1 for x in everyone if x["active"])
    n_admin = sum(1 for x in everyone if x["role"] == "admin")

    cards = "".join(user_card_html(u, current_user["user_id"]) for u in all_users)
    if not cards:
        cards = '<p class="empty">Không có tài khoản nào phù hợp.</p>'

    def opt(value, label, cur):
        return f'<option value="{value}" {"selected" if cur == value else ""}>{label}</option>'

    list_html = f"""
    <div class="panel">
        <form method="GET" action="/users#user-list" style="display:grid;grid-template-columns:2fr 1fr 1fr 1fr auto auto;gap:10px;align-items:center;margin-bottom:16px;">
            <input name="q" value="{escape(user_q)}" placeholder="Tìm mã, tên đăng nhập, họ tên">
            <select name="role"><option value="">Tất cả vị trí</option>{"".join(opt(k, v[1], user_role) for k, v in ROLES.items())}</select>
            <select name="active"><option value="">Tất cả trạng thái</option>{opt("1", "Hoạt động", user_active)}{opt("0", "Đã khóa", user_active)}</select>
            <select name="sort">{opt("name", "Tên A–Z", user_sort)}{opt("id", "Mã ID", user_sort)}{opt("role", "Vị trí", user_sort)}</select>
            <button class="btn btn-primary">Lọc</button><a class="btn btn-secondary" href="/users#user-list">Đặt lại</a>
        </form>
        <div class="acc-grid">{cards}</div>
    </div>"""
    add_html = f"""
    <div class="panel">
        <form method="POST" action="/users/add" id="add-user-form" autocomplete="off">
            <div class="step-block">
                <div class="steps"><div class="step-no">1</div><div><div class="step-title">Thông tin nhân viên</div><div class="step-sub">Tên nhân viên, tên đăng nhập và mật khẩu.</div></div></div>
                <div class="form-grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));">
                    <div class="form-group"><label>Tên nhân viên</label><input name="full_name" id="nu-name" placeholder="VD: Nguyễn Văn An" required></div>
                    <div class="form-group"><label>Tên đăng nhập</label><input name="username" id="nu-user" placeholder="tự gợi ý từ tên" required></div>
                    <div class="form-group"><label>Mật khẩu</label><input type="password" name="password" id="nu-pass" value="123456" minlength="4" required>
                        <label style="font-weight:400;font-size:12px;margin-top:4px;"><input type="checkbox" style="width:auto;" onchange="document.getElementById('nu-pass').type=this.checked?'text':'password'"> Hiện mật khẩu</label></div>
                </div>
            </div>
            <div class="step-block">
                <div class="steps"><div class="step-no">2</div><div><div class="step-title">Vị trí làm việc</div><div class="step-sub">Mỗi vị trí có sẵn bộ quyền mặc định.</div></div></div>
                {role_cards_html("waiter")}
            </div>
            <div class="step-block">
                <div class="steps"><div class="step-no">3</div><div><div class="step-title">Phân quyền chức năng</div><div class="step-sub">Quyền <b>Mặc định</b> theo vị trí được tự chọn sẵn. Tick thêm các chức năng khác nếu muốn cấp thêm quyền.</div></div></div>
                {perm_grid_html()}
            </div>
            <button class="btn btn-primary" style="padding:12px 22px;">Tạo tài khoản</button>
        </form>
    </div>"""
    user_tabs = tabs_html("users", [
        ("user-list", "Danh sách tài khoản", list_html, len(all_users)),
        ("user-add", "+ Thêm tài khoản", add_html),
    ])
    body = f"""
    {account_styles()}
    <div class="header">
        <div><h1>Quản lý tài khoản</h1></div>
    </div>
    {message}
    <div class="cards">
        <div class="card"><div class="card-title">Tổng tài khoản</div><div class="card-value">{total}</div></div>
        <div class="card"><div class="card-title">Đang hoạt động</div><div class="card-value" style="color:#16a34a;">{n_active}</div></div>
        <div class="card"><div class="card-title">Đã khóa</div><div class="card-value" style="color:#64748b;">{total - n_active}</div></div>
        <div class="card"><div class="card-title">Quản lý</div><div class="card-value" style="color:#be123c;">{n_admin}</div></div>
    </div>
    {user_tabs}
    {account_js()}
    <script>
      initPermForm(document.getElementById('add-user-form'));
      (function () {{
        var name = document.getElementById('nu-name'), user = document.getElementById('nu-user'), touched = false;
        user.addEventListener('input', function () {{ touched = true; }});
        name.addEventListener('input', function () {{ if (!touched) user.value = suggestUsername(name.value); }});
      }})();
    </script>
    """
    return render_page("Quản lý tài khoản", body, "users")


@app.route("/users/add", methods=["POST"])
@role_required("admin")
def add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    full_name = request.form.get("full_name", "").strip()
    role = request.form.get("role", "waiter")

    if not username or not password or not full_name:
        return redirect(url_for("users", _anchor="user-add", error="Vui lòng nhập đầy đủ tên nhân viên, tên đăng nhập và mật khẩu."))
    if role not in ROLES:
        return redirect(url_for("users", _anchor="user-add", error="Vị trí không hợp lệ."))
    if len(password) < 4:
        return redirect(url_for("users", _anchor="user-add", error="Mật khẩu phải có ít nhất 4 ký tự."))

    extras = parse_extra_perms(request.form, role)
    conn = get_db()
    if conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone():
        conn.close()
        return redirect(url_for("users", _anchor="user-add", error=f"Tên đăng nhập '{username}' đã tồn tại."))

    conn.execute("""
        INSERT INTO users(user_id, username, password_hash, full_name, role, active, created_at, extra_permissions)
        VALUES(?, ?, ?, ?, ?, 1, ?, ?)
    """, (next_code(conn, "users", "ND"), username, hash_password(password), full_name, role,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ",".join(extras) or None))
    conn.commit()
    conn.close()
    extra_txt = f" (cấp thêm {len(extras)} quyền)" if extras else ""
    return redirect(url_for("users", ok=f"Đã tạo tài khoản '{username}' – {ROLES[role][1]}{extra_txt}."))


@app.route("/users/<string:id>/edit")
@role_required("admin")
def users_edit(id):
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE user_id = ?", (id,)).fetchone()
    conn.close()
    if not u:
        return redirect(url_for("users", error="Không tìm thấy tài khoản."))
    is_self = (u["user_id"] == get_current_user()["user_id"])
    error = request.args.get("error", "")
    message = f'<div class="message">{escape(error)}</div>' if error else ""
    lock_note = ""
    hidden_role = f'<input type="hidden" name="role" value="{escape(u["role"])}">' if is_self else ""

    body = f"""
    {account_styles()}
    <div class="header">
        <div><h1>Phân quyền: {escape(u["full_name"])}</h1><p>@{escape(u["username"])} · {escape(u["user_id"])}</p></div>
        <a class="btn btn-secondary" href="/users">← Quay lại danh sách</a>
    </div>
    {message}
    <div class="panel">
        <form method="POST" action="/users/{escape(u["user_id"])}/update" id="edit-user-form">
            <div class="step-block">
                <div class="steps"><div class="step-no">1</div><div><div class="step-title">Thông tin nhân viên</div></div></div>
                <div class="form-group" style="max-width:420px;"><label>Tên nhân viên</label><input name="full_name" value="{escape(u["full_name"])}" required></div>
            </div>
            <div class="step-block">
                <div class="steps"><div class="step-no">2</div><div><div class="step-title">Vị trí làm việc</div></div></div>
                {lock_note}{hidden_role}{role_cards_html(u["role"], locked=is_self)}
            </div>
            <div class="step-block">
                <div class="steps"><div class="step-no">3</div><div><div class="step-title">Phân quyền chức năng</div><div class="step-sub">Quyền <b>Mặc định</b> không thể bỏ. Tick thêm để cấp quyền bổ sung.</div></div></div>
                {perm_grid_html(user_extra_perms(u))}
            </div>
            <button class="btn btn-primary" style="padding:12px 22px;">Lưu thay đổi</button>
        </form>
    </div>
    {account_js()}
    <script>initPermForm(document.getElementById('edit-user-form'));</script>
    """
    return render_page("Phân quyền tài khoản", body, "users")


@app.route("/users/<string:id>/update", methods=["POST"])
@role_required("admin")
def update_user(id):
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE user_id = ?", (id,)).fetchone()
    if not u:
        conn.close()
        return redirect(url_for("users", error="Không tìm thấy tài khoản."))
    full_name = request.form.get("full_name", "").strip()
    role = request.form.get("role", u["role"])
    is_self = (id == get_current_user()["user_id"])
    if is_self:
        role = u["role"]
    if not full_name or role not in ROLES:
        conn.close()
        return redirect(url_for("users_edit", id=id, error="Thông tin không hợp lệ."))
    if u["role"] == "admin" and role != "admin":
        remaining = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND user_id != ?", (id,)).fetchone()[0]
        if remaining < 1:
            conn.close()
            return redirect(url_for("users_edit", id=id, error="Không thể hạ quyền quản lý cuối cùng của hệ thống."))
    extras = parse_extra_perms(request.form, role)
    conn.execute("UPDATE users SET full_name = ?, role = ?, extra_permissions = ? WHERE user_id = ?",
                 (full_name, role, ",".join(extras) or None, id))
    conn.commit()
    conn.close()
    return redirect(url_for("users", ok=f"Đã cập nhật phân quyền cho '{u['username']}' thành công."))


@app.route("/users/toggle/<string:id>", methods=["POST"])
@role_required("admin")
def toggle_user(id):
    current_user = get_current_user()
    if id == current_user["user_id"]:
        return redirect(url_for("users", error="Không thể khóa chính mình."))

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (id,)).fetchone()

    if not user:
        conn.close()
        return redirect(url_for("users", error="Không tìm thấy tài khoản."))

    new_active = 0 if user["active"] else 1
    conn.execute("UPDATE users SET active = ? WHERE user_id = ?", (new_active, id))
    conn.commit()
    conn.close()

    status_text = "mở khóa" if new_active else "khóa"
    return redirect(url_for("users", ok=f"Đã {status_text} tài khoản '{user['username']}' thành công."))


@app.route("/users/reset-password/<string:id>", methods=["POST"])
@role_required("admin")
def reset_password(id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (id,)).fetchone()

    if not user:
        conn.close()
        return redirect(url_for("users", error="Không tìm thấy tài khoản."))

    conn.execute(
        "UPDATE users SET password_hash = ? WHERE user_id = ?",
        (hash_password("123456"), id)
    )
    conn.commit()
    conn.close()

    return redirect(url_for("users",
        ok=f"Đã đặt lại mật khẩu cho '{user['username']}' thành '123456'."))


@app.route("/users/delete/<string:id>", methods=["POST"])
@role_required("admin")
def delete_user(id):
    current_user = get_current_user()
    if id == current_user["user_id"]:
        return redirect(url_for("users", error="Không thể xóa chính mình."))

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (id,)).fetchone()

    if not user:
        conn.close()
        return redirect(url_for("users", error="Không tìm thấy tài khoản."))

    if user["role"] == "admin":
        admin_count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
        ).fetchone()[0]
        if admin_count <= 1:
            conn.close()
            return redirect(url_for("users",
                error="Không thể xóa quản lý cuối cùng của hệ thống."))

    conn.execute("DELETE FROM users WHERE user_id = ?", (id,))
    conn.commit()
    conn.close()

    return redirect(url_for("users", ok=f"Đã xóa tài khoản '{user['username']}' thành công."))


# =========================================================
# CHẠY ỨNG DỤNG
# =========================================================

# =========================================================
# QUẢN LÝ KHÁCH HÀNG THÀNH VIÊN
# =========================================================
@app.route("/customers")
@role_required("admin", "cashier")
def customers():
    q = request.args.get("q", "").strip()
    tier_filter = request.args.get("tier", "").strip()
    sort = request.args.get("sort", "name")
    order_by = {"name": "c.full_name", "spent_desc": "c.total_spent DESC", "spent_asc": "c.total_spent ASC", "tier": "t.min_total_spent DESC"}.get(sort, "c.full_name")
    conn = get_db()
    sql = """SELECT c.*, t.tier_name, t.discount_percent FROM customers c
             JOIN member_tiers t ON t.member_tier_id=c.member_tier_id
             WHERE (c.full_name LIKE ? OR c.phone LIKE ? OR c.customer_id LIKE ?)"""
    params = [f"%{q}%", f"%{q}%", f"%{q}%"]
    if tier_filter:
        sql += " AND c.member_tier_id=?"; params.append(tier_filter)
    rows_data = conn.execute(sql + f" ORDER BY {order_by}", params).fetchall()
    tiers = conn.execute("SELECT * FROM member_tiers ORDER BY min_total_spent").fetchall()
    conn.close()
    rows = ""
    for c in rows_data:
        rows += f"""<tr>
          <td>{escape(c['customer_id'])}</td><td>{escape(c['full_name'])}</td><td>{escape(c['phone'])}</td>
          <td>{int(c['total_orders'] or 0)}</td><td>{c['total_spent']:,.0f} đ</td>
          <td>{escape(c['tier_name'])}</td><td>{c['discount_percent']:g}%</td>
          <td><span class="status {'green' if c['active'] else 'gray'}">{'Đang hoạt động' if c['active'] else 'Ngừng hoạt động'}</span></td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="8" class="empty">Không tìm thấy khách hàng phù hợp.</td></tr>'
    tier_rows = ""
    for t in tiers:
        max_spent_label = f"{t['max_total_spent']:,.0f} đ" if t["max_total_spent"] is not None else "Không giới hạn"
        tier_rows += f"<tr><td>{escape(t['tier_name'])}</td><td>{t['min_total_spent']:,.0f} đ</td><td>{max_spent_label}</td><td>{t['discount_percent']:g}%</td><td>{escape(t['description'] or '')}</td></tr>"
    error = request.args.get("error", "")
    message = f'<div class="message">{escape(error)}</div>' if error else ""
    tier_opts = "".join(
        f'<option value="{t["member_tier_id"]}" {"selected" if tier_filter == t["member_tier_id"] else ""}>{escape(t["tier_name"])}</option>'
        for t in tiers)
    sort_opts = "".join(
        f'<option value="{k}" {"selected" if sort == k else ""}>{v}</option>'
        for k, v in (("name", "Tên A–Z"), ("spent_desc", "Chi tiêu cao–thấp"), ("spent_asc", "Chi tiêu thấp–cao"), ("tier", "Hạng thành viên")))
    list_html = f"""
    <div class="panel">
      <form method="GET" action="/customers#cust-list" class="actions" style="margin-bottom:12px;">
        <input name="q" value="{escape(q)}" placeholder="Tìm mã, tên hoặc số điện thoại">
        <select name="tier"><option value="">Tất cả hạng</option>{tier_opts}</select>
        <select name="sort">{sort_opts}</select>
        <button class="btn btn-primary">Lọc</button><a class="btn btn-secondary" href="/customers#cust-list">Đặt lại</a>
      </form>
      <table><thead><tr><th>Mã KH</th><th>Họ tên</th><th>Số điện thoại</th><th>Số đơn hoàn thành</th><th>Tổng chi tiêu</th><th>Hạng</th><th>Giảm giá</th><th>Trạng thái</th></tr></thead><tbody>{rows}</tbody></table>
    </div>"""
    add_html = """
    <div class="panel">
      <form method="POST" action="/customers/add" class="form-grid" style="grid-template-columns:1.3fr 1fr auto;">
        <div class="form-group"><label>Họ và tên</label><input name="full_name" required maxlength="100"></div>
        <div class="form-group"><label>Số điện thoại (bắt buộc)</label><input name="phone" required pattern="[0-9+() -]{9,20}" placeholder="09xxxxxxxx"></div>
        <button class="btn btn-primary">+ Đăng ký thành viên</button>
      </form>
    </div>"""
    tiers_html = f"""
    <div class="panel">
      <table><thead><tr><th>Hạng</th><th>Chi tiêu tối thiểu</th><th>Chi tiêu tối đa</th><th>Giảm giá</th><th>Mô tả</th></tr></thead><tbody>{tier_rows}</tbody></table>
    </div>"""
    cust_tabs = tabs_html("customers", [
        ("cust-list", "Danh sách khách hàng", list_html, len(rows_data)),
        ("cust-add", "+ Thêm thành viên", add_html),
        ("cust-tiers", "Quy định hạng", tiers_html, len(tiers)),
    ])
    body = f"""
    <div class="header"><div><h1>Quản lý khách hàng</h1></div></div>
    {message}
    {cust_tabs}"""
    return render_page("Quản lý khách hàng", body, "customers")


@app.route("/customers/add", methods=["POST"])
@role_required("admin", "cashier")
def add_customer():
    full_name = request.form.get("full_name", "").strip()
    phone = "".join(ch for ch in request.form.get("phone", "").strip() if ch.isdigit() or ch == "+")
    if not full_name or len(phone) < 9:
        return redirect(url_for("customers", _anchor="cust-add", error="Vui lòng nhập tên và số điện thoại hợp lệ."))
    conn = get_db()
    try:
        cid = next_code(conn, "customers", "KH")
        conn.execute("""INSERT INTO customers
            (customer_id, full_name, phone, created_at, total_spent, total_orders, member_tier_id, active)
            VALUES (?, ?, ?, ?, 0, 0, ?, 1)""",
            (cid, full_name, phone, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tier_for_spent(conn, 0)))
        conn.commit()
    except IntegrityError:
        conn.close()
        return redirect(url_for("customers", _anchor="cust-add", error="Số điện thoại đã được đăng ký."))
    conn.close()
    return redirect(url_for("customers", _anchor="cust-list", ok=f"Đã đăng ký thành viên {full_name}."))

def run_server():
    args = sys.argv[1:]
    ensure_database()

    # python py1.py --import-sql file1.sql [file2.sql ...]  -> chỉ nạp dữ liệu rồi thoát
    if "--import-sql" in args:
        files = [a for a in args[args.index("--import-sql") + 1:] if not a.startswith("--")] or [str(SQL_FILE)]
        results = [import_sql_file(f) for f in files]
        sys.exit(0 if all(results) else 1)

    # Lần chạy đầu (database trống): tự nạp file SQL mặc định.
    if database_needs_setup():
        print(f"Database trống, đang nạp {SQL_FILE.name} ...")
        if not import_sql_file(SQL_FILE):
            sys.exit(1)
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)


if __name__ == "__main__":
    run_server()
