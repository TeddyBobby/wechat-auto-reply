# 原生微信接入

默认 `./start.sh` 的只读面板无需这些依赖。下面的流程用于实际接收和回复微信消息。

## 1. 依赖与版本

| 组件 | 要求 |
| --- | --- |
| 系统 | macOS Apple Silicon（arm64） |
| Python | 3.9+ |
| Go | 1.25+ |
| 编译工具 | Xcode Command Line Tools（提供 clang） |
| Frida 开发包 | 17.8.0，构建脚本可自动下载并校验 |
| 微信 | 与 `4_1_12_53_mac.json` 偏移配置匹配的 4.1.12.53 |
| Gadget | 已按上游说明装入兼容的微信，监听本机 `27042` |
| Hermes | 已自行安装并完成模型配置，`hermes` 命令在 PATH 中可用 |

可通过 `xcode-select --install` 安装编译工具。Go 安装参见 [go.dev](https://go.dev/dl/)，Hermes 参见 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)。模型账号和密钥由你自己配置，不在这个仓库中。

## 2. 准备微信/Gadget

本项目不会自动替换、注入或重签名你的微信应用。请按上游 [Gadget 接入说明](https://github.com/yincongcyincong/wechat_chatter/blob/main/frida-gadget/readme.md) 处理你自己的兼容副本；不同微信版本的偏移和签名要求可能不同。

确认微信已登录且 Gadget 本地端口可用。端口打开只表示通道存在，面板还会等待账号和原生任务上下文就绪，不等于已经能发送。

## 3. 编译 OneBot

```bash
./setup.sh --native
```

构建脚本下载约 49 MB 的官方 Frida 开发包到 `.deps/`，验证固定 SHA-256，然后按 `go.mod` / `go.sum` 解析依赖。产物位于 `onebot/onebot`。这一步不运行微信或发送消息。

已有开发包时可跳过下载：

```bash
FRIDA_DEVKIT_DIR=/absolute/path/to/frida-devkit ./setup.sh --native
```

路径可包含空格。不要混用其他 Frida 版本的头文件与静态库。

## 4. 启动与配置

```bash
./stop.sh
./start.sh --native
```

默认端口：面板 `8080`、Webhook `36060`、OneBot `58080`、Gadget `27042`，全部限于本机。启动遇到端口占用会退出，不会杀掉占用端口的其他进程。

打开面板，先确认当前账号与服务状态，再添加白名单。初始 `auto_reply`、`send_enabled`、`allow_all` 都为 `false`，白名单为空。开启发送与自动回复必须由你在面板里操作。

消息生成会调用你本机配置的 Hermes 模型。只有你主动启用的会话上下文会进入模型请求；是否出网取决于你自己的 Hermes 模型提供方配置。

## 5. 停止与故障定位

- `./stop.sh`：仅停止本副本记录且身份匹配的面板和 OneBot，不退出微信。
- `./status.sh`：查看端口状态，需 `lsof`。端口状态不等于收发验收。
- 启动日志：`.runtime/bridge-run.log` 与 `onebot/startup.log`。可能包含私人内容，不要直接上传。
- 更换微信版本：先停止原生服务，提供该版本的正确偏移配置，再设置 `WECHAT_CONFIG`；不要猜测兼容性。
- Hermes 不可用：先在独立终端验证自己的 Hermes 配置；仓库不会读取或迁移其他人的凭据。

原生功能受微信版本、进程状态和本机配置影响。源码编译、单元测试和面板启动成功，不代表真实微信收发已通过；请在自己受控的测试会话中验证。
