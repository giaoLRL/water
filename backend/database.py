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
HISTORY_MAX_ROWS = 200_000   # 单次历史查询硬上限（1Hz 下约 55 小时），超出时保留最新部分


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


def _column_info(cur, table: str, column: str) -> dict | None:
    """查询列定义(是否为 NULL)，列不存在时返回 None。"""
    cur.execute(
        "SELECT IS_NULLABLE AS nullable FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
        (config.DB_NAME, table, column),
    )
    return cur.fetchone()


def _migrate_water_sensors(cur) -> None:
    """water_sensors 表结构迁移（幂等，可重复执行，不丢历史数据）。

    迁移原因：设备离线或读数无效期间温度/压力/加热/光照通道无数据。
    1) storage_temp / heater_temp / pressure / heater_state / flow_rate / total_flow
       保持可空，无数据的通道写 NULL，避免 0 值污染统计与曲线；
    2) 新增 pump_target 列，记录当时的定量浇水目标，便于回溯；
    3) 新增 light 列（光照 lx，/api/light 通道）。
    """
    # 列名 -> 完整列定义（类型必须与建表语句一致，勿强行统一为 DOUBLE）
    nullable_columns = {
        "storage_temp": "DOUBLE NULL",
        "heater_temp": "DOUBLE NULL",
        "pressure": "DOUBLE NULL",
        "heater_state": "VARCHAR(8) NULL",
        "flow_rate": "DOUBLE NULL",
        "total_flow": "DOUBLE NULL",
    }
    for column, definition in nullable_columns.items():
        info = _column_info(cur, "water_sensors", column)
        if info is not None and info["nullable"] != "YES":
            cur.execute(f"ALTER TABLE water_sensors MODIFY COLUMN `{column}` {definition}")

    if _column_info(cur, "water_sensors", "pump_target") is None:
        cur.execute("ALTER TABLE water_sensors ADD COLUMN pump_target DOUBLE NULL AFTER total_flow")

    if _column_info(cur, "water_sensors", "light") is None:
        cur.execute("ALTER TABLE water_sensors ADD COLUMN light DOUBLE NULL AFTER pressure")


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
            # 可空列说明：设备离线或单通道读数无效（503/负值）时对应通道写 NULL，
            # 不用 0 伪装成真实读数，避免污染曲线与统计。
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS water_sensors (
                    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    ts           DATETIME NOT NULL,
                    storage_temp DOUBLE NULL,
                    heater_temp  DOUBLE NULL,
                    flow_rate    DOUBLE NULL,
                    pressure     DOUBLE NULL,
                    light        DOUBLE NULL,
                    pump_state   VARCHAR(8) NOT NULL,
                    heater_state VARCHAR(8) NULL,
                    total_flow   DOUBLE NULL,
                    pump_target  DOUBLE NULL,
                    PRIMARY KEY (id),
                    KEY idx_ts (ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            _migrate_water_sensors(cur)
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
            # 自定义传感器数据（前端声明通道，后端按周期轮询入库；全链路：历史/统计/联动）
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS custom_sensor_data (
                    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    channel_id VARCHAR(48) NOT NULL,
                    ts         DATETIME NOT NULL,
                    value      DOUBLE NULL,
                    PRIMARY KEY (id),
                    KEY idx_custom (channel_id, ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 操作日志（设备控制 + 系统配置 + 账号管理）
            # operator: 操作账号名；系统自动动作(恒温闭环/判定服务)为空
            # source  : manual(人工) / auto(本地自动) / judge(判定服务)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS control_log (
                    id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    device_id VARCHAR(16) NOT NULL DEFAULT 'water',
                    ts       DATETIME NOT NULL,
                    action   VARCHAR(32) NOT NULL,
                    result   VARCHAR(16) NOT NULL,
                    detail   VARCHAR(255),
                    `operator` VARCHAR(64) NULL,
                    `source`   VARCHAR(16) NOT NULL DEFAULT 'manual',
                    PRIMARY KEY (id),
                    KEY idx_ctrl_ts (ts),
                    KEY idx_ctrl_op (`operator`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            _migrate_control_log(cur)
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


def _migrate_control_log(cur) -> None:
    """control_log 表结构迁移（幂等，不丢历史数据）。

    迁移原因：原日志只记录"做了什么"，没有"谁做的"，账号权限形同虚设。
    1) 新增 `operator`(操作账号名) 与 `source`(manual/auto/judge)；
    2) 历史行没有操作人，保持 NULL，前端显示"—"（不做数据猜测）。
    """
    if _column_info(cur, "control_log", "operator") is None:
        cur.execute("ALTER TABLE control_log ADD COLUMN `operator` VARCHAR(64) NULL AFTER detail")
    if _column_info(cur, "control_log", "source") is None:
        cur.execute("ALTER TABLE control_log ADD COLUMN `source` VARCHAR(16) NOT NULL "
                    "DEFAULT 'manual' AFTER `operator`")


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
    ts: datetime, storage_temp: float | None, heater_temp: float | None,
    flow_rate: float | None, pressure: float | None,
    pump_state: str, heater_state: str | None, total_flow: float | None,
    pump_target: float | None = None, light: float | None = None,
) -> None:
    """写入一条采样。设备不支持的通道传 None（存 NULL，不伪造 0 值）。"""
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO water_sensors (ts, storage_temp, heater_temp, flow_rate, pressure, "
                "pump_state, heater_state, total_flow, pump_target, light) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (ts, storage_temp, heater_temp, flow_rate, pressure,
                 pump_state, heater_state, total_flow, pump_target, light),
            )
    finally:
        conn.close()


def query_water_history(start: str, end: str, limit: int = 5000) -> list[dict]:
    """查询区间传感数据（按时间升序返回）。

    超过 limit 时**保留最新的 limit 条**——此前用 `ORDER BY ts ASC LIMIT` 取到的是最早的数据，
    长区间曲线会"停在几小时前"，最新的点被丢掉（已修正为 DESC 取数后再升序返回）。
    """
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, flow_rate, "
                "total_flow, pump_target, pump_state, storage_temp, heater_temp, "
                "pressure, heater_state, light "
                "FROM water_sensors WHERE ts BETWEEN %s AND %s "
                "ORDER BY ts DESC LIMIT %s",
                (start, end, limit),
            )
            rows = list(cur.fetchall())
    finally:
        conn.close()
    rows.reverse()
    return rows


def count_water_history(start: str, end: str) -> int:
    """区间内总行数（导出/取样提示"是否截断"用）。"""
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM water_sensors WHERE ts BETWEEN %s AND %s",
                        (start, end))
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def count_water_history_by_channel(start: str, end: str) -> dict:
    """各通道有效读数点数（区别于含离线空行的总行数）。"""
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS total, COUNT(flow_rate) AS flow, COUNT(storage_temp) AS storage_temp, "
                "COUNT(heater_temp) AS heater_temp, COUNT(pressure) AS pressure, COUNT(light) AS light "
                "FROM water_sensors WHERE ts BETWEEN %s AND %s",
                (start, end),
            )
            return cur.fetchone()
    finally:
        conn.close()


def weighted_avg_flow(start: str, end: str, max_gap_s: float = 60.0) -> float | None:
    """按时间加权的区间平均流量。

    每个采样值按其到下一个采样的时长加权（末点用中位间隔）；
    间隔超过 max_gap_s（默认 60s，多为设备离线空档）的片段不计入，避免旧值长时间占权。
    """
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT UNIX_TIMESTAMP(ts) AS t, flow_rate FROM water_sensors "
                "WHERE ts BETWEEN %s AND %s AND flow_rate IS NOT NULL ORDER BY ts ASC",
                (start, end),
            )
            rows = list(cur.fetchall())
    finally:
        conn.close()
    if not rows:
        return None
    gaps = [float(rows[i + 1]["t"]) - float(rows[i]["t"]) for i in range(len(rows) - 1)]
    typical = sorted(g for g in gaps if 0 < g <= max_gap_s)
    default_gap = typical[len(typical) // 2] if typical else 1.0
    total_w = total_v = 0.0
    for i, row in enumerate(rows):
        gap = gaps[i] if i < len(gaps) else default_gap
        if gap <= 0 or gap > max_gap_s:
            continue
        total_w += gap
        total_v += float(row["flow_rate"]) * gap
    if total_w <= 0:
        return None
    return round(total_v / total_w, 3)


def _sample_rows(rows: list[dict], points: int) -> tuple[list[dict], bool]:
    """等距降采样（保留首尾）；返回 (取样结果, 是否发生降采样)。"""
    if points <= 0 or len(rows) <= points:
        return rows, False
    step = len(rows) / float(points)
    picked = [rows[min(int(i * step), len(rows) - 1)] for i in range(points)]
    picked[-1] = rows[-1]              # 末点必须是区间内最新一条
    return picked, True


def query_water_history_chart(start: str, end: str, points: int = 1500) -> dict:
    """图表用取样：覆盖整个区间并降采样到 points 点（保证含最新数据）。"""
    rows = query_water_history(start, end, HISTORY_MAX_ROWS)
    picked, sampled = _sample_rows(rows, points)
    return {"points": picked, "raw_count": len(rows), "sampled": sampled}


def query_sensor_at(ts: str) -> dict | None:
    """回放：取 ts 时刻最近的一条传感数据（ts <= 目标时间）。"""
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, flow_rate, "
                "total_flow, pump_target, pump_state, storage_temp, heater_temp, "
                "pressure, heater_state, light FROM water_sensors "
                "WHERE ts <= %s ORDER BY ts DESC LIMIT 1",
                (ts,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def query_custom_at(channel_id: str, ts: str) -> float | None:
    """回放：取某自定义通道在 ts 时刻最近的值。"""
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM custom_sensor_data WHERE channel_id=%s AND ts <= %s "
                "ORDER BY ts DESC LIMIT 1",
                (channel_id, ts),
            )
            row = cur.fetchone()
            return row["value"] if row else None
    finally:
        conn.close()


def query_history_frames(start: str, end: str, limit: int = 120) -> dict:
    """回放帧：覆盖整个区间并降采样到约 limit 帧（含最新一帧）。"""
    rows = query_water_history(start, end, HISTORY_MAX_ROWS)
    picked, sampled = _sample_rows(rows, limit)
    return {"frames": picked, "raw_count": len(rows), "sampled": sampled}


def query_water_stats(start: str, end: str) -> dict:
    """统计面板：区间内流量均值/极值、累计水量增量、温度/压力指标、采样点数。

    设备离线期间无数据的通道聚合结果为 None（前端显示"--"）。
    """
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT AVG(flow_rate) AS avg_flow, MAX(flow_rate) AS max_flow, "
                "MIN(flow_rate) AS min_flow, COUNT(*) AS samples, "
                "SUM(flow_rate) AS sum_flow, "
                "AVG(storage_temp) AS avg_st, MAX(storage_temp) AS max_st, MIN(storage_temp) AS min_st, "
                "AVG(heater_temp) AS avg_ht, MAX(heater_temp) AS max_ht, MIN(heater_temp) AS min_ht, "
                "MAX(pressure) AS max_p, MIN(pressure) AS min_p, "
                "AVG(light) AS avg_l, MAX(light) AS max_l, MIN(light) AS min_l, "
                "(SELECT total_flow FROM water_sensors WHERE ts BETWEEN %s AND %s "
                " AND total_flow IS NOT NULL ORDER BY id ASC LIMIT 1) AS start_total, "
                "(SELECT total_flow FROM water_sensors WHERE ts BETWEEN %s AND %s "
                " AND total_flow IS NOT NULL ORDER BY id DESC LIMIT 1) AS end_total "
                "FROM water_sensors WHERE ts BETWEEN %s AND %s",
                (start, end, start, end, start, end),
            )
            row = cur.fetchone()
            # 各通道有效读数点数（COUNT(列) 自动忽略 NULL，即排除设备离线写入的空行）
            cur.execute(
                "SELECT COUNT(*) AS total, COUNT(flow_rate) AS flow, COUNT(storage_temp) AS storage_temp, "
                "COUNT(heater_temp) AS heater_temp, COUNT(pressure) AS pressure, COUNT(light) AS light "
                "FROM water_sensors WHERE ts BETWEEN %s AND %s",
                (start, end),
            )
            valid = cur.fetchone()

            def _round(value, digits=2):
                return round(value, digits) if value is not None else None

            start_total = row["start_total"]
            end_total = row["end_total"]
            # 区间用水量：结束累计 - 开始累计；期间若执行过清零则可能为负，按 0 处理
            used = None
            if start_total is not None and end_total is not None:
                used = round(max(0.0, float(end_total) - float(start_total)), 3)

            return {
                # 流量通道（真实数据）
                "avg_flow": _round(row["avg_flow"]),
                # 时间加权平均流量（采样密度不均时更准确；无有效数据为 None）
                "avg_flow_weighted": weighted_avg_flow(start, end),
                "max_flow": _round(row["max_flow"]),
                "min_flow": _round(row["min_flow"]),
                "sum_flow": _round(row["sum_flow"]),
                "samples": int(row["samples"] or 0),
                # 各通道"有效读数"点数（总行数含设备离线时写入的空行，二者不可混淆）
                "valid_samples": {
                    "flow": int(valid["flow"] or 0),
                    "storage_temp": int(valid["storage_temp"] or 0),
                    "heater_temp": int(valid["heater_temp"] or 0),
                    "pressure": int(valid["pressure"] or 0),
                    "light": int(valid["light"] or 0),
                },
                # 累计水量
                "start_total": _round(start_total, 3),
                "end_total": _round(end_total, 3),
                "volume_used": used,
                "total_flow": _round(end_total, 3),   # 兼容旧字段名
                # 温度/压力通道（有数据则统计，无数据为 None）
                "avg_storage_temp": _round(row["avg_st"]),
                "max_storage_temp": _round(row["max_st"]),
                "min_storage_temp": _round(row["min_st"]),
                "avg_heater_temp": _round(row["avg_ht"]),
                "max_heater_temp": _round(row["max_ht"]),
                "min_heater_temp": _round(row["min_ht"]),
                "max_pressure": _round(row["max_p"], 1),
                "min_pressure": _round(row["min_p"], 1),
                # 光照通道（有数据则统计，无数据为 None）
                "avg_light": _round(row["avg_l"], 1),
                "max_light": _round(row["max_l"], 1),
                "min_light": _round(row["min_l"], 1),
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


# ---------- 操作日志 ----------
def insert_control_log(action: str, result: str, detail: str | None = None,
                       operator: str | None = None, source: str = "manual") -> None:
    """写入一条操作日志。

    operator: 操作账号名；系统自动动作(恒温闭环/判定服务)传 None。
    source  : manual(人工) / auto(本地自动) / judge(判定服务)；
              仅表示动作来源，前端据此显示"操作人"列。
    """
    # detail 列 VARCHAR(255)：统一截断兜底，避免任何调用点超长导致 1406 报错
    if detail and len(detail) > 250:
        detail = detail[:250] + "…"
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO control_log (device_id, ts, action, result, detail, `operator`, `source`) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (DEVICE_ID, datetime.now(), action, result, detail, operator, source),
            )
    finally:
        conn.close()


# ---------- 自定义传感器通道（全链路：声明→轮询入库→历史/统计） ----------
def insert_custom_sensor(channel_id: str, value: float) -> None:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO custom_sensor_data (channel_id, ts, value) VALUES (%s, %s, %s)",
                (channel_id, datetime.now(), float(value)),
            )
    finally:
        conn.close()


def query_custom_history(channel_id: str, start: str, end: str, limit: int = 5000) -> list[dict]:
    """查询自定义通道区间数据（升序返回；超限时保留最新的 limit 条）。"""
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ts, value FROM custom_sensor_data "
                "WHERE channel_id=%s AND ts BETWEEN %s AND %s ORDER BY ts DESC LIMIT %s",
                (channel_id, start, end, limit),
            )
            rows = [{"ts": r["ts"].strftime("%Y-%m-%d %H:%M:%S"), "value": r["value"]}
                    for r in cur.fetchall()]
    finally:
        conn.close()
    rows.reverse()
    return rows


def query_custom_history_chart(channel_id: str, start: str, end: str, points: int = 1500) -> dict:
    """自定义通道图表用取样：覆盖整个区间并降采样到 points 点（保证含最新数据）。"""
    rows = query_custom_history(channel_id, start, end, HISTORY_MAX_ROWS)
    picked, sampled = _sample_rows(rows, points)
    return {"points": picked, "raw_count": len(rows), "sampled": sampled}


def custom_sensor_stats(channel_id: str, start: str, end: str) -> dict:
    conn = get_pool().connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS samples, AVG(value) AS avg_value, MAX(value) AS max_value, "
                "MIN(value) AS min_value FROM custom_sensor_data "
                "WHERE channel_id=%s AND ts BETWEEN %s AND %s",
                (channel_id, start, end),
            )
            row = cur.fetchone()
            return {"samples": row["samples"], "avg_value": row["avg_value"],
                    "max_value": row["max_value"], "min_value": row["min_value"]}
    finally:
        conn.close()


def query_control_log(start: str | None, end: str | None,
                      page: int = 1, page_size: int = 10,
                      category: str | None = None) -> tuple[list[dict], int]:
    """分页查询操作日志，返回 (items, total)。

    category 按 action 前缀区分：device(设备控制) / config(系统配置) /
    account(账号管理)；设备类动作无前缀，配置类为 "config."、账号类为 "account."。
    """
    conn = get_pool().connection()
    where = " WHERE device_id = %s"
    params: list = [DEVICE_ID]
    if start:
        where += " AND ts >= %s"; params.append(start)
    if end:
        where += " AND ts <= %s"; params.append(end)
    if category == "config":
        where += " AND action LIKE %s"; params.append("config.%")
    elif category == "account":
        where += " AND action LIKE %s"; params.append("account.%")
    elif category == "device":
        where += " AND action NOT LIKE %s AND action NOT LIKE %s"
        params += ["config.%", "account.%"]
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM control_log{where}", params)
            total = cur.fetchone()["n"]
            offset = (page - 1) * page_size
            cur.execute(
                "SELECT DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts, action, result, detail, "
                "`operator`, `source` "
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
