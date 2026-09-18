"""Modbus 从站模拟器：无需真实硬件即可联调/演示外部设备接入。

同时兼容两种报文：
  - Modbus TCP（MBAP 头）
  - Modbus RTU over TCP（串口服务器透明传输的常见形态）

支持 FC1/2（读线圈/离散输入）、FC3/4（读保持/输入寄存器）、FC5（写单线圈）、
FC6（写单寄存器）、FC16（写多寄存器）。

用法：
    python backend/tests/modbus_sim.py --port 15020          # 前台运行
    from modbus_sim import ModbusSim; sim = ModbusSim(15020); sim.start(); ...
"""
import argparse
import socketserver
import struct
import threading

sys_path_ok = True
try:
    from gateway import crc16            # 与后端同一份 CRC 实现（作为脚本运行时）
except Exception:                        # noqa: BLE001
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from gateway import crc16            # type: ignore


class _State:
    """模拟从站的寄存器/线圈内存（默认值模拟一台流量计 + 一台液位计 + 一路继电器）。"""

    def __init__(self):
        self.holding = [0] * 100
        self.input_regs = [0] * 100
        self.coils = [0] * 100
        self.discrete = [0] * 100
        # 默认：保持寄存器0=1234(×0.1=123.4 L/min)、1=856(×0.1=85.6 %)、2=300、3=42
        for addr, value in ((0, 1234), (1, 856), (2, 300), (3, 42)):
            self.holding[addr] = value
        self.input_regs[0] = 1024
        self.discrete[0] = 1

    def dump(self) -> dict:
        return {"holding": list(self.holding), "input": list(self.input_regs),
                "coils": list(self.coils), "discrete": list(self.discrete)}


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            while True:
                data = self.request.recv(256)
                if not data:
                    return
                resp = self._handle_frame(data)
                if resp:
                    self.request.sendall(resp)
        except Exception:  # noqa: BLE001
            return

    def _handle_frame(self, data: bytes) -> bytes:
        state: _State = self.server.state
        mbap = len(data) > 8 and data[2:4] == b"\x00\x00"
        if mbap:
            trans, proto, length, unit = struct.unpack(">HHHB", data[:7])
            pdu = data[7:7 + length - 1]
            raw = pdu
        else:
            unit = data[0]
            body = data[:-2]                     # 去掉 CRC
            want = crc16(body)
            got = data[-2] | (data[-1] << 8)
            if want != got:
                return b""
            raw = body[1:]
        func = raw[0]
        try:
            out = self._exec(state, func, raw)
        except ValueError as exc:
            out = bytes([func | 0x80, getattr(exc, "args", [1])[0] if exc.args else 1])
        if mbap:
            return struct.pack(">HHHB", trans, 0, len(out) + 1, unit) + out
        frame = bytes([unit]) + out
        crc = crc16(frame)
        return frame + bytes([crc & 0xFF, crc >> 8])

    @staticmethod
    def _exec(state: _State, func: int, raw: bytes) -> bytes:
        if func in (1, 2):
            addr, count = struct.unpack(">HH", raw[1:5])
            src = state.coils if func == 1 else state.discrete
            bits = [src[a] if a < len(src) else 0 for a in range(addr, addr + count)]
            nbytes = (count + 7) // 8
            payload = bytearray(nbytes)
            for i, bit in enumerate(bits):
                if bit:
                    payload[i // 8] |= 1 << (i % 8)
            return bytes([func, nbytes]) + bytes(payload)
        if func in (3, 4):
            addr, count = struct.unpack(">HH", raw[1:5])
            src = state.holding if func == 3 else state.input_regs
            if addr + count > len(src):
                raise ValueError(2)
            vals = src[addr:addr + count]
            return bytes([func, count * 2]) + b"".join(struct.pack(">H", v) for v in vals)
        if func == 5:
            addr, value = struct.unpack(">HH", raw[1:5])
            if addr >= len(state.coils):
                raise ValueError(2)
            state.coils[addr] = 1 if value == 0xFF00 else 0
            return raw[:5]
        if func == 6:
            addr, value = struct.unpack(">HH", raw[1:5])
            if addr >= len(state.holding):
                raise ValueError(2)
            state.holding[addr] = value
            return raw[:5]
        if func == 16:
            addr, count, nbytes = struct.unpack(">HHB", raw[1:6])
            payload = raw[6:6 + nbytes]
            for i in range(count):
                state.holding[addr + i] = struct.unpack(">H", payload[i * 2:i * 2 + 2])[0]
            return raw[:5]
        raise ValueError(1)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ModbusSim:
    """一行启动的模拟从站：sim = ModbusSim(port); sim.start() / sim.stop()。"""

    def __init__(self, port: int = 15020, host: str = "127.0.0.1"):
        self.host, self.port = host, port
        self.state = _State()
        self._srv: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "ModbusSim":
        self._srv = _Server((self.host, self.port), _Handler)
        self._srv.state = self.state
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    # 供测试/演示直接读写内存
    def set_register(self, addr: int, value: int) -> None:
        self.state.holding[addr] = value

    def coil(self, addr: int) -> int:
        return self.state.coils[addr]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Modbus 从站模拟器（TCP + RTU over TCP）")
    ap.add_argument("--port", type=int, default=15020)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    sim = ModbusSim(args.port, args.host).start()
    print(f"Modbus 模拟从站已启动: {args.host}:{args.port}（Ctrl+C 停止）")
    print("默认寄存器: holding[0]=1234 holding[1]=856 holding[2]=300 holding[3]=42 input[0]=1024")
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        sim.stop()
        print("已停止")
