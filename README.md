# 基于物联网的分布式智慧灯杆监控系统

依据《基于物联网的分布式智慧灯杆监控系统》赛题实现的前后端完整系统。多个分散的智慧灯杆作为独立监控单元，通过物联网技术实现环境监测、异常报警、视频监控、人员智能监测与远程控制。

## 功能特性

- **智慧灯杆导航与详情**：列表形式展示全部灯杆，点击进入详情查看实时状态、环境参数、视频监控。
- **环境参数监测**：实时采集每个灯杆附近的温湿度、光照强度，连续上传至服务器并入库。
- **异常报警**：温度 / 湿度 / 光照超过预设阈值时自动告警，Web 前端提示并持久化记录。
- **视频监控与人员监测**：摄像头提供实时视频流；可截图并通过 HTTP 调用智能识别接口监测人员，框选标记并记录。
- **数据存储与查询**：传感器数据、报警信息、人员监测记录、设备操作日志全部入库，支持历史查询与统计分析。
- **设备控制**：远程控制每个灯杆灯光（电磁阀）的开启 / 关闭。

## 技术栈

| 层次 | 选型 |
| --- | --- |
| 前端 | Vue 3（本地库，无构建）+ ECharts，PC / 移动端自适应 |
| 后端 | Python 3.11+ FastAPI + Uvicorn（自带 `/docs`） |
| 数据库 | MySQL 8.x（PyMySQL 连接池），库名 `iot_system` |
| 智能识别 | YOLO 推理服务，HTTP 接口 `POST /infer` |

## 快速启动

### 1. 启动智能识别服务（YOLO，可选）

前置：已安装 `ultralytics`、`flask`，模型文件 `yolo11n.pt` 位于项目根目录：

```powershell
python server.py
# 服务监听 http://127.0.0.1:5000/infer
```

### 2. 启动数据库

本地 MySQL 需已就绪（创建 `iot_system` 库及 `iot_user` 账号，见 `start_local_db.ps1` 或在 MySQL 中执行）：

```sql
CREATE DATABASE IF NOT EXISTS iot_system DEFAULT CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'iot_user'@'127.0.0.1' IDENTIFIED BY 'iot_pass_2026';
GRANT ALL ON iot_system.* TO 'iot_user'@'127.0.0.1';
FLUSH PRIVILEGES;
```

### 3. 安装后端依赖并启动

```powershell
python -m pip install -r backend\requirements.txt
cd backend
python main.py
```

浏览器访问 <http://127.0.0.1:8000>，接口文档 <http://127.0.0.1:8000/docs>。灯杆 01 使用真实 RTSP 摄像头，其余灯杆使用模拟画面与模拟传感数据，便于演示。

### 4. 环境变量（可选）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `IOT_DB_HOST/PORT/USER/PASSWORD/NAME` | 127.0.0.1 / 3306 / iot_user / iot_pass_2026 / iot_system | 数据库连接 |
| `LAMP_SAMPLE_INTERVAL` | `2.0` | 采集周期（秒） |
| `LAMP_INFER_URL` | `http://127.0.0.1:5000/infer` | 智能识别接口地址 |

## 核心接口

| 接口 | 方法 | 功能 |
| --- | --- | --- |
| `/api/lampposts` | GET | 灯杆列表（实时概要） |
| `/api/lampposts/{id}` | GET | 灯杆详情 |
| `/api/lampposts/{id}/history?start=&end=` | GET | 历史传感数据查询 |
| `/api/lampposts/{id}/stats?start=&end=` | GET | 统计指标 |
| `/api/lampposts/{id}/control` | POST | 灯光开 / 关 |
| `/api/lampposts/{id}/video` | GET | MJPEG 实时视频流 |
| `/api/lampposts/{id}/snapshot` | GET | 截图（返回 base64） |
| `/api/lampposts/{id}/detect` | POST | 截图并调用智能识别接口监测人员 |
| `/api/detections` | GET | 人员监测记录查询 |
| `/api/alarm/config` | GET/POST | 告警阈值读写 |
| `/api/alarms` | GET | 告警记录查询 |
| `/api/logs` | GET | 设备操作日志 |
| `/api/system` | GET | 系统状态 |

统一返回格式：`{"code": 0, "msg": "ok", "data": {}}`。

## 测试

```powershell
# 后端接口测试（需先启动后端）
python backend\tests\test_api.py
```

## 目录结构

```text
backend/        FastAPI 后端（配置 / 数据库 / 灯杆设备 / 告警 / API）
frontend/       Vue 3 + ECharts 前端（灯杆列表 + 详情 SPA）
scripts/        MySQL 与后端启动脚本
```