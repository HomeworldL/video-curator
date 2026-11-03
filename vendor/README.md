# 第三方源码

使用可正常安装并锁定版本的 yt-dlp、DDGS和可选Playwright；没有复制整套第三方应用。

`manifest.json` 是第三方源码溯源清单，不参与运行时加载、依赖安装或下载。它已登记一段经修改的 `xiaohongshu-mcp` 搜索状态读取JavaScript，放在平台目录中随包分发，保留Apache-2.0及UPSTREAM说明。其余平台复用已安装yt-dlp或公开接口约定。

已核查 DouK-Downloader 与 XHS-Downloader。两者均为 GPL-3.0，顶层入口带有额外模块、配置和工作目录依赖；当前不为已经由 yt-dlp 提供的详情/下载功能重复复制整套应用。

后续确需引入其站内访问或备用下载能力时，先固定 commit 和文件闭包，保留原许可证，在 `manifest.json` 登记来源、文件、资源、改动和验证结果，以独立进程运行。
