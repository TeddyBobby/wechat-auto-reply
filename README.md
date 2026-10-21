# 微信自动回复 · Hermes

本地运行的微信 AI 回复工作台：联系人白名单、会话专属提示词、上下文管理、回复草稿、日志和按微信账号隔离的数据存储。

**克隆后可以直接启动只读管理面板。实际微信收发还需要 macOS Apple Silicon、兼容的微信/Gadget 和已配置的 Hermes，不能仅靠克隆仓库自动接管微信。**

## 快速启动

需要 Git、Python 3.9+ 和 Bash。面板使用 Python 标准库，不需要 npm、pip 安装依赖，也不下载模型。支持 macOS / Linux；Windows 用户可用 WSL 预览。

```bash
git clone https://github.com/TeddyBobby/wechat-auto-reply.git
cd wechat-auto-reply
./setup.sh
./start.sh
```

浏览器打开 **http://127.0.0.1:8080**。

默认是**只读预览**：使用隔离的空数据，不连接微信，不调用模型，不启动消息 Webhook，也不允许保存配置或发送消息。页面会明确显示当前模式。

```bash
# 停止本副本启动的服务；不会退出微信或停止其他项目
./stop.sh

# 如果默认端口被占用，改用其他端口
WECHAT_PANEL_PORT=8088 ./start.sh
```

也可前台启动面板，按 Ctrl+C 退出：

```bash
python3 wechat_hermes_bridge.py
```

## 接入真实微信

支持的原生组合是 **macOS Apple Silicon + 微信 4.1.12.53 对应偏移配置 + Frida 17.8.0**。其他微信版本不能直接套用偏移地址。

1. 按 [原生接入说明](docs/NATIVE_SETUP.md) 准备兼容微信/Gadget、Go、编译工具和 Hermes。
2. `./setup.sh --native` 下载经过 SHA-256 校验的 Frida 开发包，编译仓库附带的 OneBot 源码。
3. 停止预览：`./stop.sh`，然后显式启动：`./start.sh --native`。
4. 面板确认当前微信账号后，添加需要回复的联系人白名单，再开启发送与自动回复。

首次接入时发送、自动回复、全员放行都默认关闭。账号或微信进程变化后，发送开关会重新关闭。原生接入并未在每个系统或微信版本上验证，请以状态页和你自己的受控收发测试为准。

## 功能

- 联系人白名单、群消息开关、临时静音、连续回复限制。
- 全局提示词、联系人专属提示词与备注、上下文长度管理。
- 先生成回复草稿，再编辑和发送；模型调用通过本机 Hermes CLI。
- 会话导入、图片和网页上下文处理。
- 微信账号隔离、历史记录、结构化事件日志与服务状态。
- OneBot 原生收发适配、图片恢复和合成数据回归测试。

## 配置

环境变量是可选覆盖项，示例见 [.env.example](.env.example)。脚本不会自动加载 `.env`；若使用它，请自行执行 `set -a; source .env; set +a`。

| 变量 | 默认值 / 用途 |
| --- | --- |
| `WECHAT_PYTHON` | `python3` |
| `WECHAT_PANEL_PORT` | `8080`，仅监听本机 |
| `WECHAT_HOOK_PORT` | `36060`，仅原生模式使用 |
| `WECHAT_HOME` | 当前源码目录；更改后需把 OneBot 运行文件放到该目录的 `onebot/` |
| `WECHAT_APP` | `/Applications/WeChat.app` |
| `WECHAT_CONFIG` | 附带的 `onebot/wechat_version/4_1_12_53_mac.json` |
| `WECHAT_ONEBOT_TOKEN` | 未设置时，原生启动自动生成本地随机 Token |
| `FRIDA_DEVKIT_DIR` | 可选：已有 Frida 17.8.0 开发包目录，用于编译 |

Token 存储在本地 `.runtime/onebot-token`，文件权限为 `0600`。不要把它粘贴到 issue、日志或示例配置中。

## 开发与测试

Python 3.9+、Node.js 20+：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.cjs
```

这些测试使用临时数据、模拟模型与传输，不发送真实微信消息。GitHub Actions 在 macOS / Ubuntu 与 Python 3.10 / 3.12 上执行同样的离线测试。Go 测试需要 Frida 开发包，详见 [测试说明](tests/README.md)。

## 数据与发布范围

仓库只包含源码、通用版本配置和合成测试数据。**不包含聊天记录、联系人、私人提示词、模型密钥、登录信息、收到的图片/文件或历史 Git 记录。** 本地数据、缓存、SDK、编译产物与日志已加入 `.gitignore`；再次分享文件前仍应检查内容。详见 [隐私说明](PRIVACY.md)。

这是本机桌面工具，管理接口没有网站登录系统，不应直接暴露到公网。

## 上游与许可

原生适配代码基于 [yincongcyincong/wechat_chatter](https://github.com/yincongcyincong/wechat_chatter)，保留上游 GPL-3.0 许可。仓库按 GPL-3.0 发布；来源和修改范围见 [THIRD_PARTY.md](THIRD_PARTY.md)。微信、Frida、Hermes 均为各自项目，未随仓库附带应用或账号。
