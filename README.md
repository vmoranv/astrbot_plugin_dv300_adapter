# astrbot_plugin_dv300_adapter

AstrBot 平台适配器插件：将 OHOS DV300 设备侧 `astrbot_adapter`（UDP 协议）接入 AstrBot。

## 功能

- 监听 DV300 设备发来的 UDP 协议消息（HELLO/HEARTBEAT/CAPABILITY/ACK/ERROR）
- 可选接收媒体帧事件（默认关闭，防止刷屏）
- 将消息转换为 AstrBot `AstrBotMessage` 并提交事件队列
- 支持从 AstrBot 侧发送命令到设备：
  - `start_camera` / `stop_camera`
  - `start_mic` / `stop_mic`
  - `query_capability`
  - `ping`

## 插件结构

- `main.py`：插件入口，导入并注册平台适配器
- `dv300_platform_adapter.py`：平台适配器实现
- `dv300_platform_event.py`：事件发送实现
- `client.py`：UDP 与协议编解码
- `metadata.yaml`：插件元数据

## 使用方式

1. 把整个目录放到 AstrBot 插件目录。
2. 在 AstrBot 中启用插件。
3. 在平台适配器配置中选择 `dv300`，填写配置：
   - `local_bind_ip`：默认 `0.0.0.0`
   - `local_bind_port`：默认 `19091`
   - `device_ip`：设备 IP（默认 `127.0.0.1`）
   - `device_port`：设备端口（默认 `19090`）
   - `emit_media_events`：是否把媒体帧上报为消息（默认 `false`）
4. 启动设备端 `astrbot_adapter` 后观察日志是否收到 hello/capability。

## 联调建议

- 先发 `query_capability` 与 `ping` 验证链路。
- 媒体帧建议默认关闭；需要观察时再打开 `emit_media_events`。
- 如果设备端不在同机，请确保 UDP 端口和防火墙放通。
