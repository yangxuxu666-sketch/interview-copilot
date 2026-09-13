# 面试伴航 · Interview Copilot

在自己的电脑上运行的面试准备与实时回答辅助工具。读取系统播放的声音，转写为文字，再结合简历、岗位和公司资料，通过 DeepSeek 生成流式回答建议。

适合模拟面试、个人练习，以及参与者允许使用辅助工具的交流场景。

## 功能

- **实时转写**：支持阿里云千问实时语音识别、兼容 OpenAI 转写协议的云端接口；Windows 源码版还可选本地 faster-whisper。
- **结合个人资料**：导入 TXT、Markdown、DOCX、文字型 PDF；按公司和岗位保存多份档案。
- **流式回答**：跟随问题的主要语言给出中文或英文建议，支持自动回答和手动修改问题。
- **自定义表达**：设置回答语气、结构、长度和重点，每份档案独立保存。
- **独立小窗**：白色半透明回答窗，支持移动、缩放、置顶、字号和透明度调整。
- **准备与复盘**：生成准备清单、练习问题和复盘，保存问答与反馈并导出 Markdown。

## 支持状态

| 平台 | 运行方式 | 验证情况 |
|---|---|---|
| Windows 10/11 x64 | 源码运行；云端版免安装 ZIP | 已验证启动、系统播放音频、文档导入、配置保存和回答小窗；已完成独立打包与解压启动检查 |
| macOS 13+ | Python 3.13 启动脚本；在 Mac 上构建 `.app` | 适配代码、启动和构建脚本已准备；尚未完成 Mac 实机录音、钥匙串、窗口与原生构建验证 |

Mac 版本目前属于待实机验证的预览，不应视作已经验证的独立安装版。Apple 芯片与 Intel Mac 需要各自构建并验证。

## 开始使用

### Windows 源码运行

安装 Python **3.13 x64**，下载或克隆本仓库，在仓库根目录打开 PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r packaging/windows/requirements-cloud.txt
.\.venv\Scripts\python.exe InterviewCopilot/main.py
```

页面默认在 `http://127.0.0.1:8765` 打开。该地址只能访问本机正在运行的服务。免安装包及源码的详细步骤见 [Windows 使用与打包](docs/windows.md)。

### Mac 源码运行

在 Mac 上安装带有 Tk 的 Python **3.13**（可使用 Python 官网的 macOS 安装程序），然后在仓库根目录运行：

```bash
python3.13 packaging/macos/bootstrap.py
```

脚本会建立独立环境并下载组件。也可双击 `packaging/macos/启动面试伴航.command`。首次监听需允许系统音频录制权限；详细步骤和验证限制见 [Mac 使用与打包](docs/macos.md)。

### 首次配置

1. 在「连接与设置」填写自己的 **DeepSeek API Key**。
2. 选择「阿里云千问 · 实时识别」，填写自己的 **百炼北京地域 API Key**。两种 Key 分别用于回答和语音识别，不能互相替代。
3. 在「面试准备」保存简历、岗位描述、公司材料，以及需要的回答风格。
4. 在「实时面试」选择实际播放声音的设备，点击「开始监听」。可以手动提交问题，也可开启自动回答。
5. 点击「打开半透明窗」显示独立回答窗。

API 服务需要联网并由服务商分别计费；应用不附带密钥。连接测试会发出实际请求。千问实时识别使用 `qwen3-asr-flash-realtime`；DeepSeek 默认模型为 `deepseek-flash`，可根据账号支持情况修改。

## 声音与资料如何使用

```text
电脑播放声音 → 语音识别 → 最终转写
                            ↓
             问题 + 保存的资料片段 + 回答要求
                            ↓
                    DeepSeek 流式建议
                            ↓
                    主页面 / 独立小窗
```

Windows 使用 WASAPI Loopback 采集所选播放设备，Mac 适配使用 ScreenCaptureKit 采集系统音频。应用不打开麦克风；播放设备中的其他应用、提示音，以及被系统回放的自身声音仍可能被采集。当前没有按说话人分离声音的功能。

千问路径持续发送 16 kHz 单声道音频，中间文字用于预览，最终句子才进入自动回答。端到端等待受句末停顿、网络、识别服务和模型响应共同影响，没有固定低延迟保证。

回答会参考保存的资料和有限历史。公司名称不会自动触发联网搜索；请导入已核实的材料。AI 建议仍需自行核对，未提供的经历和数字不应当作个人事实使用。

## 数据与隐私

| 运行方式 | 默认数据目录 | API 密钥保存方式 |
|---|---|---|
| Windows 源码版 | `InterviewCopilot/data/` | 当前 Windows 账户的 DPAPI |
| Windows 独立版 | `%LOCALAPPDATA%\InterviewCopilot` | 当前 Windows 账户的 DPAPI |
| Mac | `~/Library/Application Support/InterviewCopilot` | macOS 钥匙串 |

应用只监听本机回环地址。原始音频不写入磁盘；云端转写会把音频发送到所选语音服务，生成回答会把问题和选中的资料发送到 DeepSeek。简历、设置和问答记录以本地文件保存，文字资料本身没有加密。

分享代码或安装包时不要包含数据目录、API 密钥、日志或个人材料。不同电脑需要分别配置自己的密钥。更多说明见 [数据与隐私](docs/privacy.md)。

## 开发与构建

```text
InterviewCopilot/       应用、静态页面、依赖声明和测试
packaging/windows/      Windows 云端版打包脚本
packaging/macos/        Mac 启动、构建与验证脚本
docs/                   使用、构建、贡献与隐私说明
```

[贡献说明](docs/contributing.md)包含测试命令和验证范围。Windows 与 Mac 构建分别见 [Windows 文档](docs/windows.md)和 [Mac 文档](docs/macos.md)。测试结果不等同于真实会议的准确率或兼容性承诺。

## 许可

本项目源码采用 [MIT License](LICENSE)。第三方依赖遵循各自的许可证；打包脚本会收集相应的许可声明，分发时请保留。使用的 API 服务遵循各自服务条款。
