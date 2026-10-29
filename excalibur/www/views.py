import datetime as dt
import glob
import json
import logging
import os
import re
import sys

import pandas as pd
from flask import (
    Blueprint,
    Response,
    jsonify,
    request,
    url_for,
    redirect,
    render_template,
    send_from_directory,
)
from werkzeug.utils import secure_filename

from .. import configuration as conf
from ..invoices import (
    InvoiceParseError,
    _display_money,
    display_rows,
    display_verification,
    parse_invoice_bytes,
    to_batch_csv,
    to_csv,
)
from ..models import Job, File, Rule
from ..settings import Session
from ..executors import get_default_executor
from ..utils.file import mkdirs, allowed_filename
from ..utils.metadata import generate_uuid, random_string

views = Blueprint("views", __name__)


def _task_command(task_name, task_id):
    """Run the task with this interpreter so the local install is used."""
    return [
        sys.executable,
        "-m",
        "excalibur",
        "run",
        "--task",
        task_name,
        "--uuid",
        task_id,
    ]


@views.route("/", methods=["GET"])
def index():
    return redirect(url_for("views.files"))


@views.route("/files", methods=["GET", "POST"])
def files():
    if request.method == "GET":
        files_response = []
        session = Session()
        for file in session.query(File).order_by(File.uploaded_at.desc()).all():
            job = (
                session.query(Job)
                .filter(Job.file_id == file.file_id)
                .order_by(Job.started_at.desc())
                .first()
            )
            files_response.append(
                {
                    "file_id": file.file_id,
                    "job_id": job.job_id if job is not None else "",
                    "uploaded_at": file.uploaded_at.strftime("%Y-%m-%dT%H:%M:%S"),
                    "filename": file.filename,
                }
            )
        session.close()
        return render_template("files.html", files_response=files_response)
    file = request.files["file-0"]
    if file and allowed_filename(file.filename):
        file_id = generate_uuid()
        uploaded_at = dt.datetime.now()
        pages = request.form["pages"]
        filename = secure_filename(file.filename)
        filepath = os.path.join(conf.PDFS_FOLDER, file_id)
        mkdirs(filepath)
        filepath = os.path.join(filepath, filename)
        file.save(filepath)

        session = Session()
        f = File(
            file_id=file_id,
            uploaded_at=uploaded_at,
            pages=pages,
            filename=filename,
            filepath=filepath,
        )
        session.add(f)
        session.commit()
        session.close()

        executor = get_default_executor()
        executor.execute_async(_task_command("split", file_id))
    return jsonify(file_id=file_id)


@views.route("/workspaces/<string:file_id>", methods=["GET"])
def workspaces(file_id):
    session = Session()
    file = session.query(File).filter(File.file_id == file_id).first()
    rules = session.query(Rule).order_by(Rule.created_at.desc()).all()
    session.close()
    imagepaths, saved_rules = (None for i in range(2))
    filedims, imagedims, detected_areas = ("null" for i in range(3))
    if file.has_image:
        imagepaths = json.loads(file.imagepaths)
        for page in imagepaths:
            imagepaths[page] = imagepaths[page].replace(
                os.path.join(conf.PROJECT_ROOT, "www"), ""
            )
        filedims = file.filedims
        imagedims = file.imagedims
        detected_areas = file.detected_areas
        saved_rules = [
            {"rule_id": rule.rule_id, "rule_name": rule.rule_name} for rule in rules
        ]
    return render_template(
        "workspace.html",
        filename=file.filename,
        imagepaths=imagepaths,
        filedims=filedims,
        imagedims=imagedims,
        detected_areas=detected_areas,
        saved_rules=saved_rules,
    )


@views.route("/rules", methods=["GET", "POST"], defaults={"rule_id": None})
@views.route("/rules/<string:rule_id>", methods=["GET"])
def rules(rule_id):
    if request.method == "GET":
        if rule_id is not None:
            session = Session()
            rule = session.query(Rule).filter(Rule.rule_id == rule_id).first()
            session.close()
            message = "Rule not found"
            rule_options = {}
            if rule is not None:
                message = ""
                rule_options = json.loads(rule.rule_options)
            return jsonify(message=message, rule_options=rule_options)
        session = Session()
        rules = session.query(Rule).order_by(Rule.created_at.desc()).all()
        session.close()
        saved_rules = [
            {
                "rule_id": rule.rule_id,
                "created_at": rule.created_at.strftime("%Y-%m-%dT%H:%M:%S"),
                "rule_name": rule.rule_name,
                "rule_options": rule.rule_options,
            }
            for rule in rules
        ]
        return render_template("rules.html", saved_rules=saved_rules)
    message = "Rule invalid"
    file = request.files["file-0"]
    if file and allowed_filename(file.filename):
        rule_id = generate_uuid()
        created_at = dt.datetime.now()
        rule_name = os.path.splitext(secure_filename(file.filename))[0]
        rule_options = file.read()
        message = "Rule saved"

        session = Session()
        r = Rule(
            rule_id=rule_id,
            created_at=created_at,
            rule_name=rule_name,
            rule_options=rule_options,
        )
        session.add(r)
        session.commit()
        session.close()
    return jsonify(message=message)


@views.route("/jobs", methods=["GET", "POST"], defaults={"job_id": None})
@views.route("/jobs/<string:job_id>", methods=["GET"])
def jobs(job_id):
    if request.method == "GET":
        if job_id is not None:
            session = Session()
            job = session.query(Job).filter(Job.job_id == job_id).first()
            session.close()

            data = []
            render_files = json.loads(job.render_files)
            regex = r"page-(\d+)-table-(\d+)"
            for k in sorted(
                render_files,
                key=lambda x: (int(re.split(regex, x)[1]), int(re.split(regex, x)[2])),
            ):
                df = pd.read_json(render_files[k])
                columns = df.columns.values
                records = df.to_dict("records")
                data.append({"title": k, "columns": columns, "records": records})
            return render_template(
                "job.html",
                is_finished=job.is_finished,
                started_at=job.started_at,
                finished_at=job.finished_at,
                datapath=job.datapath,
                data=data,
            )
        jobs_response = []
        session = Session()
        for job in session.query(Job).order_by(Job.started_at.desc()).all():
            file = session.query(File).filter(File.file_id == job.file_id).first()
            jobs_response.append(
                {
                    "filename": file.filename,
                    "job_id": job.job_id,
                    "started_at": job.started_at.strftime("%Y-%m-%dT%H:%M:%S"),
                    "finished_at": job.finished_at.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            )
        session.close()
        return render_template("jobs.html", jobs_response=jobs_response)
    file_id = request.form["file_id"]
    rule_id = request.form["rule_id"]

    session = Session()
    file = session.query(File).filter(File.file_id == file_id).first()
    session.close()

    if not rule_id:
        rule_id = generate_uuid()
        created_at = dt.datetime.now()
        rule_name = "_".join([os.path.splitext(file.filename)[0], random_string(6)])
        rule_options = request.form["rule_options"]

        session = Session()
        r = Rule(
            rule_id=rule_id,
            created_at=created_at,
            rule_name=rule_name,
            rule_options=rule_options,
        )
        session.add(r)
        session.commit()
        session.close()

    job_id = generate_uuid()
    started_at = dt.datetime.now()

    session = Session()
    j = Job(job_id=job_id, started_at=started_at, file_id=file_id, rule_id=rule_id)
    session.add(j)
    session.commit()
    session.close()

    executor = get_default_executor()
    executor.execute_async(_task_command("extract", job_id))
    return jsonify(job_id=job_id)


@views.route("/download", methods=["POST"])
def download():
    job_id = request.form["job_id"]
    f = request.form["format"]

    session = Session()
    job = session.query(Job).filter(Job.job_id == job_id).first()
    session.close()

    datapath = os.path.join(job.datapath, f.lower())
    zipfile = glob.glob(os.path.join(datapath, "*.zip"))[0]

    directory = os.path.join(os.getcwd(), datapath)
    filename = os.path.basename(zipfile)
    return send_from_directory(
        directory=directory, path=filename, as_attachment=True
    )


_MAX_INVOICE_BYTES = 20 * 1024 * 1024


def _invoice_error(message, fmt, status):
    if fmt == "json":
        return jsonify(error=message), status
    if fmt == "csv":
        return Response(message + "\n", mimetype="text/plain", status=status)
    return (
        render_template(
            "invoice.html",
            rows=None,
            error=message,
            warnings=None,
            filename=None,
        ),
        status,
    )


@views.route("/invoice", methods=["GET", "POST"])
def invoice():
    if request.method == "GET":
        return render_template(
            "invoice.html",
            rows=None,
            error=None,
            warnings=None,
            filename=None,
        )

    fmt = (request.args.get("format") or request.form.get("format") or "html").lower()
    upload = request.files.get("file") or request.files.get("file-0")
    if upload is None or not upload.filename:
        return _invoice_error("Choose a PDF to extract.", fmt, 400)
    if not upload.filename.lower().endswith(".pdf"):
        return _invoice_error("Upload a PDF file.", fmt, 400)

    data = upload.read()
    if len(data) > _MAX_INVOICE_BYTES:
        return _invoice_error("PDF is larger than 20 MB.", fmt, 400)
    try:
        record = parse_invoice_bytes(data)
    except InvoiceParseError as exc:
        return _invoice_error(str(exc), fmt, 422)
    except Exception:
        logging.exception("Failed to extract invoice fields")
        return _invoice_error(
            "Could not read this PDF as a supplier invoice.", fmt, 500
        )

    if fmt == "json":
        return jsonify(record)
    if fmt == "csv":
        download = f"invoice-{record['invoice_number']}.csv"
        return Response(
            to_csv(record),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{download}"'},
        )
    if fmt != "html":
        return _invoice_error("Format must be html, json, or csv.", fmt, 400)

    return render_template(
        "invoice.html",
        rows=display_rows(record),
        error=None,
        warnings=record["warnings"],
        filename=upload.filename,
        invoice_json=json.dumps(record).replace("<", "\\u003c"),
        invoice_csv=to_csv(record),
        download_name=f"invoice-{record['invoice_number']}",
    )


def _bulk_error(message, fmt, status):
    if fmt == "csv":
        return Response(message + "\n", mimetype="text/plain", status=status)
    return (
        render_template(
            "bulk.html",
            error=message,
            failures=None,
            summaries=None,
        ),
        status,
    )


@views.route("/bulk", methods=["GET", "POST"])
def bulk():
    if request.method == "GET":
        return render_template(
            "bulk.html",
            error=None,
            failures=None,
            summaries=None,
        )

    fmt = (request.args.get("format") or request.form.get("format") or "html").lower()
    uploads = [
        upload
        for upload in request.files.getlist("files")
        if upload is not None and upload.filename
    ]
    if len(uploads) < 2:
        return _bulk_error("Choose at least two PDFs.", fmt, 400)

    entries = []
    failures = []
    for upload in uploads:
        filename = upload.filename
        if not filename.lower().endswith(".pdf"):
            failures.append({"filename": filename, "error": "Not a PDF."})
            continue
        data = upload.read()
        if len(data) > _MAX_INVOICE_BYTES:
            failures.append(
                {"filename": filename, "error": "PDF is larger than 20 MB."}
            )
            continue
        try:
            record = parse_invoice_bytes(data)
        except InvoiceParseError as exc:
            failures.append({"filename": filename, "error": str(exc)})
            continue
        except Exception:
            logging.exception("Failed to extract invoice fields from %s", filename)
            failures.append(
                {
                    "filename": filename,
                    "error": "Could not read this PDF as a supplier invoice.",
                }
            )
            continue
        entries.append({"filename": filename, "record": record})

    if not entries:
        message = "None of the PDFs could be read as invoices."
        if fmt == "csv":
            return Response(message + "\n", mimetype="text/plain", status=422)
        return (
            render_template(
                "bulk.html",
                error=message,
                failures=failures,
                summaries=None,
            ),
            422,
        )
    if fmt not in ("html", "csv"):
        return _bulk_error("Format must be html or csv.", fmt, 400)

    csv_text = to_batch_csv(entries)
    line_count = max(len(csv_text.splitlines()) - 1, 0)
    if fmt == "csv":
        return Response(
            csv_text,
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="invoices.csv"'},
        )

    summaries = [
        {
            "filename": entry["filename"],
            "invoice_number": entry["record"]["invoice_number"],
            "invoice_date": entry["record"]["invoice_date"],
            "total_order": _display_money(entry["record"]["total_order"]),
            "verification": display_verification(entry["record"]),
            "warnings": entry["record"]["warnings"],
        }
        for entry in entries
    ]
    return render_template(
        "bulk.html",
        error=None,
        failures=failures,
        summaries=summaries,
        bulk_csv=csv_text,
        line_count=line_count,
    )
