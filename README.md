# FFmpeg 视频目录转换器

这是一个基于 Docker 的视频转换服务：监听输入目录的顶层文件，使用 FFmpeg 转换视频，并将结果写入独立的输出目录。原始文件会保留，不会被修改或删除。

## 快速启动

```powershell
New-Item -ItemType Directory -Force input, output
docker compose -f docker-compose.yaml up -d
```

使用 `ghcr.io/jermeyhu/video_converter:latest` 镜像，挂载当前目录下的 `input`、`output` 文件夹。修改配置时编辑 Compose 文件中的 `environment` 值。

将视频复制到 `input` 目录。服务会等到文件大小和修改时间在 `FILE_STABILITY_SECONDS` 秒内保持不变后，再把它加入转换队列。

### 状态页面

服务启动后会打开一个 Web 状态页面（默认 `http://localhost:5800`），可实时查看：

- 等待队列、正在转换、已完成、失败的数量
- 每个任务的进度条、已用时间、实际使用的编码器与后端（NVENC / QSV / VAAPI / CPU）
- 硬件加速检测结果（NVENC、QSV、VAAPI 的可用性与设备）

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
| `VIDEO_CODEC` | `h264` | 设置编码格式为 `h264` 或 `h265`，自动检测该格式的可用硬件编码器；硬件不可用或失败时分别回退到 `libx264` / `libx265`。也可指定具体 FFmpeg 编码器名称，此时直接使用该编码器。 |
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

`VIDEO_CODEC=h264` 时，程序按 NVENC、QSV、VAAPI 的顺序检测 H.264 硬件编码器；`VIDEO_CODEC=h265` 时，则检测对应的 HEVC 编码器。硬件编码失败或不可用时分别回退到 CPU `libx264` 或 `libx265`。若设置为具体 FFmpeg 编码器名（如 `h264_nvenc`、`hevc_nvenc`、`libx264`、`libx265` 或 `libvpx-vp9`），则直接使用该编码器，不进行自动检测或回退。

### 硬件解码与转码

启用硬件后端时，FFmpeg 会同时使用硬件解码和硬件编码：

- **NVIDIA：** FFmpeg 初始化 CUDA 设备并使用 `-hwaccel cuda -hwaccel_output_format cuda` 解码，随后由 NVENC 编码。
- **Intel QSV：** FFmpeg 初始化 QSV 设备并使用 `-hwaccel qsv -hwaccel_output_format qsv` 解码，随后由 QSV 编码。
- **VAAPI：** FFmpeg 初始化 VAAPI render device 并使用 `-hwaccel vaapi -hwaccel_output_format vaapi` 解码，随后由 VAAPI 编码。

未设置输出尺寸时，硬件帧直接从硬件解码器传到对应硬件编码器。设置 `OUTPUT_WIDTH` / `OUTPUT_HEIGHT` 时，当前实现先将硬件解码帧下载到系统内存，再用 FFmpeg 的通用 `scale` / `pad` 滤镜保持比例并补边，最后上传到同一硬件设备编码。此时解码和编码仍由硬件完成，但缩放/补边由 CPU 执行；该路径避免依赖各后端并不一致的 padding 滤镜。

硬件状态页报告的是 FFmpeg 编码器和设备的初步可用性，不保证任意输入的视频编码格式、位深或像素格式都能硬件解码。每个任务会实际尝试硬件解码/编码链；失败时依次尝试下一后端，最后回退到 CPU。具体编码器名（如 `h264_nvenc`）是显式选择，不自动切换编码器。

Compose 只定义一个 `video-converter` 服务。若需启用 NVIDIA 硬件加速，主机安装 NVIDIA Container Toolkit，然后在 `docker-compose.yaml` 中取消 `gpus: all` 的注释；旧版 Compose 可改用注释中的 `deploy.resources.reservations.devices` 配置，两种 NVIDIA GPU 请求方式只启用其一。若主机提供 Intel QSV / VAAPI，则取消 `/dev/dri` 的 `devices` 配置注释。主机同时提供 NVIDIA 和 Intel GPU 时，两类映射可以同时启用。修改后执行 `docker compose -f docker-compose.yaml up -d` 使配置生效。

- **NVIDIA：** 主机安装 NVIDIA Container Toolkit，然后启用 `gpus: all`。使用主机 GPU 驱动，无需在主机安装 FFmpeg。
- **Intel QSV / VAAPI：** 将主机 `/dev/dri` 映射进容器；主机还需提供兼容的设备驱动和 FFmpeg 编码器。

默认 Compose 配置不映射 GPU，因此会使用 CPU。要启用自动硬件选择，取消注释当前主机实际提供的设备映射。若主机同时提供 NVIDIA 和 Intel GPU，可同时启用 `gpus: all` 与 `/dev/dri` 映射；程序按 NVIDIA、QSV、VAAPI 顺序尝试。硬件设备必须对容器可见，且容器内 FFmpeg 必须包含相应编码器。选用 `h264` 或 `h265` 后，程序会检测并尝试相应硬件编码；没有可用硬件或硬件处理失败时，自动回退 CPU。

若没有向容器映射 GPU，服务会继续使用 CPU 转换。每个输入文件都会独立尝试硬件解码与编码；失败时会清理临时文件，再尝试下一后端或 CPU 编码。

## 队列与失败处理

转换工作线程数量由 `MAX_CONCURRENT_CONVERSIONS` 限制，超出的文件会进入内存队列等待，因此同时运行的 FFmpeg 进程数不会超过配置值。队列不持久化；服务重启后会重新扫描输入目录并重建任务。转换失败会记录日志；同一文件只有在内容或修改时间变化后才会再次尝试。转换期间的临时文件会被清理，输入文件始终保留。

修改配置后应用变更或停止服务：

```powershell
docker compose -f docker-compose.yaml up -d
docker compose -f docker-compose.yaml down
```

## GitHub Actions 镜像发布

`.github/workflows/publish-image.yml` 会在 Pull Request 上构建镜像用于验证，但不会发布；推送到默认分支或推送版本标签（例如 `v1.2.3`）时，会构建多架构 `linux/amd64`、`linux/arm64` 镜像并发布到 GitHub Container Registry。默认分支还会更新 `latest` 标签。工作流需要仓库 Actions 的 `packages: write` 权限（已在工作流中声明）。首次发布后，如需公开拉取，在 GitHub Packages 设置中将该容器包的可见性改为 Public。

## 本地运行测试

需要 Python 3.10 或更高版本。单元测试仅使用 Python 标准库：

```powershell
python -m unittest discover -s tests -v
```
