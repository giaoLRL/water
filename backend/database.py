"""MySQL 数据层：连接池、建库建表初始化、灯杆传感/告警/人员监测/日志等读写接口。

所有的 SQL 都封装在本模块，业务模块（api / alarm / 采集循环）只调用这里的函数，
不直接写 SQL。启动时 init_database() 负责建库建表，并自动清理旧水循环遗留表结构、
温和补充新增列（如告警快照图列），避免丢历史数据。
"""
import threading
from datetime import datetime

import pymysql
from dbutils.pooled_db import PooledDB

import config

_pool = None
_pool_lock = threading.Lock()


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
    """连接服务器（不指定库），用于建库。"""
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        charset="utf8mb4",
        autocommit=True,
    )


# 各业务表必备字段（用于检测并清理水循环系统遗留的旧表结构）
_TABLE_COLUMNS = {
    "lamp_sensors": {"lamp_id", "ts", "temperature", "humidity", "luminance", "light_state"},
    "alarms": {"lamp_id", "ts", "type", "value", "threshold", "direction", "message", "status"},
    "person_detections": {"lamp_id", "ts", "person_count", "max_confidence", "original_image", "processed_image"},
    "control_log": {"lamp_id", "ts", "action", "result", "detail"},
    "config": {"config_key", "value"},
}

# 业务表的新增列：存在表结构时温和补列（不删表），避免丢失历史数据
_EXTRA_COLUMNS = {
    "alarms": {"image": "LONGTEXT NULL"},
}


def _ensure_extra_columns(conn: pymysql.Connection, table: str) -> None:
    """为已存在的表温和补充新增列（不删除表，保留历史数据）。"""
    extra = _EXTRA_COLUMNS.get(table)
    if not extra:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name AS col FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (config.DB_NAME, table),
        )
        existing = {row["col"] for row in cur.fetchall()}
    for col, ddl in extra.items():
        if col in existing:
            continue
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {ddl}")


def _ensure_table_schema(conn: pymysql.Connection, table: str) -> None:
    """表结构不匹配（如旧水循环表的 control_log/alarms）时删除，由建表语句重建。"""
    req = _TABLE_COLUMNS[table]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (config.DB_NAME, table),
        )
        if cur.fetchone()["n"] == 0:
            return
        cur.execute(
            "SELECT column_name AS col FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (config.DB_NAME, table),
        )
        existing = {row["col"] for row in cur.fetchall()}
        if req <= existing:
            return
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{table}`")


def init_database() -> None:
    """创建数据库与业务表（幂等）。"""
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
            # 清理结构不匹配的旧表（水循环系统遗留）
            for table in _TABLE_COLUMNS:
                _ensure_table_schema(conn, table)
            # 为已存在的表补充新增列（如 alarms.image），避免删表丢历史
            for table in _EXTRA_COLUMNS:
                _ensure_extra_columns(conn, table)

            # 灯杆传感器数据
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS lamp_sensors (
                    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    lamp_id      VARCHAR(16) NOT NULL,
                    ts           DATETIME NOT NULL,
                    temperature  DOUBLE NOT NULL,
                    humidity     DOUBLE NOT NULL,
                    luminance    DOUBLE NOT NULL,
                    light_state  VARCHAR(8) NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_lamp_ts (lamp_id, ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 告警记录（image：异常情况截图快照，base64 标注图）
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alarms (
                    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    lamp_id      VARCHAR(16) NOT NULL,
                    ts           DATETIME NOT NULL,
                    type         VARCHAR(32) NOT NULL,
                    value        DOUBLE NOT NULL,
                    threshold    DOUBLE NOT NULL,
                    direction    VARCHAR(16) NOT NULL,
                    message      VARCHAR(255),
                    status       VARCHAR(16) NOT NULL DEFAULT 'active',
                    recovered_at DATETIME NULL,
                    image        LONGTEXT NULL,
                    PRIMARY KEY (id),
                    KEY idx_alarm_lamp_ts (lamp_id, ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 人员监测记录
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS person_detections (
                    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    lamp_id         VARCHAR(16) NOT NULL,
                    ts              DATETIME NOT NULL,
                    person_count    INT NOT NULL,
                    max_confidence  DOUBLE NOT NULL DEFAULT 0,
                    original_image  LONGTEXT NULL,
                    processed_image LONGTEXT NULL,
                    PRIMARY KEY (id),
                    KEY idx_detect_lamp_ts (lamp_id, ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 设备操作日志
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS control_log (
                    id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    lamp_id  VARCHAR(16) NOT NULL,
                    ts       DATETIME NOT NULL,
                    action   VARCHAR(16) NOT NULL,
                    result   VARCHAR(16) NOT NULL,
                    detail   VARCHAR(255),
                    PRIMARY KEY (id),
                    KEY idx_ctrl_lamp_ts (lamp_id, ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 阈值配置
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS config (
                    config_key VARCHAR(64) NOT NULL,
                    value      VARCHAR(255) NOT NULL,
                    PRIMARY KEY (config_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 配置值扩容：动态配置以 JSON 存入（灯杆列表等），255 字符不够
            cur.execute("ALTER TABLE config MODIFY COLUMN value LONGTEXT NOT NULL")
            for key, value in config.DEFAULT_THRESHOLDS.items():
                cur.execute(
                    "INSERT IGNORE INTO config (config_key, value) VALUES (%s, %s)",
                    (key, str(value)),
                )
    finally:
        conn.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ping() -> bool:
    """探测数据库连接是否可用。"""
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


# ---------- 传感器数据 ----------
def insert_sensor_data(
    lamp_id: str,
    ts: datetime,
    temperature: float,
    humidity: float,
    luminance: float,
    light_state: str,
) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lamp_sensors (lamp_id, ts, temperature, humidity, luminance, light_state) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (lamp_id, ts, temperature, humidity, luminance, light_state),
            )
    finally:
        conn.close()


def query_history(lamp_id: str, start: str, end: str, limit: int = 5000) -> list[dict]:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, temperature, humidity, luminance, "
                "light_state FROM lamp_sensors "
                "WHERE lamp_id=%s AND ts BETWEEN %s AND %s ORDER BY ts ASC LIMIT %s",
                (lamp_id, start, end, limit),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def query_latest_sensor(lamp_id: str) -> dict | None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, temperature, humidity, luminance, "
                "light_state FROM lamp_sensors WHERE lamp_id=%s ORDER BY id DESC LIMIT 1",
                (lamp_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


# ---------- 告警 ----------
def insert_alarm(
    lamp_id: str,
    ts: datetime,
    type_: str,
    value: float,
    threshold: float,
    direction: str,
    message: str,
    image: str | None = None,
) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alarms (lamp_id, ts, type, value, threshold, direction, message, status, image) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s)",
                (lamp_id, ts, type_, value, threshold, direction, message, image),
            )
    finally:
        conn.close()


def recover_alarm(lamp_id: str, type_: str) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alarms SET status='recovered', recovered_at=%s "
                "WHERE lamp_id=%s AND type=%s AND status='active'",
                (datetime.now(), lamp_id, type_),
            )
    finally:
        conn.close()


def query_alarms(lamp_id: str | None, start: str | None, end: str | None,
                 type_: str | None, status: str | None, keyword: str | None = None,
                 page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
    """分页查询告警记录，返回 (items, total)。keyword 模糊匹配类型或描述。"""
    conn = get_pool().connection()
    where = " WHERE 1=1"
    params: list = []
    if lamp_id:
        where += " AND lamp_id = %s"
        params.append(lamp_id)
    if start:
        where += " AND ts >= %s"
        params.append(start)
    if end:
        where += " AND ts <= %s"
        params.append(end)
    if type_:
        where += " AND type = %s"
        params.append(type_)
    if status:
        where += " AND status = %s"
        params.append(status)
    if keyword:
        where += " AND (type LIKE %s OR message LIKE %s)"
        params += [f"%{keyword}%", f"%{keyword}%"]
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM alarms{where}", params)
            total = cur.fetchone()["n"]
            offset = (page - 1) * page_size
            sql = ("SELECT id, lamp_id, DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, type, value, "
                   "threshold, direction, message, status, "
                   "(image IS NOT NULL AND image <> '') AS has_image FROM alarms"
                   + where + " ORDER BY ts DESC LIMIT %s OFFSET %s")
            cur.execute(sql, params + [page_size, offset])
            items = list(cur.fetchall())
    finally:
        conn.close()
    return items, total


def get_alarm(alarm_id: int) -> dict | None:
    """返回单条告警记录（含 image 快照图）。"""
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, lamp_id, DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, type, value, "
                "threshold, direction, message, status, recovered_at, image "
                "FROM alarms WHERE id=%s",
                (alarm_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def query_alarm_stats(lamp_id: str | None = None) -> list[dict]:
    """按类型统计告警数量（全库或指定灯杆），供分布图使用。"""
    conn = get_pool().connection()
    sql = "SELECT type, COUNT(*) AS count FROM alarms"
    params: list = []
    if lamp_id:
        sql += " WHERE lamp_id = %s"
        params.append(lamp_id)
    sql += " GROUP BY type ORDER BY count DESC"
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


# ---------- 人员监测记录 ----------
def insert_detection(
    lamp_id: str,
    ts: datetime,
    person_count: int,
    max_confidence: float,
    original_image: str | None,
    processed_image: str | None,
) -> int:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO person_detections (lamp_id, ts, person_count, max_confidence, original_image, processed_image) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (lamp_id, ts, person_count, max_confidence, original_image, processed_image),
            )
            return cur.lastrowid
    finally:
        conn.close()


def query_detections(lamp_id: str | None, start: str | None, end: str | None,
                     keyword: str | None = None, page: int = 1,
                     page_size: int = 10) -> tuple[list[dict], int]:
    """分页查询人员监测记录，返回 (items, total)。keyword 模糊匹配灯杆编号。"""
    conn = get_pool().connection()
    where = " WHERE 1=1"
    params: list = []
    if lamp_id:
        where += " AND lamp_id = %s"
        params.append(lamp_id)
    if start:
        where += " AND ts >= %s"
        params.append(start)
    if end:
        where += " AND ts <= %s"
        params.append(end)
    if keyword:
        where += " AND lamp_id LIKE %s"
        params.append(f"%{keyword}%")
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM person_detections{where}", params)
            total = cur.fetchone()["n"]
            offset = (page - 1) * page_size
            sql = ("SELECT id, lamp_id, DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, person_count, "
                   "max_confidence FROM person_detections"
                   + where + " ORDER BY ts DESC LIMIT %s OFFSET %s")
            cur.execute(sql, params + [page_size, offset])
            items = list(cur.fetchall())
    finally:
        conn.close()
    return items, total


def get_detection(detection_id: int) -> dict | None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, lamp_id, DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, person_count, "
                "max_confidence, original_image, processed_image FROM person_detections WHERE id=%s",
                (detection_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


# ---------- 阈值配置 ----------
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


# ---------- 设备控制日志 ----------
def insert_control_log(lamp_id: str, action: str, result: str, detail: str | None = None) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO control_log (lamp_id, ts, action, result, detail) VALUES (%s, %s, %s, %s, %s)",
                (lamp_id, datetime.now(), action, result, detail),
            )
    finally:
        conn.close()


def query_control_log(lamp_id: str | None = None, start: str | None = None,
                      end: str | None = None, keyword: str | None = None,
                      page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
    """分页查询设备操作日志，返回 (items, total)。keyword 模糊匹配灯杆/指令/说明。"""
    conn = get_pool().connection()
    where = " WHERE 1=1"
    params: list = []
    if lamp_id:
        where += " AND lamp_id = %s"
        params.append(lamp_id)
    if start:
        where += " AND ts >= %s"
        params.append(start)
    if end:
        where += " AND ts <= %s"
        params.append(end)
    if keyword:
        where += " AND (lamp_id LIKE %s OR action LIKE %s OR result LIKE %s OR detail LIKE %s)"
        params += [f"%{keyword}%"] * 4
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM control_log{where}", params)
            total = cur.fetchone()["n"]
            offset = (page - 1) * page_size
            sql = ("SELECT lamp_id, DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, action, result, detail "
                   "FROM control_log" + where + " ORDER BY id DESC LIMIT %s OFFSET %s")
            cur.execute(sql, params + [page_size, offset])
            items = list(cur.fetchall())
    finally:
        conn.close()
    return items, total


# ---------- 统计 ----------
def query_stats(lamp_id: str, start: str, end: str) -> dict:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT AVG(temperature) AS avg_temp, MAX(temperature) AS max_temp, "
                "MIN(temperature) AS min_temp, AVG(humidity) AS avg_humidity, "
                "MAX(luminance) AS max_luminance, COUNT(*) AS sample_count "
                "FROM lamp_sensors WHERE lamp_id=%s AND ts BETWEEN %s AND %s",
                (lamp_id, start, end),
            )
            row = cur.fetchone()
            return {
                "avg_temp": round(row["avg_temp"], 2) if row["avg_temp"] is not None else None,
                "max_temp": round(row["max_temp"], 2) if row["max_temp"] is not None else None,
                "min_temp": round(row["min_temp"], 2) if row["min_temp"] is not None else None,
                "avg_humidity": round(row["avg_humidity"], 2) if row["avg_humidity"] is not None else None,
                "max_luminance": round(row["max_luminance"], 1) if row["max_luminance"] is not None else None,
                "sample_count": row["sample_count"] or 0,
            }
    finally:
        conn.close()