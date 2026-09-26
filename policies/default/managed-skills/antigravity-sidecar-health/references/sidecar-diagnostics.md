# Reference: Sidecar Diagnostic Commands and Launch Specs

Technical specifications and verification procedures for the Antigravity masking sidecar.

## Architecture
- **Port:** `127.0.0.1:45123`
- **Source Script:** `C:\Users\wkiri\.veyyon\sidecar\antigravity-masking-proxy.ts`
- **Runtime:** Bun (`C:\Users\wkiri\.bun\bin\bun.exe`)
- **Upstreams:** `daily-cloudcode-pa.googleapis.com`, `daily-cloudcode-pa.sandbox.googleapis.com`

## Health Probe Specification
```bash
curl -s --max-time 5 http://127.0.0.1:45123/health
```

Expected JSON response format:
```json
{
  "status": "ok",
  "service": "veyyon-antigravity-sidecar",
  "upstreams": [
    "https://daily-cloudcode-pa.googleapis.com",
    "https://daily-cloudcode-pa.sandbox.googleapis.com"
  ],
  "port": 45123,
  "uptimeSec": 12345
}
```

## Launch Parameters
```
launch(
  op="start",
  name="antigravity-sidecar",
  application="C:\\Users\\wkiri\\.bun\\bin\\bun.exe",
  args=["run", "C:\\Users\\wkiri\\.veyyon\\sidecar\\antigravity-masking-proxy.ts"],
  detached=True,
  persist=True,
  ready={"port": 45123, "timeout": 40}
)
```
