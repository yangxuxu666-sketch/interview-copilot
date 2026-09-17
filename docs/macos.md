# Mac 下载、使用与打包（预览版）

仓库提供手动运行的 [GitHub Actions 构建流程](../.github/workflows/build-macos.yml)，分别构建 Apple 芯片与 Intel 版本。它只在主动选择 Run workflow 后运行，并创建一个 GitHub 预发布页，附上各架构的 `.dmg`、ZIP 与 SHA-256 文件。构建流程检查原生应用启动、网页资源、文本导入、辅助程序、重复启动，以及冻结小窗的打开和关闭。

**真实系统音频、录制权限、语音识别、钥匙串写入及小窗移动和透明度仍需在目标 Mac 上验收。** 自动构建检查不能代替这些操作；具体结果见 GitHub Actions 运行记录，ZIP 内附自动检查报告。

## 下载并安装

当前预览 `.dmg` / ZIP 安装包要求 **macOS 15.7 或更新版本**；macOS 13/14 不能直接打开这些安装包。请在「苹果菜单 → 关于本机」确认系统版本和芯片。每个产物的准确最低版本记录在包内 `构建信息.json` 中。

1. 在仓库的 [Releases 页面](https://github.com/yangxuxu666-sketch/interview-copilot/releases)选择 Mac 预发布版本。
2. Apple 芯片下载文件名含 `arm64` 的 `.dmg`；Intel 芯片下载含 `x86_64` 的 `.dmg`。
3. 双击 `.dmg`，将「面试伴航.app」拖到「应用程序」，再从「应用程序」打开。使用 ZIP 时，先完整解压再拖入应用。安装包无需另装 Python。
4. 在应用的「连接与设置」填写自己的 DeepSeek 和语音识别 API Key。

此预览版尚未经过 Apple 开发者签名和公证。若首次打开提示无法验证开发者或 Apple 无法检查 App，确认文件来自本项目后，先尝试打开一次，再到「系统设置 → 隐私与安全性」，向下找到「仍要打开」，点击并确认「打开」。这是针对该应用的系统操作，无需关闭全局安全检查。若没有此按钮或仍打不开，请记录报错原文。[Apple 官方说明](https://support.apple.com/zh-cn/102445)

## 运行源码启动器

源码中的系统音频接口要求 macOS 13 或更新版本，但源码及其依赖在 macOS 13/14 上仍需另行验证；这个接口要求不代表上述安装包支持 macOS 13/14。

1. 下载并完整解压仓库。
2. 安装带 Tk 的 Python **3.13**。推荐使用 Python 官网的 macOS 安装程序；不使用系统 Python。
3. 在仓库根目录运行：

```bash
python3.13 packaging/macos/bootstrap.py
```

也可双击 `packaging/macos/启动面试伴航.command`。如果下载 ZIP 后脚本没有执行权限，使用上面的 Python 命令即可。

首次启动会在 `~/Library/Application Support/InterviewCopilot` 下建立独立运行环境并从 PyPI 下载依赖。之后配置自己的 DeepSeek 和语音识别 API Key。保持启动终端打开，退出时使用应用中的退出按钮。

## 声音与权限

Mac 适配通过 ScreenCaptureKit 读取系统播放音频，不读取麦克风。首次监听可能需要在系统设置中允许「屏幕与系统音频录制」或对应名称的权限；权限名称会随 macOS 版本变化。系统要求重新打开时，退出应用后再次启动。

源码启动时系统可能把权限关联到 Python 或终端；构建 `.app` 后应单独验证应用权限。其他应用播放的声音也可能进入转写。Mac 小窗支持透明度和移动的实现已加入，**不支持「共享时隐藏」**。

个人资料位于 `~/Library/Application Support/InterviewCopilot`，API Key 通过 macOS 钥匙串保存。钥匙串不可用时应保留错误提示，不要在公开 Issue 中粘贴密钥。

## 在 Mac 上构建 `.app`

简便方式，在仓库根目录运行：

```bash
python3.13 packaging/macos/bootstrap.py --build
```

也可双击 `packaging/macos/制作独立Mac应用.command`。脚本将产物写入仓库根目录的 `mac-dist/`。

如需单独的构建环境和自定义输出位置：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r InterviewCopilot/requirements-macos.txt -r packaging/macos/requirements-build.txt
.venv/bin/python packaging/macos/build_mac.py --source InterviewCopilot --output dist/macos
```

必须在对应架构的真实 Mac 环境中构建。脚本会生成本机架构的 `.app`，执行启动等检查，收集第三方许可，并通过 `ditto` 生成保留应用结构的 ZIP 和可拖入“应用程序”的 `.dmg`。构建信息记录架构、构建系统和验证限制。

构建脚本目前不执行 Apple 开发者签名和公证；本地签名校验不等于公证。分发产物前，应明确标注签名状态，并按 Apple 官方流程处理系统提示。

## 实机验证清单

- 初次启动、重复启动、退出以及从新目录启动。
- 允许或拒绝录制权限后的行为，实际播放音频的转写，停止后停止采集。
- 保存自己的 API Key、退出后重启，以及钥匙串权限异常时的提示。
- 简历导入、流式回答、小窗移动和透明度。
- Apple 芯片与 Intel 构建各自在目标架构和系统版本的结果。

只有完成相应实机检查后，才能把具体产物标记为已验证的 Mac 安装版。
