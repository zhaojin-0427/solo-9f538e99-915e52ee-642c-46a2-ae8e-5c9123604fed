"""FastAPI application: revision-safe XBRL filing and reconciliation API."""
from __future__ import annotations

from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Header, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import storage
from .db import init_db
from .errors import ApiError
from .models import (CheckAck, InstanceOut, InstanceRevisionOut, PackageOut,
                     ReportList, ReportOut)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="XBRL 财报勾稽核查 API",
    description=(
        "保存可修订的 XBRL 实例与计算关系包（XSD + calculation linkbase），"
        "按 decimals 舍入闭区间核查全部加权等式。返回同时满足全部等式的金额见证，"
        "或相互矛盾的事实/关系；缺引用事实则整份待补。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def _api_error(_request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# ------------------------------------------------------------- packages
@app.post("/packages", response_model=PackageOut, status_code=201,
          tags=["packages"], summary="创建计算关系包（XSD + calculation linkbase）")
async def create_package(
    name: str = Form(..., description="关系包名称"),
    xsd: UploadFile = File(..., description="XSD taxonomy schema"),
    calculation: UploadFile = File(..., description="calculation linkbase XML"),
):
    return storage.create_package(name, await xsd.read(), await calculation.read())


@app.post("/packages/{package_id}/revisions", response_model=PackageOut,
          tags=["packages"], summary="修订关系包；Expected-Revision 过期则 412")
async def revise_package(
    package_id: str,
    xsd: UploadFile = File(...),
    calculation: UploadFile = File(...),
    expected_revision: int = Header(..., alias="Expected-Revision"),
):
    return storage.revise_package(
        package_id, expected_revision, await xsd.read(), await calculation.read()
    )


@app.get("/packages", response_model=list[PackageOut], tags=["packages"])
def list_packages():
    return storage.list_packages()


@app.get("/packages/{package_id}", response_model=PackageOut, tags=["packages"])
def get_package(package_id: str):
    return storage.get_package(package_id)


# ------------------------------------------------------------- instances
@app.post("/packages/{package_id}/instances", response_model=InstanceOut,
          status_code=201, tags=["instances"], summary="提交 XBRL 实例（首版）")
async def create_instance(package_id: str, instance: UploadFile = File(...)):
    return storage.create_instance(package_id, await instance.read())


@app.post("/instances/{instance_id}/revisions", response_model=InstanceOut,
          tags=["instances"],
          summary="更正实例；两个 Expected 修订任一过期或处理期间变化则 412")
async def revise_instance(
    instance_id: str,
    instance: UploadFile = File(..., description="更正后的 XBRL 实例"),
    expected_instance_revision: int = Header(
        ..., alias="Expected-Instance-Revision",
        description="提交者所见实例修订；非当前最新版则 412",
    ),
    expected_package_revision: int = Header(
        ..., alias="Expected-Package-Revision",
        description="提交者所见关系包修订；用于解析并记入新修订，非当前最新版则 412",
    ),
):
    # Parse + compare + write is blocking; run it in the threadpool so two
    # concurrent corrections really do race (and one is rejected).
    return await run_in_threadpool(
        storage.revise_instance,
        instance_id,
        expected_instance_revision,
        expected_package_revision,
        await instance.read(),
    )


@app.get("/instances", response_model=list[InstanceOut], tags=["instances"])
def list_instances(package_id: Optional[str] = Query(None)):
    return storage.list_instances(package_id)


@app.get("/instances/{instance_id}", response_model=InstanceOut, tags=["instances"])
def get_instance(instance_id: str):
    return storage.get_instance(instance_id)


@app.get("/instances/{instance_id}/revisions", response_model=list[InstanceRevisionOut],
         tags=["instances"],
         summary="全部实例修订；legacy 修订的 package_revision 为 null")
def list_instance_revisions(instance_id: str):
    return storage.list_instance_revisions(instance_id)


@app.get("/instances/{instance_id}/revisions/{revision}",
         response_model=InstanceRevisionOut, tags=["instances"])
def get_instance_revision(instance_id: str, revision: int):
    return storage.get_instance_revision(instance_id, revision)


@app.get("/instances/{instance_id}/revisions/{revision}/raw", tags=["instances"],
         summary="下载某版实例原始 XML")
def get_instance_revision_raw(instance_id: str, revision: int):
    data, media = storage.get_instance_revision_raw(instance_id, revision)
    return Response(content=data, media_type=media)


# ------------------------------------------------------------- checks / reports
@app.post("/instances/{instance_id}/checks", response_model=CheckAck,
          tags=["checks"],
          summary="核查并生成报告；两侧修订与所见不一致则 412 拒绝")
def run_check(
    instance_id: str,
    expected_instance_revision: int = Header(..., alias="Expected-Instance-Revision"),
    expected_package_revision: int = Header(..., alias="Expected-Package-Revision"),
    late: bool = Query(False, description="显式声明为迟到补报：允许落后修订，仅存历史"),
):
    return storage.run_check(
        instance_id, expected_instance_revision, expected_package_revision, late
    )


@app.get("/instances/{instance_id}/reports", response_model=ReportList,
         tags=["reports"], summary="当前报告与全部历史（含迟到、已过期）")
def list_reports(instance_id: str):
    return storage.list_reports(instance_id)


@app.get("/reports/{report_id}", response_model=ReportOut, tags=["reports"])
def get_report(report_id: str):
    return storage.get_report(report_id)


@app.get("/reports/{report_id}/snapshots/{kind}", tags=["reports"],
         summary="下载报告保存的两份原始依据与实例（instance|xsd|calc）")
def report_snapshot(report_id: str, kind: str):
    if kind not in ("instance", "xsd", "calc"):
        from .errors import BadPayload
        raise BadPayload("kind must be one of instance, xsd, calc")
    data, media = storage.get_report_snapshot(report_id, kind)
    return Response(content=data, media_type=media)
