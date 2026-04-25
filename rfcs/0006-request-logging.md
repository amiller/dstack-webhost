# RFC 0006: Request Logging and Observability

## Summary
Add structured access logging to the ingress proxy so every proxied request is logged with method, path, status code, duration, and client info.

## Problem
The ingress logs nothing about proxied requests. No access log, no timing, no status codes. Makes debugging very hard — when something goes wrong there's zero visibility into what requests were made and what happened to them.

## Files to Modify
- `proxy/ingress.py` — add logging in the `handle()` method

## Implementation
1. Add Python `logging` (or `structlog`) setup to ingress
2. At the start of each request, record `time.monotonic()` start time
3. After the response is sent (or on error), log a structured entry:
   - `method`: GET/POST/etc
   - `path`: request path
   - `status`: HTTP status code returned
   - `duration_ms`: wall clock time in milliseconds
   - `client_ip`: from request.remote
   - `upstream`: which runtime/host was targeted
   - `error`: exception message if one occurred
4. Use JSON log format for machine parseability
5. Log level: INFO for successful requests, WARNING for 4xx, ERROR for 5xx and exceptions
6. Do NOT log request/response bodies (privacy and size)
7. Add a configurable log level via environment variable (default INFO)

## Testing & Validation Requirements
- Send 5 different requests (GET, POST, 404, 500, timeout) through the ingress
- Verify each appears in the log with correct method, path, status, and duration
- Verify duration is a reasonable number (not 0, not negative)
- Verify JSON format is valid and parseable with `json.loads()`
- Verify 4xx logs at WARNING level and 5xx at ERROR level
- Verify bodies are NOT present in any log entry
- Verify the log line is emitted AFTER the response is sent (not before)

## Report Requirements
- Show the logging code added with line numbers
- Include sample log output for various request types
- Show how to filter logs with `jq` or `grep` for common debugging patterns
