from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
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
from .models import (
    CallEvent,
    CallRecord,
    CallTask,
    CallbackPlan,
    ChangeSet,
    Contact,
    Customer,
    CustomerContact,
    BusinessService,
    ImportBatch,
    NetworkDevice,
    Role,
    RolePermission,
    Script,
    StagingRow,
    User,
    UserRole,
    utcnow,
)
from .scheduler import scheduler_service
from .services import imports as import_service
from .services import ledger as ledger_service
from .services import plans as plan_service
from .services.customers import customer_phone_map
from .services.dictionaries import active_items, categories_with_items
from .services.scripts import generate_script_audio
from .services.settings import (
    SCHEDULER_ENABLED_KEY,
    ensure_default_settings,
    is_scheduler_enabled,
    set_setting,
)
from .services.users import hash_password, role_names, set_user_roles


settings = get_settings()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

APP_TITLE = "政企专租线台账"


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


def format_date(value: datetime | None, timezone_name: str | None = None) -> str:
    value = _as_utc(value)
    if value is None:
        return ""
    zone = plan_service.get_zone(timezone_name or settings.default_timezone)
    return value.astimezone(zone).strftime("%Y-%m-%d")


def datetime_local_filter(value: datetime | None, timezone_name: str | None = None) -> str:
    return plan_service.datetime_local_value(
        value, timezone_name or settings.default_timezone
    )


def from_json_map(value: str, key: str) -> str:
    try:
        data = json.loads(value or "{}")
    except ValueError:
        return "-"
    result = data.get(key, "")
    return str(result) if result else "-"


templates.env.filters["dt"] = format_dt
templates.env.filters["date"] = format_date
templates.env.filters["datetime_local"] = datetime_local_filter
templates.env.filters["from_json_map"] = from_json_map


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_settings(settings)
    ensure_storage_paths(settings)
    check_schema_current()
    (BASE_DIR / "data" / "imports").mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        ensure_default_settings(db)
        db.commit()
    scheduler_service.start()
    yield
    scheduler_service.shutdown()


app = FastAPI(title=APP_TITLE, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.cookie_secret, same_site="lax")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def redirect_to(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def context(request: Request, db: Session | None = None, **extra):
    data = {
        "request": request,
        "is_authenticated": auth.is_authenticated(request),
        "current_user": auth.current_user(request),
        "scheduler_enabled": True,
        "settings": settings,
        "app_title": APP_TITLE,
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


def _form_int(form, key: str) -> int | None:
    value = str(form.get(key, "")).strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _form_float(form, key: str) -> float | None:
    value = str(form.get(key, "")).strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{key} 必须是数字。") from None


def _user_id(request: Request) -> int | None:
    principal = auth.current_user(request)
    if principal and principal.get("type") == "user":
        return principal.get("id")
    return None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- 登录与审计


@app.get("/login")
def login_page(request: Request):
    if auth.is_authenticated(request):
        return redirect_to("/")
    return render(request, "login.html")


@app.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    principal = auth.verify_credentials(db, username, password)
    if principal is None:
        ledger_service.log_action(
            db, "login_failed", detail=json.dumps({"username": username}, ensure_ascii=False),
            ip_address=_client_ip(request),
        )
        db.commit()
        return render(
            request,
            "login.html",
            status_code=status.HTTP_401_UNAUTHORIZED,
            error="用户名或密码不正确。",
        )
    auth.login(request, principal)
    ledger_service.log_action(
        db,
        "login",
        _user_id(request),
        detail=json.dumps({"username": username}, ensure_ascii=False),
        ip_address=_client_ip(request),
    )
    db.commit()
    return redirect_to("/")


@app.post("/logout")
def logout_submit(request: Request):
    auth.logout(request)
    return redirect_to("/login")


# ---------------------------------------------------------------- 工作台


@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    zone = plan_service.get_zone(settings.default_timezone)
    today = datetime.now(zone).date()
    today_start = datetime.combine(today, time.min, tzinfo=zone).astimezone(timezone.utc)
    today_end = datetime.combine(today, time.max, tzinfo=zone).astimezone(timezone.utc)
    now = utcnow()
    expiring_soon = now + timedelta(days=30)

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
    pending_reviews = db.scalar(
        select(func.count(ChangeSet.id)).where(ChangeSet.status == "submitted")
    ) or 0
    missing_total = sum(row["count"] for row in ledger_service.business_missing_fields(db))  # type: ignore[arg-type]
    missing_total += sum(row["count"] for row in ledger_service.device_missing_fields(db))  # type: ignore[arg-type]
    expiring_services = db.scalar(
        select(func.count(BusinessService.id)).where(
            BusinessService.is_active.is_(True),
            BusinessService.agreement_expires_at.is_not(None),
            BusinessService.agreement_expires_at <= expiring_soon,
        )
    ) or 0

    call_stats = {
        name: db.scalar(select(func.count(CallRecord.id)).where(CallRecord.status == name)) or 0
        for name in ["completed", "failed", "short_call", "no_answer"]
    }
    counts = {
        "services": db.scalar(
            select(func.count(BusinessService.id)).where(BusinessService.is_active.is_(True))
        ) or 0,
        "devices": db.scalar(
            select(func.count(NetworkDevice.id)).where(NetworkDevice.is_active.is_(True))
        ) or 0,
        "customers": db.scalar(select(func.count(Customer.id))) or 0,
        "plans": db.scalar(select(func.count(CallbackPlan.id))) or 0,
        "records": db.scalar(select(func.count(CallRecord.id))) or 0,
    }
    recent_records = db.scalars(
        select(CallRecord).order_by(CallRecord.created_at.desc()).limit(8)
    ).all()
    recent_batches = db.scalars(
        select(ImportBatch)
        .where(ImportBatch.error_count > 0)
        .order_by(ImportBatch.created_at.desc())
        .limit(5)
    ).all()
    return render(
        request,
        "dashboard.html",
        db,
        due_today=due_today,
        active_task=active_task,
        pending_reviews=pending_reviews,
        missing_total=missing_total,
        expiring_services=expiring_services,
        call_stats=call_stats,
        counts=counts,
        recent_records=recent_records,
        recent_batches=recent_batches,
    )


@app.post("/settings/scheduler")
async def scheduler_toggle(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    form = await request.form()
    enabled = str(form.get("enabled", "0")) == "1"
    set_setting(db, SCHEDULER_ENABLED_KEY, "1" if enabled else "0")
    db.commit()
    return redirect_to(str(form.get("next", "/")))


# ---------------------------------------------------------------- 客户与联系人


def customers_template(
    request: Request,
    db: Session,
    error: str = "",
    contact_error: str = "",
    form_data: dict[str, str] | None = None,
    contact_form: dict[str, str] | None = None,
    edit_customer: Customer | None = None,
    edit_contact_id: int | None = None,
    status_code: int = 200,
):
    customers = db.scalars(select(Customer).order_by(Customer.created_at.desc())).all()
    contacts: list[CustomerContact] = []
    if edit_customer is not None:
        contacts = db.scalars(
            select(CustomerContact)
            .where(CustomerContact.customer_id == edit_customer.id)
            .order_by(CustomerContact.id.asc())
        ).all()
    return render(
        request,
        "customers.html",
        db,
        status_code=status_code,
        customers=customers,
        contacts=contacts,
        customer_phones=customer_phone_map(db, customers),
        duties=active_items(db, "contact_duty"),
        edit_customer=edit_customer,
        edit_contact_id=edit_contact_id,
        form_data=form_data or {},
        contact_form=contact_form or {},
        error=error,
        contact_error=contact_error,
    )


@app.get("/customers")
def customers_page(
    request: Request,
    edit_id: int | None = None,
    edit_contact_id: int | None = None,
    db: Session = Depends(get_db),
):
    auth.require_login(request)
    edit_customer = db.get(Customer, edit_id) if edit_id else None
    return customers_template(
        request, db, edit_customer=edit_customer, edit_contact_id=edit_contact_id
    )


@app.post("/customers")
async def customer_create(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    notes = str(form.get("notes", "")).strip()
    form_data = {"name": name, "notes": notes}
    if not name:
        return customers_template(request, db, "客户名称不能为空。", form_data, status_code=400)
    db.add(Customer(name=name, notes=notes))
    db.commit()
    return redirect_to("/customers")


@app.post("/customers/{customer_id}/edit")
async def customer_update(customer_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    customer = get_or_404(db, Customer, customer_id)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    notes = str(form.get("notes", "")).strip()
    if not name:
        return customers_template(
            request, db, "客户名称不能为空。", edit_customer=customer, status_code=400
        )
    customer.name = name
    customer.notes = notes
    customer.version += 1
    db.commit()
    return redirect_to(f"/customers?edit_id={customer.id}")


@app.post("/customers/{customer_id}/delete")
def customer_delete(customer_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    customer = get_or_404(db, Customer, customer_id)
    try:
        db.delete(customer)
        db.commit()
    except IntegrityError:
        db.rollback()
        return customers_template(
            request,
            db,
            "该客户被业务、计划或通话记录引用，不能删除；可考虑停用。",
            status_code=400,
        )
    return redirect_to("/customers")


@app.post("/customers/{customer_id}/contacts")
async def contact_create(customer_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    customer = get_or_404(db, Customer, customer_id)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    phone = str(form.get("phone", "")).strip()
    duty = str(form.get("duty", "")).strip()
    contact_form = {"name": name, "phone": phone, "duty": duty}
    if phone and not plan_service.validate_phone(phone):
        return customers_template(
            request,
            db,
            contact_error="电话格式不正确（应为 +?[0-9]{5,20}）。",
            contact_form=contact_form,
            edit_customer=customer,
            status_code=400,
        )
    contact = Contact(name=name or None, phone=phone or None)
    db.add(contact)
    db.flush()
    db.add(CustomerContact(customer=customer, contact=contact, duty=duty))
    db.commit()
    return redirect_to(f"/customers?edit_id={customer.id}")


@app.post("/contacts/{contact_id}/edit")
async def contact_update(contact_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    contact = get_or_404(db, Contact, contact_id)
    form = await request.form()
    customer_id = _form_int(form, "customer_id")
    name = str(form.get("name", "")).strip()
    phone = str(form.get("phone", "")).strip()
    duty = str(form.get("duty", "")).strip()
    if phone and not plan_service.validate_phone(phone):
        return customers_template(
            request,
            db,
            contact_error="电话格式不正确（应为 +?[0-9]{5,20}）。",
            contact_form={"name": name, "phone": phone, "duty": duty},
            edit_customer=db.get(Customer, customer_id) if customer_id else None,
            edit_contact_id=contact.id,
            status_code=400,
        )
    contact.name = name or None
    contact.phone = phone or None
    contact.version += 1
    if customer_id is not None:
        link = db.scalars(
            select(CustomerContact).where(
                CustomerContact.customer_id == customer_id,
                CustomerContact.contact_id == contact.id,
            )
        ).first()
        if link is not None:
            link.duty = duty
    db.commit()
    return redirect_to(f"/customers?edit_id={customer_id or ''}")


@app.post("/contacts/{contact_id}/delete")
def contact_delete(contact_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    contact = get_or_404(db, Contact, contact_id)
    form = None
    customer_id = request.query_params.get("customer_id")
    try:
        db.delete(contact)
        db.commit()
    except IntegrityError:
        db.rollback()
        customer = db.get(Customer, int(customer_id)) if customer_id else None
        return customers_template(
            request,
            db,
            contact_error="该联系人被通话任务引用，不能删除。",
            edit_customer=customer,
            status_code=400,
        )
    return redirect_to(f"/customers?edit_id={customer_id or ''}")


# ---------------------------------------------------------------- 业务台账


def ledger_template(
    request: Request,
    db: Session,
    error: str = "",
    form_data: dict[str, str] | None = None,
    edit_service: BusinessService | None = None,
    status_code: int = 200,
    q: str = "",
    county_id: int | None = None,
    grid_id: int | None = None,
    status_id: int | None = None,
):
    query = select(BusinessService).where(BusinessService.is_active.is_(True))
    if q:
        like = f"%{q.strip()}%"
        query = query.join(Customer, BusinessService.customer_id == Customer.id).where(
            or_(BusinessService.service_number.ilike(like), Customer.name.ilike(like))
        )
    if county_id:
        query = query.where(BusinessService.county_item_id == county_id)
    if grid_id:
        query = query.where(BusinessService.grid_item_id == grid_id)
    if status_id:
        query = query.where(BusinessService.service_status_item_id == status_id)
    services = db.scalars(query.order_by(BusinessService.created_at.desc()).limit(500)).all()
    customers = db.scalars(select(Customer).order_by(Customer.name.asc())).all()
    return render(
        request,
        "ledger.html",
        db,
        status_code=status_code,
        services=services,
        customers=customers,
        counties=active_items(db, "county"),
        grids=active_items(db, "grid"),
        service_statuses=active_items(db, "service_status"),
        business_types=active_items(db, "business_type"),
        edit_service=edit_service,
        form_data=form_data or {},
        q=q,
        county_id=county_id or 0,
        grid_id=grid_id or 0,
        status_id=status_id or 0,
        error=error,
    )


@app.get("/ledger")
def ledger_page(
    request: Request,
    edit_id: int | None = None,
    q: str = "",
    county_id: int | None = None,
    grid_id: int | None = None,
    status_id: int | None = None,
    db: Session = Depends(get_db),
):
    auth.require_login(request)
    edit_service = db.get(BusinessService, edit_id) if edit_id else None
    return ledger_template(
        request, db, edit_service=edit_service, q=q,
        county_id=county_id, grid_id=grid_id, status_id=status_id,
    )


def _parse_service_form(form) -> dict:
    return {
        "service_number": str(form.get("service_number", "")).strip(),
        "customer_id": _form_int(form, "customer_id"),
        "county_item_id": _form_int(form, "county_item_id"),
        "grid_item_id": _form_int(form, "grid_item_id"),
        "service_status_item_id": _form_int(form, "service_status_item_id"),
        "business_type_item_id": _form_int(form, "business_type_item_id"),
        "channel_name": str(form.get("channel_name", "")).strip(),
        "accessed_at": ledger_service.parse_local_date(
            str(form.get("accessed_at", "")), settings.default_timezone
        ),
        "agreement_expires_at": ledger_service.parse_local_date(
            str(form.get("agreement_expires_at", "")), settings.default_timezone
        ),
    }


def _apply_service_form(service: BusinessService, data: dict, db: Session) -> str | None:
    error = ledger_service.validate_service_number(
        db, data["service_number"], exclude_id=service.id
    )
    if error:
        return error
    if data["customer_id"] is None:
        return "必须选择客户。"
    service.service_number = data["service_number"]
    service.customer_id = data["customer_id"]
    service.county_item_id = data["county_item_id"]
    service.grid_item_id = data["grid_item_id"]
    service.service_status_item_id = data["service_status_item_id"]
    service.business_type_item_id = data["business_type_item_id"]
    service.channel_name = data["channel_name"]
    service.accessed_at = data["accessed_at"]
    service.agreement_expires_at = data["agreement_expires_at"]
    service.version = (service.version or 0) + 1
    return None


@app.post("/ledger")
async def service_create(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    form = await request.form()
    data = _parse_service_form(form)
    service = BusinessService(
        service_number="", customer_id=data["customer_id"] or 0
    )
    error = _apply_service_form(service, data, db)
    if error:
        return ledger_template(request, db, error, dict(form), status_code=400)
    db.add(service)
    db.commit()
    ledger_service.log_action(
        db, "change", _user_id(request), "business_service", service.id,
        json.dumps({"operation": "create", "service_number": service.service_number}, ensure_ascii=False),
        _client_ip(request),
    )
    db.commit()
    return redirect_to("/ledger")


@app.post("/ledger/{service_id}/edit")
async def service_update(service_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    service = get_or_404(db, BusinessService, service_id)
    form = await request.form()
    data = _parse_service_form(form)
    error = _apply_service_form(service, data, db)
    if error:
        return ledger_template(request, db, error, dict(form), edit_service=service, status_code=400)
    db.commit()
    ledger_service.log_action(
        db, "change", _user_id(request), "business_service", service.id,
        json.dumps({"operation": "update", "service_number": service.service_number}, ensure_ascii=False),
        _client_ip(request),
    )
    db.commit()
    return redirect_to("/ledger")


@app.post("/ledger/{service_id}/delete")
def service_delete(service_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    service = get_or_404(db, BusinessService, service_id)
    if service.devices:
        return ledger_template(
            request,
            db,
            f"该业务下有 {len(service.devices)} 台设备，请先处理设备关联，不能删除。",
            status_code=400,
        )
    service.is_active = False
    db.commit()
    return redirect_to("/ledger")


# ---------------------------------------------------------------- 网络设备


def devices_template(
    request: Request,
    db: Session,
    error: str = "",
    form_data: dict[str, str] | None = None,
    edit_device: NetworkDevice | None = None,
    status_code: int = 200,
    q: str = "",
    recovery_status_id: int | None = None,
):
    query = select(NetworkDevice).where(NetworkDevice.is_active.is_(True))
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(NetworkDevice.device_code.ilike(like), NetworkDevice.vendor_model.ilike(like))
        )
    if recovery_status_id:
        query = query.where(NetworkDevice.recovery_status_item_id == recovery_status_id)
    devices = db.scalars(query.order_by(NetworkDevice.created_at.desc()).limit(500)).all()
    services = db.scalars(
        select(BusinessService)
        .where(BusinessService.is_active.is_(True))
        .order_by(BusinessService.service_number.asc())
    ).all()
    return render(
        request,
        "devices.html",
        db,
        status_code=status_code,
        devices=devices,
        services=services,
        asset_classes=active_items(db, "asset_class"),
        device_types=active_items(db, "device_type"),
        recovery_statuses=active_items(db, "recovery_status"),
        recovery_reasons=active_items(db, "recovery_reason"),
        maintenance_contacts=ledger_service.contact_options(db),
        edit_device=edit_device,
        form_data=form_data or {},
        q=q,
        recovery_status_id=recovery_status_id or 0,
        error=error,
    )


@app.get("/devices")
def devices_page(
    request: Request,
    edit_id: int | None = None,
    q: str = "",
    recovery_status_id: int | None = None,
    db: Session = Depends(get_db),
):
    auth.require_login(request)
    edit_device = db.get(NetworkDevice, edit_id) if edit_id else None
    return devices_template(
        request, db, edit_device=edit_device, q=q, recovery_status_id=recovery_status_id
    )


def _parse_device_form(form) -> dict:
    return {
        "device_code": str(form.get("device_code", "")).strip(),
        "business_service_id": _form_int(form, "business_service_id"),
        "asset_class_item_id": _form_int(form, "asset_class_item_id"),
        "device_type_item_id": _form_int(form, "device_type_item_id"),
        "recovery_status_item_id": _form_int(form, "recovery_status_item_id"),
        "recovery_reason_item_id": _form_int(form, "recovery_reason_item_id"),
        "maintenance_contact_id": _form_int(form, "maintenance_contact_id"),
        "vendor_model": str(form.get("vendor_model", "")).strip(),
        "location": str(form.get("location", "")).strip(),
        "asset_value": _form_float(form, "asset_value"),
    }


def _apply_device_form(device: NetworkDevice, data: dict, db: Session) -> str | None:
    error = ledger_service.validate_device_code(db, data["device_code"], exclude_id=device.id)
    if error:
        return error
    if data["business_service_id"] is None:
        return "必须选择所属业务。"
    device.device_code = data["device_code"]
    device.business_service_id = data["business_service_id"]
    device.asset_class_item_id = data["asset_class_item_id"]
    device.device_type_item_id = data["device_type_item_id"]
    device.recovery_status_item_id = data["recovery_status_item_id"]
    device.recovery_reason_item_id = data["recovery_reason_item_id"]
    device.maintenance_contact_id = data["maintenance_contact_id"]
    device.vendor_model = data["vendor_model"]
    device.location = data["location"]
    device.asset_value = data["asset_value"]
    device.version = (device.version or 0) + 1
    return None


@app.post("/devices")
async def device_create(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    form = await request.form()
    try:
        data = _parse_device_form(form)
    except ValueError as exc:
        return devices_template(request, db, str(exc), dict(form), status_code=400)
    device = NetworkDevice(device_code="", business_service_id=data["business_service_id"] or 0)
    error = _apply_device_form(device, data, db)
    if error:
        return devices_template(request, db, error, dict(form), status_code=400)
    db.add(device)
    db.commit()
    return redirect_to("/devices")


@app.post("/devices/{device_id}/edit")
async def device_update(device_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    device = get_or_404(db, NetworkDevice, device_id)
    form = await request.form()
    try:
        data = _parse_device_form(form)
    except ValueError as exc:
        return devices_template(request, db, str(exc), dict(form), edit_device=device, status_code=400)
    error = _apply_device_form(device, data, db)
    if error:
        return devices_template(request, db, error, dict(form), edit_device=device, status_code=400)
    db.commit()
    return redirect_to("/devices")


@app.post("/devices/{device_id}/delete")
def device_delete(device_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    device = get_or_404(db, NetworkDevice, device_id)
    device.is_active = False
    db.commit()
    return redirect_to("/devices")


# ---------------------------------------------------------------- 审核中心


@app.get("/reviews")
def reviews_page(request: Request, status_filter: str = "", db: Session = Depends(get_db)):
    auth.require_login(request)
    query = select(ChangeSet).order_by(ChangeSet.created_at.desc()).limit(200)
    if status_filter:
        query = query.where(ChangeSet.status == status_filter)
    change_sets = db.scalars(query).all()
    status_counts = {
        name: db.scalar(select(func.count(ChangeSet.id)).where(ChangeSet.status == name)) or 0
        for name in ["draft", "submitted", "returned", "approved", "rejected", "applied", "cancelled"]
    }
    return render(
        request,
        "reviews.html",
        db,
        change_sets=change_sets,
        status_filter=status_filter,
        status_counts=status_counts,
    )


# ---------------------------------------------------------------- 缺项工作台


@app.get("/missing")
def missing_page(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    business_rows = ledger_service.business_missing_fields(db)
    device_rows = ledger_service.device_missing_fields(db)
    recent_batches = db.scalars(
        select(ImportBatch)
        .where(ImportBatch.missing_count > 0)
        .order_by(ImportBatch.created_at.desc())
        .limit(10)
    ).all()
    return render(
        request,
        "missing.html",
        db,
        business_rows=business_rows,
        device_rows=device_rows,
        recent_batches=recent_batches,
    )


# ---------------------------------------------------------------- 导入导出


def imports_template(
    request: Request,
    db: Session,
    error: str = "",
    status_code: int = 200,
):
    batches = db.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(100)).all()
    return render(
        request,
        "imports.html",
        db,
        status_code=status_code,
        batches=batches,
        error=error,
    )


@app.get("/imports")
def imports_page(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    return imports_template(request, db)


@app.post("/imports/upload")
async def import_upload(request: Request, file: UploadFile, db: Session = Depends(get_db)):
    auth.require_login(request)
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        return imports_template(request, db, "只支持 .xlsx 文件。", status_code=400)
    content = await file.read()
    if not content:
        return imports_template(request, db, "文件为空。", status_code=400)

    batch = ImportBatch(
        file_name=Path(filename).name,
        file_hash=hashlib.sha256(content).hexdigest(),
        source_type="business_ledger",
        status="validating",
        created_by_user_id=_user_id(request),
    )
    db.add(batch)
    db.flush()

    import_dir = BASE_DIR / "data" / "imports"
    stored = import_dir / f"{batch.id:05d}_{uuid.uuid4().hex[:8]}_{Path(filename).name}"
    stored.write_bytes(content)
    try:
        import_service.import_ledger_workbook(db, batch, stored)
    except Exception as exc:  # noqa: BLE001 - 解析失败要给用户可见错误
        db.rollback()
        return imports_template(request, db, f"导入解析失败：{exc}", status_code=400)
    ledger_service.log_action(
        db, "import", _user_id(request), "import_batch", batch.id,
        json.dumps({"file": batch.file_name, "rows": batch.total_rows}, ensure_ascii=False),
        _client_ip(request),
    )
    db.commit()
    return redirect_to(f"/imports/{batch.id}")


@app.get("/imports/{batch_id}")
def import_detail(
    batch_id: int,
    request: Request,
    status_filter: str = "",
    db: Session = Depends(get_db),
):
    auth.require_login(request)
    batch = get_or_404(db, ImportBatch, batch_id)
    query = select(StagingRow).where(StagingRow.batch_id == batch.id).order_by(
        StagingRow.row_number.asc()
    )
    if status_filter:
        query = query.where(StagingRow.status == status_filter)
    rows = db.scalars(query.limit(500)).all()
    status_counts = {
        name: db.scalar(
            select(func.count(StagingRow.id)).where(
                StagingRow.batch_id == batch.id, StagingRow.status == name
            )
        ) or 0
        for name in ["valid", "missing", "error", "duplicate"]
    }
    return render(
        request,
        "import_detail.html",
        db,
        batch=batch,
        rows=rows,
        status_filter=status_filter,
        status_counts=status_counts,
    )


# ---------------------------------------------------------------- 系统管理


@app.get("/admin/users")
def admin_users_page(request: Request, edit_id: int | None = None, db: Session = Depends(get_db)):
    auth.require_login(request)
    users = db.scalars(select(User).order_by(User.created_at.asc())).all()
    roles = db.scalars(select(Role).order_by(Role.id.asc())).all()
    user_role_map = {
        user.id: db.scalars(
            select(UserRole.role_id).where(UserRole.user_id == user.id)
        ).all()
        for user in users
    }
    return render(
        request,
        "admin_users.html",
        db,
        users=users,
        roles=roles,
        user_role_map=user_role_map,
        role_name_map={user.id: role_names(db, user) for user in users},
        edit_user=db.get(User, edit_id) if edit_id else None,
    )


@app.post("/admin/users")
async def admin_user_create(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    form = await request.form()
    username = str(form.get("username", "")).strip()
    display_name = str(form.get("display_name", "")).strip()
    password = str(form.get("password", ""))
    role_ids = [int(v) for v in form.getlist("role_ids")]
    enabled = str(form.get("enabled", "")) == "on"
    if not username or not password:
        return render(request, "admin_users.html", db, error="用户名和初始密码必填。", status_code=400,
                      users=db.scalars(select(User).order_by(User.created_at.asc())).all(),
                      roles=db.scalars(select(Role).order_by(Role.id.asc())).all(),
                      user_role_map={}, role_name_map={}, edit_user=None)
    user = User(
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        is_enabled=enabled,
        is_superadmin=False,
    )
    db.add(user)
    try:
        db.flush()
        set_user_roles(db, user, role_ids)
        db.commit()
    except IntegrityError:
        db.rollback()
        return render(request, "admin_users.html", db, error=f"用户名 {username} 已存在。", status_code=400,
                      users=db.scalars(select(User).order_by(User.created_at.asc())).all(),
                      roles=db.scalars(select(Role).order_by(Role.id.asc())).all(),
                      user_role_map={}, role_name_map={}, edit_user=None)
    ledger_service.log_action(
        db, "manage_users", _user_id(request), "user", user.id,
        json.dumps({"operation": "create", "username": username}, ensure_ascii=False),
        _client_ip(request),
    )
    db.commit()
    return redirect_to("/admin/users")


@app.post("/admin/users/{user_id}/edit")
async def admin_user_update(user_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    user = get_or_404(db, User, user_id)
    form = await request.form()
    user.display_name = str(form.get("display_name", "")).strip()
    user.is_enabled = str(form.get("enabled", "")) == "on"
    role_ids = [int(v) for v in form.getlist("role_ids")]
    set_user_roles(db, user, role_ids)
    db.commit()
    ledger_service.log_action(
        db, "manage_users", _user_id(request), "user", user.id,
        json.dumps({"operation": "update", "username": user.username}, ensure_ascii=False),
        _client_ip(request),
    )
    db.commit()
    return redirect_to("/admin/users")


@app.post("/admin/users/{user_id}/password")
async def admin_user_password(user_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    user = get_or_404(db, User, user_id)
    form = await request.form()
    password = str(form.get("password", ""))
    if len(password) < 8:
        return render(
            request, "admin_users.html", db, error="密码至少 8 位。", status_code=400,
            users=db.scalars(select(User).order_by(User.created_at.asc())).all(),
            roles=db.scalars(select(Role).order_by(Role.id.asc())).all(),
            user_role_map={}, role_name_map={}, edit_user=user,
        )
    user.password_hash = hash_password(password)
    db.commit()
    return redirect_to("/admin/users")


@app.get("/admin/roles")
def admin_roles_page(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    roles = db.scalars(select(Role).order_by(Role.id.asc())).all()
    permissions = db.scalars(
        select(RolePermission).order_by(RolePermission.role_id.asc(), RolePermission.domain.asc())
    ).all()
    from .models import Permission

    permission_map = {p.id: p for p in db.scalars(select(Permission)).all()}
    return render(
        request,
        "admin_roles.html",
        db,
        roles=roles,
        permissions=permissions,
        permission_map=permission_map,
    )


@app.get("/admin/dictionaries")
def admin_dictionaries_page(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    categories = categories_with_items(db)
    return render(request, "admin_dictionaries.html", db, categories=categories)


# ---------------------------------------------------------------- 话术


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
    auth.require_login(request)
    edit_script = db.get(Script, edit_id) if edit_id else None
    return scripts_template(request, db, edit_script=edit_script)


@app.post("/scripts")
async def script_create(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    form = await request.form()
    title = str(form.get("title", "")).strip()
    body = str(form.get("body", "")).strip()
    wav_path = str(form.get("wav_path", "")).strip()
    form_data = {"title": title, "body": body, "wav_path": wav_path}
    if not title or not body:
        return scripts_template(request, db, "标题和话术内容必填。", form_data, status_code=400)
    db.add(Script(title=title, body=body, wav_path=wav_path))
    db.commit()
    return redirect_to("/scripts")


@app.post("/scripts/{script_id}/edit")
async def script_update(script_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    script = get_or_404(db, Script, script_id)
    form = await request.form()
    title = str(form.get("title", "")).strip()
    body = str(form.get("body", "")).strip()
    wav_path = str(form.get("wav_path", "")).strip()
    if not title or not body:
        return scripts_template(request, db, "标题和话术内容必填。", edit_script=script, status_code=400)
    script.title = title
    script.body = body
    script.wav_path = wav_path
    script.tts_status = "generated" if wav_path else script.tts_status
    db.commit()
    return redirect_to("/scripts")


@app.post("/scripts/{script_id}/delete")
def script_delete(script_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    script = get_or_404(db, Script, script_id)
    try:
        db.delete(script)
        db.commit()
    except IntegrityError:
        db.rollback()
        return scripts_template(
            request,
            db,
            "该话术被计划或通话记录引用，不能删除。",
            status_code=400,
        )
    return redirect_to("/scripts")


@app.post("/scripts/{script_id}/generate-audio")
def script_generate_audio(script_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    script = get_or_404(db, Script, script_id)
    generate_script_audio(db, script, settings)
    db.commit()
    return redirect_to("/scripts")


# ---------------------------------------------------------------- 回访计划


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
    auth.require_login(request)
    edit_plan = db.get(CallbackPlan, edit_id) if edit_id else None
    return plans_template(request, db, edit_plan=edit_plan)


@app.post("/plans")
async def plan_create(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
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
    auth.require_login(request)
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
    auth.require_login(request)
    plan = get_or_404(db, CallbackPlan, plan_id)
    try:
        db.delete(plan)
        db.commit()
    except IntegrityError:
        db.rollback()
        return plans_template(
            request,
            db,
            "该计划被排队任务或通话记录引用，不能删除。",
            status_code=400,
        )
    return redirect_to("/plans")


@app.post("/plans/{plan_id}/toggle")
def plan_toggle(plan_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    plan = get_or_404(db, CallbackPlan, plan_id)
    plan.enabled = not plan.enabled
    db.commit()
    return redirect_to("/plans")


@app.post("/plans/{plan_id}/call-now")
def plan_call_now(plan_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    plan = get_or_404(db, CallbackPlan, plan_id)
    plan_service.create_call_task(
        db,
        plan,
        due_at=utcnow(),
        status="queued",
        message="网页端发起立即拨打。",
        source="manual",
    )
    ledger_service.log_action(
        db, "dial", _user_id(request), "callback_plan", plan.id,
        json.dumps({"action": "call_now"}, ensure_ascii=False),
        _client_ip(request),
    )
    db.commit()
    return redirect_to("/calls")


# ---------------------------------------------------------------- 通话记录


@app.get("/calls")
def calls_page(request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    records = db.scalars(select(CallRecord).order_by(CallRecord.created_at.desc())).all()
    tasks = db.scalars(select(CallTask).order_by(CallTask.due_at.asc()).limit(20)).all()
    return render(request, "calls.html", db, records=records, tasks=tasks)


@app.get("/calls/{record_id}")
def call_detail(record_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    record = get_or_404(db, CallRecord, record_id)
    events = db.scalars(
        select(CallEvent)
        .where(CallEvent.call_record_id == record.id)
        .order_by(CallEvent.created_at.asc())
    ).all()
    return render(request, "call_detail.html", db, record=record, events=events)


@app.post("/calls/{record_id}/feedback")
async def call_feedback(record_id: int, request: Request, db: Session = Depends(get_db)):
    auth.require_login(request)
    record = get_or_404(db, CallRecord, record_id)
    form = await request.form()
    record.operator_feedback = str(form.get("operator_feedback", "")).strip()
    record.follow_up_required = str(form.get("follow_up_required", "")) == "on"
    db.commit()
    return redirect_to(f"/calls/{record.id}")
