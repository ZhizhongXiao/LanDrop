# 测试目录

使用 Python 标准库 `unittest` 覆盖文件安全、凭据存储、Bottle 路由和桌面版服务生命周期。

运行：

```powershell
python -m unittest discover -s tests -v
```

测试只使用临时目录、进程内 WSGI 请求和模拟服务器，不监听局域网端口，也不打开 GUI。
