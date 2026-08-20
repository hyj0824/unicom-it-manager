import pytest
from app.models import BusinessService, ChangeItem, ChangeSet
from app.services.scans import _change_set_business
from tests.flat_helpers import make_session


@pytest.fixture()
def db(tmp_path):
    session = make_session(tmp_path / "review.db")
    yield session
    session.close()


def test_review_stuck_resolves_flat_business_name(db):
    service = BusinessService(service_number="S1", customer_name="客户甲")
    db.add(service); db.flush()
    change = ChangeSet(title="审核", status="submitted", domain="business")
    db.add(change); db.flush(); db.add(ChangeItem(change_set_id=change.id, entity_type="business_service", entity_id=service.id, operation="update")); db.flush()
    assert _change_set_business(db, change).customer_name == "客户甲"
