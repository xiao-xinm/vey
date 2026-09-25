from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from vey.domain import VeyError


def install_errors(app: FastAPI):
    @app.exception_handler(VeyError)
    async def known_error(request: Request, error: VeyError):
        return JSONResponse(
            {"error": error.code, "message": error.message}, status_code=error.status
        )

    @app.exception_handler(Exception)
    async def unknown_error(request: Request, error: Exception):
        # Do not return database URLs, SDK exception strings or raw tool output.
        return JSONResponse(
            {"error": "internal_error", "message": "服务暂时不可用；未确认成功的操作请先核对状态"},
            status_code=503,
        )
