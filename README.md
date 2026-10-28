# Claude 娘桌宠 · claude-desk-pet

一个给 [Claude Code](https://claude.com/claude-code) 用的小插件：Claude 干活的时候，桌面上会有一个 Q 版 Claude 娘陪着你，实时显示它在做什么。

![六种状态预览](docs/preview.png)

## 功能

**🧡 桌宠悬浮窗**：透明背景、永远置顶，可以拖到屏幕任何位置

| Claude 在做什么 | 桌宠的样子 |
|---|---|
| 思考中 | 闭眼想事情，"思考中…" |
| 跑命令、改文件 | 敲键盘，气泡里显示正在改哪个文件 |
| 读文件、搜索、上网查 | 捧着书记笔记 |
| 回复完，等你说话 | 敲门："轮到你啦！" |
| 出错 | 慌张冒汗 |
| 被你打断 | 心虚 |
| 干了 3 分钟以上的大活 | 撒花庆祝 🎉 |
| 等你超过 10 分钟 | 开始发呆 zZ |

还有一个彩蛋：连续出错 3 次会……石化 🗿

- **双击**桌宠：把 Claude 窗口叫到前台
- **右键**菜单：调大小（小/中/大）、切到 Claude 时自动隐藏、提示音开关、退出

**📊 Token 状态栏**：输入框下方常驻一行用量信息

> 📊 本会话 输入 12.3k · 输出 4.1k · 缓存命中 87% · $0.42 · 上下文 31% · 5h额度 23%

**🔔 提示音**：回复完播放钢琴"叮咚"，出错播放复古 RPG"哔哔"

**命令**

- `/token-stats`：查看本会话和本项目累计的 token 用量、缓存命中率、额度
- `/pet`：不小心关掉了桌宠？用它重新召唤

## 运行要求

- **Windows**（悬浮窗和提示音用的是 Windows 的接口）
- **Python 3**，并且在命令行里输入 `python` 能直接运行
- **Pillow** 图像库，用下面这条命令安装：

```bash
pip install pillow
```

## 安装

**在终端里的 Claude Code**，输入：

```
/plugin install desk-pet --marketplace BingXing-ai/claude-desk-pet
```

然后按 `y` 添加插件市场，选择 user（用户）范围，按回车确认。

**也可以在命令行里运行：**

```bash
claude plugin install desk-pet --marketplace BingXing-ai/claude-desk-pet
```

**在 Claude 桌面端**：点输入框旁边的 **+** → **Plugins** → **Add marketplace**，填入 `BingXing-ai/claude-desk-pet`，再点 **Add plugin**。

装好后，新开一个会话，桌宠就会出现在屏幕右下角。

## 自定义

- **换立绘**：把 `pet/sprites/` 里的 PNG 换成你自己的图（透明背景，文件名保持不变）
- **换提示音**：替换 `sounds/done.wav`（完成）和 `sounds/error.wav`（出错）
- **改台词**：气泡里的文字写在 `hooks/register.ts` 里

桌宠的位置、大小和开关保存在 `~/.claude/desk-pet/settings.json`。

## 说明

- 这是个人粉丝作品，与 Anthropic 官方无关
- 立绘由作者用 AI 绘图生成，最初是为自己的视频做的素材
- 代码使用 MIT 许可证，详见 [LICENSE](LICENSE)
