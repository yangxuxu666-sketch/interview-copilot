# Windows 使用与打包

适用于 Windows 10/11 **x64**。独立版是云端识别版，无需安装 Python；源码版可额外安装本地识别组件。

## 使用免安装包

拿到本项目的 Windows 云端版 ZIP 后：

1. 选择「全部解压」，把完整目录放到固定位置。不要在压缩包预览中直接运行，也不要只复制一个 EXE。
2. 双击 `面试伴航.exe`。可运行包内的 `创建桌面快捷方式.cmd`，以后从桌面启动。
3. 在「连接与设置」填写自己的 DeepSeek 和语音识别 API Key。
4. 保存个人资料，播放一段练习音频，选择对应的耳机或扬声器后开始监听。

更新应用时把新包完整解压到新文件夹，保留 `%LOCALAPPDATA%\InterviewCopilot` 中的个人数据即可。源码版默认使用仓库内的 `InterviewCopilot/data/`，两者的数据目录不同；空白界面不一定表示旧资料已删除。

## 从源码运行

安装 Python 3.13 x64，在仓库根目录运行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r packaging/windows/requirements-cloud.txt
.\.venv\Scripts\python.exe InterviewCopilot/main.py
```

需要本地 CPU 识别时，另行安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r InterviewCopilot/requirements-audio.txt
```

本地 faster-whisper 首次使用会下载模型，需要额外空间和时间。云端版打包不会包含这些本地识别组件和模型。

## 构建独立云端版

在 Windows x64、Python 3.13 环境中，于仓库根目录运行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r packaging/windows/requirements-cloud.txt -r packaging/windows/requirements-build.txt
.\.venv\Scripts\python.exe packaging/windows/build_windows.py --output dist/windows
```

构建使用 PyInstaller。主程序和后台辅助程序需要和 `_internal` 目录一起保留；辅助程序负责隔离运行小窗等组件。构建时应保留第三方许可说明，发布前从 ZIP 完整解压到一个新目录验证启动。

发布前至少验证：空白初始档案、密钥保存后重启可用、文档导入、页面连接、回答小窗、重复启动及退出。再用目标设备验证播放音频采集；不要把开发环境中的 `data/`、`.venv/`、日志或任何密钥复制进包内。

## 常见问题

- **监听没有声音**：确认会议或视频本身正在播放，应用中选择的输出设备和会议软件一致。蓝牙模式切换或插拔耳机后刷新设备列表。
- **识别成其他语言**：以中文为主时将识别语言设为中文；中英文混合时使用自动。云端准确率仍取决于录音质量和服务本身。
- **填过 Key 但输入框为空**：为避免回显，已保存的 Key 不会重新填入输入框，以保存状态为准。换电脑或 Windows 账户后需重新配置。
- **端口占用**：源码启动可加 `--port 8766`；仅通过当前启动地址进入应用。
- **上传后没有文字**：支持 TXT、MD、DOCX 和带文字层的 PDF。扫描图片型 PDF 需要先做 OCR；单文件限制为 8 MB。
- **小窗共享设置**：「共享时隐藏」仅适用于支持 Windows 显示亲和性的捕获方式，需 Windows 10 2004 或更新系统。主页面不受影响，无法保证所有软件与共享模式都遵守该设置；Mac 不支持此项。

启动错误和应用日志位于相应数据目录。公开反馈前请移除密钥、简历、完整路径和会话内容。
