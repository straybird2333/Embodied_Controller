# 公网文件上传/下载测速

接口使用 RoboICL 服务访问 key。下载数据在服务器本地由一次三相机 capture 生成：三张 640×480 PNG 各复制 10 份，打成不压缩的 tar，共 30 个文件。当前归档约 10.6 MB。上传接口流式接收最多 64 MiB 的二进制数据，计算 SHA-256 和服务端接收耗时后丢弃上传内容；不永久保存用户上传的数据。

接口每分钟最多允许同一 API key 发起 20 次上传/下载。服务端测速记录按 API key 隔离，保留 7 天。

## 检查基准文件

```sh
curl -fsS \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/transfer-test/info" | jq
```

## 测下载速度

`curl` 的 `time_total` 和 `speed_download` 是客户端测得的公网端到端结果。响应头中的测试 ID 可查询服务端写出计时：

```sh
DOWNLOAD_FILE="${TMPDIR:-/tmp}/roboicl-transfer-benchmark.tar"
DOWNLOAD_HEADERS="${TMPDIR:-/tmp}/roboicl-transfer-benchmark.headers"
curl -fsS \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  -D "$DOWNLOAD_HEADERS" \
  -o "$DOWNLOAD_FILE" \
  -w 'download_bytes=%{size_download} client_seconds=%{time_total} client_bytes_per_second=%{speed_download}\n' \
  "$API/v1/transfer-test/download"

DOWNLOAD_TEST_ID=$(awk 'tolower($1)=="x-transfer-test-id:" {gsub("\\r", "", $2); print $2}' "$DOWNLOAD_HEADERS" | tail -n 1)
curl -fsS \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/transfer-test/records/$DOWNLOAD_TEST_ID" | jq
```

## 测上传速度

把刚才下载的归档原样上传。接口会返回服务端收到的字节数、耗时、MiB/s 和 SHA-256；`curl` 同时给出客户端端到端时间。`matches_download_payload: true` 表示上传文件与下载文件一致。

```sh
UPLOAD_RESULT="${TMPDIR:-/tmp}/roboicl-transfer-upload.json"
curl -fsS \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  -H 'Content-Type: application/octet-stream' \
  --data-binary "@$DOWNLOAD_FILE" \
  -o "$UPLOAD_RESULT" \
  -w 'upload_bytes=%{size_upload} client_seconds=%{time_total} client_bytes_per_second=%{speed_upload}\n' \
  "$API/v1/transfer-test/upload"
jq . "$UPLOAD_RESULT"
```

## 查询最近记录

```sh
curl -fsS \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/transfer-test/records" | jq
```

服务端下载计时从开始写响应体到写完/中断；客户端 `curl` 时间覆盖公网连接、代理和完整接收，更适合衡量实际下载速度。上传服务端计时覆盖接收请求体；Nginx 针对此路径禁用了请求缓冲，因此包含公网数据进入服务端的时间。两侧计时可以一起比较。
