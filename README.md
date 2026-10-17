# Download Progress for Codex

独立运行或配合 Codex，批量下载 HTTP/HTTPS 文件，实时查看进度、分类存放并导出回执。只追踪本工具发起的下载，不能自动接管 pip、浏览器或其他工具。

## Windows 双击启动

从 [v0.3.0 发行页](https://github.com/yigebianchengyaoyao/codex-download-progress/releases/tag/v0.3.0)下载 `DownloadWorkbench-0.3.0.exe` 或包含指南的交付 ZIP，双击程序打开本机工作台，无需 Python、Codex 或账号。适用于 Windows x64；当前只在 Windows 11 实机验证。

先点击“运行本机演示”，再粘贴自己的文件直链。每批最多 50 个链接，最多 4 个同时下载；支持去重、分类、取消、CSV 回执及默认隐藏文件名/路径/链接的分享图。演示始终标注，不伪装真实用户成果。

关闭标签页后服务仍运行；点击“退出服务”才结束。程序未签名，相关说明和排障见[快速上手](delivery/快速上手.md)。

源码启动（Python 3.10+，标准库）：

~~~powershell
python plugins/download-progress/scripts/workbench.py
~~~

源码和公开发行包免费、MIT 开源。用户选择的商业方向是“低价交付包＋可选安装协助”；付费价值是资料整理与约定服务，不是独占代码。[推广与交付方案](marketing/推广与交付方案.md)包含尚待验证的价格与内容草稿，没有支付接入或平台上架。

## 安装

需要本机 Python 3.10 或更新版本，无第三方运行依赖。

~~~powershell
codex plugin marketplace add yigebianchengyaoyao/codex-download-progress
codex plugin add download-progress@download-progress-community
~~~

安装后新开一个 Codex 对话。仓库：[codex-download-progress](https://github.com/yigebianchengyaoyao/codex-download-progress)。
评审中的分支可以用 marketplace add 的 --ref 参数选择；默认安装 GitHub 默认分支。

如果系统只有 py 命令，修改插件 .mcp.json 的 command 为 py，并在 args 前加 -3。
更改已安装插件时，请刷新版本并重新安装，避免继续使用旧缓存。

## 使用

告诉 Codex：“用 Download Progress 下载这个链接到 E:\下载，并显示进度”。

技能会把目标文件或目录转换成绝对路径。没有指定目录时，使用实际用户工作区下面的 downloads 文件夹。
MCP 服务运行在插件安装目录，不能自行推断用户工作区；直接调用工具时必须明确传 destination 或 directory，二者只能选一个。
0.2.0 不再接受相对路径，也不再把已有目录当作 destination 文件；目录请使用 directory。

工具：

| 工具 | 用途 |
| --- | --- |
| start_download | 开始任务，指定 URL、绝对路径，可选文件名与预期 SHA-256 |
| get_download_progress | 查询进度，wait_seconds 可取 0～10，避免连续空轮询 |
| cancel_download | 请求停止并保留部分文件，等待 cancelled 才表示已停止 |
| list_downloads | 找回当前服务会话内的任务，最多保留 100 条 |
| start_download_batch | 1～50 条直链加入队列，传绝对 directory，可选 title、organize |
| get_batch_progress | 按 batch_id 查询完成、失败、取消、跳过等状态 |
| cancel_download_batch | 取消该批次的排队与运行任务 |
| get_batch_receipt | 导出不含 URL、绝对路径的结构化回执，仍含文件名 |
| open_download_workbench | 打开本机服务并返回私有访问地址，可设默认目录 |

最多同时下载 4 个文件，同一目标只能有一个写入者，包括不同 MCP 进程。
Codex 在有意义的进展或间隔时更新对话；这不是由宿主自动刷新的进度条，也不保证每两秒推送一次。

## 文件可靠性

- 续传使用 URL 指纹、强 ETag、If-Range 和完整的 Content-Range 校验。
- URL 相同但文件内容改变时，服务器返回完整文件则从头下载；异常范围响应直接失败。
- 没有强 ETag 时从头下载，避免把不确定的旧内容拼入新文件。
- 现有目标文件始终拒绝覆盖，完成时也使用不会覆盖同名文件的发布操作。
- 可传 sha256 作为预期校验值；不匹配时不生成最终文件。仅计算出哈希不等于验证了发布者身份。
- 部分文件保存在 .part，恢复信息保存在 .part.json；其中只存 URL 指纹，不存查询参数中的令牌。
- .part.lock 是保留的小锁文件，服务异常退出会释放系统锁，不需要手动删锁文件。

发生断网、取消或服务重启后，用同一个 URL 与目标重新开始即可尝试恢复。
任务 ID 和历史只在当前服务会话中有效。停止是协作式的：单次网络等待有 30 秒 socket 超时，停止请求与最终停止状态应区分。

Windows 使用不覆盖目标的 rename；POSIX 使用 hard link 发布，因此目标文件系统需要支持硬链接。
当前回归测试运行在 Windows，macOS/Linux 分支尚未在真实系统执行。未提供认证会话、自动重试或多 GB 长时间压力测试结论。工作台是本机浏览器界面，API 使用随机凭据并检查 Host/Origin；访问地址不要公开分享。队列及历史不跨重启持久化。

## 验证与评审

~~~powershell
python -m unittest discover -s tests -v
~~~

回归测试覆盖：32 MiB 下载、中文路径、取消、截断、未知长度、空文件、文件变化、错误范围、416、SHA-256、并发限制、进程间锁、最终目标抢占、真实 MCP stdio 下载、批量去重/分类/跳过、私有回执和本机接口鉴权。

## Windows 构建

构建依赖 `PyInstaller==6.22.3`，运行依赖仍为 Python 标准库。安装构建依赖后执行：

~~~powershell
python scripts/build_windows.py
~~~

产物在 `dist/DownloadWorkbench-0.3.0.exe`。构建时的 Python 与目标架构应匹配；本次使用 Python 3.14.6 / Windows x64。

[工程评审](REVIEW.md)记录旧版问题、复现结果与修复方式；
[版本记录](CHANGELOG.md)记录变更；
[项目状态](project_memory.json)保存需求、功能与剩余限制。

MIT，见 [LICENSE](LICENSE)。本项目是社区插件。
