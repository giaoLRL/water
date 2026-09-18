"""外部设备接入适配器：把 Modbus（RS485/RS232 工业传感器与继电器板）接入本系统。

比赛现场若换了传感器/执行器（4-20mA 压力变送器、脉冲流量计、485 继电器板等），
无需改代码，只在系统配置页填 IP/端口/从站号/寄存器地址即可。

支持三种模式：
  - "tcp"        Modbus TCP（标准 502，报文带 7 字节 MBAP 头）
  - "rtu_tcp"    串口服务器透明传输（把 RTU 帧直接发到 TCP 端口，现场最常见）
  - "rtu_serial" 本机串口直连（需 pyserial；未安装时该模式明确报错，不静默失败）

配置存 config 表 sys.gateways（JSON 数组）：
  {id, name, mode, host, port, unit, device, baud, enabled, period,
   polls: [{key, func, addr, count, type, scale, offset, target}]}
  - func：1=读线圈 2=读离散输入 3=读保持寄存器 4=读输入寄存器
  - type：u16 / s16 / u32 / u32_swap / f32 / f32_swap（多寄存器时的拼接顺序）
  - target：本机通道键（flow_rate/pressure/storage_temp/heater_temp/light/level_heater）
            或 "custom:<卡片id>"（写成自定义通道，走历史/统计/联动）
取值 = 原始值 × scale + offset；读失败返回 None（不写库、不伪造）。
"""
import socket
import struct
import threading
import time

import database
import store

MODE_TCP = "tcp"
MODE_RTU_TCP = "rtu_tcp"
MODE_RTU_SERIAL = "rtu_serial"
MODES = (MODE_TCP, MODE_RTU_TCP, MODE_RTU_SERIAL)
REG_TYPES = ("u16", "s16", "u32", "u32_swap", "f32", "f32_swap")
FUNCS = (1, 2, 3, 4)
HOST_CHANNELS = ("flow_rate", "total_flow", "pressure", "storage_temp",
                 "heater_temp", "light", "level_heater", "level_storage")


def crc16(data: bytes) -> int:
    """Modbus RTU CRC16（多项式 0xA001）。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_read(unit: int, func: int, addr: int, count: int) -> bytes:
    """构造读请求帧（RTU，含 CRC）。"""
    body = struct.pack(">BBHH", unit & 0xFF, func, addr & 0xFFFF, count & 0xFFFF)
    crc = crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


def build_write_coil(unit: int, addr: int, on: bool) -> bytes:
    """构造写单线圈帧（FC5，用于 485 继电器板）。"""
    body = struct.pack(">BBHH", unit & 0xFF, 5, addr & 0xFFFF, 0xFF00 if on else 0x0000)
    crc = crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


def build_write_reg(unit: int, addr: int, value: int) -> bytes:
    """构造写单寄存器帧（FC6）。"""
    body = struct.pack(">BBHH", unit & 0xFF, 6, addr & 0xFFFF, value & 0xFFFF)
    crc = crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


def parse_read(resp: bytes, func: int, count: int):
    """解析读响应，返回寄存器值列表（线圈类返回 0/1 列表）；异常帧抛 ValueError。"""
    if len(resp) < 5:
        raise ValueError("响应过短")
    if resp[1] & 0x80:
        raise ValueError(f"从站异常码 0x{resp[2]:02X}")
    if func in (3, 4):
        nbytes = resp[2]
        if nbytes < count * 2 or len(resp) < 3 + nbytes:
            raise ValueError("寄存器数量不符")
        payload = resp[3:3 + nbytes]
        return [int.from_bytes(payload[i * 2:i * 2 + 2], "big") for i in range(count)]
    if func in (1, 2):
        nbytes = resp[2]
        if len(resp) < 3 + nbytes:
            raise ValueError("位数据不足")
        bits = []
        for byte in resp[3:3 + nbytes]:
            for i in range(8):
                bits.append((byte >> i) & 1)
        return bits[:count] if count else bits
    raise ValueError(f"不支持的功能码 {func}")


def decode(values: list[int], dtype: str) -> float:
    """把寄存器按类型解码为数值（多寄存器默认高字在前，_swap 为低字在前）。"""
    dtype = dtype or "u16"
    if dtype == "u16":
        return float(values[0])
    if dtype == "s16":
        v = values[0]
        return float(v - 65536 if v >= 32768 else v)
    if dtype in ("u32", "u32_swap", "f32", "f32_swap"):
        if len(values) < 2:
            raise ValueError(f"{dtype} 需要 2 个寄存器")
        hi, lo = (values[0], values[1]) if not dtype.endswith("swap") else (values[1], values[0])
        raw = (hi << 16) | lo
        if dtype.startswith("f32"):
            return float(struct.unpack(">f", struct.pack(">I", raw))[0])
        return float(raw)
    raise ValueError(f"不支持的数据类型 {dtype}")


def load_gateways() -> list[dict]:
    """读取外部设备配置（无配置返回空列表）。"""
    try:
        items = store.get_json("gateways", [])
    except Exception:  # noqa: BLE001
        items = []
    out = []
    for ep in items if isinstance(items, list) else []:
        if isinstance(ep, dict) and ep.get("id") and ep.get("mode") in MODES:
            out.append(ep)
    return out


def _transact(ep: dict, frame: bytes, expect: int, timeout: float) -> bytes:
    """一次请求-响应（TCP / 串口服务器）；串口模式走 pyserial。"""
    mode = ep.get("mode")
    if mode == MODE_RTU_SERIAL:
        try:
            import serial  # type: ignore  # 现场可选依赖
        except Exception as exc:  # noqa: BLE001
            raise ValueError("串口模式需要 pyserial（pip install pyserial），或改用串口服务器 TCP 模式") from exc
        with serial.Serial(ep.get("device") or "COM3", int(ep.get("baud") or 9600),
                           timeout=timeout) as ser:
            ser.reset_input_buffer()
            ser.write(frame)
            return ser.read(expect)
    unit = int(ep.get("unit") or 1)
    payload = frame
    if mode == MODE_TCP:               # Modbus TCP：MBAP 头 + PDU（去掉 RTU 的单元号与 CRC）
        pdu = frame[1:-2]
        payload = struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit) + pdu
    with socket.create_connection((ep.get("host"), int(ep.get("port") or 502)), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        data = sock.recv(256)
    if mode == MODE_TCP:
        if len(data) < 8:
            raise ValueError("Modbus TCP 响应过短")
        return bytes([data[6]]) + data[7:]      # 还原为 RTU 形态（单元号 + PDU），便于统一解析
    return data


def check_write_response(resp: bytes, func: int) -> None:
    """校验写响应：长度足够且无异常位（回显帧即可视为成功）。"""
    if len(resp) < 6:
        raise ValueError("写响应过短")
    if resp[1] & 0x80:
        raise ValueError(f"从站异常码 0x{resp[2]:02X}")
    if resp[1] != func:
        raise ValueError(f"写响应功能码不符: {resp[1]}")


class ModbusGateway:
    """按周期轮询外部 Modbus 设备，把读数并入采集快照（target 决定落在哪）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._endpoints: list[dict] = []
        self._values: dict[str, float] = {}      # target -> 最近一次有效读数
        self._ts: dict[str, float] = {}          # 设备id -> 上次轮询时间
        self._last_error: dict[str, str] = {}    # 设备id -> 最近一次错误（界面/接口可见）
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._endpoints = [ep for ep in load_gateways() if ep.get("enabled", True)]

    def endpoints(self) -> list[dict]:
        with self._lock:
            return list(self._endpoints)

    def values(self) -> dict:
        with self._lock:
            return dict(self._values)

    def errors(self) -> dict:
        with self._lock:
            return dict(self._last_error)

    def poll(self) -> None:
        """采集循环每周期调用；按各设备 period（默认 5s）节流。"""
        now = time.time()
        with self._lock:
            tasks = [(ep, self._ts.get(ep["id"], 0.0)) for ep in self._endpoints]
        for ep, last in tasks:
            try:
                period = max(1.0, float(ep.get("period") or 5))
            except (TypeError, ValueError):
                period = 5.0
            if now - last < period:
                continue
            with self._lock:
                self._ts[ep["id"]] = now
            try:
                fresh = self.read_endpoint(ep)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._last_error[ep["id"]] = str(exc)[:200]
                continue
            with self._lock:
                self._last_error.pop(ep["id"], None)
                self._values.update(fresh)
            # 映射到自定义通道的读数同样入库，历史曲线/统计/联动全链路可用
            for key, value in fresh.items():
                if key.startswith("custom:"):
                    try:
                        database.insert_custom_sensor(key.split(":", 1)[1], value)
                    except Exception:  # noqa: BLE001
                        pass    # 入库失败不影响采集

    def read_endpoint(self, ep: dict) -> dict:
        """读一台设备的全部采集项，返回 {target: 工程值}；单点失败不影响其他点。"""
        timeout = max(0.2, float(ep.get("timeout") or 1.0))
        unit = int(ep.get("unit") or 1)
        out: dict[str, float] = {}
        for item in ep.get("polls") or []:
            if not isinstance(item, dict) or not item.get("target"):
                continue
            func = int(item.get("func") or 3)
            if func not in FUNCS:
                raise ValueError(f"采集项功能码非法: {func}")
            addr = int(item.get("addr") or 0)
            count = int(item.get("count") or 1)
            frame = build_read(unit, func, addr, count)
            expect = 3 + count * 2 if func in (3, 4) else 3 + (count + 7) // 8 + 2
            resp = _transact(ep, frame, max(expect, 5), timeout)
            values = parse_read(resp, func, count)
            raw = decode(values, item.get("type") or "u16")
            scale = float(item.get("scale") if item.get("scale") is not None else 1)
            offset = float(item.get("offset") or 0)
            out[str(item["target"])] = raw * scale + offset
        return out

    def write_coil(self, ep_id: str, addr: int, on: bool) -> dict:
        """写线圈（485 继电器板控制）；返回执行结果。"""
        ep = next((e for e in self.endpoints() if e.get("id") == ep_id), None)
        if not ep:
            raise ValueError(f"外部设备不存在: {ep_id}")
        timeout = max(0.2, float(ep.get("timeout") or 1.0))
        unit = int(ep.get("unit") or 1)
        frame = build_write_coil(unit, int(addr), on)
        resp = _transact(ep, frame, 8, timeout)
        check_write_response(resp, 5)
        return {"device": ep_id, "addr": int(addr), "state": "on" if on else "off"}

    def write_register(self, ep_id: str, addr: int, value: int) -> dict:
        """写单个保持寄存器（如设定值下发）。"""
        ep = next((e for e in self.endpoints() if e.get("id") == ep_id), None)
        if not ep:
            raise ValueError(f"外部设备不存在: {ep_id}")
        timeout = max(0.2, float(ep.get("timeout") or 1.0))
        unit = int(ep.get("unit") or 1)
        frame = build_write_reg(unit, int(addr), int(value))
        resp = _transact(ep, frame, 8, timeout)
        check_write_response(resp, 6)
        return {"device": ep_id, "addr": int(addr), "value": int(value)}
