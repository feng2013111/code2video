---
name: code2video
description: 用代码逐帧绘制动画并无头 Chrome 渲染成 MP4 视频。当用户要求"生成视频/制作动画视频/把这段代码做成视频/HTML 动画转视频/text to video"，且期望由模型亲自编写绘制代码（HTML/Canvas）时使用本技能。原理：模型写单文件 HTML 动画 → 无头 Chrome 逐帧截图 → ffmpeg 合成 MP4；自动检测 GPU 渲染加速与硬件编码（nvenc/qsv/amf/videotoolbox）。
---

# code2video — 代码即视频

不调用任何外部视频生成 API。文本模型自己写代码，一帧一帧把画面画出来，
由无头 Chrome 渲染成截图序列，再用 ffmpeg 合成 MP4。

```
用户描述 ──► 模型编写 HTML 动画 ──► 无头 Chrome 逐帧截图 ──► ffmpeg 合成 MP4
             (__renderFrame)        (CDP, GPU 自动加速)      (硬件编码自动选择)
```

## 何时使用

- 用户要"生成/制作一个视频、动画、片头、可视化短片"，且接受由你编写绘制代码；
- 用户给了代码/数据/文字，想让它"动起来"变成视频；
- 需要确定性、可复现、可版本管理的视频（代码即源文件）。

不适合：需要真实照片/视频素材、AI 文生图风格画面的场景（那些应走图像/视频生成工具）。

## 环境准备（一次性）

```bash
pip install -r requirements.txt     # websocket-client + imageio-ffmpeg（兜底）
python -m code2video env            # 自检：GPU / 浏览器 / ffmpeg / 编码器
```

- 浏览器：自动探测 Chrome / Edge / Chromium / Brave；也可用环境变量
  `CODE2VIDEO_BROWSER=/path/to/chrome` 指定。
- ffmpeg：优先用系统 ffmpeg（通常带硬件编码器）；没有则用 pip 包
  imageio-ffmpeg 兜底（仅 CPU libx264）。
- GPU：检测到 NVIDIA/AMD/Intel/Apple GPU 时，Chrome 以 GPU 合成渲染，
  并通过 chrome://gpu 验证；软件渲染时自动回退 SwiftShader，保证一定能出片。

## HTML 动画契约（必读）

一个自包含 HTML 文件必须实现：

```js
window.VIDEO_SPEC = { fps: 30, frames: 180, width: 1280, height: 720 }; // 元数据，可被 CLI 覆盖
window.__renderFrame = function (index, t) { /* 完整绘制第 index 帧 */ };
```

确定性规则（违反则截图不可复现，视频会花掉）：

1. 禁止 `Date.now()` / `performance.now()` / `new Date()`；
2. 禁止裸 `Math.random()`——用 seeded RNG（模板里的 `mulberry32`）；
3. 禁止依赖 `requestAnimationFrame` 是否执行过；
4. 禁止外部资源（字体、图片、CDN）——全部内联；
5. 每帧从零绘制整幅画面（先铺背景再画内容）；
6. 宽高取偶数；总帧数 ≤ 600（预览用小帧数，全量再放开）。

模板在 `templates/animation_template.html`，抄它改最稳妥。

## 标准工作流

1. **写动画**：把 HTML 写到 `<工作区>/animations/<名字>.html`，
   从模板或 `examples/` 里的例子出发改。
2. **预览**（必做，省时间）：
   ```bash
   python -m code2video render --html animations/foo.html \
       --out preview/foo.mp4 --frames 12 --size 640x360
   ```
   读 preview 的帧检查布局、配色、节奏；不满意就改 HTML，再预览。
3. **全量渲染**：
   ```bash
   python -m code2video render --html animations/foo.html --out out/foo.mp4
   ```
4. **交付**：把 mp4 给用户，说明时长/帧率/分辨率/编码器；
   报告 JSON 可用 `--report out/foo.report.json` 留档。

## 命令参考

```
python -m code2video env [--json]
python -m code2video render --html A.html --out B.mp4 \
    [--fps N] [--frames N] [--size WxH] [--frames-dir DIR] \
    [--encoder auto|h264_nvenc|h264_qsv|h264_amf|h264_videotoolbox|h264_vaapi|libx264] \
    [--crf 18] [--preset medium] [--browser PATH] [--no-gpu] \
    [--keep-frames] [--report R.json]
```

平台：Windows / Linux / macOS 通用（Linux 无显示器服务器亦可，纯 headless；
VAAPI 节点用 `CODE2VIDEO_VAAPI_DEVICE` 覆盖）。

- `--frames` / `--size` 同时控制预览与最终渲染；不传则读 `VIDEO_SPEC`。
- `--frames-dir` 指定 PNG 序列目录，配合 `--keep-frames` 可复用帧做二次剪辑。
- `--no-gpu` 强制软件渲染（排查 GPU 花屏问题时用）。

## 排障

| 症状 | 处理 |
| --- | --- |
| `does not define window.__renderFrame` | HTML 没实现契约，对照模板 |
| `No Chromium-family browser found` | 装 Chrome/Edge 或设 `CODE2VIDEO_BROWSER` |
| `ffmpeg was not found` | 装 ffmpeg 或 `pip install imageio-ffmpeg` |
| 渲染慢 | 正常，GPU 下约 20-60 帧/秒；减少帧数/分辨率先预览 |
| 画面与预期不符 | 先用 `--frames 3` 看单帧，检查 `t` 与 phase 计算 |
| 视频花屏/撕裂 | 几乎都是动画里混入了不确定性随机或时间依赖 |

## 给模型自身的建议

- 动画叙事用 phase 时间轴（0-0.2 入场 / 0.2-0.8 主体 / 0.8-1 收尾），
  模板和两个例子都是这个结构；
- 文字动画优先用离屏 canvas 采样文字像素（见 `examples/hello_particles.html`）；
- 数据类动画（柱状图竞赛等）把数据写死在 HTML 里，插值用缓动函数；
- 交付前用 `--report` 留一份 JSON（含 GPU/编码器/渲染判定），方便复现。
