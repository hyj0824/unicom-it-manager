from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, time, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .config import (
    BASE_DIR,
    ensure_storage_paths,
    get_settings,
    validate_runtime_settings,
)
from .database import SessionLocal, check_schema_current, get_db
from .models import CallEvent, CallRecord, CallTask, CallbackPlan, Customer, Script, utcnow
from .scheduler import scheduler_service
from .services import plans as plan_service
from .services.customers import customer_phone_map, sync_default_contact
from .services.scripts import generate_script_audio
from .services.settings import (
    SCHEDULER_ENABLED_KEY,
    ensure_default_settings,
    is_scheduler_enabled,
    set_setting,
)


settings = get_settings()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_dt(value: datetime | None, timezone_name: str | None = None) -> str:
    value = _as_utc(value)
    if value is None:
        return "-"
    zone = plan_service.get_zone(timezone_name or settings.default_timezone)
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def datetime_local_filter(value: datetime | None, timezone_name: str | None = None) -> str:
    return plan_service.datetime_local_value(
        value, timezone_name or settings.default_timezone
    )


templates.env.filters["dt"] = format_dt
templates.env.filters["datetime_local"] = datetime_local_filter


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_settings(settings)
    ensure_storage_paths(settings)
    check_schema_current()
    with SessionLocal() as db:
        ensure_default_settings(db)
        db.commit()
    scheduler_service.start()
    yield
    scheduler_service.shutdown()


app = FastAPI(title="Callback Demo", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.cookie_secret, same_site="lax")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def redirect_to(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def context(request: Request, db: Session | None = None, **extra):
    data = {
        "request": request,
        "is_authenticated": auth.is_authenticated(request),
        "scheduler_enabled": True,
        "settings": settings,
    }
    if db is not None:
        ensure_default_settings(db)
        data["scheduler_enabled"] = is_scheduler_enabled(db)
    data.update(extra)
    return data


def render(
    request: Request,
    template_name: str,
    db: Session | None = None,
    status_code: int = 200,
    **extra,
):
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context=context(request, db, **extra),
        status_code=status_code,
    )


def get_or_404(db: Session, model, item_id: int):
    item = db.get(model, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    return item


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login")
def login_page(request: Request):
    if auth.is_authenticated(request):
        return redirect_to("/")
    return render(request, "login.html")


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    password = str(form.get("password", ""))
    if not auth.verify_admin_password(password):
        return render(
            request,
            "login.html",
            status_code=status.HTTP_401_UNAUTHORIZED,
            error="Invalid password.",
        )
    auth.login(request)
    return redirect_to("/")


@app.post("/logout")
def logout_submit(request: Request):
    auth.logout(request)
    return redirect_to("/login")


@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    zone = plan_service.get_zone(settings.default_timezone)
    today = datetime.now(zone).date()
    today_start = datetime.combine(today, time.min, tzinfo=zone).astimezone(timezone.utc)
    today_end = datetime.combine(today, time.max, tzinfo=zone).astimezone(timezone.utc)

    due_today = db.scalar(
        select(func.count(CallbackPlan.id)).where(
            CallbackPlan.enabled.is_(True),
            CallbackPlan.next_run_at >= today_start,
            CallbackPlan.next_run_at <= today_end,
        )
    ) or 0
    active_task = db.scalars(
        select(CallTask)
        .where(CallTask.status.in_(["queued", "dialing", "connected"]))
        .order_by(CallTask.due_at.asc(), CallTask.created_at.asc())
        .limit(1)
    ).first()
    stats = {
        name: db.scalar(select(func.count(CallRecord.id)).where(CallRecord.status == name)) or 0
        for name in ["completed", "failed", "short_call", "no_answer", "queued"]
    }
    counts = {
        "customers": db.scalar(select(func.count(Customer.id))) or 0,
        "scripts": db.scalar(select(func.count(Script.id))) or 0,
        "plans": db.scalar(select(func.count(CallbackPlan.id))) or 0,
        "records": db.scalar(select(func.count(CallRecord.id))) or 0,
    }
    recent_records = db.scalars(
        select(CallRecord).order_by(CallRecord.created_at.desc()).limit(8)
    ).all()
    return render(
        request,
        "dashboard.html",
        db,
        due_today=due_today,
        active_task=active_task,
        stats=stats,
        counts=counts,
        recent_records=recent_records,
    )


@app.post("/settings/scheduler")
async def scheduler_toggle(request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    form = await request.form()
    enabled = str(form.get("enabled", "0")) == "1"
    set_setting(db, SCHEDULER_ENABLED_KEY, "1" if enabled else "0")
    db.commit()
    return redirect_to(str(form.get("next", "/")))


def customers_template(
    request: Request,
    db: Session,
    error: str = "",
    form_data: dict[str, str] | None = None,
    edit_customer: Customer | None = None,
    status_code: int = 200,
):
    customers = db.scalars(select(Customer).order_by(Customer.created_at.desc())).all()
    return render(
        request,
        "customers.html",
        db,
        status_code=status_code,
        customers=customers,
        customer_phones=customer_phone_map(db, customers),
        edit_customer=edit_customer,
        form_data=form_data or {},
        error=error,
    )


@app.get("/customers")
def customers_page(
    request: Request,
    edit_id: int | None = None,
    db: Session = Depends(get_db),
):
    auth.require_admin(request)
    edit_customer = db.get(Customer, edit_id) if edit_id else None
    return customers_template(request, db, edit_customer=edit_customer)


@app.post("/customers")
async def customer_create(request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    phone = str(form.get("phone", "")).strip()
    notes = str(form.get("notes", "")).strip()
    form_data = {"name": name, "phone": phone, "notes": notes}
    if not name:
        return customers_template(request, db, "Name is required.", form_data, status_code=400)
    if not plan_service.validate_phone(phone):
        return customers_template(request, db, "Phone must match +?[0-9]{5,20}.", form_data, status_code=400)
    customer = Customer(name=name, notes=notes)
    db.add(customer)
    db.flush()
    sync_default_contact(db, customer, phone)
    db.commit()
    return redirect_to("/customers")


@app.post("/customers/{customer_id}/edit")
async def customer_update(customer_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    customer = get_or_404(db, Customer, customer_id)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    phone = str(form.get("phone", "")).strip()
    notes = str(form.get("notes", "")).strip()
    if not name:
        return customers_template(request, db, "Name is required.", edit_customer=customer, status_code=400)
    if not plan_service.validate_phone(phone):
        return customers_template(request, db, "Phone must match +?[0-9]{5,20}.", edit_customer=customer, status_code=400)
    customer.name = name
    customer.notes = notes
    customer.version += 1
    sync_default_contact(db, customer, phone)
    db.commit()
    return redirect_to("/customers")


@app.post("/customers/{customer_id}/delete")
def customer_delete(customer_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    customer = get_or_404(db, Customer, customer_id)
    try:
        db.delete(customer)
        db.commit()
    except IntegrityError:
        db.rollback()
        return customers_template(
            request,
            db,
            "This customer is referenced by plans, call records or business services and cannot be deleted.",
            status_code=400,
        )
    return redirect_to("/customers")


def scripts_template(
    request: Request,
    db: Session,
    error: str = "",
    form_data: dict[str, str] | None = None,
    edit_script: Script | None = None,
    status_code: int = 200,
):
    scripts = db.scalars(select(Script).order_by(Script.created_at.desc())).all()
    return render(
        request,
        "scripts.html",
        db,
        status_code=status_code,
        scripts=scripts,
        edit_script=edit_script,
        form_data=form_data or {},
        error=error,
    )


@app.get("/scripts")
def scripts_page(
    request: Request,
    edit_id: int | None = None,
    db: Session = Depends(get_db),
):
    auth.require_admin(request)
    edit_script = db.get(Script, edit_id) if edit_id else None
    return scripts_template(request, db, edit_script=edit_script)


@app.post("/scripts")
async def script_create(request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    form = await request.form()
    title = str(form.get("title", "")).strip()
    body = str(form.get("body", "")).strip()
    wav_path = str(form.get("wav_path", "")).strip()
    form_data = {"title": title, "body": body, "wav_path": wav_path}
    if not title or not body:
        return scripts_template(request, db, "Title and body are required.", form_data, status_code=400)
    db.add(Script(title=title, body=body, wav_path=wav_path))
    db.commit()
    return redirect_to("/scripts")


@app.post("/scripts/{script_id}/edit")
async def script_update(script_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    script = get_or_404(db, Script, script_id)
    form = await request.form()
    title = str(form.get("title", "")).strip()
    body = str(form.get("body", "")).strip()
    wav_path = str(form.get("wav_path", "")).strip()
    if not title or not body:
        return scripts_template(request, db, "Title and body are required.", edit_script=script, status_code=400)
    script.title = title
    script.body = body
    script.wav_path = wav_path
    script.tts_status = "generated" if wav_path else script.tts_status
    db.commit()
    return redirect_to("/scripts")


@app.post("/scripts/{script_id}/delete")
def script_delete(script_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    script = get_or_404(db, Script, script_id)
    try:
        db.delete(script)
        db.commit()
    except IntegrityError:
        db.rollback()
        return scripts_template(
            request,
            db,
            "This script is referenced by plans or call records and cannot be deleted.",
            status_code=400,
        )
    return redirect_to("/scripts")


@app.post("/scripts/{script_id}/generate-audio")
def script_generate_audio(script_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    script = get_or_404(db, Script, script_id)
    generate_script_audio(db, script, settings)
    db.commit()
    return redirect_to("/scripts")


def plans_template(
    request: Request,
    db: Session,
    error: str = "",
    form_data: dict[str, str] | None = None,
    edit_plan: CallbackPlan | None = None,
    status_code: int = 200,
):
    plans = db.scalars(select(CallbackPlan).order_by(CallbackPlan.created_at.desc())).all()
    customers = db.scalars(select(Customer).order_by(Customer.name.asc())).all()
    scripts = db.scalars(select(Script).order_by(Script.title.asc())).all()
    return render(
        request,
        "plans.html",
        db,
        status_code=status_code,
        plans=plans,
        customers=customers,
        scripts=scripts,
        customer_phones=customer_phone_map(db, customers),
        edit_plan=edit_plan,
        form_data=form_data or {},
        error=error,
    )


def _parse_plan_form(db: Session, form) -> tuple[Customer, Script, str, datetime | None, str, str, bool]:
    customer = get_or_404(db, Customer, int(form.get("customer_id", 0)))
    script = get_or_404(db, Script, int(form.get("script_id", 0)))
    trigger_type = str(form.get("trigger_type", "once")).strip()
    timezone_name = str(form.get("timezone", settings.default_timezone)).strip() or settings.default_timezone
    run_at = plan_service.parse_datetime_local(str(form.get("run_at", "")), timezone_name)
    cron_expr = str(form.get("cron_expr", "")).strip()
    enabled = str(form.get("enabled", "")) == "on"
    return customer, script, trigger_type, run_at, cron_expr, timezone_name, enabled


@app.get("/plans")
def plans_page(
    request: Request,
    edit_id: int | None = None,
    db: Session = Depends(get_db),
):
    auth.require_admin(request)
    edit_plan = db.get(CallbackPlan, edit_id) if edit_id else None
    return plans_template(request, db, edit_plan=edit_plan)


@app.post("/plans")
async def plan_create(request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    form = await request.form()
    try:
        customer, script, trigger_type, run_at, cron_expr, timezone_name, enabled = _parse_plan_form(db, form)
        plan_service.create_plan(db, customer, script, trigger_type, run_at, cron_expr, timezone_name, enabled)
    except (ValueError, HTTPException) as exc:
        if isinstance(exc, HTTPException):
            raise
        return plans_template(request, db, str(exc), dict(form), status_code=400)
    db.commit()
    return redirect_to("/plans")


@app.post("/plans/{plan_id}/edit")
async def plan_update(plan_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    plan = get_or_404(db, CallbackPlan, plan_id)
    form = await request.form()
    try:
        customer, script, trigger_type, run_at, cron_expr, timezone_name, enabled = _parse_plan_form(db, form)
        plan_service.update_plan(plan, customer, script, trigger_type, run_at, cron_expr, timezone_name, enabled)
    except ValueError as exc:
        return plans_template(request, db, str(exc), dict(form), edit_plan=plan, status_code=400)
    db.commit()
    return redirect_to("/plans")


@app.post("/plans/{plan_id}/delete")
def plan_delete(plan_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    plan = get_or_404(db, CallbackPlan, plan_id)
    try:
        db.delete(plan)
        db.commit()
    except IntegrityError:
        db.rollback()
        return plans_template(
            request,
            db,
            "This plan is referenced by queued tasks or call records and cannot be deleted.",
            status_code=400,
        )
    return redirect_to("/plans")


@app.post("/plans/{plan_id}/toggle")
def plan_toggle(plan_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    plan = get_or_404(db, CallbackPlan, plan_id)
    plan.enabled = not plan.enabled
    db.commit()
    return redirect_to("/plans")


@app.post("/plans/{plan_id}/call-now")
def plan_call_now(plan_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    plan = get_or_404(db, CallbackPlan, plan_id)
    plan_service.create_call_task(
        db,
        plan,
        due_at=utcnow(),
        status="queued",
        message="Manual call requested from the web UI.",
        source="manual",
    )
    db.commit()
    return redirect_to("/calls")


@app.get("/calls")
def calls_page(request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    records = db.scalars(select(CallRecord).order_by(CallRecord.created_at.desc())).all()
    tasks = db.scalars(select(CallTask).order_by(CallTask.due_at.asc()).limit(20)).all()
    return render(request, "calls.html", db, records=records, tasks=tasks)


@app.get("/calls/{record_id}")
def call_detail(record_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    record = get_or_404(db, CallRecord, record_id)
    events = db.scalars(
        select(CallEvent)
        .where(CallEvent.call_record_id == record.id)
        .order_by(CallEvent.created_at.asc())
    ).all()
    return render(request, "call_detail.html", db, record=record, events=events)


@app.post("/calls/{record_id}/feedback")
async def call_feedback(record_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_admin(request)
    record = get_or_404(db, CallRecord, record_id)
    form = await request.form()
    record.operator_feedback = str(form.get("operator_feedback", "")).strip()
    record.follow_up_required = str(form.get("follow_up_required", "")) == "on"
    db.commit()
    return redirect_to(f"/calls/{record.id}")
