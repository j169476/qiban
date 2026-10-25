"""Loopback is a location, not authentication: every write checks its origin."""
from starlette.responses import JSONResponse


class LocalBoundary:
    def __init__(self, app, *, origin, max_body=65536, upload_path=None):
        from urllib.parse import urlsplit

        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            raise ValueError("A fixed IPv4 loopback origin is required")
        self.app, self.origin, self.host, self.max_body = app, origin, parsed.netloc, max_body
        self.upload_path = upload_path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {}
        duplicate = False
        for key, value in scope.get("headers", []):
            key = key.lower()
            if key in (b"host", b"origin", b"content-length") and key in headers:
                duplicate = True
            headers[key] = value.decode("latin-1")
        if duplicate or headers.get(b"host") != self.host:
            return await JSONResponse({"error": "invalid_host"}, status_code=400)(scope, receive, send)
        if scope["method"] not in ("GET", "HEAD") and scope["path"] != "/control":
            if headers.get(b"origin") != self.origin:
                return await JSONResponse({"error": "invalid_origin"}, status_code=403)(scope, receive, send)
        # Even GET responses must not be readable via a different local origin.
        if headers.get(b"origin") not in (None, self.origin):
            return await JSONResponse({"error": "invalid_origin"}, status_code=403)(scope, receive, send)
        body_limit = 8 * 1024 * 1024 if self.upload_path == scope["path"] and scope["method"] == "POST" else self.max_body
        try:
            declared = int(headers.get(b"content-length", "0"))
        except ValueError:
            declared = body_limit + 1
        if not 0 <= declared <= body_limit:
            return await JSONResponse({"error": "body_too_large"}, status_code=413)(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > body_limit:
                return await JSONResponse({"error": "body_too_large"}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def body():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        async def secured(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []),
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'none'; style-src 'self'; script-src 'self'; "
                     b"connect-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")]
            await send(message)

        return await self.app(scope, body, secured)
