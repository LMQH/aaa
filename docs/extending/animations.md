# 启动动画

启动动画是单文件插件：一个 `.py` 文件就是一个动画，不需要注册。

## 位置与命名

- 用户目录：`~/.aaa/animations/<name>.py`
- 随包目录：`<安装目录>/aaa/cli/animations/<name>.py`
- 文件名即动画名，须匹配 `[a-z0-9][a-z0-9_-]*`；以下划线开头或不匹配的文件被忽略。
- 同名时用户目录覆盖包内，可直接复制包内文件改名作为起点。

## 最小示例

`~/.aaa/animations/hello.py`：

```python
from aaa.cli.animations import Frame

def frames(ctx):
    yield Frame(1.0, "  Hello  ")

ANIMATION = frames
```

启用：`/config start hello`，下次启动生效。

## 契约

模块级必须导出 `ANIMATION`：

- 函数：等价于 `frames(ctx)`；
- 对象：需有 `frames(ctx)`，可选 `min_columns`、`min_rows`、`static(ctx)`。

`frames(ctx)` 是可迭代的，每项产出一个 `Frame`：

```python
Frame(delay: float, content)   # delay 秒后切到下一帧
```

`content` 两种写法：

- 字符串：适用于与宽度无关的内容；
- `columns -> 字符串` 的函数：在真正绘制时才调用，能拿到当前终端宽度。

`ctx` 是 `FrameContext`，字段 `columns`、`rows`、`duration`、`version`。`rows` 取播放开始时的行数。

## 为什么 content 可以是函数

两个原因，都与正确性有关：

1. **延迟绘制**。450 帧若在启动前全部渲染，会卡住数秒；可调用内容让每一帧只在轮到它时才算。
2. **跟随窗口**。播放中终端变宽变窄时，宿主用新的列数重新取内容，动画仍然居中。

内容与宽度无关时直接写字符串，不必包函数。

## 可选成员

| 成员 | 作用 | 缺省 |
|---|---|---|
| `min_columns` | 需要的最小列数 | 0 |
| `min_rows` | 需要的最小行数 | 0 |
| `static(ctx)` | 静置画面（`ui.banner_animate=false` 或时长为 0 时显示） | 第一帧 |

最小尺寸由**动画自己**声明，宿主不猜。空间不足时宿主打印单行标题，不调用动画，也不缩小你的画面。

## 宿主替你做的事

- 按 `delay` 调度，落后时跳过已过期的帧，不补播；
- 播放中空间不足时中止，并在结束时打印一次单行标题；
- 每帧校验可见宽度（剥离 SGR 后按显示列计算），超宽即中止并回退；
- 加载失败、导入抛异常、`frames` 产出非 `Frame` 对象时，回退单行标题并记日志——**启动永远不会因为动画失败而中断**；
- 终止时不回收终端已重排进滚动历史的帧。

## 调试

用安装了 aaa 的 Python 环境执行：

```bash
python -m aaa.cli.banner --animation hello
python -m aaa.cli.banner --animation ~/tmp/wip.py --duration 2
```

从源码检出运行时，改用 `PYTHONPATH=src .venv/bin/python`。

- `--animation` 接受名字或文件路径；不给则读 `ui.banner_animation`。
- `--duration` 覆盖配置里的时长；`--duration 0` 只看静置画面。
- 预览模式直接打印 traceback 并返回 1，便于定位语法错误与契约违例；启动路径只记日志、不打扰用户。

## 约束

- 不要移动光标、清屏或使用 OSC 序列。可以带 SGR（颜色、加粗），宿主按剥离样式后的宽度校验。
- 不要读取键盘；播放期间终端输入不归动画管。
- 不要假设固定宽度，也不要假设自己的输出会被截断——超宽会被判为失败。

## 两个常用写法

逐字打出：

```python
def frames(ctx):
    for size in range(1, 12):
        yield Frame(0.06, "starting…"[:size])
```

跟随窗口宽度的分隔线：

```python
def frames(ctx):
    yield Frame(0.4, "AAA")
    yield Frame(0.4, lambda columns: "─" * max(0, columns - 2))
```

## 生效与切换

- 内置风格：`default`（像素 `α` 与渐显的 `AAA`，默认）和 `sharingan`（三阶段写轮眼）。
- 切换：`/config start sharingan`，下次启动生效；用 `/config start default` 切回。不带动画名时显示当前选择。原键 `ui.banner_animation` 同样可用。未知名字回落 `default` 并记警告。
- 关闭：`ui.banner_enabled = false`；保留配色但不要动画：`ui.banner_animate = false`。
- 两款内置动画均使用终端字符，不依赖图片协议或外部图片。
