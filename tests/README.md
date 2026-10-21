# 离线测试

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.cjs
```

测试数据是虚构联系人、文本、占位密钥和人工生成的微型图像。测试不含真实聊天、账号配置、抓包或聊天媒体。

- Bridge 导入使用临时 `WECHAT_HOME`，不读取已安装程序的配置或聊天记录。
- HTTP 测试仅使用每次新建的随机本地端口，发送与模型调用均使用桩；CLI 检查仅向该测试服务发送 `off`。
- 网络抓取测试替换 DNS 和 HTTP 连接，不访问外部网页。
- JavaScript 测试在 Node VM 中执行纯辅助函数和模拟指针，不连接 Frida、微信或原生进程。
- 预览启动验收把必要源码复制到含空格的临时目录，使用随机端口真实执行 setup/start/stop，检查空数据、只读 API、重复启动与进程清理；子进程审计拦截任何模型、原生或出站网络调用。
- Go 测试使用临时目录和合成报文，构建需要项目所列依赖与 Frida 开发头文件。

真实捕获的 `protobuf_dump_test.go` 未包含。服务回执测试改用明确构造的最小 protobuf；媒体字段均为测试占位值。
