# code2video

**让文本模型用代码一帧一帧画出视频。** 不调用任何外部视频生成 API——
模型编写一个自包含的 HTML 动画（Canvas/DOM），无头 Chrome 逐帧截图，
ffmpeg 合成 MP4。GPU 渲染加速与硬件编码全自动检测。

*Let a text model draw videos frame by frame with code: no video-generation
API, just HTML + headless Chrome + ffmpeg, with GPU auto-detection.*

```
用户描述 ──► 模型编写 HTML 动画 ──► 无头 Chrome 逐帧截图 ──► ffmpeg 合成 MP4
             (__renderFrame)        (CDP, GPU 自动加速)      (硬件编码自动选择)
```

## 特性

- **代码即视频**：视频的源文件是一个 HTML，可读、可改、可进 git，渲染结果完全可复现
- **零 AI API 依赖**：不烧 token 调视频模型，成本约等于本地算力
- **自动 GPU 加速**：检测到 GPU 时 Chrome 以 GPU 合成渲染，并用 WebGL renderer 探测实际验证；无 GPU 自动回退 SwiftShader
- **自动硬件编码**：NVIDIA → `h264_nvenc`，Intel → `h264_qsv`，AMD → `h264_amf`，Apple → `h264_videotoolbox`，Linux AMD/Intel → `h264_vaapi`，兜底 `libx264`；每个候选都会先做一次 1 帧试编码验证（`ffmpeg -encoders` 只代表"编译进去了"，不代表这台机器真能用）
- **跨平台**：**Windows / Linux / macOS** 全支持；Chrome / Edge / Chromium / Brave 均可；无显示器的 Linux 服务器也能跑（纯 `--headless=new`）
- **可作 Skill 使用**：`SKILL.md` 可直接装进 WorkBuddy / Claude 类智能体，指挥模型"写代码 → 预览 → 全量渲染 → 交付"

## 快速开始

```bash
git clone https://github.com/feng2013111/code2video.git && cd code2video
pip install -r requirements.txt     # websocket-client + imageio-ffmpeg 兜底
python -m code2video env            # 自检环境（GPU/浏览器/ffmpeg/编码器）

# 渲染自带示例
python -m code2video render --html examples/solar_system.html --out out/solar.mp4

# 先小预览，再全量（推荐工作流）
python -m code2video render --html examples/hello_particles.html \
    --out preview.mp4 --frames 12 --size 640x360
python -m code2video render --html examples/hello_particles.html --out out/hello.mp4
```

系统已装 ffmpeg 时优先使用（硬件编码器通常只在完整构建里）；
没装则自动用 pip 包 `imageio-ffmpeg` 兜底（CPU libx264，保证能出片）。

## HTML 动画契约

一个自包含 HTML 实现两个全局对象：

```js
window.VIDEO_SPEC = { fps: 30, frames: 180, width: 1280, height: 720 }; // 元数据
window.__renderFrame = function (index, t) { /* 从零完整绘制第 index 帧 */ };
```

- `t = index / fps`（秒）；每帧必须完整重绘，禁止残留上一帧
- **确定性是生命线**：禁 `Date.now()` / `performance.now()` / 裸 `Math.random()` /
  依赖 `requestAnimationFrame`；随机一律用 seeded RNG（模板内置 `mulberry32`）
- 禁止外部资源；宽高取偶数；总帧数建议 ≤ 600

模板：`templates/animation_template.html`；示例：`examples/hello_particles.html`
（粒子汇聚文字）、`examples/solar_system.html`（太阳系）。

## CLI

```
python -m code2video env [--json]
python -m code2video render --html A.html --out B.mp4 \
    [--fps N] [--frames N] [--size WxH] [--frames-dir DIR] \
    [--encoder auto|h264_nvenc|h264_qsv|h264_amf|h264_videotoolbox|h264_vaapi|libx264] \
    [--crf 18] [--preset medium] [--browser PATH] [--no-gpu] \
    [--keep-frames] [--report R.json]
```

`--report` 输出 JSON：帧数、时长、编码器、ffmpeg 来源、WebGL 渲染判定、
GPU 信息、告警——复现与排障都靠它。

## 自动检测逻辑

| 环节 | 检测方式 | 决策 |
| --- | --- | --- |
| 浏览器 | 常见安装路径 + `PATH` + `$CODE2VIDEO_BROWSER` | Chrome/Edge/Chromium/Brave 任选其一（含 Linux snap/flatpak/opt 路径） |
| GPU | `nvidia-smi` → Win32_VideoController → `system_profiler` → `lspci` | 有 GPU 给 Chrome 加 `--use-angle=gl --enable-gpu-rasterization` |
| 渲染验证 | 任意页面跑 WebGL `UNMASKED_RENDERER` 探测（比 `chrome://gpu` 更可靠，定制构建也能用） | 硬件判定直接用；软件判定且有 GPU 时换 ANGLE 后端重试，仍不行则带告警继续 |
| ffmpeg | `PATH` → `imageio-ffmpeg` | 系统版优先 |
| 编码器 | GPU 厂商 + 操作系统排序候选，每个候选 1 帧试编码验证 | nvenc/qsv/amf/videotoolbox/vaapi 按平台与 GPU 选取；无 DRI 节点跳过 vaapi；兜底 libx264 |

## 平台支持（Windows / Linux 双端通用）

| 平台 | 浏览器 | ffmpeg | 硬件编码 |
| --- | --- | --- | --- |
| Windows 10/11 | 自带 Edge / Chrome 自动探测 | `winget install Gyan.FFmpeg`，或用 pip 兜底 | nvenc / qsv / amf |
| Linux（含无显示器服务器） | `apt install chromium-browser` 或 google-chrome / snap / flatpak | `apt install ffmpeg` | vaapi / qsv / nvenc |
| macOS | 自带路径探测 | `brew install ffmpeg` | videotoolbox |

Linux 无 X 显示器也能渲染：全程 `--headless=new`，不依赖 X server；
root 下自动加 `--no-sandbox`。VAAPI 渲染节点可用
`CODE2VIDEO_VAAPI_DEVICE=/dev/dri/renderD129` 覆盖。

CI 在 `ubuntu-latest` + `windows-latest` 双端跑单元测试和
真实无头 Chrome 冒烟测试（见 `.github/workflows/ci.yml`）。

## 作为 Skill 安装（WorkBuddy / Claude）

```bash
# WorkBuddy: 复制到用户级技能目录
cp -r . ~/.workbuddy/skills/code2video        # Windows: %USERPROFILE%\.workbuddy\skills\code2video
```

之后对智能体说"用代码做个视频：……"，它会按 `SKILL.md` 的流程
写 HTML → `--frames 12` 预览 → 全量渲染 → 交付 mp4。

## 项目结构

```
code2video/
├── SKILL.md                        # 技能定义（智能体工作流与契约）
├── code2video/                     # Python 包
│   ├── detect.py                   #   GPU/浏览器/ffmpeg/编码器探测
│   ├── cdp.py                      #   极简 Chrome DevTools Protocol 客户端
│   ├── capture.py                  #   无头 Chrome 逐帧渲染（含 GPU 探测回退）
│   ├── compose.py                  #   ffmpeg 合成（编码器候选 + 试编码验证）
│   └── cli.py                      #   env / render 两个子命令
├── templates/animation_template.html
├── examples/                       # hello_particles / solar_system
├── tests/                          # 单元测试 + 可选冒烟测试
└── pyproject.toml
```

## 测试

```bash
python -m pytest                                   # 单元测试（不需要浏览器）
CODE2VIDEO_SMOKE=1 python -m pytest tests/test_smoke.py -s   # 端到端：真渲染 3 帧并合成
```

## 路线图

- [ ] WebM/VP9、GIF 输出
- [ ] 音频轨叠加（配音/BGM）
- [ ] 帧目录复用与断点续渲
- [ ] `--script` 模式：JSON 分镜 → 自动生成 HTML

## English quickstart

```bash
pip install -r requirements.txt
python -m code2video env
python -m code2video render --html examples/solar_system.html --out out/solar.mp4
```

Write an HTML file that implements `window.__renderFrame(index, t)`
deterministically (see `templates/animation_template.html`), then render it
to MP4. GPU compositing and hardware encoding are detected automatically.

## License

MIT，见 [LICENSE](LICENSE)。欢迎 PR。
