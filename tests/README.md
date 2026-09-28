# 测试目录

使用 Python 标准库 `unittest` 覆盖文件安全、可信凭据持久化与服务端过期、Bottle 路由、桌面服务控制、临时会话生命周期、HTTP Range 逻辑任务聚合、隐私化日志轮转、托盘/Toast 统一协调、旧通知隔离和并发原子动作，以及 endpoint 冻结与二次确认、启动期网络类别 fail-closed、Public 启动拒绝、异步深度诊断、防火墙证据分类、显式接口选择、HTTP 并发资源上限和上传空间预留。具体测试数量以运行时 unittest 输出为准。

运行：

```powershell
python -m unittest discover -s tests -v
```

测试以临时目录、进程内 WSGI 请求和模拟服务器为主；少数资源边界测试仅监听 loopback，不接触真实 LAN，也不打开 GUI。真实 LAN、GUI、Windows 防火墙与干净机行为仍由人工验收覆盖。
