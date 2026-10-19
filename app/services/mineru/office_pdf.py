"""LibreOffice只读转换与不可变PDF回执；不启动Microsoft Office，不调用云端。"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
from time import perf_counter
from uuid import uuid4

from app.core.business_logging import log_event
from app.core.file_formats import OFFICE_TYPES, validate_file_content
from app.parsers.common import ParserError
from app.parsers.routing.contracts import StructuralSignals
from app.parsers.routing.inspector import inspect_document
from app.services.cache_files import cache_guard
from app.services.document_processing import check_source_hash, source_path, DocumentSourceError
from app.services.mineru.errors import MinerUError
from app.services.parse_cache import ParseCache, atomic_write, canonical, digest

VERSION = "libreoffice-pdf-v1"
# 每次使用独立空配置，禁宏/活动OLE/DDE和非可信外链；不是操作系统级网络沙箱。
PROFILE = b'''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">
<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
</item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update">
<prop oor:name="Link" oor:op="fuse"><value>2</value></prop>
<prop oor:name="Field" oor:op="fuse"><value>false</value></prop>
<prop oor:name="Chart" oor:op="fuse"><value>false</value></prop>
</item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update">
<prop oor:name="Link" oor:op="fuse"><value>1</value></prop>
</item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load">
<prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>1</value></prop>
<prop oor:name="ODFRecalcMode" oor:op="fuse"><value>1</value></prop>
</item>
</oor:items>'''
FILTERS = {"doc": "writer_pdf_Export", "docx": "writer_pdf_Export",
           "xls": "calc_pdf_Export", "xlsx": "calc_pdf_Export",
           "ppt": "impress_pdf_Export", "pptx": "impress_pdf_Export"}


def file_hash(path):
    """以实际文件字节校验原件、引擎和派生PDF，不输出私有路径。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def engine_fingerprint(executable):
    """绑定启动器、实际引擎及版本描述；不执行版本命令，不把可执行路径放入缓存身份。"""
    executable = executable.resolve()
    if executable.name.casefold() not in {"soffice", "soffice.exe", "soffice.com"}:
        raise MinerUError("OFFICE_PDF_DEPENDENCY", "LIBREOFFICE_EXECUTABLE必须指向LibreOffice的soffice入口")
    files = [executable, executable.parent / "soffice.bin"]
    files += [path for path in (executable.parent / "version.ini", executable.parent / "versionrc") if path.is_file()]
    return digest(canonical({path.name: file_hash(path) for path in files}))


def safe_path(path, root):
    """仅接受服务自有目录内的普通路径，逐级拒绝符号链接和Windows junction。"""
    path, root = Path(path).absolute(), Path(root).resolve()
    if not path.resolve().is_relative_to(root):
        raise MinerUError("OFFICE_PDF_PATH", "Office转换路径不安全，未运行转换或提交云任务")
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                raise MinerUError("OFFICE_PDF_PATH", "Office转换路径包含不安全链接")
        if item == root:
            break
    return path


def conversion_request(document, settings):
    """API只冻结本地引擎/安全配置，不运行转换；安装缺失在新云提交之前明确报错。"""
    executable = Path(settings.libreoffice_executable)
    if not executable.is_absolute():
        located = shutil.which(str(executable))
        if located is None:
            raise MinerUError("OFFICE_PDF_DEPENDENCY", "请手动安装LibreOffice并配置LIBREOFFICE_EXECUTABLE")
        executable = Path(located)
    try:
        identity = {"version": VERSION, "source_hash": document.file_hash, "file_type": document.file_type,
                    "engine_sha256": engine_fingerprint(executable), "profile_sha256": digest(PROFILE)}
    except OSError:
        raise MinerUError("OFFICE_PDF_DEPENDENCY", "LibreOffice程序无法读取，请手动核对安装和配置") from None
    if document.file_type not in OFFICE_TYPES:
        raise MinerUError("OFFICE_PDF_IDENTITY", "非Office原件不能使用Office转换配方")
    return {"identity": identity, "key": digest(canonical(identity)), "executable": str(executable.resolve()),
            "timeout_seconds": settings.office_pdf_timeout_seconds}


def valid_identity(identity):
    """检查缓存中的固定转换配方，不加载引擎；Reindex不要求重新安装/运行LibreOffice。"""
    return (isinstance(identity, dict) and set(identity) == {
        "version", "source_hash", "file_type", "engine_sha256", "profile_sha256"}
        and identity["version"] == VERSION and isinstance(identity["file_type"], str) and identity["file_type"] in OFFICE_TYPES
        and identity["profile_sha256"] == digest(PROFILE)
        and all(isinstance(identity[name], str) and len(identity[name]) == 64
                and all(char in "0123456789abcdef" for char in identity[name])
                for name in ("source_hash", "engine_sha256")))


def directory_for(settings, identity):
    """按原件及冻结引擎身份寻址共享转换产物，首次有效回执发布后不再覆盖或自动重渲染。"""
    if not valid_identity(identity):
        raise MinerUError("OFFICE_PDF_IDENTITY", "Office转换配方无效，未重新渲染")
    cache = ParseCache(settings)
    return safe_path(cache.path(".office-pdf", digest(canonical(identity))), cache.root)


def validate_receipt(receipt, identity):
    """核对派生Hash、真实页摘要和转换身份，缓存/数据库恢复不能凭页数猜来源。"""
    try:
        if (not valid_identity(identity) or receipt["identity"] != identity
                or receipt["key"] != digest(canonical(identity))
                or receipt["page_semantics"] != "converted_office"
                or type(receipt["size_bytes"]) is not int or receipt["size_bytes"] <= 0
                or len(receipt["pdf_sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in receipt["pdf_sha256"])):
            raise ValueError
        signals = StructuralSignals.model_validate(receipt["pdf_signals"])
        if (signals.page_count is None or len(signals.pages) != signals.page_count
                or any(page.page != index for index, page in enumerate(signals.pages, 1))):
            raise ValueError
        return signals
    except (KeyError, TypeError, ValueError):
        raise MinerUError("OFFICE_PDF_IDENTITY", "Office转换回执不完整或身份不符") from None


def checked_conversion(document, receipt, settings):
    """恢复只读取原冻结PDF并核对双端Hash；缺失/损坏不启动引擎或重新付费。"""
    identity = receipt.get("identity") if isinstance(receipt, dict) else None
    validate_receipt(receipt, identity)
    if identity["source_hash"] != document.file_hash or identity["file_type"] != document.file_type:
        raise MinerUError("OFFICE_PDF_IDENTITY", "Office原件与转换回执不匹配")
    path = safe_path(directory_for(settings, identity) / f"{receipt['pdf_sha256']}.pdf", ParseCache(settings).root)
    try:
        check_source_hash(document, source_path(document))
        if path.stat().st_size != receipt["size_bytes"] or file_hash(path) != receipt["pdf_sha256"]:
            raise MinerUError("OFFICE_PDF_CHANGED", "已冻结的Office转换PDF已变化，未重新渲染或提交")
    except (OSError, ParserError, DocumentSourceError):
        raise MinerUError("OFFICE_PDF_CHANGED", "Office原件或冻结PDF无法校验，未重新渲染") from None
    return path


def stop_conversion(process):
    """超时仅停止本次独立配置启动的进程树，不按程序名关闭用户其他LibreOffice实例。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run([str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"),
                        "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


def run_converter(request, source, work, kind):
    """在独立空profile中无界面导出；不使用shell，不输出原件名/路径/LibreOffice原始日志。"""
    profile, output = work / "profile", work / "output"
    (profile / "user").mkdir(parents=True)
    output.mkdir()
    with (profile / "user/registrymodifications.xcu").open("xb") as stream:
        stream.write(PROFILE)
    # 只把原件副本交给转换器；即使引擎修改输入，也不能写回服务保存的原件。
    copied = work / f"input.{kind}"
    with copied.open("xb") as target, source.open("rb") as original:
        shutil.copyfileobj(original, target)
    if file_hash(copied) != request["identity"]["source_hash"]:
        raise MinerUError("OFFICE_PDF_CHANGED", "Office转换输入副本Hash不符，未运行引擎")
    with copied.open("rb") as stream:
        validate_file_content(stream, kind)
    command = [request["executable"], f"-env:UserInstallation={profile.as_uri()}", "--headless",
               "--nologo", "--nodefault", "--nofirststartwizard", "--norestore",
               "--convert-to", f"pdf:{FILTERS[kind]}", "--outdir", str(output), str(copied)]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    # 不把API Token/数据库连接等应用环境变量继承给转换进程；这不代替OS级网络隔离。
    allowed_env = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
                   "PROGRAMFILES", "PROGRAMFILES(X86)", "HOME", "LANG", "LC_ALL", "FONTCONFIG_PATH"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed_env}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, shell=False, env=environment, **flags)
    try:
        return_code = process.wait(timeout=request["timeout_seconds"])
    except subprocess.TimeoutExpired:
        try:
            stop_conversion(process)
        except (OSError, subprocess.SubprocessError):
            pass  # 暂存目录保留；没有完成回执的产物绝不发布或送云。
        raise MinerUError("OFFICE_PDF_TIMEOUT", "Office转换超时，未发布结果；请核对本次转换进程") from None
    if return_code != 0 or not (output / "input.pdf").is_file():
        raise MinerUError("OFFICE_PDF_FAILED", "LibreOffice未生成有效PDF，未提交云任务")
    return output / "input.pdf"


def ensure_conversion(document, request, settings):
    """只在CPU任务执行首次转换，完整核对后独占发布PDF/回执；失败产物不被视为缓存命中。"""
    identity = request.get("identity") if isinstance(request, dict) else None
    if (not valid_identity(identity) or identity["source_hash"] != document.file_hash
            or identity["file_type"] != document.file_type or request.get("key") != digest(canonical(identity))
            or not isinstance(request.get("executable"), str) or not Path(request["executable"]).is_absolute()
            or type(request.get("timeout_seconds")) is not int or not 30 <= request["timeout_seconds"] <= 3600):
        raise MinerUError("OFFICE_PDF_IDENTITY", "Office转换请求不属于当前原件")
    directory = directory_for(settings, identity)
    started = perf_counter()
    try:
        with cache_guard(directory):
            pointer = safe_path(directory / "receipt.json", ParseCache(settings).root)
            if pointer.exists():
                receipt = json.loads(ParseCache(settings).read_bytes(pointer, 4 * 1024**2))
                validate_receipt(receipt, identity)
                path = checked_conversion(document, receipt, settings)
                log_event("office.pdf_reused", doc_id=document.doc_id,
                          page_count=receipt["pdf_signals"]["page_count"], parse_cache_hit=True)
                return path, receipt
            executable = Path(request["executable"])
            if engine_fingerprint(executable) != identity["engine_sha256"]:
                raise MinerUError("OFFICE_PDF_CHANGED", "LibreOffice引擎已变化，请核对部署；未更换冻结配方")
            check_source_hash(document, source_path(document))
            work = safe_path(directory / f"work-{uuid4()}", ParseCache(settings).root)
            work.mkdir()
            log_event("office.pdf_started", doc_id=document.doc_id, file_type=document.file_type)
            generated = run_converter(request, source_path(document), work, document.file_type)
            inspection = inspect_document(generated, "pdf", settings)
            if inspection.uncertain or inspection.signals.page_count is None:
                raise MinerUError("OFFICE_PDF_INVALID", "转换PDF无法可靠计页或预检，未提交云任务")
            check_source_hash(document, source_path(document))
            if engine_fingerprint(executable) != identity["engine_sha256"]:
                raise MinerUError("OFFICE_PDF_CHANGED", "转换期间LibreOffice引擎已变化")
            receipt = {"identity": identity, "key": request["key"], "pdf_sha256": file_hash(generated),
                       "size_bytes": generated.stat().st_size, "page_semantics": "converted_office",
                       "pdf_signals": inspection.signals.model_dump(mode="json")}
            validate_receipt(receipt, identity)
            destination = safe_path(directory / f"{receipt['pdf_sha256']}.pdf", ParseCache(settings).root)
            if destination.exists():
                if file_hash(destination) != receipt["pdf_sha256"]:
                    raise MinerUError("OFFICE_PDF_CHANGED", "转换PDF的已有Hash目标损坏，未覆盖")
            else:
                with destination.open("xb") as target, generated.open("rb") as source:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
            # 回执最后原子发布；崩溃遗留的无回执PDF保留，不据此猜成功或覆盖其他产物。
            atomic_write(pointer, canonical(receipt))
            log_event("office.pdf_completed", doc_id=document.doc_id, page_count=inspection.signals.page_count,
                      size_bytes=receipt["size_bytes"], duration_ms=(perf_counter() - started) * 1000)
            return destination, receipt
    except MinerUError as error:
        log_event("office.pdf_failed", doc_id=document.doc_id, error_code=error.code, error_type=type(error).__name__)
        raise
    except (OSError, ValueError, ParserError, DocumentSourceError, subprocess.SubprocessError) as error:
        log_event("office.pdf_failed", doc_id=document.doc_id, error_code="OFFICE_PDF_FAILED", error_type=type(error).__name__)
        raise MinerUError("OFFICE_PDF_FAILED", "Office转换或回执保存失败，未提交云任务") from None


def bound_decision(decision, receipt):
    """为Office逻辑页绑定实际转换PDF摘要；原Office结构/快照信号保持不变。"""
    pdf = validate_receipt(receipt, receipt["identity"])
    return decision.model_copy(update={"signals": decision.signals.model_copy(update={
        "page_count": pdf.page_count, "pages": pdf.pages, "pdf_coverage_version": pdf.pdf_coverage_version,
        "office_pdf_sha256": receipt["pdf_sha256"]})})
