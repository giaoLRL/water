"""MySQL 数据层：连接池、建表初始化、水循环传感/告警/日志/账号等读写接口。

所有 SQL 封装在本模块，业务模块(api / alarm / 采集循环)只调用这里的函数。
启动时 init_database() 建库建表(幂等)。单套水循环系统，设备ID统一用 "water"。
"""
import threading
from datetime import datetime

import pymysql
from dbutils.pooled_db import PooledDB

import config

_pool = None
_pool_lock = threading.Lock()

DEVICE_ID = "water"   # 单套水循环系统的统一设备ID


def get_pool() -> PooledDB:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = PooledDB(
                creator=pymysql,
                maxconnections=config.DB_POOL_SIZE,
                mincached=1,
                blocking=True,
                cursorclass=pymysql.cursors.DictCursor,
                **config.db_dsn(),
            )
    return _pool


def _connect_server() -> pymysql.Connection:
    """连接服务器(不指定库)，用于建库。"""
    return pymysql.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        user=config.DB_USER, password=config.DB_PASSWORD,
        charset="utf8mb4", autocommit=True,
    )


def init_database() -> None:
    """创建数据库与业务表(幂等)。"""
    conn = _connect_server()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config.DB_NAME}` "
                "DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_unicode_ci"
            )
    finally:
        conn.close()

    pool = get_pool()
    conn = pool.connection()
    try:
        with conn.cursor() as cur:
            # 清理旧版(灯杆)遗留表结构：alarms/control_log 若仍是 lamp_id 结构则重建为 device_id
            for table, need in (("alarms", "device_id"), ("control_log", "device_id")):
                cur.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                    (config.DB_NAME, table, need),
                )
                if cur.fetchone()["n"] == 0:
                    cur.execute(f"DROP TABLE IF EXISTS `{table}`")

            # 水循环传感数据表
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS water_sensors (
                    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    ts           DATETIME NOT NULL,
                    storage_temp DOUBLE NOT NULL,
                    heater_temp  DOUBLE NOT NULL,
                    flow_rate    DOUBLE NOT NULL,
                    pressure     DOUBLE NOT NULL,
                    pump_state   VARCHAR(8) NOT NULL,
                    heater_state VARCHAR(8) NOT NULL,
                    total_flow   DOUBLE NOT NULL DEFAULT 0,
                    PRIMARY KEY (id),
                    KEY idx_ts (ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 告警记录
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alarms (
                    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    device_id    VARCHAR(16) NOT NULL DEFAULT 'water',
                    ts           DATETIME NOT NULL,
                    type         VARCHAR(32) NOT NULL,
                    value        DOUBLE NOT NULL,
                    threshold    DOUBLE NOT NULL,
                    direction    VARCHAR(16) NOT NULL,
                    message      VARCHAR(255),
                    status       VARCHAR(16) NOT NULL DEFAULT 'active',
                    recovered_at DATETIME NULL,
                    PRIMARY KEY (id),
                    KEY idx_alarm_ts (ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 设备操作日志(水泵/加热器开关)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS control_log (
                    id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    device_id VARCHAR(16) NOT NULL DEFAULT 'water',
                    ts       DATETIME NOT NULL,
                    action   VARCHAR(32) NOT NULL,
                    result   VARCHAR(16) NOT NULL,
                    detail   VARCHAR(255),
                    PRIMARY KEY (id),
                    KEY idx_ctrl_ts (ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 阈值/PID/判定服务动态配置
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS config (
                    config_key VARCHAR(64) NOT NULL,
                    value      LONGTEXT NOT NULL,
                    PRIMARY KEY (config_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            for key, value in config.DEFAULT_THRESHOLDS.items():
                cur.execute(
                    "INSERT IGNORE INTO config (config_key, value) VALUES (%s, %s)",
                    (key, str(value)),
                )
            # 账号表
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    username      VARCHAR(64) NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    role          VARCHAR(32) NOT NULL DEFAULT 'viewer',
                    status        VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at    DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_username (username)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
    finally:
        conn.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ping() -> bool:
    try:
        conn = get_pool().connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False


# ---------- 水循环传感数据 ----------
def insert_water_sensor(
    ts: datetime, storage_temp: float, heater_temp: float,
    flow_rate: float, pressure: float,
    pump_state: str, heater_state: str, total_flow: float,
) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO water_sensors (ts, storage_temp, heater_temp, flow_rate, pressure, "
                "pump_state, heater_state, total_flow) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (ts, storage_temp, heater_temp, flow_rate, pressure,
                 pump_state, heater_state, total_flow),
            )
    finally:
        conn.close()


def query_water_history(start: str, end: str, limit: int = 5000) -> list[dict]:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, storage_temp, heater_temp, "
                "flow_rate, pressure, pump_state, heater_state, total_flow "
                "FROM water_sensors WHERE ts BETWEEN %s AND %s "
                "ORDER BY ts ASC LIMIT %s",
                (start, end, limit),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def query_water_stats(start: str, end: str) -> dict:
    """统计面板：平均/最高/最低温度、最高/最低压力、累计水流量。"""
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT AVG(storage_temp) AS avg_storage, MAX(storage_temp) AS max_storage, "
                "MIN(storage_temp) AS min_storage, AVG(heater_temp) AS avg_heater, "
                "MAX(pressure) AS max_pressure, MIN(pressure) AS min_pressure, "
                "(SELECT total_flow FROM water_sensors WHERE ts BETWEEN %s AND %s "
                " ORDER BY id DESC LIMIT 1) AS end_total "
                "FROM water_sensors WHERE ts BETWEEN %s AND %s",
                (start, end, start, end),
            )
            row = cur.fetchone()
            return {
                "avg_storage_temp": round(row["avg_storage"], 2) if row["avg_storage"] is not None else None,
                "max_storage_temp": round(row["max_storage"], 2) if row["max_storage"] is not None else None,
                "min_storage_temp": round(row["min_storage"], 2) if row["min_storage"] is not None else None,
                "avg_heater_temp": round(row["avg_heater"], 2) if row["avg_heater"] is not None else None,
                "max_pressure": round(row["max_pressure"], 2) if row["max_pressure"] is not None else None,
                "min_pressure": round(row["min_pressure"], 2) if row["min_pressure"] is not None else None,
                "total_flow": round(row["end_total"], 2) if row["end_total"] is not None else 0.0,
            }
    finally:
        conn.close()


# ---------- 告警 ----------
def update_or_insert_alarm(
    ts: datetime, type_: str, value: float, threshold: float,
    direction: str, message: str,
) -> None:
    """同类型已有 active 记录则更新，否则插入(避免长期活跃重复多条)。"""
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alarms SET ts=%s, value=%s, threshold=%s, direction=%s, message=%s "
                "WHERE device_id=%s AND type=%s AND status='active'",
                (ts, value, threshold, direction, message, DEVICE_ID, type_),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT INTO alarms (device_id, ts, type, value, threshold, direction, message, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')",
                    (DEVICE_ID, ts, type_, value, threshold, direction, message),
                )
    finally:
        conn.close()


def recover_alarm(type_: str) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alarms SET status='recovered', recovered_at=%s "
                "WHERE device_id=%s AND type=%s AND status='active'",
                (datetime.now(), DEVICE_ID, type_),
            )
    finally:
        conn.close()


def query_alarms(start: str | None, end: str | None, type_: str | None,
                 status: str | None, page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
    """分页查询告警记录，返回 (items, total)。"""
    conn = get_pool().connection()
    where = " WHERE device_id = %s"
    params: list = [DEVICE_ID]
    if start:
        where += " AND ts >= %s"; params.append(start)
    if end:
        where += " AND ts <= %s"; params.append(end)
    if type_:
        where += " AND type = %s"; params.append(type_)
    if status:
        where += " AND status = %s"; params.append(status)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM alarms{where}", params)
            total = cur.fetchone()["n"]
            offset = (page - 1) * page_size
            cur.execute(
                "SELECT id, DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, type, value, threshold, "
                "direction, message, status FROM alarms"
                + where + " ORDER BY ts DESC LIMIT %s OFFSET %s",
                params + [page_size, offset],
            )
            items = list(cur.fetchall())
    finally:
        conn.close()
    return items, total


def query_alarm_stats() -> list[dict]:
    """按类型统计告警数量，供分布图使用。"""
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT type, COUNT(*) AS count FROM alarms "
                "WHERE device_id=%s GROUP BY type ORDER BY count DESC",
                (DEVICE_ID,),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


# ---------- 阈值/动态配置 ----------
def get_config(key: str, default: str | None = None) -> str | None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM config WHERE config_key=%s", (key,))
            row = cur.fetchone()
            return row["value"] if row else default
    finally:
        conn.close()


def set_config(key: str, value: str) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO config (config_key, value) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE value=VALUES(value)",
                (key, value),
            )
    finally:
        conn.close()


def get_all_config() -> dict:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT config_key, value FROM config")
            return {row["config_key"]: row["value"] for row in cur.fetchall()}
    finally:
        conn.close()


# ---------- 设备操作日志 ----------
def insert_control_log(action: str, result: str, detail: str | None = None) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO control_log (device_id, ts, action, result, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (DEVICE_ID, datetime.now(), action, result, detail),
            )
    finally:
        conn.close()


def query_control_log(start: str | None, end: str | None,
                      page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
    conn = get_pool().connection()
    where = " WHERE device_id = %s"
    params: list = [DEVICE_ID]
    if start:
        where += " AND ts >= %s"; params.append(start)
    if end:
        where += " AND ts <= %s"; params.append(end)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM control_log{where}", params)
            total = cur.fetchone()["n"]
            offset = (page - 1) * page_size
            cur.execute(
                "SELECT DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, action, result, detail "
                "FROM control_log" + where + " ORDER BY id DESC LIMIT %s OFFSET %s",
                params + [page_size, offset],
            )
            items = list(cur.fetchall())
    finally:
        conn.close()
    return items, total


# ---------- 账号 ----------
def count_users() -> int:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            return cur.fetchone()["n"] or 0
    finally:
        conn.close()


def get_user(username: str) -> dict | None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, password_hash, role, status "
                "FROM users WHERE username=%s",
                (username,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def insert_user(username: str, password_hash: str, role: str) -> int:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, role, status, created_at) "
                "VALUES (%s, %s, %s, 'active', %s)",
                (username, password_hash, role, datetime.now()),
            )
            return cur.lastrowid
    finally:
        conn.close()


def update_user_password(user_id: int, password_hash: str) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hash, user_id))
    finally:
        conn.close()


def update_user_role(user_id: int, role: str) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET role=%s WHERE id=%s", (role, user_id))
    finally:
        conn.close()


def update_user_status(user_id: int, status: str) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET status=%s WHERE id=%s", (status, user_id))
    finally:
        conn.close()


def delete_user(user_id: int) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
    finally:
        conn.close()


def query_users() -> list[dict]:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, role, status FROM users ORDER BY id ASC")
            return list(cur.fetchall())
    finally:
        conn.close()