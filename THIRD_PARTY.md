# 来源与许可

本项目按 GPL-3.0 发布，参见根目录 LICENSE。

`onebot/src/`、`onebot/script.js`、`onebot/wechat_version/` 基于 [yincongcyincong/wechat_chatter](https://github.com/yincongcyincong/wechat_chatter)（Go module 使用其历史路径 `github.com/yincongcyincong/weixin-macos/onebot`）。上游许可证原文保留在 `onebot/LICENSE`。

该发布副本包含对收发确认、参与者字段、图片恢复、错误处理和离线测试的本地修改；同时移除了源码中的默认 Token 与令牌日志，将运行路径改为可配置路径。

Python 桥接、HTML 面板、账号隔离、安装脚本和其他本项目代码随本仓库以同一 GPL-3.0 许可发布。

Go 依赖及版本见 `onebot/src/go.mod` / `go.sum`，依赖保留各自的许可。Frida 开发包在用户构建时从官方 Release 下载，未复制到仓库。微信应用、Gadget 二进制、Hermes 应用与模型均不随仓库分发。
