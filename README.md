# FFmpeg 视频目录转换器

这是一个基于 Docker 的视频转换服务：监听输入目录的顶层文件，使用 FFmpeg 转换视频，并将结果写入独立的输出目录。原始文件会保留，不会被修改或删除。

## 快速启动

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force input, output
docker compose pull
docker compose up -d
```

Compose 使用已发布的 `ghcr.io/jermeyhu/video_converter:latest` 镜像。若需指定其他版本，可在 `.env` 中设置 `IMAGE`，例如 `IMAGE=ghcr.io/jermeyhu/video_converter:v1.0.0`。若需使用本地构建的镜像，先运行 `docker build -t ghcr.io/jermeyhu/video_converter:latest .`，再执行 `docker compose up -d`。

将视频复制到 `input` 目录。服务会等到文件大小和修改时间在 `FILE_STABILITY_SECONDS` 秒内保持不变后，再把它加入转换队列。查看运行日志：

```powershell
docker compose logs -f
```

默认输出为 1920x1080 的 MP4（H.264/AAC）。转换时会保持原视频宽高比，并通过补边适配目标分辨率。若目标文件已存在，服务会跳过转换，绝不会覆盖已有文件。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_INPUT_DIR` | `./input` | 映射到容器输入目录的主机目录。 |
| `HOST_OUTPUT_DIR` | `./output` | 映射到容器输出目录的主机目录。 |
| `INPUT_DIR` | `/data/input` | 容器内输入目录。 |
| `OUTPUT_DIR` | `/data/output` | 容器内输出目录，必须与 `INPUT_DIR` 不同。 |
| `OUTPUT_FORMAT` | `mp4` | FFmpeg 输出容器格式及文件扩展名，例如 `mkv`、`webm`、`mov`。请为容器选择兼容的编码器。 |
| `VIDEO_CODEC` | `auto` | `auto` 自动检测可用的 H.264 硬件编码器，不可用时回退到 `libx264`；也可以指定 FFmpeg 视频编码器，例如 `libx265` 或 `libvpx-vp9`。 |
| `AUDIO_CODEC` | `aac` | FFmpeg 音频编码器，例如 `aac`、`libopus` 或 `copy`。默认会重新编码音频。 |
| `OUTPUT_WIDTH` | `1920` | 输出宽度，必须为正偶数。 |
| `OUTPUT_HEIGHT` | `1080` | 输出高度，必须为正偶数。 |
| `MAX_CONCURRENT_CONVERSIONS` | `1` | 最大同时转换数。其余稳定文件会排队等待。 |
| `POLL_INTERVAL_SECONDS` | `2` | 扫描目录的时间间隔。 |
| `FILE_STABILITY_SECONDS` | `5` | 输入文件保持不变多久后才开始排队。 |
| `FFMPEG_BIN` | `ffmpeg` | FFmpeg 可执行文件路径（在 Docker 外运行时可用）。 |

在启动 Compose 前修改 `.env` 即可配置。服务只监控输入目录顶层，不递归处理子目录。支持的输入扩展名包括 MP4、MKV、MOV、AVI、WebM、MPEG/MPG、M4V、TS/MTS/M2TS、FLV、WMV 和 3GP。

例如，输出 WebM 时可配置：

```dotenv
OUTPUT_FORMAT=webm
VIDEO_CODEC=libvpx-vp9
AUDIO_CODEC=libopus
```

## 硬件加速

当 `VIDEO_CODEC=auto` 时，程序会检查容器中的 FFmpeg 是否支持 H.264 硬件编码器，并确认对应设备已映射进容器。尝试顺序为 NVIDIA NVENC、Intel QSV、VAAPI。若编码器不存在、设备不可用或硬件编码失败，程序会记录原因并自动回退到 CPU `libx264`。显式指定 `VIDEO_CODEC` 时，将直接使用指定编码器，不再自动选择硬件或回退。

硬件设备需由主机传入容器：

- **NVIDIA：** 安装 NVIDIA Container Toolkit，然后运行 `docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up -d`。使用主机 GPU 驱动，无需在主机安装 FFmpeg。
- **Intel QSV / VAAPI：** 将 render 设备映射到容器，通常可运行 `docker compose -f docker-compose.yml -f docker-compose.vaapi.yml up -d`。主机需提供 `/dev/dri` 及兼容的设备驱动和 FFmpeg 编码器。

以上 Compose 配置会使用已发布的 GHCR 镜像。需要从本地 Dockerfile 构建时，先执行前述本地构建命令。

若没有向容器映射 GPU，服务会继续使用 CPU 转换。每个输入文件都会独立尝试硬件编码；失败时会清理临时文件，再进行 CPU 编码。

## 队列与失败处理

转换工作线程数量由 `MAX_CONCURRENT_CONVERSIONS` 限制，超出的文件会进入内存队列等待，因此同时运行的 FFmpeg 进程数不会超过配置值。队列不持久化；服务重启后会重新扫描输入目录并重建任务。转换失败会记录日志；同一文件只有在内容或修改时间变化后才会再次尝试。转换期间的临时文件会被清理，输入文件始终保留。

停止服务：

```powershell
docker compose down
```

## GitHub Actions 镜像发布

`.github/workflows/publish-image.yml` 会在 Pull Request 上构建镜像用于验证，但不会发布；推送到默认分支或推送版本标签（例如 `v1.2.3`）时，会构建多架构 `linux/amd64`、`linux/arm64` 镜像并发布到 GitHub Container Registry。默认分支还会更新 `latest` 标签。工作流需要仓库 Actions 的 `packages: write` 权限（已在工作流中声明）。首次发布后，如需公开拉取，在 GitHub Packages 设置中将该容器包的可见性改为 Public。

## 本地运行测试

需要 Python 3.10 或更高版本。单元测试仅使用 Python 标准库：

```powershell
python -m unittest discover -s tests -v
```
