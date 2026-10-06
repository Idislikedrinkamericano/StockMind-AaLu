# StockMind 网页版部署指南

目标：把 StockMind 变成一个**能分享的公开演示链接**（Hugging Face Spaces，免费）。

## 一、合入代码（在你的 Mac 上）

1. 从聊天记录下载 `stockmind-web.zip`，解压。
2. 把 zip 里的文件拷进你的 `StockMind-AaLu` 仓库根目录：
   - `app.py`（新增，Spaces 启动入口）
   - `web/__init__.py`、`web/app.py`（新增，Gradio 界面）
   - `requirements.txt`（新增；仓库原来用 `pyproject.toml`，这个文件只给 Spaces 用，不影响原有安装方式）
   - `DEPLOY.md`（本文件，可选）
3. 确认没有覆盖任何原有文件（`git status` 里应该只有新增文件）。
4. 本地冒烟测试（可选但推荐）：
   ```bash
   pip install gradio
   python app.py
   # 浏览器打开 http://127.0.0.1:7860，输入 NVDA，点生成研报
   ```
5. `git add`、`git commit -m "feat: add Gradio web demo (additive, no changes to app/)"`、`git push`。

## 二、创建 Hugging Face Space

1. 注册/登录 https://huggingface.co（免费）。
2. 点右上角头像 → **New Space**：
   - Space name：`stockmind-demo`（或任意）
   - License：MIT（或按你喜好）
   - SDK：**Gradio**
   - Space hardware：默认 CPU Basic（免费）
   - Visibility：**Public**（要分享链接就选 Public）
3. 创建后进入 Space → **Settings** → **Repository secrets**：不需要加任何 secret（演示用离线 demo 模型 + 模拟数据，零密钥零成本）。
4. 把 Space 连到你的 GitHub 仓库：Settings 里关联仓库，或直接把文件 push 到 Space 的 git 仓库（二选一）：
   - 方式 A（推荐）：Space 设置中找到 "Connected repository"，连 `Idislikedrinkamericano/StockMind-AaLu`，之后 GitHub 一 push，Space 自动重新部署。
   - 方式 B：把 Space 当成 git 远端，`git push` 文件过去。

## 三、验证

1. 等待 Space build 完成（页面顶部状态变绿，约 3–8 分钟）。
2. 打开 Space 链接，输入 `NVDA`，选中文，点"生成研报"。
3. 应看到：实时进度日志 → 中文研报 → 关键指标表 → 价格走势图。
4. 把链接贴到 GitHub README 顶部，例如：
   `🚀 [在线演示 Live Demo](https://huggingface.co/spaces/<你的用户名>/stockmind-demo)`

## 四、常见问题

- **build 失败**：点开 build 日志看，大概率是依赖版本问题；`requirements.txt` 里都是宽松下限，可按日志 pin 版本。
- **页面显示构建中**：HF 免费 CPU 首次 build 较慢，耐心等几分钟，刷新页面。
- **想用真实 LLM**：在 Space → Settings → Repository secrets 里加 `OPENAI_API_KEY`，并把 `web/app.py` 里 `build_demo_model` 那行换成默认构造（`StockMindAgent(language=lang)`）。注意：公开 Space + 真实 key = 别人能烧你的额度，**不建议**在公开演示里这么做。
- **并发**：`demo.queue(max_size=8)` 已配置，多人同时访问会排队。

## 文件清单（本 zip）

| 文件 | 说明 |
| --- | --- |
| `app.py` | Spaces 启动入口（thin launcher） |
| `web/__init__.py` | 包标记 |
| `web/app.py` | Gradio 界面：异步流式进度 + 研报渲染 + 图表 |
| `requirements.txt` | 部署依赖（原有依赖 + gradio） |
| `DEPLOY.md` | 本指南 |
