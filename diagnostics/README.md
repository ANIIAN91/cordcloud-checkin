# Patchright 独立验证

本实验仅访问 `https://cordcloud.biz/auth/login`，检查网站 Cap 组件是否产生非空令牌。
不导入业务代码，不读取账号配置或 GitHub Secrets，不提交账号密码，不执行签到，
不导出 Cookie、令牌、响应正文或浏览器异常原文。每次使用新的临时 Chrome 配置，退出后删除。

依赖固定在 `patchright-requirements.txt`。使用现有 Python 3.12 创建独立环境并安装该依赖，
已有桌面显示和 Google Chrome 时运行：

```bash
python diagnostics/patchright_probe.py
```

Linux 无显示环境使用 `xvfb-run -a python diagnostics/patchright_probe.py`。
探针采用上游建议的 Chrome、有界面模式、默认请求头和临时持久上下文；通过主执行环境
读取组件属性，但只返回布尔值。Patchright 控制台功能不可用，因此记录验证接口 HTTP 状态。

退出码：`0` 表示观察到 Cap 令牌，`1` 表示验证未通过或超时，`2` 表示浏览器调用失败。
验证成功不等于登录或签到成功，也不代表长期成功率。

独立工作流仅由诊断分支相关文件推送或手动操作触发，不设置定时任务。
云端最多检查三个全新会话，任一会话失败即停止；不加载现有签到 Secrets。
默认签到流程和依赖保持不变。来源：[Patchright Python](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)。
