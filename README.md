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
- 每个任务的进度条、已用时间、实际使用的编码器、解码后端与编码后端
- NVIDIA NVENC、Intel QSV、Intel/AMD VAAPI 对 H.264、HEVC、AV1 的独立解码/编码探测

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
| `VIDEO_CODEC` | `h264` | 输出视频编码格式：`h264`、`h265` 或 `av1`（别名：`auto`、`hevc`、`av01`）。自动探测 NVENC、QSV、VAAPI 编码器；目标硬件格式不可用或运行失败时使用 CPU 编码器。也可指定具体 FFmpeg 编码器名称。 |
| `AUDIO_CODEC` | `aac` | FFmpeg 音频编码器，例如 `aac`、`libopus` 或 `copy`。默认会重新编码音频。 |
| `OUTPUT_WIDTH` | 未设置 | 可选输出宽度，必须为正偶数；须与 `OUTPUT_HEIGHT` 同时设置。 |
| `OUTPUT_HEIGHT` | 未设置 | 可选输出高度，必须为正偶数；须与 `OUTPUT_WIDTH` 同时设置。 |
| `MAX_CONCURRENT_CONVERSIONS` | `1` | 最大同时转换数。其余稳定文件会排队等待。 |
| `POLL_INTERVAL_SECONDS` | `2` | 扫描目录的时间间隔。 |
| `FILE_STABILITY_SECONDS` | `5` | 输入文件保持不变多久后才开始排队。 |
| `WEB_PORT` | `5800` | 状态页面容器端口及映射到主机的端口。 |
| `FFMPEG_BIN` | `ffmpeg` | FFmpeg 可执行文件路径（在 Docker 外运行时可用）。 |
| `FFPROBE_BIN` | `ffprobe` | ffprobe 可执行文件路径（在 Docker 外运行时可用）。 |

`HOST_INPUT_DIR` 和 `HOST_OUTPUT_DIR` 是 Compose 插值变量，用于指定主机输入、输出目录（默认 `./input`、`./output`），不会传入容器；可在 Compose 所在目录的 `.env` 文件中设置。`WEB_PORT` 也可在 `.env` 中设置，并同步更改容器监听端口和主机端口映射。

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

选择 H.264、H.265 或 AV1 时，只需将 Compose 中的编码配置设为：

```yaml
      VIDEO_CODEC: h264
```

或：

```yaml
      VIDEO_CODEC: h265
```

或：

```yaml
      VIDEO_CODEC: av1
```

## 硬件加速

镜像使用 Jellyfin 官方 `jellyfin-ffmpeg8`，自动探测 Linux 下的 NVIDIA NVENC、Intel QSV 与 VAAPI（Intel iHD/i965、AMD RadeonSI）。对每种 codec，编码与解码能力分别用独立的实际 FFmpeg 操作探测；每个阶段优先用可用硬件，不支持或运行失败时该阶段单独回退 CPU。解码与编码可来自不同硬件后端，例如 VAAPI 解码后交给 NVENC 编码。支持的输出视频 codec 为 H.264、HEVC/H.265 和 AV1；具体能力取决于 GPU 世代、驱动、FFmpeg 构建与输入 profile。

### 硬件解码与转码

硬件解码和编码各自选择通过探测的设备，不要求来自同一厂商或后端：

- **NVIDIA：** 需要宿主机安装 NVIDIA 驱动和 NVIDIA Container Toolkit。镜像默认设置 `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`，无需额外配置；Compose 示例中的 `deploy.resources.reservations.devices` 仍需取消注释以将 GPU 暴露给容器。容器不会打包宿主机专有的 NVIDIA 驱动。
- **Intel：** QSV 与 VAAPI 使用映射进容器的 `/dev/dri` render node。iHD 适用于 Broadwell（Gen8）及更新平台，i965 适用于更早的平台。
- **AMD：** Linux 上通过 VAAPI / Mesa RadeonSI，依赖宿主机 AMDGPU 内核驱动、固件和 `/dev/dri` 映射。编码格式受 GPU 世代限制；部分型号（如 RX 6400/6500）没有视频编码器。

当解码和编码后端不同，或设置 `OUTPUT_WIDTH` / `OUTPUT_HEIGHT` 时，帧会在后端间经系统内存转换；比例缩放与补边由 CPU 执行。后端相同且不需要缩放时可直接传递硬件帧。

状态页对三个后端和三种 codec 分别展示解码/编码能力。编码探测会输出小型合成视频；解码探测会先生成对应格式的测试视频，再尝试使用硬件解码。实际转换会按输入文件尝试所有可初始化的硬件解码设备，不因输出 codec 不同而跳过潜在可用的解码器；该输入无法硬解时再单独回退 CPU。FFmpeg 是否编入编码器不是硬件可用的充分条件；只有实际探测成功才优先选用。真实媒体的位深、profile 或像素格式仍可能超出设备能力，此时仅失败的解码或编码阶段改用 CPU。

自动编码后端优先顺序为 NVENC、QSV、VAAPI。显式设置具体 FFmpeg 编码器（如 `h264_nvenc`、`hevc_qsv` 或 `av1_vaapi`）时会使用该编码器；若相应设备无法初始化，任务会明确失败。

Compose 直接拉取 GHCR 多架构镜像。Intel/AMD 默认映射 `/dev/dri`，启动前确认设备存在并允许容器访问。NVIDIA 主机需先安装驱动和 Container Toolkit，再取消 Compose 文件中完整 `deploy.resources.reservations.devices` 段落的注释。纯 NVIDIA 主机还需移除 `devices: /dev/dri` 段落，否则主机不存在该路径时容器无法启动。随后执行 `docker compose -f docker-compose.yaml up -d`；可用 `docker compose exec video-converter nvidia-smi` 确认 GPU 已传入容器。

Intel/AMD 可通过 `docker compose exec video-converter vainfo --display drm --device /dev/dri/renderD128` 查看 VAAPI profiles。参考 [Jellyfin Intel](https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/intel/)、[AMD](https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/amd/) 与 [NVIDIA](https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/nvidia/) 文档。

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

`.github/workflows/publish-image.yml` 会在构建前运行单元测试并校验 Compose 配置；Pull Request 仅验证、不发布镜像。推送到默认分支或推送版本标签（例如 `v1.2.3`）时，会构建多架构 `linux/amd64`、`linux/arm64` 镜像并发布到 GitHub Container Registry。默认分支还会更新 `latest` 标签。工作流需要仓库 Actions 的 `packages: write` 权限（已在工作流中声明）。首次发布后，如需公开拉取，在 GitHub Packages 设置中将该容器包的可见性改为 Public。

## 本地运行测试

需要 Python 3.10 或更高版本。单元测试仅使用 Python 标准库：

```powershell
python -m unittest discover -s tests -v
```
