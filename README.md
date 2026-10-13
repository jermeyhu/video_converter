# FFmpeg 视频目录转换器

这是一个基于 Docker 的视频转换服务：监听输入目录的顶层文件，使用 FFmpeg 转换视频，并将结果写入独立的输出目录。转换成功后，源视频会移动到输入目录下的 `processed` 子目录，避免重复转换；转换失败时源视频保留在输入目录。

## 快速启动

```powershell
New-Item -ItemType Directory -Force input, output
docker compose -f docker-compose.yaml pull
docker compose -f docker-compose.yaml up -d
```

使用 `ghcr.io/jermeyhu/video_converter:latest` 镜像，挂载当前目录下的 `input`、`output` 文件夹。修改配置时编辑 Compose 文件中的 `environment` 值。

将视频复制到 `input` 目录。服务会等到文件大小和修改时间在 `FILE_STABILITY_SECONDS` 秒内保持不变后，再把它加入转换队列。转换成功的原始文件会自动移动到 `input/processed`，该子目录不会被扫描。

### 状态页面

服务启动后会打开一个 Web 状态页面（默认 `http://localhost:5800`），可实时查看：

- 等待队列、正在转换、已完成、失败的数量
- 每个任务的进度条、已用时间、实际使用的编码器与后端（VAAPI / CPU）
- VAAPI 的 H.264 / HEVC 编码器和 render device 检测状态

```powershell
docker compose -f docker-compose.yaml logs -f
```

默认输出为 MP4（H.264/AAC），保持源视频原始分辨率。只有同时设置 `OUTPUT_WIDTH` 和 `OUTPUT_HEIGHT` 时才会调整分辨率；转换时保持原视频宽高比，并通过补边适配目标尺寸。若目标文件已存在，服务会跳过转换，绝不会覆盖已有文件。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_INPUT_DIR` | `./input` | 映射到容器输入目录的主机目录。 |
| `HOST_OUTPUT_DIR` | `./output` | 映射到容器输出目录的主机目录。 |
| `INPUT_DIR` | `/data/input` | 容器内输入目录。 |
| `OUTPUT_DIR` | `/data/output` | 容器内输出目录，必须与 `INPUT_DIR` 不同。 |
| `OUTPUT_FORMAT` | `mp4` | FFmpeg 输出容器格式及文件扩展名，例如 `mkv`、`webm`、`mov`。请为容器选择兼容的编码器。 |
| `VIDEO_CODEC` | `h264` | 设置编码格式为 `h264` 或 `h265`，自动检测对应 VAAPI 编码器；硬件不可用或失败时分别回退到 `libx264` / `libx265`。也可指定具体 FFmpeg 编码器名称，此时直接使用该编码器。 |
| `AUDIO_CODEC` | `aac` | FFmpeg 音频编码器，例如 `aac`、`libopus` 或 `copy`。默认会重新编码音频。 |
| `OUTPUT_WIDTH` | 未设置 | 可选输出宽度，必须为正偶数；须与 `OUTPUT_HEIGHT` 同时设置。 |
| `OUTPUT_HEIGHT` | 未设置 | 可选输出高度，必须为正偶数；须与 `OUTPUT_WIDTH` 同时设置。 |
| `MAX_CONCURRENT_CONVERSIONS` | `1` | 最大同时转换数。其余稳定文件会排队等待。 |
| `POLL_INTERVAL_SECONDS` | `2` | 扫描目录的时间间隔。 |
| `FILE_STABILITY_SECONDS` | `5` | 输入文件保持不变多久后才开始排队。 |
| `FFMPEG_BIN` | `ffmpeg` | FFmpeg 可执行文件路径（在 Docker 外运行时可用）。 |

`OUTPUT_WIDTH` 和 `OUTPUT_HEIGHT` 默认不设置，需同时设置才会缩放。服务只监控输入目录顶层，不递归处理子目录。支持的输入扩展名包括 MP4、MKV、MOV、AVI、WebM、MPEG/MPG、M4V、TS/MTS/M2TS、FLV、WMV 和 3GP。

例如，要使用 WebM 输出或指定 720p，可在 `video-converter` 下添加：

```yaml
    environment:
      OUTPUT_FORMAT: webm
      VIDEO_CODEC: libvpx-vp9
      AUDIO_CODEC: libopus
      OUTPUT_WIDTH: "1280"
      OUTPUT_HEIGHT: "720"
```

选择 H.264 或 H.265 时，只需将 Compose 中的编码配置设为：

```yaml
      VIDEO_CODEC: h264
```

或：

```yaml
      VIDEO_CODEC: h265
```

## 硬件加速

项目只提供 Linux VAAPI 硬件路径。`VIDEO_CODEC=h264` 尝试 `h264_vaapi`；`VIDEO_CODEC=h265` 尝试 `hevc_vaapi`。如果驱动、设备或当前输入不支持该路径，则回退到 CPU `libx264` / `libx265`。指定其他具体 FFmpeg 编码器名时，会直接使用该编码器，不进行自动检测或回退；只有以 `_vaapi` 结尾的显式硬件编码器会同时启用 VAAPI 解码。

### 硬件解码与转码

启用 VAAPI 时，FFmpeg 使用同一 `/dev/dri/renderD*` 设备进行硬件解码和硬件编码：

- **Intel 核显：** 镜像包含 Intel Media Driver（新款核显）及 i965 驱动（旧款核显）；实际支持的解码/编码格式取决于具体 GPU 世代。
- **AMD 核显/显卡：** 镜像包含 Mesa VAAPI 驱动（`radeonsi`）；实际支持取决于 GPU、宿主机内核驱动和固件。

未设置输出尺寸时，硬件帧直接从硬件解码器传到对应硬件编码器。设置 `OUTPUT_WIDTH` / `OUTPUT_HEIGHT` 时，当前实现先将硬件解码帧下载到系统内存，再用 FFmpeg 的通用 `scale` / `pad` 滤镜保持比例并补边，最后上传到同一硬件设备编码。此时解码和编码仍由硬件完成，但缩放/补边由 CPU 执行；该路径避免依赖各后端并不一致的 padding 滤镜。

状态页会分别用小型测试视频实际尝试 H.264 / HEVC VAAPI 编码，因此硬件或驱动不支持的编码格式会显示为不可用；不保证任意输入编码格式、位深都可硬件解码。转换任务会实际尝试 VAAPI，失败后回退 CPU。

Compose 直接拉取 GitHub Actions 发布到 GHCR 的 `ghcr.io/jermeyhu/video_converter:latest` 多架构镜像，不在 NAS 上本地构建。配置默认映射 Linux 主机的 `/dev/dri`，因此仅适用于有 VAAPI 设备节点的 Linux 主机；启动前确认宿主机存在 `/dev/dri/renderD*`。容器镜像内含 AMD Mesa 与 Intel 新旧代 VAAPI 用户态驱动；主机仍需提供匹配的内核驱动和固件。

可通过 `docker compose exec video-converter vainfo --display drm --device /dev/dri/renderD128` 验证驱动是否能访问设备。VAAPI 可用的编码 profile 随 GPU 型号而异，例如部分旧 Intel/AMD 核显不支持 HEVC 编码；此时 H.265 转换会由 CPU 完成。

## 队列与失败处理

转换工作线程数量由 `MAX_CONCURRENT_CONVERSIONS` 限制，超出的文件会进入内存队列等待，因此同时运行的 FFmpeg 进程数不会超过配置值。队列不持久化；服务重启后会重新扫描输入目录并重建任务。转换失败会记录日志，源文件保留在输入目录；转换成功则移动至 `input/processed`，若归档文件名已存在会自动添加序号，不会覆盖。转换期间的临时文件会被清理。

修改配置后应用变更，或拉取 GitHub Actions 发布的新镜像并重建容器：

```sh
docker compose -f docker-compose.yaml pull
docker compose -f docker-compose.yaml up -d
```

停止服务：

```sh
docker compose -f docker-compose.yaml down
```

## GitHub Actions 镜像发布

`.github/workflows/publish-image.yml` 会在 Pull Request 上构建镜像用于验证，但不会发布；推送到默认分支或推送版本标签（例如 `v1.2.3`）时，会构建多架构 `linux/amd64`、`linux/arm64` 镜像并发布到 GitHub Container Registry。默认分支还会更新 `latest` 标签。工作流需要仓库 Actions 的 `packages: write` 权限（已在工作流中声明）。首次发布后，如需公开拉取，在 GitHub Packages 设置中将该容器包的可见性改为 Public。

## 本地运行测试

需要 Python 3.10 或更高版本。单元测试仅使用 Python 标准库：

```powershell
python -m unittest discover -s tests -v
```
