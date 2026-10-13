# 速映 FlashPlay

基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [aria2c](https://github.com/aria2/aria2) 的**下载与播放**图形界面 —— **单文件、零依赖**（只用 Python 标准库），界面风格参考 VLC media player，内置 mpv 播放内核。
界面预览见 `docs/screenshot.png`。

## 特性

- **单文件零依赖**：只用 Python 标准库（tkinter），下载一个 `.py` 就能跑
- **VLC 风格深色界面**：工具条 + 任务列表 + 实时进度条 + 状态栏
- **全界面悬停说明**：鼠标停在任意按钮/输入框/进度条上会弹出气泡，告诉你它是干什么的
- **两种下载方式**：网址下载（[yt-dlp](https://github.com/yt-dlp/yt-dlp)，B站 / YouTube 等 **1800+ 网站**）+ 磁力下载（BitTorrent/aria2c，支持断点续传、自动获取内核）
- **内置播放器**：mpv 内核嵌入窗口，下载完直接播放（进度拖动 / 倍速 / 全屏 / 音量 / 字幕）
- 粘贴链接即可下载，自动获取标题 / UP 主 / 时长
- 画质选择：最佳 / 1080P / 720P / 480P / 仅音频 MP3
- 实时进度：百分比、速度、剩余时间；合并、转码阶段有状态提示
- 任务队列串行下载，失败自动重试（网络类错误），地址类的致命错误不浪费重试
- 内核全自动获取：yt-dlp / ffmpeg / mpv / aria2c 首次使用时自动下载（国内镜像加速），本机已有的自动检测复用
- 内核自维护：「设置 → 内核维护」一键检查更新 yt-dlp（旧版自动备份）
- 完成后双击任务打开文件，右键菜单可打开文件夹 / 复制链接 / 重新下载
- 配置持久化（画质、下载目录、cookies 浏览器、代理、重试次数）

## 环境要求

- Python 3.8+（Windows 上自带 tkinter 的官方安装包即可）
- **无需手动准备任何内核**：[yt-dlp](https://github.com/yt-dlp/yt-dlp/releases)（下载内核，约 18MB）、[ffmpeg](https://www.gyan.dev/ffmpeg/builds/)（合并内核，约 30MB）、[mpv](https://github.com/zhongfly/mpv-winbuild)（播放内核，约 32MB）、[aria2c](https://github.com/aria2/aria2)（磁力内核，约 2.4MB）都在**首次使用时自动获取**（国内镜像加速）
- 本机已有 ffmpeg / yt-dlp 的会自动检测复用；也可以手动放到程序同目录跳过下载

## 用法

**快速开始（推荐）**：下载 Release 里的 zip → 解压 → 双击 **速映.exe**（无需安装）。
首次下载时程序会自动准备所需内核（yt-dlp / ffmpeg / mpv / aria2c，走国内镜像加速）。

从源码运行（需要 Python 3.8+）：

```bash
python sy.py                 # 启动图形界面
python sy.py "视频链接"       # 启动并自动加入任务
python sy.py --selftest      # 无界面自检：真实下载一个小视频，验证链路
```

界面操作：

1. 把视频链接粘贴到底部输入框，回车（或点「下载」）
2. 在右侧选择画质，任务会自动排队下载
3. 双击任务行打开文件；右键可打开文件夹、复制链接、重新下载

## 支持的网站 & 两种下载方式

### 方式一：网址下载（视频页面链接）

粘贴视频页面链接即自动解析下载，底层是 [yt-dlp](https://github.com/yt-dlp/yt-dlp)，**支持 1800+ 个网站**，国内外主流平台基本都覆盖：

- **国内**：B站、央视网、A站、优酷、腾讯视频、爱奇艺、微博、西瓜视频、小红书、斗鱼、虎牙、网易云音乐、QQ音乐、喜马拉雅……
- **国际**：YouTube、TikTok、X/Twitter、Instagram、Twitch、Vimeo、SoundCloud、Reddit……（需要能访问外网，可在「设置」里填代理）

完整列表见 [yt-dlp 支持站点列表](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)；拿不准的链接直接粘进程序点「下载」试一下，不支持会立刻提示。

常见注意点：

- B站免登录一般到 480P；要 1080P 及以上，在「设置」里选择 `Cookies 浏览器`（先在浏览器登录 B站），或用「Get cookies.txt」扩展导出 cookies.txt 后指定。
- B站偶尔会分配到不通的 P2P CDN 节点（表现为连接超时）—— 程序会自动重试换节点。
- Netflix / Spotify 等 DRM 保护内容任何下载器都下不了；会员/付费内容需要你自己的账号权限（cookies）。
- YouTube 需要能访问外网，「设置」里可填代理，如 `http://127.0.0.1:7890`。

### 方式二：磁力下载（magnet: 链接）

粘贴 `magnet:?xt=urn:btih:…` 磁力链接（BitTorrent / P2P），程序会自动切换到「磁力下载」：

- 用 aria2c 内核，首次使用自动获取（约 2.4MB，国内镜像加速）
- 支持断点续传（中断后重新下载会接着传）
- 下载完成即停止，**不做种**（不对外上传）
- 磁力任务没有「画质」选项（内容由种子决定），也没有封面 / UP 主等网站信息

> 提醒：磁力下载请只用于你**有权下载**的内容（如 Linux 发行版、公共领域作品、你自己的文件）。

### 两种方式一览

| | 网址下载 | 磁力下载 |
| --- | --- | --- |
| 输入 | 视频页面链接 | `magnet:` 磁力链接 |
| 原理 | 直接向网站请求（平台按权限发放） | BitTorrent P2P（内容在其他用户电脑上） |
| 内核 | yt-dlp | aria2c |
| 画质 / 信息 | 可选画质，显示标题 / 时长 / 封面 | 由种子决定 |
| 断点续传 | 支持 | 支持 |

## 内核维护（外置组件说明）

程序用到的外置内核都放在程序目录下，**首次使用时自动获取**（走国内镜像加速）：

| 内核 | 用途 | 获取方式 |
| --- | --- | --- |
| yt-dlp.exe | 网站视频下载 | 首次使用时自动获取（约 18MB）；「设置 → 内核维护」可一键检查更新（yt-dlp 每周更新多次，建议定期更新） |
| ffmpeg | 音视频合并 / 转码 | 首次下载时自动获取（约 30MB）；也可在设置里手动获取 |
| mpv | 内置播放器 | 首次播放时自动获取（约 32MB） |
| aria2c | 磁力下载 | 首次磁力下载时自动获取（约 2.4MB） |

更新 yt-dlp 时旧版会自动备份为 `yt-dlp.exe.bak`，遇到问题改回即可。

## 打包成 exe（可选）

```bash
pip install pyinstaller
pyinstaller -F -w -n "速映" --icon assets/app.ico sy.py
```

## License

MIT
