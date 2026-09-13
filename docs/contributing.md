# 贡献说明

欢迎提交问题、兼容性验证和代码改进。修改前请先阅读对应平台的运行文档。

## 开发环境与测试

项目使用 Python **3.13**。在仓库根目录建立虚拟环境并安装对应平台的云端依赖，然后进入应用目录运行测试：

```powershell
# Windows
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r packaging/windows/requirements-cloud.txt
cd InterviewCopilot
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

```bash
# macOS
python3.13 -m venv .venv
.venv/bin/python -m pip install -r InterviewCopilot/requirements-macos.txt
cd InterviewCopilot
../.venv/bin/python -m unittest discover -s tests -v
```

测试使用模拟 API，不需要真实 API Key、下载语音模型或开启录音。涉及平台原生功能的测试可能需要对应平台或额外组件，请按实际输出报告跳过和失败。

2026-09-13 的 Windows 开发验证记录为 **192 项测试通过**，其中包括 Mac 音频接口的模拟测试、平台适配和集成测试。这一记录不代表已经在 Mac 上完成原生构建或硬件测试，也不等同于持续集成对每次提交的保证。

## 提交改动

- 围绕具体问题修改，说明触发条件、改变后的行为，以及运行过的验证。
- 新的音频或窗口实现应能在停止、取消、权限拒绝和进程退出时释放资源。
- 保留系统播放音频与麦克风的边界；不要让启动应用自动开始录音。
- 修改打包时检查数据目录隔离、辅助程序、完整解压启动和第三方许可文件。
- 用虚构资料测试。不要提交个人 `data/`、真实 API Key、简历、聊天记录、虚拟环境、模型缓存或运行日志。

## 报告问题

请提供系统版本、芯片架构、源码或独立版、复现步骤和脱敏后的报错。Mac 报告请同时说明启动方式及录制权限状态。不要公开粘贴完整配置、会话令牌、钥匙串内容或未脱敏截图。

## 许可

贡献到本项目的源码按仓库的 MIT License 分发。引入第三方代码、字体、图标或依赖时，请保留原始许可，并确认其允许预期的分发方式。
