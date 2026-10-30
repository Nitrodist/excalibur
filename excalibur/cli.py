import os
from threading import Timer

import click

from . import __version__, settings
from . import configuration as conf
from .invoices import (
    InvoiceParseError,
    parse_invoice,
    rename_invoice_paths,
    to_csv,
    to_json,
)
from .operators.python_operator import PythonOperator
from .tasks import extract, split
from .utils.database import initialize_database, reset_database
from .www.app import create_app


def abort_if_false(ctx, param, value):
    if not value:
        ctx.abort()


def _run(task_name, task_id):
    task_bag = {"split": split, "extract": extract}
    python_callable = task_bag[task_name]
    task = PythonOperator(python_callable, op_args=[task_id])
    task.execute()


@click.group()
@click.version_option(version=__version__)
def cli(*args, **kwargs):
    pass


@cli.command("initdb")
def initdb(*args, **kwargs):
    initialize_database()


@cli.command("resetdb")
def resetdb(*args, **kwargs):
    click.confirm("This will drop existing tables if they exist. Proceed?", abort=True)

    reset_database()
    initialize_database()


@cli.command("webserver")
def webserver(*args, **kwargs):
    if conf.USING_SQLITE:
        sqlite_path = settings.SQL_ALCHEMY_CONN.replace("sqlite:///", "")
        if not os.path.isfile(sqlite_path):
            initialize_database()

    port = int(
        os.environ.get("EXCALIBUR_WEB_PORT", conf.get("webserver", "web_server_port"))
    )
    host = os.environ.get(
        "EXCALIBUR_WEB_HOST", conf.get("webserver", "web_server_host")
    )

    # https://stackoverflow.com/a/54235461/2780127
    def open_browser():
        click.launch(f"http://127.0.0.1:{port}")

    if host in ("127.0.0.1", "localhost", "::1"):
        Timer(1, open_browser).start()

    app = create_app(conf)
    app.run(port=port, host=host, use_reloader=False)


@cli.command("worker")
def worker(*args, **kwargs):
    from celery.bin import worker

    from .executors.celery_executor import app as celery_app

    worker = worker.worker(app=celery_app)
    options = {
        "concurrency": int(conf.get("celery", "WORKER_CONCURRENCY")),
        "loglevel": conf.get("core", "LOGGING_LEVEL"),
    }
    worker.run(**options)


@cli.command("run")
@click.option("-t", "--task")
@click.option("-id", "--uuid")
def run(*args, **kwargs):
    _run(kwargs["task"], kwargs["uuid"])


@cli.command("invoice")
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "csv"]),
    default="json",
    show_default=True,
    help="json for programs, csv for a spreadsheet.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False),
    help="Write the result to this file instead of printing it.",
)
def invoice(pdf, fmt, output):
    """Read invoice number, invoice date, due date, G/L nets, line tax, and total.

    Account amounts are the Net column on the G/L summary. Each line's tax
    is the Tax column on that row. CSV is a QuickBooks Online bill import,
    one row per G/L account that is on the invoice.
    """
    try:
        record = parse_invoice(pdf)
    except InvoiceParseError as exc:
        raise click.ClickException(str(exc)) from exc

    payload = to_json(record) if fmt == "json" else to_csv(record)
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(payload)
        click.echo(f"Wrote {output}")
        return
    click.echo(payload, nl=False)


@cli.command("rename-invoices")
@click.argument(
    "pdfs",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    help="Print the new names without renaming the files.",
)
def rename_invoices(pdfs, dry_run):
    """Rename invoice PDFs to '{store} {bill number} {Mon-DD-YYYY}.pdf'."""
    renamed, failures = rename_invoice_paths(pdfs, dry_run=dry_run)
    for source, destination in renamed:
        if source.resolve() == destination.resolve():
            click.echo(f"{source.name} already has the target name")
        else:
            verb = "Would rename" if dry_run else "Renamed"
            click.echo(f"{verb} {source.name} -> {destination.name}")
    for source, error in failures:
        click.echo(f"{source.name}: {error}", err=True)
    if failures:
        raise click.ClickException(
            f"{len(failures)} of {len(pdfs)} PDF(s) could not be renamed."
        )
