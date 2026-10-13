import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

LOGGER = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>视频转换器状态</title>
<style>
:root{color-scheme:dark;--bg:#10141d;--panel:#1a2230;--muted:#9ba9bc;--text:#eef4fc;--accent:#65b5ff;--green:#55d6a0;--red:#ff7878}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,"Microsoft YaHei",sans-serif}
main{max-width:1100px;margin:36px auto;padding:0 20px}h1{font-size:26px;margin:0 0 8px}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px;margin:24px 0}
.card,section{background:var(--panel);border:1px solid #293447;border-radius:12px;padding:18px}.value{font-size:28px;font-weight:700;margin-top:6px}
section{margin:14px 0}h2{font-size:18px;margin:0 0 14px}.hardware{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.chip{padding:12px;border-radius:9px;background:#222d3d}.ready{color:var(--green)}.unready{color:var(--muted)}
.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 8px;border-bottom:1px solid #303b4e;white-space:nowrap}th{color:var(--muted);font-weight:500}
.bar{height:7px;background:#303b4e;border-radius:8px;min-width:100px;margin-top:7px;overflow:hidden}.fill{height:100%;background:var(--accent);border-radius:8px}
.status{font-weight:600}.failed{color:var(--red)}.empty{text-align:center;padding:24px;color:var(--muted)}footer{margin:20px 0;color:var(--muted);font-size:13px}
</style>
</head>
<body><main>
<h1>视频转换器</h1><div class="muted" id="service">正在连接服务…</div>
<div class="cards">
<div class="card"><div class="muted">等待队列</div><div class="value" id="queued">—</div></div>
<div class="card"><div class="muted">正在转换</div><div class="value" id="running">—</div></div>
<div class="card"><div class="muted">已完成</div><div class="value" id="completed">—</div></div>
<div class="card"><div class="muted">失败</div><div class="value" id="failed">—</div></div>
</div>
<section><h2>硬件加速检测</h2><div class="muted" id="codec"></div><div class="hardware" id="hardware"></div></section>
<section><h2>队列与转换进度</h2><div class="table-wrap"><table><thead><tr><th>文件</th><th>状态</th><th>进度</th><th>编码器</th><th>后端</th><th>错误</th></tr></thead><tbody id="jobs"></tbody></table></div></section>
<footer id="paths"></footer>
</main>
<script>
const labels={queued:"排队中",running:"转换中",completed:"已完成",failed:"失败"};
function safe(value){const node=document.createElement("span");node.textContent=value??"";return node.innerHTML}
async function refresh(){try{
 const response=await fetch("/api/status",{cache:"no-store"});if(!response.ok)throw Error(response.status);
 const data=await response.json();document.querySelector("#service").textContent=data.running?"服务运行中 · 自动每 2 秒刷新":"服务正在停止";
 for(const name of ["queued","running","completed","failed"])document.querySelector("#"+name).textContent=data.counts[name];
 document.querySelector("#codec").textContent="目标编码格式："+data.video_codec+"；同时转换上限："+data.max_concurrent_conversions;
 document.querySelector("#hardware").innerHTML=data.hardware.map(item=>`<div class="chip"><strong>${safe(item.backend.toUpperCase())} · ${safe(item.codec)}</strong><div class="${item.decode_ready?"ready":"unready"}">硬件解码：${item.decode_ready?"支持":"不支持"}</div><div class="${item.encode_ready?"ready":"unready"}">硬件编码：${item.encode_ready?"支持":"不支持"}</div><small>解码：${safe(item.decode_reason)}<br>编码：${safe(item.encode_reason)}</small></div>`).join("")||'<div class="muted">暂无检测结果</div>';
 const sorted={running:0,queued:1,failed:2,completed:3};data.jobs.sort((a,b)=>sorted[a.status]-sorted[b.status]);
 document.querySelector("#jobs").innerHTML=data.jobs.map(job=>{const pct=job.progress==null?null:Math.round(job.progress);
  return `<tr><td>${safe(job.file)}</td><td class="status ${job.status==="failed"?"failed":""}">${labels[job.status]||safe(job.status)}</td>
  <td>${pct==null?(job.out_time?safe(job.out_time):"—"):`${pct}%`}<div class="bar"><div class="fill" style="width:${pct??0}%"></div></div></td>
  <td>${safe(job.encoder||"—")}</td><td>${safe(job.backend||"—")}</td><td title="${safe(job.error)}">${safe(job.error||"—")}</td></tr>`}).join("")||'<tr><td colspan="6" class="empty">队列为空</td></tr>';
 document.querySelector("#paths").textContent=`输入：${data.input_dir}　·　输出：${data.output_dir}`;
 }catch(error){document.querySelector("#service").textContent="无法连接服务状态："+error}}
refresh();setInterval(refresh,2000);
</script></body></html>"""


def create_server(host: str, port: int, status: Any) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                content = PAGE.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            elif self.path == "/api/status":
                content = json.dumps(status.snapshot(), ensure_ascii=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format_string: str, *args: object) -> None:
            LOGGER.info("Status web: " + format_string, *args)

    return ThreadingHTTPServer((host, port), Handler)


def start_server(host: str, port: int, status: Any) -> tuple[ThreadingHTTPServer, Thread]:
    server = create_server(host, port, status)
    thread = Thread(target=server.serve_forever, name="status-web", daemon=True)
    thread.start()
    return server, thread
