"""Superadmin-only internal simulation pages and CSRF-protected compute APIs."""
import json
from datetime import datetime

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for

from app import db
from app.activity import record_audit
from app.decorators import superadmin_required
from app.i18n import t
from app.models import SimulationMaterial, SimulationSection, SimulationModel
from app.simulation.evaluation import evaluate
from app.simulation.exchange import import_model, export_model
from app.simulation.runner import run
from app.simulation.schema import SimulationError, MAX_INPUT_BYTES, number, validate_libraries
from app.simulation.service import libraries, prepare, fingerprint, MATERIAL_FIELDS, SECTION_FIELDS
from app.transactions import begin_serialized_write
from app.utils import local_now

simulation_bp = Blueprint("simulation", __name__, url_prefix="/simulation")


@simulation_bp.app_template_global("dict_from_translations")
def dict_from_translations(strings):
    return {source: t(source) for source in strings}


@simulation_bp.before_request
def bounded_request():
    if request.content_length and request.content_length > 1_100_000:
        raise SimulationError("size_limit", "导入文件过大，最大 1 MB。")
    if request.is_json and request.content_length and request.content_length > MAX_INPUT_BYTES:
        raise SimulationError("size_limit", "模型数据过大。")


@simulation_bp.errorhandler(SimulationError)
def simulation_error(exc):
    status = {"busy": 429, "timeout": 408, "resource_limit": 503, "conflict": 409}.get(exc.code, 422)
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify(ok=False, code=exc.code, error=t(str(exc))), status
    return render_template("error.html", message=str(exc)), status


def get_model(model_id):
    row = db.session.get(SimulationModel, model_id)
    if row is None: abort(404)
    return row


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict): raise SimulationError("invalid_model", "模型格式无效，请检查表格数据。")
    return data


def check_version(row, data):
    if type(data.get("version")) is not int or data["version"] != row.version:
        raise SimulationError("conflict", "模型已被修改，请刷新页面后重试。")


def text_field(value, maximum, *, required=True):
    if not isinstance(value, str) or len(value.strip()) > maximum or (required and not value.strip()):
        raise SimulationError("invalid_model", "名称或说明为空或超过长度限制。")
    return value.strip()


def localize_evaluation(evaluation):
    return {"provider": "rules", **{key: [t(entry["text"], **entry["values"]) for entry in evaluation[key]]
            for key in ("advantages", "shortcomings", "suggestions")}}


@simulation_bp.get("/")
@superadmin_required
def index():
    return render_template("simulation/index.html", models=SimulationModel.query.order_by(SimulationModel.id).all())


@simulation_bp.post("/new")
@superadmin_required
def new():
    source = SimulationModel.query.order_by(SimulationModel.id).first()
    if source is None: abort(404)
    row = SimulationModel(name=t("新的模拟模型"), description="", data_json=source.data_json)
    db.session.add(row); db.session.flush()
    record_audit("simulation.create", summary="新增线上模拟测试模型。", target_type="simulation_model", target_id=row.id)
    return redirect(url_for("simulation.editor", model_id=row.id))


@simulation_bp.get("/models/<int:model_id>")
@superadmin_required
def editor(model_id):
    row = get_model(model_id)
    materials, sections = libraries()
    previous = json.loads(row.result_json) if row.result_json else None
    stale = False
    if previous:
        try: stale = previous["fingerprint"] != fingerprint(prepare(json.loads(row.data_json)))
        except SimulationError: stale = True
    previous = None if stale else previous
    if previous: previous["evaluation"] = localize_evaluation(previous["evaluation"])
    bootstrap = dict(id=row.id, name=row.name, description=row.description, version=row.version,
                     model=json.loads(row.data_json), materials=materials, sections=sections,
                     previous=previous, stale=stale)
    return render_template("simulation/editor.html", row=row, bootstrap=bootstrap)


@simulation_bp.post("/models/<int:model_id>")
@superadmin_required
def save(model_id):
    data = json_body()
    payload = prepare(data.get("model"))
    name = text_field(data.get("name"), 100)
    description = text_field(data.get("description", ""), 500, required=False)
    begin_serialized_write()
    row = get_model(model_id); db.session.refresh(row)
    check_version(row, data)
    row.data_json = json.dumps(payload["model"], ensure_ascii=False, allow_nan=False)
    row.name, row.description = name, description
    row.version += 1; row.updated_at = local_now(); row.result_json = None
    db.session.flush()
    record_audit("simulation.save", summary="保存线上模拟模型。", target_type="simulation_model", target_id=row.id,
                 payload={"version": row.version})
    return jsonify(ok=True, version=row.version)


@simulation_bp.post("/models/<int:model_id>/calculate")
@superadmin_required
def calculate(model_id):
    data = json_body(); row = get_model(model_id); check_version(row, data)
    version = row.version
    payload = prepare(json.loads(row.data_json)); signature = fingerprint(payload)
    # No database write lock is held during numerical work.
    result = run(payload, current_app.config["DATA_DIR"], timeout=8)
    evaluation = evaluate(payload["model"], result)  # rules only; no provider/key/network
    begin_serialized_write()
    db.session.refresh(row)
    if row.version != version or signature != fingerprint(prepare(json.loads(row.data_json))):
        raise SimulationError("conflict", "模型已被修改，请刷新页面后重试。")
    snapshot = dict(fingerprint=signature, result=result, evaluation=evaluation, inputs=payload,
                    computed_at=local_now().isoformat(timespec="seconds"))
    row.result_json = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    db.session.flush()
    record_audit("simulation.calculate", summary="完成线上模拟计算。", target_type="simulation_model", target_id=row.id,
                 payload={"version": version, "summary": result["summary"]})
    return jsonify(ok=True, result=result, evaluation=localize_evaluation(evaluation))


@simulation_bp.post("/models/<int:model_id>/copy")
@superadmin_required
def copy_model(model_id):
    source = get_model(model_id)
    row = SimulationModel(name=(source.name[:85] + " · " + t("副本")), description=source.description, data_json=source.data_json)
    db.session.add(row); db.session.flush()
    record_audit("simulation.copy", summary="复制线上模拟模型。", target_type="simulation_model", target_id=row.id)
    return redirect(url_for("simulation.editor", model_id=row.id))


@simulation_bp.get("/models/<int:model_id>/export/<file_format>")
@superadmin_required
def export(model_id, file_format):
    if file_format not in ("xlsx", "csv"): abort(404)
    row = get_model(model_id)
    output = export_model(json.loads(row.data_json), file_format)
    return send_file(output, as_attachment=True, download_name=f"simulation-{row.id}.{file_format}",
                     mimetype="text/csv; charset=utf-8" if file_format == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@simulation_bp.post("/import")
@superadmin_required
def import_file():
    upload = request.files.get("file")
    if upload is None: raise SimulationError("import_error", "请选择 Excel 或 CSV 文件。")
    payload = prepare(import_model(upload))
    name = text_field(request.form.get("name") or t("导入的模拟模型"), 100)
    row = SimulationModel(name=name, description="", data_json=json.dumps(payload["model"], ensure_ascii=False))
    db.session.add(row); db.session.flush()
    record_audit("simulation.import", summary="导入线上模拟模型。", target_type="simulation_model", target_id=row.id)
    return redirect(url_for("simulation.editor", model_id=row.id))


@simulation_bp.get("/libraries")
@superadmin_required
def library():
    materials, sections = libraries()
    return render_template("simulation/libraries.html", materials=materials, sections=sections,
                           material_fields=MATERIAL_FIELDS, section_fields=SECTION_FIELDS)


@simulation_bp.post("/libraries/<kind>/<int:item_id>")
@superadmin_required
def library_save(kind, item_id):
    if kind not in ("material", "section"): abort(404)
    cls, fields = (SimulationMaterial, MATERIAL_FIELDS) if kind == "material" else (SimulationSection, SECTION_FIELDS)
    name = text_field(request.form.get("name"), 80)
    values = {key: number(request.form.get(key), positive=True, maximum=1e13 if kind == "material" else 10) for key in fields}
    sample = dict(id=item_id, name=name, enabled=True, **values)
    validate_libraries([sample] if kind == "material" else [], [sample] if kind == "section" else [])
    begin_serialized_write()
    row = db.session.get(cls, item_id) if item_id else cls()
    if row is None: abort(404)
    row.name = name; row.enabled = request.form.get("enabled") == "1"
    for key, value in values.items(): setattr(row, key, value)
    db.session.add(row); db.session.flush()
    record_audit("simulation.library_save", summary="保存模拟材料或截面预设。", target_type="simulation_"+kind, target_id=row.id,
                 payload={"properties": values, "enabled": row.enabled})
    flash("预设已保存，引用该预设的旧结果需要重新计算。", "success")
    return redirect(url_for("simulation.library"))
